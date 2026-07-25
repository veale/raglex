"""Web-app authentication + authorisation (§8 hardening).

Three ways in, two privilege levels:

- **reader** — a shared *reader* password, or a source IP on the reader allow-list. Gets a
  read-only interface: every corpus read, but writes are refused except a tiny allow-list
  (flag a passage, trigger a single on-demand fetch of a missing authority). No settings,
  no maintain/admin panels, no linking-by-highlight.
- **admin** — a shared *admin* password (or a registered passkey/WebAuthn credential), or a
  source IP on the admin allow-list. Full access. An admin password entered from a reader
  session *elevates* it in place.

Enforcement is server-side (the UI merely hides what it must not offer). The model is
**deny-by-default for writes** (an allow-list of reader-safe mutations) plus an
**admin-only read denylist** (settings, jobs control, maintain data) — so a reader can use
the whole research surface but can neither change the corpus nor read secrets.

Auth is **opt-in**: with no password, IP list or API token configured the API is open, so
local/dev/test setups keep working untouched. Configure any of them and the surface closes.

Sessions are a compact HMAC-signed cookie (no server-side session table, no extra deps);
``RAGLEX_API_TOKEN`` still works as an admin bearer token for programmatic clients. CSRF on
cookie-authenticated writes is a session-bound double-submit token (``SameSite=Lax`` already
blocks cross-site cookie-bearing POSTs; the token closes the residual gap).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------
ANON, READER, ADMIN = "anon", "reader", "admin"
_RANK = {ANON: 0, READER: 1, ADMIN: 2}


def _higher(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


@dataclass(frozen=True, slots=True)
class Principal:
    role: str          # anon | reader | admin
    method: str        # how the role was established (cookie/ip/token/anon)
    sub: str = ""      # subject label (for logging)
    csrf: str = ""     # session-bound CSRF nonce (cookie sessions only)

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN

    @property
    def is_reader_or_up(self) -> bool:
        return _RANK.get(self.role, 0) >= _RANK[READER]


# ---------------------------------------------------------------------------
# reader-safe write allow-list + admin-only read denylist
# ---------------------------------------------------------------------------
# Exact (method-agnostic) paths a *reader* may POST to. Everything else that mutates is
# admin-only. Kept deliberately tiny: analysis that writes nothing, flagging a passage, and
# the single on-demand fetch of one missing authority the user asked to permit.
READER_WRITE_ALLOW = frozenset({
    "/citations/scan",       # read-only grammar recognition (PDF text-layer linkify)
    "/detect-citations",     # read-only preview, no fetching
    "/refinement-flags",     # a reader flags a passage for an admin to action
    "/unresolved/harvest",   # trigger ONE on-demand fetch+process of a missing authority
})

# Read (GET) paths a reader must NOT see — secrets, ops control, and the maintain/curation
# surface. Prefix match. The rest of the read surface (search, documents, graph, citator…)
# stays open to readers.
ADMIN_ONLY_READ_PREFIXES = (
    "/settings",
    "/jobs",
    "/watches",
    "/probes",
    "/system",
    "/sources/keep-current",
    "/sources/catalog",
    "/aliases",
    "/suggestions",
    "/gap-status",
    "/legislation/effects",
    "/legislation/changes",
    "/corpus-map",
    "/health/embedding",
    "/embed/backlog",
    "/export",
    "/reference-context",
    "/refinement-flags",   # the review queue is admin; a reader may only POST a new flag
)

# Paths reachable with no authentication at all (the login flow + liveness).
PUBLIC_PATHS = frozenset({
    "/health",
    "/auth/login",
    "/auth/logout",
    "/auth/me",
    "/auth/webauthn/register/options",
    "/auth/webauthn/register/verify",
    "/auth/webauthn/login/options",
    "/auth/webauthn/login/verify",
})

_SESSION_COOKIE = "raglex_session"
_CSRF_COOKIE = "raglex_csrf"
_DEFAULT_TTL = int(os.environ.get("RAGLEX_SESSION_TTL") or 7 * 24 * 3600)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def _env(*names: str) -> Optional[str]:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def auth_enabled() -> bool:
    """Enforcement is on iff any credential/whitelist is configured."""
    return any((
        _env("RAGLEX_ADMIN_PASSWORD", "RAGLEX_ADMIN_PASSWORD_HASH"),
        _env("RAGLEX_READER_PASSWORD", "RAGLEX_READER_PASSWORD_HASH"),
        _env("RAGLEX_ADMIN_IPS"),
        _env("RAGLEX_READER_IPS"),
        _env("RAGLEX_API_TOKEN"),
    ))


def _hash_password(password: str, salt: bytes) -> bytes:
    # scrypt: memory-hard, in the stdlib, no extra dependency.
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    """Produce a ``scrypt$<salt_hex>$<hash_hex>`` string for RAGLEX_*_PASSWORD_HASH."""
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${_hash_password(password, salt).hex()}"


def _verify_hash(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        got = _hash_password(password, bytes.fromhex(salt_hex))
    except ValueError:
        return False
    return hmac.compare_digest(got, bytes.fromhex(hash_hex))


def _password_matches(password: str, plain_env: str, hash_env: str) -> bool:
    if not password:
        return False
    stored_hash = _env(hash_env)
    if stored_hash:
        return _verify_hash(password, stored_hash)
    plain = _env(plain_env)
    if plain:
        return hmac.compare_digest(password, plain)
    return False


def role_for_password(password: str) -> Optional[str]:
    """Admin beats reader when a password satisfies both (or they are equal)."""
    if _password_matches(password, "RAGLEX_ADMIN_PASSWORD", "RAGLEX_ADMIN_PASSWORD_HASH"):
        return ADMIN
    if _password_matches(password, "RAGLEX_READER_PASSWORD", "RAGLEX_READER_PASSWORD_HASH"):
        return READER
    return None


# ---------------------------------------------------------------------------
# client IP + IP allow-lists
# ---------------------------------------------------------------------------
def _parse_cidrs(raw: Optional[str]):
    nets = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return nets


def client_ip(request: Request) -> Optional[str]:
    """The client's IP, honouring X-Forwarded-For only when explicitly trusted.

    Behind a reverse proxy the socket peer is the proxy; the real client is in
    X-Forwarded-For. But XFF is client-settable, so trusting it unconditionally lets any
    caller forge an allow-listed IP. So it is used only when ``RAGLEX_TRUST_FORWARDED`` is
    set, and then we take the hop ``RAGLEX_TRUSTED_PROXY_HOPS`` (default 1) from the right —
    the address the *nearest trusted proxy* observed — not the leftmost (fully spoofable)
    entry.
    """
    peer = request.client.host if request.client else None
    if str(os.environ.get("RAGLEX_TRUST_FORWARDED") or "").strip().lower() in ("1", "true", "yes", "on"):
        xff = request.headers.get("x-forwarded-for", "")
        chain = [p.strip() for p in xff.split(",") if p.strip()]
        if chain:
            try:
                hops = max(1, int(os.environ.get("RAGLEX_TRUSTED_PROXY_HOPS") or 1))
            except ValueError:
                hops = 1
            idx = len(chain) - hops
            return chain[idx] if 0 <= idx < len(chain) else chain[0]
    return peer


def role_for_ip(ip: Optional[str]) -> str:
    if not ip:
        return ANON
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ANON
    for net in _parse_cidrs(_env("RAGLEX_ADMIN_IPS")):
        if addr in net:
            return ADMIN
    for net in _parse_cidrs(_env("RAGLEX_READER_IPS")):
        if addr in net:
            return READER
    return ANON


# ---------------------------------------------------------------------------
# signed session cookie
# ---------------------------------------------------------------------------
_SECRET_CACHE: Optional[bytes] = None


def session_secret(facade=None) -> bytes:
    """A stable signing key. From ``RAGLEX_SESSION_SECRET`` if set, else generated once and
    persisted to the settings file so cookies survive restarts (and stay consistent across
    the API + scheduler processes sharing the data dir)."""
    global _SECRET_CACHE
    if _SECRET_CACHE is not None:
        return _SECRET_CACHE
    env = os.environ.get("RAGLEX_SESSION_SECRET")
    if env:
        _SECRET_CACHE = env.encode()
        return _SECRET_CACHE
    if facade is not None:
        try:
            existing = facade.settings.resolve("RAGLEX_SESSION_SECRET")
        except Exception:  # noqa: BLE001
            existing = None
        if existing:
            os.environ["RAGLEX_SESSION_SECRET"] = existing
            _SECRET_CACHE = existing.encode()
            return _SECRET_CACHE
        generated = secrets.token_urlsafe(48)
        try:
            facade.update_settings({"RAGLEX_SESSION_SECRET": generated})
        except Exception:  # noqa: BLE001
            pass
        os.environ["RAGLEX_SESSION_SECRET"] = generated
        _SECRET_CACHE = generated.encode()
        return _SECRET_CACHE
    # No facade to persist through (rare) — a per-process ephemeral key.
    _SECRET_CACHE = secrets.token_bytes(48)
    return _SECRET_CACHE


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_session(role: str, secret: bytes, *, sub: str = "", ttl: int = _DEFAULT_TTL,
                  csrf: Optional[str] = None) -> tuple[str, str]:
    """Return ``(session_token, csrf_token)``. The CSRF nonce is embedded in the signed
    payload and mirrored to a readable cookie for the double-submit check."""
    csrf = csrf or secrets.token_urlsafe(24)
    now = int(time.time())
    payload = {"r": role, "s": sub, "iat": now, "exp": now + ttl, "c": csrf}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return f"{_b64e(raw)}.{_b64e(sig)}", csrf


def verify_session(token: str, secret: bytes) -> Optional[Principal]:
    if not token or "." not in token:
        return None
    body, sig = token.split(".", 1)
    try:
        raw = _b64d(body)
        got = _b64d(sig)
    except Exception:  # noqa: BLE001
        return None
    expected = hmac.new(secret, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(got, expected):
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    role = payload.get("r")
    if role not in (READER, ADMIN):
        return None
    return Principal(role=role, method="cookie", sub=payload.get("s", ""),
                     csrf=payload.get("c", ""))


# ---------------------------------------------------------------------------
# principal resolution
# ---------------------------------------------------------------------------
def resolve_principal(request: Request, secret: bytes) -> Principal:
    """Effective principal = the highest privilege any credential proves. A cookie session,
    an IP allow-list hit, and a bearer token are all considered; the strongest wins (so an
    admin-IP visitor is admin even without logging in, and an admin password *elevates* a
    reader-IP session)."""
    best = Principal(role=ANON, method="anon")

    # bearer / API token → admin (programmatic back-compat)
    api_token = _env("RAGLEX_API_TOKEN")
    if api_token:
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else \
            request.headers.get("x-api-key", "")
        if supplied and hmac.compare_digest(supplied, api_token):
            best = Principal(role=ADMIN, method="token", sub="api-token")

    # signed session cookie
    cookie = request.cookies.get(_SESSION_COOKIE, "")
    sess = verify_session(cookie, secret) if cookie else None
    if sess and _RANK[sess.role] > _RANK[best.role]:
        best = sess
    elif sess and best.role == sess.role:
        best = sess  # keep the CSRF nonce from the cookie session

    # IP allow-list
    ip_role = role_for_ip(client_ip(request))
    if _RANK.get(ip_role, 0) > _RANK[best.role]:
        best = Principal(role=ip_role, method="ip", sub=client_ip(request) or "")

    return best


def _is_admin_only_read(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in ADMIN_ONLY_READ_PREFIXES)


def authorize(principal: Principal, method: str, path: str) -> Optional[str]:
    """Return ``None`` if allowed, else a short reason string (→ 403)."""
    method = method.upper()
    if method in ("GET", "HEAD"):
        if principal.is_admin:
            return None
        if _is_admin_only_read(path):
            return "admin only"
        return None
    # write methods
    if principal.is_admin:
        return None
    if method == "POST" and path in READER_WRITE_ALLOW:
        return None
    return "read-only"


# ---------------------------------------------------------------------------
# middleware
# ---------------------------------------------------------------------------
def _cookie_kwargs(request: Request) -> dict:
    secure = request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto", "").lower() == "https"
    return {"httponly": True, "secure": secure, "samesite": "lax", "path": "/"}


def install_web_auth(app: FastAPI, facade) -> None:
    """Attach the auth middleware + login endpoints to the API sub-app."""
    if not auth_enabled():
        # Still expose /auth/me so the UI can tell it is an open deployment.
        _install_login_routes(app, facade, enforce=False)
        return

    secret = session_secret(facade)

    @app.middleware("http")
    async def _enforce(request: Request, call_next):  # noqa: ANN001
        path = request.url.path
        if request.method == "OPTIONS" or path in PUBLIC_PATHS:
            return await call_next(request)

        principal = resolve_principal(request, secret)
        request.state.principal = principal

        if not principal.is_reader_or_up:
            return JSONResponse({"error": "authentication required"}, status_code=401)

        # CSRF. Browser-ambient auth (a cookie session, or an IP on the allow-list) can be
        # ridden by a cross-site request, so writes must carry the custom ``X-Raglex-CSRF``
        # header — which a cross-origin page cannot set without a preflight our CORS policy
        # refuses. A cookie session must echo its *session-bound* nonce; an IP-only browser
        # session has no nonce, so header *presence* is the anti-CSRF signal. A bearer token
        # is not browser-ambient, so it is exempt.
        if request.method not in ("GET", "HEAD", "OPTIONS") and principal.method in ("cookie", "ip"):
            supplied = request.headers.get("x-raglex-csrf", "")
            ok = (hmac.compare_digest(supplied, principal.csrf)
                  if principal.method == "cookie" and principal.csrf else bool(supplied))
            if not ok:
                return JSONResponse({"error": "invalid or missing CSRF token"}, status_code=403)

        reason = authorize(principal, request.method, path)
        if reason is not None:
            return JSONResponse(
                {"error": reason, "role": principal.role,
                 "hint": "sign in as admin for full access"}, status_code=403)
        return await call_next(request)

    _install_login_routes(app, facade, enforce=True)


def _set_session_cookies(resp: JSONResponse, request: Request, token: str, csrf: str) -> None:
    ck = _cookie_kwargs(request)
    resp.set_cookie(_SESSION_COOKIE, token, max_age=_DEFAULT_TTL, **ck)
    resp.set_cookie(_CSRF_COOKIE, csrf, max_age=_DEFAULT_TTL, **{**ck, "httponly": False})


def _install_login_routes(app: FastAPI, facade, *, enforce: bool) -> None:
    from fastapi import Body

    secret = session_secret(facade) if enforce else b""

    @app.get("/auth/me")
    def auth_me(request: Request) -> dict:
        if not enforce:
            return {"authenticated": True, "role": ADMIN, "enforced": False,
                    "passkey_supported": _webauthn_available()}
        p = resolve_principal(request, secret)
        return {"authenticated": p.is_reader_or_up, "role": p.role, "method": p.method,
                "enforced": True, "csrf": p.csrf if p.method == "cookie" else None,
                "can_elevate": p.role != ADMIN and _admin_login_possible(),
                "passkey_supported": _webauthn_available()}

    @app.post("/auth/login")
    def auth_login(request: Request, payload: dict = Body(default={})):
        if not enforce:
            return {"role": ADMIN, "enforced": False}
        role = role_for_password(str((payload or {}).get("password") or ""))
        if role is None:
            return JSONResponse({"error": "invalid password"}, status_code=401)
        token, csrf = issue_session(role, secret, sub="password")
        resp = JSONResponse({"role": role, "csrf": csrf, "enforced": True})
        _set_session_cookies(resp, request, token, csrf)
        return resp

    @app.post("/auth/logout")
    def auth_logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(_SESSION_COOKIE, path="/")
        resp.delete_cookie(_CSRF_COOKIE, path="/")
        return resp

    if enforce:
        _install_webauthn_routes(app, facade, secret)


def _admin_login_possible() -> bool:
    return bool(_env("RAGLEX_ADMIN_PASSWORD", "RAGLEX_ADMIN_PASSWORD_HASH")) or _webauthn_available()


# ---------------------------------------------------------------------------
# WebAuthn / passkeys (admin) — optional, behind py_webauthn
# ---------------------------------------------------------------------------
def _webauthn_available() -> bool:
    try:
        import webauthn  # noqa: F401
        return bool(_env("RAGLEX_ADMIN_PASSKEYS")) or _passkey_registration_open()
    except Exception:  # noqa: BLE001
        return False


def _passkey_registration_open() -> bool:
    # Registration is only offered to an already-authenticated admin, so it is always
    # "possible" when the library is present; the availability flag above is about whether
    # a passkey *login* can be offered.
    try:
        import webauthn  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _rp_id() -> str:
    return os.environ.get("RAGLEX_RP_ID") or os.environ.get("RAGLEX_HOSTNAME") or "localhost"


def _rp_name() -> str:
    return os.environ.get("RAGLEX_RP_NAME") or "RagLex"


def _load_passkeys(facade) -> list[dict]:
    raw = facade.settings.resolve("RAGLEX_ADMIN_PASSKEYS") if facade else None
    if not raw:
        raw = os.environ.get("RAGLEX_ADMIN_PASSKEYS")
    try:
        return json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []


def _save_passkeys(facade, creds: list[dict]) -> None:
    facade.update_settings({"RAGLEX_ADMIN_PASSKEYS": json.dumps(creds)})


def _install_webauthn_routes(app: FastAPI, facade, secret: bytes) -> None:
    try:
        import webauthn
        from webauthn.helpers import options_to_json
        from webauthn.helpers.structs import (
            AuthenticationCredential, AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
            RegistrationCredential, ResidentKeyRequirement, UserVerificationRequirement,
        )
    except Exception:  # noqa: BLE001 — library absent; passkey endpoints simply 501
        from fastapi import Body

        @app.post("/auth/webauthn/register/options")
        @app.post("/auth/webauthn/register/verify")
        @app.post("/auth/webauthn/login/options")
        @app.post("/auth/webauthn/login/verify")
        def _no_passkey(payload: dict = Body(default={})):  # noqa: ANN001
            return JSONResponse({"error": "passkeys not available (install the 'webauthn' package)"},
                                status_code=501)
        return

    from fastapi import Body

    def _sign_challenge(challenge: bytes, kind: str) -> str:
        payload = {"ch": _b64e(challenge), "k": kind, "exp": int(time.time()) + 300}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        sig = hmac.new(secret, raw, hashlib.sha256).digest()
        return f"{_b64e(raw)}.{_b64e(sig)}"

    def _open_challenge(token: str, kind: str) -> Optional[bytes]:
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
        if payload.get("k") != kind or int(payload.get("exp", 0)) < int(time.time()):
            return None
        return _b64d(payload["ch"])

    @app.post("/auth/webauthn/register/options")
    def wa_register_options(request: Request) -> dict:
        # Registration requires an existing admin session (bootstrap with the admin password).
        if not resolve_principal(request, secret).is_admin:
            return JSONResponse({"error": "admin session required to register a passkey"},
                                status_code=403)
        existing = _load_passkeys(facade)
        opts = webauthn.generate_registration_options(
            rp_id=_rp_id(), rp_name=_rp_name(),
            user_name="admin", user_display_name="RagLex admin",
            exclude_credentials=[PublicKeyCredentialDescriptor(id=_b64d(c["id"])) for c in existing],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED),
        )
        resp = JSONResponse(json.loads(options_to_json(opts)))
        resp.set_cookie("raglex_wa", _sign_challenge(opts.challenge, "reg"),
                        max_age=300, **_cookie_kwargs(request))
        return resp

    @app.post("/auth/webauthn/register/verify")
    def wa_register_verify(request: Request, payload: dict = Body(...)) -> dict:
        if not resolve_principal(request, secret).is_admin:
            return JSONResponse({"error": "admin session required"}, status_code=403)
        challenge = _open_challenge(request.cookies.get("raglex_wa", ""), "reg")
        if challenge is None:
            return JSONResponse({"error": "challenge expired"}, status_code=400)
        try:
            verification = webauthn.verify_registration_response(
                credential=RegistrationCredential.parse_raw(json.dumps(payload)),
                expected_challenge=challenge, expected_rp_id=_rp_id(),
                expected_origin=_expected_origins(request))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"registration failed: {exc}"}, status_code=400)
        creds = _load_passkeys(facade)
        creds.append({"id": _b64e(verification.credential_id),
                      "public_key": _b64e(verification.credential_public_key),
                      "sign_count": verification.sign_count,
                      "label": str((payload or {}).get("label") or "passkey")})
        _save_passkeys(facade, creds)
        return {"ok": True, "count": len(creds)}

    @app.post("/auth/webauthn/login/options")
    def wa_login_options(request: Request) -> dict:
        creds = _load_passkeys(facade)
        opts = webauthn.generate_authentication_options(
            rp_id=_rp_id(), user_verification=UserVerificationRequirement.PREFERRED,
            allow_credentials=[PublicKeyCredentialDescriptor(id=_b64d(c["id"])) for c in creds])
        resp = JSONResponse(json.loads(options_to_json(opts)))
        resp.set_cookie("raglex_wa", _sign_challenge(opts.challenge, "login"),
                        max_age=300, **_cookie_kwargs(request))
        return resp

    @app.post("/auth/webauthn/login/verify")
    def wa_login_verify(request: Request, payload: dict = Body(...)):
        challenge = _open_challenge(request.cookies.get("raglex_wa", ""), "login")
        if challenge is None:
            return JSONResponse({"error": "challenge expired"}, status_code=400)
        creds = _load_passkeys(facade)
        by_id = {c["id"]: c for c in creds}
        cred_id = (payload or {}).get("id") or (payload or {}).get("rawId")
        match = by_id.get(cred_id)
        if match is None:
            return JSONResponse({"error": "unknown credential"}, status_code=400)
        try:
            verification = webauthn.verify_authentication_response(
                credential=AuthenticationCredential.parse_raw(json.dumps(payload)),
                expected_challenge=challenge, expected_rp_id=_rp_id(),
                expected_origin=_expected_origins(request),
                credential_public_key=_b64d(match["public_key"]),
                credential_current_sign_count=int(match.get("sign_count") or 0),
                require_user_verification=False)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"login failed: {exc}"}, status_code=400)
        match["sign_count"] = verification.new_sign_count
        _save_passkeys(facade, creds)
        token, csrf = issue_session(ADMIN, secret, sub="passkey")
        resp = JSONResponse({"role": ADMIN, "csrf": csrf})
        _set_session_cookies(resp, request, token, csrf)
        return resp


def _expected_origins(request: Request):
    configured = os.environ.get("RAGLEX_WEBAUTHN_ORIGIN")
    if configured:
        return [o.strip() for o in configured.split(",") if o.strip()]
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"
