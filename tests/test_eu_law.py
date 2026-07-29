"""EU legislative-change model (§EU): CELEX identity, consolidation linking, the
recast/codification classifier, and correlation-table parsing."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from raglex import eu_law as E


def test_celex_sector_and_consolidation_identity():
    assert E.celex_sector_name("32016R0679") == "legislation"
    assert E.celex_sector_name("62016CJ0001") == "case law"
    assert E.is_consolidation("02016R0679-20160504") is True
    assert E.is_consolidation("32016R0679") is False
    assert E.consolidation_base("02016R0679-20160504") == "32016R0679"
    assert E.consolidation_date("02016R0679-20160504") == "2016-05-04"
    assert E.consolidation_base("32016R0679") is None


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
