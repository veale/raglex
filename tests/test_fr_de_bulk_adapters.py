from __future__ import annotations

from datetime import date
from pathlib import Path

from raglex.adapters.de_gii import DeGiiAdapter, _slug
from raglex.adapters.de_rii import DeRiiAdapter
from raglex.adapters.fr_dila import FrDilaAdapter
from raglex.core.models import DocType, RelationshipType
from raglex.formats.gii_xml import parse_gii
from raglex.formats.rii_xml import parse_rii
from raglex.formats.dila_xml import parse_dila_article, parse_dila_juri
from xml.etree import ElementTree as ET

REFS = Path(__file__).resolve().parent.parent / "raglex design docs" / "raglex-refs"
GII_ARCHIVE = REFS / "de-legacy" / "gii-archive" / "gesetze"


# -- Germany: gii legislation (real data) -----------------------------------
def test_gii_parser_real_law():
    d = parse_gii((GII_ARCHIVE / "zappro" / "zappro.xml").read_bytes())
    assert d.title == "Approbationsordnung für Zahnärzte und Zahnärztinnen"
    assert d.metadata["jurabk"] == "ZApprO"
    assert d.decision_date == date(2019, 7, 8)
    # §§ become citable segments
    assert any(s.label.startswith("§ 1 ") for s in d.segments)
    assert len(d.segments) > 50


def test_de_gii_local_discover_and_fetch():
    adapter = DeGiiAdapter(path=str(GII_ARCHIVE), ids=["ZApprO"])
    stubs = list(adapter.discover(None))
    assert stubs and stubs[0].stable_id == "de/gesetz/zappro"
    rec = adapter.fetch(stubs[0])
    assert rec.doc_type == DocType.LEGISLATION
    assert rec.extra["jurabk"] == "ZApprO"
    assert rec.text and rec.segments


def test_de_gii_slug():
    assert _slug("SGB V") == "de/gesetz/sgbv"


# -- Germany: rii case law (constructed fixture, juris rii DTD shape) --------
RII = """<?xml version="1.0" encoding="UTF-8"?>
<dokumente>
 <dokument doknr="KVRE123">
  <gericht>Bundesgerichtshof</gericht>
  <ecli>ECLI:DE:BGH:2021:120521UVIZR100.20.0</ecli>
  <entsch-datum>2021-05-12</entsch-datum>
  <aktenzeichen>VI ZR 100/20</aktenzeichen>
  <doktyp>Urteil</doktyp>
  <titelzeile>Schadensersatz nach Datenschutzverstoß</titelzeile>
  <leitsatz><Content><P>Der Leitsatz.</P></Content></leitsatz>
  <tenor><Content><P>Die Revision wird zurueckgewiesen.</P></Content></tenor>
  <tatbestand><Content><P>Der Klaeger verlangt Schadensersatz.</P></Content></tatbestand>
  <entscheidungsgruende><Content><P>Die Revision ist unbegruendet.</P></Content></entscheidungsgruende>
 </dokument>
</dokumente>""".encode("utf-8")


def test_rii_parser_zones_and_ecli():
    d = parse_rii(RII)
    assert d.metadata["ecli"] == "ECLI:DE:BGH:2021:120521UVIZR100.20.0"
    assert d.metadata["court"] == "Bundesgerichtshof"
    assert d.decision_date == date(2021, 5, 12)
    assert [s.label for s in d.segments] == ["Leitsatz", "Tenor", "Tatbestand", "Entscheidungsgründe"]


def test_de_rii_local_fetch(tmp_path):
    f = tmp_path / "bgh.xml"
    f.write_bytes(RII)
    adapter = DeRiiAdapter(path=str(tmp_path))
    stubs = list(adapter.discover(None))
    rec = adapter.fetch(stubs[0])
    assert rec.doc_type == DocType.JUDGMENT
    assert rec.ecli == "ECLI:DE:BGH:2021:120521UVIZR100.20.0"
    assert rec.court == "Bundesgerichtshof" and rec.text


# -- France: DILA (constructed fixtures, DTD shapes) ------------------------
DILA_JURI = """<?xml version="1.0" encoding="UTF-8"?>
<TEXTE_JURI_JUDI>
 <META><META_COMMUN><ID>JURITEXT000012345</ID><NATURE>ARRET</NATURE></META_COMMUN>
  <META_SPEC><META_JURI>
    <TITRE>Cour de cassation, civile, Chambre civile 1</TITRE>
    <DATE_DEC>2021-05-12</DATE_DEC>
    <JURIDICTION>Cour de cassation</JURIDICTION>
    <NUMERO>21-00400</NUMERO>
    <ECLI>ECLI:FR:CCASS:2021:C100400</ECLI>
    <SOLUTION>Cassation</SOLUTION>
  </META_JURI></META_SPEC>
 </META>
 <TEXTE><BLOC_TEXTUEL><CONTENU><p>Sur le moyen unique, la Cour casse.</p></CONTENU></BLOC_TEXTUEL></TEXTE>
 <LIENS><LIEN id="LEGIARTI000032041571" nature="CITATION">article 1240 du code civil</LIEN></LIENS>
</TEXTE_JURI_JUDI>""".encode("utf-8")

DILA_ARTICLE = """<?xml version="1.0" encoding="UTF-8"?>
<ARTICLE>
 <META><META_COMMUN><ID>LEGIARTI000032041571</ID></META_COMMUN>
  <META_SPEC><META_ARTICLE><NUM>1240</NUM><ETAT>VIGUEUR</ETAT>
   <DATE_DEBUT>2016-10-01</DATE_DEBUT><DATE_FIN>2999-01-01</DATE_FIN></META_ARTICLE></META_SPEC>
 </META>
 <CONTEXTE><TEXTE cid="LEGITEXT000006070721" nature="CODE">
   <TITRE_TXT c_titre_court="Code civil" id_txt="LEGITEXT000006070721">Code civil</TITRE_TXT>
  </TEXTE></CONTEXTE>
 <BLOC_TEXTUEL><CONTENU><p>Tout fait quelconque de l'homme...</p></CONTENU></BLOC_TEXTUEL>
</ARTICLE>""".encode("utf-8")


def test_dila_juri_parse():
    j = parse_dila_juri(ET.fromstring(DILA_JURI))
    assert j.ecli == "ECLI:FR:CCASS:2021:C100400"
    assert j.jurisdiction == "Cour de cassation"
    assert j.date == date(2021, 5, 12)
    assert "casse" in j.text
    assert j.relations and j.relations[0].raw_citation_string.startswith("article 1240")


def test_dila_article_parse():
    a = parse_dila_article(ET.fromstring(DILA_ARTICLE))
    assert a.art_id == "LEGIARTI000032041571"
    assert a.num == "1240"
    assert a.etat == "VIGUEUR"
    assert a.date_debut == date(2016, 10, 1)
    assert a.code_cid == "LEGITEXT000006070721"
    assert a.code_title == "Code civil"


def test_fr_dila_fetch_juri_and_article(tmp_path):
    (tmp_path / "juri.xml").write_bytes(DILA_JURI)
    cass = FrDilaAdapter(path=str(tmp_path), fond="CASS")
    stubs = list(cass.discover(None))
    rec = cass.fetch(stubs[0])
    assert rec.doc_type == DocType.JUDGMENT
    assert rec.stable_id == "ECLI:FR:CCASS:2021:C100400"
    assert rec.relations[0].relationship_type == RelationshipType.MENTIONS

    legi_dir = tmp_path / "legi"
    legi_dir.mkdir()
    (legi_dir / "art.xml").write_bytes(DILA_ARTICLE)
    legi = FrDilaAdapter(path=str(legi_dir), fond="LEGI")
    rec2 = legi.fetch(list(legi.discover(None))[0])
    assert rec2.doc_type == DocType.LEGISLATION
    assert rec2.stable_id == "LEGIARTI000032041571"
    assert "Code civil" in rec2.title


def test_bulk_sources_registered():
    from raglex.adapters.registry import ADAPTERS, source_catalog
    for k in ("de-gii", "de-rii", "fr-dila", "fr-dila-legi", "fr-dila-cnil"):
        assert k in ADAPTERS
    cat = {r["key"]: r for r in source_catalog()}
    assert cat["de-gii"]["jurisdiction"] == "DE"
    assert cat["fr-dila"]["kind"] == "caselaw"
