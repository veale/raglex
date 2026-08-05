"""ICO adapter — register listing, page parsing (facts / attachments / breadcrumb /
boilerplate), regime detection and the dominant-instrument rule, the sitemap collection,
and the citation-side consequences (source aliases, the GDPR→UK GDPR rebinding, and the
host noun a statutory basis binds). Network-free."""

from __future__ import annotations

import json

import pytest

from raglex.adapters.uk_ico import (
    GUIDANCE_SECTIONS,
    ICOAdapter,
    REGISTERS,
    Attachment,
    _guidance_slug,
    _item_slug,
    action_types,
    dominant_regime,
    parse_listing,
    parse_page,
    parse_sitemap,
    qualified_title,
    regimes_in,
)
from raglex.core.models import DocType, RelationshipType

UK_GDPR = "european/regulation/2016/0679"
PECR = "uksi/2003/2426"
DPA18 = "ukpga/2018/12"

LISTING = {
    "results": [
        {"filterItemMetaData": "28 May 2026, Enforcement notices",
         "title": "Thermotech Wall and Loft Surveys Ltd",
         "createdDateTime": "2026-07-07T16:26:05.967Z", "id": 120121,
         "url": "/action-weve-taken/enforcement/2026/05/thermotech-wall-and-loft-surveys-ltd-en/",
         "description": "TWLS instigated 575,062 unsolicited direct marketing calls "
                        "in breach of PECR."},
        {"filterItemMetaData": "23 February 2026, Monetary penalties",
         "title": "Reddit, Inc.", "createdDateTime": "2026-03-19T11:56:10.78Z",
         "url": "/action-weve-taken/enforcement/2026/02/reddit-inc/",
         "description": "A penalty for infringing Articles 5(1)(a), 6 and 8 UK GDPR."},
        # a promoted card from elsewhere on the site — not this register
        {"title": "Careers", "createdDateTime": "2026-01-01T00:00:00Z",
         "url": "/about-the-ico/jobs/", "description": ""},
    ],
    "pagination": {"totalResults": 219, "totalPages": 9},
}

ITEM_PAGE = """
<html><head>
<meta name="DC.Date" content="Wednesday, July 08, 2026" />
<meta name="DC.PageID" content="120121" />
</head><body><main id="main-content">
<nav aria-label="breadcrumb"><ul>
  <li><a href="/action-weve-taken/">Action we've taken</a></li>
  <li><a href="/action-weve-taken/enforcement/">Enforcement action</a></li>
  <li>Thermotech Wall and Loft Surveys Ltd</li>
</ul></nav>
<h1>Thermotech Wall and Loft Surveys Ltd</h1>
<ul class="text-sm text-neutral-600">
  <li><span>Date</span><strong>28 May 2026</strong></li>
  <li><span>Type</span><strong>Enforcement notices</strong></li>
  <li><span>Sector</span><strong>Marketing</strong></li>
</ul>
<div class="prose"><p>In April 2025, the ICO carried out a search warrant in relation
to TWLS and its compliance with PECR. TWLS instigated unsolicited calls contrary to
regulation 21 of PECR. The Commissioner's powers derive from the Data Protection Act
2018.</p></div>
<further-Reading x-href="/media2/rcsppogm/thermotech-enforcement-notice.pdf"
                 x-title="Thermotech enforcement notice" x-size="204912"></further-Reading>
<further-Reading x-href="/media2/zzz/thermotech-cym-notice.pdf"
                 x-title="Thermotech enforcement notice - Welsh" x-size="204000"></further-Reading>
<further-Reading x-href="https://ico.org.uk/about-the-ico/media-centre/news/2026/x/"
                 x-title="A news story" x-location="About the ICO"></further-Reading>
</main></body></html>
"""

GUIDANCE_PAGE = """
<html><head><meta name="DC.Date" content="Tuesday, August 04, 2026" /></head>
<body><main id="main-content">
<nav aria-label="breadcrumb"><ul>
  <li><a href="/for-organisations/">For organisations</a></li>
  <li><a href="/x/">UK GDPR guidance and resources</a></li>
  <li><a href="/y/">A guide to lawful basis</a></li>
  <li>Consent</li>
</ul></nav>
<h1>Consent</h1>
<div class="prose">
<p>Due to changes made by the Data (Use and Access) Act, this guidance is under review
and may be subject to change.</p>
<p>Click to toggle details</p>
<p>Consent is one of the six lawful bases in Article 6 of the UK GDPR. Consent must be
freely given. The UK GDPR sets a high standard for consent, and Article 7 UK GDPR
sets out the conditions. Recital 32 UK GDPR is relevant. See also the UK GDPR
provisions on children's consent.</p>
</div>
</main></body></html>
"""

SITEMAP = """<?xml version="1.0" encoding="utf-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ico.org.uk/</loc><lastmod>2026-08-05T10:06:30+00:00</lastmod></url>
  <url><loc>https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/lawful-basis/a-guide-to-lawful-basis/consent/</loc>
       <lastmod>2026-08-04T13:03:29+00:00</lastmod></url>
  <url><loc>https://ico.org.uk/for-the-public/nuisance-calls/</loc>
       <lastmod>2026-05-05T09:00:00+00:00</lastmod></url>
  <url><loc>https://ico.org.uk/about-the-ico/research-reports-impact-and-evaluation/research-and-reports/freedom-of-information/foi-upstream-regulation-report/</loc>
       <lastmod>2025-02-06T09:00:00+00:00</lastmod></url>
  <url><loc>https://ico.org.uk/for-the-public/thanks/</loc>
       <lastmod>2026-01-01T00:00:00+00:00</lastmod></url>
  <url><loc>https://ico.org.uk/action-weve-taken/decision-notices/2026/1/ic-123/</loc>
       <lastmod>2026-01-01T00:00:00+00:00</lastmod></url>
</urlset>
""".encode()


# ── listing ──────────────────────────────────────────────────────────────────
def test_parse_listing_keeps_only_this_registers_items():
    register = REGISTERS["enforcement"][0]
    items, total_pages = parse_listing(LISTING, register)
    assert total_pages == 9 and len(items) == 2      # the /jobs/ card is dropped
    assert items[0].created == "2026-07-07T16:26:05.967Z"
    assert _item_slug(register, items[0].url) == (
        "uk-ico/enforcement/2026/thermotech-wall-and-loft-surveys-ltd-en")


def test_item_slug_keeps_the_notice_type_suffix():
    """One company, one date, two published items (-en and -mpn) — two documents."""
    register = REGISTERS["enforcement"][0]
    en = _item_slug(register, "/action-weve-taken/enforcement/2026/02/tmac-ltd-en/")
    mpn = _item_slug(register, "/action-weve-taken/enforcement/2026/02/tmac-ltd-mpn/")
    assert en != mpn and en.endswith("tmac-ltd-en")


# ── page parsing ─────────────────────────────────────────────────────────────
def test_parse_item_page_facts_attachments_and_related_links():
    page = parse_page(ITEM_PAGE)
    assert page.title == "Thermotech Wall and Loft Surveys Ltd"
    assert page.facts == {"date": "28 May 2026", "type": "Enforcement notices",
                          "sector": "Marketing"}
    assert page.page_id == "120121" and str(page.dc_date) == "2026-07-08"
    # the Welsh twin is skipped; the news story is a link, not an attachment
    assert [a.title for a in page.attachments] == ["Thermotech enforcement notice"]
    assert page.attachments[0].url.startswith("https://ico.org.uk/media2/")
    assert [r["location"] for r in page.related] == ["About the ICO"]
    assert "further-Reading" not in page.body and "regulation 21 of PECR" in page.body


def test_parse_guidance_page_strips_the_site_wide_review_banner():
    """The banner names the Data (Use and Access) Act on ~1,000 guidance pages. Left in,
    every one of them would be tagged duaa — and a page citing nothing else would
    declare the DUAA as its governing instrument."""
    page = parse_page(GUIDANCE_PAGE)
    assert "Data (Use and Access) Act" not in page.body
    assert "Click to toggle details" not in page.body
    assert "Consent is one of the six lawful bases" in page.body
    assert not regimes_in(page.body) or regimes_in(page.body)[0][0].id == UK_GDPR


def test_qualified_title_names_the_guide_only_for_nested_pages():
    assert qualified_title("Consent", ("For organisations", "UK GDPR guidance",
                                       "A guide to lawful basis", "Consent")) == (
        "A guide to lawful basis — Consent")
    # a page sitting directly under its section is already self-describing
    assert qualified_title("Nuisance calls", ("For the public", "Nuisance calls")) == (
        "Nuisance calls")


def test_attachment_ext_defaults_and_reads_the_path():
    assert Attachment("https://ico.org.uk/media2/a/b.PDF", "x", 1).ext == "pdf"
    assert Attachment("https://ico.org.uk/media2/a/b.docx", "x", 1).ext == "docx"


# ── regimes ──────────────────────────────────────────────────────────────────
def test_action_types_splits_and_slugifies():
    assert action_types("Reprimands, Monetary penalties") == [
        "reprimand", "monetary-penalty"]
    assert action_types("Our response to others' consultation") == [
        "consultation-response"]
    assert action_types("Some New Category") == ["some-new-category"]


def test_regimes_acronyms_are_uppercase_only():
    assert [r.tag for r, _ in regimes_in("a breach of PECR")] == ["pecr"]
    assert regimes_in("see pecr.pdf and the file gdpr_notes") == []


def test_bare_gdpr_in_ico_material_is_the_uk_gdpr():
    (regime, _), = regimes_in("The controller infringed Article 6 of the GDPR.")
    assert regime.id == UK_GDPR


def test_dpa_acronym_does_not_mint_the_2018_act_in_a_1998_notice():
    ids = {r.id for r, _ in regimes_in(
        "Under the Data Protection Act 1998, the DPA required a fair-processing notice.")}
    assert ids == {"ukpga/1998/29"}


def test_dominant_regime_prefers_the_registers_own_framing():
    """A PECR notice names the DPA 2018 a dozen times — that is where the power to issue
    it comes from — so body counts alone never clear the 3× bar. The title/summary say
    what the action is under."""
    body = regimes_in("PECR " * 26 + "Data Protection Act 2018 " * 11)
    assert dominant_regime(body) is None
    headline = regimes_in("Contravention of regulations 21 and 24 of PECR")
    assert dominant_regime(body, headline).id == PECR


def test_dominant_regime_declares_nothing_when_genuinely_mixed():
    counted = regimes_in("UK GDPR " * 6 + "Freedom of Information Act 2000 " * 5)
    assert dominant_regime(counted, counted) is None


# ── sitemap ──────────────────────────────────────────────────────────────────
def test_parse_sitemap_keeps_only_the_harvested_subtrees():
    entries = parse_sitemap(SITEMAP)
    paths = [e.path for e in entries]
    assert "/action-weve-taken/decision-notices/2026/1/ic-123/" not in paths  # not ours
    assert "/for-the-public/thanks/" not in paths                            # plumbing
    assert "/" not in paths                                                  # the root
    assert len(entries) == 3
    slugs = {_guidance_slug(e) for e in entries}
    # the section prefix is stripped whole, so research ids don't repeat their section
    assert "uk-ico/research/research-and-reports/freedom-of-information/foi-upstream-regulation-report" in slugs
    assert "uk-ico/public/nuisance-calls" in slugs


def test_sections_option_narrows_the_sitemap_walk():
    ad = ICOAdapter(collection="guidance", sections="for-the-public")
    assert [s[0] for s in ad.sections] == ["/for-the-public/"]
    assert len(parse_sitemap(SITEMAP, ad.sections)) == 1


# ── the adapter end to end ───────────────────────────────────────────────────
def _tiny_pdf(text: str) -> bytes:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    words = text.split()
    for i in range(0, len(words), 8):
        page.insert_text((72, 72 + 14 * (i // 8)), " ".join(words[i:i + 8]))
    return doc.tobytes()


class _Resp:
    def __init__(self, content):
        self.content = content if isinstance(content, bytes) else content.encode()


class _FakeClient:
    """Serves the search API, the item page, the sitemap and one PDF; counts requests so
    the per-run attachment cache can be asserted."""

    def __init__(self, pdf: bytes) -> None:
        self.pdf = pdf
        self.requests: list[str] = []

    def get(self, url, headers=None):
        self.requests.append(url)
        if url.endswith("sitemap.xml"):
            return _Resp(SITEMAP)
        if url.endswith(".pdf"):
            return _Resp(self.pdf)
        if "/for-organisations/" in url or "/for-the-public/" in url or "research" in url:
            return _Resp(GUIDANCE_PAGE)
        return _Resp(ITEM_PAGE)

    def request(self, method, url, json=None, headers=None):
        self.requests.append(f"{method} {url}")
        page = (json or {}).get("pageNumber", 1)
        payload = dict(LISTING) if page == 1 else {"results": [],
                                                  "pagination": {"totalPages": 1}}
        payload["pagination"] = {**payload["pagination"], "totalPages": 1}
        import json as _json
        return _Resp(_json.dumps(payload))


def test_discover_registers_yields_oldest_first_with_a_cms_change_signal():
    ad = ICOAdapter(client=_FakeClient(b""))
    stubs = list(ad.discover(None))
    assert [s.stable_id.rsplit("/", 1)[-1] for s in stubs] == [
        "reddit-inc", "thermotech-wall-and-loft-surveys-ltd-en"]   # by createdDateTime
    assert stubs[0].hints["watermark"] == stubs[0].hints["contenthash"]
    assert stubs[0].hints["feed_total"] == 2 and stubs[0].hints["resume_offset"] == 0
    # the cursor drops everything already seen
    assert list(ad.discover("2026-07-01T00:00:00Z"))[0].stable_id.endswith("-en")


def test_fetch_inlines_the_notice_pdf_and_links_the_regime():
    pdf = _tiny_pdf("The Commissioner issues this enforcement notice under regulation "
                    "26 of PECR. " * 12)
    client = _FakeClient(pdf)
    ad = ICOAdapter(client=client)
    stub = next(s for s in ad.discover(None) if s.stable_id.endswith("-en"))
    rec = ad.fetch(stub)

    assert rec.doc_type == DocType.DECISION and rec.court == "ICO"
    assert rec.source == "uk-ico-enforcement" and str(rec.decision_date) == "2026-05-28"
    assert "enforcement notice under regulation" in rec.text   # the PDF is inlined
    assert rec.extra["documents"][0]["text_chars"] > 0
    assert rec.extra["action_types"] == ["enforcement-notice"]
    assert rec.extra["sector"] == "Marketing"
    assert set(rec.topic_tags) >= {"ico", "uk", "enforcement", "enforcement-notice",
                                   "marketing", "pecr"}
    edges = {(r.relationship_type, r.dst_id) for r in rec.relations}
    assert (RelationshipType.INTERPRETS, PECR) in edges
    assert (RelationshipType.INTERPRETS, DPA18) in edges
    # PECR leads the register's own framing, so it is the declared host instrument
    assert rec.extra["citation_default_instrument"] == {"id": PECR, "kind": "regulation"}
    assert rec.extra["statutory_basis"].startswith("Privacy and Electronic")


def test_attachments_are_downloaded_once_per_run():
    client = _FakeClient(_tiny_pdf("x " * 40))
    ad = ICOAdapter(client=client)
    stubs = [s for s in ad.discover(None)]
    for s in stubs:
        ad.fetch(s)
    assert sum(1 for u in client.requests if u.endswith(".pdf")) == 1


def test_guidance_collection_titles_by_guide_and_skips_thin_plumbing():
    ad = ICOAdapter(collection="guidance", client=_FakeClient(b""))
    stubs = list(ad.discover(None))
    assert ad.source == "uk-ico-guidance" and len(stubs) == 3
    rec = ad.fetch(next(s for s in stubs if s.stable_id.endswith("consent")))
    assert rec.title == "A guide to lawful basis — Consent"
    assert rec.doc_type == DocType.GUIDANCE
    assert "duaa" not in rec.topic_tags and "uk-gdpr" in rec.topic_tags
    assert rec.extra["citation_default_instrument"]["id"] == UK_GDPR


def test_unknown_collection_is_rejected_at_construction():
    with pytest.raises(ValueError):
        ICOAdapter(collection="decision-notices")


# ── registry + taxonomy wiring ───────────────────────────────────────────────
def test_registry_taxonomy_and_options():
    from raglex.adapters.registry import ADAPTERS, SOURCE_INFO, get_adapter, source_catalog
    from raglex.citations.taxonomy import classify_document

    keys = ["uk-ico-enforcement", "uk-ico-audits", "uk-ico-consultations",
            "uk-ico-guidance"]
    catalog = {s["key"]: s for s in source_catalog()}
    for key in keys:
        assert key in ADAPTERS and key in SOURCE_INFO and key in catalog
        assert get_adapter(key).source == key
        assert catalog[key]["can_incremental"] is True
        # every declared option is accepted by the constructor
        for opt in SOURCE_INFO[key].options:
            get_adapter(key, **{opt.name: "2.0" if opt.name == "min_interval"
                                else "for-the-public"})
    assert classify_document(source="uk-ico-enforcement", doc_type="decision",
                             stable_id="uk-ico/enforcement/2026/x").subtype == (
        "uk-ico-enforcement")


def test_enforcement_displays_as_administrative_not_case_law():
    """A regulator's notice is an administrative decision (docs/adapter-authoring.md),
    and it carries doc_type 'decision', which the case-type check would otherwise read
    as case law."""
    from raglex.facade import Facade

    assert "uk-ico-enforcement" in Facade._ADMIN_SOURCES
    assert Facade._doc_kind(Facade, source="uk-ico-enforcement", doc_type="decision",
                            court="ICO") == "administrative"
    # guidance from the same regulator stays guidance
    assert Facade._doc_kind(Facade, source="uk-ico-guidance", doc_type="guidance",
                            court="ICO") == "guidance"


# ── the citation-side consequences ───────────────────────────────────────────
def _doc(source: str, meta: dict | None = None) -> dict:
    return {"source": source, "court": "ICO", "stable_id": "uk-ico/enforcement/2026/x",
            "meta_json": json.dumps(meta or {})}


def test_ico_sources_expand_the_commissioners_own_acronyms():
    from raglex.citations.stage import aliases_for_document

    aliases = aliases_for_document(_doc("uk-ico-guidance"), None,
                                   "The EIR apply to public authorities.")
    assert aliases["EIR"] == "uksi/2004/3391"
    assert aliases["NIS Regulations"] == "uksi/2018/506"
    # …and only inside ICO material: "EIR" elsewhere is an EU implementing regulation
    assert not (aliases_for_document(_doc("uk-caselaw"), None, "the EIR") or {}).get("EIR")


def test_gdpr_in_an_ico_document_rebinds_to_the_uk_instrument():
    from raglex.citations.extractor import extract_citations
    from raglex.citations.stage import _gate_domestic_statute_names

    cites = extract_citations("The controller breached Article 6 GDPR and Article 5 "
                              "of the GDPR.")
    assert {c.candidate_id for c in cites} == {"32016R0679"}
    rebound = _gate_domestic_statute_names(_doc("uk-ico-enforcement"), cites)
    assert {c.candidate_id for c in rebound} == {"european/regulation/2016/0679"}
    # the pinpoint survives the rebinding — both instruments carry Article segments
    assert {c.pinpoint for c in rebound} == {"Article 6", "Article 5"}
    # a non-ICO host is untouched
    assert {c.candidate_id for c in
            _gate_domestic_statute_names(_doc("uk-caselaw"), cites)} == {"32016R0679"}


def test_every_gdpr_form_in_an_ico_document_maps_to_the_uk_instrument():
    """Whatever form it takes. "Regulation (EU) 2016/679" is the assimilated
    instrument's own formal name — it keeps the EU's numbering — and every occurrence in
    the live corpus was in an enforcement notice, which the Commissioner has no power to
    write about the EU instrument at all."""
    from raglex.citations.extractor import extract_citations
    from raglex.citations.stage import _gate_domestic_statute_names

    for text in ("contrary to Article 5(1)(f) of Regulation (EU) 2016/679",
                 "in breach of Article 6 GDPR",
                 "under the General Data Protection Regulation",
                 "the EU GDPR standard"):
        cites = extract_citations(text)
        rebound = _gate_domestic_statute_names(_doc("uk-ico-enforcement"), cites)
        assert {c.candidate_id for c in rebound} == {UK_GDPR}, text
    # a non-ICO host keeps the EU instrument
    cites = extract_citations("in breach of Article 6 GDPR")
    assert {c.candidate_id for c in
            _gate_domestic_statute_names(_doc("uk-caselaw"), cites)} == {"32016R0679"}


def test_statutory_basis_binds_the_noun_the_instrument_actually_uses():
    from raglex.citations.stage import aliases_for_document

    pecr = aliases_for_document(
        _doc("uk-ico-enforcement",
             {"statutory_basis": "Privacy and Electronic Communications "
                                 "(EC Directive) Regulations 2003"}),
        None, "The Regulations require consent.")
    assert pecr["the Regulations"] == PECR and "the Act" not in pecr

    act = aliases_for_document(
        _doc("uk-ico-enforcement", {"statutory_basis": "Data Protection Act 2018"}),
        None, "The Act confers the power.")
    assert act["the Act"] == DPA18 and "the Regulations" not in act


def test_ico_guidance_sections_constant_covers_the_requested_directories():
    prefixes = {p for p, _, _ in GUIDANCE_SECTIONS}
    assert {"/for-organisations/", "/for-the-public/"} <= prefixes


# ── the post-exit-day rule for UK judgments ──────────────────────────────────
def _judgment(source="uk-caselaw", when="2024-03-01", doc_type="judgment"):
    return {"source": source, "court": "ewhc", "stable_id": "ewhc/2024/1",
            "doc_type": doc_type, "decision_date": when, "meta_json": "{}"}


def test_a_uk_judgment_after_exit_day_reads_the_gdpr_as_the_uk_one():
    """The only data protection regulation a UK court has applied since IP completion
    day. A heuristic, and deliberately biased: an unqualified "GDPR" in a 2024 English
    judgment pointing at the EU instrument is the commoner and the more misleading
    error."""
    from raglex.citations.extractor import extract_citations
    from raglex.citations.stage import _gate_domestic_statute_names

    text = "The claimant relies on Article 15 GDPR."
    cites = extract_citations(text)
    after = _gate_domestic_statute_names(_judgment(when="2024-03-01"), cites, text)
    assert {c.candidate_id for c in after} == {UK_GDPR}
    assert {c.pinpoint for c in after} == {"Article 15"}


def test_a_uk_judgment_before_exit_day_is_left_alone():
    """Before 1 January 2021 there was only the EU instrument."""
    from raglex.citations.extractor import extract_citations
    from raglex.citations.stage import _gate_domestic_statute_names

    text = "The claimant relies on Article 15 GDPR."
    cites = extract_citations(text)
    before = _gate_domestic_statute_names(_judgment(when="2019-06-01"), cites, text)
    assert {c.candidate_id for c in before} == {"32016R0679"}


def test_a_judgment_that_distinguishes_the_eu_gdpr_is_left_alone():
    """The document opting out of the heuristic by saying so. Tested on the whole text,
    because the acronym grammar's matched text for "the EU GDPR" is just "GDPR"."""
    from raglex.citations.extractor import extract_citations
    from raglex.citations.stage import _gate_domestic_statute_names

    text = ("The EU GDPR continues to apply to the Irish controller, whereas "
            "Article 15 GDPR in its assimilated form governs here.")
    cites = extract_citations(text)
    kept = _gate_domestic_statute_names(_judgment(when="2024-03-01"), cites, text)
    assert "32016R0679" in {c.candidate_id for c in kept}


def test_the_rule_does_not_reach_non_uk_or_non_judgment_hosts():
    from raglex.citations.extractor import extract_citations
    from raglex.citations.stage import _gate_domestic_statute_names

    text = "Article 15 GDPR"
    cites = extract_citations(text)
    for doc in (_judgment(source="ie-caselaw"),          # Irish court
                _judgment(source="eu-cellar"),           # CJEU
                _judgment(doc_type="legislation")):      # not a judgment
        out = _gate_domestic_statute_names(doc, cites, text)
        assert {c.candidate_id for c in out} == {"32016R0679"}, doc["source"]
