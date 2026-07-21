"""PISTE gateway auth — shared by the French PISTE adapters (fr-judilibre, fr-legislation).

France's Cour de cassation (Judilibre) and DILA/Légifrance APIs both sit behind the
**PISTE** API gateway (piste.gouv.fr), and one PISTE app can subscribe to both — but the
two APIs authenticate **differently**, so this helper supports both modes:

- **Judilibre** — a static API key sent in the ``KeyId`` header (confirmed by the Cour de
  cassation's own tutorials: ``headers={"KeyId": key}``). No token exchange. The key is
  the app's API key (``PISTE_KEY_ID``); on PISTE the client id doubles as it, so we fall
  back to ``PISTE_CLIENT_ID``.
- **Légifrance** — OAuth2 *client-credentials*: exchange ``PISTE_CLIENT_ID`` /
  ``PISTE_CLIENT_SECRET`` for a short-lived bearer token, refresh on expiry/401.

Following the *degrade-safely* contract (§5): with no credentials the adapters yield
nothing rather than crashing. HTTP pacing/backoff stays the shared
:class:`RateLimitedClient`'s job.
"""

from __future__ import annotations

import os
import time

import httpx

from ..core.http import RateLimitedClient

# OAuth2 token endpoints (Légifrance). Sandbox is a separate host.
_OAUTH_PROD = "https://oauth.piste.gouv.fr/api/oauth/token"
_OAUTH_SANDBOX = "https://sandbox-oauth.piste.gouv.fr/api/oauth/token"

# API roots per host (production vs sandbox). Adapters append their service path.
API_PROD = "https://api.piste.gouv.fr"
API_SANDBOX = "https://sandbox-api.piste.gouv.fr"


def piste_sandbox() -> bool:
    return (os.environ.get("PISTE_SANDBOX") or "").strip().lower() in ("1", "true", "yes")


def piste_api_root() -> str:
    return API_SANDBOX if piste_sandbox() else API_PROD


def piste_credentials() -> tuple[str | None, str | None]:
    """(client_id, client_secret) for OAuth (Légifrance)."""
    return (os.environ.get("PISTE_CLIENT_ID") or None,
            os.environ.get("PISTE_CLIENT_SECRET") or None)


def piste_key_id() -> str | None:
    """The Judilibre ``KeyId`` — its own setting, else the client id (they coincide)."""
    return os.environ.get("PISTE_KEY_ID") or os.environ.get("PISTE_CLIENT_ID") or None


def piste_configured(auth: str = "oauth") -> bool:
    if auth == "keyid":
        return bool(piste_key_id())
    cid, secret = piste_credentials()
    return bool(cid and secret)


class PisteClient:
    """A :class:`RateLimitedClient` that attaches PISTE auth — a static ``KeyId`` header
    (``auth="keyid"``, Judilibre) or an OAuth2 bearer token it fetches and refreshes
    (``auth="oauth"``, Légifrance). Construct once per adapter and use like the plain
    client; all pacing/backoff/UA behaviour is inherited."""

    def __init__(
        self,
        source: str,
        *,
        auth: str = "oauth",
        min_interval: float = 0.5,
        client_id: str | None = None,
        client_secret: str | None = None,
        key_id: str | None = None,
        client: RateLimitedClient | None = None,
        oauth_client: httpx.Client | None = None,
        clock=time.monotonic,
    ) -> None:
        self.source = source
        cid, secret = piste_credentials()
        self._client_id = client_id or cid
        self._client_secret = client_secret or secret
        self._key_id = key_id or piste_key_id()
        # PISTE apps subscribe to an API under a *plan*: the API-key plan wants a static
        # KeyId header (Judilibre's public tutorials), the OAuth plan wants a Bearer token
        # (observed: prod Judilibre answers `www-authenticate: Bearer`). "auto" picks
        # KeyId when a key is configured, else OAuth — so either plan works without code
        # changes. Légifrance is always OAuth.
        if auth == "auto":
            auth = "keyid" if self._key_id else "oauth"
        self.auth = auth if auth in ("oauth", "keyid") else "oauth"
        self._client = client or RateLimitedClient(source, min_interval=min_interval)
        self._oauth = oauth_client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def configured(self) -> bool:
        if self.auth == "keyid":
            return bool(self._key_id)
        return bool(self._client_id and self._client_secret)

    # -- OAuth token lifecycle (Légifrance) --------------------------------
    def _fetch_token(self) -> str:
        url = _OAUTH_SANDBOX if piste_sandbox() else _OAUTH_PROD
        resp = self._oauth.post(url, data={
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": "openid",
        })
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = self._clock() + max(float(body.get("expires_in", 3600)) - 60, 30)
        return self._token

    def _bearer(self, *, force: bool = False) -> str:
        if force or self._token is None or self._clock() >= self._expires_at:
            return self._fetch_token()
        return self._token

    def _auth_headers(self, *, force: bool = False) -> dict:
        if self.auth == "keyid":
            return {"KeyId": self._key_id or ""}
        return {"Authorization": f"Bearer {self._bearer(force=force)}"}

    # -- request -----------------------------------------------------------
    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._auth_headers())
        resp = self._client.request(method, url, headers=headers, raise_for_4xx=False, **kwargs)
        # OAuth 401 → token expired/rotated: refresh once and retry (KeyId has no refresh).
        if resp.status_code == 401 and self.auth == "oauth":
            headers.update(self._auth_headers(force=True))
            resp = self._client.request(method, url, headers=headers, raise_for_4xx=False, **kwargs)
        return resp

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self._client.close()
        self._oauth.close()
