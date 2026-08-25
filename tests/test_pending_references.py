"""The statute-page view of what is still before the Court, and the EU↔UK counterpart
link that a static edition must not inherit."""
from __future__ import annotations

import pytest

from raglex.config import Config
from raglex.core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    TypedRelation,
)
from raglex.facade import Facade


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing", embed_model=None,
    )


def _store(facade, record: Record) -> None:
    record.ensure_payload_hash()
    with facade._open() as (cat, _rs, ts):
        ts.put(record.payload_hash, record.text or "")
        cat.upsert_document(record, text_path="x")


def _notice(celex: str, *, procedure: str, date: str, cites: list[tuple[str, str]],
            title: str) -> Record:
    return Record(
        source="eu-cellar", stable_id=celex, doc_type=DocType.NOTE, title=title,
        raw_bytes=celex.encode(), text="notice", source_language="en",
        decision_date=__import__("datetime").date.fromisoformat(date),
        court="Court of Justice",
        extra={"pending": True, "celex": celex, "pending_procedure": procedure,
               "pending_proceeding": "Request for a preliminary ruling",
               "referring_courts": ["Bundesgerichtshof"], "origin_country": "DEU"},
        relations=[TypedRelation(
            relationship_type=RelationshipType.MENTIONS, raw_citation_string=raw,
            dst_id=dst, dst_anchor=anchor, extracted_via=ExtractedVia.REGEX,
            resolution_status=ResolutionStatus.RESOLVED)
            for dst, anchor in cites for raw in [f"{anchor} of {dst}"]],
    )


def test_pending_references_report_what_each_case_turns_on(config):
    """A statute's open questions: references apart from the other pending actions,
    ordered as the instrument reads, with the AG's Opinion where one exists."""
    f = Facade(config)
    _store(f, Record(source="eu-legislation", stable_id="32016R0679",
                     doc_type=DocType.LEGISLATION, title="GDPR", raw_bytes=b"gdpr",
                     text="Article 22 …", source_language="en"))
    today = __import__("datetime").date.today()
    recent = today.replace(year=today.year - 1).isoformat()
    _store(f, _notice("62025CN0100", procedure="PREJ", date=recent,
                      title="Pending: A v B (C-100/25)",
                      cites=[("32016R0679", "Article 22"), ("32016R0679", "Article 5(1)"),
                             ("32016R0679", "Recital 71")]))
    _store(f, _notice("62025TN0200", procedure="ANNU", date=recent,
                      title="Pending: C v Commission (T-200/25)",
                      cites=[("32016R0679", "Article 58")]))
    # The AG has opined in the reference — which does NOT end it.
    _store(f, Record(source="eu-cellar", stable_id="62025CC0100", doc_type=DocType.OPINION,
                     title="Opinion of AG Kokott", raw_bytes=b"op", text="opinion",
                     source_language="en", extra={"celex": "62025CC0100",
                                                  "advocate_general": "Kokott"}))

    out = f.pending_references("32016R0679")
    assert out["preliminary_count"] == 1 and out["other_count"] == 1
    # Both are listed together — a pending T-case on this instrument is as much "before
    # the Court" as a reference — references first, each labelled for what it is.
    assert [e["case_number"] for e in out["pending"]] == ["C-100/25", "T-200/25"]
    assert [e["procedure_label"] for e in out["pending"]] == [
        "Preliminary reference", "Action for annulment"]
    reference = out["pending"][0]
    # Reading order, not string order: the recital precedes the articles, and
    # Article 5 precedes Article 22.
    assert reference["anchors"] == ["Recital 71", "Article 5(1)", "Article 22"]
    assert reference["referring_court"] == "Bundesgerichtshof"
    assert reference["ag_opinion"]["advocate_general"] == "Kokott"
    assert out["with_ag_opinion"] == 1


def test_a_reference_older_than_the_cutoff_is_counted_not_listed(config):
    """No Article 267 reference takes five years. One still marked pending after that
    was withdrawn or decided — it is reported as stale rather than shown as live."""
    f = Facade(config)
    _store(f, Record(source="eu-legislation", stable_id="32016R0679",
                     doc_type=DocType.LEGISLATION, title="GDPR", raw_bytes=b"gdpr",
                     text="Article 22 …", source_language="en"))
    old = __import__("datetime").date.today()
    old = old.replace(year=old.year - (Facade._PENDING_STALE_YEARS + 2)).isoformat()
    _store(f, _notice("62017CN0300", procedure="PREJ", date=old,
                      title="Pending: D v E (C-300/17)",
                      cites=[("32016R0679", "Article 22")]))

    out = f.pending_references("32016R0679")
    assert out["preliminary_count"] == 0
    assert out["stale_count"] == 1
    assert out["stale"][0]["case_number"] == "C-300/17"


def test_the_urgent_preliminary_procedure_is_still_a_preliminary_reference(config):
    f = Facade(config)
    assert Facade._is_preliminary("REFER_PREL_URG")
    assert Facade._is_preliminary("PREJ")
    assert not Facade._is_preliminary("ANNU")
    assert f._doc_kind("eu-cellar", "note", "Court of Justice",
                       pending=True, preliminary=True) == "preliminary_references"
    assert f._doc_kind("eu-cellar", "note", "Court of Justice",
                       pending=True) == "pending_cases"
    # …and a RETIRED notice is ordinary EU material again, not a live question.
    assert f._doc_kind("eu-cellar", "note", "Court of Justice") == "other"


def test_assimilated_uk_law_links_to_the_eu_original_and_back(config):
    """Two separate laws that started identical. A reader of either needs a route to
    the other to know what a CJEU ruling is worth."""
    f = Facade(config)
    _store(f, Record(source="eu-legislation", stable_id="32016R0679",
                     doc_type=DocType.LEGISLATION, title="Regulation (EU) 2016/679",
                     raw_bytes=b"gdpr", text="Article 1 …", source_language="en",
                     extra={"celex": "32016R0679"}))
    _store(f, Record(
        source="uk-legislation", stable_id="european/regulation/2016/0679",
        doc_type=DocType.LEGISLATION, title="Assimilated Regulation (EU) 2016/679",
        raw_bytes=b"uk gdpr", text="Article 1 …", source_language="en",
        relations=[TypedRelation(
            relationship_type=RelationshipType.ASSIMILATED_VERSION_OF,
            raw_citation_string="32016R0679", dst_id="32016R0679",
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.RESOLVED)]))

    uk = f.get_document("european/regulation/2016/0679")["counterpart"]
    assert uk["role"] == "eu_original" and uk["stable_id"] == "32016R0679"
    eu = f.get_document("32016R0679")["counterpart"]
    assert eu["role"] == "uk_assimilated"
    assert eu["stable_id"] == "european/regulation/2016/0679"
    # legislation.gov.uk serves representations under the type-code path.
    assert eu["url"].endswith("/eur/2016/679")


def test_a_static_edition_does_not_inherit_the_counterpart_link(config):
    """A static edition of UK law is a snapshot of one jurisdiction and ships without a
    server; a link into an EU corpus it does not contain would simply be broken."""
    from raglex.static_export import StaticLawExporter

    f = Facade(config)
    _store(f, Record(
        source="uk-legislation", stable_id="european/regulation/2016/0679",
        doc_type=DocType.LEGISLATION, title="Assimilated Regulation (EU) 2016/679",
        raw_bytes=b"uk gdpr", text="Article 1 Subject matter", source_language="en",
        relations=[TypedRelation(
            relationship_type=RelationshipType.ASSIMILATED_VERSION_OF,
            raw_citation_string="32016R0679", dst_id="32016R0679",
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.RESOLVED)]))

    data = StaticLawExporter(facade=f).build_data("european/regulation/2016/0679")
    assert "counterpart" not in data
    assert "eur-lex.europa.eu" not in str(data)


def test_a_dated_consolidation_still_links_to_its_uk_counterpart(config):
    """The link has to appear where people actually read: on the dated consolidation
    (02016R0679-20160504), not only on the undated base act the edge points at."""
    f = Facade(config)
    for stable_id, celex in (("32016R0679", "32016R0679"),
                             ("02016R0679-20160504", "02016R0679-20160504")):
        _store(f, Record(source="eu-legislation", stable_id=stable_id,
                         doc_type=DocType.LEGISLATION, title="Regulation (EU) 2016/679",
                         raw_bytes=stable_id.encode(), text="Article 1 …",
                         source_language="en", extra={"celex": celex}))
    _store(f, Record(
        source="uk-legislation", stable_id="european/regulation/2016/0679",
        doc_type=DocType.LEGISLATION, title="Assimilated Regulation (EU) 2016/679",
        raw_bytes=b"uk gdpr", text="Article 1 …", source_language="en",
        relations=[TypedRelation(
            relationship_type=RelationshipType.ASSIMILATED_VERSION_OF,
            raw_citation_string="32016R0679", dst_id="32016R0679",
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.RESOLVED)]))

    counterpart = f.get_document("02016R0679-20160504")["counterpart"]
    assert counterpart["role"] == "uk_assimilated"
    assert counterpart["stable_id"] == "european/regulation/2016/0679"


def test_a_slow_instrument_still_gets_its_pending_line_in_a_static_edition(config,
                                                                          monkeypatch):
    """The reader's box may show a spinner; a file cannot.

    ``pending_references`` is a stale-while-revalidate cache with a 2.5s ``sync_wait``,
    so a cold call on an instrument whose scan takes longer returns a placeholder that
    has no ``pending`` key at all. Read as a list, that is an empty one. The v8 GDPR
    edition of 2026-08-22 published exactly that way — no pending line, 29 references
    live before the Court, and every smaller edition in the same build correct, so
    nothing looked wrong.

    This test costs a few seconds of real waiting because the bug IS the timing: a
    stall shorter than ``sync_wait`` cannot reproduce it.
    """
    import time

    from raglex.static_export import StaticLawExporter
    from raglex.storage.catalogue import Catalogue

    def _build() -> Facade:
        f = Facade(config)
        _store(f, Record(source="eu-legislation", stable_id="32016R0679",
                         doc_type=DocType.LEGISLATION, title="GDPR", raw_bytes=b"gdpr",
                         text="Article 22 …", source_language="en"))
        today = __import__("datetime").date.today()
        _store(f, _notice("62025CN0100", procedure="PREJ",
                          date=today.replace(year=today.year - 1).isoformat(),
                          title="Pending: A v B (C-100/25)",
                          cites=[("32016R0679", "Article 22")]))
        return f

    slow = Catalogue.pending_eu_citers

    def _slow(self, ids, **kwargs):
        time.sleep(3.0)                      # > the 2.5s sync_wait, as the GDPR is
        return slow(self, ids, **kwargs)

    monkeypatch.setattr(Catalogue, "pending_eu_citers", _slow)

    # The reader's path is unchanged: it gets the placeholder and polls.
    warming = _build().pending_references("32016R0679")
    assert warming.get("_warming") and "pending" not in warming

    # The build's path waits, and the edition carries the reference.
    summary = StaticLawExporter(facade=_build())._pending_summary("32016R0679")
    assert summary["total"] == 1
    assert summary["groups"][0]["label"] == "Preliminary reference"
    assert summary["cases"][0]["case"] == "C-100/25"


def test_a_build_that_cannot_see_the_pending_list_refuses_to_publish(config):
    """Not returning {}: "nothing is pending" and "I could not find out" render the
    same, and the file is not built again for weeks."""
    from raglex.static_export import StaticLawExporter

    class _Warming:
        def pending_references(self, stable_id, *, limit=200, blocking=False):
            return {"stable_id": stable_id, "preliminary": [], "other": [],
                    "_warming": True}

    with pytest.raises(RuntimeError, match="not computed"):
        StaticLawExporter(facade=_Warming())._pending_summary("32016R0679")
