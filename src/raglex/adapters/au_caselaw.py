"""Australian case law — the Open Australian Legal Corpus (JSONL).

Isaacus' `Open Australian Legal Corpus <https://huggingface.co/datasets/isaacus/open-australian-legal-corpus>`_
is a single ~9 GB JSONL file holding ~232k Australian legal documents across the
Commonwealth, NSW, Queensland, WA, SA, Tasmania and Norfolk Island. It is a *local bulk
import*: point ``path`` at the file.

The corpus mixes **legislation and case law** (``type`` is one of ``decision``,
``primary_legislation``, ``secondary_legislation``, ``bill``). This adapter imports the
**decisions** by default, because the statutes are already covered by the live
Australian register adapters ([[australian-legislation]]), which give point-in-time
compilations and a structured amendment graph that a flat text dump cannot. ``types``
overrides that if the bulk legislation text is wanted anyway.

**Identity.** Each record's ``citation`` embeds the neutral citation ("Smith v Jones
[2020] NSWSC 1"), so the stable_id is the same ``nswsc/2020/1`` slug the citation
extractor mints — meaning this import **resolves the Australian citations the corpus is
already pending on** rather than creating parallel nodes. Decisions whose citation is a
law-report or docket reference instead get a surrogate id from the upstream
``version_id``, and their report citation is minted as a resolution alias so they stay
reachable by the form practitioners actually cite.

**Streaming.** The file is far too large to load, so ``discover`` walks it line by line
and carries each record on its stub; the pipeline consumes stubs one at a time, keeping
memory flat regardless of corpus size.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from ..citations.reporters import report_citations
from ..core.adapter import BaseAdapter
from ..core.models import DocType, ExtractedVia, Record, Stub

__all__ = ["AustralianCaseLawAdapter", "au_case_slug", "JURISDICTIONS"]

# The corpus's jurisdiction slugs → the register codes used in au/… ids elsewhere in
# the corpus ([[australian-legislation]]), so both halves speak one vocabulary.
JURISDICTIONS: dict[str, str] = {
    "commonwealth": "cth", "new_south_wales": "nsw", "queensland": "qld",
    "western_australia": "wa", "south_australia": "sa", "tasmania": "tas",
    "victoria": "vic", "australian_capital_territory": "act",
    "northern_territory": "nt", "norfolk_island": "nf",
}

# The bracketed neutral citation carried inside the corpus's citation string:
# "Smith v Jones [2020] NSWSC 1" → nswsc/2020/1. A trailing parenthetical chamber
# ("[2012] UKUT 440 (AAC)"-style) is folded in the same way the extractor folds it.
_AU_NEUTRAL = re.compile(
    r"\[(?P<year>(?:18|19|20)\d{2})\]\s+(?P<court>[A-Z][A-Za-z]{1,9})\s+(?P<num>\d{1,6})")

_DEFAULT_TYPES = ("decision",)


def au_case_slug(citation: str | None) -> str | None:
    """``"Smith v Jones [2020] NSWSC 1"`` → ``"nswsc/2020/1"`` — the same slug the
    citation extractor mints, so an imported decision lands on the node the corpus was
    already citing. None when the citation carries no neutral citation."""
    m = _AU_NEUTRAL.search(citation or "")
    if not m:
        return None
    return f"{m.group('court').lower()}/{m.group('year')}/{int(m.group('num'))}"


def _surrogate_id(jurisdiction: str, version_id: str, citation: str) -> str:
    """A stable id for a decision with no neutral citation, keyed on the upstream
    ``version_id`` so re-importing updates rather than duplicates."""
    seed = (version_id or citation or "").strip().lower()
    digest = hashlib.sha256(seed.encode()).hexdigest()[:16]
    return f"au-case/{jurisdiction}/{digest}"


def report_aliases(citation: str | None) -> list[str]:
    """Alias spellings for a citation that is NOT a neutral citation — a law report or
    docket reference. Minting these lets a pending "(1992) 175 CLR 1" resolve onto the
    held judgment, which its own (surrogate) id could never do. Punctuated and
    unpunctuated spellings both, since an alias only fires on an exact folded match."""
    out: list[str] = []
    for found in report_citations(citation):
        for variant in (found, found.replace(".", "")):
            variant = " ".join(variant.split())
            if variant and variant not in out:
                out.append(variant)
    return out


def _as_date(value: str | None) -> date | None:
    try:
        return datetime.fromisoformat(str(value or "")[:10]).date()
    except ValueError:
        return None


class AustralianCaseLawAdapter(BaseAdapter):
    """Open Australian Legal Corpus decisions, imported from the local JSONL file."""

    source = "au-caselaw"
    min_interval = 0.0        # local filesystem
    requires_js = False
    requires_proxy = False

    def __init__(self, *, path: str | Path | None = None,
                 types: str | tuple[str, ...] = _DEFAULT_TYPES,
                 jurisdictions: str | tuple[str, ...] | None = None,
                 min_year: int | str | None = None) -> None:
        self.path = Path(path).expanduser() if path else None
        if isinstance(types, str):
            types = tuple(t.strip() for t in types.split(",") if t.strip())
        self.types = {t.lower() for t in types} or set(_DEFAULT_TYPES)
        if isinstance(jurisdictions, str):
            jurisdictions = tuple(j.strip() for j in jurisdictions.split(",") if j.strip())
        self.jurisdictions = {j.lower() for j in (jurisdictions or ())}
        self.min_year = int(min_year) if str(min_year or "").strip().isdigit() else None

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.path is None or not self.path.exists():
            return
        cutoff = (since or "")[:10]
        count = 0
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # One malformed line must not sink a 232k-document import.
                    continue
                stub = self._stub(row, cutoff)
                if stub is None:
                    continue
                yield stub
                count += 1
                if max_pages is not None and count >= max_pages * 500:
                    return

    def _stub(self, row: dict, cutoff: str) -> Stub | None:
        if (row.get("type") or "").lower() not in self.types:
            return None
        juris_raw = (row.get("jurisdiction") or "").lower()
        if self.jurisdictions and juris_raw not in self.jurisdictions:
            return None
        text = (row.get("text") or "").strip()
        if not text:
            return None
        citation = (row.get("citation") or "").strip()
        decided = _as_date(row.get("date"))
        if self.min_year and decided and decided.year < self.min_year:
            return None
        stamp = decided.isoformat() if decided else None
        if cutoff and stamp and stamp <= cutoff:
            return None

        jurisdiction = JURISDICTIONS.get(juris_raw, juris_raw or "au")
        stable_id = au_case_slug(citation) or _surrogate_id(
            jurisdiction, row.get("version_id") or "", citation)
        m = _AU_NEUTRAL.search(citation)
        # With no neutral citation there is no court token, so the jurisdiction stands in
        # — it groups the decision under a real place rather than under the upstream
        # scraper's slug ("nsw_caselaw"), which is a data source, not a court.
        court = m.group("court").lower() if m else jurisdiction
        return Stub(
            stable_id=stable_id, title=citation or stable_id,
            landing_url=(row.get("url") or "").strip() or None,
            court=court, hint_date=decided,
            # The row travels on the stub; the pipeline consumes stubs one at a time, so
            # a 9 GB file never needs a second read or a seek table.
            hints={"row": row, "jurisdiction": jurisdiction, "court": court,
                   "watermark": stamp},
        )

    def fetch(self, stub: Stub) -> Record | None:
        row = stub.hints.get("row")
        if not row:
            return None
        text = (row.get("text") or "").strip()
        if not text:
            return None
        citation = (row.get("citation") or "").strip()
        doc_type = (DocType.JUDGMENT if (row.get("type") or "").lower() == "decision"
                    else DocType.LEGISLATION)

        extra = {
            "jurisdiction": stub.hints["jurisdiction"],
            "court_code": stub.hints["court"],
            "citation": citation or None,
            "corpus_type": row.get("type"),
            "upstream_source": row.get("source"),
            "version_id": row.get("version_id"),
            "mime": row.get("mime"),
            "scraped_at": (row.get("when_scraped") or "")[:10] or None,
            # The corpus reproduces official texts but is itself a compilation —
            # reconcile against the court/register where fidelity matters.
            "is_authoritative": False,
            "provider": "Open Australian Legal Corpus (Isaacus)",
            # a decision cited only by law report gets a surrogate id, so its reporter
            # citation is the only way anyone will reach it — mint it as an alias
            "aliases": report_aliases(citation) or None,
            "surrogate_id": stub.stable_id.startswith("au-case/"),
        }

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=doc_type,
            title=citation or stub.stable_id,
            court=stub.hints["court"],
            decision_date=_as_date(row.get("date")),
            language="en", source_language="en",
            landing_url=stub.landing_url,
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in extra.items() if v is not None},
        )
