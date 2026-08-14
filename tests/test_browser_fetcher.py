from raglex.scraping.fetcher import BrowserBytesFetcher, _is_browser_challenge


CHALLENGE = """<html><head><title>Just a moment...</title></head>
<script src="https://challenges.cloudflare.com/turnstile"></script></html>"""
REAL = "<html><head><title>Documents</title></head><body>AS/JUR (2026) 01</body></html>"


class _Page:
    def __init__(self):
        self.waits = 0

    def set_default_timeout(self, value):
        pass

    def set_default_navigation_timeout(self, value):
        pass

    def goto(self, *args, **kwargs):
        pass

    def content(self):
        return CHALLENGE if self.waits < 2 else REAL

    def wait_for_timeout(self, value):
        self.waits += 1

    def close(self):
        pass


class _Browser:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


def test_browser_html_waits_for_cloudflare_to_replace_the_interstitial():
    page = _Page()
    fetcher = BrowserBytesFetcher(timeout_ms=10_000)
    fetcher._ensure = lambda: _Browser(page)
    assert _is_browser_challenge(CHALLENGE)
    assert not _is_browser_challenge(REAL)
    assert fetcher._fetch_html("https://example.test") == REAL
    assert page.waits == 2
