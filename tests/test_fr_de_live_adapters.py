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
