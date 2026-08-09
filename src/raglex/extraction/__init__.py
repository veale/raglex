"""Extraction (§5c): bytes → text, pluggable behind one interface."""

from .extractors import (
    DEFAULT_PROVIDERS,
    Extracted,
    ExtractionProvider,
    HtmlExtractor,
    PdfExtractor,
    PlainTextExtractor,
    extract_bytes,
    text_extension,
)
from .ocr import looks_unocrd, ocr_available, ocr_pdf, text_or_ocr

__all__ = [
    "DEFAULT_PROVIDERS",
    "Extracted",
    "ExtractionProvider",
    "HtmlExtractor",
    "PdfExtractor",
    "PlainTextExtractor",
    "extract_bytes",
    "looks_unocrd",
    "ocr_available",
    "ocr_pdf",
    "text_extension",
    "text_or_ocr",
]
