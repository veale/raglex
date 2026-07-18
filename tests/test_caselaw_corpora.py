"""Bulk case-law corpus importers — the A2AJ Canadian parquet dataset and the Open
Australian Legal Corpus JSONL. Network-free; fixtures mirror the real schemas.
"""

from __future__ import annotations

import json

import pytest

from raglex.adapters.au_caselaw import (
    AustralianCaseLawAdapter,
    au_case_slug,
    report_aliases as au_report_aliases,
)
from raglex.adapters.ca_caselaw import ca_neutral_slug, report_aliases as ca_report_aliases
from raglex.citations.reporters import report_citations
from raglex.core.models import DocType, RelationshipType

# -- identifiers -------------------------------------------------------------


def test_canadian_neutral_citations_are_bare_year_first():
    """Canada writes "2011 SCC 10" with no brackets, and the slug must match the one
    the citation extractor mints so the import resolves pending citations."""
    assert ca_neutral_slug("2011 SCC 10") == "scc/2011/10"
    assert ca_neutral_slug("R. v. Smith, 2011 SCC 10") == "scc/2011/10"
    assert ca_neutral_slug("2019 ONCA 456") == "onca/2019/456"


def test_canadian_report_and_docket_citations_yield_no_slug():
    """Pre-neutral-citation decisions and docket-numbered tribunal matters are not
    citation-addressable — they must fall back to a surrogate, not a guessed slug."""
    assert ca_neutral_slug("[1940] SCR 578") is None
    assert ca_neutral_slug("AP-2019-001") is None
    assert ca_neutral_slug("") is None


def test_australian_neutral_citation_is_extracted_from_the_style_of_cause():
    assert au_case_slug("Smith v Jones [2020] NSWSC 1") == "nswsc/2020/1"
    assert au_case_slug("Commonwealth v X [2019] HCA 23") == "hca/2019/23"
    assert au_case_slug("Mabo v Queensland (No 2) (1992) 175 CLR 1") is None


# -- reporter aliases --------------------------------------------------------

def test_aliases_are_the_matched_report_citation_not_the_whole_citation_string():
    """An alias joins on the edge's folded raw string, so aliasing a whole style-of-cause
    would never fire — only the report citation itself can match."""
    assert au_report_aliases("Mabo v Queensland (No 2) (1992) 175 CLR 1") == ["(1992) 175 CLR 1"]
    assert report_citations("Mabo v Queensland (No 2) (1992) 175 CLR 1") == ["(1992) 175 CLR 1"]


def test_aliases_cover_both_punctuation_spellings():
    """The report grammar matches "[1999] 2 S.C.R. 817" and "[1999] 2 SCR 817" alike but
    folds each to itself, so both spellings need minting."""
    aliases = ca_report_aliases("[1999] 2 S.C.R. 817")
    assert "[1999] 2 S.C.R. 817" in aliases and "[1999] 2 SCR 817" in aliases


def test_a_neutral_citation_is_not_minted_as_a_report_alias():
    """It already resolves through its own slug; aliasing it would duplicate that path."""
    assert ca_report_aliases("2011 SCC 10") == []
    assert au_report_aliases("Smith v Jones [2020] NSWSC 1") == []


# -- the Australian JSONL importer ------------------------------------------

DECISION = {
    "version_id": "nsw_caselaw:2020/5f2b", "type": "decision",
    "jurisdiction": "new_south_wales", "source": "nsw_caselaw", "mime": "text/html",
    "date": "2020-03-04", "citation": "Smith v Jones [2020] NSWSC 1",
    "url": "https://example.test/1", "when_scraped": "2024-01-01T00:00:00",
    "text": "The plaintiff sued in negligence.",
}
REPORTED = {
    "version_id": "hca:1992/mabo", "type": "decision", "jurisdiction": "commonwealth",
    "source": "high_court_of_australia", "mime": "text/html", "date": "1992-06-03",
    "citation": "Mabo v Queensland (No 2) (1992) 175 CLR 1",
    "url": "https://example.test/2", "when_scraped": "2024-01-01T00:00:00",
    "text": "Native title survives the acquisition of sovereignty.",
}
STATUTE = {
    "version_id": "tas:sr-2008-119", "type": "secondary_legislation",
    "jurisdiction": "tasmania", "source": "tasmanian_legislation", "mime": "text/html",
    "date": "2008-10-08", "citation": "Proclamation under the Commonwealth Powers Act",
    "url": "https://example.test/3", "when_scraped": "2024-01-01T00:00:00",
    "text": "A proclamation.",
}


@pytest.fixture()
def au_corpus(tmp_path):
    file = tmp_path / "corpus.jsonl"
    with file.open("w", encoding="utf-8") as fh:
        for row in (DECISION, REPORTED, STATUTE):
            fh.write(json.dumps(row) + "\n")
        fh.write("{ this is not json\n")   # a malformed line must not sink the import
    return file


def test_au_importer_takes_decisions_and_leaves_statutes_to_the_live_registers(au_corpus):
    """The corpus carries statutes too, but the live registers give point-in-time
    compilations and an amendment graph that a flat text dump cannot."""
    stubs = list(AustralianCaseLawAdapter(path=au_corpus).discover(None))
    assert {s.stable_id for s in stubs} == {"nswsc/2020/1", *(
        s.stable_id for s in stubs if s.stable_id.startswith("au-case/"))}
    assert not any("tasmanian" in (s.title or "") for s in stubs)
    # opting in gets the legislation as well
    with_statutes = list(AustralianCaseLawAdapter(
        path=au_corpus, types="decision,secondary_legislation").discover(None))
    assert len(with_statutes) == 3


def test_au_importer_survives_a_malformed_line(au_corpus):
    """One bad line must not sink a 232k-document import."""
    assert len(list(AustralianCaseLawAdapter(path=au_corpus).discover(None))) == 2


def test_au_importer_builds_a_judgment_record(au_corpus):
    adapter = AustralianCaseLawAdapter(path=au_corpus)
    stub = next(s for s in adapter.discover(None) if s.stable_id == "nswsc/2020/1")
    record = adapter.fetch(stub)
    assert record.doc_type is DocType.JUDGMENT
    assert record.court == "nswsc" and record.extra["jurisdiction"] == "nsw"
    assert record.decision_date.isoformat() == "2020-03-04"
    # a compilation of official texts, not the official text itself
    assert record.extra["is_authoritative"] is False


def test_au_report_only_decision_gets_a_surrogate_id_and_an_alias(au_corpus):
    """Its own id can never be cited, so the reporter citation is the only route to it."""
    adapter = AustralianCaseLawAdapter(path=au_corpus)
    stub = next(s for s in adapter.discover(None) if s.stable_id.startswith("au-case/"))
    record = adapter.fetch(stub)
    assert record.extra["surrogate_id"] is True
    assert record.extra["aliases"] == ["(1992) 175 CLR 1"]


def test_au_surrogate_ids_are_stable_across_reimports(au_corpus):
    """Keyed on the upstream version_id, so a refreshed dataset updates rather than
    duplicating every report-only decision."""
    first = {s.stable_id for s in AustralianCaseLawAdapter(path=au_corpus).discover(None)}
    second = {s.stable_id for s in AustralianCaseLawAdapter(path=au_corpus).discover(None)}
    assert first == second


def test_au_importer_filters_by_jurisdiction_and_year(au_corpus):
    only_nsw = list(AustralianCaseLawAdapter(
        path=au_corpus, jurisdictions="new_south_wales").discover(None))
    assert [s.stable_id for s in only_nsw] == ["nswsc/2020/1"]
    recent = list(AustralianCaseLawAdapter(path=au_corpus, min_year=2000).discover(None))
    assert [s.stable_id for s in recent] == ["nswsc/2020/1"]


def test_au_importer_is_incremental_on_the_decision_date(au_corpus):
    assert [s.stable_id for s in
            AustralianCaseLawAdapter(path=au_corpus).discover("2019-01-01")] == ["nswsc/2020/1"]


# -- the Canadian parquet importer ------------------------------------------
# Reading parquet needs pyarrow (the 'bulk' extra); the row-shaping logic below is
# exercised directly so the mapping is covered even where pyarrow isn't installed.

ROW = {
    "dataset": "SCC",
    "citation_en": "2017 SCC 15", "citation_fr": "2017 CSC 15",
    "citation2_en": "[2017] 1 SCR 202", "citation2_fr": "[2017] 1 RCS 202",
    "name_en": "R. v. Paterson", "name_fr": "R. c. Paterson",
    "document_date_en": "2017-03-17", "document_date_fr": "2017-03-17",
    "url_en": "https://example.test/scc/1", "url_fr": "https://example.test/csc/1",
    "unofficial_text_en": "The appeal is allowed.", "unofficial_text_fr": "Le pourvoi est accueilli.",
    "cases_cited_en": ["2009 SCC 32", "2013 SCC 50", "2017 SCC 15"],
    "cases_cited_fr": None,
    "citing_cases_count": 12, "upstream_license": "Reproduced with permission",
}


def _record(row: dict, **kwargs):
    from raglex.adapters.ca_caselaw import CanadianCaseLawAdapter
    from raglex.core.models import Stub

    adapter = CanadianCaseLawAdapter(path=None, **kwargs)
    stub = adapter._stub("SCC", row, "")
    return adapter, stub, (adapter.fetch(stub) if stub else None)


def test_ca_row_becomes_a_judgment_keyed_on_its_neutral_citation():
    _, stub, record = _record(ROW)
    assert stub.stable_id == "scc/2017/15"
    assert record.doc_type is DocType.JUDGMENT and record.court == "scc"
    assert record.title == "R. v. Paterson"
    assert record.decision_date.isoformat() == "2017-03-17"
    assert record.extra["is_authoritative"] is False   # A2AJ is a secondary source


def test_ca_citation_network_becomes_edges_and_drops_self_citations():
    _, stub, record = _record(ROW)
    targets = {r.dst_id for r in record.relations}
    assert targets == {"scc/2009/32", "scc/2013/50"}
    assert all(r.relationship_type is RelationshipType.MENTIONS for r in record.relations)
    assert stub.stable_id not in targets


def test_ca_record_mints_both_language_reporter_aliases():
    """A French judgment cited by its RCS reference must land on the same node."""
    _, _, record = _record(ROW)
    assert "[2017] 1 SCR 202" in record.extra["aliases"]
    assert "[2017] 1 RCS 202" in record.extra["aliases"]


def test_ca_record_is_one_document_per_case_not_one_per_language():
    """A judgment is handed down once and translated — unlike Canadian legislation,
    where the two languages are co-equal enactments and both are stored."""
    _, _, record = _record(ROW)
    assert record.language == "en"
    assert record.text == "The appeal is allowed."
    assert record.extra["has_text_fr"] is True
    assert record.extra["url_fr"] == "https://example.test/csc/1"
    # asking for French gets the French expression of the same case, same id
    _, stub_fr, record_fr = _record(ROW, language="fr")
    assert stub_fr.stable_id == "scc/2017/15"
    assert record_fr.language == "fr" and record_fr.text == "Le pourvoi est accueilli."


def test_ca_pre_neutral_citation_decision_gets_a_surrogate_and_a_report_alias():
    row = dict(ROW, citation_en="[1940] SCR 578", citation2_en="", citation_fr="",
               citation2_fr="", cases_cited_en=None, document_date_en="1940-06-29")
    _, stub, record = _record(row)
    assert stub.stable_id.startswith("ca-case/scc/")
    assert record.extra["surrogate_id"] is True
    assert record.extra["aliases"] == ["[1940] SCR 578"]


def test_ca_rows_without_text_are_skipped():
    row = dict(ROW, unofficial_text_en="", unofficial_text_fr="")
    _, stub, _ = _record(row)
    assert stub is None


def test_ca_min_year_filters_the_backfill():
    _, stub, _ = _record(dict(ROW, document_date_en="1995-01-01"), min_year=2000)
    assert stub is None
