"""A29WP adapter — justice-index/newsroom-feed/item-page parsing (pure), WP-number
identity + dedup across surfaces, EN-PDF attachment picking, title casing. Network-free."""

from __future__ import annotations

import pytest

from raglex.adapters.a29wp import (
    A29WPAdapter,
    parse_justice_index,
    parse_newsroom_feed,
    parse_newsroom_item,
    pick_en_pdf,
    sentence_case,
)
from raglex.core.models import DocType

JUSTICE = """
<h2>2016</h2><ul>
 <li><a href="/justice/article-29/documentation/opinion-recommendation/files/2016/wp240_en.pdf"
        target="_blank" class="link-ico"><span>Opinion 03/2016 on the evaluation and review of the ePrivacy Directive</span><i></i></a></li>
 <li><a href="/justice/article-29/documentation/opinion-recommendation/files/2016/wp240_fr.pdf"><span>Avis 03/2016 (FR)</span></a></li>
</ul>
<h2>2015</h2><ul>
 <li><a href="/justice/article-29/documentation/opinion-recommendation/files/2015/wp179_en_update.pdf"><span>Update of Opinion 8/2010 on applicable law</span></a></li>
</ul>
"""

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
 <item><title>WP225 GUIDELINES ON THE IMPLEMENTATION OF THE CJEU JUDGMENT ON GOOGLE SPAIN C-131/12</title>
  <link>https://ec.europa.eu/newsroom/article29/redirection/item/667236/en</link>
  <pubDate>Fri, 24 Jan 2020 15:23:14 +0100</pubDate></item>
 <item><title>Press release on the plenary</title>
  <link>https://ec.europa.eu/newsroom/article29/redirection/item/612053/en</link></item>
</channel></rss>
"""

ITEM_PAGE = """
<h1 class="ecl">WP225 GUIDELINES ON THE IMPLEMENTATION OF THE CJEU JUDGMENT</h1>
<div>Downloads WP225_EN English (326 KB - PDF)
 <a download="" href="https://ec.europa.eu/newsroom/article29/redirection/document/64437">Download</a></div>
<div>WP225 Other languages versions Other (9.56 MB - ZIP)
 <a download="" href="https://ec.europa.eu/newsroom/article29/redirection/document/64438">Download</a></div>
"""


def test_parse_justice_index_dedupes_languages_and_keeps_years():
    docs = parse_justice_index(JUSTICE)
    assert [(d.stem, d.year) for d in docs] == [("wp240", 2016), ("wp179_update", 2015)]
    assert docs[0].pdf_url.endswith("/files/2016/wp240_en.pdf")
    assert docs[0].title.startswith("Opinion 03/2016 on the evaluation")


def test_parse_newsroom_feed_and_item_page():
    items = parse_newsroom_feed(FEED)
    assert [i.item_id for i in items] == ["667236", "612053"]
    assert items[0].page_url == "https://ec.europa.eu/newsroom/article29/items/667236/en"
    meta = parse_newsroom_item(ITEM_PAGE)
    assert meta["title"].startswith("WP225 GUIDELINES")
    assert len(meta["docs"]) == 2
    pick = pick_en_pdf(meta["docs"])
    assert pick["href"].endswith("/document/64437")  # the EN PDF, not the ZIP


def test_sentence_case_preserves_identity_tokens():
    assert sentence_case("WP225 GUIDELINES ON THE CJEU JUDGMENT C-131/12") == \
        "WP225 guidelines on the cjeu judgment C-131/12"
    # mixed-case titles pass through untouched
    assert sentence_case("Opinion 03/2016 on ePrivacy") == "Opinion 03/2016 on ePrivacy"


class _Resp:
    def __init__(self, content, status=200, ctype="text/html"):
        self.content = content if isinstance(content, bytes) else content.encode()
        self.status_code = status
        self.headers = {"content-type": ctype}


class _FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return _Resp(b"", 404)


def _routes(pdf=b"%PDF"):
    return {
        "opinion-recommendation/index_en.htm": _Resp(JUSTICE),
        "item_type_id=1360": _Resp(FEED),
        "items/667236/en": _Resp(ITEM_PAGE),
        "redirection/document/64437": _Resp(pdf, ctype="application/pdf"),
        "wp240_en.pdf": _Resp(pdf, ctype="application/pdf"),
        "wp179_en_update.pdf": _Resp(pdf, ctype="application/pdf"),
    }


def test_discover_merges_surfaces_and_dedupes_by_wp_number():
    ad = A29WPAdapter(client=_FakeClient(_routes()))
    stubs = list(ad.discover(None))
    ids = [s.stable_id for s in stubs]
    # justice docs by filename identity; newsroom WP225 by title; the second feed
    # item (no WP number) keys by item id — and only type feed 1360 was routed, so
    # the other six type feeds 404 and are skipped without sinking discovery
    assert ids == ["a29wp/wp240", "a29wp/wp179_update", "a29wp/wp225", "a29wp/item/612053"]
    wp240 = stubs[0]
    assert wp240.hints["aliases"] == ["wp240", "wp 240"]
    assert wp240.hint_date is not None and wp240.hint_date.year == 2016
    wp225 = stubs[2]
    assert wp225.hints["kind"] == "guidelines" and wp225.hints["doc_type"] == DocType.GUIDANCE
    assert wp225.title.startswith("WP225 guidelines")  # de-shouted


def _tiny_pdf(text: str) -> bytes:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    words = text.split()
    for i in range(0, len(words), 8):
        page.insert_text((72, 72 + 14 * (i // 8)), " ".join(words[i:i + 8]))
    return doc.tobytes()


def test_fetch_both_surfaces():
    pdf = _tiny_pdf("Article 29 Data Protection Working Party. Opinion adopted. " * 15)
    ad = A29WPAdapter(client=_FakeClient(_routes(pdf)))
    stubs = {s.stable_id: s for s in ad.discover(None)}

    just = ad.fetch(stubs["a29wp/wp240"])
    assert just.doc_type == DocType.GUIDANCE
    assert just.title == "Opinion 03/2016 on the evaluation and review of the ePrivacy Directive (WP240)"
    assert str(just.decision_date) == "2016-01-01" and just.extra["date_precision"] == "year"
    assert just.extra["aliases"] == ["wp240", "wp 240"]
    assert "Working Party" in (just.text or "")

    news = ad.fetch(stubs["a29wp/wp225"])
    assert news.title.startswith("WP225 guidelines")
    assert news.decision_date is None  # feed pubDate = upload date, deliberately unused
    assert news.extra["newsroom_kind"] == "guidelines"
    assert news.extra["other_files"][0]["href"].endswith("/document/64438")
    assert news.extra["aliases"] == ["wp225", "wp 225"]


def test_registry_wires_a29wp():
    from raglex.adapters.registry import get_adapter, source_catalog

    assert get_adapter("a29wp").source == "a29wp"
    cat = {s["key"]: s for s in source_catalog()}
    assert cat["a29wp"]["kind"] == "guidance"
    assert cat["a29wp"]["can_incremental"] is False  # closed archive — harvest once
