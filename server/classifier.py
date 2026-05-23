"""
classifier.py
-------------
Shared classification logic: takes a CourtListener search hit (whether from
the REST API or from a webhook payload) and returns a filing row ready for
db.upsert_filing.

Returns None if the hit doesn't look like a class action.

This module is the single source of truth for our rule-based categorization.
When the LLM classifier comes online, it will be invoked separately by a
worker reading rows where needs_llm_review = TRUE.
"""

from __future__ import annotations

from typing import Any


# Nature-of-suit codes that disproportionately contain class actions.
NOS_CATEGORIES: dict[str, tuple[str, str]] = {
    "850": ("Securities/Commodities/Exchange", "Securities"),
    "410": ("Antitrust", "Antitrust"),
    "480": ("Consumer Credit", "Consumer"),
    "375": ("False Claims Act", "Consumer"),
    "370": ("Other Fraud", "Consumer"),
    "371": ("Truth in Lending", "Consumer"),
    "710": ("Fair Labor Standards Act", "Wage & Hour"),
    "740": ("Railway Labor Act", "Labor"),
    "751": ("Family and Medical Leave Act", "Labor"),
    "790": ("Other Labor Litigation", "Labor"),
    "791": ("ERISA", "ERISA"),
    "442": ("Civil Rights — Employment", "Civil Rights"),
    "440": ("Other Civil Rights", "Civil Rights"),
    "443": ("Housing/Accommodations", "Civil Rights"),
    "445": ("Amer. w/ Disabilities — Employment", "Civil Rights"),
    "446": ("Amer. w/ Disabilities — Other", "Civil Rights"),
    "448": ("Education", "Civil Rights"),
    "890": ("Other Statutory Actions", "Privacy/TCPA/Data"),
    "365": ("Personal Injury — Product Liability", "Product Liability"),
    "367": ("Health Care/Pharmaceutical Personal Injury", "Product Liability"),
    "368": ("Asbestos Personal Injury Product Liability", "Product Liability"),
    "893": ("Environmental Matters", "Environmental"),
}


# Keyword refinements. First match wins.
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

# Subcategories that should be rolled up under "Privacy / Data"
_PRIVACY_SUBCATS = {
    "TCPA", "FCRA", "BIPA / Biometric Privacy",
    "VPPA / Pixel Tracking", "Data Breach",
}


def categorize(case_name: str, snippet: str, nos_code: str) -> tuple[str, str, float]:
    """
    Returns (category, subcategory, confidence).
    Confidence is a hand-tuned proxy: 0.9 for keyword hits, 0.6 for NOS-only
    matches, 0.2 for "Other" fallthrough. This is what feeds the
    needs_llm_review flag downstream.
    """
    text = f"{case_name} {snippet}".lower()
    for kw, subcat in KEYWORD_CATEGORIES:
        if kw in text:
            if subcat in _PRIVACY_SUBCATS:
                return ("Privacy / Data", subcat, 0.9)
            if subcat == "Consumer / False Advertising":
                return ("Consumer", subcat, 0.9)
            if subcat == "Wage & Hour":
                return ("Wage & Hour", subcat, 0.9)
            if subcat == "ERISA":
                return ("ERISA", subcat, 0.9)
            if subcat == "Securities":
                return ("Securities", subcat, 0.9)
            if subcat == "Antitrust":
                return ("Antitrust", subcat, 0.9)
            return ("Other", subcat, 0.5)

    if nos_code in NOS_CATEGORIES:
        label, top = NOS_CATEGORIES[nos_code]
        return (top, label, 0.6)

    return ("Other", "Other", 0.2)


def looks_like_class_action(case_name: str, snippet: str, cause: str) -> bool:
    """Heuristic gate before we accept a hit as a class action."""
    blob = f"{case_name} {snippet} {cause}".lower()
    triggers = [
        "class action",
        "on behalf of all others",
        "fed. r. civ. p. 23",
        "rule 23",
        "putative class",
        "collective action",
    ]
    return any(t in blob for t in triggers)


def classify_search_hit(
    hit: dict[str, Any], ingest_source: str = "poll"
) -> dict[str, Any] | None:
    """
    Turn a raw CourtListener search-hit dict into a filings-table row dict.
    Returns None if the hit doesn't look like a class action or is missing
    required fields.
    """
    case_name = hit.get("caseName") or hit.get("case_name") or ""
    docket_number = hit.get("docketNumber") or hit.get("docket_number") or ""
    court = hit.get("court") or hit.get("court_citation_string") or ""
    court_id = hit.get("court_id") or ""
    date_filed = hit.get("dateFiled") or hit.get("date_filed") or None
    nos = hit.get("suitNature") or hit.get("nature_of_suit") or ""
    cause = hit.get("cause") or ""
    docket_id = hit.get("docket_id") or hit.get("id") or 0
    if not docket_id:
        return None

    docs = hit.get("recap_documents") or []
    snippet = ""
    if docs and isinstance(docs, list):
        snippet = docs[0].get("snippet") or ""

    nos_code = ""
    if nos and nos.split()[0].isdigit():
        nos_code = nos.split()[0]

    if not looks_like_class_action(case_name, snippet, cause):
        return None

    category, subcat, conf = categorize(case_name, snippet, nos_code)

    abs_url = hit.get("docket_absolute_url") or f"/docket/{docket_id}/"
    courtlistener_url = (
        f"https://www.courtlistener.com{abs_url}"
        if abs_url.startswith("/") else abs_url
    )

    complaint_url = None
    for d in docs:
        desc = (d.get("description") or "").lower()
        short = (d.get("short_description") or "").lower()
        if "complaint" in desc or "complaint" in short:
            fp = d.get("filepath_local")
            if fp:
                if fp.startswith("http://") or fp.startswith("https://"):
                    complaint_url = fp
                elif fp.startswith("/"):
                    complaint_url = f"https://www.courtlistener.com{fp}"
                else:
                    # Bare RECAP path like "recap/gov.uscourts.nysd.../...pdf"
                    complaint_url = f"https://storage.courtlistener.com/{fp}"
                break

    parties = hit.get("party") or []
    parties_summary = "; ".join(parties[:4]) if isinstance(parties, list) else ""

    # needs_llm_review fires for low-confidence (Other) categorizations.
    needs_review = category == "Other" or conf < 0.4

    return {
        "docket_id":         int(docket_id),
        "case_name":         case_name.strip(),
        "docket_number":     docket_number,
        "court_id":          court_id,
        "court":             court,
        "date_filed":        date_filed,
        "nature_of_suit":    nos,
        "nos_code":          nos_code,
        "cause":             cause,
        "parties_summary":   parties_summary,
        "courtlistener_url": courtlistener_url,
        "complaint_url":     complaint_url,
        "snippet":           snippet[:1000],
        "category":          category,
        "subcategory":       subcat,
        "category_source":   "rule",
        "category_confidence": conf,
        "needs_llm_review":  needs_review,
        "ingest_source":     ingest_source,
    }
