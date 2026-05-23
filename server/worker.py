"""
worker.py
---------
Background worker that does three things:

  1. Polls CourtListener for new class action filings (safety net for
     webhook gaps).
  2. Pulls Law360 RSS feeds and matches them against known filings.
  3. Exports a flat enriched.json snapshot that the static front-end serves.

Run from cron / Fly.io scheduled machine / GitHub Actions:

    python -m server.worker poll       # one polling pass
    python -m server.worker law360     # one Law360 RSS pass
    python -m server.worker export     # rebuild enriched.json
    python -m server.worker all        # all three, in order
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

from . import db
from .classifier import classify_search_hit


COURTLISTENER_BASE = "https://www.courtlistener.com"
SEARCH_ENDPOINT = "/api/rest/v4/search/"


# ---------- CourtListener polling -------------------------------------

def cl_get(url: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Token {token}",
            "User-Agent": "ClassActionTracker/0.2 (worker)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def poll_courtlistener(token: str, days: int = 1, max_pages: int = 10) -> int:
    """Pull recent class action filings and upsert into Postgres."""
    filed_after = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    params = {
        "type": "r",
        "q": '"class action"',
        "filed_after": filed_after,
        "order_by": "dateFiled desc",
    }
    url = f"{COURTLISTENER_BASE}{SEARCH_ENDPOINT}?{urllib.parse.urlencode(params)}"

    inserted = updated = skipped = 0
    page = 0
    while page < max_pages and url:
        payload = cl_get(url, token)
        for hit in payload.get("results", []):
            row = classify_search_hit(hit, ingest_source="poll")
            if row is None:
                skipped += 1
                continue
            was_insert = db.upsert_filing(row)
            if was_insert:
                inserted += 1
            else:
                updated += 1
        url = payload.get("next") or ""
        page += 1
        time.sleep(0.5)

    print(f"poll: inserted={inserted} updated={updated} skipped={skipped}",
          file=sys.stderr)
    return inserted + updated


# ---------- Law360 RSS ------------------------------------------------

DOCKET_RE = re.compile(
    r'\b(?:No\.\s*)?(\d{1,2}:)?(\d{2})-(?:cv|cr|md|mc|mj)-(\d{3,5})(?:-[A-Z]{1,4})?\b',
    re.IGNORECASE,
)


def _find_dockets(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in DOCKET_RE.finditer(text or ""):
        d = re.sub(r"^No\.\s*", "", m.group(0), flags=re.IGNORECASE)
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _short_caption(case_name: str) -> str:
    s = (case_name or "").lower().strip()
    s = re.sub(r"^in re:?\s+", "", s)
    s = re.sub(r"\s+(securities\s+)?litigation\s*$", "", s)
    s = re.sub(r"\s+(class\s+action|matter)\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_docket(s: str) -> str:
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"-[a-z]{1,4}$", "", s)
    return re.sub(r"\s+", "", s)


def pull_law360_rss(feeds_config_path: str) -> int:
    """Pull all configured Law360 RSS feeds, persist items, and re-match."""
    with open(feeds_config_path) as f:
        cfg = json.load(f)

    items_seen = 0
    for section, url in (cfg.get("feeds") or {}).items():
        url = (url or "").strip()
        if not url:
            continue
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "ClassActionTracker/0.2 (subscriber RSS)"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
        except Exception as e:
            print(f"law360 {section!r}: {e}", file=sys.stderr)
            continue

        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            print(f"law360 {section!r}: XML parse error {e}", file=sys.stderr)
            continue

        channel = root.find("channel")
        if channel is None:
            continue
        for it in channel.findall("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            desc = (it.findtext("description") or "").strip()
            pub_raw = (it.findtext("pubDate") or "").strip()

            pub_iso = None
            if pub_raw:
                try:
                    pub_iso = parsedate_to_datetime(pub_raw).date().isoformat()
                except (TypeError, ValueError):
                    pass

            desc_clean = re.sub(r"<[^>]+>", "", desc).strip()
            blob = f"{title}\n{desc_clean}"
            dockets = _find_dockets(blob)

            if not link:
                continue
            db.upsert_law360_item({
                "url": link,
                "section": section,
                "title": title,
                "summary": desc_clean,
                "pub_date": pub_iso,
                "docket_numbers": dockets,
                "court_id_guess": "",
                "court_label_guess": "",
            })
            items_seen += 1
        time.sleep(0.5)

    matches = rematch_law360()
    print(f"law360: items_pulled={items_seen} matches_made={matches}",
          file=sys.stderr)
    return items_seen


def rematch_law360() -> int:
    """
    Re-compute Law360 matches for all filings from the last 14 days.
    Cheap to run; called after each RSS pull.
    """
    total = 0
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT docket_id, docket_number, case_name, date_filed
            FROM filings
            WHERE date_filed >= CURRENT_DATE - INTERVAL '14 days'
        """)
        filings = cur.fetchall()

        cur.execute("""
            SELECT url, title, summary, pub_date, docket_numbers
            FROM law360_items
            WHERE pub_date >= CURRENT_DATE - INTERVAL '21 days'
               OR pub_date IS NULL
        """)
        items = cur.fetchall()

    for f in filings:
        matches: list[dict[str, Any]] = []
        f_docket = _norm_docket(f["docket_number"] or "")
        f_caption = _short_caption(f["case_name"] or "")
        f_date = f["date_filed"]

        for it in items:
            # Tier 1: docket number match
            hit_conf = None
            for d in (it["docket_numbers"] or []):
                if _norm_docket(d) == f_docket and f_docket:
                    hit_conf = "docket"
                    break

            # Tier 2: caption fuzzy match within ±10 days
            if not hit_conf and f_caption and " v. " in f_caption:
                title_lower = (it["title"] or "").lower()
                summary_lower = (it["summary"] or "").lower()
                if f_caption in title_lower or f_caption in summary_lower:
                    pd = it["pub_date"]
                    if not pd or not f_date or abs((pd - f_date).days) <= 10:
                        hit_conf = "caption"

            if hit_conf:
                matches.append({
                    "law360_url": it["url"],
                    "confidence": hit_conf,
                })

        if matches:
            total += db.replace_law360_matches_for_docket(f["docket_id"], matches)
        else:
            db.replace_law360_matches_for_docket(f["docket_id"], [])
    return total


# ---------- Export for the front-end ----------------------------------

def export_enriched_json(out_path: str, days_back: int = 7) -> int:
    """Write the enriched.json snapshot the static front-end consumes."""
    rows = db.get_filings(days_back=days_back, limit=2000)
    stats = db.get_stats(days_back=days_back)

    def serialize(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "docket_id":          r["docket_id"],
            "case_name":          r["case_name"],
            "docket_number":      r["docket_number"],
            "court":              r["court"],
            "court_id":           r["court_id"],
            "date_filed":         r["date_filed"].isoformat() if r["date_filed"] else "",
            "nature_of_suit":     r["nature_of_suit"] or "",
            "nos_code":           r["nos_code"] or "",
            "cause":              r["cause"] or "",
            "parties_summary":    r["parties_summary"] or "",
            "courtlistener_url":  r["courtlistener_url"] or "",
            "complaint_url":      r["complaint_url"],
            "snippet":            r["snippet"] or "",
            "category":           r["category"],
            "subcategory":        r["subcategory"] or "",
            "category_source":    r["category_source"],
            "category_confidence": r["category_confidence"],
            "ingest_source":      r.get("ingest_source", "poll"),
            "law360_coverage":    r["law360_coverage"] or [],
        }

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "days_back": days_back,
        "count": len(rows),
        "stats": stats,
        "actions": [serialize(r) for r in rows],
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"export: wrote {len(rows)} filings to {out_path}", file=sys.stderr)
    return len(rows)


# ---------- CLI -------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["poll", "law360", "export", "all", "migrate"])
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--export-days", type=int, default=7)
    ap.add_argument("--out", default="enriched.json")
    ap.add_argument("--law360-config", default="law360_feeds.json")
    args = ap.parse_args()

    if args.command == "migrate":
        db.migrate()
        return 0

    if args.command in ("poll", "all"):
        token = os.environ.get("COURTLISTENER_TOKEN")
        if not token:
            print("ERROR: COURTLISTENER_TOKEN not set", file=sys.stderr)
            return 2
        poll_courtlistener(token, days=args.days)

    if args.command in ("law360", "all"):
        if os.path.exists(args.law360_config):
            pull_law360_rss(args.law360_config)
        else:
            print(f"(no {args.law360_config} found — skipping Law360 step)",
                  file=sys.stderr)

    if args.command in ("export", "all"):
        export_enriched_json(args.out, days_back=args.export_days)

    return 0


if __name__ == "__main__":
    sys.exit(main())
