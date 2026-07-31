"""BEREC's document register and the Commission's DMA policy surfaces."""

from __future__ import annotations

from raglex.adapters.berec import (
    category_paths,
    doc_type_for,
    document_alias,
    document_rows,
    last_page,
    parse_document,
)
from raglex.adapters.eu_dma_policy import (
    consultation_links,
    document_cards,
    rss_links,
)
from raglex.adapters.registry import ADAPTERS, SOURCE_INFO, get_adapter

# ---------------------------------------------------------------------------
# BEREC
# ---------------------------------------------------------------------------

CATEGORY_TABLE = b"""
<nav class="menu">
  <a href="/en/all-documents/berec">BEREC</a>
  <a href="/en/all-documents/berec/opinions">Opinions</a>
  <a href="/en/all-documents/berec/regulatory-best-practices/guidelines">Guidelines</a>
  <a href="/en/news/latest-news">Not a category</a>
</nav>
<table><tbody>
  <tr>
    <td class="views-field views-field-field-document-number">BoR (26) 104</td>
    <td class="views-field views-field-field-document-date">
      <time datetime="2026-06-23T12:00:00Z">23 June 2026</time></td>
    <td class="views-field views-field-name">
      <a href="/en/all-documents/berec/opinions/berecs-position-on-general-spectrum">
      BEREC's Position on the General Spectrum issues of the DNA</a></td>
    <td class="views-field views-field-field-author-of-the-document">BEREC</td>
  </tr>
  <tr>
    <td class="views-field views-field-field-document-number"></td>
    <td class="views-field views-field-field-document-date">
      <time datetime="2025-05-08T12:00:00Z">08 May 2025</time></td>
    <td class="views-field views-field-name">
      <a href="/en/all-documents/berec/number-ranges-update-8-may-2025-pdf">
      Number Ranges update</a></td>
    <td class="views-field views-field-field-author-of-the-document">BEREC</td>
  </tr>
</tbody></table>
<nav><a href="?page=0">1</a><a href="?page=1">2</a><a href="?page=11">Last</a></nav>"""


def test_categories_come_from_the_menu_and_documents_from_the_table():
    """A category and a document have the same URL shape —
    ``/en/all-documents/berec/opinions`` versus
    ``/en/all-documents/berec/number-ranges-update-8-may-2025-pdf`` — so nothing about
    the path distinguishes them. Each must be read from where only it appears."""
    assert category_paths(CATEGORY_TABLE) == [
        "/en/all-documents/berec",
        "/en/all-documents/berec/opinions",
        "/en/all-documents/berec/regulatory-best-practices/guidelines",
    ]
    # the document link in the table is NOT mistaken for a category
    assert not any("number-ranges" in path for path in category_paths(CATEGORY_TABLE))


def test_document_row_keeps_the_bor_number_and_the_document_date():
    rows = document_rows(CATEGORY_TABLE)
    assert len(rows) == 2
    assert rows[0]["number"] == "BoR (26) 104"
    assert rows[0]["date"].isoformat() == "2026-06-23"
    assert rows[0]["author"] == "BEREC"
    assert rows[0]["url"].endswith("/opinions/berecs-position-on-general-spectrum")
    assert rows[1]["number"] is None or rows[1]["number"] == ""


def test_the_pager_bounds_the_crawl():
    assert last_page(CATEGORY_TABLE) == 11
    assert last_page(b"<div></div>") is None


def test_category_decides_the_document_type_longest_match_first():
    assert doc_type_for("berec/opinions") == "opinion"
    # the child category wins over its parent's default
    assert doc_type_for("berec/regulatory-best-practices") == "guidance"
    assert doc_type_for("berec/regulatory-best-practices/guidelines") == "guidance"
    assert doc_type_for("berec/reports") == "preparatory"
    assert doc_type_for("berec/berec-decisions") == "decision"
    # agendas and procurement notices are not authority
    assert doc_type_for("berec-office/management-board-meetings/agendas") == "note"


def test_document_page_metadata_and_attachments():
    html = b"""<div class="document-container">
      <h1 class="node-title">BEREC's Position on the General Spectrum issues</h1>
      <div class="field-body"><p>On 21 January 2026, the Commission published...</p></div>
      <div class="doc-info">
        <a href="/system/files/2026-06/BoR%20104_Spectrum.pdf">BEREC's Position</a></div>
      <div class="info-content"><span class="info-title">Document number:</span>
        <span class="info-details">BoR (26) 104</span></div>
      <div class="info-content"><span class="info-title">Document date:</span>
        <span class="info-details">23 June 2026</span></div>
      <div class="info-content"><span class="info-title">Document type:</span>
        <span class="info-details">Opinions</span></div>
      <a class="download-button" href="/system/files/2026-06/BoR%20104_Spectrum.pdf">Download</a>
    </div>"""
    parsed = parse_document(html, "https://www.berec.europa.eu/en/all-documents/berec/opinions/x")
    assert parsed["title"].startswith("BEREC's Position")
    assert parsed["meta"]["document number"] == "BoR (26) 104"
    assert parsed["meta"]["document type"] == "Opinions"
    assert "21 January 2026" in parsed["summary"]
    # the title link and the download button are the same file, not two
    assert len(parsed["files"]) == 1


def test_bor_number_becomes_an_alias():
    assert document_alias("BoR (26)  88_1") == "BoR (26) 88_1"
    assert document_alias("") is None
    assert document_alias(None) is None


# ---------------------------------------------------------------------------
# DMA
# ---------------------------------------------------------------------------

DMA_CARDS = b"""
<div class="ecl-file ecl-file--thumbnail">
  <ul><li class="ecl-file__detail-meta-item">General publications</li>
      <li class="ecl-file__detail-meta-item">22 May 2026</li></ul>
  <div class="ecl-file__footer"><div class="ecl-file__action">
    <a class="ecl-file__download" data-untranslated-label="DMA Annual Report_2025"
       href="/document/download/2f31180c-b9da-49d4-ae36-3a4109cc59a5_en?filename=x.pdf">
       Download</a></div></div>
</div>
<div class="ecl-file">
  <ul><li class="ecl-file__detail-meta-item">General publications</li>
      <li class="ecl-file__detail-meta-item">7 March 2024</li></ul>
  <a href="/document/2f31180c-b9da-49d4-ae36-3a4109cc59a5_en">landing for the same doc</a>
  <a class="ecl-file__download" data-untranslated-label="DMA Annual Report 2023"
     href="/document/download/a7463f7c-02b4-4588-bc03-9e65c11e8086_en?filename=y.pdf">Download</a>
</div>"""


def test_dma_cards_are_keyed_on_the_commission_document_uuid():
    cards = {c["uuid"]: c for c in document_cards(
        DMA_CARDS, page_url="https://digital-markets-act.ec.europa.eu/about-dma/x_en")}
    assert set(cards) == {"2f31180c-b9da-49d4-ae36-3a4109cc59a5",
                          "a7463f7c-02b4-4588-bc03-9e65c11e8086"}
    first = cards["2f31180c-b9da-49d4-ae36-3a4109cc59a5"]
    # the meta list leads with the publication TYPE; the date is the second item
    assert first["date"].isoformat() == "2026-05-22"
    assert first["title"] == "DMA Annual Report_2025"
    # a landing link and a download link for one UUID are one document, download wins
    assert "/document/download/" in first["url"]
    # …and the identity is Commission-wide, so the same document published on another
    # Commission site dedupes onto this id rather than appearing twice
    assert first["landing_url"].endswith("/document/2f31180c-b9da-49d4-ae36-3a4109cc59a5_en")


def test_dma_consultation_index_takes_english_only():
    html = b"""<a href="/consultation-first-review-digital-markets-act_en">First review</a>
      <a href="/consultation-first-review-digital-markets-act_bg">Bulgarian</a>
      <a href="/public-consultations_en">the index itself</a>
      <a href="https://example.org/consultation_en">off-site</a>"""
    links = consultation_links(html)
    # 24 language variants of one page are one document, and the index is not a document
    assert links == ["https://digital-markets-act.ec.europa.eu/"
                     "consultation-first-review-digital-markets-act_en"]


def test_dma_consultations_rss_parses():
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Consultation</title>
        <link>https://digital-markets-act.ec.europa.eu/consultation-x_en</link>
        <pubDate>Thu, 30 Apr 2026 00:15:00 +0200</pubDate></item>
      <item><link></link></item></channel></rss>"""
    rows = rss_links(feed)
    assert len(rows) == 1
    assert rows[0][1].isoformat() == "2026-04-30"
    assert rss_links(b"not xml") == []


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def test_new_eu_sources_are_registered():
    for key in ("eu-berec", "dma-consultations", "dma-annual-reports"):
        assert key in ADAPTERS
        assert SOURCE_INFO[key].jurisdiction == "EU"
        assert get_adapter(key).source == key
