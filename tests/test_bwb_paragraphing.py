"""BWB parser paragraphing — a numbered/enumerated article must render as a list, not a
flat wall, and the Juriconnect change-note / source annotations must never bleed into the
body or the title. Regression for the "wall of text" fix (parity with the fr-dila/rii
paragraphing pass)."""

from __future__ import annotations

from raglex.formats.bwb import parse_bwb

# A single article with two <lid> members, a <lijst>/<li> enumeration, and — nested inside
# the lid AND the article — a <meta-data> change-note (the "2001 584 18-12-2001" junk).
ARTICLE_XML = b"""<?xml version="1.0"?>
<toestand>
  <bwb-wijzigingen><wijziging>2001 584 18-12-2001</wijziging></bwb-wijzigingen>
  <wetgeving>
    <citeertitel>Testwet<meta-data><brondata>1869 139</brondata></meta-data></citeertitel>
    <wettekst><artikel>
      <kop><label>Artikel</label><nr>1</nr></kop>
      <lid>
        <lidnr>1</lidnr>
        <al>De rechtbanken nemen kennis van:</al>
        <lijst>
          <li><li.nr>1&#176;.</li.nr><al>de eerste categorie;</al></li>
          <li><li.nr>2&#176;.</li.nr><al>de tweede categorie.</al></li>
        </lijst>
        <meta-data><jcis><jci>2001 584 18-12-2001</jci></jcis></meta-data>
      </lid>
      <lid>
        <lidnr>2</lidnr>
        <al>Indien de zaken kantonzaken betreffen, beslist de kantonrechter.</al>
      </lid>
      <meta-data><brondata><oorspronkelijk>2001 6</oorspronkelijk></brondata></meta-data>
    </artikel></wettekst>
  </wetgeving>
</toestand>"""


def test_article_renders_as_paragraphed_list():
    doc = parse_bwb(ARTICLE_XML)
    art = next(s for s in doc.segments if s.kind == "article")
    body = doc.text[art.char_start:art.char_end]
    lines = body.split("\n")
    # each lid starts its own line, with its number inline before the alinea
    assert lines[0] == "1 De rechtbanken nemen kennis van:"
    assert "2 Indien de zaken kantonzaken betreffen, beslist de kantonrechter." in lines
    # the enumerated list items are each on their own line, marker kept with the text
    assert "1°. de eerste categorie;" in lines
    assert "2°. de tweede categorie." in lines


def test_change_notes_are_pruned_from_body_and_title():
    doc = parse_bwb(ARTICLE_XML)
    # the "2001 584 18-12-2001" / "1869 139" / "2001 6" annotation debris must be gone
    assert "18-12-2001" not in doc.text
    assert "1869 139" not in (doc.title or "")
    assert "2001 6" not in doc.text
    assert doc.title == "Testwet"


def test_segment_offsets_map_exactly():
    doc = parse_bwb(ARTICLE_XML)
    assert doc.segments
    for seg in doc.segments:
        assert doc.text[seg.char_start:seg.char_end].strip()
