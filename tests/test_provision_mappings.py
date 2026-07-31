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


def test_mapping_type_is_stored_not_silently_coerced(tmp_path):
    """A companion instrument is not an ancestor. Asking for 'equivalent' and getting
    'functional_predecessor' would put a false claim about the law in the corpus."""
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, _ts):
        for sid, kind in (("gdpr", DocType.LEGISLATION), ("eudpr", DocType.LEGISLATION),
                          ("old-law", DocType.LEGISLATION), ("case-1", DocType.JUDGMENT)):
            cat.upsert_document(Record(
                source="user-import", stable_id=sid, doc_type=kind, title=sid))
        cat.add_relations("case-1", [TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string="Article 17 of the EUDPR",
            dst_id="eudpr", dst_anchor="Article 17",
            extracted_via=ExtractedVia.MANUAL,
            resolution_status=ResolutionStatus.RESOLVED,
        )])

    # per call
    result = f.upsert_provision_mappings(
        current_id="gdpr", previous_id="eudpr", mapping_type="equivalent",
        mappings=[{"current_anchor": "Article 15", "previous_anchor": "Article 17"}])
    assert result["written"] == 1
    assert result["mappings"][0]["mapping_type"] == "equivalent"

    # per item, overriding the call default
    result = f.upsert_provision_mappings(
        current_id="gdpr", previous_id="old-law",
        mappings=[
            {"current_anchor": "Article 15", "previous_anchor": "Article 12"},
            {"current_anchor": "Article 16", "previous_anchor": "Article 13",
             "mapping_type": "equivalent"},
        ])
    by_anchor = {m["current_anchor"] + m["previous_anchor"]: m["mapping_type"]
                 for m in result["mappings"] if m["previous_doc_id"] == "old-law"}
    assert by_anchor == {"Article 15Article 12": "functional_predecessor",
                         "Article 16Article 13": "equivalent"}

    # an unrecognised claim is refused, with the vocabulary, rather than downgraded
    bad = f.upsert_provision_mappings(
        current_id="gdpr", previous_id="eudpr", mapping_type="successor",
        mappings=[{"current_anchor": "Article 15", "previous_anchor": "Article 17"}])
    assert "error" in bad and "functional_predecessor" in bad["known"]

    # and it reaches the reader, which labels history and companions differently
    inherited = f.inherited_provision_mentions(stable_id="gdpr",
                                               current_anchor="Article 15")
    assert inherited["incoming"][0]["mapping_type"] == "equivalent"


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
