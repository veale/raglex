"""Supreme Court of India, from the KanoonGPT ``indian-case-laws`` parquet dump.

The dump covers the Supreme Court and 25 High Courts (~17M rows), but only the Supreme
Court slice (~43k rows) is worth importing as documents, and for one reason: it is the only
part that carries **citations**. Every SCI row has a Supreme Court Reports citation
(``[2020] 7 S.C.R. 674``) and almost all carry the neutral citation (``2020INSC387``); the
High Court rows have neither, so there is nothing to key them by and nothing for the
citation graph to hook into. See the module tests for the shapes.

Two things this adapter exists to get right:

**1. The identity is the neutral citation, and it repeats.** ``2020INSC387`` maps to the
slug ``insc/2020/387`` — exactly the candidate the citation extractor mints for "2020 INSC
387" in someone else's judgment, so a reference resolves against the imported case. But the
dump has one row per *report entry*, not per judgment: 5,252 neutral citations appear more
than once (up to seven times), because a judgment reported across several S.C.R. volumes
gets a row each. Rows must therefore be **merged** by neutral citation, unioning their
report citations, rather than overwriting one another.

**2. The S.C.R. citation is the point.** ``[2020] 7 S.C.R. 674`` is a law-report citation:
the extractor recognises the shape but can derive no candidate id from it, so it stays
unresolved forever unless something tells the corpus which case it names. That is what this
import supplies — a report→case alias per row, which is the same reporter-equivalence value
the BAILII ICLR links carry for English cases.

**What this import does not give you is the judgment**, and that shapes how it is stored.
``headnote_text`` is a truncated snippet (~600 characters, hard-capped) and for pre-1960s
cases it is raw OCR of a scanned report page, frequently garbled. It is kept in **metadata**
and deliberately *not* stored as the document's text: doing so would set ``has_text`` and
drop all 43k judgments out of the "needs full text" worklist, which is precisely where they
belong — every one of them still needs its real judgment fetched from the recorded PDF.
So these documents are held, citable and resolvable, but honestly textless.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# "2020INSC387" — the only shape the dump uses for Supreme Court neutral citations.
_INSC = re.compile(r"^\s*(\d{4})\s*INSC\s*(\d+)\s*$", re.IGNORECASE)
# "[2020] 7 S.C.R. 674" and "[2020] SUPP. 1 S.C.R. 12" are the only two report shapes.
_SCR = re.compile(r"^\s*\[(\d{4})\]\s*(SUPP\.?\s*)?(\d+)\s*S\.?\s*C\.?\s*R\.?\s*(\d+)\s*$",
                  re.IGNORECASE)


def insc_slug(neutral_citation: str | None) -> str | None:
    """A Supreme Court neutral citation → the slug the citation extractor also mints.

    >>> insc_slug("2020INSC387")
    'insc/2020/387'
    >>> insc_slug("2023 INSC 12")
    'insc/2023/12'
    >>> insc_slug("[2020] 7 S.C.R. 674") is None
    True
    """
    m = _INSC.match(neutral_citation or "")
    return f"insc/{m.group(1)}/{int(m.group(2))}" if m else None


def scr_citation(raw: str | None) -> str | None:
    """Normalise a Supreme Court Reports citation, or None if it isn't one.

    >>> scr_citation("[2020] 7 S.C.R. 674")
    '[2020] 7 SCR 674'
    >>> scr_citation("[1998] SUPP. 2 S.C.R. 15")
    '[1998] Supp 2 SCR 15'
    """
    m = _SCR.match(raw or "")
    if not m:
        return None
    year, supp, vol, page = m.group(1), m.group(2), m.group(3), m.group(4)
    return f"[{year}] {'Supp ' if supp else ''}{vol} SCR {page}"


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass(slots=True)
class ParsedSCI:
    """One Supreme Court judgment, merged across however many report-entry rows it has."""

    stable_id: str
    title: str | None
    decision_date: date | None = None
    neutral_citation: str | None = None
    report_citations: list[str] = field(default_factory=list)
    docket_number: str | None = None
    cnr_number: str | None = None
    coram: str | None = None
    bench: str | None = None
    disposition: str | None = None
    pdf_url: str | None = None
    # metadata only, never the document's text — see the module docstring
    headnote: str | None = None

    def merge(self, other: "ParsedSCI") -> None:
        """Fold another report-entry row for the same judgment into this one."""
        for c in other.report_citations:
            if c not in self.report_citations:
                self.report_citations.append(c)
        # keep the fullest headnote across the entries
        if other.headnote and len(other.headnote) > len(self.headnote or ""):
            self.headnote = other.headnote
        for attr in ("title", "decision_date", "docket_number", "cnr_number",
                     "coram", "bench", "disposition", "pdf_url", "neutral_citation"):
            if getattr(self, attr) is None:
                setattr(self, attr, getattr(other, attr))


def parse_sci_row(row: dict) -> ParsedSCI | None:
    """One SCI parquet row → :class:`ParsedSCI`, or None if it can't be keyed at all.

    Identity is the neutral citation. The ~152 rows without one are genuine (mostly 1950s)
    Supreme Court cases that predate neutral citation; they keep a CNR-based surrogate under
    the same ``insc/`` prefix so their report citations still resolve to something and they
    still classify as Indian Supreme Court material."""
    if (row.get("court_code") or "") != "SCI":
        return None
    slug = insc_slug(row.get("neutral_citation"))
    if slug is None:
        cnr = (row.get("cnr_number") or "").strip()
        if not cnr:
            return None
        slug = f"insc/cnr/{cnr.lower()}"

    reports = [c for c in [scr_citation(row.get("law_report_citation"))] if c]
    return ParsedSCI(
        stable_id=slug,
        title=(row.get("case_title") or "").strip() or None,
        decision_date=_as_date(row.get("decision_date")),
        neutral_citation=(row.get("neutral_citation") or "").strip() or None,
        report_citations=reports,
        docket_number=(row.get("docket_number") or "").strip() or None,
        cnr_number=(row.get("cnr_number") or "").strip() or None,
        coram=(row.get("coram_members_text") or "").strip() or None,
        bench=(row.get("bench_name") or "").strip() or None,
        disposition=(row.get("disposition_text") or "").strip() or None,
        pdf_url=(row.get("source_pdf_s3_url") or "").strip() or None,
        headnote=(row.get("headnote_text") or "").strip() or None,
    )


# The columns the importer reads — a narrow projection over a 17M-row, 21GB dump.
SCI_COLUMNS = ["court_code", "neutral_citation", "law_report_citation", "case_title",
               "decision_date", "headnote_text", "docket_number", "cnr_number",
               "disposition_text", "coram_members_text", "source_pdf_s3_url", "bench_name"]
