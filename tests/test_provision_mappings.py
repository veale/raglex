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


def test_provision_scoped_citers_are_found_however_the_pinpoint_is_punctuated(tmp_path):
    """`citing_documents(anchor='s. 13')` returned 0 for the whole UK corpus.

    The Python family matcher was always right; the coarse SQL guard in front of it
    rebuilt "s. 13" as "s 13" and LIKE'd for that, so UK section citations were dropped
    before the matcher ever saw them. EU material was unaffected only because
    "Article 17" happens to survive the round trip.
    """
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "ukpga/1998/29", title="Data Protection Act 1998")
        _held(cat, ts, "case-lloyd", title="Lloyd v Google LLC")
        cat.add_relations("case-lloyd", [
            TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string="s. 13 DPA 1998", dst_id="ukpga/1998/29",
                dst_anchor=anchor, extracted_via=ExtractedVia.MANUAL,
                resolution_status=ResolutionStatus.RESOLVED,
            )
            for anchor in ("s. 13", "s. 13(2)", "s. 4(4)")
        ])

    # Every spelling of the same provision finds it, including the full segment label.
    for spelling in ("s. 13", "s 13", "section 13", "Section 13",
                     "s. 13 Compensation for failure to comply"):
        found = f.citing_documents("ukpga/1998/29", anchor=spelling)
        assert found["total"] == 1, spelling
        assert found["results"][0]["stable_id"] == "case-lloyd", spelling

    # And a different section is still a different section.
    assert f.citing_documents("ukpga/1998/29", anchor="s. 4")["total"] == 1
    assert f.citing_documents("ukpga/1998/29", anchor="s. 99")["total"] == 0

    # The contract the report asked for: anything in cites_provisions works as an anchor.
    everything = f.citing_documents("ukpga/1998/29")
    for pinpoint in everything["results"][0]["cites_provisions"]:
        assert f.citing_documents("ukpga/1998/29", anchor=pinpoint)["total"] == 1, pinpoint


def test_document_body_can_be_read_structurally_and_in_windows(tmp_path):
    """A long act could not be returned at all: no pagination, no partial read.

    The workaround was to abuse get_provision's context window at guessed char offsets
    and merge the pieces. `segments_only` answers the structural question in one call,
    and offset/limit make the text readable in pieces.
    """
    from raglex.core.models import Segment

    f = _facade(tmp_path)
    text = "".join(f"s. {n} Heading {n}\nSome provision text for section {n}.\n"
                   for n in range(1, 41))
    with f._open() as (cat, _rs, ts):
        record = Record(
            source="uk-legislation", stable_id="ukpga/2018/12",
            doc_type=DocType.LEGISLATION, title="Data Protection Act 2018",
            text=text, extracted_via=ExtractedVia.STRUCTURED,
            segments=[
                Segment(f"s. {n} Heading {n}",
                        text.index(f"s. {n} Heading {n}"),
                        text.index(f"s. {n} Heading {n}") + 50, kind="section")
                for n in range(1, 41)
            ],
        )
        record.ensure_payload_hash()
        ts.put(record.payload_hash, text)
        ts.put_segments(record.payload_hash, record.segments)
        cat.upsert_document(record, text_path="")

    spine = f.document_body("ukpga/2018/12", segments_only=True)
    assert spine["segments_only"] is True
    assert spine["segment_count"] == 40
    assert spine["segments"][0]["label"] == "s. 1 Heading 1"
    assert "text" not in spine                       # structure only: no body at all
    assert spine["text_chars"] == len(text)

    first = f.document_body("ukpga/2018/12", limit=200)
    assert first["text"] == text[:200]
    assert first["window"]["has_more"] is True
    # Offsets stay absolute, so pages stitch on char_start.
    assert all(s["char_start"] < 200 for s in first["segments"])
    second = f.document_body("ukpga/2018/12", offset=first["window"]["next_offset"])
    assert second["text"] == text[200:]
    assert second["window"]["has_more"] is False

    whole = f.document_body("ukpga/2018/12")
    assert whole["text"] == text and "window" not in whole   # unchanged by default


def test_provision_lookup_accepts_any_spelling_and_refuses_a_near_miss(tmp_path):
    """get_provision demanded the exact full label; lookup took the natural pinpoint.

    Two tools over one segment table with different resolution rules is a trap, and the
    stricter one was the tool whose whole purpose is pinpoint reading. Worse, the
    substring fallback answered "s. 16" with s. 166 — quoting the wrong provision.
    """
    from raglex.core.models import Segment

    f = _facade(tmp_path)
    text = ("s. 166 Orders to progress complaints\nThe Tribunal may order.\n"
            "s. 167 Compliance orders\nA court may make a compliance order.\n")
    with f._open() as (cat, _rs, ts):
        record = Record(
            source="uk-legislation", stable_id="ukpga/2018/12",
            doc_type=DocType.LEGISLATION, title="Data Protection Act 2018",
            text=text, extracted_via=ExtractedVia.STRUCTURED,
            segments=[
                Segment("s. 166 Orders to progress complaints", 0,
                        text.index("s. 167"), kind="section"),
                Segment("s. 167 Compliance orders", text.index("s. 167"), len(text),
                        kind="section"),
            ],
        )
        record.ensure_payload_hash()
        ts.put(record.payload_hash, text)
        ts.put_segments(record.payload_hash, record.segments)
        cat.upsert_document(record, text_path="")

    for spelling in ("s. 167", "s 167", "section 167", "SECTION 167",
                     "s. 167 Compliance orders", "s. 167(2)"):
        found = f.get_provision("ukpga/2018/12", label=spelling)
        focus = next((s for s in found.get("segments", []) if s.get("focus")), None)
        assert focus, spelling
        assert focus["label"] == "s. 167 Compliance orders", spelling
        assert "compliance order" in focus["text"], spelling

    # A provision that does not exist says so, rather than handing back a near neighbour.
    assert "error" in f.get_provision("ukpga/2018/12", label="s. 16")
    assert "error" in f.get_provision("ukpga/2018/12", label="s. 999")


def test_lookup_says_when_the_held_text_has_been_struck_out(tmp_path):
    """`held: true`, 319 segments, an outline, 1,914 citers — and a body of dots.

    legislation.gov.uk publishes a repealed provision struck out, so the live version of
    a wholly-repealed Act looks perfectly healthy in metadata while containing no law.
    Same shape of trap as `unapplied_count: 0`: a reassuring response over unusable text.
    """
    from raglex.core.models import Segment

    f = _facade(tmp_path)
    gutted = "s. 1 Basic interpretative provisions\n" + (". . . . . . . . . .\n" * 40)
    live = "s. 1 Basic interpretative provisions\n" + ("Real operative words here.\n" * 40)
    with f._open() as (cat, _rs, ts):
        for sid, text in (("ukpga/1998/29", gutted), ("ukpga/2018/12", live)):
            record = Record(
                source="uk-legislation", stable_id=sid, doc_type=DocType.LEGISLATION,
                title=sid, text=text, extracted_via=ExtractedVia.STRUCTURED,
                segments=[Segment("s. 1 Basic interpretative provisions", 0,
                                  len(text), kind="section")],
            )
            record.ensure_payload_hash()
            ts.put(record.payload_hash, text)
            ts.put_segments(record.payload_hash, record.segments)
            cat.upsert_document(record, text_path="")

    struck = f.lookup(citation="ukpga/1998/29", cited_by=False, similar=False)
    assert struck["held"] is True and struck["segment_count"] == 1   # all still true…
    assert struck["text_available"] is False                          # …and unusable
    assert "struck out" in struck["text_note"]
    assert struck["struck_out_ratio"] > 0.4

    ordinary = f.lookup(citation="ukpga/2018/12", cited_by=False, similar=False)
    assert "text_available" not in ordinary                           # no false alarm


def test_inherited_mentions_cross_the_version_and_punctuation_gap(tmp_path):
    """The DPA 2018 showed nothing at all from 260 correct mappings.

    Two independent breaks, both silent. The mappings name a dated snapshot of the old
    act (`ukpga/1998/29@2015-01-01`) while every judgment cites the base act; and the
    corpus stores `Sch. 2` where an editor wrote `Sch 2`, which an exact string join
    treats as a different provision. Neither is a "lockout on old cases citing new law" —
    there is no such rule — but the effect looked exactly like one.
    """
    f = _facade(tmp_path)
    base, snapshot = "ukpga/1998/29", "ukpga/1998/29@2015-01-01"
    with f._open() as (cat, _rs, ts):
        for sid in ("ukpga/2018/12", base, snapshot, "case-a", "case-b"):
            _held(cat, ts, sid)
        cat.add_relations(snapshot, [TypedRelation(
            relationship_type=RelationshipType.POINT_IN_TIME_OF,
            raw_citation_string=base, dst_id=base,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.RESOLVED,
        )])
        # Judgments cite the BASE act, with the corpus's own punctuation.
        for src, anchor in (("case-a", "s. 7"), ("case-b", "Sch. 2")):
            cat.add_relations(src, [TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=anchor, dst_id=base, dst_anchor=anchor,
                extracted_via=ExtractedVia.MANUAL,
                resolution_status=ResolutionStatus.RESOLVED,
            )])

    # The mapping is written against the SNAPSHOT, and spells Schedule 2 without a stop.
    f.upsert_provision_mappings(
        current_id="ukpga/2018/12", previous_id=snapshot,
        mappings=[{"current_anchor": "s. 45", "previous_anchor": "s. 7"},
                  {"current_anchor": "s. 15", "previous_anchor": "Sch 2"}])

    inherited = f.inherited_provision_mentions(stable_id="ukpga/2018/12")
    assert inherited["documents"] == 2
    by_src = {r["src_id"]: r for r in inherited["incoming"]}
    assert by_src["case-a"]["inherited_current_anchor"] == "s. 45"
    assert by_src["case-b"]["inherited_current_anchor"] == "s. 15"   # Sch 2 == Sch. 2

    # Scoping to one current provision still works, and stays scoped.
    one = f.inherited_provision_mentions(
        stable_id="ukpga/2018/12", current_anchor="s. 45")
    assert [r["src_id"] for r in one["incoming"]] == ["case-a"]


def test_one_predecessors_citations_do_not_satisfy_anothers_mapping(tmp_path):
    """Expanding each predecessor's version family must not merge the families: "s. 7"
    agrees across half the statute book, so a flat target list would cross-contaminate."""
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        for sid in ("new-act", "old-a", "old-b", "case-a"):
            _held(cat, ts, sid)
        cat.add_relations("case-a", [TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string="s. 7", dst_id="old-b", dst_anchor="s. 7",
            extracted_via=ExtractedVia.MANUAL,
            resolution_status=ResolutionStatus.RESOLVED,
        )])
    # A mapping about old-a only. The citation is of old-b, at the same anchor.
    f.upsert_provision_mappings(
        current_id="new-act", previous_id="old-a",
        mappings=[{"current_anchor": "s. 1", "previous_anchor": "s. 7"}])
    assert f.inherited_provision_mentions(stable_id="new-act")["documents"] == 0


def test_a_subsection_citation_inherits_through_its_provisions_mapping(tmp_path):
    """"s. 33A(1)" inherits from a mapping written about "s. 33A".

    A provision heading represents its family everywhere else in the reader; the
    inherited-mentions join was exact-only, which dropped 241 of the DPA 1998's
    pinpointed citations — every one of them to a subsection of a provision that IS
    mapped, and so exactly the history the mapping exists to surface.
    """
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        for sid in ("new-act", "old-act", "case-sub", "case-exact", "case-other"):
            _held(cat, ts, sid)
        for src, anchor in (("case-sub", "s. 33A(1)"), ("case-exact", "s. 33A"),
                            ("case-other", "s. 3")):
            cat.add_relations(src, [TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=anchor, dst_id="old-act", dst_anchor=anchor,
                extracted_via=ExtractedVia.MANUAL,
                resolution_status=ResolutionStatus.RESOLVED,
            )])

    f.upsert_provision_mappings(
        current_id="new-act", previous_id="old-act",
        mappings=[{"current_anchor": "s. 24", "previous_anchor": "s. 33A"}])

    inherited = f.inherited_provision_mentions(stable_id="new-act")
    srcs = {r["src_id"] for r in inherited["incoming"]}
    assert srcs == {"case-sub", "case-exact"}
    # "s. 3" must NOT be swept in by a prefix match on "s. 33A".
    assert "case-other" not in srcs


def test_a_uk_transposition_inherits_only_retained_eu_case_law(tmp_path):
    """A UK provision transposing a directive inherits pre-Brexit CJEU authority only.

    CJEU judgments handed down before IP completion day are retained EU case law and
    bind UK courts; what Luxembourg decided afterwards does not govern the domestic
    provision. Presenting the later judgments as inherited authority would be a legal
    error, not an untidy result — so the cutoff follows the claim automatically.
    """
    from datetime import date

    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "ukpga/2018/12")
        _held(cat, ts, "32016L0680")
        # Two CJEU judgments either side of the cutoff, both properly dated…
        for sid, when in (("dated-2019", date(2019, 5, 1)), ("dated-2023", date(2023, 5, 1))):
            cat.upsert_document(Record(
                source="eu-cellar", stable_id=sid, doc_type=DocType.JUDGMENT,
                title=sid, decision_date=when, extracted_via=ExtractedVia.STRUCTURED))
        # …and two dated ONLY by their ECLI year, which is 61% of the EU corpus.
        for sid, ecli in (("undated-2018", "ECLI:EU:C:2018:551"),
                          ("undated-2022", "ECLI:EU:C:2022:702")):
            cat.upsert_document(Record(
                source="eu-cellar", stable_id=sid, doc_type=DocType.JUDGMENT,
                title=sid, ecli=ecli, extracted_via=ExtractedVia.STRUCTURED))
        for src in ("dated-2019", "dated-2023", "undated-2018", "undated-2022"):
            cat.add_relations(src, [TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string="Article 4", dst_id="32016L0680",
                dst_anchor="Article 4", extracted_via=ExtractedVia.MANUAL,
                resolution_status=ResolutionStatus.RESOLVED,
            )])

    written = f.upsert_provision_mappings(
        current_id="ukpga/2018/12", previous_id="32016L0680",
        mapping_type="transposition",
        mappings=[{"current_anchor": "s. 35", "previous_anchor": "Article 4"}])
    assert written["mappings"][0]["mapping_type"] == "transposition"
    assert written["mappings"][0]["inherit_before"] == "2020-12-31"   # set from the claim

    inherited = f.inherited_provision_mentions(stable_id="ukpga/2018/12")
    srcs = {r["src_id"] for r in inherited["incoming"]}
    # Both pre-cutoff judgments come through — including the one dated only by its ECLI.
    assert srcs == {"dated-2019", "undated-2018"}

    # An ordinary predecessor mapping is not date-limited.
    f.upsert_provision_mappings(
        current_id="ukpga/2018/12", previous_id="32016L0680",
        mapping_type="functional_predecessor",
        mappings=[{"current_anchor": "s. 36", "previous_anchor": "Article 4"}])
    everything = f.inherited_provision_mentions(
        stable_id="ukpga/2018/12", current_anchor="s. 36")
    assert {r["src_id"] for r in everything["incoming"]} == {
        "dated-2019", "dated-2023", "undated-2018", "undated-2022"}


def test_the_transposition_cutoff_can_be_set_and_cleared(tmp_path):
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "ukpga/2018/12")
        _held(cat, ts, "32016L0680")

    explicit = f.upsert_provision_mappings(
        current_id="ukpga/2018/12", previous_id="32016L0680",
        mapping_type="transposition",
        mappings=[{"current_anchor": "s. 35", "previous_anchor": "Article 4",
                   "inherit_before": "2016-05-04"}])
    assert explicit["mappings"][0]["inherit_before"] == "2016-05-04"

    cleared = f.upsert_provision_mappings(
        current_id="ukpga/2018/12", previous_id="32016L0680",
        mapping_type="transposition",
        mappings=[{"current_anchor": "s. 35", "previous_anchor": "Article 4",
                   "inherit_before": "never"}])
    assert cleared["mappings"][0]["inherit_before"] is None

    bad = f.upsert_provision_mappings(
        current_id="ukpga/2018/12", previous_id="32016L0680",
        mapping_type="transposition",
        mappings=[{"current_anchor": "s. 35", "previous_anchor": "Article 4",
                   "inherit_before": "31/12/2020"}])
    assert "error" in bad and "YYYY-MM-DD" in bad["error"]


def test_a_non_uk_transposition_carries_no_cutoff(tmp_path):
    """Only the UK left. An Irish or German transposition inherits its directive's case
    law in full, so the gate must follow the jurisdiction rather than the word
    'transposition'."""
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "ie/act/2018/7")
        _held(cat, ts, "32016L0680")
    written = f.upsert_provision_mappings(
        current_id="ie/act/2018/7", previous_id="32016L0680",
        mapping_type="transposition",
        mappings=[{"current_anchor": "s. 71", "previous_anchor": "Article 4"}])
    assert written["mappings"][0]["inherit_before"] is None


def test_rescan_matching_reads_the_search_result_it_is_actually_given(tmp_path,
                                                                     monkeypatch):
    """A text-scoped rescan must not silently pass over nothing.

    freetext_search returns its rows under `items`; reading `results` gave an empty
    scope and the job reported success having re-extracted zero documents. An empty
    scope is now an error, because "matched nothing" and "did the work" are the two
    outcomes that must never look alike.
    """
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _held(cat, ts, "doc-a", text="mentions the Act")
        _held(cat, ts, "doc-b", text="also mentions the Act")

    seen: list[str] = []
    monkeypatch.setattr(f, "freetext_search", lambda q, **kw: {
        "items": [{"stable_id": "doc-a"}, {"stable_id": "doc-b"}], "total": 2})
    result = f.rescan_matching(query='"the Act"')
    assert result["documents"] == 2
    assert result["re_extracted"] == 2
    assert result["queries"] == {'"the Act"': 2}

    # A query that matches nothing is an error, not a silent success.
    monkeypatch.setattr(f, "freetext_search", lambda q, **kw: {"items": [], "total": 0})
    empty = f.rescan_matching(query='"nothing here"')
    assert "error" in empty and empty["queries"] == {'"nothing here"': 0}
    assert seen == []


def test_rescan_matching_reports_a_cancel_instead_of_looking_finished(tmp_path,
                                                                     monkeypatch):
    """A cancelled rescan must never read as a completed one.

    The resolve stage reports its own done/total into the same progress row, so a run
    stopped at document 52 of 10,767 displayed as 10,767/10,767 — indistinguishable
    from success, and duly misread as "the rescan finished". A cancel now short-circuits
    before the resolver and says so in both the result and the last progress event.
    """
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        for sid in ("doc-a", "doc-b", "doc-c"):
            _held(cat, ts, sid, text="mentions the Act")

    monkeypatch.setattr(f, "freetext_search", lambda q, **kw: {
        "items": [{"stable_id": "doc-a"}, {"stable_id": "doc-b"}, {"stable_id": "doc-c"}],
        "total": 3})
    events: list[dict] = []
    calls = {"n": 0}

    def cancel_after_one() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    result = f.rescan_matching(query='"the Act"', cancel_check=cancel_after_one,
                               on_progress=lambda **kw: events.append(kw))
    assert result["cancelled"] is True
    assert result["re_extracted"] < 3          # it did NOT get through the scope
    assert result["resolved"] == 0             # and never reached the resolver
    assert events and events[-1]["stage"] == "cancelled"
    assert events[-1]["done"] == result["re_extracted"]


def test_rescan_matching_unions_several_queries(tmp_path, monkeypatch):
    """A document naming three of the Acts is re-extracted once, not three times."""
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        for sid in ("doc-a", "doc-b", "doc-c"):
            _held(cat, ts, sid, text="text")

    by_query = {'"A"': [{"stable_id": "doc-a"}, {"stable_id": "doc-b"}],
                '"B"': [{"stable_id": "doc-b"}, {"stable_id": "doc-c"}]}
    monkeypatch.setattr(f, "freetext_search", lambda q, **kw: {
        "items": by_query[q], "total": len(by_query[q])})
    result = f.rescan_matching(query='"A"|||"B"')
    assert result["documents"] == 3          # the union, not 4
    assert result["queries"] == {'"A"': 2, '"B"': 2}


def test_a_mappings_type_can_be_corrected_in_place(tmp_path):
    """mapping_type was part of the unique key, so re-sending a pair with a corrected
    type inserted a SECOND, contradictory row beside the first. A build of ~240 AI Act
    correspondences written as 'functional_predecessor' therefore had no route back to
    'equivalent' short of deleting every row by id.

    A correspondence is identified by the two provisions it connects; what it CLAIMS
    about them is an attribute of it."""
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, _ts):
        for sid in ("ai-act", "nlf"):
            cat.upsert_document(Record(source="user-import", stable_id=sid,
                                       doc_type=DocType.LEGISLATION, title=sid))
    sent = [{"current_anchor": "Article 40", "previous_anchor": "Article 17",
             "note": "harmonised standards"}]
    f.upsert_provision_mappings(current_id="ai-act", previous_id="nlf", mappings=sent)
    again = f.upsert_provision_mappings(
        current_id="ai-act", previous_id="nlf", mapping_type="equivalent",
        mappings=[{"current_anchor": "Article 40", "previous_anchor": "Article 17"}])
    assert again["total_for_pair"] == 1, "the pair must not have been duplicated"
    assert again["mappings"][0]["mapping_type"] == "equivalent"

    # …and the whole build can be relabelled without re-sending the correspondences
    f.upsert_provision_mappings(
        current_id="ai-act", previous_id="nlf",
        mappings=[{"current_anchor": "Article 41", "previous_anchor": "Article 18"}])
    out = f.retype_provision_mappings(current_id="ai-act", to_type="equivalent",
                                      from_type="functional_predecessor", dry_run=True)
    assert out["would_update"] == 1 and out["updated"] == 0
    assert f.retype_provision_mappings(
        current_id="ai-act", to_type="equivalent")["updated"] == 1
    rows = f.provision_mappings(stable_id="ai-act")["mappings"]
    assert {r["mapping_type"] for r in rows} == {"equivalent"}


def test_retype_can_target_one_anchor_in_a_mixed_pair(tmp_path):
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, _ts):
        for sid in ("dsa", "ecd"):
            cat.upsert_document(Record(source="user-import", stable_id=sid,
                                       doc_type=DocType.LEGISLATION, title=sid))
    f.upsert_provision_mappings(
        current_id="dsa", previous_id="ecd",
        mappings=[
            {"current_anchor": "Article 3", "previous_anchor": "Article 2"},
            {"current_anchor": "Article 4", "previous_anchor": "Article 12"},
        ])
    out = f.retype_provision_mappings(
        current_id="dsa", previous_id="ecd", current_anchor="Article 3",
        previous_anchor="Article 2", to_type="equivalent")
    assert out["updated"] == 1
    got = {(m["current_anchor"], m["mapping_type"])
           for m in f.provision_mappings(stable_id="dsa")["mappings"]}
    assert got == {("Article 3", "equivalent"),
                   ("Article 4", "functional_predecessor")}


def test_a_write_names_the_rows_it_did_not_send(tmp_path):
    """A bulk call whose RESPONSE is lost still ran. The caller saw nothing, re-sent a
    slightly different list, and the pair silently kept both — diagnosable only by
    inferring orphans from gaps in the mapping_id sequence. Name them instead."""
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, _ts):
        for sid in ("ai-act", "nlf"):
            cat.upsert_document(Record(source="user-import", stable_id=sid,
                                       doc_type=DocType.LEGISLATION, title=sid))
    f.upsert_provision_mappings(   # the call whose reply never arrived
        current_id="ai-act", previous_id="nlf",
        mappings=[{"current_anchor": "Article 40", "previous_anchor": "Article 17"},
                  {"current_anchor": "Article 99", "previous_anchor": "Article 44"}])
    out = f.upsert_provision_mappings(   # the corrected re-send
        current_id="ai-act", previous_id="nlf",
        mappings=[{"current_anchor": "Article 40", "previous_anchor": "Article 17"}])
    assert out["total_for_pair"] == 2 and out["sent"] == 1
    surplus = out["not_sent_in_this_call"]
    assert [r["current_anchor"] for r in surplus] == ["Article 99"]
    assert surplus[0]["mapping_id"] and "did not send" in out["warning"]
    # and the pair alone can be listed, without paging every mapping on the law
    only = f.provision_mappings(stable_id="ai-act", previous_id="nlf")
    assert only["matched"] == 2 and only["total"] == 2


# ── the jurisdiction lock ────────────────────────────────────────────────────
def test_a_lock_admits_only_that_jurisdictions_work():
    """Every member state cites the GDPR. Without the lock the UK instrument would be
    buried under the rest of Europe instead of informed by it."""
    from raglex.core.jurisdictions import canonical_jurisdiction, prefixes_for

    assert canonical_jurisdiction("EU") == "European Union"
    assert canonical_jurisdiction("European Union") == "European Union"
    assert canonical_jurisdiction("gb") == "United Kingdom"
    assert canonical_jurisdiction("Narnia") is None
    assert "eu-" in prefixes_for("EU") and "edpb" in prefixes_for("EU")


def test_an_unknown_lock_hides_rather_than_admits():
    """The safe direction: a typo must not open the mapping to the whole corpus."""
    from raglex.core.jurisdictions import sql_source_match

    sql, params = sql_source_match("s.source", "Narnia")
    assert sql == "1 = 0" and params == []


def test_the_lock_predicate_covers_every_bucket():
    from raglex.core.jurisdictions import JURISDICTIONS, sql_lock_match

    sql, params = sql_lock_match("s.source", "pm.source_jurisdiction")
    assert sql.count("pm.source_jurisdiction = ?") == len(JURISDICTIONS)
    assert "European Union" in params and "United Kingdom" in params


def test_inherited_citers_are_filtered_by_the_lock(catalogue):
    """End to end: a CJEU judgment travels along the mapping, a Dutch one does not."""
    from datetime import date

    from raglex.core.models import (DocType, ExtractedVia, Record, RelationshipType,
                                    ResolutionStatus, TypedRelation)

    def _hold(sid, source, when, *, cites=None, anchor=None):
        rec = Record(
            source=source, stable_id=sid, doc_type=DocType.JUDGMENT, title=sid,
            decision_date=when, language="en", raw_bytes=sid.encode(), raw_ext="xml",
            text="text", extracted_via=ExtractedVia.STRUCTURED,
            relations=[TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=cites, dst_id=cites, dst_anchor=anchor,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.RESOLVED)] if cites else [])
        rec.ensure_payload_hash()
        catalogue.upsert_document(rec)

    _hold("32016R0679", "eu-legislation", date(2016, 4, 27))
    _hold("european/regulation/2016/0679", "uk-legislation", date(2021, 1, 1))
    _hold("ECLI:EU:C:2019:1", "eu-cellar", date(2019, 5, 1),
          cites="32016R0679", anchor="Article 15")
    _hold("nl/2019/9", "nl-rechtspraak", date(2019, 5, 1),
          cites="32016R0679", anchor="Article 15")
    _hold("ECLI:EU:C:2023:9", "eu-cellar", date(2023, 5, 1),      # after exit day
          cites="32016R0679", anchor="Article 15")

    catalogue.upsert_provision_mappings(
        "european/regulation/2016/0679", "32016R0679",
        [{"current_anchor": "Article 15", "previous_anchor": "Article 15",
          "mapping_type": "equivalent", "inherit_before": "2020-12-31",
          "source_jurisdiction": "European Union"}],
        created_by="structured")

    rows = catalogue.inherited_mentions_for("european/regulation/2016/0679", limit=None)
    got = {str(r["src_id"]) for r in rows}
    assert "ECLI:EU:C:2019:1" in got          # EU, before exit day → passes
    assert "nl/2019/9" not in got             # Dutch → blocked by the lock
    assert "ECLI:EU:C:2023:9" not in got      # EU, after exit day → blocked by cutoff


# ── generating the assimilated mappings ──────────────────────────────────────
def _facade_with_gdpr_pair():
    """The UK GDPR and its EU original, each segmented by article — the UK one missing
    an article the EU has, and carrying one the EU does not."""
    import os
    import tempfile
    from datetime import date

    from raglex.config import Config
    from raglex.core.models import DocType, ExtractedVia, Record, Segment
    from raglex.facade import Facade

    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    f = Facade(Config.from_env())

    def _hold(sid, labels, when):
        text, segs, pos = "", [], 0
        for label in labels:
            body = f"{label} body.\n"
            segs.append(Segment(label=label, kind="section", level=1,
                                char_start=pos, char_end=pos + len(body)))
            text += body
            pos += len(body)
        rec = Record(source="uk-legislation" if sid.startswith("european/")
                     else "eu-legislation",
                     stable_id=sid, doc_type=DocType.LEGISLATION, title=sid,
                     decision_date=when, language="en", text=text,
                     raw_bytes=text.encode(), raw_ext="xml", segments=segs,
                     extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        with f._open() as (cat, _rs, ts):
            path = str(ts.put(rec.payload_hash, rec.text))
            ts.put_segments(rec.payload_hash, rec.segments)
            cat.upsert_document(rec, text_path=path)

    _hold("32016R0679", ["Article 15", "Article 21", "Article 22"], date(2016, 4, 27))
    _hold("european/regulation/2016/0679",
          ["Article 15", "Article 21", "Article 22A"], date(2021, 1, 1))
    return f


def test_mapping_covers_only_the_articles_that_survived():
    """The article sets are INTERSECTED, so an article the UK dropped has no counterpart
    and a UK insertion (22A) has no EU one — neither is mapped, and no list of them is
    needed."""
    f = _facade_with_gdpr_pair()
    plan = f.map_assimilated_provisions()
    assert plan["instruments"] == 1 and plan["mappings"] == 2
    entry = plan["plan"][0]
    assert entry["uk"] == "european/regulation/2016/0679" and entry["eu"] == "32016R0679"
    assert entry["dropped_in_uk"] == 1 and entry["uk_only"] == 1


def test_applying_writes_locked_and_cut_off_mappings():
    f = _facade_with_gdpr_pair()
    f.map_assimilated_provisions(apply=True)
    got = f.provision_mappings(stable_id="european/regulation/2016/0679")
    rows = {m["current_anchor"]: m for m in got["mappings"]}
    assert set(rows) == {"Article 15", "Article 21"}
    for m in rows.values():
        assert m["previous_doc_id"] == "32016R0679"
        assert m["mapping_type"] == "equivalent"
        assert m["inherit_before"] == "2020-12-31"          # exit day
        assert m["source_jurisdiction"] == "European Union"  # jurisdiction-locked
    # and it is idempotent — the pair is the identity, so a second run rewrites
    f.map_assimilated_provisions(apply=True)
    assert len(f.provision_mappings(
        stable_id="european/regulation/2016/0679")["mappings"]) == 2


def test_a_predecessor_is_carried_through_to_the_uk_article():
    """The DPD sits behind the GDPR; where the UK still has that article, the same
    correspondence is written from the UK text, under the same cutoff and lock."""
    f = _facade_with_gdpr_pair()
    with f._open() as (cat, _rs, _ts):
        cat.upsert_provision_mappings(
            "32016R0679", "31995L0046",
            [{"current_anchor": "Article 15", "previous_anchor": "Article 12"},
             {"current_anchor": "Article 22", "previous_anchor": "Article 15"}],
            created_by="structured")

    st = f.map_assimilated_provisions(apply=True)
    # Article 22 is gone from the UK text, so only the Article 15 chain carries
    assert st["predecessor_mappings"] == 1
    got = f.provision_mappings(stable_id="european/regulation/2016/0679",
                               previous_id="31995L0046")
    assert [(m["current_anchor"], m["previous_anchor"]) for m in got["mappings"]] == [
        ("Article 15", "Article 12")]
    assert got["mappings"][0]["source_jurisdiction"] == "European Union"
    assert got["mappings"][0]["inherit_before"] == "2020-12-31"


def test_anchors_fall_back_to_the_readable_version():
    """Assimilated law's base node is fetched from a path that serves a landing page
    rather than the text, so it carries no article structure — the UK GDPR's base parsed
    to one segment. The version the reader actually opens has the articles."""
    import os
    import tempfile
    from datetime import date

    from raglex.config import Config
    from raglex.core.models import DocType, ExtractedVia, Record, Segment
    from raglex.facade import Facade

    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    f = Facade(Config.from_env())

    def _hold(sid, labels, when):
        text, segs, pos = "", [], 0
        for label in labels:
            body = f"{label} body.\n"
            segs.append(Segment(label=label, kind="section", level=1,
                                char_start=pos, char_end=pos + len(body)))
            text += body
            pos += len(body)
        rec = Record(source="uk-legislation", stable_id=sid,
                     doc_type=DocType.LEGISLATION, title=sid, decision_date=when,
                     language="en", text=text or "landing page",
                     raw_bytes=(text or sid).encode(), raw_ext="xml", segments=segs,
                     extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        with f._open() as (cat, _rs, ts):
            path = str(ts.put(rec.payload_hash, rec.text))
            ts.put_segments(rec.payload_hash, rec.segments)
            cat.upsert_document(rec, text_path=path)

    _hold("32016R0679", ["Article 15", "Article 21"], date(2016, 4, 27))
    _hold("european/regulation/2016/0679", [], date(2021, 1, 1))          # no articles
    _hold("european/regulation/2016/0679@2024-01-01",
          ["Article 15", "Article 21"], date(2024, 1, 1))

    st = f.map_assimilated_provisions(apply=True)
    assert st["mappings"] == 2, st
    got = f.provision_mappings(stable_id="european/regulation/2016/0679")
    assert {m["current_anchor"] for m in got["mappings"]} == {"Article 15", "Article 21"}
