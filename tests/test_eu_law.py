"""EU legislative-change model (§EU): CELEX identity, consolidation linking, the
recast/codification classifier, and correlation-table parsing."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from raglex import eu_law as E
from raglex.adapters.eu_legislation import EULegislationAdapter
from raglex.core.models import DocType, Record, RelationshipType


def test_celex_sector_and_consolidation_identity():
    assert E.celex_sector_name("32016R0679") == "legislation"
    assert E.celex_sector_name("62016CJ0001") == "case law"
    assert E.is_consolidation("02016R0679-20160504") is True
    assert E.is_consolidation("32016R0679") is False
    assert E.consolidation_base("02016R0679-20160504") == "32016R0679"
    assert E.consolidation_date("02016R0679-20160504") == "2016-05-04"
    assert E.consolidation_base("32016R0679") is None


def test_targeted_legislation_can_enumerate_and_link_consolidations(monkeypatch):
    adapter = EULegislationAdapter(
        celex="32005L0029", include_consolidations=True
    )
    monkeypatch.setattr(adapter, "_sparql", lambda query: [
        {"celex": "02005L0029-20050612"},
        {"celex": "02005L0029-20220528"},
    ])
    stubs = list(adapter.discover(None))
    assert [stub.stable_id for stub in stubs] == [
        "32005L0029", "02005L0029-20050612", "02005L0029-20220528"
    ]
    record = adapter._decorate_currency(Record(
        source="eu-legislation", stable_id="02005L0029-20220528",
        doc_type=DocType.LEGISLATION, raw_bytes=b"x", text="Article 1",
    ))
    assert record.extra["consolidation_of"] == "32005L0029"
    assert record.extra["as_at"] == "2022-05-28"
    assert record.extra["is_authoritative"] is False
    assert [(rel.relationship_type, rel.dst_id) for rel in record.relations] == [
        (RelationshipType.CONSOLIDATES, "32005L0029")
    ]


def test_reverse_sector_zero_sweep_keeps_future_versions_and_resume_offset(monkeypatch):
    adapter = EULegislationAdapter(
        consolidations_only=True, start_offset=400, page_size=2
    )
    queries = []

    def fake_sparql(query):
        queries.append(query)
        return [
            {"celex": "02005L0029-20220528"},
            {"celex": "02005L0029-20260927"},
        ] if "OFFSET 400" in query else []

    monkeypatch.setattr(adapter, "_sparql", fake_sparql)
    stubs = list(adapter.discover(None))
    assert [s.stable_id for s in stubs] == [
        "02005L0029-20220528", "02005L0029-20260927",
    ]
    assert [s.hints["consolidation_of"] for s in stubs] == [
        "32005L0029", "32005L0029",
    ]
    assert [s.hints["resume_offset"] for s in stubs] == [401, 402]
    assert "^0[0-9]{4}[A-Z]+" in queries[0]


def test_corpus_annex_repair_reparses_and_reextracts_split_packages(tmp_path):
    import io
    import zipfile

    from raglex.config import Config
    from raglex.facade import Facade

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "01.xml",
            "<ACT><ENACTING.TERMS><ARTICLE><TI.ART>Article 1</TI.ART>"
            "<P>Purpose.</P></ARTICLE></ENACTING.TERMS></ACT>",
        )
        zf.writestr(
            "02.xml",
            "<ACT><ANNEX><TITLE>ANNEX I</TITLE>"
            "<P>Article 2 of Directive 2005/29/EC applies.</P></ANNEX></ACT>",
        )
        zf.writestr("notice.doc.xml", "<DOC/>")
    raw = buf.getvalue()
    cfg = Config(
        data_dir=tmp_path,
        catalogue_path=tmp_path / "c.sqlite",
        raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text",
        settings_path=tmp_path / "s.json",
        embed_provider="local-hashing",
        embed_model=None,
    )
    facade = Facade(cfg)
    rec = Record(
        source="eu-legislation",
        stable_id="32005L0029",
        doc_type=DocType.LEGISLATION,
        title="Unfair Commercial Practices Directive",
        raw_bytes=raw,
        raw_ext="zip",
        text="Article 1\nPurpose.",
        extra={"format": "formex-legislation"},
    )
    rec.ensure_payload_hash()
    with facade._open() as (cat, rs, ts):
        raw_hash = rs.put(raw, ext="zip")
        raw_path = str(rs.path_for(raw_hash, "zip"))
        text_path = str(ts.put(rec.payload_hash, rec.text))
        cat.upsert_document(rec, raw_path=raw_path, text_path=text_path)

    result = facade.repair_eu_split_annexes()
    assert result["eligible"] == result["reparsed"] == 1
    assert result["citations_reextracted"] == 1
    with facade._open() as (cat, _rs, ts):
        doc = cat.get_document("32005L0029")
        labels = [s.label for s in ts.get_segments(doc["payload_hash"])]
        assert labels == ["Article 1", "ANNEX I"]
        assert doc["last_extracted_at"] is not None


def test_targeted_consolidation_sync_stamps_successful_lookup(tmp_path, monkeypatch):
    facade = _leg_facade(tmp_path)
    called = {}

    def fake_harvest(source, **params):
        called.update(source=source, params=params)
        return {"stored": 2}

    monkeypatch.setattr(facade, "harvest", fake_harvest)
    result = facade.sync_eu_consolidations(stable_id="32016R0679")
    assert result["stored"] == 2
    assert called["source"] == "eu-legislation"
    assert called["params"]["options"] == {
        "celex": "32016R0679",
        "include_consolidations": "true",
    }
    assert called["params"]["resume_unfinished"] is False
    with facade._open() as (cat, _rs, _ts):
        assert cat.document_meta("32016R0679")["consolidations_checked_at"]


def test_classify_change_reconstructs_the_kind():
    assert E.classify_change(celex="02016R0679-20160504") == "consolidation"
    assert E.classify_change(repeals=True, based_on=True,
                             descriptors=["Recast of Directive 95/46/EC"]) == "recast"
    assert E.classify_change(repeals=True, based_on=True, descriptors=["Codification"]) == "codification"
    # the shared signature with no distinguishing descriptor is honestly ambiguous
    assert E.classify_change(repeals=True, based_on=True) == "recast_or_codification"
    assert E.classify_change(repeals=True) == "repeal"
    assert E.classify_change() == "act"


def test_correlation_table_cells_and_text():
    rows = [["Directive 95/46/EC", "This Regulation"],   # header, dropped
            ["Article 1", "Article 1"], ["Article 2(a)", "Article 4(1)"],
            ["", "orphan"]]                               # malformed, dropped
    assert E.parse_correlation_table_cells(rows) == [("Article 1", "Article 1"),
                                                     ("Article 2(a)", "Article 4(1)")]
    txt = ("Directive 95/46/EC | This Regulation\n"
           "Article 1 | Article 1\nArticle 2(a) | Article 4(1)\nsome prose line")
    assert E.parse_correlation_table_text(txt) == [("Article 1", "Article 1"),
                                                   ("Article 2(a)", "Article 4(1)")]


def test_correlation_pairs_from_formex_tbl():
    xml = b"""<ANNEX><TITLE>Correlation table</TITLE><TBL><ROW>
      <CELL>Directive 95/46/EC</CELL><CELL>This Regulation</CELL></ROW>
      <ROW><CELL>Article 7</CELL><CELL>Article 6</CELL></ROW>
      <ROW><CELL>Article 12</CELL><CELL>Article 15</CELL></ROW>
      <ROW><CELL>Note</CELL><CELL>prose</CELL></ROW></TBL></ANNEX>"""
    annex = ET.fromstring(xml)
    assert E.correlation_pairs_from_formex(annex) == [("Article 7", "Article 6"),
                                                      ("Article 12", "Article 15")]


def test_classify_celex_treats_legislation_sectors_as_legislation():
    from raglex.adapters.eu_cellar import classify_celex
    from raglex.core.models import DocType
    assert classify_celex("32016R0679")[0] == DocType.LEGISLATION
    assert classify_celex("02016R0679-20160504")[0] == DocType.LEGISLATION
    assert classify_celex("62016CJ0001")[0] == DocType.JUDGMENT   # case law unchanged


def _leg_facade(tmp_path):
    from raglex.config import Config
    from raglex.facade import Facade
    f = Facade(Config(data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite",
                      raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                      settings_path=tmp_path / "s.json", embed_provider="local-hashing", embed_model=None))
    with f._open() as (cat, _r, _t):
        c = cat.conn
        for sid in ("31995L0046", "32016R0679"):
            c.execute("INSERT INTO documents (stable_id,source,doc_type,title,added_by,topic_tags,"
                      "upstream_status,fetched_at) VALUES (?,?,?,?, 'harvest','[]','live','2026-01-01')",
                      (sid, "eu-cellar", "legislation", sid))
        c.execute("INSERT INTO relations (src_id,dst_id,candidate_id,relationship_type,resolution_status,"
                  "raw_citation_string) VALUES ('32016R0679','31995L0046','31995L0046','repeals','resolved','x')")
        c.execute("INSERT INTO relations (src_id,dst_id,candidate_id,relationship_type,resolution_status,"
                  "raw_citation_string,dst_anchor) VALUES ('32016R0679R(01)','32016R0679','32016R0679','corrects','pending','x','Article 17')")
        cat.conn.commit()
    return f


def test_legislative_status_marks_repealed_from_incoming_edge(tmp_path):
    f = _leg_facade(tmp_path)
    dpd = f.legislative_status("31995L0046")
    assert dpd["status"] == "repealed" and dpd["repealed_by"] == ["32016R0679"]
    gdpr = f.legislative_status("32016R0679")
    assert gdpr["status"] == "corrected" and gdpr["corrected_by"] == ["32016R0679R(01)"]
    assert gdpr["repeals"] == ["31995L0046"]
    assert gdpr["by_article"]["Article 17"] == ["corrects"]
    cons = f.legislative_status("02016R0679-20180525")
    assert cons["is_consolidation"] and cons["consolidation_of"] == "32016R0679" and cons["as_at"] == "2018-05-25"


def test_legislative_status_identifies_latest_and_historical_consolidations(tmp_path):
    from raglex.core.models import ExtractedVia, ResolutionStatus, TypedRelation

    f = _leg_facade(tmp_path)
    with f._open() as (cat, _r, _t):
        for sid in (
            "02016R0679-20180525",
            "02016R0679-20240101",
            "02016R0679-20990101",
        ):
            cat.upsert_document(Record(
                source="eu-legislation", stable_id=sid,
                doc_type=DocType.LEGISLATION, title=sid,
                extracted_via=ExtractedVia.STRUCTURED,
            ))
            cat.add_relations(sid, [TypedRelation(
                relationship_type=RelationshipType.CONSOLIDATES,
                raw_citation_string="32016R0679", dst_id="32016R0679",
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.RESOLVED,
            )])

    base = f.legislative_status("32016R0679")
    assert base["version_state"] == "base_with_consolidation"
    assert base["latest_applicable_consolidation"]["stable_id"] == "02016R0679-20240101"
    assert base["latest_held_consolidation"]["stable_id"] == "02016R0679-20990101"
    historical = f.legislative_status("02016R0679-20180525")
    assert historical["version_state"] == "historical_consolidation"
    latest = f.legislative_status("02016R0679-20240101")
    assert latest["version_state"] == "latest_applicable_consolidation"
    future = f.legislative_status("02016R0679-20990101")
    assert future["version_state"] == "future_consolidation"
    versions = f.legislation_versions(stable_id="32016R0679")
    assert [(v["stable_id"], v["kind"]) for v in versions["versions"]] == [
        ("02016R0679-20990101", "consolidation"),
        ("02016R0679-20240101", "consolidation"),
        ("02016R0679-20180525", "consolidation"),
    ]


def test_consolidation_inherits_base_mentions_and_is_the_default_read(tmp_path):
    from raglex.core.models import ExtractedVia, ResolutionStatus, TypedRelation

    f = _leg_facade(tmp_path)
    current = "02016R0679-20240101"
    with f._open() as (cat, _r, _t):
        for sid, kind in (
            (current, DocType.LEGISLATION),
            ("case-base", DocType.JUDGMENT),
            ("case-direct", DocType.JUDGMENT),
        ):
            cat.upsert_document(Record(
                source="eu-legislation" if kind == DocType.LEGISLATION else "user-import",
                stable_id=sid, doc_type=kind, title=sid,
                extracted_via=ExtractedVia.STRUCTURED,
            ))
        cat.add_relations(current, [TypedRelation(
            relationship_type=RelationshipType.CONSOLIDATES,
            raw_citation_string="32016R0679", dst_id="32016R0679",
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.RESOLVED,
        )])
        cat.add_relations("case-base", [
            TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string="GDPR Article 1", dst_id="32016R0679",
                dst_anchor="Article 1", extracted_via=ExtractedVia.MANUAL,
                resolution_status=ResolutionStatus.RESOLVED,
            ),
            # A provision introduced after the original text: its base-act identity is
            # still useful, but the dated consolidation is where it can be read.
            TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string="GDPR Article 1a", dst_id="32016R0679",
                dst_anchor="Article 1a", extracted_via=ExtractedVia.MANUAL,
                resolution_status=ResolutionStatus.RESOLVED,
            ),
        ])
        cat.add_relations("case-direct", [TypedRelation(
            relationship_type=RelationshipType.APPLIES,
            raw_citation_string=current, dst_id=current, dst_anchor="Article 2",
            extracted_via=ExtractedVia.MANUAL,
            resolution_status=ResolutionStatus.RESOLVED,
        )])

    base = f.get_document("32016R0679")
    assert base["canonical_read"]["stable_id"] == current
    consolidated = f.get_document(current)
    assert consolidated["original_act"]["stable_id"] == "32016R0679"
    assert consolidated["cited_by_count"] == 2
    mentions = f.document_mentions(current)
    assert set(mentions["by_anchor"]) == {"Article 1", "Article 1a", "Article 2"}
    assert mentions["version_inheritance"]["from_base_act"] == "32016R0679"
    assert mentions["by_anchor"]["Article 1a"][0]["version_inherited"] is True

    redirected = f.lookup(
        citation="32016R0679", cited_by=False, similar=False)
    assert redirected["stable_id"] == current
    assert redirected["requested_stable_id"] == "32016R0679"
    original = f.lookup(
        citation="32016R0679", cited_by=False, similar=False, original=True)
    assert original["stable_id"] == "32016R0679"


def test_formex_quoted_amendments_are_not_promoted_to_act_articles():
    from raglex.formats.formex import parse_formex_legislation

    raw = b"""<ACT><ENACTING.TERMS>
      <ARTICLE><TI.ART>Article 14</TI.ART><P>Directive X is amended:</P>
        <QUOT.S><ARTICLE><TI.ART>Article 1</TI.ART>
          <P>The replacement purpose.</P></ARTICLE></QUOT.S>
      </ARTICLE>
      <ARTICLE><TI.ART>Article 15</TI.ART><P>Review.</P></ARTICLE>
    </ENACTING.TERMS></ACT>"""
    parsed = parse_formex_legislation(raw)
    assert [s.label for s in parsed.segments] == ["Article 14", "Article 15"]
    assert "Article 1 The replacement purpose." in parsed.text


def test_implicit_repeal_is_not_a_repeal():
    """CELLAR marks an act that supersedes a REFERENCE to another as "implicitly
    repealing" it. By that predicate Directive 2005/29 is implicitly repealed by five
    acts — 2009/22, 2011/83, 2006/114, 2017/2394, 2023/2673 — while being in force and
    amended as recently as 2024. Read as a repeal, it retires half the statute book."""
    from raglex.eu_law import CDM_ACT_TO_ACT_LINKS

    assert CDM_ACT_TO_ACT_LINKS["resource_legal_repeals_resource_legal"] == \
        ("REPEALS", "REPEALED_BY")
    assert CDM_ACT_TO_ACT_LINKS["resource_legal_implicitly_repeals_resource_legal"] == \
        ("IMPLICITLY_REPEALS", "IMPLICITLY_REPEALED_BY")


def test_implicit_repeal_edges_do_not_flip_the_status_banner(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGLEX_DATA_DIR", str(tmp_path))
    from raglex.config import Config
    from raglex.core.models import DocType, ExtractedVia, Record, RelationshipType, \
        ResolutionStatus, TypedRelation
    from raglex.facade import Facade

    f = Facade(Config.from_env())
    with f._open() as (cat, _rs, _ts):
        cat.upsert_document(Record(
            source="eu-legislation", stable_id="32005L0029", doc_type=DocType.LEGISLATION,
            title="Unfair Commercial Practices Directive",
            extracted_via=ExtractedVia.STRUCTURED))
        cat.add_relations("32005L0029", [TypedRelation(
            relationship_type=RelationshipType.IMPLICITLY_REPEALED_BY,
            raw_citation_string="32011L0083", dst_id="32011L0083",
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.RESOLVED)])

    st = f.legislative_status(stable_id="32005L0029")
    assert st["status"] != "repealed"
    assert st["repealed_by"] == []
    assert st["implicitly_affected_by"] == ["32011L0083"]
