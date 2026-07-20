"""OSCOLA (5th ed.) citation formatting for held documents.

One source of truth for how a document *names itself* in a citation — reused by the
web/MCP surfaces for page titles, the "cited by"/"mentioned by" lists, and the
sidebar reference lists. The formatter degrades gracefully: it builds the fullest
OSCOLA form the stored metadata supports and falls back to the title or stable_id
when a case names itself nowhere and carries no routable identifier.

Output is *structured* — a list of runs, each flagged italic or not — so a renderer
can honour OSCOLA's rule that **case names are italicised** while identifiers, courts
and report references are not, without re-parsing a formatted string.

    >>> cite({"stable_id": "eat/2022/12", "source": "uk-caselaw",
    ...       "doc_type": "judgment", "title": "Guardian News & Media Ltd v Rozanov"})["text"]
    'Guardian News & Media Ltd v Rozanov [2022] EAT 12'
    >>> cite({"stable_id": "ECLI:EU:C:2005:446", "source": "eu-cellar",
    ...       "doc_type": "judgment", "ecli": "ECLI:EU:C:2005:446"},
    ...      {"celex": "62003CJ0403"})["text"]
    'Case C-403/03 EU:C:2005:446'
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..adapters.bailii_corpus import slug_to_citation
from .courts import COURT_ISSUED
from .courts import lookup as courts_lookup

# A run of citation text and whether OSCOLA sets it in italics (case names only).
Part = dict[str, Any]

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _run(text: str, italic: bool = False) -> Part:
    return {"t": text, "i": italic}


def _plain(parts: list[Part]) -> str:
    return "".join(p["t"] for p in parts).strip()


def _pack(parts: list[Part]) -> dict:
    parts = [p for p in parts if p["t"]]
    return {"parts": parts, "text": _plain(parts)}


def _get(doc: Mapping, key: str) -> Any:
    try:
        return doc[key]
    except (KeyError, TypeError):
        return doc.get(key) if hasattr(doc, "get") else None


def _year(doc: Mapping) -> str | None:
    d = _get(doc, "decision_date")
    if d:
        m = re.search(r"(\d{4})", str(d))
        if m:
            return m.group(1)
    return None


def _fmt_date(raw: str | None) -> str | None:
    """A HUDOC/ISO date → OSCOLA day-month-year ("22 June 2004")."""
    if not raw:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(raw))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12):
        return None
    return f"{d} {_MONTHS[mo - 1]} {y}"


# ── EU (CJEU / General Court / AG) ───────────────────────────────────────────

def _celex_case_no(celex: str | None) -> str | None:
    """The court's case number a CJEU CELEX encodes: ``62003CJ0403`` → ``C-403/03``.

    >>> _celex_case_no("62003CJ0403")
    'C-403/03'
    >>> _celex_case_no("62016CC0189")
    'C-189/16'
    >>> _celex_case_no("62018TJ0012")
    'T-12/18'
    """
    m = re.match(r"^6(\d{4})([CTF])[A-Z]{1,2}(\d+)", celex or "")
    if not m:
        return None
    year, court, num = m.group(1), m.group(2), int(m.group(3))
    return f"{court}-{num}/{year[2:]}"


# An AG opinion's title is frequently the delivery boilerplate ("Opinion of
# Advocate General Campos Sánchez-Bordona delivered on 15 January 2020") rather
# than the case name — 6,966 opinions in the corpus. Rendered as-is it lands in
# the italic party slot AND draws a second ", Opinion of AG" tail, so the citation
# reads as neither the case nor a clean opinion label. Detect it, lift the AG's
# name out of it, and keep it OUT of the party slot.
_AG_BOILERPLATE = re.compile(
    r"^(?:Opinion|View)\s+of\s+Advocate\s+General\s+(?P<ag>.+?)"
    r"(?:\s+delivered\b.*)?$", re.IGNORECASE)


# BAILII-derived CJEU titles carry a tail the case name does not include:
#   "KISA (… - Judgment) French Text [2023] EUECJ C-560/21"
# The trailing "[YYYY] EUECJ C-560/21" is a BAILII database reference, not an OSCOLA
# citation — OSCOLA wants "Case C-560/21 … EU:C:2023:461". It is stripped, but the case
# number is *lifted out of it first*, because the BAILII-archive documents carry no CELEX
# and no ECLI, so this tail is the only place their case number appears.
_EUECJ_TAIL_RE = re.compile(
    r"\s*\[(?P<year>\d{4})\]\s*EUECJ\s*(?P<case>[CTF]-\d+/\d+)?[^)\]]*$", re.I)
# "French Text" / "Judgment" / "Order" are document-type markers, not part of the name.
_EU_LANG_MARKER_RE = re.compile(r"\s*\b(?:French|German|Italian|Spanish|Dutch)\s+Text\b", re.I)
_EU_DOCTYPE_MARKER_RE = re.compile(
    r"\s*(?:[-–—]\s*)?\b(?:Judgment|Order|Opinion|Ruling)\b\s*(?=\)|$)", re.I)


def _clean_eu_title(title: str) -> tuple[str, str | None]:
    """Strip BAILII's document-type and database markers from a CJEU title.

    Returns the cleaned name and any case number recovered from the stripped tail.
    """
    case_no = None
    m = _EUECJ_TAIL_RE.search(title)
    if m:
        case_no = m.group("case")
        title = title[:m.start()]
    title = _EU_LANG_MARKER_RE.sub("", title)
    title = _EU_DOCTYPE_MARKER_RE.sub("", title)
    # Some titles already open with the case number ("Case T-372/12 El Corte Ingles …").
    # The formatter puts it in the "Case …" slot itself, so leaving it would print twice.
    lead = re.match(r"^\s*Case\s+(?P<case>[CTF]-\d+/\d+)\s+", title, re.I)
    if lead:
        case_no = case_no or lead.group("case")
        title = title[lead.end():]
    # a parenthetical emptied by the strip ("(Judgment)" → "()") leaves brackets behind
    title = re.sub(r"\s*\(\s*\)", "", title)
    return title.strip(" -–—,"), case_no


def _eu_case(doc: Mapping, meta: Mapping) -> dict | None:
    ecli = _get(doc, "ecli") or ""
    celex = (meta or {}).get("celex")
    case_no = _celex_case_no(celex)
    # The BAILII-archive half of the CJEU corpus keys documents by an ECLI *stable_id*
    # while leaving the ecli column null, so read it from either.
    if not ecli:
        sid = str(_get(doc, "stable_id") or "")
        if sid.upper().startswith("ECLI:"):
            ecli = sid
    ecli_short = ecli[5:] if ecli.upper().startswith("ECLI:") else ecli
    title = (_get(doc, "title") or "").strip()
    title, tail_case_no = _clean_eu_title(title)
    case_no = case_no or tail_case_no
    if not case_no and not ecli_short:
        return None
    is_opinion = (_get(doc, "doc_type") == "opinion"
                  or _get(doc, "court") == "Advocate General")
    # an AG name from metadata, else parsed out of a boilerplate title
    ag = (meta or {}).get("advocate_general") or (meta or {}).get("ag")
    bm = _AG_BOILERPLATE.match(title) if title else None
    if bm:
        ag = ag or bm.group("ag").strip()
        title = ""                       # boilerplate is not the case name
    parts: list[Part] = []
    if case_no:
        parts.append(_run(f"Case {case_no} "))
    if title:
        parts.append(_run(title, italic=True))
        parts.append(_run(" "))
    if ecli_short:
        parts.append(_run(ecli_short))
    # AG opinions / views carry an "Opinion of AG …" tail (name where we have it)
    if is_opinion:
        parts.append(_run(f", Opinion of AG {ag}" if ag else ", Opinion of AG"))
    return _pack(parts)


# ── UK case law (neutral citation from the slug) ─────────────────────────────

def _uk_case(doc: Mapping) -> dict | None:
    slug = _get(doc, "stable_id")
    neutral = slug_to_citation(slug)
    title = (_get(doc, "title") or "").strip()
    parts: list[Part] = []
    if title:
        parts.append(_run(title, italic=True))
    if neutral:
        if parts:
            parts.append(_run(" "))
        parts.append(_run(neutral))
        # Neutral citations began in 2001; a pre-2001 BAILII slug yields a *pseudo*-neutral
        # citation, flagged per OSCOLA so the reader knows it isn't an official one.
        m = re.match(r"\[(\d{4})\]", neutral)
        if m and int(m.group(1)) < 2001:
            parts.append(_run(" (pseudo-neutral citation)"))
    if not parts:
        return None
    return _pack(parts)


# ── Other neutral-citation jurisdictions (CA, AU, NZ, IN, the BAILII long tail) ──

def _neutral_case(doc: Mapping) -> dict | None:
    """A case from any jurisdiction whose stable_id *is* its neutral citation.

    Canadian, Australian, NZ and Indian slugs share the UK's ``court/year/number``
    shape, so the only thing that differs is punctuation — and the court registry
    already records it: Canada and India write "2001 SCC 79" bare, while the UK,
    Australia and NZ bracket the year. Routing them through the UK formatter (which
    hard-codes brackets) would have mis-cited every Canadian case, so the bracket
    style is taken from ``Court.bracketed`` rather than assumed.

    Without this, every non-UK/EU/ECtHR case fell through to the bare stored title —
    which is why the "cited by" list read "Cooper v. Hobart" with a loose year column
    instead of an italicised case name and its citation.
    """
    slug = str(_get(doc, "stable_id") or "")
    parts_ = slug.split("/")
    if len(parts_) < 3:
        return None
    code, year, num = parts_[0], parts_[-2], parts_[-1]
    if not re.fullmatch(r"\d{4}", year) or not code.isalpha():
        return None
    court = courts_lookup(code.upper())
    if court is None:
        return None
    neutral = (f"[{year}] {code.upper()} {num.upper()}" if court.bracketed
               else f"{year} {code.upper()} {num.upper()}")
    title = (_get(doc, "title") or "").strip()
    # OSCOLA renders case names with a plain "v", no full stop — and these corpora are
    # inconsistent about it ("Cooper v. Hobart").
    title = re.sub(r"\bv\.\s", "v ", title)
    out: list[Part] = []
    if title:
        out.append(_run(title, italic=True))
        out.append(_run(" "))
    out.append(_run(neutral))
    # An LII-minted pseudo-neutral citation is not court-issued; say so rather than
    # presenting a database key as though the court had assigned it.
    if court.authority != COURT_ISSUED:
        out.append(_run(" (pseudo-neutral citation)"))
    return _pack(out)


# ── ECtHR (Strasbourg) ───────────────────────────────────────────────────────

_ECHR_PREFIXES = re.compile(
    r"^(?:Grand Chamber hearing|Chamber hearing|Hearing|Judgment|Decision|Press release)\s+",
    re.I)
_ECHR_FORMATION = {"GRANDCHAMBER": "GC", "CHAMBER": "Chamber", "COMMITTEE": "Committee"}


def _echr_case(doc: Mapping, meta: Mapping) -> dict | None:
    meta = meta or {}
    name = (_get(doc, "title") or meta.get("docname") or "").strip()
    # HUDOC docnames carry cruft: a leading doctype word and a trailing dd.mm.yy date.
    name = _ECHR_PREFIXES.sub("", name)
    name = re.sub(r"\s+\d{2}\.\d{2}\.\d{2,4}\s*$", "", name).strip()
    name = re.sub(r"\bv\.\s", "v ", name)
    appno = (meta.get("extractedappno") or meta.get("appno") or "").strip()
    date = _fmt_date(meta.get("kpdate") or _get(doc, "decision_date"))
    formation = _ECHR_FORMATION.get(str(meta.get("doctypebranch") or "").upper().replace(" ", ""))
    if not name and not appno:
        return None
    parts: list[Part] = []
    if name:
        parts.append(_run(name, italic=True))
    if formation:
        parts.append(_run(f" [{formation}]"))
    parts.append(_run(" ECtHR"))
    if appno:
        parts.append(_run(f" App No {appno}"))
    if date:
        parts.append(_run(f" ({date})"))
    return _pack(parts)


# ── dispatch ─────────────────────────────────────────────────────────────────

def cite(doc: Mapping, meta: Mapping | None = None) -> dict:
    """Structured OSCOLA citation for a held document: ``{"parts": [...], "text": str}``.

    ``parts`` is a list of ``{"t": text, "i": italic}`` runs. Always returns a usable
    citation — falls back to the stored title, then the stable_id.
    """
    meta = meta or {}
    source = _get(doc, "source") or ""
    doc_type = _get(doc, "doc_type") or ""
    out: dict | None = None
    if source in ("eu-cellar",) and doc_type in ("judgment", "opinion", "decision"):
        out = _eu_case(doc, meta)
    elif source in ("uk-caselaw", "uk-hol"):
        out = _uk_case(doc)
    elif source == "echr":
        out = _echr_case(doc, meta)
    elif doc_type in ("judgment", "opinion", "decision", "case"):
        # Every other case-law corpus (Canadian A2AJ, Australian, NZ, Indian, the
        # BAILII long tail) keys documents by neutral citation, so they can all be
        # cited properly instead of falling through to a bare title.
        out = _neutral_case(doc)
    if out and out["text"]:
        return out
    # Legislation and everything else: the stored title is already the OSCOLA short form
    # (e.g. "Data Protection Act 2018", or the full EU instrument name); italicise nothing.
    title = (_get(doc, "title") or "").strip()
    if title:
        return _pack([_run(title)])
    return _pack([_run(str(_get(doc, "ecli") or _get(doc, "stable_id") or ""))])
