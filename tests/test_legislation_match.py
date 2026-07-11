"""Name-only statute resolution against held-legislation titles (self-maintaining gazetteer)."""

from __future__ import annotations

import os
import tempfile

from raglex.citations.statute_gazetteer import normalise_title, reference_key
from raglex.config import Config
from raglex.core.models import (
    DocType, ExtractedVia, Record, RelationshipType, ResolutionStatus, TypedRelation,
)
from raglex.facade import Facade


def test_reference_key_strips_provision_and_the():
    held = normalise_title("Police and Criminal Evidence Act 1984")
    assert reference_key("the Police and Criminal Evidence Act 1984") == held
    assert reference_key("section 78 of the Police and Criminal Evidence Act 1984") == held
    assert reference_key("Part II of the Road Traffic Act 1991") == normalise_title("Road Traffic Act 1991")


def _facade() -> Facade:
    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def test_matches_name_only_statute_to_held_legislation():
    f = _facade()
    with f._open() as (cat, _rs, _ts):
        # hold the Act as harvested legislation
        cat.upsert_document(Record(source="uk-legislation", stable_id="ukpga/1984/60",
                                   doc_type=DocType.LEGISLATION, title="Police and Criminal Evidence Act 1984"))
        # a judgment with a pending, candidate-less reference to it by name
        cat.upsert_document(Record(source="uk-caselaw", stable_id="case/1", doc_type=DocType.JUDGMENT,
            relations=[TypedRelation(relationship_type=RelationshipType.MENTIONS,
                raw_citation_string="section 78 of the Police and Criminal Evidence Act 1984",
                extracted_via=ExtractedVia.REGEX, resolution_status=ResolutionStatus.PENDING)]))
    res = f.match_named_legislation()
    assert res["aliased"] >= 1 and res["resolved_edges"] >= 1
    with f._open() as (cat, _rs, _ts):
        row = cat.conn.execute(
            "SELECT dst_id, resolution_status FROM relations WHERE src_id='case/1'").fetchone()
        assert row["resolution_status"] == "resolved" and row["dst_id"] == "ukpga/1984/60"


def test_ambiguous_title_is_not_guessed():
    f = _facade()
    with f._open() as (cat, _rs, _ts):
        # two different Acts share the bare title "Finance Act" (no year in the reference)
        cat.upsert_document(Record(source="uk-legislation", stable_id="ukpga/2020/14",
                                   doc_type=DocType.LEGISLATION, title="Finance Act"))
        cat.upsert_document(Record(source="uk-legislation", stable_id="ukpga/2021/26",
                                   doc_type=DocType.LEGISLATION, title="Finance Act"))
        cat.upsert_document(Record(source="uk-caselaw", stable_id="case/2", doc_type=DocType.JUDGMENT,
            relations=[TypedRelation(relationship_type=RelationshipType.MENTIONS,
                raw_citation_string="the Finance Act", extracted_via=ExtractedVia.REGEX,
                resolution_status=ResolutionStatus.PENDING)]))
    res = f.match_named_legislation()
    with f._open() as (cat, _rs, _ts):
        row = cat.conn.execute("SELECT resolution_status FROM relations WHERE src_id='case/2'").fetchone()
        assert row["resolution_status"] == "pending"  # ambiguous → left alone
