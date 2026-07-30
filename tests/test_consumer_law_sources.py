from __future__ import annotations

import json

from raglex.adapters.eu_consumer_guidance import (
    document_stubs,
    sitemap_consumer_pages,
    title_default_instrument,
)
from raglex.adapters.it_agcm import bulletin_stubs
from raglex.adapters.nl_acm_guidance import guidance_stubs
from raglex.adapters.registry import ADAPTERS, source_catalog
from raglex.citations.extractor import extract_citations
from raglex.citations.stage import _home_of


def test_commission_sitemap_and_document_cards_are_enumerable():
    xml = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://commission.europa.eu/topics/consumers/example_en</loc>
      <lastmod>2026-06-30T10:00Z</lastmod></url>
      <url><loc>https://commission.europa.eu/topics/energy/example_en</loc></url>
    </urlset>"""
    assert sitemap_consumer_pages(xml) == [
        ("https://commission.europa.eu/topics/consumers/example_en",
         "2026-06-30T10:00Z")
    ]
    html = b"""
      <a href="/document/264d8c70-2f9a-4955-8e7b-154d55a9b684_en">Landing</a>
      <div class="ecl-file"><div class="ecl-file__detail-meta-item">30 June 2026</div>
      <div class="ecl-file__title">Common understanding</div>
      <a href="/document/download/264d8c70-2f9a-4955-8e7b-154d55a9b684_en?filename=x.pdf">
      Download</a></div>"""
    stubs = document_stubs(html, page_url="https://commission.europa.eu/topics/consumers/x_en",
                           watermark="2026-06-30T10:00Z")
    assert len(stubs) == 1
    assert stubs[0].hints["direct_download"] is True
    assert stubs[0].title == "Common understanding"
    assert stubs[0].hint_date.isoformat() == "2026-06-30"


def test_commission_title_default_is_only_single_explicit_directive():
    assert title_default_instrument(
        "Guidance on Directive 2005/29/EC concerning unfair commercial practices"
    ) == {"id": "32005L0029", "kind": "directive"}
    assert title_default_instrument(
        "Comparison of Directive 2005/29/EC and Directive 2011/83/EU"
    ) is None


def test_agcm_and_acm_official_listings_parse_without_xhr():
    agcm = b"""<table><tbody><tr><td>27/07/2026</td><td>
      <a href="/pubblicazioni/bollettino-settimanale/2026/30/Bollettino-30-2026">
      Bollettino 30/2026</a></td></tr></tbody></table>"""
    rows = bulletin_stubs(agcm)
    assert rows[0].stable_id == "it/agcm/bollettino/2026/bollettino-30-2026"
    assert rows[0].hint_date.isoformat() == "2026-07-27"

    acm = b"""<div class="m-card"><a href="/nl/publicaties/leidraad-prijzen">
      Leidraad prijzen</a><p>Publicatie Regelgeving 19-09-2025</p></div>"""
    rows = guidance_stubs(acm)
    assert rows[0].stable_id == "nl/acm/leidraad-prijzen"
    assert rows[0].hint_date.isoformat() == "2025-09-19"


def test_italian_consumer_code_articles_expand_and_orphans_can_follow():
    text = ("Ai sensi degli articoli 20, 21 e 22 del Codice del consumo. "
            "L'articolo 24 disciplina le pratiche aggressive.")
    cites = extract_citations(text)
    pins = {c.pinpoint for c in cites if c.candidate_id == "it/dlgs/2005/206"}
    assert {"Articolo 20", "Articolo 21", "Articolo 22", "Articolo 24"} <= pins


def test_guidance_default_instrument_is_opt_in_metadata():
    home = _home_of({
        "doc_type": "guidance", "stable_id": "notice",
        "meta_json": json.dumps({
            "citation_default_instrument": {"id": "32005L0029", "kind": "directive"}
        }),
    })
    assert home == ("32005L0029", "directive")
    cites = extract_citations(
        "Directive 2011/83/EU is relevant. In a new section, Article 5 applies.",
        home_id=home[0], home_kind=home[1],
    )
    article = next(c for c in cites if c.raw == "Article 5")
    assert article.candidate_id == "32005L0029"


def test_consumer_sources_are_registered_with_incremental_modes():
    expected = {
        "uk-cma-guidance", "eu-consumer-guidance", "nl-acm-guidance", "it-agcm"
    }
    assert expected <= set(ADAPTERS)
    catalog = {row["key"]: row for row in source_catalog()}
    assert catalog["uk-cma-guidance"]["incremental_mode"] == "early-stop"
    assert catalog["nl-acm-guidance"]["incremental_mode"] == "full-walk"
