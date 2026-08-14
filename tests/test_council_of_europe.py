"""Council of Europe adapters: network-free contract and regression tests."""

from __future__ import annotations

from types import SimpleNamespace

from raglex.adapters.council_of_europe import (
    CouncilOfEuropeEdocAdapter,
    ECHRPublicationsAdapter,
    PACECommitteeDocumentsAdapter,
    parse_edoc_categories,
    parse_edoc_detail,
    parse_edoc_products,
    parse_echr_publications,
    parse_pace_committee_documents,
    pdf_hudoc_links,
    parse_treaty_detail,
    parse_treaty_api_rows,
    parse_treaty_list,
    treaty_api_config,
    treaty_aliases,
    treaty_segments,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, source_catalog
from raglex.citations import extract_citations
from raglex.citations.snowball import _classify


def _card(product_id: str, reference: str, title: str, year: int = 2024) -> str:
    return f"""
    <article class="product-miniature" data-id-product="{product_id}">
      <span class="reflist">Ref {reference}</span>
      <h2 class="product-title"><a href="/en/topic/{product_id}-{title.lower()}.html">
        {title} <span class="datepubli">({year})</span></a></h2>
      <div class="minishortdesc">Summary for {title}</div>
    </article>"""


def _page(*cards: str, categories: str = "", last: int = 1) -> str:
    pages = "".join(f'<a href="?page={n}">{n}</a>' for n in range(1, last + 1))
    return (f'<ul class="category-top-menu">{categories}</ul>'
            f'<div class="products">{"".join(cards)}</div>'
            f'<nav class="pagination">{pages}</nav>')


def test_edoc_cards_use_the_printed_reference_not_product_id():
    rows = parse_edoc_products(_page(_card("12594", "043126GBR", "Rule of law")))
    assert rows == [{
        "product_id": "12594", "reference": "043126GBR",
        "url": "https://edoc.coe.int/en/topic/12594-rule of law.html",
        "title": "Rule of law", "year": 2024,
        "summary": "Summary for Rule of law",
    }]


def test_edoc_category_tree_contains_parents_and_children():
    html = """
    <ul class="category-top-menu"><li><ul class="category-sub-menu">
      <li><a href="/en/198-new-technologies-medias">New technologies / Media</a>
        <ul class="category-sub-menu"><li>
          <a href="/en/199-internet">Internet</a></li></ul></li>
    </ul></li></ul>"""
    assert [(x["id"], x["title"]) for x in parse_edoc_categories(html)] == [
        ("198", "New technologies / Media"), ("199", "Internet")]


class _Client:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        body = self.pages[url]
        return SimpleNamespace(content=body if isinstance(body, bytes) else body.encode(),
                               status_code=200, headers={})


def test_catalog_backfill_pages_every_category_and_dedupes_before_fetch():
    categories = ('<li><a href="/en/204-bioethics">Bioethics</a></li>'
                  '<li><a href="/en/203-health">Health</a></li>')
    home = _page(categories=categories)
    bio = _page(_card("1", "REF-ONE-GBR", "One"),
                _card("2", "REF-SHARED-GBR", "Shared"))
    health = _page(_card("2", "REF-SHARED-GBR", "Shared"),
                   _card("3", "REF-THREE-GBR", "Three"))
    client = _Client({
        "https://edoc.coe.int/en/": home,
        "https://edoc.coe.int/en/204-bioethics?order=epi.date_public.desc&page=1": bio,
        "https://edoc.coe.int/en/203-health?order=epi.date_public.desc&page=1": health,
    })
    adapter = CouncilOfEuropeEdocAdapter(catalog=True, client=client)
    stubs = list(adapter.discover(None))
    assert [s.stable_id for s in stubs] == [
        "coe/edoc/REF-ONE-GBR", "coe/edoc/REF-SHARED-GBR", "coe/edoc/REF-THREE-GBR"]
    # The duplicate advances the feed-wide cursor but does not emit or trigger fetch().
    assert [s.hints["resume_offset"] for s in stubs] == [1, 2, 4]
    assert len(client.calls) == 3


def test_edoc_detail_keeps_pdf_and_metadata():
    html = """
      <h1 class="prodtitle">A title <span class="datepubli">(2025)</span></h1>
      <div class="cartouchecat">Bioethics</div>
      <div class="product-reference"><span itemprop="sku">043126GBR</span></div>
      <section class="product-features"><div class="featuregroup">
        <div class="featurename">Language :</div><div class="featurevalue">English</div>
      </div></section>
      <div class="product-manufacturer"><span>Council of Europe</span></div>
      <div class="downloadadd"><a href="/download/key">Download</a></div>
      <div id="description_short"><div class="product-description">A summary.</div></div>
    """
    meta = parse_edoc_detail(html)
    assert (meta["reference"], meta["year"], meta["features"]["language"]) == (
        "043126GBR", 2025, "English")
    assert meta["download_url"] == "https://edoc.coe.int/download/key"


ECHR_HTML = """
<h2>Children and parents</h2>
<a href="/documents/d/echr/FS_Children_ENG">Children's rights</a>
<h2>Another topic</h2>
<a href="/documents/d/echr/FS_Children_ENG">Children's rights</a>
<a href="/documents/d/echr/FS_Children_FRA">Droits des enfants</a>
<h3>Handbook on European data protection law</h3>
<p><a href="/documents/d/echr/Handbook_data_protection_ENG">English</a> (2018)</p>
"""


def test_echr_indexes_keep_only_english_and_dedupe_repeated_topic_links():
    rows = parse_echr_publications(ECHR_HTML, "factsheets")
    assert [r["slug"] for r in rows] == ["FS_Children_ENG", "Handbook_data_protection_ENG"]
    assert rows[1]["title"] == "Handbook on European data protection law"
    assert rows[1]["year"] == 2018


class _HtmlFetcher:
    def fetch(self, url):
        return SimpleNamespace(html=ECHR_HTML)


def test_factsheet_discovery_marks_stable_urls_for_monthly_revision_checks():
    adapter = ECHRPublicationsAdapter(fetcher=_HtmlFetcher())
    stubs = list(adapter.discover(None))
    assert stubs[0].stable_id == "echr/publication/factsheets/fs_children_eng"
    assert all(s.hints["revision"] is True for s in stubs)


def test_treaty_list_and_detail_choose_the_english_official_text():
    listing = """
    <table><tr><td>229</td><td>Claims Convention (CETS No. 229)</td>
      <td>16/12/2025</td><td><a href="?module=treaty-detail&amp;treatynum=229">Details</a></td>
    </tr></table>"""
    assert parse_treaty_list(listing)[0] == {
        "number": "229", "title": "Claims Convention",
        "url": "https://www.coe.int/en/web/conventions/full-list?module=treaty-detail&treatynum=229",
        "opened": __import__("datetime").date(2025, 12, 16),
    }
    detail = """
    <table>
      <tr><th>Title</th><td>Claims Convention (CETS No. 229)</td></tr>
      <tr><th>Reference</th><td>CETS No. 229</td></tr>
      <tr><th>Opening of the treaty</th><td>The Hague 16/12/2025</td></tr>
      <tr><th>Entry in force</th><td>Special conditions</td></tr>
      <tr><th>Official Texts</th><td><a href="https://rm.coe.int/english/123">English</a>
        <a href="https://rm.coe.int/french/123">French</a></td></tr>
    </table>"""
    parsed = parse_treaty_detail(detail, "229")
    assert parsed["pdf_url"] == "https://rm.coe.int/english/123"
    assert parsed["reference"] == "CETS No. 229"


def test_treaty_react_shell_exposes_api_and_api_rows_are_complete_metadata():
    shell = """<script>
      window.conventions_api_url="https://conventions.example/WS/";
      window.conventions_api_key="public-token";
    </script><noscript>You need to enable JavaScript to run this app.</noscript>"""
    assert treaty_api_config(shell) == (
        "https://conventions.example/WS/", "public-token")
    rows = parse_treaty_api_rows([{
        "Numero_traite": "229", "Mention": "CETS",
        "Libelle_titre_ENG": "Claims Convention (CETS No. 229)",
        "Nom_commun_ENG": "Claims Convention", "Date_ste": "2025-12-16T00:00:00",
        "Date_vigueur_ste": None, "Code_lieu_ste": 11,
        "Lien_pdf_traite_ENG": "https://rm.coe.int/english/229",
        "Lien_html_resume_ENG": "/Treaty/en/Summaries/Html/229.htm",
    }])
    assert rows[0]["number"] == "229"
    assert rows[0]["title"] == "Claims Convention"
    assert rows[0]["detail"]["reference"] == "CETS No. 229"
    assert rows[0]["detail"]["short_title"] == "Claims Convention"
    assert rows[0]["detail"]["pdf_url"] == "https://rm.coe.int/english/229"


def test_old_and_new_treaty_layouts_become_articles_and_paragraphs():
    text = """PREAMBLE
Article 1 – Purpose
1 This Convention protects rights.
2 It applies throughout Europe.
ARTICLE 2
1. Parties shall cooperate.
2. They shall report.
"""
    segments = treaty_segments(text)
    assert [(s.label, s.kind) for s in segments] == [
        ("Article 1", "article"), ("Article 1(1)", "paragraph"),
        ("Article 1(2)", "paragraph"), ("Article 2", "article"),
        ("Article 2(1)", "paragraph"), ("Article 2(2)", "paragraph")]
    assert all(text[s.char_start:s.char_end].strip() for s in segments)


def test_treaties_mint_official_name_aliases_from_metadata():
    aliases = treaty_aliases("108", {
        "reference": "CETS No. 108",
        "title": "Convention for the Protection of Individuals (CETS No. 108)",
        "short_title": "Convention 108",
    })
    assert "Convention for the Protection of Individuals" in aliases
    assert "Convention for the Protection of Individuals (CETS No. 108)" in aliases
    assert "Convention 108" in aliases


def test_cets_citations_resolve_with_pinpoints_and_echr_005_does_not_fork():
    cites = extract_citations("Article 9(2) of CETS No. 108 and Article 6 of ETS 005")
    got = {(c.candidate_id, c.pinpoint) for c in cites if c.method == "coe_treaty_series"}
    assert ("coe/treaty/108", "Article 9(2)") in got
    assert ("echr/convention", "Article 6") in got


PACE_HTML = """
<main><div class="archive-row">
  <span>04/02/2026</span>
  <a href="https://rm.coe.int/as-jur-2026-01/1680abc">AS/Jur (2026) 01</a>
  <span>Human rights and the environment</span><span>Information document</span>
</div></main>
"""

PACE_VARIANTS_HTML = """
<span>30/04/2026</span><a href="https://rm.coe.int/a">AS/JUR/Inf (2026) 01 rev.3</a>
<span>07/12/2021</span><a href="https://rm.coe.int/b">AS/Jur (2021) PV 10 / Minutes</a>
<span>21/06/2023</span><a href="https://rm.coe.int/c">AS/Jur (2023) 19 Appendix 3</a>
"""


def test_pace_committee_archive_uses_reference_as_identity():
    rows = parse_pace_committee_documents(PACE_HTML, "asjur")
    assert rows == [{
        "reference": "AS/Jur (2026) 01",
        "url": "https://rm.coe.int/as-jur-2026-01/1680abc",
        "title": "Human rights and the environment Information document",
        "published": __import__("datetime").date(2026, 2, 4),
        "committee": "asjur",
    }]


def test_pace_reference_keeps_inf_minutes_appendices_and_revisions_distinct():
    rows = parse_pace_committee_documents(PACE_VARIANTS_HTML, "asjur")
    assert [row["reference"] for row in rows] == [
        "AS/JUR/Inf (2026) 01 rev.3",
        "AS/Jur (2021) PV 10",
        "AS/Jur (2023) 19 Appendix 3",
    ]


class _PaceFetcher:
    def fetch(self, url):
        return SimpleNamespace(html=PACE_HTML)


class _TransitionalPaceFetcher:
    def __init__(self):
        self.calls = 0

    def fetch(self, url):
        self.calls += 1
        return SimpleNamespace(html="<html><body></body></html>" if self.calls == 1
                               else PACE_HTML)


def test_pace_full_walk_dedupes_across_committee_indexes_before_download():
    adapter = PACECommitteeDocumentsAdapter(
        committees="asjur,aspol", fetcher=_PaceFetcher())
    stubs = list(adapter.discover(None))
    assert len(stubs) == 1
    assert stubs[0].stable_id == "coe/pace/committee/as-jur-2026-01"
    assert stubs[0].hints["committees"] == ["asjur", "aspol"]
    assert stubs[0].hints["resume_offset"] == 1


def test_pace_retries_a_blank_cloudflare_transition_document():
    fetcher = _TransitionalPaceFetcher()
    adapter = PACECommitteeDocumentsAdapter(committees="asmig", fetcher=fetcher)
    assert len(list(adapter.discover(None))) == 1
    assert fetcher.calls == 2


def test_pdf_hudoc_annotation_recovers_itemid_when_visible_text_is_only_case_name():
    fitz = __import__("pytest").importorskip("fitz")
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Someone v Turkiye")
    rect = page.search_for("Someone v Turkiye")[0]
    uri = 'https://hudoc.echr.coe.int/eng#{"itemid":["001-145343"]}'
    page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": uri})
    pdf = document.tobytes()
    document.close()
    assert pdf_hudoc_links(pdf) == [{
        "itemid": "001-145343", "url": uri,
        "text": "Someone v Turkiye", "page": 1,
    }]


def test_hudoc_itemid_is_routable_to_the_targeted_echr_adapter():
    assert _classify("001-145343", "case") == (
        "ECHR HUDOC item ID", "CoE", "echr")


def test_sources_are_registered_with_truthful_incremental_modes():
    keys = {"coe-edoc", "coe-edoc-catalog", "echr-factsheets",
            "echr-joint-publications", "coe-treaties", "coe-pace-committees"}
    assert keys <= ADAPTERS.keys()
    assert INCREMENTAL_MODE["coe-edoc"] == "early-stop"
    assert all(INCREMENTAL_MODE[k] == "full-walk" for k in keys - {"coe-edoc"})
    catalog = {row["key"]: row for row in source_catalog()}
    assert all(catalog[k]["group_label"] == "Council of Europe" for k in keys)
