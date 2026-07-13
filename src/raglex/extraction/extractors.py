"""Extraction providers (§5c) — bytes → text, pluggable behind one interface.

§5c makes the extractor pluggable because "extraction quality and the tool
landscape move fast" and different document classes want different engines. This
ships the lightweight defaults needed for manual import (PDF via pypdf, HTML via a
tag stripper, plain text); the heavier SOTA engines (Docling, Marker, PaddleOCR,
VLM-OCR) drop in behind the same ``ExtractionProvider`` interface without touching
callers.

The decisive split (§5c) is **born-digital vs scanned**: a fast parser extracts a
born-digital PDF's text near-perfectly, but returns *nothing, silently* for a
scanned/image-only PDF — the classic production failure. So a born-digital parser
returning empty text sets ``needs_ocr`` rather than yielding a silently-empty
document; an OCR escalation tier (§5c) fills it in a later pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class Extracted:
    text: str
    engine: str
    engine_version: str
    needs_ocr: bool = False  # born-digital parser found no text → likely scanned
    per_page_confidence: list[float] | None = None
    # (page_number, char_start, char_end) per page in `text` — lets an importer
    # make pages addressable so a "pp. 45-47" fragment link is meaningful (§1.9).
    page_spans: list[tuple[int, int, int]] | None = None


@runtime_checkable
class ExtractionProvider(Protocol):
    name: str

    def handles(self, ext: str, mime: str | None) -> bool: ...

    def extract(self, data: bytes, *, ext: str, mime: str | None = None) -> Extracted: ...


class StructuredPdfExtractor:
    """Layout-aware PDF text via PyMuPDF (§5c) — the preferred engine when installed
    (``pip install 'raglex[import]'``). Unlike pypdf's flat stream it walks each
    page's text *blocks* in reading order, so paragraphs survive as paragraphs —
    which is what lets numbered-paragraph guidance (EDPB/A29WP/Ofcom) gain citable
    ``para N`` segments downstream. Optional: ``handles`` says no when PyMuPDF is
    missing and the router falls through to pypdf."""

    name = "pymupdf"

    def handles(self, ext: str, mime: str | None) -> bool:
        if not (ext.lower().lstrip(".") == "pdf" or (mime or "") == "application/pdf"):
            return False
        try:
            import fitz  # noqa: F401 — PyMuPDF
        except ImportError:
            return False
        return True

    def extract(self, data: bytes, *, ext: str, mime: str | None = None) -> Extracted:
        import fitz

        doc = fitz.open(stream=data, filetype="pdf")
        confidences: list[float] = []
        page_spans: list[tuple[int, int, int]] = []
        parts: list[str] = []
        cursor = 0
        sep = "\n\n"
        for i, page in enumerate(doc):
            blocks = [b for b in page.get_text("blocks") if b[6] == 0 and (b[4] or "").strip()]
            blocks.sort(key=lambda b: (round(b[1]), b[0]))  # reading order: top-down, then left
            # inside a block, hard newlines are line-wrapping; between blocks, paragraphs
            page_text = sep.join(
                re.sub(r"\s*\n\s*", " ", b[4].strip()) for b in blocks)
            confidences.append(1.0 if page_text else 0.0)
            if not page_text:
                continue
            if parts:
                cursor += len(sep)
            page_spans.append((i + 1, cursor, cursor + len(page_text)))
            parts.append(page_text)
            cursor += len(page_text)
        text = sep.join(parts)
        return Extracted(
            text=text,
            engine=self.name,
            engine_version=getattr(fitz, "__doc__", "") .split()[1] if fitz.__doc__ else "?",
            needs_ocr=not text.strip(),  # no text layer → scanned (§5c silent-empty)
            per_page_confidence=confidences,
            page_spans=page_spans,
        )


class PdfExtractor:
    """Born-digital PDF text via pypdf (the fast path, §5c). Empty output flags
    ``needs_ocr`` so a scanned PDF routes to OCR rather than silently vanishing."""

    name = "pypdf"

    def handles(self, ext: str, mime: str | None) -> bool:
        return ext.lower().lstrip(".") == "pdf" or (mime or "") == "application/pdf"

    def extract(self, data: bytes, *, ext: str, mime: str | None = None) -> Extracted:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        confidences: list[float] = []
        page_spans: list[tuple[int, int]] = []
        parts: list[str] = []
        cursor = 0
        sep = "\n\n"
        for i, page in enumerate(reader.pages):
            txt = (page.extract_text() or "").strip()
            confidences.append(1.0 if txt else 0.0)
            if not txt:
                continue
            if parts:
                cursor += len(sep)
            page_spans.append((i + 1, cursor, cursor + len(txt)))  # 1-based page number
            parts.append(txt)
            cursor += len(txt)
        text = sep.join(parts)
        version = getattr(__import__("pypdf"), "__version__", "?")
        return Extracted(
            text=text,
            engine=self.name,
            engine_version=version,
            needs_ocr=not text.strip(),  # no text layer → scanned (§5c silent-empty)
            per_page_confidence=confidences,
            page_spans=page_spans,
        )


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]+")


class HtmlExtractor:
    """Plain text from HTML by stripping tags (and script/style). Good enough for
    saved articles/commentary; a structure-aware reader (Docling) plugs in later."""

    name = "html-strip"

    def handles(self, ext: str, mime: str | None) -> bool:
        return ext.lower().lstrip(".") in {"html", "htm"} or (mime or "").startswith("text/html")

    def extract(self, data: bytes, *, ext: str, mime: str | None = None) -> Extracted:
        html = data.decode("utf-8", errors="replace")
        html = _SCRIPT_STYLE_RE.sub(" ", html)
        text = _TAG_RE.sub("\n", html)
        text = _WS_RE.sub(" ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
        return Extracted(text=text, engine=self.name, engine_version="1")


class PlainTextExtractor:
    name = "plain"

    def handles(self, ext: str, mime: str | None) -> bool:
        return ext.lower().lstrip(".") in {"txt", "md", "markdown", "text"} or (
            mime or ""
        ).startswith("text/plain")

    def extract(self, data: bytes, *, ext: str, mime: str | None = None) -> Extracted:
        return Extracted(text=data.decode("utf-8", errors="replace"), engine=self.name, engine_version="1")


class RtfExtractor:
    """RTF → plain text (§5c). Without this, an uploaded ``.rtf`` fell through to the
    plain-text provider, which stored the raw ``{\\rtf1 …}`` control-word markup *as the
    document text* — the "why is my upload garbage" failure. striprtf strips the control
    words to readable text."""

    name = "striprtf"

    def handles(self, ext: str, mime: str | None) -> bool:
        return ext.lower().lstrip(".") == "rtf" or (mime or "") in ("application/rtf", "text/rtf")

    def extract(self, data: bytes, *, ext: str, mime: str | None = None) -> Extracted:
        try:
            from striprtf.striprtf import rtf_to_text
        except ImportError as exc:  # pragma: no cover — optional dep
            raise RuntimeError("RTF import needs striprtf: pip install 'raglex[import]'") from exc
        text = rtf_to_text(data.decode("utf-8", errors="replace")).strip()
        return Extracted(text=text, engine=self.name, engine_version="1")


# Router (§5c): try providers in order — the structured PDF engine outranks pypdf
# when PyMuPDF is installed (its handles() declines otherwise, so the fallback holds).
DEFAULT_PROVIDERS: tuple[ExtractionProvider, ...] = (
    StructuredPdfExtractor(),
    PdfExtractor(),
    RtfExtractor(),
    HtmlExtractor(),
    PlainTextExtractor(),
)


def extract_bytes(
    data: bytes,
    *,
    ext: str,
    mime: str | None = None,
    providers: tuple[ExtractionProvider, ...] = DEFAULT_PROVIDERS,
) -> Extracted:
    for provider in providers:
        if provider.handles(ext, mime):
            return provider.extract(data, ext=ext, mime=mime)
    # last resort: decode as text so nothing is ever silently dropped
    return PlainTextExtractor().extract(data, ext=ext, mime=mime)
