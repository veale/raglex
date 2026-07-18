"""Ofcom enforcement adapter — listing + detail parsing, PDF classification (inline
case docs vs referenced guidance), OSA edges, guidance cross-links, and the update
content hash. Network-free."""

from __future__ import annotations

import pytest

from raglex.adapters.ofcom_enforcement import (
    OfcomEnforcementAdapter,
    OSA_ID,
    classify_pdf,
    parse_detail,
    parse_listing,
    _action_slug,
    _content_hash,
)
from raglex.core.models import DocType, RelationshipType

LISTING = """
<div class="col-md-4 onethird search-results-block">
  <a href="https://www.ofcom.org.uk/online-safety/protecting-children/investigation-into-acme-under-section-12">
    <div class="info-card"><div class="d-flex">
      <h3 class="info-card-header"> Investigation into Acme's compliance under section 12 </h3></div>
      <div class="serach-date"><p class="mt-0">Published: 16 July 2026</p></div>
      <p> Ofcom has opened an investigation into Acme under Part 3 of the Online Safety Act. </p>
    </div></a></div>
<div class="col-md-4 onethird search-results-block">
  <a href="https://www.ofcom.org.uk/x/direct-decision.pdf?v=9"><div class="info-card">
    <h3 class="info-card-header"> A direct PDF result </h3><p class="mt-0">Published: 1 Jan 2026</p><p> x </p></div></a></div>
"""

DETAIL = """
<h1>Investigation into Acme's compliance under section 12</h1>
<span class="status-pill">Open</span>
<div class="standard-content-area">
  <p>Ofcom has opened an investigation into Acme Ltd under section 12 of the Online Safety Act 2023.</p>
  <p>Part 3 of the Online Safety Act imposes duties on user-to-user services.</p>
  <a href="/siteassets/decisions/acme/provisional-decision.pdf?v=101">Provisional Decision — Acme</a>
  <a href="/siteassets/il/illegal-harms/online-safety-enforcement-guidance.pdf?v=414891">Online Safety Enforcement Guidance</a>
</div>
<footer>Follow us</footer>
"""


def test_parse_listing_skips_direct_pdf_results():
    items = parse_listing(LISTING)
    assert len(items) == 1  # the direct-PDF result is left to ofcom-osa
    assert items[0].title == "Investigation into Acme's compliance under section 12"
    assert str(items[0].published) == "2026-07-16"
    assert _action_slug(items[0].url) == "ofcom-enf/investigation-into-acme-under-section-12"


def test_classify_pdf_case_docs_inline_guidance_references():
    assert classify_pdf("Penalty Notice", "") == ("penalty", True)
    assert classify_pdf("Confirmation Decision", "") == ("confirmation-decision", True)
    assert classify_pdf("Provisional Decision — Acme", "") == ("provisional-decision", True)
    assert classify_pdf("Online Safety Enforcement Guidance", "") == ("guidance", False)
    assert classify_pdf("Protection of Children Code of Practice", "") == ("code", False)
    assert classify_pdf("Something Unusual", "") == ("document", True)  # default: inline


def test_parse_detail_status_narrative_and_pdf_classification():
    d = parse_detail(DETAIL)
    assert d.status == "Open" and d.title.startswith("Investigation into Acme")
    assert "section 12 of the Online Safety Act 2023" in d.narrative
    kinds = {p.kind: p.inline for p in d.pdfs}
    assert kinds == {"provisional-decision": True, "guidance": False}
    # a stable content hash exists and changes with the doc set
    assert _content_hash(d) and _content_hash(d) != _content_hash(parse_detail(
        DETAIL.replace("provisional-decision.pdf?v=101", "provisional-decision.pdf?v=102")))


class _Resp:
    def __init__(self, content):
        self.content = content if isinstance(content, bytes) else content.encode()


def _tiny_pdf(text: str) -> bytes:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    words = text.split()
    for i in range(0, len(words), 8):
        page.insert_text((72, 72 + 14 * (i // 8)), " ".join(words[i:i + 8]))
    return doc.tobytes()


class _FakeClient:
    def __init__(self, listing, detail, pdf):
        self.listing, self.detail, self.pdf = listing, detail, pdf

    def get(self, url, headers=None):
        if url.endswith(".pdf") or "?v=" in url:
            return _Resp(self.pdf)
        if "/enforcement?" in url:
            return _Resp(self.listing)
        return _Resp(self.detail)


def test_fetch_combines_html_and_case_pdf_and_links_osa():
    pdf = _tiny_pdf("Provisional decision: Acme breached section 12 duties. " * 20)
    ad = OfcomEnforcementAdapter(client=_FakeClient(LISTING, DETAIL, pdf))
    stubs = list(ad.discover(None))
    assert len(stubs) == 1
    rec = ad.fetch(stubs[0])
    assert rec.doc_type == DocType.DECISION and rec.court == "Ofcom"
    assert rec.extra["status"] == "Open" and rec.extra["regime"] == OSA_ID
    # the case-specific PDF is inlined into the record text; the guidance PDF is not
    assert "Provisional decision: Acme" in rec.text
    docs = {d["kind"]: d["inlined"] for d in rec.extra["documents"]}
    assert docs == {"provisional-decision": True, "guidance": False}
    edges = [(r.relationship_type, r.dst_id, r.dst_anchor) for r in rec.relations]
    # base OSA link + section + Part pinpoints from the title/summary
    assert (RelationshipType.INTERPRETS, OSA_ID, None) in edges
    assert (RelationshipType.INTERPRETS, OSA_ID, "s. 12") in edges
    assert (RelationshipType.INTERPRETS, OSA_ID, "Part 3") in edges
    # the referenced guidance PDF cross-links to the held ofcom-osa doc (shared slug)
    assert (RelationshipType.MENTIONS,
            "ofcom/illegal-harms/online-safety-enforcement-guidance", None) in edges


def test_update_detection_hash_in_stub():
    pdf = _tiny_pdf("x " * 40)
    ad = OfcomEnforcementAdapter(client=_FakeClient(LISTING, DETAIL, pdf))
    stub = next(iter(ad.discover(None)))
    assert stub.hints["contenthash"]  # drives the pipeline's in-place re-fetch on change


def test_registry_and_taxonomy_wire_enforcement():
    from raglex.adapters.registry import get_adapter, source_catalog
    from raglex.citations.taxonomy import classify_document

    assert get_adapter("ofcom-enforcement").source == "ofcom-enforcement"
    cat = {s["key"]: s for s in source_catalog()}
    assert cat["ofcom-enforcement"]["can_incremental"] is True
    t = classify_document(source="ofcom-enforcement", doc_type="decision", stable_id="ofcom-enf/x")
    assert t.category == "guidance" and t.subtype == "ofcom-enforcement"


NON_OSA_DETAIL = """
<h1>Investigation into a broadcaster under section 325 of the Communications Act 2003</h1>
<span class="status-pill">Closed</span>
<div class="standard-content-area">
  <p>Ofcom investigated a licensed broadcaster for a breach of section 325 duties.</p>
  <a href="/siteassets/decisions/x/decision.pdf?v=5">Decision</a>
</div><footer>Follow us</footer>
"""


def test_non_osa_action_gets_no_forced_osa_edge():
    from raglex.core.models import Stub
    pdf = _tiny_pdf("Decision on a broadcasting standards breach. " * 20)

    class _C:
        def get(self, url, headers=None):
            return _Resp(pdf if ".pdf" in url else NON_OSA_DETAIL)
    ad = OfcomEnforcementAdapter(client=_C())
    d = parse_detail(NON_OSA_DETAIL)
    stub = Stub(stable_id="ofcom-enf/x", landing_url="u", raw_url="u", hint_date=None,
                title=d.title, hints={"item": type("I", (), {"summary": "", "url": "u", "title": d.title, "published": None})(),
                                      "detail": d, "contenthash": _content_hash(d)})
    rec = ad.fetch(stub)
    # the OSA is never named → no interprets edge to the OSA is asserted, and no regime
    assert all(r.dst_id != OSA_ID for r in rec.relations)
    assert "regime" not in rec.extra
