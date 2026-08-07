"""Commission follow-up to European Parliament resolutions (the EP's "SP" documents).

This is what the Parliament's ``/external-documents`` service actually contains, and
the answer to the obvious question is worth stating plainly: **it is not the research
service.** The endpoint admits exactly one work type, ``ACT_FOLLOWUP``, and every one
of its 4,301 records is a Commission reply to an adopted text — ``answers_to:
eli/dl/doc/TA-10-2025-0343``, titled "Follow up to T10-0343/2025". EPRS studies,
briefings and in-depth analyses exist in the Parliament's document-type vocabulary
(``STUDY``, ``STUDY_BRIEFING``, ``STUDY_DEPTH_ANALYSIS``, ``EP_SUPPORTING_ANALYSE``)
but no endpoint of the Open Data API serves them — ``/documents?work-type=STUDY``
returns nothing — and the Think Tank site that does publish them answers automated
requests with an empty HTTP 202. There is no adapter to write for EPRS here.

What is here is worth having anyway, and for the same reason the resolutions are: a
follow-up is the Commission saying, on the record, what it will and will not do about a
reading of existing law, point by point against the resolution's numbered paragraphs.
Each record is stored with a ``related_to`` edge back to the resolution it answers, so
the pair reads together once both are held.

Note the direction of that edge. The follow-up names the resolution; the resolution
predates it and cannot name the follow-up. The edge is therefore minted from the
follow-up's own metadata, and the resolution's citers view is where it surfaces.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)

BASE = "https://data.europarl.europa.eu/api/v2"
DISTRIBUTION = "https://data.europarl.europa.eu"
PAGE_SIZE = 500

#: ``SP-2026-04-14-TA-10-2025-0343`` — the follow-up's id embeds the adopted text it
#: answers, which is also given explicitly as ``answers_to``.
_ANSWERS = re.compile(r"TA-(\d{1,2})-(\d{4})-(\d{3,4})\s*$", re.I)


def ta_reference(doc_id: str | None) -> str | None:
    """``TA-9-2024-0138`` → ``P9_TA(2024)0138``.

    The edge points at the Parliament's own reference rather than at a CELEX because
    the CELEX cannot be derived: its descriptor is ``AP`` for a legislative resolution
    and ``IP`` for any other, and the ``TA-…`` identifier does not say which. The
    Parliament reference is unambiguous, and ``eu-ep-resolutions`` mints it as an alias
    on every resolution it stores, so the edge resolves against either family."""
    m = _ANSWERS.search((doc_id or "").strip())
    return f"P{m.group(1)}_TA({m.group(2)}){m.group(3)}" if m else None


def _as_date(value: str | None) -> date | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _last_segment(value: str | None) -> str:
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def followup_stubs(payload: dict) -> list[Stub]:
    out: list[Stub] = []
    for work in payload.get("data") or []:
        doc_id = _last_segment(work.get("id"))
        if not doc_id:
            continue
        answers = work.get("answers_to")
        if isinstance(answers, list):
            answers = answers[0] if answers else None
        out.append(Stub(
            stable_id=f"ep/followup/{doc_id}",
            landing_url=f"{BASE}/external-documents/{doc_id}",
            raw_url=f"{BASE}/external-documents/{doc_id}",
            hint_date=_as_date(work.get("document_date")),
            hints={"doc_id": doc_id,
                   "answers_to": _last_segment(answers) or None,
                   "watermark": str(work.get("document_date") or "")[:10] or None},
        ))
    return out


class EPFollowUpsAdapter(BaseAdapter):
    source = "eu-ep-followups"
    min_interval = 0.6

    def __init__(self, *, client: RateLimitedClient | None = None,
                 start_offset: int = 0) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)
        # emitting resume_offset obliges us to accept it back (see be_gba_decisions)
        self.start_offset = max(0, int(start_offset or 0))

    # ---- discovery ---------------------------------------------------------------

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """A full walk, and ``since`` is deliberately ignored.

        ``/external-documents`` takes neither a date filter nor a sort parameter — only
        ``work-type``, ``offset`` and ``limit``. The register therefore has **no order**,
        and every cursor mechanism in the pipeline assumes one: applying ``since`` here
        drops rows from the middle of an offset-paged walk, and the backfill frontier
        (which resumes a later backfill from the point a clean one reached, sound only
        when the feed is newest-first) then cut the walk off entirely — a resumed
        backfill discovered 4 records out of 4,301 and reported itself done.

        So the walk is whole, every time, which is what ``full-walk`` in
        ``INCREMENTAL_MODE`` promises. It costs nine cheap metadata requests and the
        pipeline's held-check skips everything already stored; ``meta.total`` makes the
        progress determinate.
        """
        offset, pages, total = self.start_offset, 0, None
        while True:
            try:
                resp = self._client.get(
                    f"{BASE}/external-documents",
                    params={"format": "application/ld+json", "limit": PAGE_SIZE,
                            "offset": offset},
                    headers={"Accept": "application/ld+json"})
                payload = resp.json() if (resp.content or b"").strip() else {}
            except (FetchError, ValueError):
                return
            rows = followup_stubs(payload)
            if not rows:
                return
            if total is None:
                total = int((payload.get("meta") or {}).get("total") or 0) or None
            for stub in rows:
                stub.hints["feed_total"] = total
                stub.hints["resume_offset"] = offset
                yield stub
            offset += len(rows)
            pages += 1
            if len(rows) < PAGE_SIZE or (total is not None and offset >= total):
                return
            if max_pages is not None and pages >= max_pages:
                return

    # ---- fetch --------------------------------------------------------------------

    def _english_pdf(self, doc_id: str) -> tuple[bytes, str, str | None] | None:
        """The English manifestation's bytes, its URL, and the expression title.

        As with the adopted texts, the distribution path is read off the record rather
        than constructed: it is not derivable from the document id."""
        try:
            resp = self._client.get(
                f"{BASE}/external-documents/{doc_id}",
                params={"format": "application/ld+json"},
                headers={"Accept": "application/ld+json"})
            data = resp.json() if (resp.content or b"").strip() else {}
        except (FetchError, ValueError):
            return None
        best: tuple[str, str | None] | None = None
        for work in data.get("data") or []:
            for expression in work.get("is_realized_by") or []:
                if not str(expression.get("id") or "").endswith("/en"):
                    continue
                title = (expression.get("title") or {}).get("en")
                for manifestation in expression.get("is_embodied_by") or []:
                    href = str(manifestation.get("is_exemplified_by") or "")
                    if href.lower().endswith(".pdf"):
                        best = (href, title)
                        break
        if not best:
            return None
        url = f"{DISTRIBUTION}/{best[0].lstrip('/')}"
        try:
            resp = self._client.get(url)
        except FetchError:
            return None
        content = resp.content or b""
        if getattr(resp, "status_code", 200) >= 400 or not content:
            return None
        return content, url, best[1]

    def fetch(self, stub: Stub) -> Record | None:
        doc_id = stub.hints.get("doc_id") or stub.stable_id.rsplit("/", 1)[-1]
        got = self._english_pdf(doc_id)
        if not got:
            return None
        blob, url, source_title = got
        from ..extraction import extract_bytes

        ext = "pdf" if blob.startswith(b"%PDF") else "html"
        try:
            extracted = extract_bytes(
                blob, ext=ext, mime="application/pdf" if ext == "pdf" else "text/html")
        except ValueError:
            return None
        text = (extracted.text or "").strip()
        if not text:
            return None

        answers = stub.hints.get("answers_to")
        ta = ta_reference(answers)
        relations = []
        if ta:
            relations.append(TypedRelation(
                relationship_type=RelationshipType.RELATED_TO,
                raw_citation_string=f"Follow-up to {ta}", dst_id=ta,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))
        title = source_title or (f"Commission follow-up to {ta}" if ta
                                 else f"Commission follow-up {doc_id}")
        return Record(
            source=self.source, stable_id=stub.stable_id,
            doc_type=DocType.PREPARATORY,
            title=title, language="en", source_language="en",
            decision_date=stub.hint_date,
            landing_url=url, raw_bytes=blob, raw_ext=ext, text=text,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["european-parliament", "commission-follow-up"],
            extra={"jurisdiction": "eu", "ep_document_id": doc_id,
                   "answers_to": answers, "ta_reference": ta,
                   # A follow-up that recites the resolution's paragraph numbers and
                   # nothing else is administrative correspondence, not legal material.
                   # Hold it for the pairing; let the grammars decide whether it earns a
                   # place in retrieval.
                   "require_recognized_legal_citation": True},
        )
