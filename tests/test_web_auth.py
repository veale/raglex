"""Role-based web auth + read-only enforcement (§8 hardening)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.storage import Catalogue, TextStore
from raglex.web import auth as authmod
from raglex.web import create_app


@pytest.fixture
def build(tmp_path, monkeypatch):
    """Factory: set auth env vars, then build a fresh app so install-time checks see them."""
    cat_path = tmp_path / "catalogue.sqlite"
    text_dir = tmp_path / "text"
    cat = Catalogue(cat_path)
    ts = TextStore(text_dir)
    rec = Record(
        source="eu-cellar", stable_id="ECLI:EU:C:2020:1", ecli="ECLI:EU:C:2020:1",
        doc_type=DocType.JUDGMENT, title="t", court="CJEU", decision_date=date(2024, 1, 1),
        language="en", source_language="en", text="hello world", raw_bytes=b"hello world",
        relations=[], extracted_via=ExtractedVia.STRUCTURED,
    )
    rec.ensure_payload_hash()
    cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, "hello world")))
    cat.close()

    def _make(**env):
        monkeypatch.setenv("RAGLEX_SESSION_SECRET", "test-secret-value-000")
        authmod._SECRET_CACHE = None
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        config = Config(
            data_dir=tmp_path, catalogue_path=cat_path, raw_dir=tmp_path / "raw",
            text_dir=text_dir, settings_path=tmp_path / "settings.json",
            embed_provider="local-hashing", embed_model=None,
        )
        # let handler-level errors on deliberately-empty payloads surface as 5xx responses
        # (we only assert on the auth verdict, not the handler outcome)
        return TestClient(create_app(config), raise_server_exceptions=False)

    return _make


def test_open_when_unconfigured(build):
    c = build()
    assert c.get("/stats").status_code == 200
    assert c.get("/auth/me").json()["enforced"] is False


def test_anon_blocked_when_configured(build):
    c = build(RAGLEX_ADMIN_PASSWORD="a", RAGLEX_READER_PASSWORD="r")
    assert c.get("/health").status_code == 200          # public
    assert c.get("/stats").status_code == 401           # needs auth
    assert c.get("/auth/me").json()["authenticated"] is False


def test_reader_is_read_only(build):
    c = build(RAGLEX_ADMIN_PASSWORD="admin-pw", RAGLEX_READER_PASSWORD="reader-pw")
    r = c.post("/auth/login", json={"password": "reader-pw"})
    assert r.status_code == 200 and r.json()["role"] == "reader"
    csrf = r.json()["csrf"]
    h = {"x-raglex-csrf": csrf}
    # reads: research OK, ops/secrets denied
    assert c.get("/stats").status_code == 200
    assert c.get("/settings").status_code == 403
    assert c.get("/jobs").status_code == 403
    # writes: general mutation denied, flag + single-fetch allowed
    assert c.post("/link", json={}, headers=h).status_code == 403
    assert c.post("/tag", json={}, headers=h).status_code == 403
    # allowed reader writes reach the handler (not a 403); may 4xx/5xx on payload, that's fine
    assert c.post("/refinement-flags", json={}, headers=h).status_code != 403
    assert c.post("/detect-citations", json={"text": "x"}, headers=h).status_code != 403


def test_admin_full_access(build):
    c = build(RAGLEX_ADMIN_PASSWORD="admin-pw", RAGLEX_READER_PASSWORD="reader-pw")
    r = c.post("/auth/login", json={"password": "admin-pw"})
    assert r.json()["role"] == "admin"
    csrf = r.json()["csrf"]
    assert c.get("/settings").status_code == 200
    assert c.get("/jobs").status_code == 200
    # admin write reaches handler
    assert c.post("/link", json={}, headers={"x-raglex-csrf": csrf}).status_code != 403


def test_csrf_required_for_cookie_writes(build):
    c = build(RAGLEX_ADMIN_PASSWORD="admin-pw")
    c.post("/auth/login", json={"password": "admin-pw"})
    # cookie present but no CSRF header → 403
    assert c.post("/link", json={}).status_code == 403


def test_bad_password_rejected(build):
    c = build(RAGLEX_ADMIN_PASSWORD="admin-pw")
    assert c.post("/auth/login", json={"password": "nope"}).status_code == 401


def test_api_token_is_admin_bearer(build):
    c = build(RAGLEX_API_TOKEN="tok-123")
    assert c.get("/stats").status_code == 401
    hdr = {"Authorization": "Bearer tok-123"}
    assert c.get("/settings", headers=hdr).status_code == 200
    # bearer is exempt from CSRF (not browser-ambient)
    assert c.post("/link", json={}, headers=hdr).status_code != 403


def test_admin_ip_whitelist_elevates(build):
    c = build(RAGLEX_ADMIN_PASSWORD="admin-pw", RAGLEX_ADMIN_IPS="9.9.9.9",
              RAGLEX_TRUST_FORWARDED="1")
    hdr = {"X-Forwarded-For": "9.9.9.9", "x-raglex-csrf": "1"}
    assert c.get("/settings", headers=hdr).status_code == 200
    assert c.post("/link", json={}, headers=hdr).status_code != 403
    # IP write without the CSRF header is refused
    assert c.post("/link", json={}, headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 403


# -- enable/disable toggles + long session cookie (Security panel) -------------
def test_password_gating_toggle(monkeypatch):
    monkeypatch.delenv("RAGLEX_AUTH_PASSWORDS_ENABLED", raising=False)
    monkeypatch.setenv("RAGLEX_ADMIN_PASSWORD", "hunter2")
    assert authmod.passwords_enabled() is True
    assert authmod.role_for_password("hunter2") == authmod.ADMIN
    # toggled off: the stored password is kept but login is ignored
    monkeypatch.setenv("RAGLEX_AUTH_PASSWORDS_ENABLED", "0")
    assert authmod.passwords_enabled() is False
    assert authmod.role_for_password("hunter2") is None


def test_ip_gating_toggle(monkeypatch):
    monkeypatch.delenv("RAGLEX_AUTH_IPS_ENABLED", raising=False)
    monkeypatch.setenv("RAGLEX_ADMIN_IPS", "10.0.0.0/8")
    assert authmod.ip_gating_enabled() is True
    assert authmod.role_for_ip("10.1.2.3") == authmod.ADMIN
    # toggled off: the stored list is kept but not enforced
    monkeypatch.setenv("RAGLEX_AUTH_IPS_ENABLED", "off")
    assert authmod.ip_gating_enabled() is False
    assert authmod.role_for_ip("10.1.2.3") == authmod.ANON


def test_session_ttl_defaults_to_thirty_days(monkeypatch):
    monkeypatch.delenv("RAGLEX_SESSION_TTL", raising=False)
    assert authmod._session_ttl() == 30 * 24 * 3600
    monkeypatch.setenv("RAGLEX_SESSION_TTL", "3600")
    assert authmod._session_ttl() == 3600
    monkeypatch.setenv("RAGLEX_SESSION_TTL", "garbage")
    assert authmod._session_ttl() == 30 * 24 * 3600
