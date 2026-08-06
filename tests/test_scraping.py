from __future__ import annotations

from datetime import date

import pytest

from raglex.core.errors import RateLimitException
from raglex.core.models import DocType
from raglex.scraping import (
    FetchedPage,
    HttpxFetcher,
    RecipeScrapeAdapter,
    ScrapeRecipe,
    get_fetcher,
    parse_detail,
    parse_listing,
)

RECIPE = ScrapeRecipe(
    source="test-reg",
    base_url="https://reg.example",
    listing_url="https://reg.example/decisions/",
    item_link_selector="a.decision",
    title_selector="h1",
    date_selector="time",
    body_selector="div.body",
    next_page_selector="a.next",
    doc_type=DocType.DECISION,
    court="REG",
)

LISTING = """
<html><body>
  <a class="decision" href="/decisions/2024-001">Fine against Acme</a>
  <a class="decision" href="/decisions/2024-002">Reprimand of Beta</a>
  <a class="decision" href="/decisions/2024-001">dup link</a>
  <a class="next" href="/decisions/?page=2">Next</a>
</body></html>
"""

DETAIL = """
<html><body>
  <h1>Monetary penalty against Acme Ltd</h1>
  <time>14 March 2024</time>
  <div class="body"><p>The Commissioner imposes a fine for processing personal data unlawfully.</p></div>
</body></html>
"""


# -- pure parsing -----------------------------------------------------------
def test_parse_listing_dedupes_and_resolves_urls():
    items, next_url = parse_listing(LISTING, RECIPE)
    assert [i.url for i in items] == [
        "https://reg.example/decisions/2024-001",
        "https://reg.example/decisions/2024-002",
    ]
    assert items[0].title == "Fine against Acme"
    assert next_url == "https://reg.example/decisions/?page=2"


def test_parse_detail_extracts_title_date_body():
    d = parse_detail(DETAIL, RECIPE)
    assert d.title == "Monetary penalty against Acme Ltd"
    assert d.decision_date == date(2024, 3, 14)
    assert "personal data unlawfully" in d.text


# -- httpx fetcher: anti-bot detection -------------------------------------
class _Resp:
    def __init__(self, status, text="", url="http://x"):
        self.status_code = status
        self.text = text
        self.url = url


class _FakeHttpClient:
    def __init__(self, resp):
        self._resp = resp

    def request(self, method, url, **kw):
        return self._resp

    def close(self):
        pass


def _fetcher_with(resp):
    from raglex.core.http import RateLimitedClient

    f = HttpxFetcher.__new__(HttpxFetcher)
    f.source = "test-reg"
    f._client = RateLimitedClient("test-reg", client=_FakeHttpClient(resp), sleep=lambda s: None)
    return f


def test_httpx_fetcher_returns_page():
    page = _fetcher_with(_Resp(200, "<html>ok</html>")).fetch("http://x")
    assert isinstance(page, FetchedPage) and page.status == 200 and "ok" in page.html


def test_httpx_fetcher_raises_on_waf_block():
    with pytest.raises(RateLimitException):
        _fetcher_with(_Resp(403, "blocked")).fetch("http://x")  # anti-bot wall → escalate


def test_get_fetcher_selects_backend():
    assert get_fetcher("httpx").name == "httpx"
    assert get_fetcher("stealth").name == "stealth"  # constructed, not yet run
    assert get_fetcher(requires_js=True).name == "playwright"


# -- adapter quarantine: scraped source → standard Record -------------------
class _FakeFetcher:
    name = "fake"

    def __init__(self):
        self.pages = {
            RECIPE.listing_url: LISTING,
            "https://reg.example/decisions/2024-001": DETAIL,
            "https://reg.example/decisions/2024-002": DETAIL,
        }

    def fetch(self, url, *, headers=None):
        # only one listing page (no real ?page=2 content) → stop after first
        html = self.pages.get(url, DETAIL if "decisions/" in url else LISTING)
        return FetchedPage(url=url, status=200, html=html, engine="fake")

    def close(self):
        pass


def test_scrape_adapter_yields_records_like_any_source():
    ad = RecipeScrapeAdapter(RECIPE, fetcher=_FakeFetcher())
    stubs = list(ad.discover(None, max_pages=1))
    assert len(stubs) == 2 and stubs[0].court == "REG"

    rec = ad.fetch(stubs[0])
    assert rec.source == "test-reg"
    assert rec.doc_type == DocType.DECISION
    assert rec.extracted_via.value == "scrape"
    assert rec.payload_hash and "personal data unlawfully" in rec.text
    assert rec.extra["fetch_engine"] == "fake"  # how it was fetched stays inside


def test_scrape_adapter_registered():
    from raglex.adapters.registry import get_adapter

    ad = get_adapter("uk-ico")
    assert ad.source == "uk-ico" and ad.requires_js is False


# -- the browser fetcher, which must never be able to hang a harvest ---------
def test_a_wedged_browser_page_gives_up_and_is_thrown_away():
    """One stuck page must not become a stuck corpus.

    Bounding goto was not enough: launching the browser, opening a page, reading a body
    and closing a page all block too, and the lock that serialises them turns any one of
    those into a stall of every later fetch. A committee harvest sat 15 minutes on one
    paper at 0.4% CPU exactly that way, reporting a live worker throughout.
    """
    import time

    from raglex.scraping.fetcher import BrowserBytesFetcher

    fetcher = BrowserBytesFetcher()
    fetcher.hard_deadline = 1.0
    started = time.time()
    # a job that would never return, i.e. a wedged Playwright call
    assert fetcher._run("wedge", "https://x.example/a.pdf",
                        lambda: time.sleep(30)) is None
    assert time.time() - started < 10, "the deadline did not fire"
    # the browser it was using is discarded, not handed to the next caller
    assert fetcher._browser is None and fetcher._pool is None
    # …and the fetcher still works afterwards
    assert fetcher._run("ok", "https://x.example/b.pdf", lambda: b"%PDF-") == b"%PDF-"


def test_a_missing_browser_is_a_miss_not_a_crash():
    from raglex.scraping.fetcher import BrowserBytesFetcher

    fetcher = BrowserBytesFetcher()
    assert fetcher._run("boom", "https://x.example/c.pdf",
                        lambda: (_ for _ in ()).throw(RuntimeError("no browser"))) is None


def test_a_recheck_that_finds_nothing_still_counts_as_a_check():
    """Every completed re-check must re-arm the backoff, including the empty ones.

    fetched_at is written only on a store, so a document that never changes is
    permanently overdue and re-downloaded on every run. note_refetch fixes that for the
    unchanged-bytes case; a re-check whose fetch returns None took the not_found branch
    and skipped it, so a decision whose rendition cannot be read at all kept the very
    loop the stamp exists to break.
    """
    import inspect

    from raglex.pipeline.runner import Pipeline

    src = inspect.getsource(Pipeline.run)
    not_found = src.index("stats.not_found += 1")
    following = src[not_found:not_found + 700]
    assert "note_refetch" in following, "an empty re-check leaves the backoff un-armed"
    # and it is scoped to a deliberate re-check, not every miss
    assert "if refreshed and held_id:" in following
