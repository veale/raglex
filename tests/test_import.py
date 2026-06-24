from __future__ import annotations

import io

from raglex.core.models import DocType, RelationshipType
from raglex.extraction import extract_bytes
from raglex.imports import (
    add_note,
    attach_asset,
    import_file,
    import_url,
    link_documents,
)
from raglex.imports.zotero import ZoteroImporter
from raglex.storage import RawStore, TextStore


# -- extraction (§5c) -------------------------------------------------------
def test_html_extraction_strips_tags_and_scripts():
    html = b"<html><head><style>x{}</style></head><body><h1>Title</h1><p>Body text.</p><script>bad()</script></body></html>"
    ex = extract_bytes(html, ext="html")
    assert "Title" in ex.text and "Body text." in ex.text
    assert "bad()" not in ex.text and "x{}" not in ex.text


def test_plain_text_extraction():
    ex = extract_bytes(b"just words", ext="txt")
    assert ex.text == "just words" and ex.engine == "plain"


def test_scanned_pdf_flags_needs_ocr():
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)  # no text layer → "scanned" (§5c)
    buf = io.BytesIO()
    w.write(buf)
    ex = extract_bytes(buf.getvalue(), ext="pdf")
    assert ex.needs_ocr is True  # silent-empty caught, not a silently-empty doc


# -- import service (§1.9) --------------------------------------------------
def _stores(tmp_path):
    return RawStore(tmp_path / "raw"), TextStore(tmp_path / "text")


def test_import_file_creates_secondary_doc_linked_to_case(catalogue, tmp_path):
    rs, ts = _stores(tmp_path)
    # the primary case it's about, already in the corpus
    from tests.conftest import make_record
    catalogue.upsert_document(make_record(stable_id="ECLI:EU:C:2020:559"))

    html = b"<p>A case note analysing the Schrems II judgment.</p>"
    res = import_file(
        catalogue, rs, ts, data=html, filename="note.html",
        doc_type=DocType.COMMENTARY, link_to="ECLI:EU:C:2020:559",
    )
    assert res.doc_type == "commentary"
    assert res.linked_to == "ECLI:EU:C:2020:559"
    assert res.relationship == "analyses"  # default for commentary

    doc = catalogue.get_document(res.stable_id)
    assert doc["added_by"] == "user"  # kept separable from primary law (§10)
    edge = catalogue.relations_for(res.stable_id)[0]
    assert edge["dst_id"] == "ECLI:EU:C:2020:559"
    assert edge["resolution_status"] == "resolved"  # target present → live edge


def test_import_url_uses_injected_client(catalogue, tmp_path):
    rs, ts = _stores(tmp_path)

    class FakeResp:
        content = b"<p>Downloaded commentary.</p>"
        headers = {"content-type": "text/html"}
        def raise_for_status(self): ...

    class FakeHttp:
        def get(self, url):
            return FakeResp()

    res = import_url(catalogue, rs, ts, url="https://x.test/doc.html", http=FakeHttp())
    assert res.chars > 0
    assert catalogue.get_document(res.stable_id)["source"] == "user-import"


def test_add_note_and_link(catalogue, tmp_path):
    _rs, ts = _stores(tmp_path)
    from tests.conftest import make_record
    catalogue.upsert_document(make_record(stable_id="uksc/2024/1"))

    res = add_note(catalogue, ts, text="My summary of the holding.", link_to="uksc/2024/1")
    assert res.doc_type == "note"
    assert catalogue.relations_for(res.stable_id)[0]["relationship_type"] == "summarises"


def test_link_documents_between_existing(catalogue):
    from tests.conftest import make_record
    catalogue.upsert_document(make_record(stable_id="a"))
    catalogue.upsert_document(make_record(stable_id="b"))
    resolved = link_documents(catalogue, src_id="a", dst_id="b", relationship=RelationshipType.CRITICISES)
    assert resolved is True
    assert any(r["dst_id"] == "b" and r["relationship_type"] == "criticises"
               for r in catalogue.relations_for("a"))


def test_attach_asset(catalogue, tmp_path):
    rs, _ts = _stores(tmp_path)
    from tests.conftest import make_record
    catalogue.upsert_document(make_record(stable_id="a"))
    asset_id = attach_asset(catalogue, rs, doc_id="a", data=b"PDFBYTES", filename="exhibit.pdf", kind="exhibit")
    assets = catalogue.assets_for("a")
    assert len(assets) == 1 and assets[0]["kind"] == "exhibit" and assets[0]["added_by"] == "user"


# -- Zotero (§1.9) ----------------------------------------------------------
def test_zotero_import_maps_items_and_extracts_citations(catalogue, tmp_path):
    rs, ts = _stores(tmp_path)

    class FakeResp:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    class FakeHttp:
        def get(self, url, headers=None, params=None):
            return FakeResp([
                {"data": {"key": "ABCD1234", "itemType": "journalArticle",
                          "title": "The GDPR right of access after Schrems",
                          "abstractNote": "Discusses ECLI:EU:C:2020:559 (CELEX 32016R0679).",
                          "creators": [{"firstName": "A", "lastName": "Author"}],
                          "date": "2023"}},
            ])

    importer = ZoteroImporter(FakeHttp(), "12345", "key", "users")
    ids = importer.import_into(catalogue, rs, ts, limit=10)
    assert ids == ["zotero:ABCD1234"]
    doc = catalogue.get_document("zotero:ABCD1234")
    assert doc["doc_type"] == "article" and doc["added_by"] == "user"
    # ECLI + CELEX in the abstract became dangling commentary edges (§5b)
    cites = {r["raw_citation_string"] for r in catalogue.relations_for("zotero:ABCD1234")}
    assert "ECLI:EU:C:2020:559" in cites and "32016R0679" in cites
