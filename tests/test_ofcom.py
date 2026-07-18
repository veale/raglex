"""Ofcom OSA adapter — page parsing, version tokens, supersession chains, and the
Online Safety Act edges (base + Part pinpoints + supersedes). Network-free."""

from __future__ import annotations

import pytest

from raglex.adapters.ofcom import (
    OfcomOSAAdapter,
    OSA_ID,
    parse_page,
    supersession_edges,
    _slug,
)
from raglex.core.models import DocType, RelationshipType

PAGE = """
<div class="rich-text-block"><h2>Causes and impacts of harms</h2></div>
<a class="file-download" href="/siteassets/il/illegal-harms/updates/register-of-risks.pdf?v=419946">
  Register of Risks (Updated) • PDF • 4.47 MB • 25 June 2026</a>
<a class="file-download" href="/siteassets/il/illegal-harms/register-of-risks.pdf?v=419942">
  Register of Risks (superseded) • PDF • 5.69 MB • 7 February 2025</a>
<div class="rich-text-block"><h2>Other online safety guidance</h2></div>
<a class="file-download" href="/siteassets/il/main-documents/part-3-guidance-on-highly-effective-age-assurance.pdf?v=395680">
  Part 3 Guidance on highly effective age assurance • PDF • 394.54 KB • 16 January 2025</a>
<a class="file-download" href="/siteassets/il/codes/draft-illegal-content-codes-of-practice-for-search-services.pdf?v=392428">
  DRAFT Illegal content Codes of Practice for search services (PDF, 660.05 KB)</a>
"""


def test_parse_page_extracts_titles_status_versions_dates_categories():
    docs = parse_page(PAGE)
    by = {_slug(d.href): d for d in docs}
    upd = by["ofcom/updates/register-of-risks"]
    assert upd.title == "Register of Risks (Updated)" and upd.status == "current"
    assert upd.version == "419946" and str(upd.published) == "2026-06-25"
    assert upd.category == "Causes and impacts of harms"
    old = by["ofcom/illegal-harms/register-of-risks"]
    assert old.status == "superseded" and old.version == "419942"
    # base title groups the two versions
    assert upd.base_title.lower() == old.base_title.lower() == "register of risks"
    # OSA Part read from the title
    part = by["ofcom/main-documents/part-3-guidance-on-highly-effective-age-assurance"]
    assert part.parts == ("Part 3",)
    # a draft is flagged, and the "(PDF, 660.05 KB)" size form is parsed
    draft = next(d for d in docs if "draft" in d.href)
    assert draft.status == "draft" and draft.size == "660.05 KB"


def test_supersession_edges_link_current_to_superseded():
    edges = supersession_edges(parse_page(PAGE))
    assert edges == {"ofcom/updates/register-of-risks":
                     ["ofcom/illegal-harms/register-of-risks"]}


class _Resp:
    def __init__(self, content):
        self.content = content if isinstance(content, bytes) else content.encode()


class _FakeClient:
    def __init__(self, page, pdf=b"%PDF-"):
        self.page, self.pdf = page, pdf

    def get(self, url, headers=None):
        return _Resp(self.pdf if url.endswith(".pdf") or "?v=" in url else self.page)


def _tiny_pdf(text: str) -> bytes:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    words = text.split()
    for i in range(0, len(words), 8):
        page.insert_text((72, 72 + 14 * (i // 8)), " ".join(words[i:i + 8]))
    return doc.tobytes()


def test_discover_yields_versioned_stubs_with_supersession():
    ad = OfcomOSAAdapter(client=_FakeClient(PAGE))
    stubs = {s.stable_id: s for s in ad.discover(None)}
    upd = stubs["ofcom/updates/register-of-risks"]
    assert upd.hints["contenthash"] == "419946"          # version token = change signal
    assert upd.hints["supersedes"] == ["ofcom/illegal-harms/register-of-risks"]
    assert upd.raw_url.endswith("register-of-risks.pdf?v=419946")


def test_fetch_links_to_osa_and_supersedes_and_titles_it():
    pdf = _tiny_pdf("This guidance concerns section 9 of the Online Safety Act 2023. " * 20)
    ad = OfcomOSAAdapter(client=_FakeClient(PAGE, pdf))
    stubs = {s.stable_id: s for s in ad.discover(None)}
    rec = ad.fetch(stubs["ofcom/updates/register-of-risks"])
    assert rec.doc_type == DocType.GUIDANCE and rec.court == "Ofcom"
    assert rec.title == "Register of Risks (Updated)"
    assert rec.extra["status"] == "current" and rec.extra["regime"] == OSA_ID
    assert rec.extra["category"] == "Causes and impacts of harms"
    edges = [(r.relationship_type, r.dst_id, r.dst_anchor) for r in rec.relations]
    assert (RelationshipType.INTERPRETS, OSA_ID, None) in edges          # base OSA link
    assert (RelationshipType.SUPERSEDES, "ofcom/illegal-harms/register-of-risks", None) in edges
    # the Part-named doc pinpoints the OSA Part
    part = ad.fetch(stubs["ofcom/main-documents/part-3-guidance-on-highly-effective-age-assurance"])
    assert (RelationshipType.INTERPRETS, OSA_ID, "Part 3") in \
        [(r.relationship_type, r.dst_id, r.dst_anchor) for r in part.relations]


def test_registry_and_taxonomy_wire_ofcom():
    from raglex.adapters.registry import get_adapter, source_catalog
    from raglex.citations.taxonomy import classify_document

    assert get_adapter("ofcom-osa").source == "ofcom-osa"
    cat = {s["key"]: s for s in source_catalog()}
    assert cat["ofcom-osa"]["can_incremental"] is True and cat["ofcom-osa"]["kind"] == "guidance"
    t = classify_document(source="ofcom-osa", doc_type="guidance", stable_id="ofcom/x")
    assert t.category == "guidance" and t.subtype == "ofcom-osa"


def test_osa_sections_resolve_in_text():
    from raglex.citations.extractor import extract_citations
    by = {c.raw: c for c in extract_citations(
        "See section 9 of the Online Safety Act 2023 and s.121 of the Online Safety Act.")}
    assert by["section 9 of the Online Safety Act 2023"].candidate_id == "ukpga/2023/50"
    assert by["section 9 of the Online Safety Act 2023"].pinpoint == "s. 9"
