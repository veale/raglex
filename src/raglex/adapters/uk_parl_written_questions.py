"""UK parliamentary written questions and their answers.

A written question and the Government's answer are one document: the question asks what
the law requires or how it is being applied, and the answer is the department's position
on it, on the record. Splitting them would leave both halves unreadable, so they are
stored as a single record keyed on the UIN — ``HL2522``, ``12345`` — which is how a
written answer is cited.

**The cooldown.** A question is tabled days before it is answered (Commons convention is
roughly a week; Lords fourteen days; a *named-day* question has an explicit
``dateForAnswer``), and a department may file a *holding* answer that says nothing and is
replaced later. Re-fetching every unanswered question on every run would be pure waste, so
an unanswered or holding-answered question is parked with a due date and only revisited
once an answer is plausibly there.

The efficient half of that is free: the API filters on ``answeredWhenFrom``, so an
incremental run asks "what has been answered since I last looked" and inherently collects
answers to questions tabled long before — no polling, no per-question requests. The
cooldown proper covers the rest: questions whose answer date has passed and which we still
hold unanswered are re-asked for, with a backoff so a question that is never going to be
answered stops costing anything.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub

BASE = "https://questions-statements-api.parliament.uk"
QUESTIONS = f"{BASE}/api/writtenquestions/questions"
PAGE_SIZE = 100

# How long to wait before looking at an unanswered question again. The first re-check
# tracks the answering convention; after that it backs off, because a question still
# unanswered a month past its date is usually one that never will be (prorogation, a
# withdrawn member, a department that has simply let it lapse) and should not be polled
# forever. Days since the date the answer was due.
COOLDOWN_STEPS = (3, 7, 14, 30, 90)


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def recheck_due(due_for_answer: date | None, attempts: int,
                today: date | None = None) -> date | None:
    """When an unanswered question is worth asking about again.

    ``None`` once the backoff is exhausted — the question is left as it stands rather
    than re-fetched indefinitely."""
    if due_for_answer is None:
        return None
    if attempts >= len(COOLDOWN_STEPS):
        return None
    return due_for_answer + timedelta(days=COOLDOWN_STEPS[attempts])


def is_answered(value: dict) -> bool:
    """A holding answer is not an answer. It is the department saying it will reply
    later, and treating it as the answer would freeze the placeholder into the corpus
    and never come back for the real one."""
    if not value.get("dateAnswered"):
        return False
    return not bool(value.get("answerIsHolding"))


def stable_id(uin: str, tabled: date | None) -> str:
    """UINs repeat across sessions, so the tabling date disambiguates. ``HL2522`` in one
    session and the next are different questions."""
    year = tabled.year if tabled else 0
    return f"uk/parl/wq/{year}/{str(uin).strip().upper()}"


def question_text(value: dict) -> str:
    """Question and answer as one readable document, each attributed."""
    parts = []
    heading = " ".join(str(value.get("heading") or "").split())
    if heading:
        parts.append(heading)
    asked = " ".join(str(value.get("questionText") or "").split())
    if asked:
        parts.append(f"Question\n\n{asked}")
    answer = " ".join(str(value.get("answerText") or "").split())
    if answer:
        body = value.get("answeringBodyName") or "the Government"
        label = "Holding answer" if value.get("answerIsHolding") else "Answer"
        parts.append(f"{label} — {body}\n\n{answer}")
    return "\n\n".join(parts).strip()


def question_stubs(payload: dict) -> list[Stub]:
    out: list[Stub] = []
    for row in payload.get("results") or []:
        value = row.get("value") if isinstance(row, dict) else None
        if not isinstance(value, dict) or not value.get("uin"):
            continue
        tabled = _as_date(value.get("dateTabled"))
        due = _as_date(value.get("dateForAnswer"))
        answered = is_answered(value)
        out.append(Stub(
            stable_id=stable_id(str(value["uin"]), tabled),
            landing_url=f"{BASE}/api/writtenquestions/questions/{value.get('id')}",
            raw_url=f"{BASE}/api/writtenquestions/questions/{value.get('id')}",
            title=" ".join(str(value.get("heading") or value.get("uin")).split()),
            court="uk-parliament",
            hint_date=_as_date(value.get("dateAnswered")) or tabled,
            hints={
                "uin": value.get("uin"),
                "question_id": value.get("id"),
                "house": value.get("house"),
                "answered": answered,
                "answer_is_holding": bool(value.get("answerIsHolding")),
                "date_for_answer": due.isoformat() if due else None,
                # What the scheduler needs to leave this alone until it is worth asking
                # again. Absent once answered — there is nothing left to wait for.
                "recheck_after": (None if answered else
                                  (recheck_due(due, 0).isoformat()
                                   if recheck_due(due, 0) else None)),
                "answering_body": value.get("answeringBodyName"),
                "value": value,
                "watermark": (value.get("dateAnswered") or value.get("dateTabled")
                              or "")[:10] or None,
            },
        ))
    return out


class UKWrittenQuestionsAdapter(BaseAdapter):
    source = "uk-parl-written-questions"
    min_interval = 0.4

    def __init__(self, *, client: RateLimitedClient | None = None,
                 include_unanswered: str | None = None, start_offset: int = 0) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)
        # see be_gba_decisions: emitting resume_offset obliges us to accept it back
        self.start_offset = max(0, int(start_offset or 0))
        # Unanswered questions are still legal material — the question itself cites the
        # statute it is about — but they are provisional, so holding them is opt-in.
        self._include_unanswered = str(include_unanswered or "").strip().lower() in (
            "1", "true", "yes", "on")

    # ---- discovery -------------------------------------------------------------

    def _page(self, params: dict) -> dict:
        return self._client.get(QUESTIONS, params=params).json()

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """Incremental runs ask what has been ANSWERED since the cursor.

        That is the whole cooldown, expressed as a filter the API already supports: a
        question tabled in June and answered in August arrives in August's run without
        anyone tracking it in between, and nothing is re-fetched merely to discover it is
        still unanswered."""
        windows: list[dict] = []
        if since:
            windows.append({"answeredWhenFrom": str(since)[:10]})
            if self._include_unanswered:
                windows.append({"tabledWhenFrom": str(since)[:10],
                                "answered": "Unanswered"})
        else:
            windows.append({"answered": "Answered"})
            if self._include_unanswered:
                windows.append({"answered": "Unanswered"})
        for window in windows:
            params = {**window, "take": PAGE_SIZE, "expandMember": "false"}
            skip, total, pages = self.start_offset, None, 0
            while True:
                params["skip"] = skip
                try:
                    payload = self._page(params)
                except (FetchError, ValueError):
                    break
                if total is None:
                    total = int(payload.get("totalResults") or 0)
                rows = question_stubs(payload)
                if not rows:
                    break
                for stub in rows:
                    stub.hints["feed_total"] = total
                    stub.hints["resume_offset"] = skip
                    yield stub
                skip += len(rows)
                pages += 1
                if total is not None and skip >= total:
                    break
                if max_pages is not None and pages >= max_pages:
                    break

    # ---- fetch -----------------------------------------------------------------

    def fetch(self, stub: Stub) -> Record | None:
        """Discovery already carried the whole question, so a second request would buy
        nothing; it is re-read only when the listing did not supply the body."""
        value = stub.hints.get("value")
        if not isinstance(value, dict) or not value.get("questionText"):
            try:
                payload = self._client.get(stub.raw_url).json()
            except (FetchError, ValueError):
                return None
            value = (payload.get("value") if isinstance(payload, dict) else None) or {}
        text = question_text(value)
        if len(text) < 80:
            return None
        answered = is_answered(value)
        uin = str(value.get("uin") or "")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.PREPARATORY,
            title=" ".join(str(value.get("heading") or uin).split()),
            court="uk-parliament",
            decision_date=_as_date(value.get("dateAnswered")) or _as_date(value.get("dateTabled")),
            language="en", source_language="en",
            landing_url=f"https://questions-statements.parliament.uk/written-questions/detail/"
                        f"{str(value.get('dateTabled') or '')[:10]}/{uin}",
            raw_bytes=text.encode("utf-8"), raw_ext="txt", text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["uk-parliament", "written-question",
                        str(value.get("house") or "").lower() or "commons",
                        "answered" if answered else "unanswered"],
            extra={
                "jurisdiction": "uk",
                "uin": uin,
                "house": value.get("house"),
                "answering_body": value.get("answeringBodyName"),
                "date_tabled": str(value.get("dateTabled") or "")[:10] or None,
                "date_for_answer": str(value.get("dateForAnswer") or "")[:10] or None,
                "date_answered": str(value.get("dateAnswered") or "")[:10] or None,
                "is_named_day": bool(value.get("isNamedDay")),
                "answer_is_holding": bool(value.get("answerIsHolding")),
                "answered": answered,
                "recheck_after": stub.hints.get("recheck_after"),
                # Most written questions are about potholes and bus routes. The ones worth
                # holding are the ones that argue about a statute or an authority, and the
                # grammars are what tell those apart — so anything they find no citation
                # in stays stored but out of retrieval.
                "require_recognized_legal_citation": True,
            },
        )
