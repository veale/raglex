"""Office for Students adapter — listing parsing, the chapter walk (and the selector
that once ate every chapter's text), the downloads panel, and the HERA edge.
Network-free."""

from __future__ import annotations

from raglex.adapters.uk_ofs import (
    HERA_ID,
    OfSPublicationsAdapter,
    child_pages,
    last_page,
    listing_total,
    parse_documents,
    parse_listing,
    parse_metadata,
    stable_id,
)
from raglex.core.models import DocType, RelationshipType

LISTING = """
<header class="row search-header">
  <div class="col-md-6">Your search returned 673 results</div>
</header>
<article class="event-listing-article">
  <div class="event-listing-article__body">
    <h2 class="event-listing-article__heading">
      <a href="/publications/gravity-assist/" class="event-listing-article__link">
        Consultation on the OfS&#x2019;s new free speech complaints scheme  </a></h2>
    <div class="event-listing-article__text">Our response to the consultation.</div>
    <div class="event-listing-article__date">
      <span class="event-listing-article__category">Consultations and their outcomes</span>
      05 Aug 2026
    </div>
  </div>
</article>
<article class="event-listing-article">
  <div class="event-listing-article__body">
    <h2 class="event-listing-article__heading">
      <a href="/publications/guide-to-funding/" class="event-listing-article__link">Guide to funding</a></h2>
    <div class="event-listing-article__text">How we manage funding.</div>
    <div class="event-listing-article__date">
      <span class="event-listing-article__category">Publications and letters for providers</span>
      30 Jul 2026
    </div>
  </div>
</article>
<div class="pagination-navigation"><ul class="pagination">
  <li class="pagination-container"><a href="?pg=2">Next</a></li>
  <li class="pagination-container"><a href="?pg=68">Last</a></li>
</ul></div>
"""

PARENT = """
<html><body><main id="main-content">
  <h1>Gravity assist</h1>
  <div class="publication-intro"><p>A review of digital teaching.</p></div>
  <div class="publication-category well">
    <dl class="publication-category__two-columns">
      <dt>Ref:</dt><dd>OfS 2026.38</dd>
      <dt>Date:</dt><dd>30 July 2026</dd>
    </dl>
  </div>
  <div class="publication-body">
    <nav class="row guide-navigation"><ol>
      <li><a href="/publications/gravity-assist/executive-summary/">Executive summary</a></li>
      <li><a href="/publications/gravity-assist/recommendations/">Recommendations</a></li>
    </ol></nav>
    <p>Read our <a href="/publications/gravity-assist/recommendations/">practical
      recommendations</a> and the <a href="/publications/other-report/">other report</a>
      and a <a href="/publications/gravity-assist/recommendations/deeper/">deeper page</a>.</p>
    <div class="document">
      <a class="document pdf" href="/media/abc/guide.pdf" title="Download the guide">
        <div class="document__heading">Guide to funding 2026-27</div>
        <div class="document__information">PDF, <span class="document__size">632Kb</span></div>
      </a>
    </div>
    <table><tr><td>
      <a rel="nofollow" href="/media/def/older.docx" target="_blank" class="document word">
        Guide to funding 2025-26 </a></td></tr></table>
    <table><tr><td>
      <a href="/media/ghi/data.xlsx" class="document excel">Dataset</a></td></tr></table>
  </div>
  <div class="links-of-interest"><a href="/publications/promo/">A promo block</a></div>
</main></body></html>
"""

CHAPTER = """
<html><body><main id="main-content">
  <h1 class="heading">Gravity assist</h1>
  <div class="col-md-8"><div class="guide-body">
    <div class="umb-block-grid__area-container">
      <div class="ofs__rich-text">
        <h2>Executive summary</h2>
        <p>The first national lockdown sparked a rush of activity in universities.</p>
      </div>
    </div>
  </div></div>
  <div class="social-share"><span>Share on Twitter</span></div>
</main></body></html>
"""


def test_parse_listing_reads_link_title_summary_category_and_date():
    rows = parse_listing(LISTING)
    assert [r.path for r in rows] == ["/publications/gravity-assist/",
                                      "/publications/guide-to-funding/"]
    assert rows[0].title.startswith("Consultation on the OfS’s new free speech")
    assert rows[0].category == "Consultations and their outcomes"
    assert str(rows[0].date) == "2026-08-05"
    assert rows[1].summary == "How we manage funding."


def test_listing_states_its_own_length():
    assert listing_total(LISTING) == 673
    assert last_page(LISTING) == 68


def test_parse_metadata_reads_the_ofs_reference_and_date():
    assert parse_metadata(PARENT) == {"ref": "OfS 2026.38", "date": "30 July 2026"}


def test_child_pages_are_direct_children_titled_from_the_parents_navigation():
    children = child_pages(PARENT, "/publications/gravity-assist/")
    # the nav label wins over the later prose link to the same chapter, and a
    # grandchild ("…/recommendations/deeper/") is not followed
    assert children == [("/publications/gravity-assist/executive-summary/",
                         "Executive summary"),
                        ("/publications/gravity-assist/recommendations/",
                         "Recommendations")]


def test_parse_documents_handles_both_attribute_orders_and_reads_the_format():
    docs = parse_documents(PARENT)
    assert [d.ext for d in docs] == ["pdf", "docx", "xlsx"]
    assert docs[0].title == "Guide to funding 2026-27"
    assert docs[0].info.startswith("PDF")
    assert docs[1].url.endswith("/media/def/older.docx")


def test_page_text_keeps_the_chapter_body_that_lives_inside_the_block_grid():
    """``.umb-block-grid__area-container`` is the chapter's own wrapper, so stripping
    it as a promo block cost every chapter its entire text."""
    text = OfSPublicationsAdapter()._page_text(CHAPTER)
    assert "first national lockdown" in text
    assert "Share on Twitter" not in text


def test_stable_id_drops_the_publications_prefix():
    assert stable_id("/publications/guide-to-funding/") == "ofs/guide-to-funding"


class _FakeClient:
    def __init__(self):
        self.urls: list[str] = []

    def get(self, url, headers=None):
        self.urls.append(url)

        class _Resp:
            content = b""

        resp = _Resp()
        if url.rstrip("/").endswith("/publications"):
            resp.content = LISTING.encode()
        elif url.endswith(".pdf"):
            resp.content = b"%PDF-1.7 fake"
        elif url.endswith(".xlsx"):
            raise AssertionError("a spreadsheet must never be downloaded for text")
        elif url.endswith(".docx"):
            resp.content = b"PK\x03\x04 not really a docx"
        elif "gravity-assist/" in url and url.rstrip("/").count("/") > 4:
            resp.content = CHAPTER.encode()
        else:
            resp.content = PARENT.encode()
        return resp


def test_discover_reads_the_listing_and_carries_the_category_forward():
    adapter = OfSPublicationsAdapter(client=_FakeClient())
    stubs = list(adapter.discover(None, max_pages=1))
    assert [s.stable_id for s in stubs] == ["ofs/gravity-assist",
                                            "ofs/guide-to-funding"]
    assert stubs[0].court == "Office for Students"
    assert stubs[0].hints["category"] == "Consultations and their outcomes"
    assert stubs[0].hints["feed_total"] == 673


def test_discover_stops_at_the_cursor():
    adapter = OfSPublicationsAdapter(client=_FakeClient())
    assert [s.stable_id for s in adapter.discover("2026-08-01")] == [
        "ofs/gravity-assist"]


def test_fetch_inlines_chapters_and_readable_downloads_and_links_to_hera():
    client = _FakeClient()
    adapter = OfSPublicationsAdapter(client=client)
    stubs = list(adapter.discover(None, max_pages=1))
    record = adapter.fetch(stubs[0])

    assert record.doc_type is DocType.DECISION       # a consultation outcome
    assert record.extra["ref"] == "OfS 2026.38"
    assert record.extra["aliases"] == ["OFS 2026.38"]
    assert record.extra["citation_default_instrument"] == {"id": HERA_ID, "kind": "act"}
    assert [(r.relationship_type, r.dst_id) for r in record.relations] == [
        (RelationshipType.INTERPRETS, HERA_ID)]
    assert [c["title"] for c in record.extra["chapters"]] == ["Executive summary",
                                                              "Recommendations"]
    assert "first national lockdown" in record.text
    by_format = {a["format"]: a for a in record.extra["attachments"]}
    assert by_format["xlsx"]["skipped"] == "no-text-engine"
    assert set(by_format) == {"pdf", "docx", "xlsx"}


def test_fetch_can_skip_chapters_and_downloads():
    client = _FakeClient()
    adapter = OfSPublicationsAdapter(client=client, include_child_pages=False,
                                     include_documents=False)
    record = adapter.fetch(next(iter(adapter.discover(None, max_pages=1))))
    assert "chapters" not in record.extra and "attachments" not in record.extra
    assert not any(u.endswith((".pdf", ".docx")) for u in client.urls)
