"""DOCX extractor — dependency-free zip/document.xml text extraction."""

from __future__ import annotations

import io
import zipfile

from raglex.extraction import extract_bytes
from raglex.extraction.extractors import DocxExtractor

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _make_docx(paragraphs) -> bytes:
    body = "".join(
        "<w:p><w:r><w:t>" + p + "</w:t></w:r></w:p>" for p in paragraphs)
    document = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def test_docx_extractor_handles_by_ext_and_mime():
    e = DocxExtractor()
    assert e.handles("docx", None)
    assert e.handles(".DOCX", None)
    assert e.handles("", DOCX_MIME)
    assert not e.handles("pdf", "application/pdf")


def test_docx_text_extracted_paragraph_per_line():
    data = _make_docx(["Judgment of the Court.", "The appeal is dismissed."])
    out = extract_bytes(data, ext="docx", mime=DOCX_MIME)
    assert out.engine == "docx-zip"
    assert out.text == "Judgment of the Court.\nThe appeal is dismissed."
    assert out.needs_ocr is False


def test_docx_entities_unescaped_and_runs_joined():
    # a paragraph split across runs, with an escaped entity
    doc = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           "<w:body><w:p><w:r><w:t>Smith &amp; </w:t></w:r>"
           "<w:r><w:t>Jones</w:t></w:r></w:p></w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
    out = extract_bytes(buf.getvalue(), ext="docx", mime=DOCX_MIME)
    assert out.text == "Smith & Jones"


def test_non_docx_bytes_flag_needs_ocr_not_crash():
    out = extract_bytes(b"not a zip at all", ext="docx", mime=DOCX_MIME)
    assert out.engine == "docx-zip"
    assert out.text == ""
    assert out.needs_ocr is True
