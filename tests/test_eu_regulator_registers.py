from raglex.adapters.eu_regulator_registers import (
    ESMASanctionsAdapter,
    parse_esa_appeals,
    parse_esma_sanctions,
    parse_srb_appeals,
)
from raglex.adapters.registry import source_catalog


def test_parse_esma_sanctions_keeps_structured_context_and_clean_entity():
    total, rows = parse_esma_sanctions({
        "response": {
            "numFound": 1,
            "docs": [{
                "id": "sn15116",
                "sn_sanctionEsmaID": 15116,
                "sn_entityName": "<a href='details'>Example Bank plc</a>",
                "sn_sanctionLegalFrameworkName": "MAR",
                "sn_date": "2026-06-24T00:00:00Z",
                "sn_modificationDate": "2026-07-27T00:00:00Z",
                "sn_translatedText": (
                    "Article 14 of Regulation (EU) No 596/2014 was infringed."
                ),
                "sn_ncaCodeFullName": "Example Authority",
                "sn_countryName": "IRELAND",
                "sn_natureFullName": "Administrative sanction and measure",
                "sn_lan_orig": "English",
                "_version_": 123,
            }],
        },
    })
    assert total == 1
    assert rows[0]["stable_id"] == "eu/esma/sanction/15116"
    assert rows[0]["doc_id"] == "sn15116"
    assert rows[0]["entity"] == "Example Bank plc"
    assert "Regulation (EU) No 596/2014" in rows[0]["text"]
    assert str(rows[0]["changed"]) == "2026-07-27"


def test_parse_esa_board_of_appeal_cards():
    rows = parse_esa_appeals(
        """
        <div class="ecl-file">
          <ul><li class="ecl-file__detail-meta-item">16 JULY 2026</li></ul>
          <div class="ecl-file__title">2026-06-30 – Decision by D against EBA.pdf</div>
          <a href="/document/download/12473d49-0b6f-480d-a8b6-26d7fc515b3a_en?filename=x.pdf">
            Download
          </a>
        </div>
        """
    )
    assert rows == [{
        "stable_id": "eu/esas-boa/12473d49-0b6f-480d-a8b6-26d7fc515b3a",
        "uuid": "12473d49-0b6f-480d-a8b6-26d7fc515b3a",
        "title": "2026-06-30 – Decision by D against EBA",
        "url": (
            "https://www.eiopa.europa.eu/document/download/"
            "12473d49-0b6f-480d-a8b6-26d7fc515b3a_en?filename=x.pdf"
        ),
        "published": rows[0]["published"],
        "decided": rows[0]["decided"],
        "respondent": "EBA",
    }]
    assert str(rows[0]["published"]) == "2026-07-16"
    assert str(rows[0]["decided"]) == "2026-06-30"


def test_parse_srb_appeal_panel_row():
    rows = parse_srb_appeals(
        """
        <div class="views-row"><h3>
          <a href="/system/files/case-1-2025.pdf">Case 1/2025</a>
        </h3>
        <span class="field-name-srb-publishing-date">
          <time datetime="2026-02-17T12:46:04Z">17/02/2026</time>
        </span>
        <span class="field-name-srb-decision-date">
          <time datetime="2025-12-18T12:00:00Z">18/12/2025</time>
        </span>
        <div class="srb-case-document__srb-description">
          Appeal against the MREL decision
        </div></div>
        """
    )
    assert len(rows) == 1
    assert rows[0]["case"] == "Case 1/2025"
    assert rows[0]["title"] == (
        "SRB Appeal Panel — Case 1/2025 — Appeal against the MREL decision"
    )
    assert str(rows[0]["published"]) == "2026-02-17"
    assert str(rows[0]["decided"]) == "2025-12-18"


class _Response:
    def __init__(self, payload):
        import json

        self.content = json.dumps(payload).encode()


class _ESMAClient:
    def __init__(self):
        self.params = None

    def get(self, url, **kwargs):
        self.params = kwargs["params"]
        return _Response({
            "response": {
                "numFound": 1,
                "docs": [{
                    "id": "sn1",
                    "sn_sanctionEsmaID": 1,
                    "sn_sanctionLegalFrameworkName": "MAR",
                    "sn_date": "2026-06-24T00:00:00Z",
                    "sn_modificationDate": "2026-07-27T00:00:00Z",
                    "sn_text": "Regulation (EU) No 596/2014 applies.",
                }],
            },
        })


def test_esma_adapter_uses_server_date_filter_and_search_gate():
    client = _ESMAClient()
    adapter = ESMASanctionsAdapter(client=client)
    stubs = list(adapter.discover("2026-07-20", max_pages=1))
    assert len(stubs) == 1
    assert client.params["fq"].startswith(
        "sn_modificationDate:[2026-07-20T00:00:00Z"
    )
    record = adapter.fetch(stubs[0])
    assert record.extra["require_recognized_legal_citation"] is True
    assert record.extra["legal_framework"] == "MAR"


def test_new_eu_regulator_sources_are_live_registered():
    catalog = {row["key"]: row for row in source_catalog()}
    assert catalog["eu-esma-sanctions"]["incremental_mode"] == "server"
    assert catalog["eu-esas-boa"]["incremental_mode"] == "early-stop"
    assert catalog["eu-srb-appeals"]["incremental_mode"] == "early-stop"
    assert all(
        catalog[key]["can_incremental"]
        for key in ("eu-esma-sanctions", "eu-esas-boa", "eu-srb-appeals")
    )
