"""OCR escalation (§5c): the tier that runs when the born-digital parser found no text.

A PDF with no text layer is not an empty document, it is a photograph of one. The
born-digital extractors report that honestly as ``needs_ocr`` rather than yielding a
silently-empty record; this module is what turns that flag into text.

It lived in ``adapters.edpb`` because a DPA register was the first place a scan turned
up, and three unrelated adapters were importing OCR from a Belgian data-protection
module. It is not a property of that source. The ISC's pre-2000 annual reports are the
same problem — ``1995_ISC_AR.pdf`` extracts zero characters and OCRs to real prose — and
so is every Commons Library research paper published before the Library typeset them.

Rasterisation is PyMuPDF and recognition is tesseract, both optional: when any piece of
the stack is missing this returns ``None`` and the caller records ``needs_ocr`` instead
of failing the harvest. That distinction matters, because "we have not OCR'd this yet"
and "this document has no text" must never look the same in the corpus.

**It is slow** — roughly ten seconds a page at 200 dpi — so it is bounded by
``max_pages`` and is only ever reached after the cheap path has already failed. The job
manager knows about this: a worker inside OCR is quiet, not wedged (see
`job-authoring.md` §10 and ``jobs.STALL_SECONDS``).
"""

from __future__ import annotations

import logging

log = logging.getLogger("raglex.extraction.ocr")

#: Enough pages for a full committee report; beyond this the cost stops being worth a
#: single harvest slot and the remainder is better handled by a targeted re-extraction.
DEFAULT_MAX_PAGES = 80


def looks_unocrd(text: str | None, page_count: int) -> bool:
    """Whether a PDF's extracted text is too thin for its length to be real text.

    The threshold is deliberately low — a stamped cover page can carry a few dozen
    characters of metadata text while the body of the document is still all image."""
    if page_count <= 0:
        return False
    return len((text or "").strip()) < max(120, 40 * page_count)


def ocr_available() -> bool:
    """Whether this image can actually OCR, so a caller can say so before trying."""
    try:
        import fitz  # noqa: F401  — PyMuPDF, the rasteriser
        import pytesseract
        from PIL import Image  # noqa: F401

        pytesseract.get_tesseract_version()
    except Exception:  # noqa: BLE001 — any missing piece means no OCR here
        return False
    return True


def ocr_pdf(data: bytes, *, dpi: int = 200, max_pages: int = DEFAULT_MAX_PAGES,
            language: str = "eng") -> str | None:
    """Tesseract the PDF's pages (rasterised via PyMuPDF), or ``None``.

    ``None`` means "could not OCR" — a missing stack, a corrupt scan, or nothing
    recognised — and is never the same as an empty string from a real pass."""
    try:
        import io

        import fitz  # PyMuPDF
        import pytesseract
        from PIL import Image

        pytesseract.get_tesseract_version()
    except Exception:  # noqa: BLE001 — any missing piece → no OCR available
        return None
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        for page in doc[:max_pages]:
            pix = page.get_pixmap(dpi=dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            pages.append(pytesseract.image_to_string(img, lang=language))
        return "\n\n".join(pages).strip() or None
    except Exception:  # noqa: BLE001 — a corrupt scan must not kill the batch
        log.warning("ocr_pdf failed on a %d-byte PDF", len(data or b""), exc_info=True)
        return None


def text_or_ocr(data: bytes, *, min_chars: int = 200,
                max_pages: int = DEFAULT_MAX_PAGES) -> tuple[str, bool, list, str]:
    """The text of a PDF, OCR'ing it when the born-digital parse came back empty.

    Returns ``(text, needs_ocr, page_spans, engine)``. ``needs_ocr`` stays True only
    when the scan could not be read at all — that is the review flag, not a description
    of the source. ``page_spans`` come from the born-digital parse and are therefore
    empty for an OCR'd document, whose page boundaries are not offsets into this text.
    """
    from . import extract_bytes

    extracted = extract_bytes(data, ext="pdf", mime="application/pdf")
    text = (extracted.text or "").strip()
    spans = list(extracted.page_spans or [])
    if len(text) >= min_chars and not extracted.needs_ocr:
        return text, False, spans, extracted.engine or "pdf"
    if extracted.needs_ocr or looks_unocrd(text, len(spans)) or len(text) < min_chars:
        ocred = (ocr_pdf(data, max_pages=max_pages) or "").strip()
        if len(ocred) > len(text):
            # The page spans described the born-digital parse, which we just replaced.
            return ocred, False, [], "tesseract"
    return text, bool(extracted.needs_ocr) and not text, spans, extracted.engine or "pdf"
