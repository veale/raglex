from __future__ import annotations

from raglex.adapters._piste import PisteClient
from raglex.adapters.eu_cellar import national_transposition_edges
from raglex.core.models import RelationshipType


# -- CELLAR transposition edges ---------------------------------------------
def test_national_transposition_edges_from_sparql():
    rows = [
        {"nim": "http://.../nim1", "country": "FRA",
         "eli": "http://data.europa.eu/eli/FR/loi/1978/78-17",
         "title": "Loi Informatique et Libertés"},
        {"nim": "http://.../nim2", "country": "DEU", "title": "Bundesdatenschutzgesetz"},
        # duplicate of nim2 (same title+country) → deduped
        {"nim": "http://.../nim2b", "country": "DEU", "title": "Bundesdatenschutzgesetz"},
    ]
    edges = national_transposition_edges("32016L0680", lambda q: rows)
    assert len(edges) == 2
    assert all(e.relationship_type == RelationshipType.TRANSPOSES for e in edges)
    fr = edges[0]
    assert fr.dst_id == "eli/FR/loi/1978/78-17"  # national ELI → resolvable destination
    de = edges[1]
    assert de.dst_id is None  # no ELI → dangling, resolved when de-neuris harvests it
    assert "country: DEU" in de.raw_citation_string


def test_transposition_query_is_directive_scoped():
    from raglex.adapters.eu_cellar import _transposition_query
    q = _transposition_query("32016L0680")
    assert "resource_legal_implements_resource_legal" in q
    assert "32016L0680" in q


# -- PISTE auth modes -------------------------------------------------------
class _Resp:
    def __init__(self, payload=None, status=200):
        self._p = payload or {}; self.status_code = status
    def json(self): return self._p
    def raise_for_status(self): pass


class _RecordingRLC:
    """Captures the headers each request is sent with."""
    def __init__(self): self.headers_seen = []
    def request(self, method, url, headers=None, raise_for_4xx=True, **kw):
        self.headers_seen.append(headers or {})
        return _Resp({"ok": True})


class _FakeOAuth:
    def __init__(self): self.token_calls = 0
    def post(self, url, data=None):
        self.token_calls += 1
        return _Resp({"access_token": f"tok{self.token_calls}", "expires_in": 3600})


def test_keyid_mode_sends_keyid_header():
    rlc = _RecordingRLC()
    pc = PisteClient("x", auth="keyid", key_id="MYKEY", client=rlc)
    assert pc.configured()
    pc.get("https://api.piste.gouv.fr/cassation/judilibre/v1.0/healthcheck")
    assert rlc.headers_seen[-1]["KeyId"] == "MYKEY"
    assert "Authorization" not in rlc.headers_seen[-1]


def test_oauth_mode_fetches_and_reuses_bearer():
    rlc = _RecordingRLC()
    oauth = _FakeOAuth()
    pc = PisteClient("x", auth="oauth", client_id="c", client_secret="s",
                     client=rlc, oauth_client=oauth)
    assert pc.configured()
    pc.get("https://api.piste.gouv.fr/dila/legifrance/lf-engine-app/list/code")
    pc.get("https://api.piste.gouv.fr/dila/legifrance/lf-engine-app/list/code")
    assert rlc.headers_seen[0]["Authorization"] == "Bearer tok1"
    # token cached — the second call reuses it (only one token fetch)
    assert oauth.token_calls == 1
    assert rlc.headers_seen[1]["Authorization"] == "Bearer tok1"


def test_unconfigured_pisteclient_reports_false():
    assert not PisteClient("x", auth="keyid", key_id=None,
                           client=_RecordingRLC()).configured()
    assert not PisteClient("x", auth="oauth", client_id="c", client_secret=None,
                           client=_RecordingRLC()).configured()
