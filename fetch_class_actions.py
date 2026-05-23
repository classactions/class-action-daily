#!/usr/bin/env python3
"""
fetch_class_actions.py
----------------------
Pulls recently-filed federal class actions from CourtListener's v4 RECAP search
API, categorizes them, and writes a JSON file the front-end can consume.

Usage:
    export COURTLISTENER_TOKEN="your_token_here"
    python fetch_class_actions.py [--days 1] [--out class_actions.json]

Get a free token at https://www.courtlistener.com/sign-in/ (then your profile).

Notes on coverage:
- "Class action" is not a single filter at the docket level. We use a
  combination of (a) nature-of-suit codes that are class-action-heavy,
  (b) the free-text query "class action" against case name + complaint text,
  and (c) post-filter on case name keywords. This is a prototype heuristic;
  production would also parse the complaint PDF for FRCP 23 language.
- CourtListener's RECAP archive is crowd-sourced from PACER, so coverage is
  best-effort. Some same-day filings may take 24-72h to appear.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any

import urllib.request
import urllib.parse
import urllib.error


COURTLISTENER_BASE = "https://www.courtlistener.com"
SEARCH_ENDPOINT = "/api/rest/v4/search/"


# Nature-of-suit codes that disproportionately contain class actions.
# Source: AO JS-44 Civil Cover Sheet codes.
# We include the code, a human label, and the high-level category we map it to.
NOS_CATEGORIES: dict[str, tuple[str, str]] = {
    # Securities / shareholder
    "850": ("Securities/Commodities/Exchange", "Securities"),
    # Antitrust
    "410": ("Antitrust", "Antitrust"),
    # Consumer protection / credit
    "480": ("Consumer Credit", "Consumer"),
    "375": ("False Claims Act", "Consumer"),
    "370": ("Other Fraud", "Consumer"),
    "371": ("Truth in Lending", "Consumer"),
    # Labor & employment
    "710": ("Fair Labor Standards Act", "Wage & Hour"),
    "740": ("Railway Labor Act", "Labor"),
    "751": ("Family and Medical Leave Act", "Labor"),
    "790": ("Other Labor Litigation", "Labor"),
    "791": ("ERISA", "ERISA"),
    "442": ("Civil Rights — Employment", "Civil Rights"),
    # Civil rights
    "440": ("Other Civil Rights", "Civil Rights"),
    "443": ("Housing/Accommodations", "Civil Rights"),
    "445": ("Amer. w/ Disabilities — Employment", "Civil Rights"),
    "446": ("Amer. w/ Disabilities — Other", "Civil Rights"),
    "448": ("Education", "Civil Rights"),
    # Privacy / TCPA / data — these often ride under "Other Statutory Actions"
    "890": ("Other Statutory Actions", "Privacy/TCPA/Data"),
    # Products
    "365": ("Personal Injury — Product Liability", "Product Liability"),
    "367": ("Health Care/Pharmaceutical Personal Injury", "Product Liability"),
    "368": ("Asbestos Personal Injury Product Liability", "Product Liability"),
    # Environmental
    "893": ("Environmental Matters", "Environmental"),
}


# Keyword refinements applied to case name + snippet to refine categorization
# beyond NOS codes. Order matters: first match wins.
KEYWORD_CATEGORIES: list[tuple[str, str]] = [
    ("data breach", "Data Breach"),
    ("data security incident", "Data Breach"),
    ("cybersecurity incident", "Data Breach"),
    ("biometric", "BIPA / Biometric Privacy"),
    ("bipa", "BIPA / Biometric Privacy"),
    ("telephone consumer protection", "TCPA"),
    ("tcpa", "TCPA"),
    ("fair credit reporting", "FCRA"),
    ("fcra", "FCRA"),
    ("wage and hour", "Wage & Hour"),
    ("flsa", "Wage & Hour"),
    ("unpaid overtime", "Wage & Hour"),
    ("securities fraud", "Securities"),
    ("10b-5", "Securities"),
    ("erisa", "ERISA"),
    ("antitrust", "Antitrust"),
    ("price-fixing", "Antitrust"),
    ("price fixing", "Antitrust"),
    ("monopoliz", "Antitrust"),
    ("false advertising", "Consumer / False Advertising"),
    ("deceptive", "Consumer / False Advertising"),
    ("mislabel", "Consumer / False Advertising"),
    ("video privacy protection", "VPPA / Pixel Tracking"),
    ("vppa", "VPPA / Pixel Tracking"),
    ("meta pixel", "VPPA / Pixel Tracking"),
]


@dataclass
class ClassAction:
    case_name: str
    docket_number: str
    court: str
    court_id: str
    date_filed: str
    nature_of_suit: str
    nos_code: str
    category: str
    subcategory: str
    cause: str
    parties_summary: str
    courtlistener_url: str
    complaint_url: str | None
    snippet: str
    docket_id: int


def http_get(url: str, token: str) -> dict[str, Any]:
    """Authenticated GET against CourtListener's REST API."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Token {token}",
            "User-Agent": "ClassActionTracker/0.1 (prototype)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} from {url}\n{body}") from e


def categorize(case_name: str, snippet: str, nos_code: str) -> tuple[str, str]:
    """
    Returns (category, subcategory).
    Subcategory comes from keyword match if available, else the NOS label.
    """
    text = f"{case_name} {snippet}".lower()
    for kw, subcat in KEYWORD_CATEGORIES:
        if kw in text:
            # Map keyword subcategory back to a high-level category
            top = subcat
            if subcat in ("TCPA", "FCRA", "BIPA / Biometric Privacy",
                          "VPPA / Pixel Tracking", "Data Breach"):
                top = "Privacy / Data"
            elif subcat in ("Consumer / False Advertising",):
                top = "Consumer"
            elif subcat in ("Wage & Hour",):
                top = "Wage & Hour"
            elif subcat in ("ERISA",):
                top = "ERISA"
            elif subcat in ("Securities",):
                top = "Securities"
            elif subcat in ("Antitrust",):
                top = "Antitrust"
            return (top, subcat)

    if nos_code in NOS_CATEGORIES:
        label, top = NOS_CATEGORIES[nos_code]
        return (top, label)

    return ("Other", "Other")


def looks_like_class_action(case_name: str, snippet: str, cause: str) -> bool:
    """
    Heuristic: a docket is treated as a class action if its case name or
    snippet contains class-action language, OR if its NOS+cause strongly
    suggest one (handled at the query level). This is intentionally
    permissive for the prototype.
    """
    blob = f"{case_name} {snippet} {cause}".lower()
    triggers = [
        "class action",
        "on behalf of all others",
        "fed. r. civ. p. 23",
        "rule 23",
        "putative class",
        "collective action",  # FLSA collectives
    ]
    return any(t in blob for t in triggers)


def fetch_page(token: str, params: dict[str, str]) -> dict[str, Any]:
    qs = urllib.parse.urlencode(params, doseq=True)
    url = f"{COURTLISTENER_BASE}{SEARCH_ENDPOINT}?{qs}"
    return http_get(url, token)


def search_class_actions(
    token: str, days: int = 1, max_pages: int = 10
) -> list[ClassAction]:
    """
    Pulls recently-filed RECAP dockets matching class-action language.
    """
    today = dt.date.today()
    filed_after = (today - dt.timedelta(days=days)).isoformat()

    params = {
        "type": "r",                          # RECAP dockets
        "q": '"class action"',                # phrase search
        "filed_after": filed_after,
        "order_by": "dateFiled desc",
    }

    results: list[ClassAction] = []
    next_url: str | None = None
    page = 0

    while page < max_pages:
        if next_url:
            payload = http_get(next_url, token)
        else:
            payload = fetch_page(token, params)

        for hit in payload.get("results", []):
            # The v4 RECAP search returns docket-level hits with nested
            # recap_documents. Field names per CL v4 docs.
            case_name = hit.get("caseName") or hit.get("case_name") or ""
            docket_number = hit.get("docketNumber") or hit.get("docket_number") or ""
            court = hit.get("court") or hit.get("court_citation_string") or ""
            court_id = hit.get("court_id") or ""
            date_filed = hit.get("dateFiled") or hit.get("date_filed") or ""
            nos = hit.get("suitNature") or hit.get("nature_of_suit") or ""
            cause = hit.get("cause") or ""
            snippet = ""
            # snippets live on the nested docs in RECAP search
            docs = hit.get("recap_documents") or []
            if docs:
                snippet = docs[0].get("snippet") or ""

            nos_code = ""
            if nos and nos.split()[0].isdigit():
                nos_code = nos.split()[0]

            if not looks_like_class_action(case_name, snippet, cause):
                continue

            category, subcat = categorize(case_name, snippet, nos_code)

            docket_id = hit.get("docket_id") or 0
            abs_url = hit.get("docket_absolute_url") or f"/docket/{docket_id}/"
            courtlistener_url = f"{COURTLISTENER_BASE}{abs_url}"

            # Try to surface a complaint PDF if one is in the nested docs.
            complaint_url = None
            for d in docs:
                desc = (d.get("description") or "").lower()
                short = (d.get("short_description") or "").lower()
                if "complaint" in desc or "complaint" in short:
                    fp = d.get("filepath_local")
                    if fp:
                        complaint_url = f"{COURTLISTENER_BASE}{fp}" if fp.startswith("/") else fp
                        break

            parties = hit.get("party") or []
            parties_summary = "; ".join(parties[:4]) if isinstance(parties, list) else ""

            results.append(ClassAction(
                case_name=case_name.strip(),
                docket_number=docket_number,
                court=court,
                court_id=court_id,
                date_filed=date_filed,
                nature_of_suit=nos,
                nos_code=nos_code,
                category=category,
                subcategory=subcat,
                cause=cause,
                parties_summary=parties_summary,
                courtlistener_url=courtlistener_url,
                complaint_url=complaint_url,
                snippet=snippet[:400],
                docket_id=int(docket_id) if docket_id else 0,
            ))

        next_url = payload.get("next")
        if not next_url:
            break
        page += 1
        time.sleep(0.5)  # be polite to the API

    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=1,
                    help="How many days back to search (default: 1)")
    ap.add_argument("--out", default="class_actions.json",
                    help="Output JSON file (default: class_actions.json)")
    ap.add_argument("--max-pages", type=int, default=10,
                    help="Max pages of results to pull (default: 10)")
    args = ap.parse_args()

    token = os.environ.get("COURTLISTENER_TOKEN")
    if not token:
        print("ERROR: set COURTLISTENER_TOKEN env var. "
              "Get one at https://www.courtlistener.com/sign-in/",
              file=sys.stderr)
        return 2

    print(f"Fetching class actions filed in the last {args.days} day(s)...",
          file=sys.stderr)
    rows = search_class_actions(token, days=args.days, max_pages=args.max_pages)
    print(f"Found {len(rows)} likely class actions.", file=sys.stderr)

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "days_back": args.days,
        "count": len(rows),
        "actions": [asdict(r) for r in rows],
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
