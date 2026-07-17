"""DMA cases adapter — search-body construction, record grouping, DMA-statute edges,
article pinpoints, and the decision-list content hash for in-place monitoring.
Network-free (the ODSE search client is injected)."""

from __future__ import annotations

import json

from raglex.adapters.dma import (
    DMA_CELEX,
    DMACasesAdapter,
    articles_for,
    build_search_body,
    group_by_case,
)
from raglex.core.models import DocType, RelationshipType

# A realistic slice of the ODSE response for DMA.100209 (SP – Alphabet – Article 6(11)):
# one case + three decisions, each with a press release, mirroring the live shape.
CASE = {"metadataType": ["METADATA_CASE"], "caseNumber": ["DMA.100209"],
        "caseInstrument": ["InstrumentDMA"], "caseType": ["DmaComplianceCaseType"],
        "caseTitle": ["SP – Alphabet – Article 6(11)"],
        "caseLastDecisionDate": ["2026-07-15T22:00:00.000+0000"],
        "caseCorePlatformServices": ["DmaCPS002"], "caseConcernedObligations": ["DmaObligationAtoD"],
        "caseCompanies": ["Alphabet"],
        "caseTimelineEvents": [json.dumps({"items": [{"date": "2026-07-14T22:00:00Z",
                              "description": "Link to Comitology register", "type": "DMAPubEventType01",
                              "url": "https://ec.europa.eu/transparency/comitology-register/x"}]})]}


def _dec(ref, ip, pr_date, oj=None):
    md = {"metadataType": ["METADATA_DECISION"], "caseNumber": ["DMA.100209"],
          "metadataReference": [ref],
          "decisionPressReleases": [json.dumps({"items": [{"reference": ip, "publicationDate": pr_date}]})]}
    md["decisionOfficialJournalPublications"] = [json.dumps({"items": [{"reference": oj}]})] if oj \
        else [json.dumps({"items": [{}]})]
    return md


RECORDS = [
    CASE,
    _dec("DMA.100209-DEC{A}", "IP_26_1634", "2026-07-15T22:00:00Z"),
    _dec("DMA.100209-DEC{B}", "IP_26_825", "2026-04-15T22:00:00Z"),
    _dec("DMA.100209-DEC{C}", "IP_26_202", "2026-01-26T23:00:00Z", oj="C:2025:5000:1"),
    {"metadataType": ["METADATA_DECISION_ATTACHMENT"], "caseNumber": ["DMA.100209"],
     "metadataReference": ["DMA.100209-DEC{A}-ATT1"], "url": ["https://europa.eu/index_en.htm"]},
]


class _Resp:
    def __init__(self, content):
        self.content = content if isinstance(content, bytes) else content.encode()


class _FakeClient:
    """One page of results, then empty — mimics the paged ODSE API."""

    def __init__(self, records):
        self._records = records
        self.calls = []

    def request(self, method, url, *, params=None, content=None, headers=None):
        self.calls.append((method, params, headers))
        page = params["pageNumber"]
        results = [{"metadata": m} for m in self._records] if page == 1 else []
        return _Resp(json.dumps({"totalResults": len(self._records), "results": results}))


def test_build_search_body_is_multipart_with_three_blobs():
    body, ctype = build_search_body({"q": 1}, [{"s": 1}], ["a", "b"])
    assert ctype.startswith("multipart/form-data; boundary=")
    text = body.decode()
    assert text.count('filename="blob"') == 3
    assert 'name="query"' in text and 'name="sort"' in text and 'name="displayFields"' in text


def test_group_by_case_splits_record_types():
    g = group_by_case(RECORDS)
    b = g["DMA.100209"]
    assert b["case"] is CASE
    assert len(b["decisions"]) == 3 and len(b["attachments"]) == 1


def test_articles_for_reads_the_title():
    assert articles_for(group_by_case(RECORDS)["DMA.100209"]) == ["Article 6(11)"]


def test_discover_yields_one_stub_per_case_with_cursor_and_hash():
    ad = DMACasesAdapter(client=_FakeClient(RECORDS))
    stubs = list(ad.discover(None))
    assert [s.stable_id for s in stubs] == ["dma/DMA.100209"]
    s = stubs[0]
    assert s.title == "SP – Alphabet – Article 6(11)"
    assert s.hints["watermark"] == "2026-07-15T22:00:00.000+0000"
    assert s.hints["contenthash"]  # a decision-list digest for in-place refetch
    # incremental: a cursor at/after the last decision date yields nothing
    assert list(ad.discover("2026-07-15T22:00:00.000+0000")) == []
    # a cursor before it still yields the case
    assert len(list(ad.discover("2026-01-01T00:00:00.000+0000"))) == 1


def test_fetch_links_everything_to_the_dma_statute():
    ad = DMACasesAdapter(client=_FakeClient(RECORDS))
    rec = ad.fetch(next(iter(ad.discover(None))))
    assert rec.doc_type == DocType.DECISION and rec.court == "dma"
    assert str(rec.decision_date) == "2026-07-15"
    interprets = [(r.dst_id, r.dst_anchor) for r in rec.relations
                  if r.relationship_type == RelationshipType.INTERPRETS]
    # base link to the DMA regulation + the pinpointed article
    assert (DMA_CELEX, None) in interprets
    assert (DMA_CELEX, "Article 6(11)") in interprets
    # each press release becomes a presscorner mention
    prs = [r.raw_citation_string for r in rec.relations
           if r.relationship_type == RelationshipType.MENTIONS]
    assert any("IP_26_1634" in p for p in prs) and all("presscorner" in p for p in prs)
    # rich metadata captured, decisions newest-first, OJ ref retained
    assert rec.extra["case_number"] == "DMA.100209"
    assert rec.extra["dma_articles"] == ["Article 6(11)"]
    assert [d["press_releases"][0] for d in rec.extra["decisions"]] == \
        ["IP_26_1634", "IP_26_825", "IP_26_202"]
    assert rec.extra["official_journal"] == ["C:2025:5000:1"]
    assert "IP_26_1634" in (rec.text or "") and "Article 6(11)" in (rec.text or "")


def test_registry_wires_dma_cases():
    from raglex.adapters.registry import IN_SCOPE_SOURCES, get_adapter, source_catalog
    from raglex.citations.taxonomy import classify_document

    assert get_adapter("dma-cases").source == "dma-cases"
    assert "dma-cases" in IN_SCOPE_SOURCES
    cat = {s["key"]: s for s in source_catalog()}
    assert cat["dma-cases"]["can_incremental"] is True
    t = classify_document(source="dma-cases", doc_type="decision", stable_id="dma/DMA.100209")
    assert t.category == "guidance" and t.subtype == "dma"
