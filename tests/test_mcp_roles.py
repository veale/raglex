"""Two MCP credentials, the same two the web login uses.

The MCP endpoint had one shared password and every token could do everything —
including the ~60 mutation ops behind maintenance(). It now takes the SAME admin and
reader passwords as the web, and the password typed at the consent screen decides
what the issued token may do: a reader token reads, an admin token may also change
the corpus.

The gate lives on maintenance() rather than on each op, because maintenance() is the
single door to all of them — a new op inherits the gate instead of having to remember
it.
"""

from __future__ import annotations

import pytest

from raglex.web import mcp_oauth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("RAGLEX_MCP_PASSWORD", "RAGLEX_ADMIN_PASSWORD", "RAGLEX_READER_PASSWORD",
              "RAGLEX_ADMIN_PASSWORD_HASH", "RAGLEX_READER_PASSWORD_HASH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RAGLEX_AUTH_PASSWORDS_ENABLED", "1")


def test_the_web_login_credentials_switch_mcp_auth_on(monkeypatch):
    assert not mcp_oauth.mcp_oauth_enabled()
    monkeypatch.setenv("RAGLEX_READER_PASSWORD", "read-me")
    assert mcp_oauth.mcp_oauth_enabled(), "a reader password alone should protect the endpoint"


def test_the_password_decides_the_role(monkeypatch):
    monkeypatch.setenv("RAGLEX_ADMIN_PASSWORD", "let-me-in")
    monkeypatch.setenv("RAGLEX_READER_PASSWORD", "just-looking")
    assert mcp_oauth._role_for("let-me-in") == "admin"
    assert mcp_oauth._role_for("just-looking") == "reader"
    assert mcp_oauth._role_for("neither") is None
    assert mcp_oauth._role_for("") is None


def test_the_legacy_mcp_password_still_grants_admin(monkeypatch):
    """A deployment configured before the split must keep working."""
    monkeypatch.setenv("RAGLEX_MCP_PASSWORD", "old-shared-secret")
    assert mcp_oauth.mcp_oauth_enabled()
    assert mcp_oauth._role_for("old-shared-secret") == "admin"


def test_the_roles_map_to_distinct_scopes():
    assert mcp_oauth.SCOPE_BY_ROLE["admin"] == mcp_oauth.SCOPE_ADMIN
    assert mcp_oauth.SCOPE_BY_ROLE["reader"] == mcp_oauth.SCOPE_READ
    assert mcp_oauth.SCOPE_ADMIN != mcp_oauth.SCOPE_READ


# -- the gate ------------------------------------------------------------------
class _Token:
    def __init__(self, scopes):
        self.scopes = scopes


def _patch_token(monkeypatch, token):
    import mcp.server.auth.middleware.auth_context as ctx

    monkeypatch.setattr(ctx, "get_access_token", lambda: token)


def test_a_reader_token_cannot_change_the_corpus(monkeypatch):
    from raglex.mcp_server import NotPermitted, _require_admin

    monkeypatch.setenv("RAGLEX_ADMIN_PASSWORD", "a")
    _patch_token(monkeypatch, _Token([mcp_oauth.SCOPE_READ]))
    with pytest.raises(NotPermitted) as e:
        _require_admin()
    assert "read-only" in str(e.value)


def test_an_admin_token_may(monkeypatch):
    from raglex.mcp_server import _require_admin

    monkeypatch.setenv("RAGLEX_ADMIN_PASSWORD", "a")
    _patch_token(monkeypatch, _Token([mcp_oauth.SCOPE_ADMIN]))
    _require_admin()          # no raise


def test_a_scopeless_token_is_treated_as_a_reader(monkeypatch):
    """Fail closed: a token that carries no scope at all must not inherit admin."""
    from raglex.mcp_server import NotPermitted, _require_admin

    monkeypatch.setenv("RAGLEX_ADMIN_PASSWORD", "a")
    _patch_token(monkeypatch, _Token([]))
    with pytest.raises(NotPermitted):
        _require_admin()


def test_an_unauthenticated_deployment_keeps_working(monkeypatch):
    """No credentials configured → no gate, exactly as the HTTP API behaves. A local
    stdio client has the operator's own shell anyway."""
    from raglex.mcp_server import _require_admin

    _patch_token(monkeypatch, _Token([]))
    _require_admin()          # auth_enabled() is False, so no raise


def test_stdio_has_no_bearer_token_and_is_not_blocked(monkeypatch):
    from raglex.mcp_server import _require_admin

    monkeypatch.setenv("RAGLEX_ADMIN_PASSWORD", "a")
    _patch_token(monkeypatch, None)
    _require_admin()          # no HTTP request → no token → local client
