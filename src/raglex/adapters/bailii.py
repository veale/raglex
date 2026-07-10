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
