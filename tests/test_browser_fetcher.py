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


class _RedirectingPage(_Page):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def content(self):
        self.reads += 1
        if self.reads == 1:
            return CHALLENGE
        if self.reads == 2:
            raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
        return REAL


def test_browser_html_waits_for_cloudflare_to_replace_the_interstitial():
    page = _Page()
    fetcher = BrowserBytesFetcher(timeout_ms=10_000)
    fetcher._ensure = lambda: _Browser(page)
    assert _is_browser_challenge(CHALLENGE)
    assert not _is_browser_challenge(REAL)
    assert fetcher._fetch_html("https://example.test") == REAL
    assert page.waits == 2


def test_browser_html_tolerates_cloudflare_redirect_context_destruction():
    page = _RedirectingPage()
    fetcher = BrowserBytesFetcher(timeout_ms=10_000)
    fetcher._ensure = lambda: _Browser(page)
    assert fetcher._fetch_html("https://example.test") == REAL
    assert page.reads == 3


def test_dead_mcp_endpoint_is_not_retried_for_every_tool_and_page(monkeypatch):
    import httpx
    from raglex.scraping import fetcher as mod
    from raglex.embeddings import remote
    calls=[]
    class Remote:
        def __init__(self,*a,**kw):pass
        def call_tool(self,*a):
            calls.append(a)
            response=httpx.Response(401,request=httpx.Request('POST','https://scraper.test/mcp'))
            raise httpx.HTTPStatusError('invalid token',request=response.request,response=response)
    class Local:
        def available(self):return True
        def fetch_html(self,url):return REAL
    monkeypatch.setattr(remote,'MCPToolClient',Remote)
    monkeypatch.setattr(mod,'get_bytes_fetcher',lambda:Local())
    monkeypatch.setattr(mod.ScraplingMcpFetcher,'_unavailable_until',{})
    f=mod.ScraplingMcpFetcher(url='https://scraper.test/mcp',api_key='old')
    assert f.fetch('https://example.test/1').html==REAL
    assert f.fetch('https://example.test/2').html==REAL
    assert len(calls)==1
    # Credential rotation gets an immediate fresh attempt, not the old token's cooldown.
    g=mod.ScraplingMcpFetcher(url='https://scraper.test/mcp',api_key='new')
    g.fetch('https://example.test/3')
    assert len(calls)==2


def test_browser_failure_does_not_fan_out_as_tool_name_discovery(monkeypatch):
    from raglex.scraping import fetcher as mod
    from raglex.embeddings import remote
    calls=[]
    class Remote:
        def __init__(self,*a,**kw):pass
        def call_tool(self,*a):
            calls.append(a)
            raise RuntimeError('browser process limit reached')
    monkeypatch.setattr(remote,'MCPToolClient',Remote)
    f=mod.ScraplingMcpFetcher(url='https://scraper.test/mcp',api_key='key')
    import pytest
    with pytest.raises(RuntimeError,match='process limit'):
        f._mcp_fetch('https://example.test')
    assert len(calls)==1


def test_json_browser_session_closes_after_decode_failure(monkeypatch):
    from raglex.scraping.fetcher import ScraplingMcpFetcher
    from raglex.embeddings.remote import MCPToolClient
    calls = []
    def call(self, name, args):
        calls.append(name)
        if name == 'browser_evaluate':
            raise ValueError('invalid source JSON')
        return {}
    monkeypatch.setattr(MCPToolClient, 'call_tool', call)
    fetcher = ScraplingMcpFetcher(url='http://scrapling.example/mcp', api_key='test')
    import pytest
    with pytest.raises(ValueError, match='invalid source JSON'):
        fetcher.fetch_json('https://hudoc.example/app/query/results')
    assert calls == ['browser_open', 'browser_fetch', 'browser_evaluate', 'close_session']
