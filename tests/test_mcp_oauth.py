"""OAuth 2.1 for the MCP endpoint (§8) — discovery, DCR, PKCE authorization-code + refresh."""

from __future__ import annotations

import base64
import hashlib
import urllib.parse as up

import pytest
from fastapi.testclient import TestClient

from raglex.config import Config
from raglex.web import auth as authmod


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGLEX_MCP_PASSWORD", "sharedmcp")
    monkeypatch.setenv("RAGLEX_PUBLIC_URL", "http://localhost")   # localhost exempt from HTTPS rule
    monkeypatch.setenv("RAGLEX_SESSION_SECRET", "fixed-secret-abc")
    authmod._SECRET_CACHE = None
    from raglex.web.app import serve_app
    cfg = Config(data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite",
                 raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                 settings_path=tmp_path / "s.json", embed_provider="local-hashing", embed_model=None)
    with TestClient(serve_app(cfg)) as c:
        yield c


def _pkce():
    v = "v-" + base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode().rstrip("=")
    ch = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    return v, ch


def _register(c):
    return c.post("/mcp/register", json={"redirect_uris": ["http://localhost:9999/callback"],
                                         "token_endpoint_auth_method": "none"}).json()["client_id"]


def _authorize_req(c, cid, challenge, state):
    r = c.get("/mcp/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": "http://localhost:9999/callback",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state},
        follow_redirects=False)
    assert r.status_code == 302
    return up.parse_qs(up.urlparse(r.headers["location"]).query)["req"][0]


def test_unauthenticated_mcp_is_401_with_discovery(client):
    r = client.get("/mcp/")
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


def test_metadata_endpoints(client):
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == "http://localhost/mcp"
    md = client.get("/.well-known/oauth-authorization-server").json()
    assert md["authorization_endpoint"] == "http://localhost/mcp/authorize"
    assert md["token_endpoint"] == "http://localhost/mcp/token"
    assert md["registration_endpoint"] == "http://localhost/mcp/register"


def test_full_pkce_flow_and_refresh(client):
    cid = _register(client)
    verifier, challenge = _pkce()
    req = _authorize_req(client, cid, challenge, "xyz")
    # wrong shared password is refused
    assert client.post("/mcp-oauth/consent", data={"req": req, "password": "nope"},
                       follow_redirects=False).status_code == 401
    # correct password → redirect back to the client with a code, state preserved
    r = client.post("/mcp-oauth/consent", data={"req": req, "password": "sharedmcp"},
                    follow_redirects=False)
    assert r.status_code == 302
    q = up.parse_qs(up.urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"]
    code = q["code"][0]
    # token exchange with the matching verifier
    tok = client.post("/mcp/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "http://localhost:9999/callback", "client_id": cid,
        "code_verifier": verifier}).json()
    assert tok["token_type"] == "Bearer" and tok["access_token"] and tok["refresh_token"]
    # the access token passes auth on the MCP endpoint (421 = transport wants SSE, not an auth fail)
    assert client.get("/mcp/", headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code != 401
    # refresh works
    assert client.post("/mcp/token", data={"grant_type": "refresh_token",
                                           "refresh_token": tok["refresh_token"],
                                           "client_id": cid}).status_code == 200
    # code is single-use
    assert client.post("/mcp/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "http://localhost:9999/callback", "client_id": cid,
        "code_verifier": verifier}).status_code == 400


def test_pkce_mismatch_rejected(client):
    cid = _register(client)
    _, challenge = _pkce()
    req = _authorize_req(client, cid, challenge, "s2")
    r = client.post("/mcp-oauth/consent", data={"req": req, "password": "sharedmcp"},
                    follow_redirects=False)
    code = up.parse_qs(up.urlparse(r.headers["location"]).query)["code"][0]
    assert client.post("/mcp/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "http://localhost:9999/callback", "client_id": cid,
        "code_verifier": "WRONG-verifier"}).status_code == 400


def test_garbage_token_rejected(client):
    assert client.get("/mcp/", headers={"Authorization": "Bearer garbage"}).status_code == 401
