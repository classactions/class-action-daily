#!/usr/bin/env python3
"""
enrich.py
---------
Joins CourtListener docket data (class_actions.json) with Law360 RSS items
(law360_items.json) to produce the merged dataset the front-end consumes
(enriched.json).

Match strategy, in order of confidence:

  1. Exact docket-number match. If a Law360 item's extracted docket number
     equals (or normalizes to) a CourtListener docket_number, that's a hit.
  2. Case-caption fuzzy match. If the lowercased "X v. Y" portion of the
     CourtListener case_name appears as a substring of the Law360 title,
     and the dates are within ±10 days, we treat it as a hit.

Each enriched docket gets a `law360_coverage` list with the matching Law360
items. The front-end shows these only in "Internal" view mode.

If law360_items.json is absent, the script still runs and produces an
enriched.json that's identical to class_actions.json (plus empty coverage
arrays). This is the right behavior for the public-mode build.

Usage:
    python enrich.py [--ca class_actions.json] [--l360 law360_items.json] \
                     [--out enriched.json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from typing import Any


def norm_docket(s: str) -> str:
    """Normalize docket numbers for comparison: lowercase, strip judge suffix."""
    if not s:
        return ""
    s = s.lower().strip()
    # Drop trailing -XYZ judge initials
    s = re.sub(r"-[a-z]{1,4}$", "", s)
    # Collapse multiple spaces
    s = re.sub(r"\s+", "", s)
    return s


def short_caption(case_name: str) -> str:
    """
    Return a normalized 'X v. Y' caption for fuzzy matching.
    Strips 'In re' prefixes and trailing 'Litigation', 'Securities Litigation', etc.
    """
    if not case_name:
        return ""
    s = case_name.lower().strip()
    s = re.sub(r"^in re:?\s+", "", s)
    s = re.sub(r"\s+(securities\s+)?litigation\s*$", "", s)
    s = re.sub(r"\s+(class\s+action|matter)\s*$", "", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_date(s: str) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def days_between(a: str, b: str) -> int | None:
    da, db = parse_date(a), parse_date(b)
    if not da or not db:
        return None
    return abs((da - db).days)


def find_matches(
    ca_row: dict[str, Any], law360_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    ca_docket = norm_docket(ca_row.get("docket_number", ""))
    ca_caption = short_caption(ca_row.get("case_name", ""))
    ca_date = ca_row.get("date_filed", "")

    for it in law360_items:
        # Tier 1: docket-number match
        hit = False
        confidence = ""
        for d in (it.get("docket_numbers") or []):
            if norm_docket(d) == ca_docket and ca_docket:
                hit = True
                confidence = "docket"
                break

        # Tier 2: caption fuzzy match, gated on date proximity (±10 days)
        if not hit and ca_caption and " v. " in ca_caption:
            title_lower = (it.get("title") or "").lower()
            summary_lower = (it.get("summary") or "").lower()
            if ca_caption in title_lower or ca_caption in summary_lower:
                gap = days_between(ca_date, it.get("pub_date", ""))
                if gap is None or gap <= 10:
                    hit = True
                    confidence = "caption"

        if hit:
            matches.append({
                "section": it.get("section"),
                "title": it.get("title"),
                "summary": it.get("summary"),
                "url": it.get("url"),
                "pub_date": it.get("pub_date"),
                "confidence": confidence,
            })
    return matches


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ca", default="class_actions.json",
                    help="CourtListener output from fetch_class_actions.py")
    ap.add_argument("--l360", default="law360_items.json",
                    help="Law360 RSS output from fetch_law360_rss.py")
    ap.add_argument("--out", default="enriched.json")
    args = ap.parse_args()

    if not os.path.exists(args.ca):
        print(f"ERROR: {args.ca} not found. Run fetch_class_actions.py first.",
              file=sys.stderr)
        return 2

    with open(args.ca) as f:
        ca = json.load(f)

    l360_items: list[dict[str, Any]] = []
    if os.path.exists(args.l360):
        with open(args.l360) as f:
            l360 = json.load(f)
        l360_items = l360.get("items", [])
        print(f"Loaded {len(l360_items)} Law360 items.", file=sys.stderr)
    else:
        print(f"  (no {args.l360} found — proceeding without Law360 enrichment)",
              file=sys.stderr)

    actions = ca.get("actions", [])
    enriched: list[dict[str, Any]] = []
    n_matched = 0
    for row in actions:
        coverage = find_matches(row, l360_items)
        if coverage:
            n_matched += 1
        # Annotate the row in place
        row = dict(row)
        row["law360_coverage"] = coverage
        enriched.append(row)

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "days_back": ca.get("days_back", 1),
        "count": len(enriched),
        "law360_match_count": n_matched,
        "law360_total_items": len(l360_items),
        "actions": enriched,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {args.out}: {n_matched}/{len(enriched)} dockets matched to Law360 coverage",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
