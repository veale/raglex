"""A document's display KIND and the filter that selects that kind must agree.

They had drifted, silently and in two ways. The bucket shown to a reader is computed
in Python by ``_doc_kind``; the filter behind "show me this slice" was hand-written
SQL. It compared ``doc_type`` directly against the kind — which is not what a kind is
— and its admin clause named three sources when ``_ADMIN_SOURCES`` had grown to
eleven. So the Law Commission's 722 reports, stored ``doc_type='preparatory'`` and
displayed as guidance, gave "nothing in this slice" when the guidance slice was
narrowed to them.

The invariant is the point of this file: for every document, the kind SQL selects it
if and only if ``_doc_kind`` puts it in that bucket. Testing the clause against the
same function that labels the document is what stops them parting again.
"""

from __future__ import annotations

from datetime import date

import pytest

from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.facade import Facade

KINDS = ["cases", "legislation", "guidance", "administrative", "preparatory", "other"]

# one document per interesting corner, as (source, doc_type, court)
SPECIMENS = [
    ("uk-caselaw", "judgment", "ewca"),
    ("uk-caselaw", "decision", "ukut"),
    ("uk-legislation", "legislation", None),
    ("uk-lawcom-reports", "preparatory", None),      # the reported bug
    ("eu-preparatory", "preparatory", None),         # a real travaux collection
    ("edpb", "guidance", None),
    ("edpb", "opinion", None),
    ("ie-dpc", "decision", "dpa-ie"),
    # a DPA's guidance is guidance, not an administrative decision…
    ("ie-dpc-guidance", "guidance", "dpa-ie"),
    ("nl-ap", "decision", "dpa-nl"),
    # …and its annual report is a report, which files as preparatory
    ("nl-ap", "preparatory", "dpa-nl"),
    ("uk-cma", "decision", None),
    ("gdprhub", "judgment", "dpa-de"),
    ("uk-judiciary", "guidance", "judiciary"),
    ("eu-cellar", "opinion", "cjeu"),
    ("uk-caselaw", "commentary", None),
    # a judgment with NO recorded court: `NULL LIKE 'dpa-%'` is NULL, so a naive
    # `NOT (...)` drops the row and the case vanishes from its own slice
    ("au-caselaw", "judgment", None),
]


@pytest.fixture()
def facade(tmp_path):
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=str(tmp_path / "c.sqlite"),
        raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
        settings_path=tmp_path / "s.json",
        embed_provider="local-hashing", embed_model=None))


def _seed(facade):
    with facade._open() as (cat, _rs, ts):
        for i, (source, doc_type, court) in enumerate(SPECIMENS):
            rec = Record(source=source, stable_id=f"d{i}", doc_type=DocType(doc_type),
                         title=f"{source} {doc_type}", court=court,
                         decision_date=date(2020, 1, 1), text="body text",
                         raw_bytes=b"body text", extracted_via=ExtractedVia.STRUCTURED)
            rec.ensure_payload_hash()
            cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, "body text")))
    return SPECIMENS


def test_the_law_commission_is_guidance_not_a_hole(facade):
    """The reported symptom, pinned: 722 documents that display under guidance and
    were selected by nothing."""
    assert facade._doc_kind("uk-lawcom-reports", "preparatory", None) == "guidance"
    sql, params = facade._kind_clause("guidance")
    _seed(facade)
    with facade._open() as (cat, _rs, _ts):
        rows = cat.conn.execute(
            f"SELECT d.source FROM documents d WHERE {sql}", params).fetchall()
    assert "uk-lawcom-reports" in {r["source"] for r in rows}


def test_a_real_travaux_collection_stays_preparatory(facade):
    assert facade._doc_kind("eu-preparatory", "preparatory", None) == "preparatory"
    sql, params = facade._kind_clause("preparatory")
    _seed(facade)
    with facade._open() as (cat, _rs, _ts):
        rows = cat.conn.execute(
            f"SELECT d.source FROM documents d WHERE {sql}", params).fetchall()
    assert {r["source"] for r in rows} == {"eu-preparatory"}


def test_the_admin_slice_uses_the_same_source_list_as_the_label(facade):
    """The clause named three sources while _ADMIN_SOURCES had eleven, so most
    administrative documents displayed as administrative and were filtered as
    something else."""
    _seed(facade)
    sql, params = facade._kind_clause("administrative")
    with facade._open() as (cat, _rs, _ts):
        rows = cat.conn.execute(
            f"SELECT d.source, d.doc_type, d.court FROM documents d WHERE {sql}",
            params).fetchall()
    selected = {(r["source"], r["doc_type"], r["court"]) for r in rows}
    expected = {s for s in SPECIMENS
                if facade._doc_kind(s[0], s[1], s[2]) == "administrative"}
    assert selected == expected
    assert ("uk-cma", "decision", None) in selected      # added to _ADMIN_SOURCES


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_selects_exactly_what_it_labels(facade, kind):
    """The invariant. For each kind, the SQL must select precisely the documents
    ``_doc_kind`` assigns to it — no more, no fewer."""
    _seed(facade)
    sql, params = facade._kind_clause(kind)
    with facade._open() as (cat, _rs, _ts):
        rows = cat.conn.execute(
            f"SELECT d.source, d.doc_type, d.court FROM documents d WHERE {sql}",
            params).fetchall()
    selected = {(r["source"], r["doc_type"], r["court"]) for r in rows}
    expected = {s for s in SPECIMENS if facade._doc_kind(s[0], s[1], s[2]) == kind}
    assert selected == expected, (
        f"{kind}: selected-but-not-labelled {selected - expected}, "
        f"labelled-but-not-selected {expected - selected}")


def test_the_kinds_partition_the_corpus(facade):
    """Every document lands in exactly one slice — nothing is invisible everywhere,
    which is how 722 reports went missing."""
    _seed(facade)
    seen: dict[tuple, list[str]] = {}
    for kind in KINDS:
        sql, params = facade._kind_clause(kind)
        with facade._open() as (cat, _rs, _ts):
            rows = cat.conn.execute(
                f"SELECT d.source, d.doc_type, d.court FROM documents d WHERE {sql}",
                params).fetchall()
        for r in rows:
            seen.setdefault((r["source"], r["doc_type"], r["court"]), []).append(kind)
    for spec in SPECIMENS:
        assert seen.get(spec), f"{spec} is in no slice at all"
        assert len(seen[spec]) == 1, f"{spec} is in several slices: {seen[spec]}"


def test_an_explicit_doc_type_still_works(facade):
    """A caller passing "judgment" rather than a display kind gets the raw column."""
    _seed(facade)
    sql, params = facade._kind_clause("judgment")
    with facade._open() as (cat, _rs, _ts):
        rows = cat.conn.execute(
            f"SELECT d.doc_type FROM documents d WHERE {sql}", params).fetchall()
    assert {r["doc_type"] for r in rows} == {"judgment"}
