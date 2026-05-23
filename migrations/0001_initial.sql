-- migrations/0001_initial.sql
-- Initial schema for Class Action Daily.
-- Compatible with Postgres 14+. Designed to also run on Neon and Supabase.

-- =========================================================================
-- filings: one row per federal docket we've identified as a class action.
-- =========================================================================
CREATE TABLE IF NOT EXISTS filings (
    -- CourtListener's docket_id is the primary key. It's stable across
    -- updates to the same docket, which makes upserts trivial.
    docket_id          BIGINT       PRIMARY KEY,

    -- Core docket metadata (from CourtListener search API)
    case_name          TEXT         NOT NULL,
    docket_number      TEXT         NOT NULL,
    court_id           TEXT         NOT NULL,
    court              TEXT,
    date_filed         DATE,
    nature_of_suit     TEXT,
    nos_code           TEXT,
    cause              TEXT,
    parties_summary    TEXT,
    courtlistener_url  TEXT,
    complaint_url      TEXT,
    snippet            TEXT,

    -- Categorization (rule-based today; LLM-augmented later)
    category           TEXT         NOT NULL DEFAULT 'Other',
    subcategory        TEXT,
    -- 'rule' = keyword/NOS heuristic; 'llm' = LLM-assigned; 'manual' = user override.
    category_source    TEXT         NOT NULL DEFAULT 'rule',
    -- 0.0-1.0; rule-based categorizations set this based on whether they had
    -- a keyword hit (0.9) or fell through to NOS code (0.6) or to Other (0.2).
    category_confidence REAL        NOT NULL DEFAULT 0.6,
    -- True when the rule classifier was uncertain and an LLM pass would help.
    -- The future categorize_llm.py worker reads this flag.
    needs_llm_review   BOOLEAN      NOT NULL DEFAULT FALSE,
    -- Optional: free-text reasoning produced by the LLM classifier.
    category_reasoning TEXT,

    -- Provenance + housekeeping
    -- 'webhook', 'poll', 'manual'
    ingest_source      TEXT         NOT NULL DEFAULT 'poll',
    -- Hash of the inputs we used for categorization, so we can re-run
    -- categorization when the schema changes without re-categorizing
    -- unchanged rows.
    categorize_hash    TEXT,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS filings_date_filed_idx     ON filings (date_filed DESC);
CREATE INDEX IF NOT EXISTS filings_court_id_idx       ON filings (court_id);
CREATE INDEX IF NOT EXISTS filings_category_idx       ON filings (category);
CREATE INDEX IF NOT EXISTS filings_needs_llm_idx      ON filings (needs_llm_review)
    WHERE needs_llm_review = TRUE;
CREATE INDEX IF NOT EXISTS filings_updated_at_idx     ON filings (updated_at DESC);

-- =========================================================================
-- webhook_events: idempotency log. CourtListener delivers at-least-once and
-- includes an Idempotency-Key header; we record it and refuse to process
-- the same key twice.
-- =========================================================================
CREATE TABLE IF NOT EXISTS webhook_events (
    idempotency_key    TEXT         PRIMARY KEY,
    event_type         TEXT         NOT NULL,         -- e.g. 'search_alert'
    received_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    processed_at       TIMESTAMPTZ,
    -- Full raw payload, for replay/debugging
    payload            JSONB        NOT NULL,
    -- How many filings did this event add or update?
    filings_affected   INT          NOT NULL DEFAULT 0,
    error              TEXT
);

CREATE INDEX IF NOT EXISTS webhook_events_received_idx ON webhook_events (received_at DESC);
CREATE INDEX IF NOT EXISTS webhook_events_unprocessed_idx ON webhook_events (received_at)
    WHERE processed_at IS NULL;

-- =========================================================================
-- law360_items: every Law360 RSS item we've seen, regardless of whether we
-- matched it to a filing yet.
-- =========================================================================
CREATE TABLE IF NOT EXISTS law360_items (
    -- We don't have a stable Law360 ID from RSS; URL is the closest thing.
    url                TEXT         PRIMARY KEY,
    section            TEXT         NOT NULL,
    title              TEXT         NOT NULL,
    summary            TEXT,
    pub_date           DATE,
    docket_numbers     TEXT[]       NOT NULL DEFAULT '{}',
    court_id_guess     TEXT,
    court_label_guess  TEXT,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS law360_items_pub_date_idx ON law360_items (pub_date DESC);

-- =========================================================================
-- law360_matches: join table between filings and law360 items.
-- A single filing can have multiple Law360 articles; a single article can
-- reference multiple dockets.
-- =========================================================================
CREATE TABLE IF NOT EXISTS law360_matches (
    docket_id          BIGINT       NOT NULL REFERENCES filings(docket_id) ON DELETE CASCADE,
    law360_url         TEXT         NOT NULL REFERENCES law360_items(url)  ON DELETE CASCADE,
    -- 'docket' (high-confidence docket-number match) or 'caption' (medium)
    confidence         TEXT         NOT NULL,
    matched_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (docket_id, law360_url)
);

CREATE INDEX IF NOT EXISTS law360_matches_docket_idx ON law360_matches (docket_id);

-- =========================================================================
-- Convenience view: filings joined with their Law360 coverage as JSON.
-- The export script reads from this; the front-end never touches the DB.
-- =========================================================================
CREATE OR REPLACE VIEW v_filings_enriched AS
SELECT
    f.*,
    COALESCE(
        (
            SELECT jsonb_agg(jsonb_build_object(
                'section',     l.section,
                'title',       l.title,
                'summary',     l.summary,
                'url',         l.url,
                'pub_date',    l.pub_date,
                'confidence',  m.confidence
            ) ORDER BY l.pub_date DESC NULLS LAST)
            FROM law360_matches m
            JOIN law360_items   l ON l.url = m.law360_url
            WHERE m.docket_id = f.docket_id
        ),
        '[]'::jsonb
    ) AS law360_coverage
FROM filings f;
