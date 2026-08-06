"""Pluggable page fetchers for the scraping tier (§5a, §1.6).

Most of the EU Tier-3 long tail and many regulator sites have no API: legacy
portals, JS-heavy SPAs, and — increasingly — anti-bot walls (Cloudflare, WAF
challenges, TLS fingerprinting). So fetching sits behind a small interface with
several backends, chosen by config, all routed through the configured proxy:

- ``httpx`` — fast, cheap, low-memory; the default for plain HTML. Detects a
  WAF/anti-bot block (403/429) and raises ``RateLimitException`` so the
  orchestrator can pause the queue or the operator can escalate.
- ``stealth`` — **Scrapling's StealthyFetcher (Camoufox)**: a real, fingerprint-
  randomised Firefox that bypasses most anti-bot systems. The answer to "httpx
  gets blocked". Heavy; gate it.
- ``playwright`` — headless Chromium for genuinely client-rendered SPAs.

The stealth/playwright backends are optional (`pip install 'raglex[scrape]'`
brings Scrapling) and lazily imported, so the core stays dependency-light. The
boundary is the §5a quarantine rule: messy, fragile fetching in; a clean
``FetchedPage`` out — the rest of the pipeline never learns how the bytes arrived.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..core.errors import RateLimitException
from ..core.http import RateLimitedClient, get_proxy

# Statuses that usually mean an anti-bot/WAF wall rather than a missing page.
_BLOCK_STATUSES = frozenset({403, 429, 503})


@dataclass(slots=True)
class FetchedPage:
    url: str
    status: int
    html: str
    final_url: str | None = None
    engine: str = "httpx"


@runtime_checkable
class Fetcher(Protocol):
    name: str

    def fetch(self, url: str, *, headers: dict | None = None) -> FetchedPage: ...

    def close(self) -> None: ...


class HttpxFetcher:
    """Plain, paced, proxy-aware HTTP (the fast path). Raises ``RateLimitException``
    on a 403/429/503 wall so the caller can pause or escalate to ``stealth``."""

    name = "httpx"

    def __init__(self, source: str = "scrape", *, min_interval: float = 1.0, proxy: str | None = None) -> None:
        self._client = RateLimitedClient(source, min_interval=min_interval, proxy=proxy)
        self.source = source

    def fetch(self, url: str, *, headers: dict | None = None) -> FetchedPage:
        resp = self._client.get(url, headers=headers, raise_for_4xx=False)
        if resp.status_code in _BLOCK_STATUSES:
            # likely anti-bot — surface it so the orchestrator pauses / escalates
            raise RateLimitException(self.source)
        return FetchedPage(
            url=url, status=resp.status_code, html=resp.text,
            final_url=str(resp.url), engine=self.name,
        )

    def close(self) -> None:
        self._client.close()


class StealthyFetcher:
    """Scrapling StealthyFetcher (Camoufox) — anti-bot bypass. Lazy-imported."""

    name = "stealth"

    def __init__(self, *, proxy: str | None = None, headless: bool = True,
                 network_idle: bool = True, timeout_ms: float | None = None) -> None:
        import os

        self.proxy = proxy if proxy is not None else get_proxy()
        self.headless = headless
        self.network_idle = network_idle
        # A browser fetch inside a harvest loop MUST be bounded. Left to its own devices
        # this waited on network-idle, and a Cloudflare interstitial that keeps re-polling
        # its own challenge never goes idle — so the call simply did not return, and the
        # worker thread could not be killed (see jobs.STALL_SECONDS: we can flag a frozen
        # job, we cannot free it).
        self.timeout_ms = float(timeout_ms if timeout_ms is not None
                                else os.environ.get("RAGLEX_STEALTH_TIMEOUT_MS") or 90000)

    def fetch(self, url: str, *, headers: dict | None = None) -> FetchedPage:
        try:
            from scrapling.fetchers import StealthyFetcher as _SF
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "stealth scraping needs Scrapling + Camoufox: "
                "pip install 'raglex[scrape]' && scrapling install"
            ) from exc
        try:
            page = _SF.fetch(
                url, headless=self.headless, network_idle=self.network_idle,
                proxy=self.proxy, timeout=self.timeout_ms,
            )
        except TypeError:
            # older scrapling: no timeout kwarg. Better an unbounded call than none at
            # all, but say so — this is the shape that froze a backfill.
            page = _SF.fetch(
                url, headless=self.headless, network_idle=self.network_idle,
                proxy=self.proxy,
            )
        html = _page_html(page)
        return FetchedPage(
            url=url, status=getattr(page, "status", 200), html=html,
            final_url=getattr(page, "url", url), engine=self.name,
        )

    def close(self) -> None:  # pragma: no cover
        pass


class PlaywrightFetcher:
    """Headless Chromium via Scrapling's PlayWright fetcher — for JS-rendered SPAs.
    Lazy-imported; heavy, so the orchestrator serialises ``requires_js`` adapters."""

    name = "playwright"

    def __init__(self, *, proxy: str | None = None, headless: bool = True) -> None:
        self.proxy = proxy if proxy is not None else get_proxy()
        self.headless = headless

    def fetch(self, url: str, *, headers: dict | None = None) -> FetchedPage:
        try:
            from scrapling.fetchers import PlayWrightFetcher as _PF
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "JS scraping needs Scrapling + Playwright: "
                "pip install 'raglex[scrape]' && playwright install chromium"
            ) from exc
        page = _PF.fetch(url, headless=self.headless, network_idle=True, proxy=self.proxy)
        return FetchedPage(
            url=url, status=getattr(page, "status", 200), html=_page_html(page),
            final_url=getattr(page, "url", url), engine=self.name,
        )

    def close(self) -> None:  # pragma: no cover
        pass


def _page_html(page) -> str:  # noqa: ANN001
    """Scrapling's response object exposes HTML under a few names across versions."""
    for attr in ("html_content", "body", "content"):
        val = getattr(page, attr, None)
        if isinstance(val, str) and val:
            return val
    return str(page)


class ScraplingMcpFetcher:
    """Fetch via a **scrapling-MCP service** (a shared Scrapling/Camoufox instance behind an
    MCP endpoint) instead of running a browser in-process. Preferred when one is deployed
    alongside — the raglex image needn't ship a browser — and it **falls back to in-process
    Camoufox** (:class:`StealthyFetcher`) when the service is unreachable, so a scrape never
    hard-fails just because the shared instance is down.

    Config: ``RAGLEX_SCRAPLING_MCP_URL`` (e.g. ``http://scrapling-mcp:8000/mcp``) + optional
    ``RAGLEX_SCRAPLING_MCP_KEY``. The service is expected to expose a fetch tool
    (``stealthy_fetch`` / ``fetch`` / ``get``) returning the page HTML."""

    name = "scrapling-mcp"

    def __init__(self, *, url: str | None = None, api_key: str | None = None,
                 proxy: str | None = None) -> None:
        import os

        self.url = url or os.environ.get("RAGLEX_SCRAPLING_MCP_URL")
        self.api_key = api_key or os.environ.get("RAGLEX_SCRAPLING_MCP_KEY")
        self.proxy = proxy
        # Ceiling for ONE page, across every attempt. A harvest is a long loop of these,
        # so a page that cannot be fetched has to fail in bounded time or it stops the run.
        self.deadline = float(os.environ.get("RAGLEX_STEALTH_DEADLINE") or 240)
        self._client = None       # reused across pages: one MCP handshake, not one per try
        self._tool: str | None = None      # the fetch tool this service actually exposes
        self._fallback: "StealthyFetcher | None" = None

    def _mcp_fetch(self, url: str) -> str | None:
        """Fetch one URL through the scrapling MCP service, under a WHOLE-CALL deadline.

        The per-request timeout alone did not bound this. The loop tries four tool names,
        each twice (full args, then url-only), so one page could spend 8 × 180s = 24
        minutes before returning — and every attempt built a fresh ``MCPToolClient``, so
        each also paid a fresh MCP handshake. A committee backfill parked on exactly this:
        the worker kept heartbeating (so it was never reaped as stalled) while no item
        completed for over half an hour.

        The tool-name fan-out exists because the service's fetch tool has been named
        differently across versions; it is discovery, not retry, so once one name works we
        remember it and stop probing the rest.
        """
        import time

        from ..embeddings.remote import MCPToolClient

        deadline = time.monotonic() + self.deadline
        if self._client is None:
            self._client = MCPToolClient(self.url, token=self.api_key,
                                         timeout=min(self.deadline, 180))
        # Scrapling's stealthy_fetch solves Cloudflare and returns raw HTML; extraction_type
        # html keeps it as markup (not markdown), which the HoL parser needs.
        args = {"url": url, "extraction_type": "html", "solve_cloudflare": True, "timeout": 120000}
        tools = ((self._tool,) if self._tool
                 else ("stealthy_fetch", "fetch", "get", "scrape"))
        last_exc: Exception | None = None
        for tool in tools:
            for arguments in (args, {"url": url}):
                if time.monotonic() >= deadline:
                    if last_exc:
                        raise last_exc
                    return None
                try:
                    res = self._client.call_tool(tool, arguments)
                except Exception as exc:  # noqa: BLE001 — wrong tool/arg shape; try the next
                    last_exc = exc
                    continue
                html = _extract_html(res)
                if html:
                    self._tool = tool      # this service's name for it; stop probing
                    return html
        if last_exc:
            raise last_exc
        return None

    def fetch(self, url: str, *, headers: dict | None = None) -> FetchedPage:
        if self.url:
            try:
                html = self._mcp_fetch(url)
                if html:
                    return FetchedPage(url=url, status=200, html=html, engine=self.name)
            except Exception:  # noqa: BLE001 — fall back to the in-process browser
                pass
        if self._fallback is None:
            self._fallback = StealthyFetcher(proxy=self.proxy)
        return self._fallback.fetch(url, headers=headers)

    def close(self) -> None:  # pragma: no cover
        if self._fallback:
            self._fallback.close()


def _extract_html(res) -> str | None:
    """Pull the page HTML out of a scrapling-MCP tool result, whatever the shape: a bare
    string, ``{html|body|text: str}``, or ``{content: [str]|str}`` (scrapling wraps the
    markup in a ``content`` list alongside status/url)."""
    if isinstance(res, str):
        return res or None
    if not isinstance(res, dict):
        return None
    for key in ("html", "body", "text"):
        val = res.get(key)
        if isinstance(val, str) and val:
            return val
    content = res.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str) and item:
                return item
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return item["text"]
    return None


_FETCHERS = {
    "httpx": HttpxFetcher, "stealth": StealthyFetcher,
    "playwright": PlaywrightFetcher, "scrapling-mcp": ScraplingMcpFetcher,
}


def get_fetcher(
    name: str | None = None, *, source: str = "scrape", min_interval: float = 1.0,
    proxy: str | None = None, requires_js: bool = False,
) -> Fetcher:
    """Build the configured fetcher. ``requires_js`` forces at least Playwright; an explicit
    ``name`` (or ``RAGLEX_SCRAPER``) overrides. When ``stealth`` is requested and a
    scrapling-MCP service is configured, that service is used (with an in-process Camoufox
    fallback) so the image needn't ship a browser."""
    import os

    chosen = name or os.environ.get("RAGLEX_SCRAPER") or ("playwright" if requires_js else "httpx")
    if chosen == "stealth" and os.environ.get("RAGLEX_SCRAPLING_MCP_URL"):
        chosen = "scrapling-mcp"
    cls = _FETCHERS.get(chosen, HttpxFetcher)
    if cls is HttpxFetcher:
        return cls(source, min_interval=min_interval, proxy=proxy)
    return cls(proxy=proxy)
