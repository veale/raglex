from __future__ import annotations

from datetime import date
from pathlib import Path

from raglex.adapters.de_neuris import DeNeurisAdapter, _members, _xml_content_url, parse_caselaw
from raglex.adapters.fr_conseil_etat import parse_hit
from raglex.core.models import DocType, RelationshipType
from raglex.formats.ldml_de import parse_ldml_de
from raglex.formats.legifrance_json import parse_legifrance_obj

REFS = Path(__file__).resolve().parent.parent / "raglex design docs" / "raglex-refs"


# -- Légifrance JSON parser -------------------------------------------------
def test_legifrance_legipart_articles_become_segments():
    obj = {
        "title": "Code civil", "cid": "LEGITEXT000006070721",
        "eli": "eli/code/civ", "nature": "CODE",
        "sections": [
            {"title": "Livre Ier", "articles": [
                {"num": "1240", "content": "<p>Tout fait quelconque de l'homme...</p>",
                 "etat": "VIGUEUR"},
                {"num": "1241", "content": "<p>Chacun est responsable...</p>"},
            ]},
        ],
    }
    doc = parse_legifrance_obj(obj)
    assert doc.title == "Code civil"
    assert doc.eli == "eli/code/civ"
    assert [s.label for s in doc.segments] == ["Livre Ier", "Article 1240", "Article 1241"]
    assert "Tout fait quelconque" in doc.text
    assert "<p>" not in doc.text  # HTML stripped


def test_legifrance_getarticle_versions():
    obj = {"article": {
        "num": "1382", "cid": "LEGIARTI000006419292",
        "content": "<p>ancienne rédaction</p>",
        "articleVersions": [
            {"id": "v1", "etat": "ABROGE", "dateDebut": "1804-03-15", "dateFin": "2016-10-01"},
            {"id": "v2", "etat": "VIGUEUR", "dateDebut": "2016-10-01"},
        ]}}
    doc = parse_legifrance_obj(obj)
    assert len(doc.versions) == 2
    assert doc.versions[0].date_debut == date(1804, 3, 15)
    assert doc.versions[1].etat == "VIGUEUR"
    assert doc.segments[0].label == "Article 1382"


# -- LDML.de parser (real example) ------------------------------------------
def test_ldml_de_parses_real_regelungstext():
    # a Stammform (consolidated) example → title, ELI, jurabk, and §/Abschnitt segments
    matches = sorted(REFS.glob(
        "de-ldml/ldml_de/Beispiele*/01-04_Gesetz_Stammform*/**/regelungstext-1.xml"))
    doc = parse_ldml_de(matches[0].read_bytes())
    assert doc.title == "Saatgutverkehrsgesetz"  # docTitle, note stripped
    assert doc.metadata["eli"].startswith("eli/bund/")
    assert doc.metadata["jurabk"] == "SaatG"  # from shortTitle "(SaatG)"
    assert doc.segments and doc.text


# -- NeuRIS case law --------------------------------------------------------
CASELAW = {
    "@type": "Decision",
    "documentNumber": "KVRE12345",
    "ecli": "ECLI:DE:BGH:2021:120521UVIZR100.20.0",
    "courtName": "Bundesgerichtshof", "courtType": "BGH",
    "decisionDate": "2021-05-12", "fileNumbers": ["VI ZR 100/20"],
    "guidingPrinciple": "Der Leitsatz.",
    "tenor": "Die Revision wird zurückgewiesen.",
    "caseFacts": "Der Kläger verlangt Schadensersatz.",
    "decisionGrounds": "Die Revision ist unbegründet.",
    "documentType": "Urteil",
}


def test_neuris_caselaw_zones_and_ecli():
    rec = parse_caselaw(CASELAW)
    assert rec.ecli == "ECLI:DE:BGH:2021:120521UVIZR100.20.0"
    assert rec.doc_type == DocType.JUDGMENT
    assert rec.court == "Bundesgerichtshof"
    assert rec.decision_date == date(2021, 5, 12)
    labels = [s.label for s in rec.segments]
    assert labels == ["Leitsatz", "Tenor", "Tatbestand", "Entscheidungsgründe"]
    assert "Schadensersatz" in rec.text


def test_neuris_members_unwrap_and_xml_content_url():
    collection = {"member": [{"@type": "SearchMember", "item": CASELAW}], "view": {}}
    assert _members(collection) == [CASELAW]
    expr = {"encoding": [
        {"encodingFormat": "text/html", "contentUrl": "/x.html"},
        {"encodingFormat": "application/xml", "contentUrl": "/norms/eli/bund/x/regelungstext-1.xml"},
    ]}
    assert _xml_content_url(expr).endswith("regelungstext-1.xml")


class _Resp:
    def __init__(self, payload=None, content=b"", status=200):
        self._p = payload; self.content = content; self.status_code = status
    def json(self): return self._p


class _FakeHTTP:
    def __init__(self, routes): self.routes = routes; self.calls = []
    def get(self, url, params=None, headers=None, raise_for_4xx=True):
        self.calls.append(url)
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return _Resp(status=404)


def test_neuris_caselaw_discover_and_fetch():
    listing = {"member": [{"item": {"ecli": CASELAW["ecli"],
                                    "documentNumber": "KVRE12345",
                                    "decisionDate": "2021-05-12"}}],
               "view": {}}  # no `next` → single page
    http = _FakeHTTP({"/case-law?": _Resp(listing), "case-law/KVRE12345": _Resp(CASELAW)})
    # route matching: the list call hits `/case-law` and detail hits `/case-law/KVRE12345`
    http.routes = {"case-law/KVRE12345": _Resp(CASELAW), "case-law": _Resp(listing)}
    adapter = DeNeurisAdapter(mode="caselaw", client=http)
    stubs = list(adapter.discover("2021-01-01"))
    assert [s.stable_id for s in stubs] == [CASELAW["ecli"]]
    rec = adapter.fetch(stubs[0])
    assert rec.ecli == CASELAW["ecli"] and rec.segments


# -- Conseil d'État ---------------------------------------------------------
def test_conseil_etat_parse_hit():
    src = {"ecli": "ECLI:FR:CE:2021:433506.20210421", "numero": "433506",
           "juridiction": "Conseil d'État", "date_lecture": "2021-04-21",
           "texte_integral": "1. Considérant que la requête...\n2. Décide : rejet."}
    rec = parse_hit(src)
    assert rec.ecli == "ECLI:FR:CE:2021:433506.20210421"
    assert rec.court == "Conseil d'État"
    assert rec.decision_date == date(2021, 4, 21)
    assert rec.text


# -- NeuRIS's two ceilings: 10,000 hits and pageIndex 99 ----------------------

class _WindowedHTTP:
    """A register that answers like NeuRIS: any query reports totalItems capped at
    10,000, pageIndex 100 is a 422, and a dated window reports its true count."""

    def __init__(self, per_year: dict[int, int]) -> None:
        self.per_year = per_year
        self.windows: list[tuple[str, str]] = []

    def get(self, url, params=None, headers=None, raise_for_4xx=True):
        p = params or {}
        if p.get("pageIndex", 0) > 99:
            return _Resp(status=422)
        lo, hi = p.get("dateFrom"), p.get("dateTo")
        if lo and hi and p.get("size") == 1:
            self.windows.append((lo, hi))
            total = self.per_year.get(int(lo[:4]), 0) if lo[:4] == hi[:4] else 10000
            return _Resp({"member": [], "totalItems": total, "view": {}})
        if not (lo and hi):
            return _Resp({"member": [], "totalItems": 10000, "view": {}})
        year = int(lo[:4])
        n = self.per_year.get(year, 0)
        if not n:
            return _Resp({"member": [], "totalItems": 0, "view": {}})
        members = [{"item": {"documentNumber": f"K{year}{i}", "decisionDate": f"{year}-06-01"}}
                   for i in range(n)]
        return _Resp({"member": members, "totalItems": n, "view": {}})


def test_backfill_windows_by_date_instead_of_running_into_the_cap(monkeypatch):
    """One unbounded walk can only ever see 10,000 decisions — the newest ~2 years and
    nothing before them, however long it runs. A backfill therefore asks year by year,
    where the register reports a true count and the page ceiling can't truncate it."""
    import raglex.adapters.de_neuris as mod

    monkeypatch.setattr(mod, "_EARLIEST_YEAR", 2023)
    http = _WindowedHTTP({2026: 2, 2025: 3, 2024: 1, 2023: 0})
    stubs = list(DeNeurisAdapter(mode="caselaw", client=http).discover(None))
    assert len(stubs) == 6                       # every year, not just the newest
    assert {w[0][:4] for w in http.windows} == {"2026", "2025", "2024", "2023"}


def test_a_year_over_the_cap_is_split(monkeypatch):
    """A window that saturates is halved — the count is not a count, it is the ceiling."""
    import raglex.adapters.de_neuris as mod

    monkeypatch.setattr(mod, "_EARLIEST_YEAR", 2025)
    http = _WindowedHTTP({2026: 10000, 2025: 1})
    list(DeNeurisAdapter(mode="caselaw", client=http).discover(None))
    spans_2026 = [w for w in http.windows if w[0].startswith("2026")]
    assert len(spans_2026) > 1                   # split, not silently truncated


def test_stub_id_matches_the_id_the_record_will_carry():
    """Otherwise the pipeline's "already held?" check never matches and a backfill
    re-downloads the entire register, paying a fetch per document to discard it on the
    payload hash — which is what "done=344, stored=0" looked like on the live box."""
    from raglex.adapters.de_neuris import parse_caselaw

    listing = {"member": [{"item": {"documentNumber": "KORE615362026", "ecli": None,
                                    "decisionDate": "2026-05-18"}}], "view": {}}
    http = _FakeHTTP({"case-law": _Resp(listing)})
    stub = next(iter(DeNeurisAdapter(mode="caselaw", client=http).discover("2026-01-01")))
    record = parse_caselaw({"documentNumber": "KORE615362026", "decisionDate": "2026-05-18"})
    assert stub.stable_id == record.stable_id == "de/KORE615362026"
    assert stub.hints["id"] == "KORE615362026"      # …while the fetch still uses the raw id


def test_incremental_run_keeps_the_simple_path():
    """"Everything since the watermark" is small by construction — no windowing."""
    listing = {"member": [{"item": {"documentNumber": "KVRE1", "decisionDate": "2026-07-01"}}],
               "view": {}}
    http = _FakeHTTP({"case-law": _Resp(listing)})
    stubs = list(DeNeurisAdapter(mode="caselaw", client=http).discover("2026-06-30"))
    assert [s.stable_id for s in stubs] == ["de/KVRE1"]


def test_neuris_root_absolute_paths_are_not_double_versioned():
    """The register answers with root-absolute @id/contentUrl values that already carry
    the version segment. Joining those onto BASE (which ends in /v1) produced
    /v1/v1/legislation/… — a 404 on the expression JSON, the LDML.de manifestation and
    the .xml fallback alike, so the legislation collection never stored one document.
    The 404s surfaced as the host's generic 403, which reads like an anti-bot wall."""
    from raglex.adapters.de_neuris import HOST, _url

    assert _url("/v1/legislation/eli/bund/bgbl-1/2026/201/2026-07-10/1/deu") == (
        f"{HOST}/v1/legislation/eli/bund/bgbl-1/2026/201/2026-07-10/1/deu")
    assert "/v1/v1/" not in _url("/v1/legislation/eli/x.xml")
    # case law builds its own paths and must still get the version segment added
    assert _url("case-law/KORE300492026") == f"{HOST}/v1/case-law/KORE300492026"
    assert _url("https://elsewhere.example/x") == "https://elsewhere.example/x"


def test_legifrance_search_payload_carries_the_operateur_the_api_demands():
    """Omit ``operateur`` on the CHAMP and Légifrance answers 500 "une exception non
    gérée est survenue" — an outage-shaped reply to a malformed body. It is the only
    difference between no French administrative case law and 26,806 deliberations."""
    from raglex.adapters.fr_legislation import FrLegislationAdapter

    sent = {}

    class _Client:
        def configured(self):
            return True

        def post(self, url, json=None, headers=None):
            sent["url"], sent["json"] = url, json

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {"results": []}
            return _R()

    list(FrLegislationAdapter(fond="CNIL", client=_Client()).discover(None, max_pages=1))
    champ = sent["json"]["recherche"]["champs"][0]
    assert champ["operateur"] == "ET"
    assert champ["criteres"][0]["operateur"] == "ET"
    assert sent["json"]["recherche"]["operateur"] == "ET"
    assert "filtres" in sent["json"]["recherche"]


def test_each_fund_gets_the_sort_it_actually_implements():
    """An unknown sort is not rejected — the API answers 200 and orders by something
    else, so CNIL asked for PUBLICATION_DATE_DESC returns 2019 at the top of a fund
    whose newest deliberation is 2026-07-24, and the cursor is set from a stale slice."""
    from raglex.adapters.fr_legislation import _SORT_BY_FOND

    assert _SORT_BY_FOND["CNIL"] == "DATE_DECISION_DESC"
    assert _SORT_BY_FOND["CONSTIT"] == "DATE_DESC"
    assert _SORT_BY_FOND["JORF"] == "PUBLICATION_DATE_DESC"


def test_each_fund_gets_its_own_consult_route():
    from raglex.adapters.fr_legislation import _text_kind

    assert _text_kind("CNILTEXT000054466005") == "cnil"
    assert _text_kind("CONSTEXT000054617301") == "juri"
    assert _text_kind("LEGITEXT000006072051") == "legipart"
    assert _text_kind("LEGIARTI000006465098") == "article"
    assert _text_kind("JORFTEXT000054595429") == "jorf"


def test_constit_date_comes_out_of_the_title_because_every_date_field_is_null():
    from raglex.adapters.fr_legislation import _date_in_title

    assert _date_in_title(
        "Décision 2026-1214/1215 QPC - 31 juillet 2026 - Société Airbnb") == "2026-07-31"
    assert _date_in_title("Décision du 1er août 2026") == "2026-08-01"
    assert _date_in_title("Décision 2026-327 L") is None


def test_a_whole_text_body_is_not_an_empty_document():
    """CNIL and CONSTIT are one body of text under ``text.texte``, not a tree of
    articles. Without that branch the fetch succeeds and stores an empty document."""
    from raglex.formats.legifrance_json import parse_legifrance_obj

    doc = parse_legifrance_obj({"executionTime": 5, "text": {
        "id": "CONSTEXT1", "titre": "Décision 2026-1 QPC",
        "ecli": "ECLI:FR:CC:2026:2026.1.QPC",
        "texte": "LE CONSEIL CONSTITUTIONNEL A ÉTÉ SAISI…"}})
    assert doc.ecli == "ECLI:FR:CC:2026:2026.1.QPC"
    assert doc.title == "Décision 2026-1 QPC"
    assert doc.text and "CONSEIL CONSTITUTIONNEL" in doc.text


def test_the_key_id_is_not_the_oauth_client_id(monkeypatch):
    """Four different values are issued per PISTE app. Sending the client id as KeyId
    earns a bare 400, so the old fallback turned an OAuth-only configuration into a
    silently dead source."""
    from raglex.adapters import _piste

    monkeypatch.delenv("PISTE_KEY_ID", raising=False)
    monkeypatch.delenv("PISTE_API_KEY", raising=False)
    monkeypatch.setenv("PISTE_CLIENT_ID", "the-oauth-client-id")
    assert _piste.piste_key_id() is None
    monkeypatch.setenv("PISTE_API_KEY", "the-api-key")
    assert _piste.piste_key_id() == "the-api-key"


def test_judilibre_cursor_reads_the_same_clock_the_walk_is_ordered_by():
    """The export is a changes feed — date_type=update, order=asc, resumed with
    date_start — so the watermark must be the update date. It rode on the decision date,
    and the first live run of the watch came back with a cursor of 1965 after reading
    2,000 decisions."""
    from raglex.adapters.fr_judilibre import FrJudilibreAdapter

    page = {"results": [{"id": "abc123", "ecli": "ECLI:FR:CCASS:2026:C100400",
                         "decision_date": "1965-03-18", "update_date": "2026-08-07"}],
            "next_batch": None}

    class _Client:
        def configured(self):
            return True

        def get(self, url, **kw):
            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return page
            return _R()

    stub = next(iter(FrJudilibreAdapter(client=_Client()).discover(None)))
    assert stub.hints["watermark"] == "2026-08-07"
    assert stub.hint_date.isoformat() == "1965-03-18"   # still the decision's own date
    assert stub.stable_id == "ECLI:FR:CCASS:2026:C100400"


def test_provenance_follows_the_fund_not_the_class():
    """One adapter class serves three registry keys. A class-level ``source`` stored a
    CNIL deliberation, a Conseil constitutionnel decision and a consolidated code
    indistinguishably as "fr-legislation", so the keep-current view could not tell which
    of the three registers had gone stale."""
    from raglex.adapters.fr_legislation import FrLegislationAdapter

    class _C:
        def configured(self):
            return True

    assert FrLegislationAdapter(fond="LEGI", client=_C()).source == "fr-legislation"
    assert FrLegislationAdapter(fond="CNIL", client=_C()).source == "fr-cnil"
    assert FrLegislationAdapter(fond="CONSTIT", client=_C()).source == "fr-constit"


def test_static_export_declares_the_document_language():
    """The renderer already read law["language"] for <html lang> and schema.org
    inLanguage; build_data never set it, so a 2.6 MB Code des postes with 70,209
    accented characters went out as lang="en"."""
    import inspect

    from raglex import static_export

    src = inspect.getsource(static_export.StaticLawExporter.build_data)
    assert '"language": target["language"]' in src


def test_judilibre_opens_a_new_window_when_one_is_exhausted():
    """A Judilibre query is capped at 10,000 results however many the register holds,
    and it holds 566,124. Treating the last batch of a query as the end of the walk
    ended the backfill after 10,000 with a cursor in 1971 and reported success."""
    from raglex.adapters.fr_judilibre import FrJudilibreAdapter

    windows = {
        None: [{"id": "a", "decision_date": "1960-01-01", "update_date": "1970-01-01"}],
        "1970-01-01": [{"id": "b", "decision_date": "1961-01-01",
                        "update_date": "1980-01-01"}],
        "1980-01-01": [{"id": "c", "decision_date": "1962-01-01",
                        "update_date": "1980-01-01"}],   # cursor cannot advance → stop
    }
    seen_starts = []

    class _Client:
        def configured(self):
            return True

        def get(self, url, params=None, **kw):
            start = (params or {}).get("date_start")
            seen_starts.append(start)

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {"results": windows.get(start, []), "next_batch": None,
                            "total": 566124}
            return _R()

    stubs = list(FrJudilibreAdapter(client=_Client()).discover(None))
    assert [s.stable_id for s in stubs] == ["a", "b", "c"]
    assert seen_starts == [None, "1970-01-01", "1980-01-01"]   # terminates, no spin
    assert stubs[0].hints["feed_total"] == 566124
