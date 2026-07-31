"""Data-protection authority guidance registers: the Irish DPC and the Dutch AP.

Both are two-step sources — a landing page over the operative PDF — and both carry
the authority's own classification of the document, which is the part worth getting
right: it is what lets a reader ask for the AP's *fines* rather than its newsletters,
or the DPC's guidance on transfers rather than its COVID-19 blogs.
"""

from __future__ import annotations

from raglex.adapters.ie_dpc import (
    dpc_article_relations,
    guidance_pdf_date,
    guidance_stable_id,
    parse_dpc_detail,
    parse_dpc_guidance_hub,
    parse_dpc_guidance_page,
    parse_dpc_listing,
)
from raglex.adapters.nl_ap import (
    DOC_TYPES,
    document_stubs,
    facet_tree,
    last_page,
    parse_document,
    parse_dutch_date,
    rss_stubs,
    stable_id,
    type_path,
)
from raglex.adapters.registry import ADAPTERS, source_catalog
from raglex.citations.extractor import all_grammar_citations, extract_citations

# ---------------------------------------------------------------------------
# Irish DPC
# ---------------------------------------------------------------------------

# The register lays its results out two-up: one .views-row holds two decisions. Keying
# on the row returned the first card only, which is why the harvest held 33 of the 63
# published decisions.
TWO_UP_LISTING = b"""
<div class="views-row">
  <div class="views-col col-1"><div class="faq-section-results-box">
    <div class="faq-section-category-link">
      <div class="item-list"><ul><li>
        <a href="/en/dpc-guidance/decisions?decision_tags=93">Bank/Credit/Insurance</a>
      </li></ul></div>
      <span class="datetime">30 Apr 2026</span>
    </div>
    <h3><a href="/en/dpc-guidance/decisions/inquiry-permanent-tsb">Inquiry into PTSB</a></h3>
    <div class="classArticles"><p><span>Article(s):</span>
      <a href="/en/dpc-guidance/decisions?decision_articles=81">5</a>,
      <a href="/en/dpc-guidance/decisions?decision_articles=82">32</a></p></div>
    <a aria-label="Read this case study"
       href="/en/dpc-guidance/decisions/inquiry-permanent-tsb#read-full-decision">More</a>
  </div></div>
  <div class="views-col col-2"><div class="faq-section-results-box">
    <div class="faq-section-category-link">
      <div class="item-list"><ul><li>
        <a href="/en/dpc-guidance/decisions?decision_tags=87">University</a>
      </li></ul></div>
      <span class="datetime">10 Dec 2025</span>
    </div>
    <h3><a href="/en/dpc-guidance/decisions/inquiry-university-limerick">UL inquiry</a></h3>
    <div class="classArticles"><p><span>Article(s):</span>
      <a href="/en/dpc-guidance/decisions?decision_articles=123">30</a></p></div>
  </div></div>
</div>"""


def test_dpc_register_yields_both_decisions_in_a_two_up_row():
    rows = parse_dpc_listing(TWO_UP_LISTING)
    assert [row["url"].rsplit("/", 1)[-1] for row in rows] == [
        "inquiry-permanent-tsb", "inquiry-university-limerick"]
    assert rows[0] == {
        "url": "https://www.dataprotection.ie/en/dpc-guidance/decisions/inquiry-permanent-tsb",
        "title": "Inquiry into PTSB", "date": "30 Apr 2026",
        "sector": "Bank/Credit/Insurance", "articles": ["5", "32"],
    }
    assert rows[1]["articles"] == ["30"]


def test_dpc_detail_keeps_every_operative_pdf_and_both_classifications():
    detail = b"""<div class="field--name-body"><h1>Inquiry into PTSB</h1>
      <div class="block-tags">Area: Bank/Credit/Insurance</div>
      <div class="block-tags">Topic: Data security</div>
      <div class="block-tags">Articles: <a href="#">5</a> <a href="#">S 110</a></div>
      <div class="block-tags">DPC Reference: IN-22-7-3</div>
      <div class="block-tags">Decision Date: 30 April 2026</div>
      <a href="/sites/default/files/uploads/2026-07/decision.pdf">Decision (PDF)</a>
      <a href="/sites/default/files/uploads/2026-07/appeal.pdf">Circuit Court judgment</a>
      <a href="/sites/default/files/uploads/2026-07/decision.pdf">Decision (again)</a>
      </div>"""
    parsed = parse_dpc_detail(detail)
    assert parsed["sector"] == "Bank/Credit/Insurance"
    assert parsed["topic"] == "Data security"
    assert parsed["reference"] == "IN-22-7-3"
    # deduped, order preserved, and ``pdf`` still names the first for compatibility
    assert [p["url"].rsplit("/", 1)[-1] for p in parsed["pdfs"]] == [
        "decision.pdf", "appeal.pdf"]
    assert parsed["pdf"].endswith("/decision.pdf")
    # the S prefix is the register's own marker for an Irish Act section
    kinds = {r.dst_id: r.dst_anchor for r in dpc_article_relations(parsed["articles"])}
    assert kinds == {"32016R0679": "Article 5", "ie/2018/act/7": "section 110"}


GUIDANCE_HUB = b"""
<div class="accordion-item">
  <div class="accordion-header"><button class="accordion-button">General Guidance</button></div>
  <div class="accordion-body">
    <a href="/en/dpc-guidance/anonymisation-and-pseudonymisation">Anonymisation</a>
    <a href="https://dataprotection.ie/en/dpc-guidance/my-childs-data-protection-rights">Children</a>
    <a href="/en/organisations/data-protection-basics">Data Protection Basics</a>
  </div>
</div>
<div class="accordion-item">
  <div class="accordion-header"><button class="accordion-button">Technological issues</button></div>
  <div class="accordion-body">
    <a href="/en/dpc-guidance/anonymisation-and-pseudonymisation">Anonymisation</a>
    <a href="/en/dpc-guidance/guidance-connected-toys-and-devices">Connected toys</a>
  </div>
</div>
<div class="accordion-item">
  <div class="accordion-header"><button class="accordion-button">
    Guidance from the European Data Protection Board</button></div>
  <div class="accordion-body">
    <a href="https://edpb.europa.eu/our-work-tools/our-documents/x_en">Guidelines 1/2021</a>
    <a href="/en/dpc-guidance/edpb-mirror-page">A mirrored EDPB note</a>
  </div>
</div>"""


def test_dpc_guidance_hub_keeps_sections_and_drops_the_edpb_accordion():
    rows = parse_dpc_guidance_hub(GUIDANCE_HUB)
    by_id = {guidance_stable_id(row["url"]): row for row in rows}
    # a page listed under two headings is one document carrying both classifications
    assert by_id["ie/dpc/guidance/anonymisation-and-pseudonymisation"]["sections"] == [
        "General Guidance", "Technological issues"]
    # the apex-host spelling folds onto the canonical www host, and guidance outside
    # /dpc-guidance/ keeps its path so it cannot collide with a same-slug page
    assert "ie/dpc/guidance/my-childs-data-protection-rights" in by_id
    assert "ie/dpc/guidance/organisations/data-protection-basics" in by_id
    # the whole EDPB accordion is skipped — those documents belong to the edpb source —
    # including the item the DPC re-hosts itself
    assert not any("edpb" in key for key in by_id)


def test_dpc_guidance_page_is_two_step_and_dates_from_the_pdf_path():
    page = b"""<div class="field--name-body"><h1>Anonymisation and pseudonymisation</h1>
      <p>Short orientation on the page.</p>
      <a href="/sites/default/files/uploads/2022-04/Anonymisation%20-%20April%202022.pdf">
      Full Guidance Note</a></div>"""
    parsed = parse_dpc_guidance_page(page)
    assert parsed["title"] == "Anonymisation and pseudonymisation"
    assert "Short orientation" in parsed["text"]
    assert parsed["pdfs"][0]["title"] == "Full Guidance Note"
    # the DPC stamps no date field; the upload folder is where the month survives
    assert guidance_pdf_date(parsed["pdfs"][0]["url"]).isoformat() == "2022-04-01"
    assert guidance_pdf_date("https://example.org/guide.pdf") is None


# ---------------------------------------------------------------------------
# Dutch AP
# ---------------------------------------------------------------------------

AP_LISTING = b"""
<div class="view-document-overview-documents__row"><article class="node-publication-card">
  <div class="node-publication-card__content">
    <h3 class="node-publication-card__title"><span>Boete Netflix</span></h3>
    <div class="node-publication-card__submitted">18 december 2024</div>
    <a class="node-publication-card__link" href="/documenten/boete-netflix">Bekijk</a>
    <div class="node-publication-card__files"><div class="node-publication-card__files-item">
      <span data-type="pdf">PDF, 876 kB</span></div></div>
  </div>
</article></div>
<nav class="pager"><ul>
  <li class="pager__item"><a href="?page=1">Pagina 2</a></li>
  <li class="pager__item"><a href="?page=165">Laatste</a></li>
</ul></nav>
<form class="views-exposed-form">
<fieldset name="document_type[]"><ul class="bef-checkboxes__list">
  <li class="bef-checkboxes__list-item bef-checkboxes__group"><details><summary>
    <input name="document_type[4]" value="4"/>
    <label>Besluit <span class="bef-checkboxes__amount">(465)</span></label>
    </summary>
    <ul class="bef-checkboxes__list">
      <li class="bef-checkboxes__list-item bef-checkboxes__group"><details><summary>
        <input name="document_type[32]" value="32"/>
        <label>Sanctie <span class="bef-checkboxes__amount">(49)</span></label>
        </summary>
        <ul class="bef-checkboxes__list"><li class="bef-checkboxes__list-item">
          <input name="document_type[37]" value="37"/>
          <label>Boete <span class="bef-checkboxes__amount">(34)</span></label>
        </li></ul>
      </details></li>
    </ul>
  </details></li>
  <li class="bef-checkboxes__list-item">
    <input name="document_type[6]" value="6"/>
    <label>Wetgevingstoets <span class="bef-checkboxes__amount">(719)</span></label>
  </li>
</ul></fieldset></form>"""


def test_ap_listing_card_carries_date_and_whether_a_pdf_is_attached():
    stubs = document_stubs(AP_LISTING)
    assert len(stubs) == 1
    assert stubs[0].stable_id == "nl/ap/boete-netflix"
    assert stubs[0].title == "Boete Netflix"
    assert stubs[0].hint_date.isoformat() == "2024-12-18"
    assert stubs[0].hints["file_types"] == ["pdf"]
    assert stubs[0].hints["watermark"] == "2024-12-18"
    assert stubs[0].court == "dpa-nl"


def test_ap_backfill_knows_where_the_view_ends():
    assert last_page(AP_LISTING) == 165
    assert last_page(b"<div></div>") is None


def test_ap_document_type_facet_is_read_as_a_tree_deepest_first():
    tree = facet_tree(AP_LISTING)
    assert [(row["label"], row["depth"], row["count"]) for row in tree] == [
        ("Boete", 2, 34), ("Sanctie", 1, 49), ("Besluit", 0, 465),
        ("Wetgevingstoets", 0, 719),
    ]
    # deepest-first is what makes the sweep assign the NARROWEST type: a fine answers
    # the Boete, Sanctie and Besluit filters alike, and Boete must win.
    assert type_path(tree, "37") == ["Besluit", "Sanctie", "Boete"]
    assert type_path(tree, "6") == ["Wetgevingstoets"]
    assert type_path(tree, None) == []


def test_ap_document_types_map_to_the_kind_of_document_they_are():
    assert DOC_TYPES["boete"] == "decision"
    assert DOC_TYPES["woo-besluit"] == "decision"
    assert DOC_TYPES["wetgevingstoets"] == "opinion"   # advice on a draft law
    assert DOC_TYPES["jaarverslag"] == "preparatory"   # a report, not authority
    assert DOC_TYPES["speech"] == "note"
    # the long tail (handreiking, infographic, voorbeeldbrief…) falls back to guidance
    assert "handreiking" not in DOC_TYPES


def test_ap_rss_and_listing_agree_on_identity():
    feed = b"""<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
      <item><title>Boete Netflix</title>
        <link>https://www.autoriteitpersoonsgegevens.nl/documenten/boete-netflix</link>
        <pubDate>Wed, 18 Dec 2024 09:00:00 +0100</pubDate>
        <guid isPermaLink="false">7141cb1e</guid></item>
      <item><link></link></item></channel></rss>"""
    stubs = rss_stubs(feed)
    assert len(stubs) == 1  # the link-less item is dropped
    assert stubs[0].stable_id == stable_id(
        "https://www.autoriteitpersoonsgegevens.nl/documenten/boete-netflix")
    assert stubs[0].hint_date.isoformat() == "2024-12-18"
    assert rss_stubs(b"not xml at all") == []


def test_ap_document_page_gives_summary_themes_and_the_operative_pdf():
    page = b"""<article class="node-publication-full">
      <h1 class="node-publication-full__title"><span>Boete Netflix</span></h1>
      <div class="node-publication-full__meta">
        <div class="node-publication-full__meta-submitted">18 december 2024</div>
        <div class="node-publication-full__topics">
          <div class="node-publication-full__topics-item">
            <a href="/themas/internationaal/doorgifte">Doorgifte</a></div></div>
      </div>
      <div class="node-publication-full__intro"><p>De AP legt een boete op.</p></div>
      <div class="node-publication-full__primary-content"><p>Toelichting.</p></div>
      <div class="node-publication-full__files"><div>
        <a data-type="pdf" download="Boete Netflix.pdf"
           href="/system/files?file=2024-12/boete-netflix.pdf">PDF, 876 kB</a>
        <a data-type="pdf" download="Boete Netflix.pdf"
           href="/system/files?file=2024-12/boete-netflix.pdf">PDF, 876 kB</a>
      </div></div>
    </article>"""
    parsed = parse_document(page)
    assert parsed["title"] == "Boete Netflix"
    assert parse_dutch_date(parsed["date"]).isoformat() == "2024-12-18"
    assert [t["label"] for t in parsed["topics"]] == ["Doorgifte"]
    assert "De AP legt een boete op." in parsed["text"]
    assert "Toelichting." in parsed["text"]
    assert len(parsed["files"]) == 1  # the repeated link is one attachment
    assert parsed["files"][0]["filename"] == "Boete Netflix.pdf"


def test_dutch_month_names_and_junk_dates():
    assert parse_dutch_date("05 februari 2026").isoformat() == "2026-02-05"
    assert parse_dutch_date("31 juli 2026").isoformat() == "2026-07-31"
    assert parse_dutch_date("32 juli 2026") is None      # not a real day
    assert parse_dutch_date("18 dezember 2024") is None  # German, not Dutch
    assert parse_dutch_date(None) is None


# ---------------------------------------------------------------------------
# the grammar the Dutch decisions actually need
# ---------------------------------------------------------------------------

def test_dutch_lid_and_onder_become_a_formex_pincite():
    cites = extract_citations(
        "Op grond van artikel 6, eerste lid, aanhef en onder f, van de AVG en "
        "artikel 5, tweede lid, AVG is de verwerking toegestaan.")
    gdpr = {c.pinpoint for c in cites if c.candidate_id == "32016R0679" and c.pinpoint}
    # before: the comma before the instrument name stopped the match, so both fell
    # through to the carry-forward guess and lost their sub-article pincite entirely
    assert {"Article 6(1)(f)", "Article 5(2)"} <= gdpr
    assert not any(c.method == "carry_forward" for c in cites
                   if c.pinpoint in ("Artikel 6", "Artikel 5"))


def test_dutch_uavg_is_not_the_gdpr_however_it_is_spelled():
    cites = extract_citations(
        "Ingevolge artikel 33, vierde lid, aanhef en onder c, van de Uitvoeringswet "
        "Algemene verordening gegevensbescherming mogen deze gegevens worden verwerkt.")
    assert [(c.candidate_id, c.pinpoint) for c in cites
            if c.candidate_id.startswith("nl:law:")] == [
        ("nl:law:uitvoeringswet avg", "Artikel 33, lid vierde, onder c")]
    assert not any(c.candidate_id == "32016R0679" for c in cites)


def test_de_verordening_is_the_gdpr_only_where_the_avg_is_named():
    defined = extract_citations(
        "Onverminderd artikel 10 van de verordening mogen persoonsgegevens van "
        "strafrechtelijke aard worden verwerkt. Dit volgt ook uit de AVG.")
    assert ("32016R0679", "Article 10") in [
        (c.candidate_id, c.pinpoint) for c in defined
        if c.method == "nl_verordening_article"]
    # a document about some other regulation must not acquire a GDPR edge
    bare = extract_citations(
        "Onverminderd artikel 10 van de verordening geldt het volgende.")
    assert not any(c.method == "nl_verordening_article" for c in bare)


def test_relevance_gate_sees_continental_citations():
    """The gate that decides whether a regulator document reaches search ran only the
    anglophone grammars, so a Dutch decision citing the AVG counted as citing nothing
    and every AP document would have been stored search-excluded."""
    dutch = "De AP stelt vast dat artikel 5, eerste lid, van de AVG is geschonden."
    assert not [c for c in all_grammar_citations(dutch)
                if c.entity_kind in ("regulation", "act")] == []
    assert any(c.candidate_id == "32016R0679" for c in all_grammar_citations(dutch))


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def test_new_dpa_sources_are_registered_and_pollable():
    rows = {row["key"]: row for row in source_catalog()}
    for key in ("ie-dpc-guidance", "nl-ap"):
        assert key in ADAPTERS
        assert rows[key]["can_incremental"], key
        assert rows[key]["kind_label"] == "Guidance and regulatory material"
    assert rows["nl-ap"]["group_label"] == "Netherlands"
    assert rows["ie-dpc-guidance"]["group_label"] == "Ireland"
