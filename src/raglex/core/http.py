"""Rate-limited HTTP client shared by REST/Atom/static-scrape adapters.

Implements the Appendix A resilience contract in one place so every adapter gets
it for free: a per-source ``min_interval`` floor (§1.8 — pacing exists to keep
jobs alive, not as etiquette), exponential backoff with jitter on 429/503, honour
``Retry-After``, a realistic User-Agent (§5a — a ``python-requests`` UA is a
fingerprint for "bot"), and a typed ``RateLimitException`` on a hard wall.
"""

from __future__ import annotations

import os
import random
import time

import httpx

from .errors import FetchError, RateLimitException

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
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        proxy: str | None = None,
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self.source = source
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._sleep = sleep
        self._last_request_at = 0.0
        # Route through the configured proxy by default (§5a requires_proxy); an
        # explicit proxy arg overrides, None falls back to RAGLEX_PROXY.
        self._client = client or httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy if proxy is not None else get_proxy(),
        )

    def __enter__(self) -> "RateLimitedClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _pace(self) -> None:
        """Block until ``min_interval`` has elapsed since the last request."""
        wait = self.min_interval - (time.monotonic() - self._last_request_at)
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
                # 404/410 etc. are fatal for this stub — caller decides upstream_status.
                raise FetchError(
                    f"{self.source}: HTTP {resp.status_code} for {url}",
                    transient=False,
                )
            return resp

    def _backoff(self, attempt: int, *, retry_after: float | None = None) -> None:
        if retry_after is not None:
            self._sleep(retry_after)
            return
        # exponential backoff with full jitter, capped
        delay = min(2.0**attempt, 60.0)
        self._sleep(delay * random.random())


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        # HTTP-date form is rare for these sources; ignore and use backoff.
        return None
