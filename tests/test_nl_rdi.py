from __future__ import annotations

from datetime import date

from raglex.adapters.nl_rdi import (
    RDIDocumentsAdapter, rdi_detail, rdi_doc_type, rdi_search_page, search_payload,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.core.models import DocType


def test_xhr_is_a_bounded_paged_backfill():
    request = search_payload(2)
    assert request["requestState"]["current"] == 2
    assert request["requestState"]["resultsPerPage"] == 10
    assert request["requestState"]["filters"][0]["values"] == ["pro:downloadDocument"]
    response = {
        "totalPages": 37, "totalResults": 363,
        "results": [{"id": {"raw": "doc-abc"}, "url": {"raw": "/documenten/2026/a"},
                     "page_title": {"raw": "Beschikking boete"},
                     "information_type": {"raw": "Besluit"},
                     "sort_date": {"raw": "2026-08-12T10:00:00+00:00"}}],
    }
    rows, pages, total = rdi_search_page(response)
    assert (pages, total) == (37, 363)
    assert rows[0].stable_id == "nl/rdi/doc-abc"
    assert rows[0].hint_date == date(2026, 8, 12)


def test_landing_page_exposes_each_pdf_and_metadata():
    html = """<script id='elastic-content' type='application/json'>
    {"pageTitle":"Boetebesluiten", "publicationDate":"2026-07-10T09:00:00Z",
     "informationType":{"label":"Besluit"}}</script><h1>Fallback</h1>
    <a class='download-list__link' href='/files/a.pdf'><span class='title'>Besluit A</span></a>
    <a class='download-list__link' href='/files/b.xlsx'><span class='title'>Table</span></a>
    <a class='download-list__link' href='/files/c.pdf'><span class='title'>Besluit C</span></a>"""
    got = rdi_detail(html)
    assert got["title"] == "Boetebesluiten"
    assert got["publication_date"] == date(2026, 7, 10)
    assert got["information_type"] == "Besluit"
    assert [(x["title"], x["url"]) for x in got["files"]] == [
        ("Besluit A", "https://www.rdi.nl/files/a.pdf"),
        ("Besluit C", "https://www.rdi.nl/files/c.pdf"),
    ]


def test_rdi_legal_typing_gracefully_falls_back_to_guidance():
    assert rdi_doc_type("Beschikking boete", "Publicatie") == DocType.DECISION
    assert rdi_doc_type("Advies cyberweerbaarheid", "Publicatie") == DocType.OPINION
    assert rdi_doc_type("Frequentieregeling", "Regeling") == DocType.LEGISLATION
    assert rdi_doc_type("Jaarbericht", "Rapport") == DocType.GUIDANCE


def test_rdi_registration_and_resume_contract():
    assert ADAPTERS["nl-rdi"] is RDIDocumentsAdapter
    assert SOURCE_INFO["nl-rdi"].jurisdiction == "NL"
    assert INCREMENTAL_MODE["nl-rdi"] == "early-stop"
    assert RDIDocumentsAdapter(client=object(), start_offset="20").start_offset == 10
