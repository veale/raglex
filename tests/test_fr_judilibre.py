from __future__ import annotations

from datetime import date

from raglex.adapters.fr_judilibre import FrJudilibreAdapter, parse_decision
from raglex.core.models import RelationshipType

# A decisionFull shaped like the Judilibre /decision + /export payload (JUDILIBRE-public.json):
# `text` is the flat body, `zones` are {start,end} offsets into it, `visa` is textLink[],
# `rapprochements` is decisionLink[] (no ECLI, only id/title/number/jurisdiction).
TEXT = ("Introduction ici. " "Exposé du litige détaillé. "
        "Sur le moyen unique. " "La Cour, motivations. " "PAR CES MOTIFS, casse.")
DECISION = {
    "id": "5fca...abcd",
    "ecli": "ECLI:FR:CCASS:2021:C100400",
    "jurisdiction": "Cour de cassation",
    "chamber": "Chambre civile 1",
    "formation": "formation de section",
    "number": "21-00400",
    "numbers": ["21-00400"],
    "publication": ["b", "r"],
    "solution": "Cassation",
    "nac": "54G",
    "decision_date": "2021-05-12",
    "update_date": "2021-05-20",
    "text": TEXT,
    "zones": {
        "introduction": [{"start": 0, "end": 18}],
        "expose": [{"start": 19, "end": 45}],
        "motivations": [{"start": 68, "end": 90}],
        "dispositif": [{"start": 91, "end": len(TEXT)}],
    },
    "visa": [
        {"id": 12345, "title": "article 1240 du code civil",
         "url": "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000032041571"},
        {"id": 0, "title": "article 9 du code de procédure civile", "url": ""},
    ],
    "rapprochements": [
        {"id": "abc123", "title": "1re Civ., 3 avril 2019, pourvoi n° 18-11.916",
         "number": "18-11.916", "jurisdiction": "Cour de cassation"},
    ],
}


def test_parse_decision_zones_become_segments():
    parsed = parse_decision(DECISION)
    assert parsed.ecli == "ECLI:FR:CCASS:2021:C100400"
    assert parsed.decision_date == date(2021, 5, 12)
    # zones surface as segments in layout order, offsets slicing the flat text
    labels = [s.label for s in parsed.segments]
    assert labels == ["introduction", "expose", "motivations", "dispositif"]
    disp = parsed.segments[-1]
    assert "CES MOTIFS" in TEXT[disp.char_start:disp.char_end]
    assert all(s.kind == "zone" for s in parsed.segments)


def test_visa_and_rapprochement_edges():
    rels = parse_decision(DECISION).relations
    visa = [r for r in rels if r.relationship_type == RelationshipType.INTERPRETS]
    assert len(visa) == 2
    # the Légifrance id is lifted from the visa URL → a resolvable destination
    assert visa[0].dst_id == "LEGIARTI000032041571"
    assert visa[1].dst_id is None  # no URL → dangling on the title
    rapp = [r for r in rels if r.relationship_type == RelationshipType.CONSIDERS]
    assert len(rapp) == 1
    assert rapp[0].dst_id is None  # decisionLink carries no ECLI
    assert "18-11.916" in rapp[0].raw_citation_string


class _Resp:
    def __init__(self, payload): self._p = payload; self.status_code = 200
    def json(self): return self._p


class _FakePiste:
    """Stands in for PisteClient — records params, returns queued payloads."""
    def __init__(self, payloads): self._payloads = list(payloads); self.calls = []
    def configured(self): return True
    def get(self, url, params=None, headers=None):
        self.calls.append((url, params or {}))
        return _Resp(self._payloads.pop(0))


def test_discover_pages_export_until_next_batch_null():
    page1 = {"results": [DECISION], "next_batch": "https://.../export?batch=1", "batch": 0}
    page2 = {"results": [dict(DECISION, ecli="ECLI:FR:CCASS:2021:C100401")],
             "next_batch": None, "batch": 1}
    fake = _FakePiste([page1, page2])
    adapter = FrJudilibreAdapter(client=fake)
    stubs = list(adapter.discover("2021-05-01"))
    assert [s.stable_id for s in stubs] == [
        "ECLI:FR:CCASS:2021:C100400", "ECLI:FR:CCASS:2021:C100401"]
    # export params carry the update-date watermark + resolve_references
    assert fake.calls[0][1]["date_type"] == "update"
    assert fake.calls[0][1]["date_start"] == "2021-05-01"
    assert fake.calls[0][1]["resolve_references"] == "true"
    # the exported decision is stashed so fetch needn't re-request
    rec = adapter.fetch(stubs[0])
    assert rec.ecli == "ECLI:FR:CCASS:2021:C100400"
    assert rec.text == TEXT and rec.segments
    # fetch used the stash — no extra /decision call was made
    assert len(fake.calls) == 2


def test_discover_no_credentials_yields_nothing():
    class _Unconfigured(_FakePiste):
        def configured(self): return False
    adapter = FrJudilibreAdapter(client=_Unconfigured([]))
    assert list(adapter.discover(None)) == []
