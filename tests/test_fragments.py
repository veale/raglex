"""Pinpoint / fragment linking (§1.9): a handbook's pages → a law's article."""

from __future__ import annotations

from raglex.core.models import DocType, RelationshipType
from raglex.imports import link_documents
from tests.conftest import make_record


def _doc(catalogue, stable_id, doc_type=DocType.LEGISLATION):
    rec = make_record(stable_id=stable_id, doc_type=doc_type, relations=[])
    catalogue.upsert_document(rec)


def test_fragment_link_records_anchors_both_directions(catalogue):
    _doc(catalogue, "32016R0679")  # the GDPR (target law)
    _doc(catalogue, "user:commentary:abc", doc_type=DocType.COMMENTARY)  # handbook

    resolved = link_documents(
        catalogue, src_id="user:commentary:abc", dst_id="32016R0679",
        relationship=RelationshipType.ANALYSES, src_anchor="pp. 45-47", dst_anchor="Article 17",
    )
    assert resolved is True  # target present → live edge

    # outgoing from the handbook carries the fragment anchors
    edge = catalogue.relations_for("user:commentary:abc")[0]
    assert edge["src_anchor"] == "pp. 45-47" and edge["dst_anchor"] == "Article 17"
    assert edge["relationship_type"] == "analyses"

    # the law sees it as incoming commentary pinned to Article 17
    incoming = catalogue.relations_to("32016R0679")
    assert any(r["dst_anchor"] == "Article 17" and r["src_id"] == "user:commentary:abc"
               for r in incoming)


def test_facade_link_with_anchors(tmp_path, monkeypatch):
    monkeypatch.delenv("RAGLEX_PROXY", raising=False)
    from raglex.config import Config
    from raglex.facade import Facade

    cfg = Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "s.json", topic_threshold=3.0,
        embed_provider="local-hashing", embed_model=None,
    )
    f = Facade(cfg)
    law = f.import_bytes(data=b"<p>Article 17 right to erasure</p>", filename="art.html",
                         doc_type="legislation", title="GDPR Art 17")
    hb = f.import_bytes(data=b"<p>commentary chapter on erasure</p>", filename="hb.html",
                        doc_type="commentary")
    r = f.link(src_id=hb["stable_id"], dst_id=law["stable_id"], relationship="analyses",
               src_anchor="ch. 3, pp. 45-47", dst_anchor="Article 17")
    assert r["src_anchor"] == "ch. 3, pp. 45-47" and r["dst_anchor"] == "Article 17"

    # the law document surfaces the pinned commentary as incoming
    doc = f.get_document(law["stable_id"])
    assert any(i["dst_anchor"] == "Article 17" for i in doc["incoming"])


def test_pdf_page_spans_make_pages_addressable():
    """A born-digital PDF yields per-page spans → page Segments on import (§1.9)."""
    import io

    from pypdf import PdfWriter

    from raglex.extraction import extract_bytes

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    ex = extract_bytes(buf.getvalue(), ext="pdf")
    # blank page has no text → no spans, but the field exists and is well-formed
    assert ex.page_spans == [] and ex.needs_ocr is True
