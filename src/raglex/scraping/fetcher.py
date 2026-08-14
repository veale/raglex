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

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..core.errors import RateLimitException
from ..core.http import RateLimitedClient, get_proxy

log = logging.getLogger("raglex.scraping.fetcher")

# Statuses that usually mean an anti-bot/WAF wall rather than a missing page.
_BLOCK_STATUSES = frozenset({403, 429, 503})


def _is_browser_challenge(html: str) -> bool:
    """A Cloudflare browser-check page, not merely a site that loads CF analytics.

    ``domcontentloaded`` fires before the challenge is solved. Capturing at that event
    made a healthy Camoufox session return the interstitial about eight seconds before
    Cloudflare replaced it with the real PACE page.
    """
    folded = (html or "")[:30000].casefold()
    return ("<title>just a moment" in folded
            or "challenges.cloudflare.com" in folded
            or "cf-chl-" in folded)


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


class BrowserBytesFetcher:
    """Camoufox fetch that returns the response BYTES, for files behind a JS challenge.

    The HTML-only stealth path cannot carry a PDF, and neither can the obvious
    workaround. Measured against publications.parliament.uk, whose committee reports are
    the case that forced this:

    * a plain client gets HTTP 403 (a Cloudflare interstitial, 5,860 bytes of markup);
    * ``context.request.get()`` from a browser that has ALREADY cleared the challenge on
      an HTML page in the same context still gets 403 — it is an XHR-class request, and
      the rule refuses it whatever cookie it carries;
    * a top-level NAVIGATION to the same URL returns 200 and 965,499 bytes beginning
      ``%PDF-``.

    So the working shape is: clear the challenge on an ordinary page, then navigate to
    the file and take the body off the response event. ``referer_url`` is the page to
    clear on — for a committee paper, its own HTML report — and going through it is what
    makes the navigation look like a reader clicking the download link.

    The browser is expensive to start (a second or two) and is therefore kept alive
    between calls, behind a lock: the Camoufox sync API is not thread-safe and a harvest
    may be running several adapters at once.

    EVERY WAY OUT OF HERE IS TIMED, because the lock makes one hang everybody's hang.
    Bounding ``goto`` was not enough: starting the browser, opening a page, reading a
    response body and closing a page are all calls that can block indefinitely, and while
    one thread is stuck inside the lock every other fetch queues behind it forever. A
    committee harvest sat 15 minutes on one paper at 0.4% CPU that way — a wedged browser
    reported as a live worker, which is the same failure that started this whole day. So
    the work runs on a single owned thread under a hard deadline, and a deadline that
    expires discards the browser rather than trusting it again.
    """

    name = "browser-bytes"

    def __init__(self, *, headless: bool = True, timeout_ms: float | None = None) -> None:
        import os
        import threading

        self.headless = headless
        self.timeout_ms = float(timeout_ms if timeout_ms is not None
                                else os.environ.get("RAGLEX_BROWSER_TIMEOUT_MS") or 90000)
        # The ceiling for one page across everything: launch, navigate, read, close.
        # Comfortably above timeout_ms so a normal slow page fails on its own timeout with
        # a useful message, and this only catches a genuine wedge.
        self.hard_deadline = float(
            os.environ.get("RAGLEX_BROWSER_HARD_DEADLINE") or (self.timeout_ms / 1000) * 3)
        self._browser = None
        self._ctx = None
        self._lock = threading.Lock()
        self._pool = None

    def available(self) -> bool:
        """Whether this image actually ships the browser (the app runs without it)."""
        try:
            import camoufox.sync_api  # noqa: F401
        except Exception:  # noqa: BLE001 — any import failure means "not installed"
            return False
        return True

    def _ensure(self):
        if self._browser is None:
            from camoufox.sync_api import Camoufox

            self._ctx = Camoufox(headless=self.headless, geoip=True)
            self._browser = self._ctx.__enter__()
        return self._browser

    def _run(self, what: str, url: str, job):
        """Run one browser job under a hard deadline, serialised, never blocking forever.

        Playwright's sync API is thread-affine, so the browser is owned by one worker
        thread and every job is handed to it. If that thread wedges, the deadline expires
        here and the browser is abandoned — the next call builds a fresh one rather than
        queueing behind a process that is never coming back.
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout

        # If another page is mid-flight, wait only as long as it is allowed to take.
        if not self._lock.acquire(timeout=self.hard_deadline):
            log.warning("browser-bytes: gave up waiting for the browser to free up (%s)", url)
            return None
        try:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1,
                                                thread_name_prefix="browser-bytes")
            future = self._pool.submit(job)
            try:
                return future.result(timeout=self.hard_deadline)
            except _Timeout:
                # The worker thread is stuck inside Playwright and cannot be cancelled.
                # Drop the browser AND the thread that owns it; both are unusable now.
                log.warning("browser-bytes: %s exceeded %ss on %s — discarding the browser",
                            what, int(self.hard_deadline), url)
                self._discard()
                return None
            except Exception:  # noqa: BLE001 — a page that will not load is a miss
                log.warning("browser-bytes: %s failed for %s", what, url, exc_info=True)
                return None
        finally:
            self._lock.release()

    def _discard(self) -> None:
        """Abandon a wedged browser and its owner thread; the next call starts clean."""
        pool, ctx = self._pool, self._ctx
        self._pool = self._browser = self._ctx = None
        for closer in (lambda: ctx.__exit__(None, None, None) if ctx else None,
                       lambda: pool.shutdown(wait=False, cancel_futures=True) if pool else None):
            try:
                closer()
            except Exception:  # noqa: BLE001 — best effort; it is already broken
                pass

    def fetch_bytes(self, url: str, *, referer_url: str | None = None) -> bytes | None:
        """The bytes at ``url``, or None. Never raises, and never blocks indefinitely."""
        return self._run("fetch_bytes", url, lambda: self._fetch_bytes(url, referer_url))

    def _fetch_bytes(self, url: str, referer_url: str | None) -> bytes | None:
        try:
            browser = self._ensure()
            page = browser.new_page()
            # every Playwright call on this page is bounded, not just goto
            page.set_default_timeout(self.timeout_ms)
            page.set_default_navigation_timeout(self.timeout_ms)
        except Exception:  # noqa: BLE001 — no browser in this image, or launch failed
            log.warning("browser-bytes: cannot start Camoufox for %s", url, exc_info=True)
            self._discard()
            return None
        captured: dict = {}

        def _on_response(resp):
            # The navigation's FINAL response, not a hop on the way to it and not a
            # sub-resource of the PDF viewer. Matching the requested URL looked right
            # and was wrong: the older Lords papers are linked as http://www.… and
            # redirect, so the only response whose URL matched was the 301 — captured
            # with an empty body, reported as a miss, and the report never read.
            try:
                if not resp.request.is_navigation_request() or resp.status >= 300:
                    return
                captured["status"] = resp.status
                captured["body"] = resp.body()
                captured["url"] = resp.url
            except Exception:  # noqa: BLE001 — body already streamed away
                pass

        try:
            if referer_url:
                # clear the challenge on an ordinary page first; a direct hit on the
                # file is what gets refused
                page.goto(referer_url, wait_until="domcontentloaded",
                          timeout=self.timeout_ms)
            page.on("response", _on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            except Exception:  # noqa: BLE001 — a download aborts the navigation
                pass
            page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001
            log.warning("browser-bytes: navigation failed for %s", url)
            return None
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        body = captured.get("body")
        status = captured.get("status")
        if not body or (status and status >= 400):
            log.warning("browser-bytes: %s returned status=%s bytes=%s",
                        url, status, len(body) if body else 0)
            return None
        return body

    def fetch_html(self, url: str) -> str | None:
        """The rendered HTML at ``url`` after any challenge, or None.

        The local fallback for the shared scraping service, and the first one that has
        ever actually run. Scrapling's own StealthyFetcher cannot be it: it imports
        ``patchright`` (a patched Playwright fork) which this image does not ship, so for
        four days — while the shared service answered every request with EAGAIN — nine
        adapters fell back to an ImportError. Camoufox is what that fetcher drives
        underneath anyway, so drive it directly and drop the broken indirection.
        """
        return self._run("fetch_html", url, lambda: self._fetch_html(url))

    def _fetch_html(self, url: str) -> str | None:
        import time

        try:
            browser = self._ensure()
            page = browser.new_page()
            page.set_default_timeout(self.timeout_ms)
            page.set_default_navigation_timeout(self.timeout_ms)
        except Exception:  # noqa: BLE001
            log.warning("browser-bytes: cannot start Camoufox for %s", url)
            self._discard()
            return None
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            deadline = time.monotonic() + self.timeout_ms / 1000
            while True:
                html = page.content()
                if not _is_browser_challenge(html) or time.monotonic() >= deadline:
                    return html
                # A normal CF managed challenge takes roughly 5–10 seconds. This loop is
                # bounded by the existing per-page timeout and the outer hard deadline.
                page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001
            log.warning("browser-bytes: html navigation failed for %s", url)
            return None
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self._discard()


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
                log.warning("scrapling-mcp: no html for %s; trying the local browser", url)
            except Exception as exc:  # noqa: BLE001 — fall back to the local browser
                log.warning("scrapling-mcp: %s failed (%s); trying the local browser",
                            url, str(exc)[:160])
        # The shared service can be down or wedged — it spent four days answering every
        # request "[Errno 11] Resource temporarily unavailable" — so this fallback has to
        # be real. Camoufox directly, not Scrapling's StealthyFetcher, which needs a
        # patchright this image does not ship and so could only ever raise.
        local = get_bytes_fetcher()
        if local.available():
            html = local.fetch_html(url)
            if html:
                return FetchedPage(url=url, status=200, html=html, engine="browser-local")
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

#: One browser for the whole process. Starting Camoufox costs seconds and a few hundred
#: MB; adapters that need bytes share this rather than each launching their own.
_BYTES_FETCHER: "BrowserBytesFetcher | None" = None


def get_bytes_fetcher() -> "BrowserBytesFetcher":
    """The shared bytes-capable browser fetcher (started on first real use)."""
    global _BYTES_FETCHER
    if _BYTES_FETCHER is None:
        _BYTES_FETCHER = BrowserBytesFetcher()
    return _BYTES_FETCHER


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
