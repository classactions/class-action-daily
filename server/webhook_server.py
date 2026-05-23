"""
webhook_server.py
-----------------
Receives CourtListener search-alert webhook events for new class action
filings, persists them to Postgres, and triggers downstream processing.

Design notes
------------

CourtListener webhooks (per their docs):
- POST only, application/json
- NO signature header. Security model is:
    1. Long random URL path (use the WEBHOOK_PATH env var)
    2. Source IP allowlist: 34.210.230.218, 54.189.59.91
- Idempotency-Key header for dedup; we must process each key at most once.
- Must return 2xx within ~10s or they retry (with backoff, up to 5 times).
- We respond 200 immediately, then process in a background task. If the
  background work fails, we record the error on the webhook_events row but
  do not signal CourtListener (since they already got their 200) -- the
  polling fallback in worker.py is the safety net.

Search alert payload shape (v2):
    {
        "webhook": {"event_type": 1, ...},
        "payload": {
            "results": [
                { ...same shape as /api/rest/v4/search/?type=r... },
                ...
            ]
        }
    }

Each result is a docket-level hit. We feed it through the same
classification logic the polling fetcher uses, then upsert into filings.

Run locally:
    export DATABASE_URL='postgres://...'
    export COURTLISTENER_TOKEN='...'
    export WEBHOOK_PATH='/hooks/long-random-string-here/'
    uvicorn webhook_server:app --host 0.0.0.0 --port 8080

On Fly.io: see fly.toml + Dockerfile in this directory.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from . import db
from .classifier import classify_search_hit


# ---------- Config -----------------------------------------------------

# Long random path. Set this in your environment; it's the only thing
# protecting your endpoint from random POST traffic.
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/hooks/please-set-WEBHOOK_PATH/")
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

# CourtListener's static source IPs (from their docs). If the request
# arrives from anywhere else we 403.
COURTLISTENER_IPS = {"34.210.230.218", "54.189.59.91"}

# Set to "0" to disable the IP check (useful for testing with ngrok or curl).
ENFORCE_IP_ALLOWLIST = os.environ.get("ENFORCE_IP_ALLOWLIST", "1") == "1"

log = logging.getLogger("webhook_server")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


# ---------- App --------------------------------------------------------

app = FastAPI(
    title="Class Action Daily — Webhook Receiver",
    docs_url=None,        # disable Swagger UI in prod
    redoc_url=None,
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe for Fly.io. Doesn't touch the DB."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    """Readiness probe — does touch the DB."""
    try:
        stats = db.get_stats(days_back=7)
        return {"status": "ok", "filings_last_7d": stats.get("total_filings", 0)}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# Trick: register the webhook handler at the dynamic path. We use a catch-all
# and check the path inside, so the same code can serve any configured URL.
@app.post("/{full_path:path}")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    full_path: str,
) -> Response:
    expected = WEBHOOK_PATH.strip("/")
    if full_path.strip("/") != expected:
        raise HTTPException(status_code=404, detail="Not found")

    # --- IP allowlist
    if ENFORCE_IP_ALLOWLIST:
        client_ip = _client_ip(request)
        if client_ip not in COURTLISTENER_IPS:
            log.warning("Webhook POST from disallowed IP %s", client_ip)
            raise HTTPException(status_code=403, detail="forbidden")

    # --- Idempotency
    idem = request.headers.get("Idempotency-Key", "").strip()
    if not idem:
        log.warning("Webhook POST without Idempotency-Key header")
        raise HTTPException(status_code=400, detail="missing Idempotency-Key")

    try:
        if db.webhook_already_seen(idem):
            # Spec says: same key, same response. Return 200 quickly so they
            # stop retrying.
            log.info("Duplicate webhook idem=%s — returning 200", idem)
            return Response(status_code=200, content="duplicate")
    except Exception as e:
        # If the DB is down we can't dedupe; better to refuse and let them retry
        # than to risk double-processing.
        log.exception("DB error during idempotency check")
        raise HTTPException(status_code=503, detail="db unavailable") from e

    # --- Parse + record (synchronously, before returning 200)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    event_type = (
        (payload.get("webhook") or {}).get("event_type")
        if isinstance(payload, dict) else None
    )
    try:
        db.record_webhook(idem, str(event_type or "unknown"), payload)
    except Exception:
        log.exception("Failed to record webhook")
        raise HTTPException(status_code=503, detail="db unavailable")

    # --- Process asynchronously and return 200 immediately
    background_tasks.add_task(_process_event, idem, payload)
    return Response(status_code=200, content="accepted")


# ---------- Event processing ------------------------------------------

def _process_event(idempotency_key: str, payload: dict[str, Any]) -> None:
    """
    Walk the payload's `results` array and upsert each docket as a filing.
    Runs in a background task -- we've already returned 200 to CL.
    """
    affected = 0
    err: str | None = None
    try:
        results = ((payload.get("payload") or {}).get("results")) or []
        for hit in results:
            row = classify_search_hit(hit, ingest_source="webhook")
            if row is None:
                continue
            db.upsert_filing(row)
            affected += 1
        log.info("idem=%s processed=%d filings", idempotency_key, affected)
    except Exception as e:
        log.exception("Error processing webhook idem=%s", idempotency_key)
        err = str(e)[:1000]
    finally:
        try:
            db.mark_webhook_processed(idempotency_key, affected, err)
        except Exception:
            log.exception("Failed to mark webhook processed")


# ---------- Helpers ----------------------------------------------------

def _client_ip(request: Request) -> str:
    """
    Get the originating client IP. On Fly.io, the real client IP is in
    Fly-Client-IP; the X-Forwarded-For chain works too. Fall back to the
    socket peer when running locally.
    """
    for hdr in ("Fly-Client-IP", "X-Forwarded-For"):
        v = request.headers.get(hdr)
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else ""
