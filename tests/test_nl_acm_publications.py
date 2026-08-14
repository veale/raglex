from __future__ import annotations

from datetime import date

from raglex.adapters.nl_acm_guidance import (
    ACMLegalPublicationsAdapter, _legal_doc_type, legal_ajax_html, legal_detail,
    legal_rss_stubs, legal_stubs,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.core.models import DocType


RESULTS = """<div class='js-view-dom-id-raglex'>
<div class='m-card'><h2 class='m-card__title'><a href='/nl/publicaties/besluit-a'>Besluit A</a></h2>
<div class='m-card__meta'><span>Publicatie</span><span>Besluit</span><span>13-08-2026</span></div>
<div class='m-card__body'>Samenvatting</div></div>
<div class='m-card'><h2 class='m-card__title'><a href='/nl/publicaties/uitspraak-a'>Uitspraak A</a></h2>
<div class='m-card__meta'><span>Publicatie</span><span>Gerechtelijke uitspraak</span><span>12-08-2026</span></div></div>
<div class='m-pager__item--last'><a href='?page=665'>666</a></div></div>"""


def test_ajax_result_parser_and_bounded_page_count():
    payload = [{"command": "insert", "selector": ".js-view-dom-id-raglex", "data": RESULTS}]
    html = legal_ajax_html(payload)
    rows, pages = legal_stubs(html)
    assert pages == 666
    assert [x.hints["publication_type"] for x in rows] == ["Besluit", "Gerechtelijke uitspraak"]
    assert rows[0].stable_id == "nl/acm/publication/besluit-a"
    assert rows[0].hint_date == date(2026, 8, 13)


def test_official_rss_is_the_watch_surface():
    xml = """<rss><channel><item><title>Besluit A</title>
    <link>https://www.acm.nl/nl/publicaties/besluit-a</link>
    <description>Samenvatting</description><pubDate>Thu, 13 Aug 2026 10:30:00 +0200</pubDate>
    </item></channel></rss>"""
    rows = legal_rss_stubs(xml)
    assert rows[0].stable_id == "nl/acm/publication/besluit-a"
    assert rows[0].hint_date == date(2026, 8, 13)


def test_detail_keeps_pdf_titles_and_external_ecli():
    html = """<meta name='dcterms.type' content='Gerechtelijke uitspraak'><main><h1>Uitspraak CBb</h1>
    <a href='/files/a.pdf'>Volledige uitspraak (PDF - 20 kB)</a>
    <a href='https://deeplink.rechtspraak.nl/uitspraak?id=ECLI:NL:CBB:2026:354'>Rechtspraak</a>
    <p>Het College heeft uitspraak gedaan.</p></main>"""
    got = legal_detail(html)
    assert got["publication_type"] == "Gerechtelijke uitspraak"
    assert got["files"] == [{"url": "https://www.acm.nl/files/a.pdf", "title": "Volledige uitspraak"}]
    assert got["eclis"] == ["ECLI:NL:CBB:2026:354"]
    assert "College" in got["body"]


def test_legal_types_and_registration():
    assert _legal_doc_type("Besluit") == DocType.DECISION
    assert _legal_doc_type("Beslissing op bezwaar") == DocType.DECISION
    assert _legal_doc_type("Visie en opinie") == DocType.OPINION
    assert _legal_doc_type("Gerechtelijke uitspraak") == DocType.JUDGMENT
    assert ADAPTERS["nl-acm-publications"] is ACMLegalPublicationsAdapter
    assert SOURCE_INFO["nl-acm-publications"].jurisdiction == "NL"
    assert INCREMENTAL_MODE["nl-acm-publications"] == "early-stop"
    assert ACMLegalPublicationsAdapter(client=object(), start_offset="20").start_offset == 10
