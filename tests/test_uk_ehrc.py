"""EHRC adapter — sitemap discovery (the lastmod pairing bug), the downloads panel,
the page's own dates, and the transport retry Cloudflare's resets need. Network-free."""

from __future__ import annotations

import pytest

from raglex.adapters.uk_ehrc import (
    EQUALITY_ACT_ID,
    EHRCAdapter,
    EHRCHTTP,
    is_content,
    parse_dates,
    parse_documents,
    parse_sitemap,
    stable_id,
)
from raglex.core.errors import FetchError, RateLimitException
from raglex.core.models import DocType

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.equalityhumanrights.com/</loc><changefreq>daily</changefreq></url>
<url><loc>https://www.equalityhumanrights.com/guidance/codes-practice/chapter-four</loc>
  <lastmod>2026-08-05T07:37Z</lastmod></url>
<url><loc>https://www.equalityhumanrights.com/our-work/our-research/coronavirus</loc>
  <lastmod>2023-10-26T12:05Z</lastmod>
  <xhtml:link rel="alternate" hreflang="cy" href="https://www.equalityhumanrights.com/cy/x"/></url>
<url><loc>https://www.equalityhumanrights.com/search?keys=</loc>
  <lastmod>2026-08-01T00:00Z</lastmod></url>
</urlset>
"""

PAGE = """
<html><body><main>
  <article class="article container" data-history-node-id="12627">
  <h1  class="heading">How coronavirus has affected equality and human rights</h1>
  <p  class="landing-header__date paragraph text--small">Published: <b>20 October 2020</b></p>
  <p  class="landing-header__date paragraph text--small">Last updated: <b>4 March 2021</b></p>
  <div class="article__section article__section--countries">
    <ul class="countries__list list list--inline">
      <li  class="countries__list-item countries__list-item--england text--small">England</li>
      <li  class="countries__list-item countries__list-item--scotland text--small">Scotland</li>
    </ul>
  </div>
  <div class="text-long"><p>This report summarises evidence about the pandemic.</p></div>
  <div id="document-download" class="article__section">
    <div  class="document document--pdf">
      <div  class="document__container"><div  class="document__content">
        <a class="link link--large" href="https://www.equalityhumanrights.com/sites/default/files/2022/report.pdf">
      How coronavirus has affected equality and human rights  </a>
        <p  class="document__details paragraph text--medium">PDF, 2.99 MB, 57 pages</p>
        <a class="link link--small" href="/our-work/our-research/effect-coronavirus">
      See alternative formats  </a>
      </div></div>
    </div>
    <div  class="document document--word">
      <div  class="document__container"><div  class="document__content">
        <a class="link link--large" href="/sites/default/files/2022/report.docx">Word version</a>
        <p  class="document__details paragraph text--medium">DOCX, 1.2 MB</p>
      </div></div>
    </div>
    <div  class="document document--excel">
      <div  class="document__container"><div  class="document__content">
        <a class="link link--large" href="/sites/default/files/2022/tables.xlsx">Data tables</a>
        <p  class="document__details paragraph text--medium">XLSX, 400 KB</p>
      </div></div>
    </div>
  </div>
  <div class="article__section article__section--found-on">
    <a  class="link"  href="/our-work/our-research/coronavirus">The parent report</a>
  </div>
  </article>
  <div class="social-share"><span>Share with Linkedin</span></div>
  <script>var t = 1;</script>
</main></body></html>
"""


def test_parse_sitemap_pairs_each_url_with_its_own_lastmod():
    """The home page carries no ``lastmod``; zipping the two element lists positionally
    would shift every date on the site by one entry."""
    entries = parse_sitemap(SITEMAP)
    assert len(entries) == 4
    assert entries[0].lastmod is None and entries[0].path == "/"
    assert entries[1].lastmod == "2026-08-05T07:37Z"
    assert entries[1].section == "guidance"
    assert entries[2].lastmod == "2023-10-26T12:05Z"
    assert entries[2].section == "our-work"


def test_is_content_drops_the_sites_furniture():
    entries = {e.path: e for e in parse_sitemap(SITEMAP)}
    assert not is_content(entries["/"])
    assert not is_content(entries["/search"])
    assert is_content(entries["/guidance/codes-practice/chapter-four"])


def test_parse_dates_reads_both_header_lines():
    published, updated = parse_dates(PAGE)
    assert str(published) == "2020-10-20" and str(updated) == "2021-03-04"


def test_parse_documents_reads_every_format_in_the_panel():
    docs = parse_documents(PAGE)
    assert [d.ext for d in docs] == ["pdf", "docx", "xlsx"]
    assert docs[0].details == "PDF, 2.99 MB, 57 pages"
    assert docs[0].title.startswith("How coronavirus")
    assert docs[1].url == ("https://www.equalityhumanrights.com/sites/default/files/"
                           "2022/report.docx")


def test_stable_id_is_the_site_path():
    assert stable_id("https://www.equalityhumanrights.com/guidance/codes-practice/x") \
        == "ehrc/guidance/codes-practice/x"


class _FakeHTTP:
    def __init__(self, *, fail: set[str] | None = None):
        self.urls: list[str] = []
        self.fail = fail or set()

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url in self.fail:
            raise FetchError("boom", transient=True)
        if url.endswith("sitemap.xml"):
            return SITEMAP.encode()
        if url.endswith(".pdf"):
            return b"%PDF-1.7 fake"
        if url.endswith(".xlsx"):
            raise AssertionError("a spreadsheet must never be downloaded for text")
        if url.endswith(".docx"):
            return b"PK\x03\x04 not really a docx"
        return PAGE.encode()


def test_discover_filters_furniture_and_honours_the_lastmod_cursor():
    adapter = EHRCAdapter(http=_FakeHTTP())
    all_stubs = list(adapter.discover(None))
    assert [s.stable_id for s in all_stubs] == [
        "ehrc/guidance/codes-practice/chapter-four",
        "ehrc/our-work/our-research/coronavirus",
    ]
    assert all_stubs[0].hints["contenthash"] == "2026-08-05T07:37Z"
    # a page whose lastmod has not moved past the cursor is not re-fetched
    fresh = list(adapter.discover("2026-01-01T00:00Z"))
    assert [s.stable_id for s in fresh] == ["ehrc/guidance/codes-practice/chapter-four"]


def test_discover_can_be_limited_to_one_section():
    adapter = EHRCAdapter(section="guidance", http=_FakeHTTP())
    assert [s.hints["section"] for s in adapter.discover(None)] == ["guidance"]


def test_unknown_section_is_refused():
    with pytest.raises(ValueError):
        EHRCAdapter(section="not-a-section")


def test_fetch_inlines_readable_downloads_and_records_the_page_metadata():
    http = _FakeHTTP()
    adapter = EHRCAdapter(http=http)
    stub = list(adapter.discover(None))[1]
    record = adapter.fetch(stub)

    assert record.doc_type is DocType.GUIDANCE
    assert record.court == "Equality and Human Rights Commission"
    assert str(record.decision_date) == "2020-10-20"
    assert record.extra["updated"] == "2021-03-04"
    assert record.extra["node_id"] == "12627"
    assert record.extra["countries"] == ["england", "scotland"]
    # the pair of pages carrying the same report in different formats stays linked
    assert record.extra["alternative_formats"].endswith("/effect-coronavirus")
    assert record.extra["found_on"].endswith("/our-work/our-research/coronavirus")
    by_format = {a["format"]: a for a in record.extra["attachments"]}
    assert by_format["xlsx"]["skipped"] == "no-text-engine"
    assert set(by_format) == {"pdf", "docx", "xlsx"}
    assert "Share with Linkedin" not in record.text
    assert "england" in record.topic_tags


def test_a_page_that_declares_no_host_pins_no_instrument():
    """Pinning the Equality Act across the whole site is measurably wrong: on the
    Commission's human-rights material the bare provisions belong to the Human Rights
    Act 1998 and the Equality Act 2006, and a blanket default moves them off both."""
    record = EHRCAdapter(http=_FakeHTTP()).fetch(
        list(EHRCAdapter(http=_FakeHTTP()).discover(None))[1])
    assert "citation_default_instrument" not in record.extra


def test_a_code_of_practice_pins_the_act_it_declares():
    declaring = PAGE.replace(
        "<p>This report summarises evidence about the pandemic.</p>",
        "<p>This code of practice is guidance. The Equality Act 2010 (the Act) "
        "consolidates discrimination law. Section 9(1) of the Act defines race.</p>")

    class _Declaring(_FakeHTTP):
        def get(self, url, **kwargs):
            self.urls.append(url)
            if url.endswith("sitemap.xml"):
                return SITEMAP.encode()
            if url.endswith((".pdf", ".docx", ".xlsx")):
                return b""
            return declaring.encode()

    adapter = EHRCAdapter(include_documents=False, http=_Declaring())
    record = adapter.fetch(list(adapter.discover(None))[1])
    assert record.extra["citation_default_instrument"] == {"id": EQUALITY_ACT_ID,
                                                           "kind": "act"}


def test_http_retries_a_reset_connection_rather_than_losing_the_document():
    """Cloudflare resets the connection on a repeated large download instead of
    answering 429, so a transport failure must be retried, not treated as absent."""
    calls: list[str] = []
    slept: list[float] = []

    class _Flaky(EHRCHTTP):
        def _request(self, url, **kwargs):
            calls.append(url)
            if len(calls) < 3:
                raise ConnectionError("Recv failure: Connection reset by peer")
            return 200, b"ok", None

    http = _Flaky("test", min_interval=0, sleep=slept.append)
    assert http.get("https://example.invalid/x.pdf") == b"ok"
    assert len(calls) == 3 and slept                 # it backed off between attempts


def test_http_does_not_retry_a_real_404():
    calls: list[str] = []

    class _Missing(EHRCHTTP):
        def _request(self, url, **kwargs):
            calls.append(url)
            return 404, b"", None

    with pytest.raises(FetchError):
        _Missing("test", min_interval=0, sleep=lambda _s: None).get("https://x.invalid/y")
    assert len(calls) == 1


def test_a_burst_429_is_waited_out_rather_than_ending_the_run():
    """Cloudflare's burst limit is a wall the walk hits, not a standing quota: the
    first backfill was 429'd once after 185 pages and the page served normally
    afterwards. Raising it pauses the source and abandons the other 1,788."""
    calls: list[str] = []
    slept: list[float] = []

    class _Bursty(EHRCHTTP):
        def _request(self, url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return 429, b"", None
            return 200, b"ok", None

    http = _Bursty("test", min_interval=0, sleep=slept.append)
    assert http.get("https://x.invalid/y") == b"ok"
    assert slept == [EHRCHTTP.rate_limit_waits[0]]
    # a 429 must not spend a transport attempt — those are for reset connections
    assert len(calls) == 2


def test_a_429_that_never_clears_still_reaches_the_pipeline():
    slept: list[float] = []

    class _Walled(EHRCHTTP):
        def _request(self, url, **kwargs):
            return 429, b"", None

    with pytest.raises(RateLimitException):
        _Walled("test", min_interval=0, sleep=slept.append).get("https://x.invalid/y")
    assert slept == list(EHRCHTTP.rate_limit_waits)


def test_a_retry_after_header_is_honoured_over_the_default_backoff():
    slept: list[float] = []

    class _Polite(EHRCHTTP):
        def _request(self, url, **kwargs):
            if not slept:
                return 429, b"", 7.0
            return 200, b"ok", None

    assert _Polite("test", min_interval=0,
                   sleep=slept.append).get("https://x.invalid/y") == b"ok"
    assert slept == [7.0]
