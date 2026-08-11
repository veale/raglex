"""Evidence given to Oireachtas committees.

The material this source exists for is the half of committee work that is never laid and
so reaches no catalogue. Its two live hazards are covered here: the website answers
RagLex's own User-Agent with a flat 403 while serving any browser, and the complete index
is behind the site's captcha *and* its robots.txt — so the ceiling on coverage is a fact
about the source, not a bug to be fixed later.
"""

from __future__ import annotations

from datetime import date

import pytest

from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.adapters.ie_oireachtas_committees import (
    BROWSER_UA, CANARY, PAGE_HEADERS, REPORTS, SUBMISSIONS,
    OireachtasCommitteeEvidenceAdapter, committee_pages, committee_slugs, file_family,
    parse_documents_page, parse_file_date, stable_id,
)
from raglex.core.errors import RateLimitException
from raglex.core.models import DocType


SUBMISSION_URL = (
    "https://data.oireachtas.ie/ie/oireachtas/committee/dail/33/joint_committee_on_justice"
    "/submissions/2024/2024-10-08_opening-statement-dr-sharon-lambert-senior-lecturer_en.pdf")
REPORT_URL = (
    "https://data.oireachtas.ie/ie/oireachtas/committee/dail/33/joint_committee_on_justice"
    "/reports/2024/2024-05-15_report-on-pre-legislative-scrutiny_en.pdf")

# The real markup of one row, trimmed to what the parser reads.
def _row(url: str, title: str, ext: str = "pdf") -> str:
    return f'''
    <div class="c-publications-list-compact__row">
    <p class="c-publications-list-compact__item-date">Tue,  8 Oct</p>
    <p class="c-publications-list-compact__item-title">{title}</p>
    <p class="c-publications-list-compact__item-button">
        <a href="{url}" class="no-title" data-file-type="{ext}" title="{title} ()"> {title} </a>
    </p>
    </div>'''


PAGE = ("<html><body><h2>Opening statements and submissions</h2>"
        + _row(SUBMISSION_URL, "Opening statement, Dr. Sharon Lambert")
        + "<h2>Reports</h2>"
        + _row(REPORT_URL, "Report on pre-legislative scrutiny")
        + "</body></html>")


class FakeSite:
    """Answers committee pages off a {url: html} map; anything else is a 404."""

    def __init__(self, pages: dict[str, str], api: dict | None = None):
        self.pages = pages
        self.api = api or {"results": [{"debateRecord": {"house": {
            "houseCode": "dail", "houseNo": "33",
            "committeeCode": "joint_committee_on_justice"}}}]}
        self.asked: list[str] = []
        self.headers: list[dict] = []

    #: This site answers an unknown committee slug and a blocked client with the same
    #: 403, which is the whole reason the adapter needs a canary.
    missing_status = 403

    def get(self, url, *, params=None, headers=None, raise_for_4xx=True, **_kw):
        self.asked.append(url)
        self.headers.append(headers or {})
        if url.startswith("https://api.oireachtas.ie"):
            return _Response(200, json_body=self.api)
        if url in self.pages:
            return _Response(200, text=self.pages[url])
        return _Response(self.missing_status, text="forbidden")


class _Response:
    def __init__(self, status, text="", json_body=None, content=b""):
        self.status_code = status
        self.text = text
        self.content = content
        self._json = json_body

    def json(self):
        return self._json


def _adapter(site: FakeSite, **kwargs) -> OireachtasCommitteeEvidenceAdapter:
    return OireachtasCommitteeEvidenceAdapter(client=site, **kwargs)


# --- registration -------------------------------------------------------------------

def test_registered():
    info = SOURCE_INFO["ie-oireachtas-committees"]
    assert "ie-oireachtas-committees" in ADAPTERS
    assert info.kind == "preparatory" and info.jurisdiction == "IE"
    # The pages carry no date filter and no paging: each run re-reads the same rows.
    assert INCREMENTAL_MODE["ie-oireachtas-committees"] == "full-walk"
    assert ADAPTERS["ie-oireachtas-committees"]().source == "ie-oireachtas-committees"


def test_every_declared_option_is_accepted_by_the_constructor():
    for option in SOURCE_INFO["ie-oireachtas-committees"].options:
        ADAPTERS["ie-oireachtas-committees"](**{option.name: option.placeholder})


def test_it_does_not_introduce_itself_as_a_harvester():
    """www.oireachtas.ie answers the shared RagLex User-Agent with a flat 403 and serves
    any browser normally. It is the header alone — not the captcha — so reading that 403
    as "walled" would have emptied this source without a word."""
    assert "RagLex" not in BROWSER_UA and BROWSER_UA.startswith("Mozilla/5.0")
    assert "Accept" in PAGE_HEADERS and "Accept-Language" in PAGE_HEADERS
    site = FakeSite({committee_pages("33", "justice")[0]: PAGE})
    list(_adapter(site, houses="33").discover(None))
    page_calls = [h for u, h in zip(site.asked, site.headers) if "committees" in u]
    assert page_calls and all(h.get("Accept-Language") for h in page_calls)


# --- deriving a committee's page ----------------------------------------------------

@pytest.mark.parametrize("code,expected_first", [
    ("joint_committee_on_justice", "justice"),
    ("select_committee_on_health", "health"),
    ("joint_committee_on_justice_and_equality", "justice-and-equality"),
    # Not a prefix but the committee's actual name — the unstripped form must survive.
    ("committee_of_public_accounts", "committee-of-public-accounts"),
])
def test_slug_derivation(code, expected_first):
    assert committee_slugs(code)[0] == expected_first
    assert committee_slugs(code)[-1] == code.replace("_", "-")


def test_sub_committees_strip_the_longer_prefix_first():
    """"joint_sub_committee_on_" contains "joint_committee_on_" nowhere, but the shorter
    prefixes must not shadow the longer ones as the list grows."""
    assert committee_slugs("joint_sub_committee_on_mental_health")[0] == "mental-health"


def test_pages_carry_no_query_string():
    """robots.txt disallows every query-string URL on this site (Disallow: /*?), which is
    also where the captcha sits. Everything read here is a plain path."""
    assert all("?" not in url for url in committee_pages("33", "justice"))


def test_a_committee_whose_page_does_not_exist_is_skipped_not_retried():
    """The 31st Dáil predates the current website: every one of its committees 404s. That
    is a fact about the site, not a transient error."""
    site = FakeSite({}, api={"results": [{"debateRecord": {"house": {
        "houseCode": "dail", "houseNo": "31", "committeeCode": "joint_committee_on_jobs"}}}]})
    assert list(_adapter(site, first_house=31).discover(None)) == []


def test_the_committee_list_comes_from_the_api_not_the_website_index():
    """The website's index lists the current and previous Dáil (89 committees); the
    open-data API knows every committee that ever met (232)."""
    site = FakeSite({committee_pages("33", "justice")[0]: PAGE})
    list(_adapter(site, houses="33").discover(None))
    assert any(u.startswith("https://api.oireachtas.ie") for u in site.asked)


def test_a_block_stops_the_sweep_instead_of_recording_the_rest_as_empty():
    """A missing committee and a rate-limited client are the SAME 403 on this site. A
    sweep of every committee at 0.6s got the whole IP blocked two-thirds of the way
    through — and without the canary the run would have finished "successfully" with the
    remaining committees recorded as having no documents."""
    api = {"results": [{"debateRecord": {"house": {
        "houseCode": "dail", "houseNo": "33", "committeeCode": f"joint_committee_on_c{i}"}}}
        for i in range(20)]}
    site = FakeSite({}, api=api)          # every page 403s, canary included
    adapter = _adapter(site, houses="33")
    with pytest.raises(RateLimitException):
        list(adapter.discover(None))
    assert CANARY in site.asked
    # It stopped early rather than walking all twenty committees.
    assert len([u for u in site.asked if "/committees/33/" in u]) < 20


def test_an_ordinary_missing_slug_does_not_stop_the_sweep():
    """Each committee is tried under two spellings, so misses are routine — only a RUN of
    them is evidence of a block, and the canary is what settles it."""
    api = {"results": [
        {"debateRecord": {"house": {"houseCode": "dail", "houseNo": "33",
                                    "committeeCode": "joint_committee_on_justice"}}},
        {"debateRecord": {"house": {"houseCode": "dail", "houseNo": "33",
                                    "committeeCode": "joint_committee_on_health"}}}]}
    site = FakeSite({committee_pages("33", "justice")[0]: PAGE,
                     CANARY: "<html>the index</html>"}, api=api)
    stubs = list(_adapter(site, houses="33").discover(None))
    assert len(stubs) == 1                      # justice resolved, health did not
    assert any("/committees/33/health/" in u for u in site.asked)


def test_it_is_paced_slowly_enough_not_to_earn_the_block_again():
    assert OireachtasCommitteeEvidenceAdapter.min_interval >= 2.0


# --- reading a page -----------------------------------------------------------------

def test_rows_are_split_on_their_opening_marker():
    """Every terminator that works for the rows in the middle has nothing to stop on for
    the last one, which is how a page silently loses its final record."""
    rows = parse_documents_page(PAGE)
    assert [r["family"] for r in rows] == [SUBMISSIONS, REPORTS]
    assert rows[0]["title"] == "Opening statement, Dr. Sharon Lambert"


def test_titles_are_unescaped():
    """The page writes names as "O&#039;Sullivan"; the entity would otherwise end up in
    the title, the search index and every citation of the document."""
    page = "<html>" + _row(SUBMISSION_URL, "Correspondence, Mr Doncha O&#039;Sullivan &amp; co")
    assert parse_documents_page(page)[0]["title"] == \
        "Correspondence, Mr Doncha O'Sullivan & co"


def test_the_date_comes_from_the_filename_not_the_row():
    """The row prints "Tue, 8 Oct" with no year at all; the file it links to is named
    2024-10-08_…, so reading the row would date every October item to a guess."""
    assert parse_file_date(SUBMISSION_URL) == date(2024, 10, 8)
    assert "2024" not in PAGE.split("__item-date")[1][:40]
    assert parse_file_date("https://data.oireachtas.ie/x/no-date_en.pdf") is None


def test_a_document_is_classified_by_its_path_not_by_the_heading_above_it():
    """A page that renames its sections must not silently reclassify everything under
    them; the path segment is what the publisher actually asserts."""
    assert file_family(SUBMISSION_URL) == SUBMISSIONS
    assert file_family(REPORT_URL) == REPORTS
    assert file_family("https://data.oireachtas.ie/ie/oireachtas/bill/2026/75/x.pdf") is None


def test_identity_is_the_files_own_path_without_the_year_directory():
    assert stable_id(SUBMISSION_URL) == (
        "ie/oireachtas/committee/dail/33/joint_committee_on_justice/submissions/"
        "2024-10-08_opening-statement-dr-sharon-lambert-senior-lecturer")
    assert stable_id("https://data.oireachtas.ie/ie/oireachtas/committee/dail/33") is None


# --- what is taken ------------------------------------------------------------------

def test_reports_are_left_to_the_catalogue_source_by_default():
    """The same PDFs arrive through ie-oireachtas WITH their date laid, DL number and
    enabling provision. Content-hash dedup is global, so whichever source runs first wins
    the row — and if it were this one the document would lose all of that."""
    site = FakeSite({committee_pages("33", "justice")[0]: PAGE})
    stubs = list(_adapter(site, houses="33").discover(None))
    assert [s.hints["family"] for s in stubs] == [SUBMISSIONS]


def test_include_reports_opts_back_in():
    site = FakeSite({committee_pages("33", "justice")[0]: PAGE})
    stubs = list(_adapter(site, houses="33", include_reports="true").discover(None))
    assert sorted(s.hints["family"] for s in stubs) == [REPORTS, SUBMISSIONS]


def test_the_same_committee_reached_by_two_codes_is_read_once():
    """A joint and a select committee of the same name share one website page, and the
    API lists both codes."""
    site = FakeSite({committee_pages("33", "health")[0]: PAGE},
                    api={"results": [
                        {"debateRecord": {"house": {"houseCode": "dail", "houseNo": "33",
                                                    "committeeCode": "joint_committee_on_health"}}},
                        {"debateRecord": {"house": {"houseCode": "dail", "houseNo": "33",
                                                    "committeeCode": "select_committee_on_health"}}}]})
    stubs = list(_adapter(site, houses="33").discover(None))
    assert len(stubs) == 1


def test_an_incremental_run_drops_what_predates_the_cursor():
    site = FakeSite({committee_pages("33", "justice")[0]: PAGE})
    assert list(_adapter(site, houses="33").discover("2025-01-01")) == []
    assert len(list(_adapter(site, houses="33").discover("2024-01-01"))) == 1


def test_a_spreadsheet_row_is_not_offered_for_fetch():
    page = ("<html>" + _row(SUBMISSION_URL.replace(".pdf", ".xlsx"),
                            "Statistics", ext="xlsx") + "</html>")
    site = FakeSite({committee_pages("33", "justice")[0]: page})
    assert list(_adapter(site, houses="33").discover(None)) == []


# --- fetch --------------------------------------------------------------------------

def _one_stub(adapter):
    return next(iter(adapter.discover(None)))


def test_evidence_is_stored_as_a_note_and_gated_on_citing_law():
    """Sampling found submissions citing statutes and the minutes beside them citing
    nothing; the gate is what keeps the minutes out of retrieval without a brittle rule
    about titles."""
    site = FakeSite({committee_pages("33", "justice")[0]: PAGE})
    adapter = _adapter(site, houses="33")
    stub = _one_stub(adapter)
    adapter._client = _FetchStub(b"%PDF-1.4")                       # noqa: SLF001
    adapter._text = lambda *_a, **_k: ("law " * 300, False, "pypdf")  # noqa: SLF001
    record = adapter.fetch(stub)
    assert record.doc_type is DocType.NOTE
    assert record.extra["require_recognized_legal_citation"] is True
    assert record.decision_date == date(2024, 10, 8)
    assert record.court == "oireachtas"
    assert record.extra["committee_code"] == "joint_committee_on_justice"


def test_a_scan_with_no_text_layer_is_ocrd_rather_than_stored_empty():
    """Committee evidence is often a letter that was signed, printed and scanned back in
    — the first document this source ever fetched was one."""
    site = FakeSite({committee_pages("33", "justice")[0]: PAGE})
    adapter = _adapter(site, houses="33")
    stub = _one_stub(adapter)
    adapter._client = _FetchStub(b"%PDF-1.4")                        # noqa: SLF001
    adapter._text = lambda *_a, **_k: ("OCR'd prose", False, "tesseract")  # noqa: SLF001
    record = adapter.fetch(stub)
    assert record.text == "OCR'd prose"
    assert record.extra["extraction_engine"] == "tesseract"
    assert record.extra["needs_ocr"] is False


def test_bytes_that_are_neither_pdf_nor_docx_are_not_guessed_at():
    site = FakeSite({committee_pages("33", "justice")[0]: PAGE})
    adapter = _adapter(site, houses="33")
    stub = _one_stub(adapter)
    stub.hints["extension"] = None
    adapter._client = _FetchStub(b"\xff\xd8\xff\xe0jpeg")             # noqa: SLF001
    assert adapter.fetch(stub) is None


class _FetchStub:
    def __init__(self, content: bytes):
        self.content = content

    def get(self, *_a, **_kw):
        return _Response(200, content=self.content)
