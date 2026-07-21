from __future__ import annotations

import pytest

from raglex.adapters.eu_legislation import EULegislationAdapter, celex_title
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


# -- new-legislation feed discovery (auto-import new statute, §5a) -----------
LEG_FEED_PAGE1 = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:leg="http://www.legislation.gov.uk/namespaces/legislation">
  <id>http://www.legislation.gov.uk/ukpga+uksi/data.feed</id>
  <leg:page>1</leg:page>
  <leg:morePages>2</leg:morePages>
  <entry>
    <id>http://www.legislation.gov.uk/id/uksi/2026/820</id>
    <title>The Data Protection (Charges) (Amendment) Regulations 2026</title>
    <published>2026-07-16T03:04:13+01:00</published>
    <updated>2026-07-16T03:05:36+01:00</updated>
  </entry>
  <entry>
    <id>http://www.legislation.gov.uk/id/ukia/2026/99</id>
    <title>An impact assessment (not legislation)</title>
    <published>2026-07-15T09:00:00+01:00</published>
  </entry>
  <entry>
    <id>http://www.legislation.gov.uk/id/ukpga/2026/12</id>
    <title>Data (Use and Access) Act 2026</title>
    <published>2026-07-10T00:00:00+01:00</published>
  </entry>
</feed>
"""


def test_parse_legislation_feed_entries_and_paging():
    from raglex.adapters.uk_legislation import parse_legislation_feed

    page = parse_legislation_feed(LEG_FEED_PAGE1)
    assert page.more_pages is True
    paths = [e.path for e in page.entries]
    assert paths == ["uksi/2026/820", "ukia/2026/99", "ukpga/2026/12"]
    assert page.entries[0].published == "2026-07-16T03:04:13+01:00"
    assert page.entries[0].title.startswith("The Data Protection")


def test_feed_discovery_yields_new_items_and_stops_at_cursor():
    ad = UKLegislationAdapter(feed="new", client=_FakeClient(LEG_FEED_PAGE1))
    # no cursor: everything legislation-shaped (the ukia impact assessment is dropped)
    stubs = list(ad.discover(None, max_pages=1))
    assert [s.stable_id for s in stubs] == ["uksi/2026/820", "ukpga/2026/12"]
    assert stubs[0].raw_url.endswith("/uksi/2026/820/data.akn")
    # the full published timestamp is the cursor the pipeline stores
    assert stubs[0].hints["watermark"] == "2026-07-16T03:04:13+01:00"
    # with a cursor: stop at/before it — only the strictly-newer item comes back
    stubs = list(ad.discover("2026-07-10T00:00:00+01:00", max_pages=1))
    assert [s.stable_id for s in stubs] == ["uksi/2026/820"]


def test_feed_mode_is_the_default_and_ids_switch_to_by_id():
    # No ids → full-catalogue feed discovery is the default.
    assert UKLegislationAdapter(client=_FakeClient(b"")).feed is True
    assert UKLegislationAdapter(types="ukpga", client=_FakeClient(b"")).feed is True
    assert UKLegislationAdapter(query="unfair dismissal", client=_FakeClient(b"")).feed is True
    # ids switch to the by-id path
    ad = UKLegislationAdapter(ids="ukpga/2000/36", client=_FakeClient(b""))
    assert ad.feed is False and list(ad.discover(None))[0].stable_id == "ukpga/2000/36"


# -- schedules (OSCOLA pinpoints) -------------------------------------------
# A schedule's citable units are <paragraph>, not <section>, and they live under
# <hcontainer name="schedule">. Labelling them with the generic section rule made
# a schedule read as "s. 1", "s. 2" … restarting the Act's numbering — the
# Children Act 1989 appeared to run s.108 → s.1 — and colliding with the real
# sections for pinpoint matching.
AKN_SCHEDULES = b"""<?xml version="1.0"?>
<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
 <act>
  <body>
   <section><num>1</num><heading>Welfare of the child</heading>
    <subsection><content><p>The child's welfare is paramount.</p></content></subsection></section>
   <section><num>108</num><heading>Short title</heading>
    <subsection><content><p>This Act may be cited as the Test Act.</p></content></subsection></section>
  </body>
  <hcontainer name="schedules">
   <hcontainer name="schedule" eId="schedule-A1">
    <num>SCHEDULE A1</num><heading>Enforcement orders</heading>
    <part eId="schedule-A1-part-1"><num>PART 1</num><heading>Unpaid work</heading>
     <paragraph><num>1</num><content><p>The responsible officer must.</p></content></paragraph>
     <paragraph><num>3A</num><content><p>An inserted paragraph.</p></content></paragraph>
    </part>
    <part eId="schedule-A1-part-2"><num>Part 2</num><heading>Revocation</heading>
     <paragraph><num>4</num><content><p>Revocation of an order.</p></content></paragraph>
    </part>
   </hcontainer>
   <hcontainer name="schedule" eId="schedule-1">
    <num>SCHEDULE 1Section 15(1).</num><heading>Financial Provision</heading>
    <hcontainer name="crossheading"><heading>Orders against parents</heading>
     <paragraph><num>1</num><content><p>On an application the court may make an order.</p></content></paragraph>
     <paragraph><num>2</num><content><p>Orders for those reaching eighteen.</p></content></paragraph>
    </hcontainer>
   </hcontainer>
  </hcontainer>
 </act>
</akomaNtoso>
"""


def _labels(pd):
    return [s.label for s in pd.segments]


def test_schedule_paragraphs_use_oscola_pinpoints():
    labels = _labels(parse("akoma-ntoso", AKN_SCHEDULES))
    # a schedule divided into Parts: "sch 1 pt 1 para 1"
    assert "Sch A1 Pt 1 para 1" in labels
    assert "Sch A1 Pt 1 para 3A" in labels        # inserted paragraphs keep their suffix
    assert "Sch A1 Pt 2 para 4" in labels
    # an undivided schedule: "sch 1 para 1" — no spurious Part
    assert "Sch 1 para 1" in labels
    assert "Sch 1 para 2" in labels


def test_schedule_paragraphs_never_masquerade_as_sections():
    pd = parse("akoma-ntoso", AKN_SCHEDULES)
    labels = _labels(pd)
    sections = [lab for lab in labels if lab.startswith("s. ")]
    # the body's sections survive untouched…
    assert sections == ["s. 1 Welfare of the child", "s. 108 Short title"]
    # …and nothing in a schedule claims to be one, so "s 1" can't match a schedule
    # paragraph during pinpoint resolution
    assert len(labels) == len(set(labels)), "duplicate labels collide on pinpoint lookup"
    kinds = {s.label: s.kind for s in pd.segments}
    assert kinds["Sch 1 para 1"] == "paragraph"
    assert kinds["s. 1 Welfare of the child"] == "section"


def test_act_body_parts_do_not_qualify_section_labels():
    # OSCOLA cites "s 1", never "pt 1 s 1" — a Part of the Act's BODY must not
    # leak into a section's label, unlike a Part of a schedule
    labels = _labels(parse("akoma-ntoso", AKN))
    assert "s. 1 General right of access" in labels
    assert not any(lab.startswith("Pt ") or " Pt " in lab for lab in labels)


# -- EUR-Lex titles ---------------------------------------------------------
# The page <title> is routinely a placeholder — the CELEX banner, or the OJ's own
# XML filename — and the adapter then fell back to a CELEX-derived stand-in
# ("Directive 1995/46"), leaving ~3,000 EU instruments unnamed in the corpus even
# though their real title was in the HTML. No single element carries it across
# EUR-Lex's several layouts, hence the ladder.
EURLEX_LEGACY = b"""<html><head>
 <title>EUR-Lex - 31995L0046 - EN</title>
 <meta name="DC.description" content="Directive 95/46/EC of the European Parliament and of the Council of 24 October 1995 on the protection of individuals with regard to the processing of personal data"/>
 </head><body>
 <h1>31995L0046</h1>
 <p><strong>Directive 95/46/EC of the European Parliament and of the Council of 24 October 1995 on the protection of individuals</strong>
    <em><br>Official Journal L 281 , 23/11/1995 P. 0031 - 0050</em></p>
 <p>Article 1</p><p>Member States shall protect fundamental rights.</p>
</body></html>"""

# same legacy shape, but with NO meta — the title has to come off the body
EURLEX_LEGACY_NO_META = b"""<html><head><title>EUR-Lex - 31981L0712 - EN</title></head><body>
 <h1>31981L0712</h1>
 <p>First Commission Directive 81/712/EEC of 28 July 1981 laying down Community methods of analysis</p>
 <p>Article 1</p><p>Member States shall adopt the methods.</p>
</body></html>"""

# modern Official Journal rendition: <title> is the OJ filename and the real name
# is split across consecutive oj-doc-ti lines
EURLEX_OJ = b"""<html><head><title>L_2011296EN.01000301.xml</title></head><body>
 <p class="oj-hd-ti">Official Journal of the European Union</p>
 <p class="oj-doc-ti">COUNCIL IMPLEMENTING REGULATION (EU) No 1151/2011</p>
 <p class="oj-doc-ti">of 14 November 2011</p>
 <p class="oj-doc-ti">implementing Regulation (EU) No 442/2011 concerning restrictive measures</p>
 <p class="oj-normal">THE COUNCIL OF THE EUROPEAN UNION,</p>
</body></html>"""

# the portal wrapper keeps the name only in an analytics meta tag
EURLEX_PORTAL = b"""<html><head><title>EUR-Lex - CELEX:31994R2291 - EN</title>
 <meta name="WT.z_docTitle" content="Commission Regulation (EC) No 2291/94 of 22 September 1994 fixing the export refunds on cereals"/>
 </head><body>
 <p class="DocumentTitle pull-left">Document&nbsp;31994R2291</p>
 <p>This document is an excerpt from the EUR-Lex website</p>
</body></html>"""


def test_eurlex_title_recovered_from_legacy_layout():
    pd = parse("eurlex-html", EURLEX_LEGACY)
    assert pd.title.startswith("Directive 95/46/EC of the European Parliament")
    # the OJ reference in the <em> is not part of the name
    assert "Official Journal" not in pd.title


def test_eurlex_title_recovered_from_body_without_meta():
    pd = parse("eurlex-html", EURLEX_LEGACY_NO_META)
    # "First Commission Directive …" — a real opener that an allowlist of
    # openers ("Council"/"Commission") would have missed
    assert pd.title.startswith("First Commission Directive 81/712/EEC")


def test_eurlex_title_joined_from_official_journal_lines():
    pd = parse("eurlex-html", EURLEX_OJ)
    # the name only reads as a title once the oj-doc-ti lines are joined
    assert pd.title.startswith("COUNCIL IMPLEMENTING REGULATION (EU) No 1151/2011")
    assert "14 November 2011" in pd.title
    # and the OJ xml filename must never survive as the title
    assert not pd.title.endswith(".xml")


def test_eurlex_title_recovered_from_portal_meta():
    pd = parse("eurlex-html", EURLEX_PORTAL)
    assert pd.title.startswith("Commission Regulation (EC) No 2291/94")


def test_eurlex_page_furniture_is_never_taken_for_a_title():
    junk = b"""<html><head><title>EUR-Lex - 31994R2291 - EN</title></head><body>
     <h1>31994R2291</h1>
     <p>This document is an excerpt from the EUR-Lex website</p>
    </body></html>"""
    pd = parse("eurlex-html", junk)
    # nothing plausible on the page — keep the placeholder rather than invent one,
    # so the adapter's CELEX fallback still fires
    assert pd.title == "EUR-Lex - 31994R2291 - EN"


# -- EU metadata-only stubs -------------------------------------------------
def test_backfill_eu_stubs_upgrades_recoverable_instruments(tmp_path, monkeypatch):
    """An instrument becomes a stub when neither Formex nor HTML came back at
    harvest time — which includes every transient failure, and nothing retried
    them. Re-fetching upgrades the ones that now parse and leaves the rest."""
    from raglex.adapters import eu_legislation as eul
    from raglex.config import Config
    from raglex.core.models import DocType, ExtractedVia, Record
    from raglex.facade import Facade

    cfg = Config(data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite",
                 raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                 settings_path=tmp_path / "s.json", embed_provider="local-hashing",
                 embed_model=None)
    f = Facade(cfg)

    # two stubs exactly as the harvester leaves them
    with f._open() as (cat, _rs, _ts):
        for celex in ("31987D0373", "31931D0081"):
            rec = Record(source="eu-legislation", stable_id=celex,
                         doc_type=DocType.LEGISLATION, title=celex,
                         extracted_via=ExtractedVia.STRUCTURED,
                         raw_bytes=celex.encode(), raw_ext="txt",
                         extra={"celex": celex, "metadata_only": True})
            rec.ensure_payload_hash()
            cat.upsert_document(rec)

    # one is now retrievable upstream; the other genuinely isn't
    def fake_fetch(self, stub):
        if stub.stable_id != "31987D0373":
            return None
        rec = Record(source="eu-legislation", stable_id=stub.stable_id,
                     doc_type=DocType.LEGISLATION,
                     title="87/373/EEC: Council Decision of 13 July 1987 laying down "
                           "the procedures for the exercise of implementing powers",
                     extracted_via=ExtractedVia.STRUCTURED,
                     raw_bytes=b"<html>body</html>", raw_ext="html",
                     text="The Council shall confer on the Commission powers.",
                     extra={"celex": stub.stable_id, "format": "eurlex-html"})
        rec.ensure_payload_hash()
        return rec

    monkeypatch.setattr(eul.EULegislationAdapter, "fetch", fake_fetch)
    res = f.backfill_eu_stubs(limit=10)
    assert res == {"checked": 2, "upgraded": 1}

    got = f.get_document("31987D0373")["document"]
    assert got["title"].startswith("87/373/EEC: Council Decision")
    assert got["payload_hash"]                       # it has real text now
    # the genuinely-absent one is untouched, not blanked
    assert f.get_document("31931D0081")["document"]["title"] == "31931D0081"


def test_backfill_eu_stubs_is_a_noop_without_stubs(tmp_path):
    from raglex.config import Config
    from raglex.facade import Facade

    cfg = Config(data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite",
                 raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                 settings_path=tmp_path / "s.json", embed_provider="local-hashing",
                 embed_model=None)
    assert Facade(cfg).backfill_eu_stubs() == {"checked": 0, "upgraded": 0}
def test_eu_primary_law_celexes_use_formal_names_eli_and_aliases():
    ad = EULegislationAdapter(celex="12012P/TXT")
    stub = next(ad.discover(None))
    assert stub.stable_id == "12012P"
    assert stub.landing_url == "https://eur-lex.europa.eu/eli/treaty/char_2012/oj/eng"
    assert celex_title("12012P") == "Charter of Fundamental Rights of the European Union"
    assert celex_title("12016M").endswith("Treaty on European Union")
    assert celex_title("12016E").endswith("Treaty on the Functioning of the European Union")


def test_eu_legislation_default_enumeration_includes_whole_treaties():
    q = EULegislationAdapter()._enumerate_query(None, 0)
    assert "a cdm:treaty" in q
    assert "work_has_resource-type" in q
    assert '^1[0-9]{4}[A-Z]{1,2}$' in q
    assert "TREATY" in EULegislationAdapter().types
