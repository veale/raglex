"""Rate-limited HTTP client shared by REST/Atom/static-scrape adapters.

Implements the Appendix A resilience contract in one place so every adapter gets
it for free: a per-source ``min_interval`` floor (§1.8 — pacing exists to keep
jobs alive, not as etiquette), exponential backoff with jitter on 429/503, honour
``Retry-After``, a realistic User-Agent (§5a — a ``python-requests`` UA is a
fingerprint for "bot"), and a typed ``RateLimitException`` on a hard wall.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time

import httpx

from .errors import FetchError, RateLimitException

log = logging.getLogger("raglex.core.http")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 RagLex/0.1 (research harvester)"
)


def get_proxy() -> str | None:
    """The proxy all outbound traffic routes through, if configured (§5a:
    `requires_proxy`). ``RAGLEX_PROXY`` accepts ``socks5://…`` (anonymise/escape
    IP-blocks) or ``http(s)://…``; populated from the settings file via
    ``SettingsStore.apply_to_env``, so it's set once in the UI and applies to every
    adapter, importer, and the SPARQL/Zotero clients alike."""
    return os.environ.get("RAGLEX_PROXY") or None


def build_client(
    *, timeout: float = 30.0, user_agent: str = DEFAULT_USER_AGENT, proxy: str | None = None
) -> httpx.Client:
    """A plain httpx client honouring the configured proxy + a realistic UA — used
    by the non-paced callers (URL import, Zotero, ad-hoc fetches)."""
    return httpx.Client(
        headers={"User-Agent": user_agent},
        timeout=timeout,
        follow_redirects=True,
        proxy=proxy if proxy is not None else get_proxy(),
    )

# Statuses that mean "the source is pushing back" — pause/back off, don't crash.
_RATE_LIMIT_STATUSES = frozenset({429, 503})


class RateLimitedClient:
    """A thin httpx wrapper enforcing one source's pacing and backoff policy.

    One instance per source so ``min_interval`` is tracked per-source, matching the
    orchestrator's "pause *that* source's queue" model (§5a).
    """

    def __init__(
        self,
        source: str,
        *,
        min_interval: float = 1.0,
        max_retries: int = 5,
        # Floor under the retry sleep, for a source that throttles for a fixed period
        # and does not say so with Retry-After. Zero keeps the historical behaviour.
        min_backoff: float = 0.0,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        proxy: str | None = None,
        verify=True,
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self.source = source
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.min_backoff = min_backoff
        self._sleep = sleep
        self._last_request_at = 0.0
        # Reservation clock for the pacer: the monotonic time the NEXT request
        # may start. Held under a lock so concurrent callers queue rather than
        # race (see _pace).
        self._next_slot_at = 0.0
        self._pace_lock = threading.Lock()
        # Route through the configured proxy by default (§5a requires_proxy); an
        # explicit proxy arg overrides, None falls back to RAGLEX_PROXY.
        self._client = client or httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy if proxy is not None else get_proxy(),
            verify=verify,
        )

    def __enter__(self) -> "RateLimitedClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _pace(self) -> None:
        """Block until ``min_interval`` has elapsed since the last request.

        Thread-safe, and that is what makes concurrent fetching POLITE rather than a
        multiplier on the request rate. The reservation (read the last time, claim the
        next slot) happens under the lock and the sleep happens outside it, so N threads
        sharing this client queue up and emit at most one request per ``min_interval``
        BETWEEN THEM — the same aggregate rate a serial crawl produced, with the network
        latency overlapped instead of paid one document at a time. Without the lock two
        threads would both observe the interval elapsed and fire together, quietly
        doubling the rate the source was promised.
        """
        with self._pace_lock:
            now = time.monotonic()
            start_at = max(now, self._next_slot_at)
            self._next_slot_at = start_at + self.min_interval
        wait = start_at - now
        if wait > 0:
            self._sleep(wait)
        self._last_request_at = time.monotonic()

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def request(self, method: str, url: str, *, raise_for_4xx: bool = True, **kwargs) -> httpx.Response:
        """Paced, retrying request. Raises ``RateLimitException`` once retries on a
        429/503 are exhausted; ``FetchError`` (fatal) on a non-retryable 4xx —
        unless ``raise_for_4xx`` is False (scrapers inspect the status to detect a
        WAF/anti-bot block and escalate to a stealth fetcher)."""
        attempt = 0
        while True:
            self._pace()
            try:
                resp = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise FetchError(f"{self.source}: transport error: {exc}") from exc
                self._backoff(attempt)
                attempt += 1
                continue

            if resp.status_code in _RATE_LIMIT_STATUSES:
                retry_after = _parse_retry_after(resp)
                if attempt >= self.max_retries:
                    raise RateLimitException(self.source, retry_after=retry_after)
                self._backoff(attempt, retry_after=retry_after)
                attempt += 1
                continue

            if resp.status_code >= 500:
                # The source is broken, not the item. Retry, then report it as transient so
                # callers cool the item off for hours — never for months (a 500 says nothing
                # about whether the document exists).
                if attempt >= self.max_retries:
                    raise FetchError(
                        f"{self.source}: HTTP {resp.status_code} for {url}", transient=True
                    )
                self._backoff(attempt)
                attempt += 1
                continue

            if resp.status_code >= 400 and raise_for_4xx:
                _warn_if_walled(self.source, url, resp)
                # 404/410 etc. are fatal for this stub — caller decides upstream_status.
                raise FetchError(
                    f"{self.source}: HTTP {resp.status_code} for {url}",
                    transient=False,
                )
            _warn_if_walled(self.source, url, resp)
            return resp

    def _backoff(self, attempt: int, *, retry_after: float | None = None) -> None:
        if retry_after is not None:
            self._sleep(retry_after)
            return
        # exponential backoff with full jitter, capped
        delay = min(2.0**attempt, 60.0)
        # Full jitter is right for spreading a thundering herd, but it makes the FIRST
        # retries sleep for almost nothing — and a source that throttles for a fixed
        # period and sends no Retry-After (ENISA: ~30s, no header) can burn every retry
        # inside its own block. min_backoff is that source's measured floor.
        self._sleep(max(self.min_backoff, delay * random.random()))


#: Markers that a response IS the anti-bot wall rather than the document — the
#: interstitial's own words, plus the Cloudflare/WAF response headers.
_WALL_MARKERS = (
    b"just a moment", b"enable javascript and cookies", b"cf-browser-verification",
    b"challenge-platform", b"attention required! | cloudflare", b"__cf_chl",
    b"ddos protection by", b"access denied", b"request unsuccessful. incapsula",
)
_WALL_STATUSES = frozenset({403, 503})
#: One review row per (source, host) per process — the fingerprinting in ops.errorlog
#: already collapses occurrences, and this stops a 5,000-document harvest doing 5,000
#: string builds to say the same thing.
_WALLED_SEEN: set[tuple[str, str]] = set()


def _looks_walled(resp: httpx.Response) -> bool:
    if resp.status_code in _WALL_STATUSES:
        return True
    if resp.headers.get("cf-mitigated"):
        return True
    # a document request answered with an interstitial: 200, but HTML where the caller
    # asked for a file — the shape that reads as success and stores nothing
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "html" not in ctype:
        return False
    try:
        head = (resp.content or b"")[:4000].lower()
    except Exception:  # noqa: BLE001 — streamed/!read response
        return False
    return any(m in head for m in _WALL_MARKERS)


def _warn_if_walled(source: str, url: str, resp: httpx.Response) -> None:
    """Flag an anti-bot wall as a REVIEW ITEM, with the fix, the first time it appears.

    A wall is not an outage and not a missing document: the source is up, the item exists,
    and a plain client simply cannot have it. That failure is invisible by default — it
    arrives as a 403 the adapter turns into "skipped", which is exactly how committee PDFs
    went unfetched behind a Cloudflare challenge without anybody noticing.

    This is a WARNING on a raglex logger, so ops.errorlog fingerprints it into the same
    ``kind='error'`` queue as everything else and counts the repeats. It names the fix,
    because by the time somebody reads the row the context is gone.
    """
    try:
        if not _looks_walled(resp):
            return
        host = str(getattr(resp, "url", url)).split("/")[2] if "//" in str(url) else url
        key = (source, host)
        if key in _WALLED_SEEN:
            return
        _WALLED_SEEN.add(key)
        log.warning(
            "%s: %s is behind an anti-bot wall (HTTP %s) — a plain client cannot fetch "
            "it. FIX: route this adapter's fetch through the browser. For a file, "
            "scraping.fetcher.get_bytes_fetcher().fetch_bytes(url, referer_url=<a page "
            "on the same host>); for a page, .fetch_html(url). Both clear the challenge "
            "and are already installed. First seen at %s",
            source, host, resp.status_code, url,
        )
    except Exception:  # noqa: BLE001 — a detector must never break the fetch it watches
        pass


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        # HTTP-date form is rare for these sources; ignore and use backoff.
        return None
