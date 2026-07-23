"""DILA JADE parser — <br/> paragraph structure in <CONTENU> is preserved as real
paragraph breaks rather than collapsed into one blob."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from raglex.formats.dila_xml import _render_br, parse_dila_juri

JURI = """<TEXTE_JURI_ADMIN>
  <META><META_COMMUN><ID>CETATEXT000</ID></META_COMMUN>
    <META_SPEC><META_JURI>
      <TITRE>M. B</TITRE><DATE_DEC>2025-04-04</DATE_DEC>
      <JURIDICTION>Conseil d'Etat</JURIDICTION><NUMERO>491870</NUMERO>
      <NATURE>Decision</NATURE><ECLI>ECLI:FR:CECHS:2025:491870.20250404</ECLI>
    </META_JURI></META_SPEC>
  </META>
  <TEXTE><BLOC_TEXTUEL><CONTENU>Vu la procedure suivante :<br/>
<br/>
Par une requete, M. B demande au Conseil d'Etat :<br/>
<br/>
1) d'annuler l'arrete ;<br/>
<br/>
2) de mettre a la charge de l'Etat la somme.</CONTENU></BLOC_TEXTUEL></TEXTE>
</TEXTE_JURI_ADMIN>"""


def test_br_paragraphs_preserved():
    root = ET.fromstring(JURI)
    r = parse_dila_juri(root)
    t = r.text or ""
    # double <br/> becomes a paragraph break; the body is no longer one blob
    assert t.count("\n\n") >= 3
    assert "Vu la procedure suivante :" in t
    assert "1) d'annuler l'arrete ;" in t
    # paragraphs are separated, not run together on one line
    assert "suivante :\n" in t
    assert "requete, M. B demande" in t


def test_render_br_single_vs_double():
    el = ET.fromstring("<CONTENU>Line one<br/>line two<br/><br/>New paragraph.</CONTENU>")
    out = _render_br(el)
    assert out == "Line one\nline two\n\nNew paragraph."


def test_no_br_still_reads():
    el = ET.fromstring("<CONTENU>A single sentence with no breaks.</CONTENU>")
    assert _render_br(el) == "A single sentence with no breaks."
