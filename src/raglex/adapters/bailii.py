"""BAILII URL generator and RTF import helper.

BAILII (British and Irish Legal Information Institute) makes pre-digital UK
judgments available as RTF files at predictable URLs. This module maps a
Find Case Law stable_id (e.g. ``ewca/civ/2006/717``) to the corresponding
BAILII download URL, and provides the import helper that reads the RTF into
the corpus keyed by that stable_id — so all existing pending citations to
that case resolve the moment the file is imported.

BAILII prohibits automated scraping; these URLs are links for *manual* download
only. The user fetches the file themselves (one click, no bot) and drops it into
the UI — the import then runs entirely locally against their own download.
"""

from __future__ import annotations

# ── jurisdiction mapping ────────────────────────────────────────────────────

_JURIS: dict[str, str] = {
    # England & Wales
    "ewca": "ew", "ewhc": "ew", "ewcop": "ew", "ewfc": "ew", "ewcaf": "ew",
    # UK-wide
    "uksc": "uk", "ukhl": "uk", "ukpc": "uk",
    "ukftt": "uk", "ukut": "uk", "ukeat": "uk", "ukiptrib": "uk",
    # Scotland
    "csoh": "scot", "csih": "scot",
    # Northern Ireland
    "niqb": "nie", "nica": "nie", "nimiscr": "nie", "nicty": "nie",
}

# EWHC division codes → BAILII path segment (BAILII uses title-case or acronyms)
_EWHC_DIV: dict[str, str] = {
    "admin": "Admin",
    "ch": "Ch",
    "comm": "Comm",
    "fam": "Fam",
    "pat": "Patents",
    "qb": "QB",
    "kb": "KB",
    "tcc": "TCC",
    "ip": "IPEC",
    "mer": "Mercantile",
    "costs": "Costs",
    "admlty": "Admty",
    "scco": "SCCO",
    "exch": "Exch",
}

# Tribunal/upper-court chambers → BAILII uses ALL-CAPS (GRC, AAC, TCC, IAC)
_UPPER_COURTS: frozenset[str] = frozenset({"ukftt", "ukut", "ukeat", "ukiptrib"})


def bailii_url(stable_id: str) -> str | None:
    """Return the BAILII RTF download URL for a UK Find Case Law stable_id.

    Examples::

        bailii_url("ewca/civ/2006/717")
        # → 'https://www.bailii.org/ew/cases/EWCA/Civ/2006/717.rtf'

        bailii_url("ewhc/admin/2007/3039")
        # → 'https://www.bailii.org/ew/cases/EWHC/Admin/2007/3039.rtf'

        bailii_url("uksc/2021/12")
        # → 'https://www.bailii.org/uk/cases/UKSC/2021/12.rtf'

    Returns ``None`` for ids with no BAILII equivalent (opaque ``d-UUID`` ids,
    non-UK jurisdictions, or court codes not recognised).
    """
    if not stable_id or stable_id.startswith("d-"):
        return None  # opaque new-style TNA id — no predictable BAILII path

    parts = stable_id.lower().split("/")
    if len(parts) < 3:
        return None

    court = parts[0]
    juris = _JURIS.get(court)
    if not juris:
        return None

    court_up = court.upper()

    if len(parts) == 3:
        # 3-segment: court/year/num  (e.g. uksc/2021/12, csoh/2020/50)
        _, year, num = parts
        return f"https://www.bailii.org/{juris}/cases/{court_up}/{year}/{num}.rtf"

    if len(parts) >= 4:
        # 4-segment: court/division/year/num  (ewca/civ/2006/717)
        div_raw, year, num = parts[1], parts[2], parts[3]
        if court == "ewhc":
            div = _EWHC_DIV.get(div_raw, div_raw.title())
        elif court in _UPPER_COURTS:
            div = div_raw.upper()   # GRC, AAC, TCC, IAC, …
        else:
            div = div_raw.title()   # Civ, Crim, …
        return f"https://www.bailii.org/{juris}/cases/{court_up}/{div}/{year}/{num}.rtf"

    return None


def bailii_search_url(citation: str) -> str:
    """BAILII's find-by-citation search — the link for a case with no constructible direct
    URL (a classic law report like "[1982] AC 1", or a case cited by name). The user
    clicks through, finds the judgment, and downloads whatever format BAILII offers."""
    from urllib.parse import quote_plus

    return f"https://www.bailii.org/cgi-bin/find_by_citation.cgi?citation={quote_plus(citation.strip())}"


# Where a non-BAILII jurisdiction's cases are actually findable — the free LIIs.
# BAILII covers GB + IE; a Canadian/Australian/NZ citation gets its own institute's
# search, not a BAILII search that can never hit.
_LII_SEARCH: dict[str, tuple[str, str]] = {
    "CA": ("CanLII", "https://www.canlii.org/en/#search/text={q}"),
    "AU": ("AustLII", "https://www.austlii.edu.au/cgi-bin/sinosrch.cgi?method=boolean&query={q}"),
    "NZ": ("NZLII", "http://www.nzlii.org/cgi-bin/sinosrch.cgi?method=boolean&query={q}"),
    "IN": ("LII of India", "http://www.liiofindia.org/cgi-bin/sinosrch.cgi?method=boolean&query={q}"),
}


def external_link(candidate: str | None, raw: str | None) -> dict | None:
    """The best external link for an *unfetchable* reference, plus whether an upload can
    resolve it in place.

    - a UK neutral-citation slug → the direct BAILII **RTF** (one-click download); an
      uploaded RTF is imported under that stable_id, resolving every pending citation to
      it (``import_bailii``);
    - a Canadian / Australian / NZ / Indian citation → that jurisdiction's own legal
      information institute search (BAILII doesn't hold them);
    - anything else (a classic law report, a case by name) → a BAILII **search** link; the
      user resolves it by uploading the file against the reference (``resolve-file``).
    """
    from urllib.parse import quote_plus

    from ..citations.courts import lookup

    if candidate:
        rtf = bailii_url(candidate)
        if rtf:
            return {"kind": "rtf", "url": rtf, "label": "BAILII RTF ↓",
                    "can_upload": True, "stable_id": candidate}
    cite = (raw or candidate or "").strip()
    if not cite:
        return None
    head = (candidate or "").split("/", 1)[0]
    known = lookup(head) if head else None
    if known and known.jurisdiction in _LII_SEARCH:
        name, tmpl = _LII_SEARCH[known.jurisdiction]
        return {"kind": "search", "url": tmpl.format(q=quote_plus(cite)),
                "label": f"find on {name} ↗", "can_upload": True}
    return {"kind": "search", "url": bailii_search_url(cite), "label": "find on BAILII ↗",
            "can_upload": True}
