"""UK Parliament sources: committee publications and the written Q&A record.

Both are registered, both key on the identity the material is actually cited by, and both
gate retrieval on the grammars finding a statute or an authority — most written questions
are about bus routes.
"""

from __future__ import annotations

from datetime import date

import pytest

from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.core.models import Stub
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


def test_a_lords_report_is_read_from_its_pdf_not_one_chapter(monkeypatch):
    """A Lords paper's whole report is the PDF; its HTML is a single numbered chapter.

    So the Lords side of the corpus was either empty or a fragment stored as if it were
    the report. The PDF is Cloudflare-walled and no plain request can have it — a direct
    GET is 403, and so is an XHR from a browser that has already cleared the challenge.
    Only a real navigation returns the file, which is what BrowserBytesFetcher does.
    """
    import raglex.adapters.uk_parl_committees as mod

    stub = {s.stable_id: s for s in publication_stubs(_PUB_PAYLOAD)}["uk/parl/committee/hl-45-2026-27"]
    stub.hints["pdf_url"] = "https://publications.parliament.uk/y/45.pdf"
    asked: dict = {}

    class _Fetcher:
        def available(self): return True

        def fetch_bytes(self, url, *, referer_url=None):
            asked["url"], asked["referer"] = url, referer_url
            return b"%PDF-1.4\n" + b"The Committee recommends. " * 40

    monkeypatch.setattr("raglex.scraping.fetcher.get_bytes_fetcher", lambda: _Fetcher())
    # the routing is what is under test, not pypdf
    from raglex.extraction import Extracted
    monkeypatch.setattr(
        "raglex.extraction.extract_bytes",
        lambda blob, **kw: Extracted(text="The Committee recommends. " * 40,
                                     engine="stub", engine_version="0"))
    ad = mod.UKCommitteePublicationsAdapter()
    rec = ad.fetch(stub)
    assert rec is not None and rec.raw_ext == "pdf"
    assert asked["url"].endswith("45.pdf")
    # cleared via the paper's own page — a cold hit on the file is what gets refused
    assert asked["referer"] == "https://publications.parliament.uk/y/4502.htm"


def test_a_missing_browser_skips_the_paper_rather_than_crashing(monkeypatch):
    # the browser is a 1.3 GB optional layer; an image without it must degrade, not die
    import raglex.adapters.uk_parl_committees as mod

    stub = {s.stable_id: s for s in publication_stubs(_PUB_PAYLOAD)}["uk/parl/committee/hl-45-2026-27"]
    stub.hints["pdf_url"] = "https://publications.parliament.uk/y/45.pdf"

    class _Absent:
        def available(self): return False
        def fetch_bytes(self, url, **kw): raise AssertionError("must not be called")

    monkeypatch.setattr("raglex.scraping.fetcher.get_bytes_fetcher", lambda: _Absent())
    ad = mod.UKCommitteePublicationsAdapter()
    monkeypatch.setattr(ad, "_stealth_html", lambda url: None)
    assert ad.fetch(stub) is None


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


def test_adapters_that_report_resume_offset_accept_it_back():
    """An interrupted harvest is resumed with options["start_offset"] taken from the
    checkpoint. An adapter that reports resume_offset but ignores it on the way back in
    restarts its walk from the top — and, because discovery then re-finds what is already
    held and dedupes it all, reports SUCCESS having done nothing. That is what happened to
    the first Belgian backfill after a deploy interrupted it: 185 of 294 held, resumed
    attempt marked done, 0 new documents."""
    from raglex.adapters.registry import ADAPTERS
    for key in ("be-gba-decisions", "uk-parl-committees", "uk-parl-written-questions"):
        adapter = ADAPTERS[key](start_offset=150)
        assert adapter.start_offset == 150, key


def test_only_the_commons_joint_report_convention_is_rewritten():
    """Commons/Joint reports live at report.html; the .htm form is a cookie-consent shell
    with no report in it. Lords papers are numbered chapter pages whose .html sibling is
    the bare banner — appending an "l" there turns a page with content into one without."""
    from raglex.adapters.uk_parl_committees import UKCommitteePublicationsAdapter as A
    base = "https://publications.parliament.uk/pa"
    assert A.report_url(f"{base}/jt5902/jtselect/jtrights/325/report.htm") == \
        f"{base}/jt5902/jtselect/jtrights/325/report.html"
    assert A.report_url(f"{base}/cm5902/cmselect/cmdfence/69/report.htm").endswith("report.html")
    # Lords: left exactly as the API gave it
    lords = f"{base}/ld5902/ldselect/ldenvcl/45/4502.htm"
    assert A.report_url(lords) == lords
    assert A.report_url("https://example.org/report.htm") == "https://example.org/report.htm"


def test_a_backfill_walks_bounded_windows_because_a_wide_range_is_a_500():
    """Measured against the live API:

        answeredWhenFrom=2014-01-01 -> HTTP 500
        answeredWhenFrom=2026-01-01 -> HTTP 500
        answeredWhenFrom=2026-07-01 -> HTTP 200, 8,083 results

    So a backfill cannot be one open-ended query. Worse, the adapter swallowed that 500
    and broke out of its loop, which is how the first backfill "discovered 0" and reported
    success — an error dressed up as the end of the data."""
    from raglex.adapters.uk_parl_written_questions import WINDOW_DAYS, _date_windows
    windows = list(_date_windows(date(2026, 5, 1), date(2026, 8, 6), WINDOW_DAYS))
    assert len(windows) > 1, "a three-month backfill must be more than one request"
    assert windows[0][0] == date(2026, 5, 1)
    assert windows[-1][1] == date(2026, 8, 6)
    # contiguous and non-overlapping, so nothing is skipped or fetched twice
    for (_, prev_hi), (next_lo, _) in zip(windows, windows[1:]):
        assert next_lo == prev_hi + __import__("datetime").timedelta(days=1)
    assert all((hi - lo).days < WINDOW_DAYS for lo, hi in windows)


def test_an_audio_publication_is_never_handed_to_a_browser():
    """Navigating to media starts a stream, so domcontentloaded never fires and the fetch
    hangs. HC 1880 is such a row — an .mp3 of an evidence session, with backslashes for
    slashes — and it wedged the same harvest twice, fifteen minutes each time."""
    import raglex.adapters.uk_parl_committees as mod

    ad = mod.UKCommitteePublicationsAdapter()
    audio = "https://publications.parliament.uk\\pa\\cm201719\\cmselect\\audio\\audio1.mp3"
    assert ad.report_url(audio).startswith("https://publications.parliament.uk/pa/")
    assert not ad._readable(ad.report_url(audio))
    # a real report is still readable
    assert ad._readable("https://publications.parliament.uk/pa/cm201719/x/1880/1880.pdf")
    assert ad._readable("https://publications.parliament.uk/pa/cm5902/x/69/report.html")
    # and the fetch skips it without touching the network
    stub = Stub(stable_id="uk/parl/committee/hc-1880-2017-19", landing_url=audio,
                raw_url=audio, title="Audio", court="uk-parliament", hints={})
    assert ad.fetch(stub) is None
