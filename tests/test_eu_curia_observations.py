"""InfoCuria written-observations discovery, identity and fetch normalisation."""

from __future__ import annotations

from datetime import date

from raglex.adapters.eu_curia_observations import (
    EUCuriaObservationsAdapter,
    judgment_celex,
    observation_stubs,
)
from raglex.core.models import DocType, RelationshipType


def _response():
    return {
        "totalHits": 1,
        "searchHits": [{
            "content": {
                "publishedId": "C-492/23",
                "procedureId": "C/0492/23/00000000RP/01/P/01",
                "usualNameML": [{"fr": "Russmedia"},
                                  {"en": "Russmedia Digital and Inform Media Press"}],
            },
            "innerHits": {"document": {"totalHits": 2, "searchHits": [
                {"content": {
                    "logicDocId": "id_320359", "docTypeCode": "OBSRP_PUB",
                    "docDate": "2023-12-12", "author": "Commission",
                    "authorCode": "NAT_ACM",
                    "idProcedure": "C/0492/23/00000000RP/01/P/01",
                    "groupByLogicalId": [
                        {"docLang": "FR", "formats": ["PDF"]},
                        {"docLang": "RO", "formats": ["PDF"]},
                    ],
                }},
                # Other procedural documents in an unfiltered/malformed response are ignored.
                {"content": {
                    "logicDocId": "id_278403", "docTypeCode": "DDP",
                    "docDate": "2023-08-03",
                    "groupByLogicalId": [{"docLang": "RO", "formats": ["PDF"]}],
                }},
            ]}},
        }],
    }


def test_judgment_celex_matches_eu_case_grammar():
    assert judgment_celex("C-492/23") == "62023CJ0492"
    assert judgment_celex("T-8/93") == "61993TJ0008"
    assert judgment_celex("F‑12/05") == "62005FJ0012"
    assert judgment_celex("not a case") is None


def test_observation_stubs_are_one_per_language():
    stubs = observation_stubs(_response(), cutoff=date(2021, 1, 1))
    assert [s.stable_id for s in stubs] == [
        "curia/observations/320359/fr", "curia/observations/320359/ro"]
    assert stubs[0].raw_url.endswith("/320359/FR/PDF")
    assert stubs[0].hints["case_number"] == "C-492/23"
    assert "Commission" in stubs[0].title


class _Response:
    def __init__(self, *, payload=None, content=b""):
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


class _Client:
    def __init__(self):
        self.posts = 0

    def request(self, method, url, **kwargs):
        assert method == "POST"
        self.posts += 1
        return _Response(payload=_response())

    def get(self, url, **kwargs):
        assert url.endswith("/320359/FR/PDF")
        return _Response(content=b"%PDF-1.7 fixture")


def test_discovery_full_walk_and_fetch(monkeypatch):
    from raglex.adapters import eu_curia_observations as module

    client = _Client()
    adapter = EUCuriaObservationsAdapter(client=client, years=10)
    stubs = list(adapter.discover("2099-01-01", max_pages=1))
    # ``since`` is intentionally not a cutoff: late publication of an older filing must
    # be rediscovered and left to stable-id deduplication.
    assert len(stubs) == 2
    assert client.posts == 1

    monkeypatch.setattr(
        module, "text_or_ocr",
        lambda raw, max_pages: (
            "Directive 2000/31 and Regulation (EU) 2016/679; Case C-311/18.",
            False, [(1, 0, 62)], "fixture-pdf"))
    record = adapter.fetch(stubs[0])
    assert record is not None
    assert record.doc_type is DocType.PREPARATORY
    assert record.language == "fr"
    assert record.raw_bytes.startswith(b"%PDF-")
    assert record.relations[0].relationship_type is RelationshipType.RELATED_TO
    assert record.relations[0].dst_id == "62023CJ0492"
    assert record.extra["author"] == "Commission"
    assert record.segments[0].label == "p. 1"


def test_old_filing_is_outside_backfill_window():
    assert observation_stubs(_response(), cutoff=date(2024, 1, 1)) == []


def test_impossible_future_filing_date_is_rejected():
    payload = _response()
    payload["searchHits"][0]["innerHits"]["document"]["searchHits"][0]["content"][
        "docDate"] = "2054-05-16"
    assert observation_stubs(payload, cutoff=date(2021, 1, 1)) == []
