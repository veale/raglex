from raglex.config import Config
from raglex.core.models import (
    DocType, ExtractedVia, Record, RelationshipType, ResolutionStatus, TypedRelation,
)
from raglex.facade import Facade


def _facade(tmp_path):
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "catalogue.sqlite",
        raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
        settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing", embed_model=None,
    ))


def test_provision_mapping_preserves_literal_old_citation_and_projects_it(tmp_path):
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, _ts):
        for sid, kind in (
            ("new-law", DocType.LEGISLATION),
            ("old-law", DocType.LEGISLATION),
            ("case-1", DocType.JUDGMENT),
        ):
            cat.upsert_document(Record(
                source="user-import", stable_id=sid, doc_type=kind, title=sid))
        cat.add_relations("case-1", [TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string="Article 15 of the old law",
            dst_id="old-law", dst_anchor="Article 15",
            extracted_via=ExtractedVia.MANUAL,
            resolution_status=ResolutionStatus.RESOLVED,
        )])

    result = f.upsert_provision_mappings(
        current_id="new-law", previous_id="old-law", created_by="llm",
        mappings=[{
            "current_anchor": "Article 17", "previous_anchor": "Article 15",
            "confidence": 0.9, "note": "same access function",
        }],
    )
    assert result["written"] == 1
    inherited = f.inherited_provision_mentions(
        stable_id="new-law", current_anchor="Article 17")
    assert inherited["documents"] == 1
    row = inherited["incoming"][0]
    assert row["src_id"] == "case-1"
    assert row["inherited_from_id"] == "old-law"
    assert row["inherited_from_anchor"] == "Article 15"
    assert row["inherited_current_anchor"] == "Article 17"

    # The literal edge remains exactly where its author put it.
    with f._open() as (cat, _rs, _ts):
        edge = cat.relations_for("case-1")[0]
        assert edge["dst_id"] == "old-law"
        assert edge["dst_anchor"] == "Article 15"


def test_source_catalogue_has_one_grouping_schema():
    from raglex.adapters.registry import ADAPTERS, SOURCE_INFO, source_catalog

    rows = source_catalog()
    assert rows
    assert set(ADAPTERS) == set(SOURCE_INFO), \
        "every adapter needs an explicit SourceInfo row; see docs/adapter-authoring.md"
    assert all(row["group_key"] and row["group_label"] and row["kind_label"]
               for row in rows)
    assert rows == sorted(rows, key=lambda row: tuple(row["sort_key"]))
    acm = next(row for row in rows if row["key"] == "nl-acm-guidance")
    assert acm["label"].startswith("ACM ")
    assert acm["group_label"] == "Netherlands"
