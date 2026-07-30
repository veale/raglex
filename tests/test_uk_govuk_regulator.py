import json

from raglex.adapters.uk_govuk_regulator import CONTENT, GOVUKRegulatorAdapter, content_text
from raglex.core.models import DocType, Stub


def test_content_text_combines_structured_parts_and_description():
    content = {
        "description": "A formal decision.",
        "details": {
            "body": "<p>Under the Competition Act 1998.</p>",
            "parts": [{"title": "Outcome", "body": "<p>See [2024] UKSC 1.</p>"}],
        },
    }
    text = content_text(content)
    assert "Competition Act 1998" in text
    assert "Outcome\nSee [2024] UKSC 1." in text
    assert text.endswith("A formal decision.")


class _Response:
    def __init__(self, data=None, *, content=None):
        self._data = data
        self.content = content if content is not None else json.dumps(data).encode()

    def json(self):
        return self._data


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses[url]
        return value() if callable(value) else value


def test_cma_guidance_follows_html_children_and_declares_safe_default():
    parent_url = f"{CONTENT}/government/publications/unfair-commercial-practices-cma207"
    child_url = f"{CONTENT}/government/publications/cma207/body"
    parent = {
        "base_path": "/government/publications/unfair-commercial-practices-cma207",
        "title": "Unfair commercial practices: CMA207",
        "first_published_at": "2025-04-04T00:00:00Z",
        "details": {
            "body": "<p>Overview.</p>",
            "attachments": [
                {"attachment_type": "html", "title": "Unfair commercial practices",
                 "url": "/government/publications/cma207/body"},
                {"attachment_type": "file", "title": "Unfair commercial practices",
                 "url": "https://assets.test/cma207.pdf",
                 "content_type": "application/pdf", "unique_reference": "CMA207"},
            ],
        },
    }
    child = {"title": "Unfair commercial practices",
             "details": {"body": "<p>Section 225 of the 2024 Act applies.</p>"}}
    client = _Client({parent_url: _Response(parent), child_url: _Response(child)})
    adapter = GOVUKRegulatorAdapter(
        source="uk-cma-guidance", organisation="competition-and-markets-authority",
        court="CMA", record_doc_type=DocType.GUIDANCE,
        require_recognized_legal_citation=False, client=client,
    )
    record = adapter.fetch(Stub(
        stable_id="x", raw_url=parent_url, landing_url="https://www.gov.uk/x", hints={}
    ))
    assert record and "Section 225" in record.text
    assert "https://assets.test/cma207.pdf" not in [url for url, _ in client.calls]
    assert record.extra["aliases"] == ["CMA207"]
    assert record.extra["citation_default_instrument"]["id"] == "ukpga/2024/13"
