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


def test_discover_pages_export_then_opens_the_next_window():
    """Batches inside one query, then the NEXT query. `next_batch` going null means the
    query's 10,000-result window is exhausted, not the register — Judilibre holds
    566,124 — so the walk re-opens at the newest update date it saw and only stops when
    a fresh window comes back empty."""
    page1 = {"results": [DECISION], "next_batch": "https://.../export?batch=1", "batch": 0}
    page2 = {"results": [dict(DECISION, ecli="ECLI:FR:CCASS:2021:C100401")],
             "next_batch": None, "batch": 1}
    page3 = {"results": [], "next_batch": None, "batch": 0}
    fake = _FakePiste([page1, page2, page3])
    adapter = FrJudilibreAdapter(client=fake)
    stubs = list(adapter.discover("2021-05-01"))
    assert [s.stable_id for s in stubs] == [
        "ECLI:FR:CCASS:2021:C100400", "ECLI:FR:CCASS:2021:C100401"]
    # export params carry the update-date watermark + resolve_references
    assert fake.calls[0][1]["date_type"] == "update"
    assert fake.calls[0][1]["date_start"] == "2021-05-01"
    assert fake.calls[0][1]["resolve_references"] == "true"
    # …and the second query starts from the newest update date the first one returned
    assert fake.calls[1][1]["batch"] == 1
    assert fake.calls[2][1] == {**fake.calls[2][1], "batch": 0,
                                "date_start": DECISION["update_date"]}
    # the exported decision is stashed so fetch needn't re-request
    rec = adapter.fetch(stubs[0])
    assert rec.ecli == "ECLI:FR:CCASS:2021:C100400"
    assert rec.text == TEXT and rec.segments
    # fetch used the stash — no extra /decision call was made
    assert len(fake.calls) == 3


def test_discover_no_credentials_yields_nothing():
    class _Unconfigured(_FakePiste):
        def configured(self): return False
    adapter = FrJudilibreAdapter(client=_Unconfigured([]))
    assert list(adapter.discover(None)) == []


def test_a_seed_runs_newest_first_and_stops_at_the_bulk_edge():
    """Both directions sweep the same feed; the direction only matters when the walk is
    cut short, which it always is (10,000 per query, 566,124 in the register). A seed
    ascending from the beginning spends its whole budget in the 1860s — the first real
    backfill stopped in July 1971 and reported success. Newest-first means a truncated
    run leaves the oldest hole, not the newest."""
    windows = {
        None: [{"id": "new", "decision_date": "2026-07-08", "update_date": "2026-08-07"}],
        "2026-08-07": [{"id": "older", "decision_date": "2025-09-01",
                        "update_date": "2025-10-01"}],
        "2025-10-01": [],
    }
    calls = []

    class _Client:
        def configured(self):
            return True

        def get(self, url, params=None, headers=None):
            calls.append(params or {})
            return _Resp({"results": windows.get((params or {}).get("date_end"), []),
                          "next_batch": None, "total": 566124})

    stubs = list(FrJudilibreAdapter(client=_Client(), since_date="2025-07-01").discover(None))
    assert [s.stable_id for s in stubs] == ["new", "older"]
    assert [c["order"] for c in calls] == ["desc", "desc", "desc"]
    # the upper bound steps DOWN through the feed…
    assert [c.get("date_end") for c in calls] == [None, "2026-08-07", "2025-10-01"]
    # …and every window is floored where the offline bulk already covers
    assert {c.get("date_start") for c in calls} == {"2025-07-01"}


def test_an_incremental_run_still_ascends_from_its_cursor():
    """With a cursor the feed is small and ordered, and ascending is what guarantees no
    update is stepped over."""
    calls = []

    class _Client:
        def configured(self):
            return True

        def get(self, url, params=None, headers=None):
            calls.append(params or {})
            return _Resp({"results": [], "next_batch": None})

    list(FrJudilibreAdapter(client=_Client()).discover("2026-01-01"))
    assert calls[0]["order"] == "asc"
    assert calls[0]["date_start"] == "2026-01-01"
    assert "date_end" not in calls[0]


def test_the_three_registers_are_separate_sources():
    """Judilibre is three registers behind one endpoint and asking for none of them gets
    you the first, which is why 1.3M appellate and first-instance decisions were
    invisible."""
    class _C:
        def configured(self): return True

    assert FrJudilibreAdapter(client=_C()).jurisdiction == "cc"
    assert FrJudilibreAdapter(client=_C()).source == "fr-judilibre"
    ca = FrJudilibreAdapter(jurisdiction="ca", client=_C())
    assert (ca.jurisdiction, ca.source) == ("ca", "fr-judilibre-ca")
    tj = FrJudilibreAdapter(jurisdiction="tj", client=_C())
    assert (tj.jurisdiction, tj.source) == ("tj", "fr-judilibre-tj")
    assert FrJudilibreAdapter(jurisdiction="nonsense", client=_C()).jurisdiction == "cc"


def test_the_export_asks_for_the_register_it_wants():
    calls = []

    class _C:
        def configured(self): return True
        def get(self, url, params=None, headers=None):
            calls.append(params or {})
            return _Resp({"results": [], "next_batch": None})

    list(FrJudilibreAdapter(jurisdiction="tj", client=_C()).discover("2026-01-01"))
    assert calls[0]["jurisdiction"] == "tj"


def test_an_rg_number_is_scoped_by_the_court_that_issued_it():
    """A pourvoi number is unique nationally; an RG number is unique only within its
    court. "24/00002" is a live docket at Nîmes and at Amiens on the same day, and the
    pipeline folds an ECLI-less record whose alias names another source's document —
    neither ca nor tj carries an ECLI, so a bare fr:pourvoi: key would have merged
    unrelated judgments from different cities into one node."""
    from raglex.adapters.fr_judilibre import _number_alias

    assert _number_alias({"jurisdiction": "Cour de cassation"}, "21-00400") == (
        "fr:pourvoi:21-00400")
    nimes = _number_alias(
        {"jurisdiction": "Cour d'appel", "location": "Cour d'appel de Nîmes"}, "24/00002")
    amiens = _number_alias(
        {"jurisdiction": "Cour d'appel", "location": "Cour d'appel d'Amiens"}, "24/00002")
    assert nimes != amiens
    assert nimes.startswith("fr:rg:") and "nimes" in nimes
    assert _number_alias({"jurisdiction": "Cour d'appel", "location": "x"}, None) is None


def test_the_bulk_and_the_api_can_recognise_the_same_appeal_judgment():
    """The DILA CAPP bulk keys a cour d'appel judgment JURITEXT…, Judilibre keys the same
    judgment by an opaque hash, and neither carries an ECLI — so the court-scoped RG key
    is the only identifier they share. Without it the live register would store a second
    copy of all 73,046 the bulk already holds."""
    from raglex.adapters.fr_judilibre import _number_alias
    from raglex.citations.french import rg_alias

    from_api = _number_alias(
        {"jurisdiction": "Cour d'appel", "location": "Cour d'appel de Nîmes"}, "1999/4512")
    from_bulk = rg_alias("Cour d'appel de Nîmes", "1999/4512")
    assert from_api == from_bulk == "fr:rg:cour-d-appel-de-nimes:1999/4512"
