"""The Parliament's research service: date-windowed scraping, two XML schemas, and the
metadata that is only on the web page. Network-free."""

from __future__ import annotations

from datetime import date

import pytest

from raglex.adapters.eu_ep_thinktank import (
    EPThinkTankAdapter,
    choose_asset,
    month_windows,
    parse_about,
    parse_date,
    parse_results,
    service_of,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, source_catalog
from raglex.core.models import DocType, ExtractedVia, Stub
from raglex.formats import parse

# ------------------------------------------------------------------ fixtures

def _result(doc_id: str, title: str, kind: str, when: str) -> str:
    return f"""
<div class="es_document t-y-block a-i">
  <div class="es_document-header mb-1" data-link-mode="true">
    <h3 class="es_document-title es_title-h3 mb-0">
      <a href="/thinktank/en/document/{doc_id}"><span class="t-item">{title}</span></a>
    </h3>
    <div class="es_document-subtitle small mt-25 mb-0" data-separator="-">
      <span class="es_document-subtitle-documenttype text-primary font-weight-bold">{kind}</span>
      <span class="es_document-subtitle-date">{when}</span>
    </div>
  </div>
</div>"""


RESULTS_PAGE = "<html><body>Showing 10 of 126 results" + "".join([
    _result("EPRS_BRI(2026)789356", "World Health Organization at a crossroads",
            "Briefing", "26-06-2026"),
    # the Fact Sheets carry an N before the number, and the pre-2004 papers use hyphens
    _result("04A_FT(2017)N51055", "Consumer policy: principles and instruments",
            "EU Fact Sheets", "01-06-2026"),
    _result("DG-4-JOIN_ET(1995)165643", "The Gulf Cooperation Council",
            "Study", "01-11-1995"),
]) + "</body></html>"

EMPTY_PAGE = "<html><body>Showing 130 of 126 results</body></html>"


def _panel(heading: str, items: list[tuple[str, str, str]]) -> str:
    links = "".join(
        f'<li><a href="https://www.europarl.europa.eu/thinktank/en/research/'
        f'advanced-search?{facet}={code}"><span class="t-x">{label}</span></a></li>'
        for facet, code, label in items)
    return (f'<div class="es_other-links-item px-0 p-md-2 h-100">'
            f'<h5 class="es_title-h5">{heading}</h5>'
            f'<div class="es_links-list"><ul>{links}</ul></div></div>')


DOCUMENT_PAGE = f"""<html><head><title>WHO | Think Tank | European Parliament</title></head>
<body>
<h1><span class="es_product-name">World Health Organization at a crossroads</span></h1>
<div class="d-md-flex"><div class="mb-1 mb-md-0"><div class="small es_product-subtitle">
  <span class="text-primary"> <strong>Briefing</strong></span>
  <span class="date"> <span class="es_product-published-date">26-06-2026 </span></span>
</div></div></div>
<a href="https://www.europarl.europa.eu/RegData/etudes/BRIE/2026/789356/EPRS_BRI(2026)789356_EN.pdf">PDF</a>
<a href="https://www.europarl.europa.eu/RegData/etudes/BRIE/2026/789356/EPRS_BRI(2026)789356_EN.xml">XML</a>
<div class="es_other-links">
  <h2>About this document</h2>
  {_panel("Publication type", [("publicationTypes", "BRIEFING", "Briefing")])}
  {_panel("Author", [("authors", "226235", "LECLERC GABIJA")])}
  {_panel("Policy area", [("policyAreas", "GLOGOV", "Global Governance"),
                          ("policyAreas", "PUBHEA", "Public Health")])}
  {_panel("Keyword", [("keywords", "3226", "communications"),
                      ("keywords", "001854", "disease prevention")])}
  {_panel("Geographical area", [("geographicalAreas", "EURUNI", "EU Member States")])}
</div></body></html>"""

STUDY_PAGE = """<html><body>
<h1><span class="es_product-name">Cross-border Parental Child Abduction</span></h1>
<div class="small es_product-subtitle"><span class="text-primary"> <strong>Study</strong></span>
<span class="date"> <span class="es_product-published-date">30-01-2015 </span></span></div>
<a href="http://www.europarl.europa.eu/RegData/etudes/STUD/2015/510012/IPOL_STU(2015)510012_EN.pdf">PDF</a>
<div class="row"><div class="col-12">
  <p><strong>External author</strong></p>
  <p>Lukas HECKENDORN URSCHELER; Ilaria PRETELLI and Aladar SEBENI - SICL</p>
</div></div>
</body></html>"""

JATS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<article article-type="briefing" xml:lang="en"><front><article-meta>
  <article-id>PE: 789.356</article-id>
  <article-categories><subj-group subj-group-type="document_category">
    <subject>Briefing</subject></subj-group></article-categories>
  <title-group><article-title>World Health Organization at a crossroads</article-title></title-group>
  <pub-date><day>26</day><month>6</month><year>2026</year></pub-date>
  <permissions><copyright-statement>The reuse of this document is authorised under a
    Creative Commons Attribution 4.0 International (CC-BY 4.0) licence.</copyright-statement></permissions>
  <abstract abstract-type="snippet"><sec><title>Snippet</title><p>Trailer.</p></sec></abstract>
  <abstract abstract-type="summary"><sec><title>Summary</title><p>The WHO has had a central role.</p></sec></abstract>
  <kwd-group kwd-group-type="eurovoc"><kwd>placeholder</kwd></kwd-group>
  <kwd-group kwd-group-type="authors"><kwd>Gabija LECLERC</kwd></kwd-group>
</article-meta></front>
<body>
  <sec><title>Key WHO challenges</title><p>Chronic underfunding<ext-link xmlns:xlink="http://www.w3.org/1999/xlink"
      xlink:href="https://eur-lex.europa.eu/eli/C/2024/4003/oj/eng">as reported</ext-link>.</p>
    <sec><title>Funding</title><p>Stagnant financing.</p>
      <sec><title>Financing gap</title><p>A widening gap.</p></sec></sec></sec>
  <sec><title>EU and WHO</title><p>A long partnership<ext-link xmlns:xlink="http://www.w3.org/1999/xlink"
      xlink:href="https://example.org/report">elsewhere</ext-link>.</p></sec>
</body>
<back><fn-group><title>Endnotes</title>
  <fn id="fn1"><label>1.</label><p>Parliament's resolution <ext-link xmlns:xlink="http://www.w3.org/1999/xlink"
     xlink:href="https://www.europarl.europa.eu/doceo/document/TA-10-2025-0159_EN.html">of 2025</ext-link>.</p></fn>
</fn-group></back></article>"""

FTU_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<FTU-DOCUMENT AUTHOR="Fabiola VALENTINI" DATE="04/2026" SHEET-ID="2.2.1." LANGUAGE="en">
<SEO-DESCRIPTION>Read about EU consumer policy.</SEO-DESCRIPTION>
<FTU-HEADER>Consumer policy: principles and instruments</FTU-HEADER>
<FTU-SUMMARY>This fact sheet outlines the key principles.</FTU-SUMMARY>
<FTU-H1>Legal basis</FTU-H1>
<FTU-P>TFEU: Articles 4(2)(f), 12, 114 and 169.</FTU-P>
<FTU-H1>Objectives</FTU-H1>
<FTU-P>Protect consumers.</FTU-P>
<FTU-H2>Consumer groups</FTU-H2>
<FTU-P>Representation matters.</FTU-P>
</FTU-DOCUMENT>"""


# ------------------------------------------------------------------ discovery

def test_every_id_shape_thirty_years_of_them_is_read_from_a_results_page():
    rows = parse_results(RESULTS_PAGE)
    assert [r["doc_id"] for r in rows] == [
        "EPRS_BRI(2026)789356", "04A_FT(2017)N51055", "DG-4-JOIN_ET(1995)165643"]
    assert rows[0]["publication_type"] == "Briefing"
    assert rows[0]["date"] == "26-06-2026"
    assert rows[2]["title"] == "The Gulf Cooperation Council"


def test_windows_run_newest_first_and_stop_at_the_archive_floor():
    got = list(month_windows(date(2026, 5, 15), date(2026, 7, 3)))
    assert got == [(date(2026, 7, 1), date(2026, 7, 3)),
                   (date(2026, 6, 1), date(2026, 6, 30)),
                   (date(2026, 5, 15), date(2026, 5, 31))]


def test_an_empty_page_ends_the_window_and_the_result_count_is_ignored(monkeypatch):
    """The count on the page over-reports by a margin that varies with the window — 126
    against 116 reachable for one June window, 17 against 10 for the whole of 1995 —
    and splitting the window recovers nothing. The pages are the truth: ten per page,
    disjoint, and an empty one is the end. So no ``feed_total`` is published; a total
    wrong by an unknown margin is worse than none (job-authoring.md)."""
    ad = EPThinkTankAdapter(years="2026-2026")
    pages: list[str] = [RESULTS_PAGE, EMPTY_PAGE] + [EMPTY_PAGE] * 40
    monkeypatch.setattr(ad, "_page", lambda s, e, p: pages[p - 1] if p <= len(pages) else "")
    stubs = list(ad.discover(None, max_pages=1))
    assert len(stubs) == 3
    assert all("feed_total" not in s.hints for s in stubs)
    assert stubs[0].hints["window"].endswith(date.today().isoformat())


def test_an_incremental_run_drops_results_older_than_its_cursor(monkeypatch):
    ad = EPThinkTankAdapter(years="2026-2026")
    monkeypatch.setattr(ad, "_page",
                        lambda s, e, p: RESULTS_PAGE if p == 1 else EMPTY_PAGE)
    stubs = list(ad.discover("2026-06-01", max_pages=1))
    assert [s.hints["doc_id"] for s in stubs] == [
        "EPRS_BRI(2026)789356", "04A_FT(2017)N51055"]     # the 1995 paper is behind us


def test_a_window_entirely_behind_the_cursor_ends_discovery(monkeypatch):
    """Windows are newest-first, so the first one that ends before the cursor means
    every remaining window does too — that is what makes keep-current cheap."""
    ad = EPThinkTankAdapter(years="2020-2026")
    calls: list[int] = []

    def page(start, end, p):
        calls.append(p)
        return EMPTY_PAGE

    monkeypatch.setattr(ad, "_page", page)
    assert list(ad.discover(date.today().isoformat())) == []
    assert len(calls) <= 1


# ------------------------------------------------------------------ metadata

def test_the_web_page_supplies_the_subject_metadata_the_xml_does_not():
    about = parse_about(DOCUMENT_PAGE)
    assert [v["label"] for v in about["Policy area"]] == ["Global Governance", "Public Health"]
    # the facet CODE is kept beside the label — it is the token the Think Tank filters by
    assert [v["code"] for v in about["Keyword"]] == ["3226", "001854"]
    # the last panel used to be dropped by the block regex, costing every document its
    # geographical areas
    assert [v["label"] for v in about["Geographical area"]] == ["EU Member States"]


def test_a_commissioned_studys_outside_authors_are_read_from_the_prose():
    about = parse_about(STUDY_PAGE)
    assert [v["label"] for v in about["External author"]] == [
        "Lukas HECKENDORN URSCHELER", "Ilaria PRETELLI", "Aladar SEBENI - SICL"]


def test_the_id_prefix_names_the_service_that_wrote_it():
    assert service_of("EPRS_BRI(2026)789356")[1] == "European Parliamentary Research Service"
    assert service_of("04A_FT(2017)N51055")[1] == "Fact Sheets on the European Union"
    assert service_of("DG-4-JOIN_ET(1995)165643")[1].startswith("Directorate-General for Research")


def test_both_asset_naming_schemes_are_understood():
    briefing = ["https://x/EPRS_BRI(2026)789356_EN.pdf", "https://x/EPRS_BRI(2026)789356_FR.pdf"]
    assert choose_asset(briefing, ".pdf") == briefing[0]
    factsheet = ["https://x/doc_fr.xml", "https://x/doc_en.xml"]
    assert choose_asset(factsheet, ".xml") == factsheet[1]
    assert choose_asset(["https://x/doc_fr.xml", "https://x/doc_de.xml"], ".xml") is None


def test_listing_dates_parse_in_both_printed_forms():
    assert parse_date("26-06-2026") == date(2026, 6, 26)
    assert parse_date("30/09/1989") == date(1989, 9, 30)
    assert parse_date("not a date") is None


# ------------------------------------------------------------------ the two XML schemas

def test_jats_keeps_the_section_hierarchy_the_pdf_flattens():
    doc = parse("jats-article", JATS_XML)
    assert doc.title == "World Health Organization at a crossroads"
    assert doc.decision_date == date(2026, 6, 26)
    assert doc.metadata["pe_number"] == "PE 789.356"
    labels = [(s.label, s.level, s.kind) for s in doc.segments]
    assert labels[0] == ("Summary", 0, "abstract")
    assert ("Key WHO challenges", 0, "section") in labels
    assert ("Funding", 1, "section") in labels
    assert ("Financing gap", 2, "section") in labels
    assert labels[-1] == ("Endnotes", 0, "note")
    # only the substantive abstract becomes a segment; the trailer is not one
    assert "Trailer." not in doc.text
    # the placeholder EuroVoc group is dropped rather than stored as a subject
    assert "eurovoc" not in doc.metadata["keywords"]


def test_a_link_to_a_legal_source_is_part_of_the_citation():
    """These documents cite in a web style — the identifier lives in the href, not the
    prose. Dropping it would lose a reference the grammars can resolve; keeping every
    href would bury the text in URLs."""
    doc = parse("jats-article", JATS_XML)
    assert "(https://eur-lex.europa.eu/eli/C/2024/4003/oj/eng)" in doc.text
    assert "(https://www.europarl.europa.eu/doceo/document/TA-10-2025-0159_EN.html)" in doc.text
    assert "example.org/report" not in doc.text          # an ordinary web reference
    # …and no space is left in front of the punctuation the link interrupted — these
    # documents link mid-sentence constantly, so " ." would be in almost every one
    assert "oj/eng)." in doc.text
    assert " ." not in doc.text and " ," not in doc.text


def test_the_fact_sheets_have_their_own_dtd_and_a_legal_basis_section():
    doc = parse("ep-factsheet", FTU_XML)
    assert doc.title == "Consumer policy: principles and instruments"
    # a sheet carries a month, so it is dated to the end of it
    assert doc.decision_date == date(2026, 4, 30)
    assert doc.metadata["sheet_id"] == "2.2.1."
    labels = [(s.label, s.level) for s in doc.segments]
    assert labels == [("Summary", 0), ("Legal basis", 0), ("Objectives", 0),
                      ("Consumer groups", 1)]
    assert "Articles 4(2)(f), 12, 114 and 169" in doc.text
    assert "Read about EU consumer policy" not in doc.text      # the SEO blurb


# ------------------------------------------------------------------ fetch

class _Resp:
    def __init__(self, content: bytes, status: int = 200):
        self.content, self.status_code = content, status


def _wire(ad, monkeypatch, pages: dict[str, bytes]):
    def get(url, **kw):
        for key, payload in pages.items():
            if key in url:
                return _Resp(payload)
        return _Resp(b"", 404)

    monkeypatch.setattr(ad._client, "get", get)


def test_fetch_prefers_the_xml_and_labels_the_record_from_the_page(monkeypatch):
    ad = EPThinkTankAdapter(document_ids="EPRS_BRI(2026)789356")
    _wire(ad, monkeypatch, {"/thinktank/en/document/": DOCUMENT_PAGE.encode(),
                            "_EN.xml": JATS_XML})
    rec = ad.fetch(next(ad.discover(None)))
    assert rec.doc_type is DocType.COMMENTARY
    assert rec.extracted_via is ExtractedVia.STRUCTURED
    assert rec.extra["format"] == "jats-article"
    # the header on the page is authoritative — a targeted fetch has no listing row
    assert rec.title == "World Health Organization at a crossroads"
    assert rec.decision_date == date(2026, 6, 26)
    assert rec.extra["publication_type"] == "Briefing"
    assert rec.extra["service"] == "European Parliamentary Research Service"
    assert rec.extra["authors"] == ["LECLERC GABIJA"]
    assert rec.extra["policy_areas"] == ["Global Governance", "Public Health"]
    assert rec.extra["keyword_codes"] == ["3226", "001854"]
    assert rec.extra["geographical_areas"] == ["EU Member States"]
    assert rec.extra["pe_number"] == "PE 789.356"
    assert rec.extra["licence"] == "CC-BY-4.0"
    assert rec.topic_tags == ["european-parliament", "ep-research", "eprs", "briefing"]
    assert [s.label for s in rec.segments][:2] == ["Summary", "Key WHO challenges"]


def test_fetch_falls_back_to_the_pdf_and_keeps_the_page_metadata(monkeypatch):
    ad = EPThinkTankAdapter(document_ids="IPOL_STU(2015)510012")
    _wire(ad, monkeypatch, {"/thinktank/en/document/": STUDY_PAGE.encode(),
                            "_EN.pdf": b"%PDF-1.4 fake"})
    monkeypatch.setattr("raglex.extraction.extract_bytes",
                        lambda *a, **k: type("E", (), {"text": "The study says."})())
    rec = ad.fetch(next(ad.discover(None)))
    assert rec.extra["format"] == "pdf" and rec.text == "The study says."
    assert rec.decision_date == date(2015, 1, 30)
    assert rec.extra["authors"][0] == "Lukas HECKENDORN URSCHELER"
    assert rec.extra["service_code"] == "IPOL"


def test_a_scanned_paper_with_no_text_layer_asks_for_ocr_rather_than_vanishing(monkeypatch):
    ad = EPThinkTankAdapter(document_ids="DG-4-JOIN_ET(1995)165643")
    page = STUDY_PAGE.replace("IPOL_STU(2015)510012", "DG-4-JOIN_ET(1995)165643")
    _wire(ad, monkeypatch, {"/thinktank/en/document/": page.encode(),
                            "_EN.pdf": b"%PDF-1.2 scanned"})
    monkeypatch.setattr("raglex.extraction.extract_bytes",
                        lambda *a, **k: type("E", (), {"text": "   "})())
    rec = ad.fetch(next(ad.discover(None)))
    assert rec.extra["metadata_only"] is True and rec.extra["needs_ocr"] is True
    assert rec.title == "Cross-border Parental Child Abduction"   # still a real node


def test_a_page_that_cannot_be_read_is_dropped_rather_than_stored_empty(monkeypatch):
    ad = EPThinkTankAdapter(document_ids="EPRS_BRI(2026)000000")
    _wire(ad, monkeypatch, {})
    assert ad.fetch(next(ad.discover(None))) is None


# ------------------------------------------------------------------ registry

def test_the_source_is_in_the_catalogue_with_a_truthful_incremental_mode():
    row = next(r for r in source_catalog() if r["key"] == "eu-ep-thinktank")
    assert row["jurisdiction"] == "EU" and row["kind"] == "guidance"
    assert INCREMENTAL_MODE["eu-ep-thinktank"] == "early-stop"


@pytest.mark.parametrize("option", ["years", "window_days", "document_ids", "language"])
def test_every_declared_option_is_accepted_by_the_constructor(option):
    assert ADAPTERS["eu-ep-thinktank"](**{option: "1"}) is not None
