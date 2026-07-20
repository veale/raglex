"""Canada federal, Hong Kong and New Zealand legislation — the three format parsers,
their adapters, and the identifier grammars that make their cross-references resolve.
Network-free; fixtures are trimmed from the real corpora.
"""

from __future__ import annotations

import json

import pytest

from raglex.adapters.ca_legislation import (
    CanadaFederalAdapter,
    build_annual_index,
    load_lookup,
    parse_historical_citation,
)
from raglex.adapters.hk_legislation import HKLegislationAdapter, scan_bulk_dir, sitemap_caps
from raglex.adapters.nz_legislation import NZLegislationAdapter, parse_work_id
from raglex.core.errors import RateLimitException
from raglex.core.models import DocType, RelationshipType
from raglex.formats.hklm_xml import hk_id, parse_hklm_xml
from raglex.formats.lims_xml import ca_id, parse_lims_xml
from raglex.formats.nz_pco_xml import nz_id, parse_nz_pco_xml

# -- fixtures ----------------------------------------------------------------

STATUTE_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<Statute xmlns:lims="http://justice.gc.ca/lims" lims:pit-date="2024-01-22"
         hasPreviousVersion="true" lims:lastAmendedDate="2024-01-22"
         lims:current-date="2024-01-23" lims:inforce-start-date="2018-12-13"
         lims:id="167" bill-origin="commons" bill-type="govt-public" in-force="yes"
         xml:lang="en">
  <Identification>
    <LongTitle lims:enacted-date="2019-06-21">An Act to extend the present laws</LongTitle>
    <ShortTitle status="official">Access to Information Act</ShortTitle>
    <Chapter><ConsolidatedNumber official="yes">A-1</ConsolidatedNumber></Chapter>
  </Identification>
  <Body>
    <Heading level="1"><TitleText>Short Title</TitleText></Heading>
    <Section lims:inforce-start-date="2002-12-31" lims:lastAmendedDate="2002-12-31">
      <MarginalNote>Short title</MarginalNote>
      <Label>1</Label>
      <Text>This Act may be cited as the
        <XRefExternal reference-type="act" link="A-1">Access to Information Act</XRefExternal>.</Text>
      <HistoricalNote>
        <HistoricalNoteSubItem lims:inforce-start-date="2002-12-31">1980-81-82-83, c. 111</HistoricalNoteSubItem>
      </HistoricalNote>
    </Section>
    <Section lims:inforce-start-date="2019-06-21" lims:enacted-date="2019-06-21">
      <MarginalNote>Purpose</MarginalNote>
      <Label>2</Label>
      <Text>The purpose of this Act is to enhance accountability under the
        <XRefExternal reference-type="act" link="F-27">FOOD AND DRUGS ACT</XRefExternal>.</Text>
      <HistoricalNote>
        <HistoricalNoteSubItem type="original">R.S., 1985, c. A-1, s. 2</HistoricalNoteSubItem>
        <HistoricalNoteSubItem>2019, c. 18, s. 2</HistoricalNoteSubItem>
      </HistoricalNote>
    </Section>
  </Body>
</Statute>"""

REGULATION_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<Regulation xmlns:lims="http://justice.gc.ca/lims" lims:pit-date="2023-11-24"
            regulation-type="SOR" xml:lang="en">
  <Identification>
    <InstrumentNumber>C.R.C., c. 870</InstrumentNumber>
    <ConsolidationDate><Date><YYYY>2023</YYYY><MM>11</MM><DD>28</DD></Date></ConsolidationDate>
    <EnablingAuthority>
      <XRefExternal reference-type="act" link="F-27">FOOD AND DRUGS ACT</XRefExternal>
    </EnablingAuthority>
    <ShortTitle>Food and Drug Regulations</ShortTitle>
  </Identification>
  <Body>
    <Section>
      <Label>A.01.002</Label>
      <Text>These Regulations prescribe standards of composition.</Text>
      <HistoricalNote>
        <HistoricalNoteSubItem>SOR/2024-244, s. 1</HistoricalNoteSubItem>
      </HistoricalNote>
    </Section>
  </Body>
</Regulation>"""

LOOKUP_XML = """<?xml version="1.0" encoding="utf-8"?>
<Database>
<Statutes>
<Statute id="167e"><ChapterNumber>A-1</ChapterNumber><OfficialNumber>A-1</OfficialNumber>
<Language>en</Language><ShortTitle>Access to Information Act</ShortTitle>
<LastConsolidationDate>20260527</LastConsolidationDate>
<Relationships><Relationship rid="870e" /></Relationships></Statute>
<Statute id="167f"><ChapterNumber>A-1</ChapterNumber><OfficialNumber>A-1</OfficialNumber>
<Language>fr</Language><ShortTitle>Loi sur l'acces a l'information</ShortTitle>
<LastConsolidationDate>20260527</LastConsolidationDate></Statute>
<Statute id="900e"><ChapterNumber>A-0.6</ChapterNumber><OfficialNumber>2019, c. 10</OfficialNumber>
<Language>en</Language><ShortTitle>Accessible Canada Act</ShortTitle>
<LastConsolidationDate>20260101</LastConsolidationDate></Statute>
</Statutes>
<Regulations>
<Regulation id="870e" olid="870f"><AlphaNumber>C.R.C., c. 870</AlphaNumber>
<Language>en</Language><ShortTitle>Food and Drug Regulations</ShortTitle>
<LastConsolidationDate>20260527</LastConsolidationDate></Regulation>
</Regulations>
</Database>"""

ORDINANCE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<lawDoc xmlns="http://www.xml.gov.hk/schemas/hklm/1.0"
        xmlns:dc="http://purl.org/dc/elements/1.1/" xml:lang="en">
<meta><docName>Cap. 486</docName><docType>cap</docType><docNumber>486</docNumber>
<docStatus>In effect</docStatus><dc:identifier>/hk/cap486!en</dc:identifier>
<dc:date>2022-10-01</dc:date><dc:language>en</dc:language></meta>
<main xml:lang="en">
  <longTitle><content>An Ordinance to protect the privacy of individuals.</content></longTitle>
  <part name="P1"><num>Part 1</num><heading>Preliminary</heading>
    <section name="s1"><num value="1">1.</num><heading>Short title</heading>
      <content>This Ordinance may be cited as the Personal Data (Privacy) Ordinance.</content>
      <sourceNote>(Amended <ref href="/hk/2007/ln130">L.N. 130 of 2007</ref>)</sourceNote>
    </section>
    <section name="s4"><num value="4">4.</num><heading>Data protection principles</heading>
      <content>A data user shall not contravene a data protection principle, subject to
        <ref href="/hk/cap66">Cap. 66</ref> and <ref href="/hk/cap571/s5">s. 5</ref>.</content>
    </section>
  </part>
</main></lawDoc>"""

NZ_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<act as.at="2022-08-30">
  <cover><title>New Zealand Bill of Rights Act 1990</title></cover>
  <body>
    <part><label>1</label><heading>General provisions</heading>
      <prov><label>3</label><heading>Application</heading>
        <text>This Bill of Rights applies only to acts done by the legislative branch.</text>
      </prov>
      <prov><label>4</label><heading>Other enactments not affected</heading>
        <text>No court shall hold any provision of an enactment to be impliedly repealed.</text>
      </prov>
    </part>
  </body>
</act>"""


@pytest.fixture()
def ca_repo(tmp_path):
    """A miniature laws-lois-xml checkout."""
    (tmp_path / "lookup").mkdir()
    (tmp_path / "lookup" / "lookup.xml").write_text(LOOKUP_XML, encoding="utf-8")
    for rel in ("eng/acts", "eng/regulations", "fra/lois", "fra/reglements"):
        (tmp_path / rel).mkdir(parents=True)
    (tmp_path / "eng/acts/A-1.xml").write_bytes(STATUTE_XML)
    (tmp_path / "eng/acts/A-0.6.xml").write_bytes(STATUTE_XML)
    (tmp_path / "eng/regulations/C.R.C.,_c._870.xml").write_bytes(REGULATION_XML)
    (tmp_path / "fra/lois/A-1.xml").write_bytes(STATUTE_XML)
    return tmp_path


@pytest.fixture()
def hk_drop(tmp_path):
    """A miniature e-Legislation bulk drop."""
    folder = tmp_path / "cap_486_en_c"
    folder.mkdir()
    (folder / "cap_486_20221001000000_en_c.xml").write_bytes(ORDINANCE_XML)
    other = tmp_path / "cap_1_en_c"
    other.mkdir()
    (other / "cap_1_20251218000000_en_c.xml").write_bytes(ORDINANCE_XML)
    return tmp_path


# -- Canada: identifiers -----------------------------------------------------

def test_ca_id_folds_every_naming_form_to_one_id():
    """The same regulation is named three ways across the corpus — instrument number,
    XRefExternal link, filename stem — and all must fold to one id or every
    cross-reference dangles."""
    assert ca_id("regulation", "C.R.C., c. 870") == "ca/regulation/crc-c-870"
    assert ca_id("regulation", "C.R.C.,_c._870") == "ca/regulation/crc-c-870"
    assert ca_id("regulation", "SOR/2001-383") == ca_id("regulation", "SOR-2001-383")


def test_ca_id_keeps_act_code_dots_because_they_are_significant():
    """A-1.3 and A-13 are different Acts — folding dots out of Act codes would merge
    them silently."""
    assert ca_id("act", "A-1.3") != ca_id("act", "A-13")
    assert ca_id("act", "P-31.55") == "ca/act/p-31.55"


def test_ca_id_language_suffix_keeps_english_as_the_primary_key():
    assert ca_id("act", "A-1", "eng") == "ca/act/a-1"
    assert ca_id("act", "A-1", "fra") == "ca/act/a-1/fra"


# -- Canada: the lims parser -------------------------------------------------

def test_lims_parser_reads_identity_pit_and_provision_temporal_data():
    doc = parse_lims_xml(STATUTE_XML)
    meta = doc.metadata
    assert doc.title == "Access to Information Act"
    assert meta["code"] == "A-1" and meta["kind"] == "act"
    assert meta["pit_date"].isoformat() == "2024-01-22"
    # provision-level in-force dates are the finest-grained point-in-time in the corpus
    labels = {p.label: p for p in meta["provisions"]}
    assert labels["s. 2 — Purpose"].inforce_start.isoformat() == "2019-06-21"


def test_lims_parser_lifts_historical_notes_out_of_the_operative_text():
    """Left inline, the amendment chain reads as part of the law and wrecks diffs
    between consolidations."""
    doc = parse_lims_xml(STATUTE_XML)
    assert "1980-81-82-83" not in (doc.text or "")
    citations = [n.citation for n in doc.metadata["historical_notes"]]
    assert "R.S., 1985, c. A-1, s. 2" in citations
    assert any(n.original for n in doc.metadata["historical_notes"])


def test_lims_parser_mints_the_enabling_act_edge_from_the_regulation():
    doc = parse_lims_xml(REGULATION_XML)
    assert doc.metadata["enabling_act_id"] == "ca/act/f-27"
    enabling = [r for r in doc.relations if r.dst_anchor == "made under"]
    assert enabling and enabling[0].relationship_type is RelationshipType.IMPLEMENTS
    assert doc.metadata["regulation_type"] == "SOR"


def test_lims_parser_skips_non_corpus_xref_types():
    """reference-type other/standard/canada-gazette point outside the legislative
    corpus — minting ids for them is the hanging-reference trap."""
    xml = STATUTE_XML.replace(b'reference-type="act" link="F-27"',
                              b'reference-type="standard" link="ISO-9001"')
    doc = parse_lims_xml(xml)
    assert all("iso-9001" not in (r.dst_id or "") for r in doc.relations)


# -- Canada: historical-note citations --------------------------------------

def test_parse_historical_citation_handles_each_published_form():
    index = {"2019, c. 18": "A-0.6"}
    assert parse_historical_citation("R.S., 1985, c. A-1, s. 3")[0] == "ca/act/a-1"
    assert parse_historical_citation("SOR/2024-244, s. 1")[0] == "ca/regulation/sor-2024-244"
    # DORS/TR are the French series names for the same instruments
    assert parse_historical_citation("DORS/2007-151")[0] == "ca/regulation/sor-2007-151"
    assert parse_historical_citation("2019, c. 18, s. 2", index)[0] == "ca/act/a-0.6"
    assert parse_historical_citation("R.S., 1985, c. A-1, s. 3")[1] == "s. 3"


def test_parse_historical_citation_refuses_to_guess_unmappable_annual_cites():
    """Pre-1985 annual chapters name a statute volume the consolidated corpus doesn't
    carry — an id here could never resolve."""
    assert parse_historical_citation("1980-81-82-83, c. 111, Sch. I")[0] is None
    assert parse_historical_citation("2019, c. 18, s. 2")[0] is None  # no index supplied


def test_build_annual_index_maps_official_numbers_to_chapter_codes(ca_repo):
    index = build_annual_index(load_lookup(ca_repo))
    assert index["2019, c. 10"] == "A-0.6"


# -- Canada: the adapter -----------------------------------------------------

def test_ca_adapter_enumerates_from_the_manifest(ca_repo):
    stubs = list(CanadaFederalAdapter(path=ca_repo).discover(None))
    ids = {s.stable_id for s in stubs}
    assert ids == {"ca/act/a-1", "ca/act/a-0.6", "ca/regulation/crc-c-870"}
    assert all(s.hint_date is not None for s in stubs)


def test_ca_adapter_is_incremental_on_the_consolidation_date(ca_repo):
    """The manifest's LastConsolidationDate is the change signal — a document that
    hasn't been re-consolidated is skipped without being opened."""
    stubs = list(CanadaFederalAdapter(path=ca_repo).discover("2026-03-01"))
    assert {s.stable_id for s in stubs} == {"ca/act/a-1", "ca/regulation/crc-c-870"}
    assert list(CanadaFederalAdapter(path=ca_repo).discover("2026-05-27")) == []


def test_ca_adapter_language_and_type_filters(ca_repo):
    assert {s.stable_id for s in CanadaFederalAdapter(path=ca_repo, types="act").discover(None)} \
        == {"ca/act/a-1", "ca/act/a-0.6"}
    both = {s.stable_id for s in CanadaFederalAdapter(path=ca_repo, lang="both").discover(None)}
    assert "ca/act/a-1/fra" in both and "ca/act/a-1" in both


def test_ca_adapter_builds_amendment_and_made_under_edges(ca_repo):
    adapter = CanadaFederalAdapter(path=ca_repo)
    stub = next(s for s in adapter.discover(None) if s.stable_id == "ca/act/a-1")
    record = adapter.fetch(stub)
    assert record.doc_type is DocType.LEGISLATION
    assert record.extra["is_authoritative"] is True
    assert record.extra["authoritative_languages"] == ["eng", "fra"]
    # the Act -> regulations-made-under-it edge lives ONLY in the manifest
    made_under = [r for r in record.relations if r.dst_anchor == "made under this Act"]
    assert [r.dst_id for r in made_under] == ["ca/regulation/crc-c-870"]
    # and the co-equal French Expression is paired by chapter code (statutes have no olid)
    assert record.extra["other_language_id"] == "ca/act/a-1/fra"


def test_ca_adapter_carries_provision_level_point_in_time(ca_repo):
    adapter = CanadaFederalAdapter(path=ca_repo)
    stub = next(s for s in adapter.discover(None) if s.stable_id == "ca/act/a-1")
    record = adapter.fetch(stub)
    assert record.extra["point_in_time"] == "2024-01-22"
    assert any(p["inforce_start"] == "2019-06-21" for p in record.extra["provisions"])


def test_ca_adapter_falls_back_to_a_directory_walk_without_a_manifest(ca_repo):
    """A partial checkout must still be ingestible — it just loses the dates."""
    (ca_repo / "lookup" / "lookup.xml").unlink()
    stubs = list(CanadaFederalAdapter(path=ca_repo, types="act").discover(None))
    assert {s.stable_id for s in stubs} == {"ca/act/a-1", "ca/act/a-0.6"}


# -- Hong Kong ---------------------------------------------------------------

def test_hk_id_and_cross_reference_forms_agree():
    assert hk_id("cap", "486") == "hk/cap/486"
    assert hk_id("cap", "132CI") == "hk/cap/132ci"
    assert hk_id("instrument", "A101") == "hk/instrument/a101"


def test_hklm_parser_recovers_the_short_title_from_section_one():
    """HKLM has no short-title element — without the citation formula the corpus would
    be titled "Cap. 486", which is not how anyone cites or searches for it."""
    doc = parse_hklm_xml(ORDINANCE_XML)
    assert doc.title == "Personal Data (Privacy) Ordinance"
    assert doc.metadata["number"] == "486" and doc.metadata["kind"] == "cap"
    assert doc.decision_date.isoformat() == "2022-10-01"


def test_hklm_parser_lifts_source_notes_out_of_the_text():
    doc = parse_hklm_xml(ORDINANCE_XML)
    assert "L.N. 130 of 2007" not in (doc.text or "")
    notes = doc.metadata["source_notes"]
    assert notes and notes[0].instruments == ("L.N. 130 of 2007",)


def test_hklm_parser_links_corpus_refs_and_ignores_non_corpus_ones():
    """Cap references are corpus nodes; Legal Notices and Ordinances-of-year are not,
    so they must not be minted as ids that can never resolve."""
    doc = parse_hklm_xml(ORDINANCE_XML)
    targets = {(r.dst_id, r.dst_anchor) for r in doc.relations}
    assert ("hk/cap/66", None) in targets
    assert ("hk/cap/571", "s. 5") in targets
    assert all(r.dst_id is not None for r in doc.relations)


def test_hk_adapter_reads_version_from_the_filename_and_is_incremental(hk_drop):
    adapter = HKLegislationAdapter(path=hk_drop)
    stubs = {s.stable_id: s for s in adapter.discover(None)}
    assert set(stubs) == {"hk/cap/486", "hk/cap/1"}
    assert stubs["hk/cap/486"].hint_date.isoformat() == "2022-10-01"
    # the filename timestamp is the only change signal the drop provides
    later = {s.stable_id for s in HKLegislationAdapter(path=hk_drop).discover("2024-01-01")}
    assert later == {"hk/cap/1"}


def test_hk_adapter_records_amending_instruments_without_fabricating_ids(hk_drop):
    adapter = HKLegislationAdapter(path=hk_drop)
    stub = next(s for s in adapter.discover(None) if s.stable_id == "hk/cap/486")
    record = adapter.fetch(stub)
    assert record.title == "Personal Data (Privacy) Ordinance"
    assert record.extra["is_authoritative"] is True
    amended = [r for r in record.relations
               if r.relationship_type is RelationshipType.AMENDED_BY]
    assert amended and amended[0].raw_citation_string == "L.N. 130 of 2007"
    assert amended[0].dst_id is None   # the corpus doesn't hold Legal Notices


def test_hk_scan_reads_chapter_key_from_the_directory_name(hk_drop):
    files = {f.stable_id: f for f in scan_bulk_dir(hk_drop)}
    assert files["hk/cap/486"].number == "486"


def test_sitemap_caps_extracts_the_upstream_chapter_list():
    xml = (b"<urlset><url><loc>https://www.elegislation.gov.hk/hk/cap1</loc></url>"
           b"<url><loc>https://www.elegislation.gov.hk/hk/cap132CI</loc></url></urlset>")
    assert sitemap_caps(xml) == {"1", "132ci"}


# -- New Zealand -------------------------------------------------------------

def test_parse_work_id_reads_the_six_segment_pco_grammar():
    work = parse_work_id("act_public_1990_109")
    assert work.stable_id == "nz/act/public/1990/109"
    version = parse_work_id("act_public_1990_109_en_2022-08-30")
    assert version.version_date == "2022-08-30" and version.stable_id == work.stable_id
    assert parse_work_id("secondary-legislation_pco-drafted_1982_221") is not None


def test_parse_work_id_flags_ephemeral_segments():
    """PCO prefixes unstable segments with "~" — those ids can change upstream, so they
    must never be presented as settled."""
    assert parse_work_id("act_public_~2024_~5").ephemeral is True
    assert parse_work_id("act_public_1990_109").ephemeral is False


def test_nz_id_matches_the_register_grammar():
    assert nz_id("act", "public", 1990, "109") == "nz/act/public/1990/109"


def test_nz_adapter_yields_nothing_without_a_key_instead_of_scraping():
    """The NZ website is bot-walled; the absence of a key must degrade to "no results",
    never to an HTML fallback."""
    adapter = NZLegislationAdapter(api_key=None)
    assert adapter.configured is False
    assert list(adapter.discover(None)) == []
    assert adapter.fetch(object()) is None


def test_nz_adapter_pages_works_and_builds_stubs_with_format_urls():
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def get(self, url, params=None, headers=None, **kwargs):
            calls.append((url, params or {}))
            page = (params or {}).get("page", 1)
            body = {"total": 2, "page": page, "per_page": 1, "results": [{
                "work_id": "act_public_1990_109",
                "legislation_type": "act", "act_status": "in_force",
                "administering_agencies": ["Ministry of Justice"],
                "latest_matching_version": {
                    "title": "New Zealand Bill of Rights Act 1990",
                    "version_id": "act_public_1990_109_en_2022-08-30",
                    "formats": [{"type": "xml", "url": "https://example/x.xml"}]},
            }] if page <= 2 else []}
            return type("R", (), {"content": json.dumps(body).encode(),
                                  "status_code": 200, "headers": {}})()

    adapter = NZLegislationAdapter(api_key="k", client=FakeClient(), per_page=1)
    stubs = list(adapter.discover(None, max_pages=2))
    assert stubs[0].stable_id == "nz/act/public/1990/109"
    assert stubs[0].hints["formats"]["xml"] == "https://example/x.xml"
    assert stubs[0].hint_date.isoformat() == "2022-08-30"
    # the key travels in a header, never in a logged URL
    assert all("api_key" not in params for _, params in calls)


def test_nz_adapter_fetches_xml_and_records_the_point_in_time_edge():
    class FakeClient:
        def get(self, url, params=None, headers=None, **kwargs):
            assert headers["X-Api-Key"] == "k"
            return type("R", (), {"content": NZ_XML,
                                  "status_code": 200, "headers": {}})()

    adapter = NZLegislationAdapter(api_key="k", client=FakeClient())
    stub = type("S", (), {
        "stable_id": "nz/act/public/1990/109", "title": None, "raw_url": None,
        "landing_url": "https://www.legislation.govt.nz/act/public/1990/109/latest/",
        "hints": {"work_id": "act_public_1990_109",
                  "version_id": "act_public_1990_109_en_2022-08-30",
                  "formats": {"xml": "https://example/x.xml"}, "agencies": []}})()
    record = adapter.fetch(stub)
    assert record.doc_type is DocType.LEGISLATION
    assert record.extra["point_in_time"] == "2022-08-30"
    pit = [r for r in record.relations
           if r.relationship_type is RelationshipType.POINT_IN_TIME_OF]
    assert pit and pit[0].dst_id == "nz/act/public/1990/109"


def test_nz_parser_infers_structure_by_shape():
    """The PCO schema is unverified, so structure is inferred from numbering/heading
    children rather than hard-coded element names."""
    doc = parse_nz_pco_xml(NZ_XML)
    assert doc.title == "New Zealand Bill of Rights Act 1990"
    assert doc.metadata["inferred_structure"] is True
    labels = [s.label for s in doc.segments]
    assert any(l.startswith("s. 3") for l in labels)
    assert "impliedly repealed" in doc.text


def test_nz_parser_never_loses_content_on_an_unknown_schema():
    """Losing structure is recoverable by re-parsing; losing the document is not."""
    doc = parse_nz_pco_xml(b"<mystery><blob>operative text here</blob></mystery>")
    assert "operative text here" in (doc.text or "")


def test_nz_parser_expands_internal_dtd_entities():
    """The PCO schema is DTD-defined and ElementTree hard-fails on the first undefined
    entity — one entity would otherwise cost the whole document."""
    xml = (b'<?xml version="1.0"?><!DOCTYPE act [<!ENTITY yr "1990">]>'
           b"<act><cover><title>Bill of Rights &yr;</title></cover></act>")
    assert "1990" in (parse_nz_pco_xml(xml).title or "")


# A miniature file in the *verified* PCO shape (checked against the live Income Tax Act
# 2007 on 2026-07-20): a provision whose operative text is wrapped in subprov/label-para,
# trailed by the editorial apparatus that makes up ~35% of a real act.
NZ_XML_WITH_APPARATUS = b"""<?xml version="1.0" encoding="utf-8"?>
<act id="DLM224791" act.no="109" act.type="public" date.as.at="2022-08-30"
     date.assent="1990-08-28">
  <cover><title>New Zealand Bill of Rights Act 1990</title></cover>
  <contents><row><entry>3 Application</entry></row></contents>
  <body>
    <prov id="DLM224799"><label>3</label><heading>Application</heading>
      <prov.body>
        <subprov><label>1</label><para><text>This Bill of Rights applies to acts done by\xe2\x80\x94</text>
          <label-para><label>a</label><para><text>the legislative branch:</text></para></label-para>
          <label-para><label>b</label><para><text>the judicial branch.</text></para></label-para>
        </para></subprov>
      </prov.body>
      <cf><citation jurisdiction="nz">1688 No 2 s 1</citation></cf>
      <ird.aids><term.list><term href="DLM1">branch</term></term.list></ird.aids>
      <notes><history>
        <history-note id="DLM9"><amended-provision>Section 3(1)(b)</amended-provision>:
          <amending-operation>replaced</amending-operation>, on
          <amendment-date>1 April 2008</amendment-date>, by
          <amending-provision href="DLM1172356">section 307</amending-provision> of the
          <amending-leg>Statutes Amendment Act 2007</amending-leg> (2007 No 109)</history-note>
      </history></notes>
    </prov>
  </body>
  <end><skeletons><act><cover><title>Statutes Amendment Act 2007</title></cover></act></skeletons></end>
</act>"""


def test_nz_parser_keeps_editorial_apparatus_out_of_the_operative_text():
    """~35% of a real PCO act is annotation about the law rather than the law. Ingesting
    it strands amendment notes and IRD index terms mid-provision, which pollutes both
    retrieval snippets and embeddings."""
    doc = parse_nz_pco_xml(NZ_XML_WITH_APPARATUS)
    text = doc.text or ""
    assert "This Bill of Rights applies to acts done by" in text
    # amendment annotation, comparative reference, IRD index term, table of contents,
    # and the text of the *amending* act — none of them are this act's operative text
    assert "Statutes Amendment Act 2007" not in text
    assert "1688 No 2" not in text
    assert "branch</term>" not in text and "\nbranch" not in text
    assert text.count("Application") <= 1


def test_nz_parser_recovers_amendment_notes_as_edges_with_constructible_targets():
    """The notes are pruned from the body but not discarded: the trailing "(2007 No 109)"
    maps onto the PCO work-id grammar, so each note becomes an AMENDED_BY edge whose
    dst_id is a real candidate the resolver activates once that act is harvested."""
    doc = parse_nz_pco_xml(NZ_XML_WITH_APPARATUS)
    amendments = [r for r in doc.relations
                  if r.relationship_type is RelationshipType.AMENDED_BY]
    assert len(amendments) == 1
    edge = amendments[0]
    assert edge.dst_id == "nz/act/public/2007/109"
    assert edge.src_anchor == "Section 3(1)(b)"      # which provision changed
    assert edge.dst_anchor == "section 307"          # what changed it
    assert "replaced" in (edge.raw_citation_string or "")
    assert doc.metadata["amendment_notes"] == 1
    assert doc.metadata["dlm_id"] == "DLM224791"


def test_nz_parser_reads_enumerated_provisions_as_lines_not_stranded_labels():
    """subprov/label-para are the enumerated units; `para` merely wraps text. Treating
    `para` as a line break too would strand every number on a line of its own."""
    text = parse_nz_pco_xml(NZ_XML_WITH_APPARATUS).text or ""
    assert "a the legislative branch:" in text
    assert "b the judicial branch." in text


def test_nz_adapter_normalises_the_hyphenated_legislation_type():
    """Work ids hyphenate (`secondary-legislation_pco-drafted_2026_209`) but the API's
    filter enum underscores. The UI suggests the hyphenated spelling, which would
    otherwise silently return an empty result set."""
    adapter = NZLegislationAdapter(api_key="k", legislation_type="secondary-legislation")
    assert adapter.legislation_type == "secondary_legislation"


def test_nz_adapter_raises_rather_than_truncating_when_the_daily_quota_is_spent():
    """The catalogue is ~38k works against a 10,000/day key, so a backfill spans days.
    Swallowing exhaustion would end discovery early and look like a clean finish —
    writing a watermark over a half-built corpus."""
    class SpentClient:
        def get(self, url, params=None, headers=None, **kwargs):
            return type("R", (), {"content": b"{}", "status_code": 200,
                                  "headers": {"x-ratelimit-remaining": "0"}})()

    adapter = NZLegislationAdapter(api_key="k", client=SpentClient())
    with pytest.raises(RateLimitException):
        list(adapter.discover(None))
