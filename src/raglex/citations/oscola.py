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


def _eu_case(doc: Mapping, meta: Mapping) -> dict | None:
    ecli = _get(doc, "ecli") or ""
    celex = (meta or {}).get("celex")
    case_no = _celex_case_no(celex)
    ecli_short = ecli[5:] if ecli.startswith("ECLI:") else ecli
    title = (_get(doc, "title") or "").strip()
    if not case_no and not ecli_short:
        return None
    parts: list[Part] = []
    if case_no:
        parts.append(_run(f"Case {case_no} "))
    if title:
        parts.append(_run(title, italic=True))
        parts.append(_run(" "))
    if ecli_short:
        parts.append(_run(ecli_short))
    # AG opinions / views carry an "Opinion of AG …" tail (name where we have it).
    if _get(doc, "doc_type") == "opinion" and _get(doc, "court") == "Advocate General":
        ag = (meta or {}).get("advocate_general") or (meta or {}).get("ag")
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
    if out and out["text"]:
        return out
    # Legislation and everything else: the stored title is already the OSCOLA short form
    # (e.g. "Data Protection Act 2018", or the full EU instrument name); italicise nothing.
    title = (_get(doc, "title") or "").strip()
    if title:
        return _pack([_run(title)])
    return _pack([_run(str(_get(doc, "ecli") or _get(doc, "stable_id") or ""))])
