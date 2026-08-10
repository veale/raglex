"""EUIPO Observatory publications — the Algolia index and the PDFs one level down.

The whole listing is one public JSON index, so the parser is the adapter: what it gets
right or wrong about a hit decides whether a study is held under a stable id, dated,
and joined to the PDFs that actually contain it.
"""

from __future__ import annotations

import json
from datetime import date

from raglex.adapters.eu_euipo import (
    DEFAULT_MAX_PDFS,
    EUIPOPublicationsAdapter,
    parse_hits,
    pdf_label,
    pdf_links,
    published_on,
    slug_id,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.citations import extract_citations

PAGE = {
    "nbHits": 173, "nbPages": 9, "page": 0, "hitsPerPage": 20,
    "hits": [
        {
            "objectID": "a6f382dd-714d-48e1-8078-92be8de41db1",
            "fullSlug": "en/publications/euipn-trends-report-2025",
            "type": ["observatory-publications"],
            "category": "IP in the economy",
            "title": "EUIPN Trends Report 2025",
            "summary": "An overview of intellectual property activity across Europe.",
            "startDate": 1782835200000,
            "body": ["EUIPN Trends Report 2025", "30/06/2026",
                     "The report brings together harmonised filing data."],
        },
        {
            # The site is inconsistent about slug case; two spellings are one document.
            "fullSlug": "en/publications/IP-backed-finance-in-Europe",
            "category": "IP in the economy",
            "title": "IP-backed finance in Europe",
            "summary": "State of play and future perspectives.",
            "startDate": 1775631600000,
            "body": [],
        },
        {"fullSlug": "", "title": "no slug, no document"},
    ],
}

PUBLICATION_HTML = """
<main>
  <h1>Online Advertising on IPR-Infringing Websites and Apps 2025</h1>
  <a class="MuiButtonBase-root" role="button" aria-label="Download"
     href="https://euipo.europa.eu/tunnel-web/secure/webdav/guest/document_library/
observatory/documents/reports/OA_2025/OA_2025_FullR_en.pdf">Download</a>
  <a role="button"
     href="https://euipo.europa.eu/tunnel-web/secure/webdav/guest/document_library/OA_2025_ExSum_en.pdf">Summary</a>
  <a role="button"
     href="https://euipo.europa.eu/tunnel-web/secure/webdav/guest/document_library/OA_2025_ExSum_en.pdf">Summary again</a>
  <img src="https://a.storyblok.com/f/139646/cover.jpg"/>
</main>
""".replace("document_library/\nobservatory", "document_library/observatory")


def test_the_index_page_yields_one_publication_per_slug():
    pubs = parse_hits(PAGE)

    assert [p.stable_id for p in pubs] == [
        "euipo/euipn-trends-report-2025", "euipo/ip-backed-finance-in-europe"]
    first = pubs[0]
    assert first.published == date(2026, 6, 30)          # epoch ms → the date
    assert first.category == "IP in the economy"
    # the landing page's own prose, which the index already carries
    assert "harmonised filing data" in first.text
    assert first.title in first.text


def test_a_hit_without_a_slug_or_a_date_is_not_invented():
    assert slug_id("") is None and slug_id("/") is None
    assert published_on({}) is None
    assert published_on({"startDate": 0}) is None
    assert published_on({"startDate": "yesterday"}) is None


def test_every_linked_pdf_is_found_once_and_named():
    links = pdf_links(PUBLICATION_HTML)

    # de-duplicated, in page order, and the cover image is not a PDF
    assert links == [
        "https://euipo.europa.eu/tunnel-web/secure/webdav/guest/document_library/"
        "observatory/documents/reports/OA_2025/OA_2025_FullR_en.pdf",
        "https://euipo.europa.eu/tunnel-web/secure/webdav/guest/document_library/"
        "OA_2025_ExSum_en.pdf",
    ]
    assert pdf_label(links[0]) == "OA 2025 FullR en"


def test_discovery_walks_every_page_and_honours_the_watermark():
    """The index is not date-ordered, so ``since`` filters the stubs — it must never
    stop the walk, or a study published late is invisible for ever."""
    seen: list[int] = []

    class _Client:
        def request(self, method, url, *, headers=None, content=None):
            payload = json.loads(content.decode())
            seen.append(payload["page"])
            assert payload["facetFilters"][0] == "type:observatory-publications"

            class _R:
                @staticmethod
                def json():
                    return {**PAGE, "nbPages": 3, "page": payload["page"]}
            return _R()

    adapter = EUIPOPublicationsAdapter(client=_Client())
    stubs = list(adapter.discover(None))
    assert seen == [0, 1, 2]
    assert len(stubs) == 6                                  # 2 usable hits × 3 pages
    assert stubs[0].stable_id == "euipo/euipn-trends-report-2025"
    assert stubs[0].hint_date == date(2026, 6, 30)

    later = list(EUIPOPublicationsAdapter(client=_Client()).discover("2026-05-01"))
    assert {s.stable_id for s in later} == {"euipo/euipn-trends-report-2025"}
    assert seen == [0, 1, 2, 0, 1, 2]      # the watermark filtered, it did not truncate


def test_the_source_is_registered_as_a_full_walk_with_its_pdf_bound():
    assert ADAPTERS["eu-euipo"] is EUIPOPublicationsAdapter
    assert INCREMENTAL_MODE["eu-euipo"] == "full-walk"
    info = SOURCE_INFO["eu-euipo"]
    assert info.jurisdiction == "EU" and info.kind == "guidance"
    assert [o.name for o in info.options] == ["max_pdfs"]
    assert EUIPOPublicationsAdapter(max_pdfs="").max_pdfs == DEFAULT_MAX_PDFS
    assert EUIPOPublicationsAdapter(max_pdfs="4").max_pdfs == 4


def test_the_ip_acquis_resolves_by_the_names_these_studies_use():
    """An Observatory study cites the IP acquis by acronym and nickname, never by
    number. Unmapped, "Article 9(2) EUTMR" resolves to nothing — and the bare article
    then carries forward onto whatever regulation was named last."""
    found = {c.raw: c.candidate_id for c in extract_citations(
        "Article 9(2)(a) EUTMR and Article 8 IPRED, read with the Trade Secrets "
        "Directive, the InfoSoc Directive and Article 17 CDSM.")}
    assert found == {
        "Article 9(2)(a) EUTMR": "32017R1001",
        "Article 8 IPRED": "32004L0048",
        "Trade Secrets Directive": "32016L0943",
        "InfoSoc Directive": "32001L0029",
        "Article 17 CDSM": "32019L0790",
    }


def test_an_ambiguous_instrument_nickname_is_left_alone():
    """"the Designs Directive" was 98/71/EC until 2024 and (EU) 2024/2823 after it;
    "the Customs Regulation" is a different act in every field. A confident wrong edge
    is worse than no edge — the numeric form still resolves."""
    for phrase in ("the Designs Directive", "the Customs Regulation",
                   "the Enforcement Directive"):
        assert not [c for c in extract_citations(phrase) if c.candidate_id], phrase
