"""Documents laid before the Houses of the Oireachtas.

The catalogue behind this source fails quietly in three ways, and each has a test here:
a sort field it does not declare returns HTTP 200 with the results unsorted, ``-"value"``
looks like exclusion but is a fuzzy match, and a search result carries neither the date
laid nor the DL number however you ask for it.
"""

from __future__ import annotations

import base64
from datetime import date

import pytest

from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.adapters.ie_oireachtas import (
    DEFAULT_FLOOR_YEAR, PAGE_SIZE, SORT_NEWEST_FIRST, STATUTORY_INSTRUMENT,
    STATUTORY_INSTRUMENT_TYPO, OireachtasLaidAdapter, awql, date_window, detail_record,
    doc_type_for, file_extension, laid_under, landing_url, month_windows,
    parse_catalogue_date, parse_size_kb, record_url, search_results, sniff_extension,
    split_list, stable_id,
)
from raglex.core.models import DocType, RelationshipType, ResolutionStatus


# A real search result and its real detail record (DL209743 / NAMA), trimmed.
SUMMARY_RECORD = {
    "creator": "National Asset Management Agency (Ireland);National Asset Management "
               "Agency;Aileen Gleeson (DFIN)",
    "product_size": "1,673 KB",
    "field17": "National Asset Management Agency act 2009 -- Section 55(3)",
    "title": "NAMA quarterly report and accounts (section 55 NAMA act 2009) : 30 June 2025",
    "uuid": "bbd2d52a-7ee0-44d2-b493-bb8682dfb366",
    "legacy_virtual_path": "https://opac.oireachtas.ie/Data/Library3/Documents%20Laid/"
                           "2025/pdf/NAMAdoclaid221025_103758.pdf",
    "lb_document_id": "101039",
    "library": "library3_lib",
    "field26": "2025",
    "document_path": "awss3://ptfs-oireachtas/KV6Data/OPAC/Library3/"
                     "bbd2d52a-7ee0-44d2-b493-bb8682dfb366/NAMAdoclaid221025_103758.pdf",
    "id": "101039",
}

DETAIL_RECORD = {
    **SUMMARY_RECORD,
    "notes": "Official laid version available at: DL209743.",
    "subject": "National Asset Management Agency (Ireland) -- Finance.",
    "corpauthor": "National Asset Management Agency (Ireland)",
    "field20": "DL209743",
    "contributor": "National Asset Management Agency",
    "issued_date": "Wed Oct 22 10:37:58 GMT 2025",
    "field24": "None",
    "field15": "No",
    "field6": "Dáil and Seanad",
    "browse2": "Documents Laid",
    "browse3": "2025",
    "usertext4": "English",
    "db_description": "34 pages.",
}


def _search_payload(records: list[dict], total: int) -> dict:
    return {"csw:GetRecordsResponse": {"csw:SearchResults": {
        "numberOfRecordsMatched": total,
        "numberOfRecordsReturned": len(records),
        "iStoreRecord": records,
    }}}


def _constraint_of(params: dict) -> str:
    return base64.b64decode(params["CONSTRAINT"]).decode("utf-8")


class FakeCatalogue:
    """Stands in for the CSW endpoint, answering off the constraint it is given."""

    def __init__(self, by_window: dict[str, list[dict]], detail: dict | None = None):
        self.by_window = by_window
        self.detail = detail or DETAIL_RECORD
        self.constraints: list[str] = []
        self.sorts: list[str] = []

    def __call__(self, params: dict) -> dict:
        if params.get("REQUEST") == "GetRecordById":
            return {"csw:GetRecordByIdResponse": {"iStoreRecord": self.detail}}
        constraint = _constraint_of(params)
        self.constraints.append(constraint)
        self.sorts.append(params.get("SORTBY", ""))
        for window, records in self.by_window.items():
            if window in constraint:
                start = int(params["STARTPOSITION"])
                return _search_payload(records[start - 1:start - 1 + PAGE_SIZE],
                                       len(records))
        return _search_payload([], 0)


def _adapter(fake: FakeCatalogue, **kwargs) -> OireachtasLaidAdapter:
    adapter = OireachtasLaidAdapter(**kwargs)
    adapter._call = fake          # noqa: SLF001 — the whole network surface is one method
    adapter._token = "test-token"  # noqa: SLF001 — pretend the session is already open
    return adapter


# --- registration -------------------------------------------------------------------

def test_registered():
    info = SOURCE_INFO["ie-oireachtas"]
    assert "ie-oireachtas" in ADAPTERS
    assert info.kind == "preparatory"
    # "IE", the code JURISDICTION_LABELS declares — an undeclared code becomes its own
    # top-level heading, away from every other Irish source.
    assert info.jurisdiction == "IE"
    # Each month is a server-side date filter, so keep-current is one bounded request.
    assert INCREMENTAL_MODE["ie-oireachtas"] == "server"
    assert ADAPTERS["ie-oireachtas"]().source == "ie-oireachtas"


def test_every_declared_option_is_accepted_by_the_constructor():
    for option in SOURCE_INFO["ie-oireachtas"].options:
        ADAPTERS["ie-oireachtas"](**{option.name: option.placeholder})


def test_sort_field_is_one_the_catalogue_declares():
    """A SORTBY naming a field the catalogue does not have returns 200 with the results
    in internal id order — no error, no sort. A newest-first sweep that got the name
    wrong would silently harvest 1922 first and look like it worked."""
    field, _, direction = SORT_NEWEST_FIRST.partition(":")
    assert field in {"issued_date", "load_date", "lb_document_id", "aw_rank",
                     "AW5_SORT_BY_FINE_RANK"}
    assert direction == "DESC"


# --- the query language -------------------------------------------------------------

def test_statutory_instruments_are_excluded_by_default_by_both_spellings():
    """``NOT "Statutory Instrument"`` leaves behind the four records the catalogue spells
    ``Stautory Instrument``, and ``-"Statutory Instrument"`` is not exclusion at all — it
    matches fuzzily and returns exactly those four."""
    constraint = _adapter(FakeCatalogue({}))._plans(
        date(2026, 1, 1), date(2026, 1, 31), ["Documents Laid"], [])[0][0]
    assert f'NOT ("{STATUTORY_INSTRUMENT}" OR "{STATUTORY_INSTRUMENT_TYPO}")' in constraint
    assert '-"' not in constraint


def test_an_explicit_subcollection_list_replaces_the_exclusion():
    adapter = _adapter(FakeCatalogue({}), subcollections="Committee Report")
    constraint = adapter._plans(date(2026, 1, 1), date(2026, 1, 31),
                                ["Documents Laid"], [])[0][0]
    assert 'usertext17=["Committee Report"]' in constraint
    assert "NOT" not in constraint


def test_star_means_the_whole_register_instruments_included():
    adapter = OireachtasLaidAdapter(subcollections="*")
    assert adapter.subcollections == ()
    assert adapter.include_statutory_instruments is True


def test_awql_uses_or_inside_the_brackets():
    constraint = awql(collections=("Documents Laid", "L&RS Publications"))
    assert 'browse2=["Documents Laid" OR "L&RS Publications"]' in constraint
    assert "queryType=[16]" in constraint


def test_date_window_is_month_day_year():
    assert date_window(date(1996, 1, 1), date(2026, 12, 31)) == "01/01/1996-12/31/2026"


# --- windows ------------------------------------------------------------------------

def test_month_windows_run_newest_first_and_stop_at_the_floor():
    windows = month_windows(date(2026, 6, 10), date(2026, 8, 11))
    assert windows == [
        (date(2026, 8, 1), date(2026, 8, 11)),
        (date(2026, 7, 1), date(2026, 7, 31)),
        (date(2026, 6, 10), date(2026, 6, 30)),
    ]


def test_the_newest_window_never_asks_for_the_future():
    _start, end = month_windows(date(2026, 1, 1), date(2026, 8, 11))[0]
    assert end == date(2026, 8, 11)


def test_a_thirty_year_backfill_is_a_bounded_number_of_windows():
    windows = month_windows(date(DEFAULT_FLOOR_YEAR, 1, 1), date(2026, 8, 11))
    assert len(windows) == (2026 - DEFAULT_FLOOR_YEAR) * 12 + 8


# --- the catalogue's encodings ------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("Wed Oct 22 10:37:58 GMT 2025", date(2025, 10, 22)),
    ("Fri Jul 31 10:26:21 IST 2026", date(2026, 7, 31)),   # the zone is the server's
    ("Tue Jun 25 00:00:00 GMT 1935", date(1935, 6, 25)),
    ("", None), (None, None), ("2025-10-22", None),
])
def test_parse_catalogue_date(value, expected):
    assert parse_catalogue_date(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("1,673 KB", 1673), ("95 KB", 95), ("29,073 KB", 29073), ("", None), (None, None),
])
def test_parse_size_kb(value, expected):
    assert parse_size_kb(value) == expected


def test_split_list_reads_the_catalogues_semicolons():
    assert split_list(DETAIL_RECORD["creator"])[0] == \
        "National Asset Management Agency (Ireland)"
    assert len(split_list(DETAIL_RECORD["creator"])) == 3
    assert split_list(None) == []


def test_search_results_accepts_a_single_record_that_is_not_in_a_list():
    records, total = search_results(
        {"csw:GetRecordsResponse": {"csw:SearchResults": {
            "numberOfRecordsMatched": 1, "iStoreRecord": SUMMARY_RECORD}}})
    assert [r["id"] for r in records] == ["101039"] and total == 1


def test_detail_record_of_an_empty_payload_is_a_dict():
    assert detail_record({}) == {}


# --- identity and links -------------------------------------------------------------

def test_identity_is_the_catalogue_document_id():
    """The DL number is what an Order Paper cites, but a SEARCH RESULT does not carry it
    — only the per-record detail call does. Minting identity from the detail would mean
    fetching every record in the register to learn which ones we already hold, so the id
    is the catalogue's own and the DL number becomes a citation alias."""
    assert stable_id(SUMMARY_RECORD) == "ie/oireachtas/opac/101039"
    assert stable_id({}) is None
    assert "field20" not in SUMMARY_RECORD


def test_the_file_url_is_the_static_path_and_the_landing_url_is_the_record():
    assert record_url(SUMMARY_RECORD).endswith("NAMAdoclaid221025_103758.pdf")
    assert record_url(SUMMARY_RECORD).startswith("https://")
    assert "lb_document_id%3D101039" in landing_url(SUMMARY_RECORD)


def test_a_record_with_no_plain_url_falls_back_to_the_document_service():
    url = record_url({"uuid": "abc", "document_path": "awss3://x/y.pdf"})
    assert url.endswith("/awdocumentprovider/uuid/abc")


def test_http_only_legacy_paths_are_upgraded():
    assert record_url({"legacy_virtual_path":
                       "http://opac.oireachtas.ie/AWData/Library3/Library2/DL008762.pdf"}
                      ).startswith("https://")


def test_file_extension_reads_either_path():
    assert file_extension(SUMMARY_RECORD) == "pdf"
    assert file_extension({"document_path": "awss3://b/k/report.DOCX"}) == "docx"
    assert file_extension({"document_path": "awss3://b/k/nodot"}) is None


# --- the laid-under provision -------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("National Asset Management Agency act 2009 -- Section 55(3)",
     ("National Asset Management Agency Act 2009", "Section 55(3)")),
    ("European Communities act 1972 (as amended) -- Section 3A",
     ("European Communities Act 1972 (as amended)", "Section 3A")),
    ("Water Safety Ireland (establishment) order 2019 (S.I. no. 56 of 2019) -- Article 12(2)",
     ("Water Safety Ireland (establishment) Order 2019 (S.I. no. 56 of 2019)",
      "Article 12(2)")),
    ("EU (Scrutiny) Act 2002", ("EU (Scrutiny) Act 2002", None)),
])
def test_laid_under_reads_the_act_and_the_pinpoint(value, expected):
    assert laid_under(value) == expected


@pytest.mark.parametrize("value", [
    "N/A", "", None,
    # Standing orders are Oireachtas procedure, not legislation, and would resolve to
    # nothing; every Act and S.I. title carries a year, so requiring one excludes them.
    "SO 109 Dáil and SO 86 Seanad", "SO 86 Seanad",
])
def test_laid_under_ignores_what_is_not_legislation(value):
    assert laid_under(value) is None


def test_doc_type_defaults_to_preparatory_for_an_untagged_record():
    """Roughly half the laid register carries no subcollection: those are the annual
    reports, accounts and treaty texts this source exists for."""
    assert doc_type_for(None) is DocType.PREPARATORY
    assert doc_type_for("Committee Report") is DocType.PREPARATORY
    assert doc_type_for("EU Scrutiny Information Note") is DocType.NOTE
    assert doc_type_for(STATUTORY_INSTRUMENT) is DocType.LEGISLATION


# --- discovery ----------------------------------------------------------------------

def test_discovery_windows_the_search_and_carries_the_window_as_its_cursor():
    """Discovery cannot know a record's date laid — the search element set is fixed and
    has no date in it — so the cursor is the window the record was found in. It costs one
    re-read of the current month per incremental run."""
    fake = FakeCatalogue({"08/01/2026-08/11/2026": [SUMMARY_RECORD]})
    stubs = list(_adapter(fake).discover(None, today=date(2026, 8, 11)))
    assert [s.stable_id for s in stubs] == ["ie/oireachtas/opac/101039"]
    assert stubs[0].hints["watermark"] == "2026-08-01"
    assert stubs[0].hint_date is None
    assert "issued_date=[08/01/2026-08/11/2026]" in fake.constraints[1]
    assert all(sort == SORT_NEWEST_FIRST for sort in fake.sorts)


def test_an_incremental_run_asks_only_from_the_cursor():
    fake = FakeCatalogue({})
    list(_adapter(fake).discover("2026-07-15", today=date(2026, 8, 11)))
    windows = [c for c in fake.constraints if "issued_date" in c]
    assert "issued_date=[08/01/2026-08/11/2026]" in windows[-2]
    assert "issued_date=[07/15/2026-07/31/2026]" in windows[-1]
    assert not [c for c in windows if "06/" in c]


def test_the_configured_floor_bounds_a_backfill():
    fake = FakeCatalogue({})
    list(_adapter(fake, since_year=2025).discover(None, today=date(2026, 8, 11)))
    windows = [c for c in fake.constraints if "issued_date=[" in c]
    assert any("01/01/2025-01/31/2025" in c for c in windows)
    assert not any("/2024]" in c or "-12/31/2024" in c for c in windows)


def test_a_month_in_which_nothing_was_laid_does_not_spend_the_page_budget():
    """Empty months are cheap and the window list is finite, so counting them would let a
    quiet summer exhaust max_pages before a single document was yielded."""
    fake = FakeCatalogue({"05/01/2026-05/31/2026": [SUMMARY_RECORD]})
    stubs = list(_adapter(fake).discover(None, max_pages=1, today=date(2026, 8, 11)))
    assert [s.stable_id for s in stubs] == ["ie/oireachtas/opac/101039"]


def test_discovery_reports_the_registers_own_total_and_a_resumable_offset():
    records = [dict(SUMMARY_RECORD, id=str(i), lb_document_id=str(i)) for i in range(3)]
    # The whole-sweep span answers the one unpaged count request; the month answers the
    # walk. The count is the catalogue's own, so the job draws a real progress bar.
    fake = FakeCatalogue({f"01/01/{DEFAULT_FLOOR_YEAR}-08/11/2026": records,
                          "08/01/2026-08/11/2026": records})
    stubs = list(_adapter(fake).discover(None, today=date(2026, 8, 11)))
    assert [s.hints["feed_total"] for s in stubs] == [3, 3, 3]
    assert [s.hints["resume_offset"] for s in stubs] == [0, 1, 2]


def test_a_resumed_run_skips_what_it_already_yielded():
    """Emitting resume_offset obliges the adapter to accept it back."""
    records = [dict(SUMMARY_RECORD, id=str(i), lb_document_id=str(i)) for i in range(3)]
    fake = FakeCatalogue({"08/01/2026-08/11/2026": records})
    stubs = list(_adapter(fake, start_offset=2).discover(None, today=date(2026, 8, 11)))
    assert [s.stable_id for s in stubs] == ["ie/oireachtas/opac/2"]
    assert stubs[0].hints["resume_offset"] == 2


def test_a_collection_with_no_date_laid_is_windowed_by_year_instead():
    """377 of the 378 Library & Research Service records have no Date Laid at all, so a
    date window returns nothing for them — the collection would silently vanish."""
    fake = FakeCatalogue({})
    list(_adapter(fake, collections="L&RS Publications", since_year=2024)
         .discover(None, today=date(2026, 8, 11)))
    assert len(fake.constraints) == 2          # the total, then the one walk
    assert 'browse3=["2026" OR "2025" OR "2024"]' in fake.constraints[-1]
    assert "issued_date" not in fake.constraints[-1]


# --- fetch --------------------------------------------------------------------------

_PDF = b"%PDF-1.4 pretend"


def _fetched(adapter: OireachtasLaidAdapter, stub, blob=_PDF, text="x" * 900):
    adapter._document = lambda *_a, **_k: blob                       # noqa: SLF001
    adapter._text = lambda *_a, **_k: (text, False, "pypdf")         # noqa: SLF001
    return adapter.fetch(stub)


def _one_stub(adapter: OireachtasLaidAdapter):
    return next(iter(adapter.discover(None, today=date(2026, 8, 11))))


def test_a_fetched_record_carries_the_dl_number_as_a_citation_alias():
    """The DL number is how the Order Papers cite a laid document, and identity had to be
    minted before it was known — so it is registered as an alias rather than lost."""
    fake = FakeCatalogue({"08/01/2026-08/11/2026": [SUMMARY_RECORD]})
    adapter = _adapter(fake)
    record = _fetched(adapter, _one_stub(adapter))
    assert record.extra["aliases"] == ["DL209743"]
    assert record.extra["dl_number"] == "DL209743"
    assert record.decision_date == date(2025, 10, 22)
    assert record.extra["laid_before"] == "Dáil and Seanad"
    assert record.court == "oireachtas"
    assert record.doc_type is DocType.PREPARATORY


def test_the_enabling_provision_becomes_an_edge():
    """The Act that obliged a report to be laid is named in the catalogue and, very
    often, nowhere in the PDF — so it is recorded as structured metadata, not left to a
    grammar reading the text."""
    fake = FakeCatalogue({"08/01/2026-08/11/2026": [SUMMARY_RECORD]})
    adapter = _adapter(fake)
    record = _fetched(adapter, _one_stub(adapter))
    edge, = record.relations
    assert edge.relationship_type is RelationshipType.MENTIONS
    assert edge.raw_citation_string == \
        "Section 55(3) of the National Asset Management Agency Act 2009"
    assert edge.dst_anchor == "Section 55(3)"
    assert edge.resolution_status is ResolutionStatus.PENDING


def test_a_spreadsheet_is_not_a_document_to_read():
    """Only PDF and DOCX have a text engine behind them; byte-decoding an XLSX stores
    zip noise as the document's text."""
    fake = FakeCatalogue({"08/01/2026-08/11/2026": [
        dict(SUMMARY_RECORD, legacy_virtual_path="https://x/y.xlsx",
             document_path="awss3://x/y.xlsx")]})
    adapter = _adapter(fake)
    assert _fetched(adapter, _one_stub(adapter)) is None


def test_a_plate_scan_past_the_size_limit_is_skipped():
    fake = FakeCatalogue({"08/01/2026-08/11/2026": [
        dict(SUMMARY_RECORD, product_size="900,000 KB")]})
    adapter = _adapter(fake)
    assert _fetched(adapter, _one_stub(adapter)) is None


def test_an_unreadable_scan_is_stored_flagged_rather_than_dropped():
    """needs_ocr must mean "this could not be read", never "this was a scan" — a missing
    OCR stack is a review item, and dropping the record hides it."""
    fake = FakeCatalogue({"08/01/2026-08/11/2026": [SUMMARY_RECORD]})
    adapter = _adapter(fake)
    adapter._document = lambda *_a, **_k: _PDF                    # noqa: SLF001
    adapter._text = lambda *_a, **_k: ("", True, "pypdf")         # noqa: SLF001
    record = adapter.fetch(_one_stub(adapter))
    assert record is not None and record.extra["needs_ocr"] is True


def test_bytes_that_are_neither_format_are_not_guessed_at():
    """A record whose path carries no extension is read off its bytes. Assuming DOCX
    would hand a JPEG to a zip reader and store the empty result as the text."""
    assert sniff_extension(b"%PDF-1.7") == "pdf"
    assert sniff_extension(b"PK\x03\x04rest") == "docx"
    assert sniff_extension(b"\xff\xd8\xff\xe0jpeg") is None
    fake = FakeCatalogue({"08/01/2026-08/11/2026": [
        dict(SUMMARY_RECORD, legacy_virtual_path="https://opac.oireachtas.ie/Data/x/y",
             document_path="awss3://x/y")]})
    adapter = _adapter(fake)
    assert _fetched(adapter, _one_stub(adapter), blob=b"\xff\xd8\xff\xe0jpeg") is None


def test_a_docx_is_read_by_its_own_engine():
    fake = FakeCatalogue({"08/01/2026-08/11/2026": [
        dict(SUMMARY_RECORD, legacy_virtual_path="https://x/report.docx",
             document_path="awss3://x/report.docx")]})
    adapter = _adapter(fake)
    seen: dict = {}

    def _text(blob, extension):
        seen["ext"] = extension
        return "committee report text", False, "docx-zip"

    adapter._document = lambda *_a, **_k: b"PK\x03\x04"           # noqa: SLF001
    adapter._text = _text                                         # noqa: SLF001
    record = adapter.fetch(_one_stub(adapter))
    assert seen["ext"] == "docx" and record.raw_ext == "docx"
