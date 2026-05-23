"""
db.py
-----
Postgres access for Class Action Daily.

This module is the only place that talks to the database. Everything else
(webhook server, polling fetcher, exporter) goes through these functions.

Designed to work against:
- Local Postgres (development)
- Neon (production)
- Supabase (production alternative)

Connection string is read from the DATABASE_URL environment variable, e.g.:
    postgres://user:pass@host/dbname?sslmode=require

Uses psycopg 3 (the modern replacement for psycopg2).
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row


# -------- Connection management ----------------------------------------

def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Example for Neon:\n"
            "  export DATABASE_URL='postgres://user:pass@ep-xyz.neon.tech/dbname?sslmode=require'"
        )
    # Neon and Supabase URLs sometimes come back as `postgresql://`; both work.
    return url


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """
    Yield a psycopg connection with dict_row factory (so fetchall returns dicts).
    Caller is responsible for committing; we wrap in a context manager that
    rolls back on exception.
    """
    conn = psycopg.connect(database_url(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# -------- Schema migrations --------------------------------------------

def migrate() -> None:
    """
    Apply all SQL files in ./migrations in lexical order. Idempotent because
    every CREATE uses IF NOT EXISTS and we record applied migrations.
    """
    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.exists():
        # When deployed from a subdirectory layout, the migrations dir lives
        # alongside this file. Otherwise look one level up.
        migrations_dir = Path(__file__).parent.parent / "migrations"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("SELECT filename FROM schema_migrations")
        applied = {r["filename"] for r in cur.fetchall()}

        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in applied:
                continue
            print(f"Applying migration {path.name}...")
            sql = path.read_text()
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)",
                (path.name,),
            )
        print("Migrations complete.")


# -------- Filings -------------------------------------------------------

# The columns we accept on upsert. Keep this list in sync with migrations.
FILING_COLS = [
    "docket_id", "case_name", "docket_number", "court_id", "court",
    "date_filed", "nature_of_suit", "nos_code", "cause", "parties_summary",
    "courtlistener_url", "complaint_url", "snippet",
    "category", "subcategory", "category_source", "category_confidence",
    "needs_llm_review", "ingest_source", "categorize_hash",
]


def categorize_hash(row: dict[str, Any]) -> str:
    """
    Stable hash of the inputs used for categorization. If this changes, the
    LLM worker knows to re-categorize the row even if the docket_id matches.
    """
    blob = json.dumps({
        "case_name": row.get("case_name", ""),
        "snippet":   row.get("snippet", ""),
        "nos_code":  row.get("nos_code", ""),
        "cause":     row.get("cause", ""),
    }, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def upsert_filing(row: dict[str, Any]) -> bool:
    """
    Insert or update a filing. Returns True if a new row was inserted,
    False if an existing row was updated.

    Idempotent: calling this twice with the same docket_id is safe.
    """
    if not row.get("docket_id"):
        raise ValueError("upsert_filing requires docket_id")

    row = dict(row)  # don't mutate caller
    row.setdefault("category", "Other")
    row.setdefault("category_source", "rule")
    row.setdefault("category_confidence", 0.6)
    row.setdefault("needs_llm_review", row.get("category") == "Other")
    row.setdefault("ingest_source", "poll")
    row["categorize_hash"] = categorize_hash(row)

    cols = [c for c in FILING_COLS if c in row]
    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    # On conflict, update everything except docket_id and created_at.
    update_cols = [c for c in cols if c != "docket_id"]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    sql = f"""
        INSERT INTO filings ({col_list}, updated_at)
        VALUES ({placeholders}, NOW())
        ON CONFLICT (docket_id) DO UPDATE SET
            {update_clause},
            updated_at = NOW()
        RETURNING (xmax = 0) AS inserted
    """
    values = [row[c] for c in cols]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, values)
        result = cur.fetchone()
        return bool(result and result["inserted"])


def get_filings(
    days_back: int = 7,
    category: str | None = None,
    court_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Read filings out of the enriched view."""
    where = ["date_filed >= CURRENT_DATE - INTERVAL '%s days'" % int(days_back)]
    params: list[Any] = []
    if category:
        where.append("category = %s")
        params.append(category)
    if court_id:
        where.append("court_id = %s")
        params.append(court_id)
    sql = f"""
        SELECT * FROM v_filings_enriched
        WHERE {' AND '.join(where)}
        ORDER BY date_filed DESC, docket_id DESC
        LIMIT {int(limit)}
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# -------- Webhook idempotency ------------------------------------------

def webhook_already_seen(idempotency_key: str) -> bool:
    """Return True if we've already processed this event."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM webhook_events WHERE idempotency_key = %s",
            (idempotency_key,),
        )
        return cur.fetchone() is not None


def record_webhook(
    idempotency_key: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Record a webhook event as received (but not yet processed)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO webhook_events (idempotency_key, event_type, payload)
            VALUES (%s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (idempotency_key, event_type, json.dumps(payload)),
        )


def mark_webhook_processed(
    idempotency_key: str,
    filings_affected: int = 0,
    error: str | None = None,
) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE webhook_events
            SET processed_at = NOW(),
                filings_affected = %s,
                error = %s
            WHERE idempotency_key = %s
            """,
            (filings_affected, error, idempotency_key),
        )


# -------- Law360 -------------------------------------------------------

def upsert_law360_item(item: dict[str, Any]) -> None:
    """Insert/update a Law360 RSS item."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO law360_items (
                url, section, title, summary, pub_date,
                docket_numbers, court_id_guess, court_label_guess
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (url) DO UPDATE SET
                section = EXCLUDED.section,
                title = EXCLUDED.title,
                summary = EXCLUDED.summary,
                pub_date = EXCLUDED.pub_date,
                docket_numbers = EXCLUDED.docket_numbers,
                court_id_guess = EXCLUDED.court_id_guess,
                court_label_guess = EXCLUDED.court_label_guess
            """,
            (
                item["url"],
                item.get("section", ""),
                item.get("title", ""),
                item.get("summary", ""),
                item.get("pub_date") or None,
                item.get("docket_numbers") or [],
                item.get("court_id_guess", ""),
                item.get("court_label_guess", ""),
            ),
        )


def replace_law360_matches_for_docket(
    docket_id: int, matches: list[dict[str, Any]]
) -> int:
    """
    Replace all Law360 matches for a single docket. Returns the number of
    matches inserted.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM law360_matches WHERE docket_id = %s", (docket_id,)
        )
        n = 0
        for m in matches:
            cur.execute(
                """
                INSERT INTO law360_matches (docket_id, law360_url, confidence)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (docket_id, m["law360_url"], m.get("confidence", "caption")),
            )
            n += 1
        return n


# -------- Stats --------------------------------------------------------

def get_stats(days_back: int = 7) -> dict[str, Any]:
    """A few headline numbers for the front-end masthead."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS total_filings,
                COUNT(DISTINCT category) AS distinct_categories,
                COUNT(DISTINCT court_id) AS distinct_courts,
                COUNT(*) FILTER (WHERE needs_llm_review) AS needs_llm
            FROM filings
            WHERE date_filed >= CURRENT_DATE - INTERVAL '%s days'
            """ % int(days_back)
        )
        return dict(cur.fetchone() or {})


if __name__ == "__main__":
    # `python db.py migrate` runs migrations
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        migrate()
    else:
        print(__doc__)
