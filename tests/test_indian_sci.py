"""Supreme Court of India import: identity, report aliases, and the headnote honesty rules.

The dump is one row per *report entry*, not per judgment, and its "text" is a truncated
headnote that is raw OCR for older cases — so the two things most worth pinning are that
repeated rows merge into one judgment carrying all its citations, and that a garbled
headnote never gets stored as if it were the judgment.
"""

from __future__ import annotations

from datetime import date

import pytest

from raglex.adapters.in_caselaw import insc_slug, parse_sci_row, scr_citation
from raglex.config import Config
from raglex.facade import Facade


# ── identity and citation shapes ────────────────────────────────────────────

def test_insc_slug_matches_the_candidate_the_extractor_mints():
    """The slug has to equal what `extract_citations("2020 INSC 387")` produces, or an
    imported case and a reference to it never meet."""
    from raglex.citations import extract_citations

    assert insc_slug("2020INSC387") == "insc/2020/387"
    assert insc_slug("2023 INSC 12") == "insc/2023/12"
    assert insc_slug("2020INSC007") == "insc/2020/7"      # leading zeros normalised
    assert insc_slug("[2020] 7 S.C.R. 674") is None
    assert insc_slug(None) is None

    got = [c.candidate_id for c in extract_citations("see 2020 INSC 387 at [12]")]
    assert "insc/2020/387" in got


def test_scr_citation_normalises_both_report_shapes():
    assert scr_citation("[2020] 7 S.C.R. 674") == "[2020] 7 SCR 674"
    assert scr_citation("[1998] SUPP. 2 S.C.R. 15") == "[1998] Supp 2 SCR 15"
    assert scr_citation("2020 INSC 387") is None
    assert scr_citation(None) is None


def test_headnote_is_metadata_not_text():
    """The headnote is a ~600-char truncated snippet — OCR-garbled for pre-1960s cases —
    so it is never the document's text. Storing it as text would set has_text and drop all
    43k judgments out of the needs-full-text worklist, which is where they belong until
    their PDFs are fetched."""
    garbled = "S.C.R. SUPREME ootJ:R\u2022r REPORTS 58i \"Any dispute or diff~rence ari~ing 195"
    p = parse_sci_row(_row(headnote_text=garbled))
    assert p.headnote == garbled          # kept verbatim, as provenance
    # ...and the import keeps the document textless (asserted in the import tests below)


def _row(**over) -> dict:
    row = {
        "court_code": "SCI",
        "neutral_citation": "2020INSC387",
        "law_report_citation": "[2020] 7 S.C.R. 674",
        "case_title": "PUNJAB NATIONAL BANK & ORS versus ATMANAND SINGH & ORS",
        "decision_date": "2020-05-06",
        "headnote_text": ("Writ - Jurisdiction of - When the petition raises questions of "
                          "fact of a complex nature - the Bank had advanced a term loan."),
        "docket_number": "CIVIL APPEAL No. 2410/2020",
        "cnr_number": "ESCR010004422020",
        "disposition_text": "Appeal(s) allowed",
        "coram_members_text": "A.M. KHANWILKAR; DINESH MAHESHWARI",
        "source_pdf_s3_url": "https://example.invalid/english.tar#member=2020_7_674_694.pdf",
        "bench_name": "2 Judges",
    }
    row.update(over)
    return row


def test_parses_a_row_into_a_keyed_judgment():
    p = parse_sci_row(_row())
    assert p.stable_id == "insc/2020/387"
    assert p.report_citations == ["[2020] 7 SCR 674"]
    assert p.decision_date == date(2020, 5, 6)
    assert p.headnote and "Jurisdiction" in p.headnote


def test_high_court_rows_are_not_this_adapters_business():
    assert parse_sci_row(_row(court_code="10~8")) is None


def test_pre_neutral_citation_cases_keep_a_cnr_surrogate():
    """~152 genuine 1950s Supreme Court cases predate neutral citation. Dropping them would
    discard exactly the old report-only citations the corpus most needs to resolve."""
    p = parse_sci_row(_row(neutral_citation=None, law_report_citation="[1953] 1 S.C.R. 581",
                           cnr_number="ESCR010000851953", headnote_text="ootJ:R•r 58i ~"))
    assert p.stable_id == "insc/cnr/escr010000851953"
    assert p.report_citations == ["[1953] 1 SCR 581"]
    assert p.headnote                    # kept as metadata, never as text


def test_report_entries_for_one_judgment_merge_into_one_document():
    """5,252 neutral citations repeat (up to seven times) because a judgment reported in
    several volumes gets a row each — they must union, not overwrite."""
    a = parse_sci_row(_row(law_report_citation="[2020] 7 S.C.R. 674"))
    b = parse_sci_row(_row(law_report_citation="[2020] SUPP. 1 S.C.R. 12", case_title=None))
    a.merge(b)
    assert a.stable_id == "insc/2020/387"
    assert a.report_citations == ["[2020] 7 SCR 674", "[2020] Supp 1 SCR 12"]
    assert a.title  # the entry that had a title keeps it


# ── the import ──────────────────────────────────────────────────────────────

@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing", embed_model=None,
    ))


def _write_dump(tmp_path, rows: list[dict]):
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    d = tmp_path / "structured" / "v1" / "year=2020"
    d.mkdir(parents=True)
    cols = list(rows[0])
    pq.write_table(pa.table({c: [r.get(c) for r in rows] for c in cols}), d / "part.parquet")
    return str(tmp_path / "structured" / "v1")


def test_import_creates_one_judgment_with_every_report_alias(facade, tmp_path):
    path = _write_dump(tmp_path, [
        _row(law_report_citation="[2020] 7 S.C.R. 674"),
        _row(law_report_citation="[2020] SUPP. 1 S.C.R. 12"),   # same judgment, 2nd entry
        _row(court_code="10~8", neutral_citation=None),          # a High Court row
    ])
    st = facade.import_indian_sci(dir_path=path, extract=False)
    assert st["judgments"] == 1 and st["imported"] == 1

    doc = facade.get_document("insc/2020/387")
    assert doc["stable_id"] == "insc/2020/387"
    with facade._open() as (cat, _rs, _ts):
        # both report citations resolve to the one judgment
        assert cat.get_alias("[2020] 7 scr 674") == "insc/2020/387"
        assert cat.get_alias("[2020] supp 1 scr 12") == "insc/2020/387"
        meta = cat.document_meta("insc/2020/387")
        assert meta["needs_full_text"] is True
        assert meta["headnote"]              # the snippet is kept, as metadata
        assert meta["source_pdf_url"]
        # honestly textless: it still needs its real judgment fetched
        assert cat.get_document("insc/2020/387")["has_text"] == 0


def test_reimport_is_idempotent(facade, tmp_path):
    path = _write_dump(tmp_path, [_row()])
    facade.import_indian_sci(dir_path=path, extract=False)
    again = facade.import_indian_sci(dir_path=path, extract=False)
    assert again["imported"] == 0 and again["skipped"] == 1
