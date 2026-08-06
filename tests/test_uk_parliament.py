"""UK Parliament sources: committee publications and the written Q&A record.

Both are registered, both key on the identity the material is actually cited by, and both
gate retrieval on the grammars finding a statute or an authority — most written questions
are about bus routes.
"""

from __future__ import annotations

from datetime import date

import pytest

from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.adapters.uk_parl_committees import (
    DEFAULT_TYPES, SORT_NEWEST_FIRST, paper_number, publication_stubs,
    stable_id as pub_id,
)
from raglex.adapters.uk_parl_written_questions import (
    COOLDOWN_STEPS, is_answered, question_stubs, question_text, recheck_due,
    stable_id as wq_id,
)


@pytest.mark.parametrize("key,kind", [
    ("uk-parl-committees", "preparatory"),
    ("uk-parl-written-questions", "preparatory"),
])
def test_registered(key, kind):
    assert key in ADAPTERS and key in SOURCE_INFO
    assert SOURCE_INFO[key].kind == kind
    assert SOURCE_INFO[key].jurisdiction == "UK"
    # both filter server-side on a date, which is what makes keep-current one request
    assert INCREMENTAL_MODE[key] == "server"
    assert ADAPTERS[key]().source == key


def test_every_declared_option_is_accepted_by_the_constructor():
    for key in ("uk-parl-committees", "uk-parl-written-questions"):
        for option in SOURCE_INFO[key].options:
            ADAPTERS[key](**{option.name: option.placeholder or "1"})


def test_sort_order_is_one_the_api_actually_defines():
    """An invented sort value is an HTTP 400, and a 400 inside discovery is silent — the
    sweep yields nothing and reads as "no publications". This is exactly what happened."""
    assert SORT_NEWEST_FIRST in (
        "PublicationDateDescending", "PublicationDateAscending",
        "ResponseDateDescending", "ResponseDateAscending")


# --- committee publications --------------------------------------------------

_PUB_PAYLOAD = {
    "totalResults": 8507,
    "items": [
        {"id": 54558, "description": "1st Report - Shifting heaven and earth?",
         "publicationStartDate": "2026-07-30T00:01:00",
         "hcNumber": {"number": "HC 69", "sessionDescription": "2026-27"},
         "hlPaper": None, "type": {"id": 1, "name": "Report"},
         "committee": {"name": "Defence Committee", "house": "Commons"},
         "documents": [{"documentId": 302493}],
         "additionalContentUrl": "https://publications.parliament.uk/x/report.htm"},
        {"id": 54592, "description": "1st Report - Paws for concern",
         "publicationStartDate": "2026-08-05T00:01:00",
         "hcNumber": None,
         "hlPaper": {"number": "HL Paper 45", "sessionDescription": "2026-27"},
         "type": {"id": 1, "name": "Report"},
         "committee": {"name": "Environment Committee", "house": "Lords"},
         "documents": [],
         "additionalContentUrl": "https://publications.parliament.uk/y/4502.htm"},
        {"id": 999, "description": "A letter with no paper number",
         "publicationStartDate": "2026-08-01T00:01:00",
         "hcNumber": None, "hlPaper": None, "type": {"id": 3, "name": "Correspondence"},
         "committee": {"name": "Defence Committee", "house": "Commons"},
         "documents": [], "additionalContentUrl": ""},
    ],
}


def test_identity_is_the_paper_number_with_its_session():
    """A committee report is cited as "HC 69 (2026-27)", and paper numbers repeat every
    session — there is an HC 69 in most of them — so the session is part of the identity,
    not decoration."""
    stubs = {s.stable_id: s for s in publication_stubs(_PUB_PAYLOAD)}
    assert "uk/parl/committee/hc-69-2026-27" in stubs
    assert "uk/parl/committee/hl-45-2026-27" in stubs
    got = stubs["uk/parl/committee/hc-69-2026-27"]
    assert got.hint_date == date(2026, 7, 30)
    assert got.hints["committee"] == "Defence Committee"
    assert paper_number(_PUB_PAYLOAD["items"][0]) == ("HC 69", "2026-27")


def test_a_publication_with_no_paper_number_falls_back_honestly():
    """Much correspondence genuinely has no citable number; inventing one would be worse
    than using the API's internal handle."""
    assert pub_id(_PUB_PAYLOAD["items"][2]) == "uk/parl/committee/pub-999"


def test_default_types_exclude_the_documents_that_cite_nothing():
    """Attendance statistics (9), gender balance (11), declarations of interest (7) and
    agendas (5) are tables of numbers — thousands of documents with no legal content."""
    assert set(DEFAULT_TYPES).isdisjoint({5, 7, 9, 11})
    assert 1 in DEFAULT_TYPES and 2 in DEFAULT_TYPES     # reports + government responses


# --- written questions -------------------------------------------------------

def _wq(**over) -> dict:
    base = {
        "id": 1929646, "uin": "HL2522", "house": "Lords",
        "dateTabled": "2026-07-22T00:00:00", "dateForAnswer": "2026-08-05T00:00:00",
        "dateAnswered": "2026-08-05T00:00:00", "answerIsHolding": False,
        "heading": "Windsor Framework Independent Monitoring Panel",
        "questionText": "To ask His Majesty's Government what assessment they have made.",
        "answerText": "The Government remains committed to the Northern Ireland Act 1998.",
        "answeringBodyName": "Northern Ireland Office",
    }
    base.update(over)
    return base


def test_question_and_answer_are_one_document():
    """Split apart, both halves are unreadable: the question is what the answer answers."""
    text = question_text(_wq())
    assert "Question" in text and "Answer — Northern Ireland Office" in text
    assert "Northern Ireland Act 1998" in text


def test_a_holding_answer_is_not_an_answer():
    """A holding answer says the department will reply later. Treating it as the answer
    freezes the placeholder into the corpus and never comes back for the real one."""
    assert is_answered(_wq()) is True
    assert is_answered(_wq(answerIsHolding=True)) is False
    assert is_answered(_wq(dateAnswered=None)) is False


def test_unanswered_questions_carry_a_recheck_date():
    stubs = {s.stable_id: s for s in question_stubs(
        {"results": [{"value": _wq(dateAnswered=None, answerText="")}]})}
    stub = next(iter(stubs.values()))
    assert stub.hints["answered"] is False
    assert stub.hints["recheck_after"] == "2026-08-08"   # due date + first cooldown step


def test_the_cooldown_backs_off_and_then_gives_up():
    """A question still unanswered a month past its date is usually one that never will
    be — prorogation, a withdrawn member, a department that let it lapse. Polling it
    forever costs requests and buys nothing."""
    due = date(2026, 8, 5)
    seen = [recheck_due(due, n) for n in range(len(COOLDOWN_STEPS))]
    assert seen == [due + __import__("datetime").timedelta(days=d) for d in COOLDOWN_STEPS]
    assert recheck_due(due, len(COOLDOWN_STEPS)) is None
    assert recheck_due(None, 0) is None


def test_uin_identity_is_scoped_by_session_year():
    """UINs repeat: HL2522 in one session and the next are different questions."""
    assert wq_id("HL2522", date(2026, 7, 22)) == "uk/parl/wq/2026/HL2522"
    assert wq_id("HL2522", date(2025, 7, 22)) != wq_id("HL2522", date(2026, 7, 22))


def test_a_written_question_builds_a_gated_record():
    """Most written questions are about potholes. Storing them is fine; putting them in
    front of a researcher is not, so retrieval is gated on the grammars finding a statute
    or an authority in the text."""
    from raglex.adapters.uk_parl_written_questions import UKWrittenQuestionsAdapter
    stub = question_stubs({"results": [{"value": _wq()}]})[0]
    record = UKWrittenQuestionsAdapter().fetch(stub)
    assert record is not None
    assert record.extra["require_recognized_legal_citation"] is True
    assert record.extra["uin"] == "HL2522"
    assert record.extra["answered"] is True
    assert record.decision_date == date(2026, 8, 5)
    assert "Northern Ireland Act 1998" in record.text


def test_committee_records_are_gated_too():
    import raglex.adapters.uk_parl_committees as mod
    src = __import__("inspect").getsource(mod.UKCommitteePublicationsAdapter.fetch)
    assert '"require_recognized_legal_citation": True' in src
