# Class Action Daily — Federal Filings Tracker

A daily-feed website that pulls newly-filed federal class actions from
CourtListener's RECAP archive (PACER mirror), enriches them with Law360
coverage where available, categorizes them, and exposes the complaint
PDFs for viewing or download.

Targets **plaintiffs' firms** with a **CourtListener API token** and a
**Law360 subscription**. Covers **federal courts only**.

## Two ways to run it

### Prototype mode (local, file-based)

The original three-script pipeline from the prototype phase still works
end-to-end and is the fastest way to see results:

```bash
export COURTLISTENER_TOKEN="..."
python fetch_class_actions.py --days 1     # → class_actions.json
python fetch_law360_rss.py                  # → law360_items.json
python enrich.py                            # → enriched.json
python -m http.server 8000
```

Open http://localhost:8000/ — the UI picks up `enriched.json`
automatically. The Internal/Public toggle in the masthead controls
whether Law360 enrichment is visible. See the earlier sections of this
file for details.

### Production mode (Postgres + webhook server)

The full stack adds three things on top of the prototype:

1. **Postgres persistence** — filings live forever, not just one day
2. **Webhook receiver** — CourtListener pushes new class actions in
   near real-time
3. **Worker** — polls as a safety net, pulls Law360 RSS, exports the
   static `enriched.json` the front-end consumes

See `DEPLOY.md` for the step-by-step runbook to get this running on
**Neon (or Supabase) + Fly.io** in about 30 minutes.

## Repository layout

```
class_action_tracker/
├── index.html                    # static front-end (both modes)
├── enriched.json                 # generated; gitignored in production
│
├── fetch_class_actions.py        # PROTOTYPE: CL fetcher → class_actions.json
├── fetch_law360_rss.py           # PROTOTYPE: L360 fetcher → law360_items.json
├── enrich.py                     # PROTOTYPE: joiner → enriched.json
├── law360_feeds.json             # config: RSS URLs (do not commit)
│
├── server/                       # PRODUCTION: webhook + DB + worker
│   ├── __init__.py
│   ├── classifier.py             # rule-based categorization (shared)
│   ├── db.py                     # Postgres access layer
│   ├── webhook_server.py         # FastAPI receiver for CL webhooks
│   ├── worker.py                 # poll + L360 + export, runs from cron
│   └── requirements.txt
│
├── migrations/
│   └── 0001_initial.sql          # Postgres schema
│
├── Dockerfile                    # for fly.io deploy of webhook_server
├── fly.toml                      # fly.io app config
├── DEPLOY.md                     # production deployment runbook
└── README.md                     # this file
```

## Configuration

### CourtListener API token

Get one at https://www.courtlistener.com/sign-in/ (free; required for the
search API). Set as `COURTLISTENER_TOKEN`.

### Law360 RSS feed URLs

Per-section RSS URLs are personal subscriber credentials. Get them by:

1. Logging in at https://www.law360.com
2. Visiting each section page (e.g. `/classaction`, `/securities`,
   `/consumerprotection`, `/cybersecurity-privacy`, `/employment`,
   `/benefits`, `/productliability`, `/competition`)
3. Clicking the orange RSS button at the top right
4. Pasting the URL into `law360_feeds.json`

**Do not commit this file to a public repo.** Keep it outside the
repo entirely in production, and pass `--config /secrets/law360_feeds.json`
to the worker.

### Database (production)

Set `DATABASE_URL` to a Postgres connection string. Works with:

- **Neon** (recommended): `postgres://...@ep-xxx.neon.tech/db?sslmode=require`
- **Supabase**: use the pooler URL on port 6543 for app traffic
- **Local Postgres** for development

## Categorization

Two-pass rule-based, in `server/classifier.py`:

1. **Nature-of-suit code** (AO JS-44) maps to a top-level category:
   - 850 → Securities
   - 410 → Antitrust
   - 710 → Wage & Hour (FLSA)
   - 791 → ERISA
   - 442/446 → Civil Rights
   - 480 → Consumer
   - 890 → Privacy/TCPA/Data (placeholder; refined by keywords)

2. **Keyword refinement** on case name + complaint snippet:
   - "data breach" → Privacy / Data · Data Breach (0.9 confidence)
   - "BIPA" → Privacy / Data · BIPA / Biometric Privacy (0.9)
   - "TCPA" → Privacy / Data · TCPA (0.9)
   - "Meta Pixel" → Privacy / Data · VPPA / Pixel Tracking (0.9)
   - "price-fixing" → Antitrust (0.9)
   - "securities fraud" → Securities (0.9)
   - etc.

Each categorization gets a **confidence score** (0.0–1.0) that's
exposed in the UI. The front-end shows a "low conf." chip on any row
with confidence < 0.5, so you know which ones to spot-check against
the underlying complaint.

The schema includes a `needs_llm_review` boolean that the future
LLM classifier (planned, not yet built) will key off of. Smart-hybrid
plan: only call the LLM when the rules return "Other" or low
confidence; that keeps costs to ~$1-3/day at expected volumes.

## View modes (Internal vs Public)

The masthead toggle controls visibility of Law360 enrichment.

- **Internal** (default): teal "L360" chips on rows with Law360
  coverage, full coverage panel in the drawer with article summaries
  and subscriber links.
- **Public**: all Law360 enrichment hidden. A banner across the top
  marks it as public mode. Use this when serving the site to non-
  subscribers, since Law360 article content is licensed.

When you go fully public, either:
1. Remove the toggle and force public mode in `index.html`, or
2. Put the Internal view behind firm SSO and serve Public to everyone
   else.

The toggle is a UI convenience, not a security boundary.

## Webhook architecture

CourtListener delivers webhooks per their docs:
- No signature header — security is via a long random URL path +
  source IP allowlist (`34.210.230.218`, `54.189.59.91`)
- `Idempotency-Key` header for dedup
- POST returns 2xx within ~10s or they retry; we return 200 fast and
  process in a background task

The flow:

```
CourtListener  --POST-->  Fly.io webhook_server  --INSERT-->  Postgres
                                                                 |
                              Worker (cron)  ----poll, L360, export
                                                                 |
                                                          enriched.json
                                                                 |
                                                            index.html
```

The static front-end never touches the database. The worker exports a
flat `enriched.json` after each refresh, which the front-end loads.

## Known limitations

- **CourtListener RECAP is not real-time.** Complaints are added when
  somebody pulls them from PACER. Webhooks fire when new search results
  appear, but search indexing lags PACER itself by 24-72h for many
  filings. Real same-day coverage requires PACER direct (paid).
- **"Class action" is a heuristic.** We require the phrase or close
  variant in the case caption or first-page text.
- **Federal only.** State-court class actions (a large chunk of
  consumer and wage-and-hour) are not covered.
- **CourtListener charges for high-volume webhooks.** Plan for a
  conversation with Free Law Project about pricing once your traffic
  picks up.

## Legal / ToS notes

- Federal complaints are public records once filed; hosting copies is fine.
- CourtListener's API has rate-limit and citation-attribution expectations.
- Law360 content is licensed and cannot be republished. RSS subscription
  content is for personal/firm use; the Public view in this tool is
  designed to keep you on the right side of that line.
- PACER's terms prohibit some bulk redistribution.
- This is not legal advice; consult your own counsel.
