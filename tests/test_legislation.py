from __future__ import annotations

import pytest

from raglex.adapters.eu_legislation import EULegislationAdapter
from raglex.adapters.uk_legislation import UKLegislationAdapter
from raglex.core.errors import FetchError
from raglex.core.models import DocType
from raglex.formats import available, parse

AKN = b"""<?xml version="1.0"?>
<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
 <act>
  <meta><identification><FRBRWork><FRBRname value="2000 c. 36"/></FRBRWork></identification></meta>
  <preface><longTitle><p>An Act to make provision for Freedom of Information.</p></longTitle></preface>
  <body>
   <part><num>Part I</num><heading>Access to information</heading>
    <section><num>1</num><heading>General right of access</heading>
     <subsection><num>(1)</num><content><p>Any person may request, see
       <ref href="http://www.legislation.gov.uk/ukpga/1998/29">DPA 1998</ref>.</p></content></subsection>
    </section>
    <section><num>14</num><heading>Vexatious requests</heading>
     <subsection><content><p>A request may be refused if vexatious.</p></content></subsection>
    </section>
   </part>
  </body>
 </act>
</akomaNtoso>
"""

FORMEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<ACT>
 <TITLE><TI><P>Regulation (EU) Test 2016/679</P></TI></TITLE>
 <PREAMBLE>
  <GR.CONSID>
   <CONSID><NP><NO.P>(1)</NO.P><TXT>The protection of personal data is a fundamental right.</TXT></NP></CONSID>
   <CONSID><NP><NO.P>(2)</NO.P><TXT>This Regulation respects fundamental rights.</TXT></NP></CONSID>
  </GR.CONSID>
 </PREAMBLE>
 <ENACTING.TERMS>
  <ARTICLE><TI.ART>Article 1</TI.ART><STI.ART>Subject-matter</STI.ART>
    <PARAG><ALINEA>This Regulation lays down rules on personal data.</ALINEA></PARAG></ARTICLE>
  <ARTICLE><TI.ART>Article 2</TI.ART><STI.ART>Scope</STI.ART>
    <PARAG><NO.PARAG>1.</NO.PARAG><ALINEA>It applies to processing of personal data.</ALINEA></PARAG>
    <PARAG><NO.PARAG>2.</NO.PARAG><ALINEA><P>It does not apply to:</P>
      <LIST><ITEM><NP><NO.P>(a)</NO.P><TXT>household activity;</TXT></NP></ITEM>
            <ITEM><NP><NO.P>(b)</NO.P><TXT>law enforcement.</TXT></NP></ITEM></LIST></ALINEA></PARAG></ARTICLE>
 </ENACTING.TERMS>
</ACT>
"""


# -- format parsers ---------------------------------------------------------
def test_registry_has_legislation_formats():
    assert {"akoma-ntoso", "formex-legislation"} <= set(available())


def test_akn_parses_hierarchy_levels_and_external_refs():
    pd = parse("akoma-ntoso", AKN)
    assert pd.title == "An Act to make provision for Freedom of Information."
    kinds = [(s.kind, s.level) for s in pd.segments]
    assert ("part", 0) in kinds and ("section", 1) in kinds
    # spans index into the flat text
    for s in pd.segments:
        assert pd.text[s.char_start:s.char_end].strip()
    # external citation kept, internal cross-refs dropped
    cites = [r.raw_citation_string for r in pd.relations]
    assert any("ukpga/1998/29" in c for c in cites)


def test_formex_legislation_parses_articles():
    pd = parse("formex-legislation", FORMEX)
    assert pd.title and "2016/679" in pd.title
    labels = [s.label for s in pd.segments]
    # recitals come first (preamble), then articles
    assert labels == ["Recital 1", "Recital 2", "Article 1 Subject-matter", "Article 2 Scope"]
    assert "personal data" in pd.text


def test_formex_recitals_and_formatting():
    pd = parse("formex-legislation", FORMEX)
    by_label = {s.label: pd.text[s.char_start:s.char_end] for s in pd.segments}
    # recital body keeps the text but drops the redundant leading "(1)"
    assert by_label["Recital 1"].startswith("The protection of personal data")
    art2 = by_label["Article 2 Scope"]
    # the article heading is NOT duplicated inside the body
    assert not art2.startswith("Article 2")
    # numbered paragraphs and lettered points each sit on their own line
    assert "1. It applies to processing" in art2
    assert "\n2. It does not apply to:" in art2
    assert "\n(a) household activity;" in art2 and "\n(b) law enforcement." in art2


def test_akn_section_formatting_breaks_subsections():
    pd = parse("akoma-ntoso", AKN)
    s1 = next(pd.text[s.char_start:s.char_end] for s in pd.segments if s.label.startswith("s. 1 "))
    assert not s1.startswith("1")  # num/heading not duplicated into the body
    assert "(1) Any person may request" in s1


# -- adapters ---------------------------------------------------------------
class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeClient:
    def __init__(self, content):
        self._c = content

    def get(self, url, **kw):
        return _Resp(self._c)


def test_uk_legislation_adapter_builds_legislation_record():
    ad = UKLegislationAdapter(ids="ukpga/2000/36", client=_FakeClient(AKN))
    stubs = list(ad.discover(None))
    assert stubs[0].stable_id == "ukpga/2000/36"  # resolution target form (§5b)
    rec = ad.fetch(stubs[0])
    assert rec.doc_type == DocType.LEGISLATION
    assert rec.title == "An Act to make provision for Freedom of Information."
    assert len(rec.segments) >= 3 and rec.extra["format"] == "akoma-ntoso"


AKN_WITH_EFFECTS = b"""<?xml version="1.0"?>
<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
            xmlns:ukm="http://www.legislation.gov.uk/namespaces/metadata">
 <act>
  <meta>
   <identification><FRBRWork><FRBRname value="2018 c. 12"/></FRBRWork></identification>
   <proprietary>
    <ukm:UnappliedEffects>
     <ukm:UnappliedEffect Type="s. 166 substituted"
         AffectingURI="http://www.legislation.gov.uk/id/ukpga/2025/8" AffectedSectionRef="section-166"/>
     <ukm:UnappliedEffect Type="words inserted"
         AffectingURI="http://www.legislation.gov.uk/id/ukpga/2025/8/section/9" AffectedSectionRef="section-167"/>
    </ukm:UnappliedEffects>
   </proprietary>
  </meta>
  <preface><longTitle><p>An Act about data protection.</p></longTitle></preface>
  <body><section><num>166</num><content><p>Orders to progress complaints.</p></content></section></body>
 </act>
</akomaNtoso>
"""


def test_uk_adapter_mints_amended_by_edges_and_effects_summary():
    ad = UKLegislationAdapter(ids="ukpga/2018/12", client=_FakeClient(AKN_WITH_EFFECTS))
    rec = ad.fetch(list(ad.discover(None))[0])
    # one edge per distinct effect, all to the amending Act, each carrying which
    # provision is changed (src_anchor) and how (dst_anchor) — as much metadata as given
    amended = sorted((r for r in rec.relations if r.relationship_type.value == "amended_by"),
                     key=lambda r: r.src_anchor)
    assert [r.dst_id for r in amended] == ["ukpga/2025/8", "ukpga/2025/8"]
    assert [(r.src_anchor, r.dst_anchor) for r in amended] == [
        ("section-166", "s. 166 substituted"), ("section-167", "words inserted")]
    # the outstanding-effects summary rides on the record for the pipeline to queue
    assert rec.extra["unapplied_effects"]["outstanding"] == 2
    assert rec.extra["unapplied_effects"]["affecting"] == ["ukpga/2025/8"]


def test_point_in_time_copy_does_not_track_effects():
    # a dated snapshot has no effects machinery → no queue entry, no amended_by edges
    ad = UKLegislationAdapter(ids="ukpga/2018/12", version_date="2010-01-01",
                              client=_FakeClient(AKN_WITH_EFFECTS))
    rec = ad.fetch(list(ad.discover(None))[0])
    assert "unapplied_effects" not in rec.extra
    assert not [r for r in rec.relations if r.relationship_type.value == "amended_by"]


def test_uk_adapter_treats_eur_typecode_as_assimilated_eu_law():
    # legislation.gov.uk type-code form for assimilated EU law (eur/eudr/eudn) must be
    # titled "Assimilated …" and linked to the EU original's CELEX, like /european/…
    ad = UKLegislationAdapter(ids="eur/2016/679", client=_FakeClient(AKN))
    rec = ad.fetch(list(ad.discover(None))[0])
    assert rec.title.startswith("Assimilated ")
    assert any(r.relationship_type.value == "assimilated_version_of" and r.dst_id == "32016R0679"
               for r in rec.relations)


class _StatusResp:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code


def test_uk_adapter_raises_transient_on_202_async_generation(monkeypatch):
    # Large representations answer 202 + empty body while legislation.gov.uk builds them.
    # The adapter must retry, then report a TRANSIENT failure rather than store an empty
    # stub — the document exists, so the drain must retry it in hours, not write it off
    # as absent for months.
    import raglex.adapters.uk_legislation as ukl
    monkeypatch.setattr(ukl.time, "sleep", lambda *_: None)

    class _Always202:
        def get(self, url, **kw):
            return _StatusResp(b"", status_code=202)

    ad = UKLegislationAdapter(ids="eur/2008/1272", client=_Always202())
    with pytest.raises(FetchError) as exc:
        ad.fetch(list(ad.discover(None))[0])
    assert exc.value.transient is True


def test_eu_legislation_adapter_builds_from_formex():
    ad = EULegislationAdapter(celex="32016R0679", client=_FakeClient(FORMEX))
    rec = ad.fetch(list(ad.discover(None))[0])
    assert rec.stable_id == "32016R0679"  # CELEX = resolution target
    assert rec.doc_type == DocType.LEGISLATION
    assert [s.kind for s in rec.segments] == ["recital", "recital", "article", "article"]


BWB = b"""<?xml version="1.0"?>
<toestand><wetgeving>
  <intitule>Wet van 16 mei 2018 houdende regels</intitule>
  <citeertitel>Uitvoeringswet AVG</citeertitel>
  <wettekst>
   <hoofdstuk><kop><nr>1</nr><titel>Algemene bepalingen</titel></kop>
    <artikel><kop><nr>1</nr><titel>Definities</titel></kop>
      <lid><al>In deze wet wordt verstaan onder persoonsgegevens: gegevens.</al></lid></artikel>
    <artikel><kop><nr>2</nr><titel>Reikwijdte</titel></kop>
      <lid><al>Deze wet is van toepassing.</al></lid></artikel>
   </hoofdstuk>
  </wettekst>
</wetgeving></toestand>
"""


def test_bwb_parses_dutch_legislation():
    pd = parse("bwb", BWB)
    assert pd.title == "Uitvoeringswet AVG"  # citeertitel preferred
    labels = [s.label for s in pd.segments]
    assert "Artikel 1 Definities" in labels and "Artikel 2 Reikwijdte" in labels
    assert any(s.kind == "hoofdstuk" and s.level == 0 for s in pd.segments)
    assert "persoonsgegevens" in pd.text


def test_legislation_adapters_registered():
    from raglex.adapters.registry import get_adapter

    assert get_adapter("uk-legislation").source == "uk-legislation"
    assert get_adapter("eu-legislation").source == "eu-legislation"
    assert get_adapter("nl-legislation").source == "nl-legislation"
