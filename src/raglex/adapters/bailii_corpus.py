"""Join the BAILII full-text corpus (``all.jsonl``) to the BAILII index CSV.

The full-text dump keys each judgment by a BAILII URL path (``/ew/cases/EWHC/Comm/
2015/3076.html``) but carries no case name. The index CSV (``bailii_cases*.csv``) carries
the name and citation but keys them by ``live_url``. Both reduce to the same **Find Case
Law stable_id** — the neutral-citation slug ``ewhc/comm/2015/3076`` — so that slug is the
join key and, once imported, the id every pending citation to the case resolves against.

Two pure helpers do the work:
  * :func:`bailii_path_to_slug` — the inverse of :func:`raglex.adapters.bailii.bailii_url`;
  * :func:`clean_case_name` — strip the index's cruft (leading ``#``/``>`` anchors, the
    trailing ``(30 October 2012)`` date, BAILII catchword parens) down to the bare
    party title, while pulling out the embedded citation(s) it also records.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# ── path → stable_id ─────────────────────────────────────────────────────────

# The juris segment BAILII puts before ``/cases/`` (ew, uk, scot, nie, ie). We drop it —
# the FCL stable_id is ``court[/division]/year/num`` with no jurisdiction prefix.
_JURIS_PREFIXES = frozenset({"ew", "uk", "scot", "nie", "ie", "eu"})


def bailii_path_to_slug(path: str | None) -> str | None:
    """Map a BAILII URL path to a Find Case Law stable_id, or None if it isn't a
    recognisable ``/<juris>/cases/COURT[/DIV]/YEAR/NUM`` judgment path.

    >>> bailii_path_to_slug("/ew/cases/EWHC/Comm/2015/3076.html")
    'ewhc/comm/2015/3076'
    >>> bailii_path_to_slug("/uk/cases/UKSC/2021/12.html")
    'uksc/2021/12'
    >>> bailii_path_to_slug("/ew/cases/EWCA/Civ/2006/717.html")
    'ewca/civ/2006/717'

    Returns None for tribunal paths whose final segment isn't a bare number
    (``/ew/cases/EWLVT/2007/LON_00AY_OCE_2007_0100.html``) — those aren't in the
    full-text corpus and have no clean neutral-citation slug.
    """
    if not path:
        return None
    # tolerate a full URL as well as a bare path
    if "://" in path:
        path = urlparse(path).path
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 4 or parts[0].lower() not in _JURIS_PREFIXES:
        return None
    if parts[1].lower() != "cases":
        return None
    rest = parts[2:]  # COURT [DIV] YEAR NUM.html
    if len(rest) < 3:
        return None
    # last segment: strip the .html (or .rtf) extension
    num = rest[-1].rsplit(".", 1)[0]
    year = rest[-2]
    # case numbers are usually bare integers but also "B17", "68_2", "J1" — allow any
    # alphanumeric-with-underscore run that carries at least one digit. The long
    # constructed tribunal ids ("LON_LV_NFE_00AY_0100") also pass, which is fine: those
    # judgments simply aren't in this corpus.
    if not (len(year) == 4 and year.isdigit() and re.fullmatch(r"[0-9A-Za-z_]*\d[0-9A-Za-z_]*", num)):
        return None
    slug = "/".join(rest).rsplit(".", 1)[0].lower()
    return slug


# ── case-name cleaning ───────────────────────────────────────────────────────

# A neutral citation ("[2012] EWHC 3009 (Comm)") or a BAILII-constructed one
# ("[2007] EWLVT LON_..."): a bracketed year, a court token, then an id run, with an
# optional trailing chamber tag in parens that belongs to the citation, not the name.
_CITATION = re.compile(
    r"\[(1[6-9]\d{2}|20\d{2})\]\s+"                    # [year]
    r"([A-Z]{2,10}(?:\s+[A-Z][a-z]+)?)"               # court acronym + optional division word
    r"\s+([0-9][0-9A-Za-z_]*|[A-Z][0-9A-Za-z_]*_[0-9A-Za-z_]+)"  # number, or constructed id
    r"(?:\s*\(([A-Za-z]{2,10})\))?"                    # optional (Comm)/(Admin) chamber
)

# The trailing "(30 October 2012)" / "(30th October, 2000)" decision-date paren.
_DATE_TAIL = re.compile(
    r"\s*\((?:\d{1,2}(?:st|nd|rd|th)?\s+)?"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s*\d{4}\)\s*$",
    re.IGNORECASE,
)

# Leading anchor cruft the scrape left on some names: "# ", "> ", ">", "* ".
_LEADING_JUNK = re.compile(r"^\s*[#>*]+\s*")


@dataclass(frozen=True, slots=True)
class CleanName:
    """The bare party title of a case, plus the citations and catchwords stripped off it."""

    title: str
    citations: tuple[str, ...] = ()       # e.g. ("[2012] EWHC 3009 (Comm)",)
    catchwords: str | None = None         # discarded BAILII subject-matter paren, if any


def _normalise_citation(m: re.Match) -> str:
    year, court, num, chamber = m.group(1), m.group(2), m.group(3), m.group(4)
    core = f"[{year}] {court} {num}"
    return f"{core} ({chamber})" if chamber else core


def clean_case_name(raw: str | None) -> CleanName:
    """Reduce an index ``case_name`` to its bare party title and pull out the citation(s).

    >>> clean_case_name("> Bominflot v Petroplus AG [2012] EWHC 3009 (Comm) (30 October 2012)").title
    'Bominflot v Petroplus AG'
    >>> clean_case_name("Baxter, R (on the application of) v Lincolnshire CC [2015] EWCA Civ 1290 (18 December 2015)").citations
    ('[2015] EWCA Civ 1290',)
    """
    s = (raw or "").strip()
    s = _LEADING_JUNK.sub("", s)

    citations = tuple(dict.fromkeys(_normalise_citation(m) for m in _CITATION.finditer(s)))
    # everything from the first citation onwards is metadata, not the name
    first = _CITATION.search(s)
    name = s[: first.start()].strip() if first else s

    # a trailing "(30 October 2012)" can survive when there was no citation to cut at
    name = _DATE_TAIL.sub("", name).strip()

    # BAILII catchword paren: a trailing "(Subject : more subject)" run — discard from the
    # name but keep it for provenance. Heuristic: a parenthesical containing a colon or
    # several words, at the very end, that isn't itself a citation chamber tag.
    catchwords = None
    cw = re.search(r"\s*\(([^()]{12,})\)\s*$", name)
    if cw and (":" in cw.group(1) or len(cw.group(1).split()) >= 3):
        catchwords = cw.group(1).strip()
        name = name[: cw.start()].strip()

    name = re.sub(r"\bv\.\s", "v ", name)                # "v." → "v"
    name = re.sub(r"\s{2,}", " ", name).strip(" ,;-")
    return CleanName(title=name, citations=citations, catchwords=catchwords)


# ── stable_id → neutral citation (the sanity-check counterpart of the path) ──

# EWHC division slug → the parenthetical chamber tag as it prints in the neutral citation.
_EWHC_CHAMBER: dict[str, str] = {
    "admin": "Admin", "ch": "Ch", "comm": "Comm", "fam": "Fam", "pat": "Pat",
    "qb": "QB", "kb": "KB", "tcc": "TCC", "ipec": "IPEC", "ip": "IPEC",
    "costs": "Costs", "admlty": "Admlty", "mercantile": "Mercantile", "scco": "SCCO",
    "patents": "Pat", "technology": "TCC", "exch": "Exch",
}


def slug_to_citation(slug: str | None) -> str | None:
    """The neutral citation a Find Case Law slug encodes — the mechanical identifier the
    corpus can always derive even when the case names itself nowhere in its text.

    >>> slug_to_citation("ewhc/comm/2015/3076")
    '[2015] EWHC 3076 (Comm)'
    >>> slug_to_citation("ewca/civ/2006/717")
    '[2006] EWCA Civ 717'
    >>> slug_to_citation("uksc/2021/12")
    '[2021] UKSC 12'
    """
    if not slug:
        return None
    parts = slug.split("/")
    if len(parts) == 3:
        court, year, num = parts
        return f"[{year}] {court.upper()} {num.upper()}"
    if len(parts) == 4:
        court, div, year, num = parts
        num = num.upper()
        if court == "ewhc":
            chamber = _EWHC_CHAMBER.get(div, div.title())
            return f"[{year}] EWHC {num} ({chamber})"
        if court in ("ewca", "ewcop", "ewfc"):
            return f"[{year}] {court.upper()} {div.title()} {num}"
        # UK upper tribunals / others: chamber is an uppercase tag in parens
        return f"[{year}] {court.upper()} {num} ({div.upper()})"
    return None


def _cite_year_num(citation: str) -> tuple[str, str] | None:
    m = _CITATION.search(citation or "")
    if not m:
        return None
    return m.group(1), m.group(3).upper()


def citation_agrees_with_slug(slug: str, citation: str) -> bool:
    """Do a slug and a printed citation refer to the same case? True when their year and
    case-number agree — the check that catches an index row mis-joined to the wrong path."""
    parts = slug.split("/")
    if len(parts) < 3:
        return False
    slug_year, slug_num = parts[-2], parts[-1].upper()
    yn = _cite_year_num(citation)
    return yn is not None and yn == (slug_year, slug_num)


# ── CSV index ────────────────────────────────────────────────────────────────


def load_name_index(csv_path: str) -> dict[str, CleanName]:
    """Read the BAILII index CSV into ``{stable_id: CleanName}``, keyed by the same slug
    the full-text corpus derives from its ``id`` — so the two join on equal keys. Rows
    whose ``live_url`` has no clean slug are skipped."""
    out: dict[str, CleanName] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = bailii_path_to_slug(row.get("live_url") or "")
            if not slug or slug in out:
                continue
            out[slug] = clean_case_name(row.get("case_name") or "")
    return out
