"""Classifying and linking the cited-but-unfetchable frontier (§5).

The unfetchable list groups references the system can't fetch. What each one *is* — a law
report, a statute cited by name, an EU instrument by name, a case by name — decides both
its label and the best external link to go find it. Keeping that logic here (pure, from
the raw string) keeps the facade thin and lets it be tested directly.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

from .reporters import report_series
from .statute_gazetteer import resolve as _gazetteer

# "... Act 1980", "... Regulations 2003", "... Order 2015", "... Rules 1998", "Measure".
_LEG_NAME = re.compile(
    r"\b(?P<kind>Act|Regulations|Rules|Order|Measure|Scheme|Bill)\s+(?P<year>(?:1[6-9]|20)\d{2})\b",
    re.IGNORECASE,
)
# "Directive 95/46", "Regulation (EU) 2016/679", "Council Decision 94/800", "Framework
# Decision 2002/584" — an EU instrument named rather than given as a CELEX.
_EU_NAME = re.compile(
    r"\b(?P<kind>Regulation|Directive|Decision|Framework\s+Decision)\b[^.\n]{0,40}?\b\d{1,4}/\d{1,4}\b",
    re.IGNORECASE,
)
# a stored URL that never normalised to an id — a truncated web-archive / content link.
# These are noise in the frontier, not citable authorities.
_JUNK_URL = re.compile(r"^https?://(?:web\.archive\.org|webarchive\.|.*/eu-exit/)", re.IGNORECASE)


def legislation_search_url(query: str) -> str:
    """legislation.gov.uk title search — the operator finds the Act and can then harvest
    it by id (the gazetteer is incomplete, so a name that doesn't resolve offline still
    has a one-click way to be located)."""
    return f"https://www.legislation.gov.uk/all?title={quote_plus(query.strip())}"


def eurlex_search_url(query: str) -> str:
    """EUR-Lex quick search for an EU instrument named rather than given as a CELEX."""
    return f"https://eur-lex.europa.eu/search.html?scope=EURLEX&text={quote_plus(query.strip())}&type=quick"


# a leading pinpoint the citation carries before the Act's name ("Part II of the …",
# "section 5 of the …") — stripped so the gazetteer sees just the title.
_LEG_PREFIX = re.compile(
    r"^(?:(?:under|in|of|per)\s+)?(?:the\s+)?"
    r"(?:s(?:ection|s|ub-?section)?|art(?:icle)?|reg(?:ulation)?|para(?:graph)?|"
    r"sch(?:edule)?|part|chapter|ch|title)\b[^.\n]*?\bof\s+(?:the\s+)?",
    re.IGNORECASE,
)


def _leg_title_year(raw: str) -> tuple[str, str] | None:
    m = _LEG_NAME.search(raw or "")
    if not m:
        return None
    title = raw[: m.end()].strip()               # everything up to and including "Act 1980"
    title = _LEG_PREFIX.sub("", title).strip()    # drop a leading "Part II of the …" pinpoint
    # drop a bare leading preposition ("under the …", "pursuant to the …")
    title = re.sub(r"^(?:under|pursuant to|by virtue of|in|of|per|for the purposes of)\s+the\s+",
                   "", title, flags=re.IGNORECASE).strip()
    return title, m.group("year")


def classify(raw: str | None, candidate: str | None) -> dict | None:
    """Classify one unfetchable reference from its raw string (and candidate, if any).

    Returns ``{form, link, is_report, gazetteer_id}`` — or ``None`` if the reference is
    junk (a truncated web-archive URL) and should be dropped from the frontier entirely.
    ``gazetteer_id`` is set when a statute name resolves offline to a legislation.gov.uk
    id: the caller can treat it as routable rather than merely unfetchable.
    """
    raw = raw or ""
    if _JUNK_URL.match(raw) or _JUNK_URL.match(candidate or ""):
        return None

    series = report_series(raw) or report_series(candidate)
    if series:
        return {"form": f"law report ({series})", "is_report": True, "gazetteer_id": None,
                "link": {"kind": "search", "label": "find on BAILII ↗", "can_upload": True,
                         "url": _bailii_search(raw or candidate or "")}}

    leg = _leg_title_year(raw)
    if leg:
        title, year = leg
        gid = _gazetteer(re.sub(r"\s+\d{4}$", "", title), year)  # gazetteer wants title sans year
        return {"form": "legislation (by name)", "is_report": False, "gazetteer_id": gid,
                "link": {"kind": "search", "label": "find on legislation.gov.uk ↗",
                         "can_upload": True, "url": legislation_search_url(title)}}

    if _EU_NAME.search(raw):
        return {"form": "EU instrument (by name)", "is_report": False, "gazetteer_id": None,
                "link": {"kind": "search", "label": "find on EUR-Lex ↗", "can_upload": True,
                         "url": eurlex_search_url(raw)}}

    return None  # not specially classifiable here — the caller falls back to _classify


def _bailii_search(citation: str) -> str:
    from ..adapters.bailii import bailii_search_url

    return bailii_search_url(citation)
