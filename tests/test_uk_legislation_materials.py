from __future__ import annotations

from dataclasses import dataclass

from raglex.adapters.registry import get_adapter, source_catalog
from raglex.adapters.uk_legislation_materials import (
    UKLegislationMaterialsAdapter,
    combine_notes_html,
    impact_stubs_for_legislation,
    parse_explanatory_notes_xml,
    parse_impact_feed,
    parse_impact_metadata,
    _parent_id,
    _impact_title,
)
from raglex.citations.extractor import extract_citations
from raglex.citations.taxonomy import classify_document
from raglex.core.models import DocType, RelationshipType, Stub
from raglex.static_export import _public_links


OLD_NOTES = b'''<EN xmlns="http://www.legislation.gov.uk/namespaces/legislation"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <dc:title>Explanatory Notes to Example Act 2000</dc:title>
 <ExplanatoryNotes><Body><Division><Title>Introduction</Title>
 <NumberedPara><Pnumber>1</Pnumber><Para><Text>These notes relate to the Example Act.</Text></Para></NumberedPara>
 <CommentaryP1><Title>Section 4: Duty</Title>
 <NumberedPara><Pnumber>12</Pnumber><Para><Text>Section 4 requires action.</Text></Para></NumberedPara>
 </CommentaryP1></Division></Body></ExplanatoryNotes></EN>'''

MODERN_PAGE = b'''<html><head><meta name="Title" content="Example Act 2018"/></head><body>
 <div class="en-content"><article><h2>Policy background</h2>
 <ol start="3"><li>The Act creates a duty under section 4.</li><li>It applies broadly.</li></ol>
 <h4>Detail</h4><ol start="5"><li>Section 5 is explained.<ul><li>supporting bullet</li></ul></li></ol>
 </article></div></body></html>'''

IMPACT_XML = b'''<ImpactAssessment xmlns="http://www.legislation.gov.uk/namespaces/legislation"
 xmlns:ukm="http://www.legislation.gov.uk/namespaces/metadata"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dct="http://purl.org/dc/terms/"
 xmlns:atom="http://www.w3.org/2005/Atom" IdURI="http://www.legislation.gov.uk/id/ukia/2016/251">
 <ukm:Metadata><dc:title>Investigatory powers: overarching assessment</dc:title>
 <dc:modified>2017-08-04</dc:modified><dct:valid>2016-07-07</dct:valid>
 <atom:link rel="alternate" type="application/pdf" href="http://www.legislation.gov.uk/a.pdf"/>
 <atom:link rel="nav" href="http://www.legislation.gov.uk/ukpga/2016/25/impacts/2017/125" title="Bulk data"/>
 <ukm:ImpactAssessmentMetadata><ukm:DocumentStage Value="Final"/><ukm:Department Value="Home Office"/></ukm:ImpactAssessmentMetadata>
 <ukm:Legislation URI="http://www.legislation.gov.uk/id/ukpga/2016/25"/>
 </ukm:Metadata></ImpactAssessment>'''

IMPACT_FEED = b'''<feed xmlns="http://www.w3.org/2005/Atom"
 xmlns:leg="http://www.legislation.gov.uk/namespaces/legislation"
 xmlns:ukm="http://www.legislation.gov.uk/namespaces/metadata"><leg:page>1</leg:page><leg:morePages>2</leg:morePages>
 <entry><id>http://www.legislation.gov.uk/id/ukia/2026/143</id><title> Digital rules </title>
 <published>2026-03-13T23:59:59Z</published>
 <link rel="alternate" href="http://www.legislation.gov.uk/uksi/2026/858/impacts/2026/143"/>
 <link rel="alternate" type="application/pdf" href="http://www.legislation.gov.uk/i.pdf"/>
 <ukm:DocumentStage Value="Final"/><ukm:Department Value="DSIT"/></entry></feed>'''


def test_old_explanatory_notes_keep_native_paragraph_pinpoints():
    title, text, segments = parse_explanatory_notes_xml(OLD_NOTES)
    assert title == "Explanatory Notes to Example Act 2000"
    assert "12. Section 4 requires action." in text
    assert [s.label for s in segments] == ["Introduction", "para. 1", "Section 4: Duty", "para. 12"]
    assert text[segments[-1].char_start:segments[-1].char_end].startswith("12.")


def test_modern_explanatory_notes_keep_numbering_and_do_not_duplicate_nested_lists():
    text, segments = combine_notes_html([MODERN_PAGE, MODERN_PAGE])
    assert [s.label for s in segments] == [
        "Policy background", "para. 3", "para. 4", "Detail", "para. 5"]
    assert text.count("3. The Act creates") == 1
    assert "supporting bullet" in text


def test_impact_feed_and_per_act_metadata_link_both_directions():
    stubs, more = parse_impact_feed(IMPACT_FEED)
    assert more is True
    assert stubs[0].stable_id == "ukia/2026/143"
    assert stubs[0].hints["parent_id"] == "uksi/2026/858"
    assert stubs[0].hints["department"] == "DSIT"

    meta = parse_impact_metadata(IMPACT_XML)
    assert meta["parent_id"] == "ukpga/2016/25"
    assert meta["title"] == "Investigatory powers: overarching assessment"
    assert str(meta["date"]) == "2016-07-07"
    linked = impact_stubs_for_legislation(IMPACT_XML, "ukpga/2016/25")
    assert {s.stable_id for s in linked} == {"ukia/2016/251", "ukia/2017/125"}


def test_parent_identity_supports_both_calendar_year_and_old_regnal_routes():
    assert _parent_id("https://www.legislation.gov.uk/id/ukpga/2018/12") == "ukpga/2018/12"
    assert _parent_id("https://www.legislation.gov.uk/id/ukpga/Geo5/10-11/41") == \
        "ukpga/geo5/10-11/41"
    assert _parent_id("https://www.legislation.gov.uk/ukpga/2018/12/impacts") == \
        "ukpga/2018/12"


def test_impact_titles_are_self_explanatory_in_mixed_search_results():
    assert _impact_title("Communications Data", "ukia/2017/126") == \
        "Impact Assessment: Communications Data"
    assert _impact_title("Overarching Impact Assessment", "ukia/2017/130") == \
        "Overarching Impact Assessment"


@dataclass
class Response:
    content: bytes
    status_code: int = 200
    url: str = "https://www.legislation.gov.uk/ukpga/2000/36/notes/data.xml"
    headers: dict | None = None


class Client:
    def get(self, url, **_kwargs):
        if url.endswith("/notes/data.xml"):
            return Response(OLD_NOTES, url=url)
        return Response(b"not found", status_code=404, url=url)


def test_notes_are_first_class_related_records_with_local_act_grammar():
    adapter = UKLegislationMaterialsAdapter(ids="ukpga/2000/36", impacts=False, client=Client())
    stub = next(adapter.discover(None))
    record = adapter.fetch(stub)
    assert record is not None
    assert record.doc_type == DocType.NOTE
    assert record.title == "Explanatory Notes to Example Act 2000"
    assert record.relations[0].relationship_type == RelationshipType.ANALYSES
    assert record.relations[0].dst_id == "ukpga/2000/36"
    assert record.extra["citation_default_instrument"]["id"] == "ukpga/2000/36"
    assert record.landing_url == "https://www.legislation.gov.uk/ukpga/2000/36/notes"
    assert record.extra["html_url"] == record.landing_url
    assert record.extra["contents_url"].endswith("/notes/contents")

    aliases = record.extra["citation_local_aliases"]
    got = {(c.candidate_id, c.pinpoint) for c in
           extract_citations("Section 5 of the Act and section 6 of this Act.", aliases=aliases)}
    assert got >= {("ukpga/2000/36", "s. 5"), ("ukpga/2000/36", "s. 6")}


def test_registry_and_taxonomy_expose_an_appropriate_material_category():
    assert get_adapter("uk-legislation-materials", ids="ukpga/2018/12", impacts=False).source == \
        "uk-legislation-materials"
    assert any(x["key"] == "uk-legislation-materials" for x in source_catalog())
    note = classify_document(source="uk-legislation-materials",
                             stable_id="ukpga/2018/12/notes", doc_type="note")
    impact = classify_document(source="uk-legislation-materials",
                               stable_id="ukia/2016/251", doc_type="preparatory")
    assert (note.category, note.subtype) == ("uk-legislation-materials", "explanatory-notes")
    assert impact.subtype == "impact-assessments"


def test_static_links_offer_the_human_page_and_pdf_not_ingest_xml():
    class Facade:
        @staticmethod
        def source_label(_source):
            return "UK explanatory material"

    row = {
        "landing_url": "https://www.legislation.gov.uk/uksi/2013/3134/memorandum",
        "source": "uk-legislation-materials",
    }
    links = _public_links(Facade(), row, {
        "download_url": "http://www.legislation.gov.uk/uksi/2013/3134/pdfs/em.pdf",
    })
    urls = [link["url"] for link in links]
    assert row["landing_url"] in urls
    assert any(url.endswith(".pdf") for url in urls)
    assert not any(url.endswith("data.xml") for url in urls)
