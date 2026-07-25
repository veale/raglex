"""EU legislative-change model helpers (§EU) — the identifier spine, the consolidation
link, the recast/codification classifier, and the correlation-table parser.

Grounded in the CELLAR/CDM + FRBR reality:

- **CELEX sector** is semantically load-bearing. Sector ``0`` is a *consolidated version*
  (``0`` + base-act-body + ``-YYYYMMDD``); stripping the ``0`` and the date recovers the
  authoritative base act — that string match *is* the consolidation→base link.
- **Recast, codification, consolidation are not distinct edge types.** A consolidation is
  unambiguous (sector-0 + ``consolidates``). Codification and recast look identical in the
  graph (new Work that ``repeals`` predecessors, with derivation) — the difference is
  substantive-change-vs-none, which is a *descriptor classification*, not a relationship.
  ``classify_change`` reconstructs it heuristically; don't expect an ``is_recast`` predicate.
- **Article-level old→new mapping is not machine-exposed.** It lives only in the recast's
  correlation table (a Formex ``<TBL>`` / a two-column text table). ``parse_correlation_
  table`` extracts it heuristically — the weakest seam, by design.

Pure functions only (no I/O), so they're cheap to test and safe to reuse across the adapter,
the resolver, and probes.
"""

from __future__ import annotations

import re

# CELEX sector (first char) → meaning. Load-bearing: parse it, don't ignore it.
CELEX_SECTORS: dict[str, str] = {
    "1": "treaties",
    "2": "international agreements",
    "3": "legislation",
    "4": "internal/complementary acts",
    "5": "preparatory acts",       # COM proposals etc.
    "6": "case law",
    "7": "national transposition",
    "8": "national case law / other",
    "9": "parliamentary questions",
    "0": "consolidated version",
    "C": "OJ C-series notices",
    "E": "EFTA documents",
}

# CDM act-to-act relationship properties (queryable via SPARQL against CELLAR) → the
# RagLex relationship they mint. These are the CLEAN, machine-exposed edges (unlike
# recast/codification, which is a classification). The values are ``RelationshipType`` member
# NAMES (kept as strings so this module has no models import); the adapter maps them. A
# future SPARQL harvest binds ``?work cdm:<prop> ?target`` for each key. See classify_change
# for the recast/codification layer that sits on top of ``repeals`` + ``based_on``.
CDM_ACT_TO_ACT_LINKS: dict[str, str] = {
    "resource_legal_amends_resource_legal": "AMENDS",
    "resource_legal_amended_by_resource_legal": "AMENDED_BY",
    "resource_legal_repeals_resource_legal": "REPEALS",
    "resource_legal_repealed_by_resource_legal": "REPEALED_BY",
    "resource_legal_consolidates_resource_legal": "CONSOLIDATES",
    "resource_legal_corrects_resource_legal": "CORRECTS",
    "resource_legal_corrected_by_resource_legal": "CORRECTED_BY",
    "resource_legal_based_on_resource_legal": "LEGAL_BASIS",
}

_CONSOLIDATED_RE = re.compile(r"^0(\d{4}[A-Z]+\d+)-(\d{8})$")
# The base-act body after the sector char: 4-digit year, one/more type letters, a number.
_BASE_BODY_RE = re.compile(r"^\d{4}[A-Z]+\d+", re.I)


def celex_sector(celex: str | None) -> str | None:
    """The single sector character (semantic class) of a CELEX, or None."""
    if not celex:
        return None
    c = celex.strip()
    return c[0] if c else None


def celex_sector_name(celex: str | None) -> str | None:
    s = celex_sector(celex)
    return CELEX_SECTORS.get(s) if s else None


def is_consolidation(celex: str | None) -> bool:
    """True for a consolidated-version CELEX (sector 0 with a ``-YYYYMMDD`` date suffix)."""
    return bool(celex and _CONSOLIDATED_RE.match(celex.strip()))


def consolidation_date(celex: str | None) -> str | None:
    """The ISO date (YYYY-MM-DD) a consolidation snapshot represents, or None."""
    m = _CONSOLIDATED_RE.match((celex or "").strip())
    if not m:
        return None
    d = m.group(2)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def consolidation_base(celex: str | None) -> str | None:
    """The authoritative base-act CELEX a consolidation derives from: strip the leading ``0``
    and the ``-YYYYMMDD`` suffix, and restore sector ``3`` (legislation). e.g.
    ``02016R0679-20160504`` → ``32016R0679``. None if not a consolidation."""
    m = _CONSOLIDATED_RE.match((celex or "").strip())
    if not m:
        return None
    return "3" + m.group(1)


# Descriptor/classification cues (case-insensitive substrings) that disambiguate the shared
# "new Work + repeals predecessors + derivation" signature. Recast carries substantive
# change; codification does not; both differ from a plain repeal.
_RECAST_CUES = ("recast", "refonte", "neufassung", "rifusione", "refundición")
_CODIFICATION_CUES = ("codif",)   # codification / codified / codificación / kodifiz…


def classify_change(*, celex: str | None = None, repeals: bool = False,
                    based_on: bool = False, descriptors=None) -> str:
    """Reconstruct which kind of legislative change a Work represents, from the pattern the
    research describes (no single predicate exists):

    - ``consolidation`` — sector-0 dated CELEX (unambiguous).
    - ``recast`` / ``codification`` — a new Work that repeals + derives from predecessors,
      disambiguated by descriptor cues (recast = substantive change; codification = none).
    - ``repeal`` — repeals without derivation cues.
    - ``act`` — none of the above (an ordinary/amending act).

    ``descriptors`` is any iterable of classification/subject-matter strings from the CDM
    metadata; the cues are matched case-insensitively."""
    if is_consolidation(celex):
        return "consolidation"
    text = " ".join(str(d) for d in (descriptors or [])).lower()
    if any(cue in text for cue in _RECAST_CUES):
        return "recast"
    if any(cue in text for cue in _CODIFICATION_CUES):
        return "codification"
    if repeals and based_on:
        # the shared recast-or-codification signature with no distinguishing descriptor —
        # honest about the ambiguity rather than guessing substantive change
        return "recast_or_codification"
    if repeals:
        return "repeal"
    return "act"


# ---------------------------------------------------------------------------
# correlation-table parsing (old→new article mapping) — the weak seam
# ---------------------------------------------------------------------------
_ARTICLE_REF_RE = re.compile(
    r"(?:Article|Art\.?|Annex|Recital)\s+[0-9IVXLC]+[a-z]?(?:\([0-9a-z]+\))*", re.I)
_DASH_ROW_RE = re.compile(r"\s{2,}|\t|\s+[—–-]\s+|\s*\|\s*")


def parse_correlation_table_cells(rows: list[list[str]]) -> list[tuple[str, str]]:
    """Old→new article pairs from already-tokenised table rows (e.g. a Formex ``<TBL>``
    decomposed into rows of cell texts). Takes the first two non-empty cells of each row as
    (old, new); skips the header row and any row whose cells aren't article-shaped."""
    pairs: list[tuple[str, str]] = []
    for cells in rows:
        vals = [c.strip() for c in cells if c and c.strip()]
        if len(vals) < 2:
            continue
        old, new = vals[0], vals[1]
        # both cells must look like provision references (drops the header + prose rows)
        if _ARTICLE_REF_RE.search(old) and _ARTICLE_REF_RE.search(new):
            pairs.append((old, new))
    return pairs


def correlation_pairs_from_formex(annex_elem) -> list[tuple[str, str]]:
    """Old→new article pairs from a Formex ``<TBL>`` inside a recast's correlation-table
    annex. Walks each ``ROW`` → its ``CELL``s (localname-based, namespace-agnostic) and
    hands the cell texts to :func:`parse_correlation_table_cells`. Returns [] if the annex
    has no table (many annexes are prose)."""
    def _ln(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def _text(e) -> str:
        return " ".join(t.strip() for t in e.itertext() if t and t.strip())

    rows: list[list[str]] = []
    for row in (e for e in annex_elem.iter() if _ln(e.tag) == "ROW"):
        cells = [_text(c) for c in row if _ln(c.tag) in ("CELL", "TXT.ROW", "TXT.COL")]
        if cells:
            rows.append(cells)
    return parse_correlation_table_cells(rows)


def parse_correlation_table_text(text: str) -> list[tuple[str, str]]:
    """Old→new pairs from a flat two-column correlation table (the text fallback when the
    ``<TBL>`` structure is lost): each line is split on a run of whitespace / a dash / a pipe,
    keeping lines whose first two tokens are both provision references."""
    pairs: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        tokens = [t for t in _DASH_ROW_RE.split(line) if t.strip()]
        if len(tokens) >= 2 and _ARTICLE_REF_RE.fullmatch(tokens[0].strip()) \
                and _ARTICLE_REF_RE.search(tokens[1]):
            pairs.append((tokens[0].strip(), tokens[1].strip()))
    return pairs
