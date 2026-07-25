"""OAuth 2.1 for the MCP endpoint (§8) — a light, spec-compliant shared login.

The MCP Python SDK already ships the hard part: an OAuth 2.1 Authorization Server + Resource
Server (RFC 8414 metadata, RFC 9728 protected-resource metadata, RFC 7591 dynamic client
registration, PKCE, the ``/authorize`` + ``/token`` + ``/register`` routes, and the
``WWW-Authenticate`` 401 that lets a client discover all of it). We only supply an
``OAuthAuthorizationServerProvider`` — the storage + the human consent step.

Design choices, given the operator's "a shared login is fine, keep it light":

- **One shared password.** ``/authorize`` redirects the browser to our own consent page,
  which checks ``RAGLEX_MCP_PASSWORD`` and only then mints the authorization code. No user
  table; everyone shares the login.
- **Stateless tokens.** Access/refresh tokens are HMAC-signed blobs verified without server
  state, so a redeploy doesn't sign everyone out. Registered clients are persisted to the
  settings store so a DCR client survives a restart too.
- **Mounted-subapp discovery fix.** The MCP app is mounted at ``/mcp``; the SDK computes the
  protected-resource metadata URL at the *origin root* (``/.well-known/oauth-protected-
  resource/mcp``) but registers the route inside the mount. We re-serve the well-known
  metadata at the parent-app root so discovery actually resolves.

Opt-in: with ``RAGLEX_MCP_PASSWORD`` unset the MCP endpoint keeps its current behaviour
(stdio + unauthenticated HTTP), so nothing local breaks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

log = logging.getLogger("raglex.mcp_oauth")

_ACCESS_TTL = int(os.environ.get("RAGLEX_MCP_ACCESS_TTL") or 3600)          # 1 hour
_REFRESH_TTL = int(os.environ.get("RAGLEX_MCP_REFRESH_TTL") or 30 * 86400)  # 30 days
_CODE_TTL = 600                                                              # 10 minutes


def mcp_oauth_enabled() -> bool:
    return bool(os.environ.get("RAGLEX_MCP_PASSWORD"))


def public_base_url() -> Optional[str]:
    base = os.environ.get("RAGLEX_PUBLIC_URL")
    return base.rstrip("/") if base else None


# ---------------------------------------------------------------------------
# stateless signed tokens (access + refresh) — verified without server state
# ---------------------------------------------------------------------------
def _b64e(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(secret: bytes, payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return f"{_b64e(raw)}.{_b64e(sig)}"


def _unsign(secret: bytes, token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    body, sig = token.split(".", 1)
    try:
        raw = _b64d(body)
        if not hmac.compare_digest(_b64d(sig), hmac.new(secret, raw, hashlib.sha256).digest()):
            return None
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def build_provider(facade):
    """Construct the RagLex OAuth provider (imports the MCP SDK lazily)."""
    from mcp.server.auth.provider import (
        AccessToken, AuthorizationCode, AuthorizationParams, OAuthAuthorizationServerProvider,
        RefreshToken, construct_redirect_uri,
    )
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    from .auth import session_secret

    secret = session_secret(facade)
    base = public_base_url()

    class RaglexOAuthProvider(OAuthAuthorizationServerProvider):
        def __init__(self) -> None:
            self._auth_codes: dict[str, AuthorizationCode] = {}
            self._revoked: set[str] = set()
            self._clients: dict[str, OAuthClientInformationFull] = self._load_clients()

        # -- dynamic client registration (persisted) --------------------------
        def _load_clients(self) -> dict:
            raw = facade.settings.resolve("RAGLEX_MCP_CLIENTS") or os.environ.get("RAGLEX_MCP_CLIENTS")
            out: dict[str, OAuthClientInformationFull] = {}
            if raw:
                try:
                    for cid, blob in json.loads(raw).items():
                        out[cid] = OAuthClientInformationFull.model_validate(blob)
                except Exception:  # noqa: BLE001
                    log.warning("could not load persisted MCP OAuth clients", exc_info=True)
            return out

        def _persist_clients(self) -> None:
            try:
                blob = {cid: json.loads(c.model_dump_json()) for cid, c in self._clients.items()}
                facade.update_settings({"RAGLEX_MCP_CLIENTS": json.dumps(blob)})
            except Exception:  # noqa: BLE001
                log.warning("could not persist MCP OAuth clients", exc_info=True)

        async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
            return self._clients.get(client_id)

        async def register_client(self, client_info: OAuthClientInformationFull) -> None:
            self._clients[client_info.client_id] = client_info
            self._persist_clients()

        # -- authorization: hand off to our consent page ----------------------
        async def authorize(self, client: OAuthClientInformationFull,
                             params: AuthorizationParams) -> str:
            # The SDK has already validated redirect_uri against this client. Sign the
            # request into a short-lived blob the consent page will complete after the
            # shared-password check.
            req = _sign(secret, {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_explicit": bool(params.redirect_uri_provided_explicitly),
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [],
                "resource": str(params.resource) if params.resource else None,
                "exp": int(time.time()) + _CODE_TTL,
            })
            return f"{base}/mcp-oauth/consent?{urlencode({'req': req})}"

        # completing the flow from the consent page: mint + stash the code, return the
        # client redirect the browser should follow.
        def complete_authorization(self, req_token: str) -> Optional[str]:
            payload = _unsign(secret, req_token)
            if payload is None:
                return None
            code = f"rlx_{secrets.token_urlsafe(32)}"
            self._auth_codes[code] = AuthorizationCode(
                code=code, scopes=payload["scopes"], expires_at=time.time() + _CODE_TTL,
                client_id=payload["client_id"], code_challenge=payload["code_challenge"],
                redirect_uri=payload["redirect_uri"],
                redirect_uri_provided_explicitly=payload["redirect_uri_explicit"],
                resource=payload["resource"], subject="mcp-user",
            )
            return construct_redirect_uri(payload["redirect_uri"], code=code, state=payload["state"])

        async def load_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: str) -> Optional[AuthorizationCode]:
            ac = self._auth_codes.get(authorization_code)
            if ac is None or ac.client_id != client.client_id or ac.expires_at < time.time():
                return None
            return ac

        async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                              authorization_code: AuthorizationCode) -> OAuthToken:
            self._auth_codes.pop(authorization_code.code, None)  # one-time use
            return self._issue(client.client_id, authorization_code.scopes,
                               resource=authorization_code.resource)

        # -- refresh ----------------------------------------------------------
        async def load_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: str) -> Optional[RefreshToken]:
            p = _unsign(secret, refresh_token)
            if p is None or p.get("t") != "refresh" or p.get("client_id") != client.client_id \
                    or refresh_token in self._revoked:
                return None
            return RefreshToken(token=refresh_token, client_id=client.client_id,
                                scopes=p.get("scopes", []), expires_at=p.get("exp"))

        async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                         refresh_token: RefreshToken,
                                         scopes: list[str]) -> OAuthToken:
            self._revoked.add(refresh_token.token)  # rotate
            return self._issue(client.client_id, scopes or refresh_token.scopes)

        # -- access-token verification (Resource Server) ----------------------
        async def load_access_token(self, token: str) -> Optional[AccessToken]:
            p = _unsign(secret, token)
            if p is None or p.get("t") != "access" or token in self._revoked:
                return None
            return AccessToken(token=token, client_id=p.get("client_id", ""),
                               scopes=p.get("scopes", []), expires_at=p.get("exp"),
                               resource=p.get("resource"))

        async def revoke_token(self, token) -> None:  # noqa: ANN001
            self._revoked.add(getattr(token, "token", token))

        # -- helpers ----------------------------------------------------------
        def _issue(self, client_id: str, scopes: list[str], *, resource=None) -> OAuthToken:
            now = int(time.time())
            access = _sign(secret, {"t": "access", "client_id": client_id, "scopes": scopes,
                                    "resource": resource, "iat": now, "exp": now + _ACCESS_TTL,
                                    "jti": secrets.token_urlsafe(8)})
            refresh = _sign(secret, {"t": "refresh", "client_id": client_id, "scopes": scopes,
                                     "iat": now, "exp": now + _REFRESH_TTL,
                                     "jti": secrets.token_urlsafe(8)})
            return OAuthToken(access_token=access, token_type="Bearer",
                              expires_in=_ACCESS_TTL, scope=" ".join(scopes) or None,
                              refresh_token=refresh)

    return RaglexOAuthProvider()


def auth_settings():
    """AuthSettings for FastMCP, or ``None`` if MCP OAuth is not (properly) configured."""
    if not mcp_oauth_enabled():
        return None
    base = public_base_url()
    if not base:
        log.warning("RAGLEX_MCP_PASSWORD is set but RAGLEX_PUBLIC_URL is not — "
                    "MCP OAuth needs a public base URL for its issuer/resource metadata; "
                    "leaving the MCP endpoint unauthenticated.")
        return None
    from pydantic import AnyHttpUrl
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

    issuer = f"{base}/mcp"
    return AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(issuer),
        required_scopes=[],
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )


# ---------------------------------------------------------------------------
# parent-app routes: consent page + root-level well-known metadata
# ---------------------------------------------------------------------------
_CONSENT_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorise RagLex MCP</title>
<style>
 body{{font-family:system-ui,sans-serif;background:#1e1e2e;color:#cdd6f4;display:flex;
   min-height:100vh;align-items:center;justify-content:center;margin:0}}
 .card{{background:#313244;padding:32px;border-radius:12px;max-width:360px;width:100%;
   box-shadow:0 8px 40px rgba(0,0,0,.35)}}
 h1{{margin:0 0 4px;font-size:22px;color:#89b4fa}}
 p{{color:#a6adc8;font-size:13px;margin:0 0 18px}}
 input{{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #45475a;
   background:#1e1e2e;color:#cdd6f4;font-size:14px;margin-bottom:12px}}
 button{{width:100%;padding:10px;border:none;border-radius:8px;background:#89b4fa;color:#1e1e2e;
   font-weight:700;font-size:14px;cursor:pointer}}
 .err{{color:#f38ba8;font-size:13px;margin-bottom:10px}}
</style></head><body><div class="card">
 <h1>RagLex MCP</h1>
 <p>An MCP client is asking to connect. Enter the shared access password to authorise it.</p>
 {error}
 <form method="post" action="/mcp-oauth/consent">
  <input type="hidden" name="req" value="{req}">
  <input type="password" name="password" autofocus placeholder="Access password" autocomplete="current-password">
  <button type="submit">Authorise</button>
 </form>
</div></body></html>"""


def install_mcp_oauth_routes(app, provider, mcp_app) -> None:
    """Consent page + root-level well-known metadata (the mounted-subapp discovery fix).

    Registered as raw Starlette routes (not FastAPI routes): with ``from __future__ import
    annotations`` the ``request: Request`` hints are strings FastAPI can't resolve from this
    local scope, and would be misread as query params. Starlette passes the Request
    positionally, sidestepping that entirely.
    """
    from starlette.responses import HTMLResponse, RedirectResponse, JSONResponse
    from mcp.server.auth.routes import build_metadata, build_resource_metadata_url
    from mcp.shared.auth import ProtectedResourceMetadata
    from pydantic import AnyHttpUrl
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions

    base = public_base_url() or ""
    issuer = AnyHttpUrl(f"{base}/mcp")
    prm = ProtectedResourceMetadata(
        resource=issuer, authorization_servers=[issuer], scopes_supported=[])
    prm_path = build_resource_metadata_url(issuer).path  # /.well-known/oauth-protected-resource/mcp
    as_meta = build_metadata(issuer, None, ClientRegistrationOptions(enabled=True),
                             RevocationOptions(enabled=True))
    _cors = {"Access-Control-Allow-Origin": "*"}

    async def consent_form(request):
        req = request.query_params.get("req", "")
        return HTMLResponse(_CONSENT_HTML.format(req=_html_escape(req), error=""))

    async def consent_submit(request):
        form = await request.form()
        req = str(form.get("req") or "")
        password = str(form.get("password") or "")
        expected = os.environ.get("RAGLEX_MCP_PASSWORD") or ""
        if not (expected and hmac.compare_digest(password, expected)):
            return HTMLResponse(
                _CONSENT_HTML.format(req=_html_escape(req),
                                     error='<div class="err">Incorrect password</div>'),
                status_code=401)
        redirect = provider.complete_authorization(req)
        if redirect is None:
            return HTMLResponse(
                _CONSENT_HTML.format(req=_html_escape(req),
                                     error='<div class="err">This request expired — retry from your MCP client.</div>'),
                status_code=400)
        return RedirectResponse(redirect, status_code=302)

    async def protected_resource_metadata(request):
        return JSONResponse(json.loads(prm.model_dump_json(exclude_none=True)), headers=_cors)

    async def as_metadata(request):
        return JSONResponse(json.loads(as_meta.model_dump_json(exclude_none=True)), headers=_cors)

    app.add_route("/mcp-oauth/consent", consent_form, methods=["GET"], include_in_schema=False)
    app.add_route("/mcp-oauth/consent", consent_submit, methods=["POST"], include_in_schema=False)
    app.add_route(prm_path, protected_resource_metadata, methods=["GET"], include_in_schema=False)
    app.add_route("/.well-known/oauth-authorization-server", as_metadata, methods=["GET"], include_in_schema=False)
    app.add_route("/.well-known/oauth-authorization-server/mcp", as_metadata, methods=["GET"], include_in_schema=False)


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
