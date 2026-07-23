"""juris rii parser — numbered-paragraph (Randnummer) segmentation from RspDL blocks,
and that the reparse sniffer recognises rii XML."""

from __future__ import annotations

from raglex.facade import _sniff_format
from raglex.formats.rii_xml import parse_rii

RII = """<?xml version="1.0" encoding="UTF-8"?>
<dokument doknr="KORE000012345">
  <ecli>ECLI:DE:BGH:2023:200723UIIIZR267.20.0</ecli>
  <gertyp>BGH</gertyp>
  <entsch-datum>20230720</entsch-datum>
  <aktenzeichen>III ZR 267/20</aktenzeichen>
  <leitsatz>
    <dl class="RspDL"><dt/><dd><p>Ein Leitsatz zum Schadensersatz.</p></dd></dl>
  </leitsatz>
  <tenor>
    <dl class="RspDL"><dt/><dd><p>Auf die Revision wird das Urteil aufgehoben.</p></dd></dl>
  </tenor>
  <gruende>
    <dl class="RspDL"><dt><a name="rd_1">1</a></dt>
      <dd><p>Die Klaegerin nimmt den Fahrzeughersteller in Anspruch.</p></dd></dl>
    <dl class="RspDL"><dt><a name="rd_2">2</a></dt>
      <dd><p>Das Berufungsgericht hat die Klage abgewiesen.</p></dd></dl>
    <dl class="RspDL"><dt><a name="rd_3">3</a></dt>
      <dd><p>Die Revision hat Erfolg.</p></dd></dl>
  </gruende>
</dokument>"""


def test_numbered_paragraphs_become_segments():
    pd = parse_rii(RII.encode("utf-8"))
    para = [s for s in pd.segments if s.kind == "paragraph"]
    labels = [s.label for s in para]
    # the Gründe Randnummern become paragraph labels
    assert "1" in labels and "2" in labels and "3" in labels
    # and each segment is byte-aligned to its paragraph text
    seg2 = next(s for s in pd.segments if s.label == "2")
    assert pd.text[seg2.char_start:seg2.char_end] == "Das Berufungsgericht hat die Klage abgewiesen."


def test_no_inline_randnummern_and_real_paragraph_breaks():
    pd = parse_rii(RII.encode("utf-8"))
    # the paragraph text must NOT begin with its own number (the flattening bug)
    assert "1 Die Klaegerin" not in pd.text
    assert "Die Klaegerin nimmt" in pd.text
    # paragraphs are separated by blank lines, not run together
    assert pd.text.count("\n\n") >= 4


def test_flat_zone_still_single_block():
    # a zone with no RspDL keeps its single-block behaviour
    xml = ("<dokument><gertyp>BGH</gertyp><ecli>ECLI:DE:BGH:2020:1</ecli>"
           "<tenor>Die Klage wird abgewiesen.</tenor></dokument>")
    pd = parse_rii(xml.encode("utf-8"))
    tenor = [s for s in pd.segments if s.label == "Tenor"]
    assert len(tenor) == 1
    assert pd.text[tenor[0].char_start:tenor[0].char_end] == "Die Klage wird abgewiesen."


def test_sniffer_recognises_rii():
    assert _sniff_format(RII.encode("utf-8")) == "rii-xml"
    # not confused with legislation / other xml
    assert _sniff_format(b"<toestand><wetgeving/></toestand>") == "bwb"
