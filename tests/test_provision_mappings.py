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


def _held(cat, textstore, sid, *, title=None, text=None, segments=()):
    from raglex.core.models import Segment  # noqa: PLC0415 — test-local

    record = Record(
        source="user-import", stable_id=sid, doc_type=DocType.LEGISLATION,
        title=title or sid, text=text,
        segments=[Segment(*s) for s in segments],
        extracted_via=ExtractedVia.STRUCTURED,
    )
    if text:
        record.ensure_payload_hash()
        textstore.put(record.payload_hash, text)
        textstore.put_segments(record.payload_hash, record.segments)
        cat.upsert_document(record, text_path="")
    else:
        cat.upsert_document(record)


def test_link_refuses_an_unknown_relationship_instead_of_coercing_it(tmp_path):
    """A typo must not become a plausible-looking statement about the law.

    'cross_references' used to be stored as 'analyses' with a cheerful resolved: true,
    and the response could not be told apart from a success.
    """
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "law-a")
        _held(cat, ts, "law-b")

    bad = f.link(src_id="law-a", dst_id="law-b", relationship="cross_references")
    assert "error" in bad and "cross_references" in bad["error"]
    assert "analyses" in bad["known"] and "supersedes" in bad["known"]

    with f._open() as (cat, _rs, _ts):
        assert cat.relations_for("law-a") == []


def test_link_is_idempotent_and_carries_a_note(tmp_path):
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "law-a")
        _held(cat, ts, "law-b")

    first = f.link(src_id="law-a", dst_id="law-b", relationship="supersedes",
                   src_anchor="Article 4", dst_anchor="Article 12",
                   note="ECD correlation table")
    again = f.link(src_id="law-a", dst_id="law-b", relationship="supersedes",
                   src_anchor="Article 4", dst_anchor="Article 12")
    assert first["created"] is True and again["created"] is False
    assert first["relation_id"] == again["relation_id"]

    with f._open() as (cat, _rs, _ts):
        rows = cat.relations_for("law-a")
        assert len(rows) == 1                       # not two
        assert rows[0]["note"] == "ECD correlation table"
        # A different provision pair IS a different edge.
        f.link(src_id="law-a", dst_id="law-b", relationship="supersedes",
               src_anchor="Article 5", dst_anchor="Article 13")
    with f._open() as (cat, _rs, _ts):
        assert len(cat.relations_for("law-a")) == 2


def test_manual_edge_can_be_retracted_without_touching_an_extracted_one(tmp_path):
    """The only delete-ish op used to suppress the whole relation, taking a genuine
    regex citation between the same two documents down with it."""
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "law-a")
        _held(cat, ts, "law-b")
        cat.add_relations("law-a", [TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string="Directive 2015/1535", dst_id="law-b",
            dst_anchor="Article 1", extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.RESOLVED,
        )])

    wrong = f.link(src_id="law-a", dst_id="law-b", relationship="supersedes",
                   src_anchor="Article 3", dst_anchor="Article 1")
    listed = f.manual_links(stable_id="law-a")
    assert [row["relation_id"] for row in listed["outgoing"]] == [wrong["relation_id"]]

    assert f.delete_manual_link(relation_id=wrong["relation_id"])["deleted"] is True
    with f._open() as (cat, _rs, _ts):
        remaining = cat.relations_for("law-a")
        assert len(remaining) == 1
        assert remaining[0]["extracted_via"] == "structured"   # the real one survived
        # …and the extracted edge is not deletable through this door.
        refused = f.delete_manual_link(relation_id=int(remaining[0]["relation_id"]))
    assert refused["deleted"] is False and "manual" in refused["error"]


def test_link_dry_run_reports_anchor_resolution_and_writes_nothing(tmp_path):
    f = _facade(tmp_path)
    text = "Article 12 Right of access\nMember States shall guarantee."
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "law-a")
        _held(cat, ts, "law-b", text=text,
              segments=[("Article 12 Right of access", 0, len(text))])

    ok = f.link(src_id="law-a", dst_id="law-b", relationship="supersedes",
                dst_anchor="Article 12", dry_run=True)
    assert ok["dry_run"] is True and ok["written"] is False
    assert ok["dst_anchor_resolved"] is True
    missing = f.link(src_id="law-a", dst_id="law-b", relationship="supersedes",
                     dst_anchor="regulation 6", dry_run=True)
    assert missing["dst_anchor_resolved"] is False
    with f._open() as (cat, _rs, _ts):
        assert cat.relations_for("law-a") == []


def test_provision_mapping_reports_an_anchor_that_matches_no_segment(tmp_path):
    """The table's whole value is that its anchors resolve. One that names nothing was
    previously stored in silence and simply never matched a citation."""
    f = _facade(tmp_path)
    current = "Article 17 Right to erasure\n1. The data subject shall have the right."
    previous = "Article 12 Right of access\nMember States shall guarantee."
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "new-law", text=current,
              segments=[("Article 17 Right to erasure", 0, len(current))])
        _held(cat, ts, "old-law", text=previous,
              segments=[("Article 12 Right of access", 0, len(previous))])

    planned = f.upsert_provision_mappings(
        current_id="new-law", previous_id="old-law", dry_run=True,
        mappings=[
            {"current_anchor": "Article 17", "previous_anchor": "Article 12"},
            {"current_anchor": "Article 17", "previous_anchor": "regulation 6"},
        ])
    assert planned["written"] == 0 and planned["would_write"] == 2
    assert len(planned["unresolved_anchors"]) == 1
    assert planned["unresolved_anchors"][0]["previous_anchor"] == "regulation 6"

    written = f.upsert_provision_mappings(
        current_id="new-law", previous_id="old-law",
        mappings=[{"current_anchor": "Article 17", "previous_anchor": "regulation 6"}])
    assert written["written"] == 1                 # stored, but not in silence
    assert "warning" in written and written["unresolved_anchors"]


def test_relationship_vocabulary_is_discoverable(tmp_path):
    known = _facade(tmp_path).relationship_types()
    assert "supersedes" in known["relationship_types"]
    assert "cross_references" not in known["relationship_types"]
    assert "implements" in known["families"]["legislative"]


def test_graph_neighbours_shows_every_per_provision_edge_from_both_ends(tmp_path):
    """Four provision-level edges are four, and are visible from the target too.

    They used to collapse to a single row (so a caller verifying its work read a false
    negative on three of four), and a type-filtered query from the target side returned
    nothing at all, because the filter ran in Python after a bounded, unordered fetch.
    """
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "new-act")
        _held(cat, ts, "old-act")
        # Enough ordinary citations to push the rare edge past any bounded prefix.
        for n in range(150):
            _held(cat, ts, f"citer-{n}")
            cat.add_relations(f"citer-{n}", [TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string="old-act", dst_id="old-act",
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.RESOLVED,
            )])

    for src, dst in (("Article 4", "Article 12"), ("Article 5", "Article 13"),
                     ("Article 6", "Article 14"), ("Article 8", "Article 15")):
        f.link(src_id="new-act", dst_id="old-act", relationship="supersedes",
               src_anchor=src, dst_anchor=dst)

    outward = f.graph("new-act", rel=["supersedes"])["neighbours"]
    assert len(outward) == 1 and outward[0]["passages"] == 4
    assert {p["dst_anchor"] for p in outward[0]["anchor_pairs"]} == {
        "Article 12", "Article 13", "Article 14", "Article 15"}

    inward = f.graph("old-act", rel=["supersedes"])["neighbours"]
    assert [n["id"] for n in inward] == ["new-act"]
    assert inward[0]["direction"] == "in" and inward[0]["passages"] == 4


def test_unapplied_count_is_unknown_when_there_is_nothing_to_compare(tmp_path):
    """Zero must not read as reassurance.

    ePrivacy reported unapplied_count: 0 / degraded: false while its held Article 5(3)
    was the pre-2009 opt-out text — because with no consolidation there was nothing to
    diff the enacted text against. That state is 'unknown', not 'up to date'.
    """
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "32002L0058", title="Directive 2002/58/EC")
        _held(cat, ts, "32009L0136", title="Directive 2009/136/EC")
        cat.add_relations("32002L0058", [TypedRelation(
            relationship_type=RelationshipType.AMENDED_BY,
            raw_citation_string="32009L0136", dst_id="32009L0136",
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.RESOLVED,
        )])

    status = f.legislative_status("32002L0058")
    assert status["version_state"] == "base_without_consolidation"
    assert status["amended_by"] == ["32009L0136"]
    assert status["unapplied_count"] is None          # unknown, not zero
    assert status["degraded"] is True
    assert status["amendments_uncomparable"] is True
    assert "nothing to compare" in status["currency_note"]

    # An act with no recorded amendments is not swept up by this.
    clean = f.legislative_status("32009L0136")
    assert clean["amendments_uncomparable"] is False
    assert clean["unapplied_count"] == 0


def test_a_mapping_written_against_the_act_carries_to_its_later_versions(tmp_path):
    """DPA 1998 → DPA 2018 must still hold when a newer DPA 2018 text is harvested.

    A mapping is an editorial claim about the ACT, not about one dated expression of
    it. Keyed strictly to the id it was written against, the whole correlation table
    vanished the moment the text was updated.
    """
    f = _facade(tmp_path)
    base, version = "ukpga/2018/12", "ukpga/2018/12@2023-01-01"
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, base)
        _held(cat, ts, version)
        _held(cat, ts, "ukpga/1998/29")
        cat.add_relations(version, [TypedRelation(
            relationship_type=RelationshipType.POINT_IN_TIME_OF,
            raw_citation_string=base, dst_id=base,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.RESOLVED,
        )])
        _held(cat, ts, "case-old", title="Old v Registrar")
        cat.add_relations("case-old", [TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string="s. 7 DPA 1998", dst_id="ukpga/1998/29",
            dst_anchor="Section 7", extracted_via=ExtractedVia.MANUAL,
            resolution_status=ResolutionStatus.RESOLVED,
        )])

    f.upsert_provision_mappings(
        current_id=base, previous_id="ukpga/1998/29",
        mappings=[{"current_anchor": "Section 45", "previous_anchor": "Section 7"}])

    # Written against the base act…
    assert f.provision_mappings(stable_id=base)["mappings"][0]["current_anchor"] == "Section 45"
    # …and read from the dated version, flagged as arriving from the base act.
    carried = f.provision_mappings(stable_id=version)["mappings"]
    assert [row["current_anchor"] for row in carried] == ["Section 45"]
    assert carried[0]["mapping_from_base_act"] == 1
    inherited = f.inherited_provision_mentions(
        stable_id=version, current_anchor="Section 45")
    assert inherited["documents"] == 1
    assert inherited["incoming"][0]["src_id"] == "case-old"
