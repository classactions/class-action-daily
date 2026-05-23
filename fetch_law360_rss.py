#!/usr/bin/env python3
"""
fetch_law360_rss.py
-------------------
Pulls Law360 RSS feeds for the practice-area sections configured in
law360_feeds.json, normalizes entries, and writes law360_items.json.

For each item we attempt to extract:
  - case caption (from title)
  - docket number (regex against title + summary)
  - court hint (from summary)
  - filing date (from pubDate)
  - Law360 section that surfaced it
  - the article URL (subscriber-gated; opens for users with a Law360 login)
  - a short summary (Law360 publishes a 1-2 sentence dek per article — fine
    for internal use; the public-facing UI will hide this)

Usage:
    python fetch_law360_rss.py --config law360_feeds.json --out law360_items.json

Notes:
- RSS feed URLs are personal subscriber credentials. The config file should not
  be committed to a public repo.
- Law360 article content is licensed; this tool is intended for internal use
  by a firm with an active subscription. Public republication is prohibited
  by Law360's ToS — the front-end has a view-mode toggle that hides Law360
  enrichment in public mode.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

import urllib.request
import urllib.error


# Match common federal-docket-number formats:
#   1:24-cv-08877
#   24-cv-08877
#   3:25-cv-00123-XYZ
#   No. 24-1234 (appellate)
DOCKET_RE = re.compile(
    r'\b(?:No\.\s*)?(\d{1,2}:)?(\d{2})-(?:cv|cr|md|mc|mj)-(\d{3,5})(?:-[A-Z]{1,4})?\b',
    re.IGNORECASE
)

# Court hints we can pick out of summary text.
COURT_HINTS = [
    ("S.D.N.Y.", "nysd"), ("Southern District of New York", "nysd"),
    ("E.D.N.Y.", "nyed"), ("Eastern District of New York", "nyed"),
    ("N.D. Cal.", "cand"), ("Northern District of California", "cand"),
    ("C.D. Cal.", "cacd"), ("Central District of California", "cacd"),
    ("N.D. Ill.", "ilnd"), ("Northern District of Illinois", "ilnd"),
    ("S.D. Fla.", "flsd"), ("Southern District of Florida", "flsd"),
    ("D.N.J.", "njd"),    ("District of New Jersey", "njd"),
    ("E.D. Pa.", "paed"), ("Eastern District of Pennsylvania", "paed"),
    ("W.D. Tex.", "txwd"),("Western District of Texas", "txwd"),
    ("E.D. Tex.", "txed"),("Eastern District of Texas", "txed"),
    ("D. Del.", "ded"),   ("District of Delaware", "ded"),
    ("D. Mass.", "mad"),  ("District of Massachusetts", "mad"),
    ("D. Or.", "ord"),    ("District of Oregon", "ord"),
    ("N.D. Ga.", "gand"), ("Northern District of Georgia", "gand"),
    ("D.D.C.",  "dcd"),   ("District of Columbia", "dcd"),
]


@dataclass
class Law360Item:
    section: str                # which Law360 section feed surfaced this
    title: str
    summary: str                # short dek/lede from RSS
    url: str                    # subscriber-gated article URL
    pub_date: str               # ISO date
    docket_numbers: list[str]   # extracted from title + summary
    court_id_guess: str         # best-effort court code
    court_label_guess: str      # human-readable hint


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ClassActionTracker/0.1 (internal firm tool; subscriber RSS)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def normalize_docket(m: re.Match[str]) -> str:
    """Render a normalized docket number from a regex match."""
    # Reconstruct in the form 1:24-cv-08877 (drop the appellate prefix variant)
    parts = m.group(0).strip()
    # collapse "No. " prefix
    parts = re.sub(r'^No\.\s*', '', parts, flags=re.IGNORECASE)
    return parts


def find_dockets(text: str) -> list[str]:
    seen = set()
    out = []
    for m in DOCKET_RE.finditer(text or ""):
        d = normalize_docket(m)
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def guess_court(text: str) -> tuple[str, str]:
    """Returns (court_id, label) or ('', '')."""
    if not text:
        return ("", "")
    for label, court_id in COURT_HINTS:
        if label in text:
            return (court_id, label)
    return ("", "")


def parse_rss(xml_bytes: bytes, section: str) -> list[Law360Item]:
    """Parse an RSS 2.0 document into Law360Item rows."""
    items: list[Law360Item] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"  ! XML parse error for {section}: {e}", file=sys.stderr)
        return items

    # RSS 2.0: channel/item
    channel = root.find("channel")
    if channel is None:
        return items

    for it in channel.findall("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = (it.findtext("description") or "").strip()
        pub_raw = (it.findtext("pubDate") or "").strip()

        # Normalize pubDate -> ISO
        pub_iso = ""
        if pub_raw:
            try:
                d = parsedate_to_datetime(pub_raw)
                pub_iso = d.date().isoformat()
            except (TypeError, ValueError):
                pub_iso = ""

        # Strip simple HTML tags from description; RSS deks are usually plain.
        desc_clean = re.sub(r"<[^>]+>", "", desc).strip()

        blob = f"{title}\n{desc_clean}"
        dockets = find_dockets(blob)
        court_id, court_lbl = guess_court(blob)

        items.append(Law360Item(
            section=section,
            title=title,
            summary=desc_clean,
            url=link,
            pub_date=pub_iso,
            docket_numbers=dockets,
            court_id_guess=court_id,
            court_label_guess=court_lbl,
        ))

    return items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="law360_feeds.json")
    ap.add_argument("--out", default="law360_items.json")
    ap.add_argument("--max-items-per-feed", type=int, default=100)
    args = ap.parse_args()

    if not os.path.exists(args.config):
        print(f"ERROR: config file {args.config} not found.", file=sys.stderr)
        return 2

    with open(args.config) as f:
        cfg = json.load(f)

    feeds = cfg.get("feeds", {})
    all_items: list[Law360Item] = []

    for section, url in feeds.items():
        url = (url or "").strip()
        if not url:
            print(f"  - skipping {section!r}: no URL configured", file=sys.stderr)
            continue
        print(f"  > fetching {section!r}…", file=sys.stderr)
        try:
            body = http_get(url)
        except urllib.error.HTTPError as e:
            print(f"  ! HTTP {e.code} for {section!r}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"  ! error for {section!r}: {e}", file=sys.stderr)
            continue

        items = parse_rss(body, section)[: args.max_items_per_feed]
        all_items.extend(items)
        print(f"    got {len(items)} items", file=sys.stderr)
        time.sleep(0.5)  # be polite

    # Sort newest first
    all_items.sort(key=lambda x: x.pub_date or "", reverse=True)

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(all_items),
        "items": [asdict(i) for i in all_items],
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {args.out} ({len(all_items)} items across {sum(1 for v in feeds.values() if v)} feeds)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
