"""GOV.UK Employment Tribunal adapter — pure parsing and a fake-client round trip."""

from __future__ import annotations

import json
from datetime import date

from raglex.adapters.uk_et import (
    UKEmploymentTribunalAdapter,
    case_numbers,
    parse_search_page,
    uket_id,
)
from raglex.core.models import DocType


_SEARCH = {
    "total": 133027,
    "results": [{
        "title": "Mrs J Munley v Kernow Learning Academy Trust: 6031054/2025",
        "link": (
            "/employment-tribunal-decisions/"
            "mrs-j-munley-v-kernow-learning-academy-trust-6031054-slash-2025"
        ),
        "public_timestamp": "2026-07-27T10:45:09Z",
        "tribunal_decision_decision_date": "2026-07-03",
        "tribunal_decision_categories": ["disability-discrimination"],
        "tribunal_decision_country": "england-and-wales",
    }],
}

_CONTENT = {
    "content_id": "d03bd86c-4e5b-4a71-92c3-1c2c7ccc6a84",
    "title": "Mrs J Munley v Kernow Learning Academy Trust: 6031054/2025",
    "first_published_at": "2026-07-27T11:45:09+01:00",
    "public_updated_at": "2026-07-27T11:45:09+01:00",
    "details": {
        "metadata": {
            "hidden_indexable_content": (
                "Case Number: 6031054/2025\nEMPLOYMENT TRIBUNALS\n"
                "JUDGMENT\nThe claimant was disabled at the relevant time. " * 3
            ),
            "tribunal_decision_categories": ["disability-discrimination"],
            "tribunal_decision_country": "england-and-wales",
            "tribunal_decision_decision_date": "2026-07-03",
        },
        "attachments": [],
    },
}


class _Response:
    def __init__(self, data):
        self.content = json.dumps(data).encode()

    def json(self):
        return json.loads(self.content)


class _Client:
    def __init__(self):
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return _Response(_SEARCH if url.endswith("/api/search.json") else _CONTENT)


def test_case_number_identity_matches_bailii_seed():
    assert case_numbers("A v B: 3314122/2020 and 3300001/2021") == [
        "3314122/2020", "3300001/2021"
    ]
    assert uket_id("3314122/2020", date(2023, 4, 23)) == "uket/2023/3314122_2020"


def test_search_page_is_tolerant_of_bad_json():
    total, rows = parse_search_page(json.dumps(_SEARCH))
    assert total == 133027 and rows[0]["tribunal_decision_country"] == "england-and-wales"
    assert parse_search_page(b"not-json") == (0, [])


def test_adapter_discovers_and_fetches_official_full_text():
    client = _Client()
    adapter = UKEmploymentTribunalAdapter(client=client)
    stubs = list(adapter.discover(None, max_pages=1))
    assert len(stubs) == 1
    stub = stubs[0]
    assert stub.stable_id == "uket/2026/6031054_2025"
    assert stub.hints["watermark"] == "2026-07-27T10:45:09Z"
    assert stub.hints["feed_total"] == 133027

    record = adapter.fetch(stub)
    assert record is not None
    assert record.stable_id == stub.stable_id
    assert record.doc_type == DocType.JUDGMENT
    assert record.court == "uket"
    assert record.decision_date == date(2026, 7, 3)
    assert "EMPLOYMENT TRIBUNALS" in record.text
    assert record.extra["case_numbers"] == ["6031054/2025"]
    assert record.extra["neutral_citation"] == "[2026] UKET 6031054/2025"
    assert record.raw_ext == "json"


def test_discovery_stops_at_publication_watermark():
    adapter = UKEmploymentTribunalAdapter(client=_Client())
    assert list(adapter.discover("2026-07-27T10:45:09Z")) == []
