from __future__ import annotations

import json

from raglex.adapters.de_bundestag import (
    BundestagDrucksachenAdapter,
    canonical_wd_id,
    drucksache_aliases,
    drucksache_id,
    parse_wd_fragment,
    segment_drucksache,
    wd_aliases,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, source_catalog
from raglex.citations.extractor import extract_citations
from raglex.core.models import RelationshipType


class Response:
    def __init__(self, payload=None, *, content=None, status_code=200):
        self._payload = payload
        self.content = (json.dumps(payload).encode() if content is None else content)
        self.status_code = status_code

    def json(self):
        return self._payload


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def test_drucksache_identity_and_aliases_are_citation_shaped():
    assert drucksache_id("BT-Drs. 20/5548") == "de/bt-drs/20/5548"
    assert drucksache_aliases("20/5548") == [
        "BT-Drs 20/5548", "BT-Drs. 20/5548", "BT-Drucksache 20/5548",
        "Drucksache 20/5548", "BTDrs 20/5548", "Bundestagsdrucksache 20/5548",
    ]


def test_wd_identity_normalises_old_and_new_forms():
    assert canonical_wd_id("WD 3 - 3000 - 045/21") == "WD 3 - 3000 - 045/21"
    assert canonical_wd_id("WD5-077/2026") == "WD 5 - 077/26"
    assert "WD3/045/21" in wd_aliases("WD 3 - 3000 - 045/21")


def test_wd_fragment_reads_table_once_and_retains_resume_offset():
    raw = b"""
    <template data-js-document-results="table"><tr class="m-documents__tableRow">
      <td class="m-documents__tableData">14. Juli 2026</td>
      <td class="m-documents__tableData"><a href="https://www.bundestag.de/resource/blob/1194936/WD-5-077-26.pdf">Literatur zu Windenergie, WD 5 - 077/26</a><div>PDF | 137 KB</div></td>
    </tr></template>
    <template data-js-document-results="list"><a href="https://www.bundestag.de/resource/blob/1194936/WD-5-077-26.pdf">duplicate</a></template>
    """
    rows = parse_wd_fragment(raw, offset=50)
    assert len(rows) == 1
    assert rows[0].stable_id == "de/bt-wd/wd-5-077-26"
    assert rows[0].hint_date.isoformat() == "2026-07-14"
    assert rows[0].hints["resume_offset"] == 50


def test_begruendung_stack_segments_and_links_only_explicit_heading_target():
    text = """Begr\u00fcndung
A. Allgemeiner Teil
Allgemeine Erw\u00e4gungen.
B. Besonderer Teil
Zu Artikel 1
Rahmen.
Zu Nummer 3 (\u00a7 45b Absatz 2 BDSG)
Diese Vorschrift wird eingef\u00fcgt und erw\u00e4hnt daneben \u00a7 10 TKG.
Zu Buchstabe a
Einzelheit.
Vereinbarkeit mit dem Recht der Europ\u00e4ischen Union
Die Regelung ist unionsrechtskonform.
"""
    segments, relations = segment_drucksache(text)
    assert [s.kind for s in segments] == [
        "section", "section", "provision-commentary", "provision-commentary",
        "provision-commentary", "eu-compatibility",
    ]
    assert segments[4].label.endswith("Zu Buchstabe a")
    assert len(relations) == 1
    assert relations[0].relationship_type == RelationshipType.INTERPRETS
    assert relations[0].dst_id == "de/gesetz/bdsg"
    assert relations[0].dst_anchor == "\u00a7 45b Abs. 2"
    assert "TKG" not in relations[0].raw_citation_string


def test_dip_delta_uses_updated_filter_and_cursor_stops_when_unchanged():
    item = {
        "id": "42", "dokumentnummer": "20/5548", "titel": "Entwurf eines Gesetzes",
        "datum": "2023-02-08", "aktualisiert": "2026-08-10T12:00:00+02:00",
        "drucksachetyp": "Gesetzentwurf", "herausgeber": "BT", "pdf_hash": "abc",
    }
    client = Client([
        Response({"numFound": 1, "cursor": "done", "documents": [item]}),
        Response({"numFound": 1, "cursor": "done", "documents": []}),
    ])
    adapter = BundestagDrucksachenAdapter(api_key="secret", types="Gesetzentwurf", client=client)
    rows = list(adapter.discover("2026-08-01T00:00:00+02:00"))
    assert [row.stable_id for row in rows] == ["de/bt-drs/20/5548"]
    assert client.calls[0][1]["params"]["f.aktualisiert.start"].startswith("2026-08-01")
    assert client.calls[0][1]["headers"] == {"Authorization": "ApiKey secret"}
    assert client.calls[1][1]["params"]["cursor"] == "done"


def test_dip_page_cap_applies_to_each_document_class_not_only_the_first():
    def item(dip_id, number, kind):
        return {"id": dip_id, "dokumentnummer": number, "titel": kind,
                "datum": "2026-08-10", "aktualisiert": "2026-08-10T12:00:00+02:00",
                "drucksachetyp": kind, "herausgeber": "BT", "pdf_hash": dip_id}

    client = Client([
        Response({"numFound": 2, "cursor": "bill-next",
                  "documents": [item("1", "21/100", "Gesetzentwurf")]}),
        Response({"numFound": 2, "cursor": "report-next",
                  "documents": [item("2", "21/101", "Bericht")]}),
    ])
    adapter = BundestagDrucksachenAdapter(
        api_key="secret", types="Gesetzentwurf,Bericht", client=client)
    rows = list(adapter.discover(None, max_pages=1))
    assert [row.stable_id for row in rows] == ["de/bt-drs/21/100", "de/bt-drs/21/101"]
    assert [call[1]["params"]["f.drucksachetyp"] for call in client.calls] == [
        "Gesetzentwurf", "Bericht"]


def test_dip_fetch_emits_structural_rights_and_alias_metadata():
    item = {
        "id": "42", "dokumentnummer": "20/5548", "titel": "Entwurf eines Gesetzes",
        "datum": "2023-02-08", "aktualisiert": "2026-08-10T12:00:00+02:00",
        "drucksachetyp": "Gesetzentwurf", "wahlperiode": 20,
        "text": "B. Besonderer Teil\nZu Artikel 1 (\u00a7 45b BDSG)\nErl\u00e4uterung.",
    }
    client = Client([Response(item)])
    adapter = BundestagDrucksachenAdapter(api_key="secret", client=client)
    stub = adapter._stub(item)
    record = adapter.fetch(stub)
    assert record.stable_id == "de/bt-drs/20/5548"
    assert record.title.startswith("BT-Drs 20/5548")
    assert record.extra["rights_status"] == "official-work-public-domain"
    assert record.segments and record.relations[0].dst_anchor == "\u00a7 45b"
    assert "BT-Drucksache 20/5548" in record.extra["aliases"]


def test_parliamentary_citations_round_trip_to_adapter_ids():
    cites = extract_citations(
        "Siehe BT-Drs. 20/5548, S. 12 und WD 3 - 3000 - 045/21.")
    assert [(c.candidate_id, c.method) for c in cites if c.method.startswith("de_bt_")] == [
        ("de/bt-drs/20/5548", "de_bt_drucksache"),
        ("de/bt-wd/wd-3-3000-045-21", "de_bt_wd"),
    ]


def test_bundestag_sources_are_registered_with_truthful_incremental_modes():
    catalog = {row["key"]: row for row in source_catalog()}
    assert ADAPTERS["de-bt-drucksachen"] is BundestagDrucksachenAdapter
    assert catalog["de-bt-drucksachen"]["kind"] == "preparatory"
    assert catalog["de-bt-wd"]["kind"] == "guidance"
    assert INCREMENTAL_MODE["de-bt-drucksachen"] == "server"
    assert INCREMENTAL_MODE["de-bt-wd"] == "early-stop"


def test_german_citator_cues_are_returned_verbatim_not_promoted_to_holdings():
    from raglex.facade import _treatment_cues

    cues = _treatment_cues(
        "Ausweislich der Begründung verlangt die unionsrechtskonforme Auslegung, "
        "entgegen der Auffassung der Vorinstanz, dieses Ergebnis.")
    assert {(cue["phrase"], cue["signal"]) for cue in cues} == {
        ("Ausweislich der Begründung", "legislative-intent"),
        ("unionsrechtskonforme Auslegung", "interpretive-method"),
        ("entgegen der Auffassung", "negative"),
    }
