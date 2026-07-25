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
