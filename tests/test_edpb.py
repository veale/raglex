"""EDPB adapter — sitemap/document-page/OSS-register parsing (pure), discovery
cursors, WAF-block handling, and OCR detection. Network-free."""

from __future__ import annotations

import pytest

from raglex.adapters.edpb import (
    EDPBAdapter,
    looks_unocrd,
    parse_document_page,
    parse_oss_register,
    parse_sitemap,
)
from raglex.core.errors import RateLimitException
from raglex.core.models import DocType

SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
 <url><loc>https://www.edpb.europa.eu/documents/guideline/guidelines-blockchain_en</loc>
      <lastmod>2026-07-08T13:47:37+02:00</lastmod></url>
 <url><loc>https://www.edpb.europa.eu/documents/guideline/guidelines-blockchain_fr</loc>
      <lastmod>2026-07-08T13:47:37+02:00</lastmod></url>
 <url><loc>https://www.edpb.europa.eu/documents/edpb-binding-decisions/binding-decision-012021_en</loc>
      <lastmod>2026-01-02T00:00:00+02:00</lastmod></url>
 <url><loc>https://www.edpb.europa.eu/news/some-news_en</loc>
      <lastmod>2026-07-01T00:00:00+02:00</lastmod></url>
</urlset>
"""

DOC_PAGE = """
<html><head>
<meta property="og:description" content="Guidelines on blockchain and the GDPR." />
</head><body>
<h1 class="document-full__title">Guidelines on processing of personal data through blockchain technologies</h1>
<ul><li class="document-full__meta-item"><div class="document-full__meta">Guideline</div></li>
<li class="document-full__meta-item"><div class="document-full__date"><time datetime="2026-07-07T12:00:00Z">07 July 2026</time></div></li>
<li class="document-full__meta-item"><div class="document-full__version">Final version</div></li></ul>
<div class="document-full__public-consultation"><span>See the
  <a class="document-full__public-consultation-link" href="/public-consultations/guidelines-022025-blockchain_en">first version</a>
  drafted before public consultation.</span></div>
<div class="document-full__files">
 <div class="file__title"><a href="/system/files/2026-07/edpb_guidelines_202502_blockchain_v2_en.pdf">Guidelines 202502 blockchain v2</a></div>
 <a href="/system/files/2026-07/edpb_guidelines_202502_blockchain_v2_en.pdf" class="file__link">Download Guidelines 202502 blockchain v2</a>
 <div class="file__title"><a href="/system/files/2026-07/report_consultation.pdf">Report Public Consultation</a></div>
</div>
<div class="document-full__relevant-topics">
 <a class="document-full__relevant-topics-list-item-link" href="#">#Technology</a>
 <a class="document-full__relevant-topics-list-item-link" href="#">#Basic principles</a>
</div>
</body></html>
"""

OSS_PAGE = """
<html><body>
<a href="?page=1">2</a><a href="?page=119">last</a>
<div class="foss-decision-teaser">
 <h3 class="foss-decision-teaser__title"><div>EDPBI:LU:OSS:D:2026:3920</div></h3>
 <div class="foss-decision-foss-decision-teaser__date-of-decision"><time datetime="2026-02-10T12:00:00Z">10 February 2026</time></div>
 <div class="foss-decision-foss-decision-teaser__lead-sa"><div class="member-country-token member-country-token__icon-flag-lu"></div></div>
 <div class="file__title"><a href="/system/files/2026-06/decision-no-3920.pdf">decision-no-3920.pdf</a></div>
 <div class="foss-decision-teaser__concerned-sa-list-item"><a class="member-state-token member-state-token__icon-flag-de"></a></div>
 <div class="foss-decision-teaser__concerned-sa-list-item"><a class="member-state-token member-state-token__icon-flag-fr"></a></div>
 <span class="foss-decision-teaser__main-legel-ref-value">Article 17 (1) (a)</span>
 <a class="foss-decision-teaser__relevant-topics-list-item-link" href="#">#Right to erasure</a>
</div>
<div class="foss-decision-teaser">
 <h3><div>EDPBI:FR:OSS:D:2025:3826</div></h3>
 <time datetime="2025-11-03T12:00:00Z">3 November 2025</time>
 <div class="foss-decision-foss-decision-teaser__lead-sa"><div class="member-country-token__icon-flag-fr"></div></div>
 <div class="file__title"><a href="/system/files/2025-12/decision-no-3826.pdf">decision-no-3826.pdf</a></div>
</div>
</body></html>
"""


def test_parse_sitemap_keeps_english_document_pages_only():
    entries = parse_sitemap(SITEMAP)
    assert [(e.section, e.slug) for e in entries] == [
        ("guideline", "guidelines-blockchain"),
        ("edpb-binding-decisions", "binding-decision-012021"),
    ]
    assert entries[0].lastmod == "2026-07-08T13:47:37+02:00"


def test_parse_document_page_captures_all_header_metadata():
    meta = parse_document_page(DOC_PAGE)
    assert meta["title"].startswith("Guidelines on processing of personal data")
    assert meta["type_label"] == "Guideline"
    assert str(meta["date"]) == "2026-07-07"
    assert meta["version_status"] == "Final version"
    assert meta["consultation_url"] == "/public-consultations/guidelines-022025-blockchain_en"
    assert [f["href"] for f in meta["files"]] == [
        "/system/files/2026-07/edpb_guidelines_202502_blockchain_v2_en.pdf",
        "/system/files/2026-07/report_consultation.pdf",
    ]
    # the duplicate download link is folded and the "Download " prefix stripped
    assert meta["files"][0]["label"] == "Guidelines 202502 blockchain v2"
    assert meta["topics"] == ["Technology", "Basic principles"]
    assert "blockchain" in meta["description"]


def test_parse_oss_register_decisions_and_pager():
    decisions, last_page = parse_oss_register(OSS_PAGE)
    assert last_page == 119
    d = decisions[0]
    assert d.edpbi == "EDPBI:LU:OSS:D:2026:3920"
    assert (d.country, d.year, d.serial) == ("lu", 2026, 3920)
    assert str(d.decided) == "2026-02-10"
    assert d.pdf_url == "/system/files/2026-06/decision-no-3920.pdf"
    assert d.concerned == ("de", "fr")
    assert d.legal_refs == ("Article 17 (1) (a)",)
    assert d.topics == ("Right to erasure",)
    assert decisions[1].edpbi == "EDPBI:FR:OSS:D:2025:3826"


class _Resp:
    def __init__(self, content, status=200, ctype="text/html"):
        self.content = content
        self.status_code = status
        self.headers = {"content-type": ctype}


class _FakeClient:
    """Maps url-substring → response; records calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return _Resp(b"", 404)


def test_document_discovery_filters_cursor_and_yields_oldest_first():
    ad = EDPBAdapter(client=_FakeClient({"sitemap.xml?page=1": _Resp(SITEMAP)}))
    stubs = list(ad.discover(None))
    # oldest lastmod first — the drip cursor advances only over what was processed
    assert [s.stable_id for s in stubs] == [
        "edpb/binding-decision-012021", "edpb/guidelines-blockchain"]
    assert stubs[1].hints["watermark"] == "2026-07-08T13:47:37+02:00"
    assert stubs[1].hints["contenthash"] == "2026-07-08T13:47:37+02:00"
    assert stubs[1].hints["section"] == "guideline"
    # cursor: only entries with a newer lastmod come back
    stubs = list(ad.discover("2026-01-02T00:00:00+02:00"))
    assert [s.stable_id for s in stubs] == ["edpb/guidelines-blockchain"]
    # section filter
    ad2 = EDPBAdapter(sections="edpb-binding-decisions",
                      client=_FakeClient({"sitemap.xml?page=1": _Resp(SITEMAP)}))
    assert [s.stable_id for s in ad2.discover(None)] == ["edpb/binding-decision-012021"]


def test_register_discovery_stops_at_serial_cursor():
    client = _FakeClient({"register-of-final-one-stop-shop-decisions_en": _Resp(OSS_PAGE.encode())})
    ad = EDPBAdapter(register=True, client=client)
    assert ad.source == "edpb-oss"
    # cursor at 3826 → only the newer 3920 comes back — exactly once, even though the
    # fake serves the same listing for every page (the walk tolerates the register's
    # rough ordering by reading a few pages past the cursor, deduping by serial)
    stubs = list(ad.discover(f"{3826:08d}"))
    assert [s.stable_id for s in stubs] == ["edpb/oss/2026/3920"]
    assert stubs[0].hints["watermark"] == f"{3920:08d}"
    assert stubs[0].court == "dpa-lu" and stubs[0].title == "EDPBI:LU:OSS:D:2026:3920"
    assert len(client.calls) <= 5  # stops after a few stale pages, not all 120


def test_waf_block_raises_rate_limit_not_item_failure():
    ad = EDPBAdapter(client=_FakeClient({"sitemap.xml?page=1": _Resp(b"", 403)}))
    with pytest.raises(RateLimitException):
        list(ad.discover(None))
    # an HTML body where a PDF was requested is the WAF challenge page
    ad2 = EDPBAdapter(client=_FakeClient({"x.pdf": _Resp(b"<html>blocked</html>")}))
    with pytest.raises(RateLimitException):
        ad2._get("https://www.edpb.europa.eu/system/files/x.pdf", expect_pdf=True)


def _tiny_pdf(text: str) -> bytes:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    # one line per insert — a single long line runs off-page and extracts as almost
    # nothing, which would trip the OCR detector on a perfectly good PDF
    words = text.split()
    for i in range(0, len(words), 8):
        page.insert_text((72, 72 + 14 * (i // 8)), " ".join(words[i:i + 8]))
    return doc.tobytes()


def test_fetch_oss_builds_decision_with_gdpr_links_and_dpa_split():
    pdf = _tiny_pdf("Decision of the CNPD concerning the right to erasure. " * 20)
    ad = EDPBAdapter(register=True, client=_FakeClient({
        "register-of-final": _Resp(OSS_PAGE.encode()),
        "decision-no-3920.pdf": _Resp(pdf, ctype="application/pdf"),
    }))
    stub = next(iter(ad.discover(None)))
    rec = ad.fetch(stub)
    assert rec.doc_type == DocType.DECISION and rec.court == "dpa-lu"
    assert rec.title.startswith("EDPBI:LU:OSS:D:2026:3920")
    assert "Article 17" in rec.title
    # the register's main legal reference → an interprets edge to the GDPR article
    assert [(r.dst_id, r.dst_anchor) for r in rec.relations] == [
        ("32016R0679", "Article 17 (1) (a)")]
    assert rec.extra["edpbi"] == "EDPBI:LU:OSS:D:2026:3920"
    assert rec.extra["lead_sa"] == "lu" and rec.extra["concerned_sas"] == ["de", "fr"]
    assert rec.extra["aliases"] == ["edpbi:lu:oss:d:2026:3920"]
    assert "erasure" in (rec.text or "")
    assert "needs_ocr" not in rec.extra  # the PDF had a text layer


def test_fetch_document_page_plus_pdf():
    pdf = _tiny_pdf("Guidelines 02/2025 on blockchain. Version 2.0. Adopted on 7 July 2026. " * 10)
    ad = EDPBAdapter(client=_FakeClient({
        "sitemap.xml?page=1": _Resp(SITEMAP),
        "guidelines-blockchain_en": _Resp(DOC_PAGE.encode()),
        "blockchain_v2_en.pdf": _Resp(pdf, ctype="application/pdf"),
    }))
    stubs = list(ad.discover(None))
    rec = ad.fetch(next(s for s in stubs if s.stable_id == "edpb/guidelines-blockchain"))
    assert rec.doc_type == DocType.GUIDANCE
    assert rec.title.startswith("Guidelines on processing")
    assert str(rec.decision_date) == "2026-07-07"
    assert rec.raw_ext == "pdf" and "blockchain" in (rec.text or "")
    assert rec.extra["edpb_type"] == "guideline"
    assert rec.extra["version_status"] == "Final version"
    assert rec.extra["topics"] == ["Technology", "Basic principles"]
    assert rec.extra["other_files"] == [
        {"href": "/system/files/2026-07/report_consultation.pdf", "label": "Report Public Consultation"}]
    assert "edpb" in rec.topic_tags and "technology" in rec.topic_tags


def test_looks_unocrd_thresholds():
    assert looks_unocrd("", 5) is True
    assert looks_unocrd("stamped cover text only", 30) is True
    assert looks_unocrd("plenty of extracted text " * 100, 5) is False
    assert looks_unocrd("anything", 0) is False  # unreadable page count → don't guess


def test_registry_wires_edpb_sources():
    from raglex.adapters.registry import IN_SCOPE_SOURCES, get_adapter, source_catalog

    assert get_adapter("edpb").source == "edpb"
    assert get_adapter("edpb-oss").source == "edpb-oss"
    assert {"edpb", "edpb-oss"} <= IN_SCOPE_SOURCES
    cat = {s["key"]: s for s in source_catalog()}
    assert cat["edpb"]["can_incremental"] is True
    assert cat["edpb-oss"]["kind"] == "guidance"


def test_guidance_class_recognises_binding_decision_series():
    from raglex.citations.guidance_class import classify_guidance

    out = classify_guidance(title="Binding Decision 01/2021 on the dispute arisen…",
                            url="https://www.edpb.europa.eu/documents/x_en")
    assert out["number"]["value"] == "Binding Decision 01/2021"
    assert "binding decision 01/2021" in out["aliases"]


def test_parse_sitemap_includes_csc_subsite():
    sm = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
 <url><loc>https://www.edpb.europa.eu/csc/documents/imi-reports/recommendations-on-imi-transparency-obligations_en</loc>
      <lastmod>2025-03-01T00:00:00+02:00</lastmod></url>
</urlset>"""
    entries = parse_sitemap(sm)
    assert entries[0].section == "csc-imi-reports" and entries[0].csc is True
    ad = EDPBAdapter(client=_FakeClient({"sitemap.xml?page=1": _Resp(sm)}))
    assert [s.stable_id for s in ad.discover(None)] == [
        "edpb/csc/recommendations-on-imi-transparency-obligations"]
