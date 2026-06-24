"""Extraction (§5c): bytes → text, pluggable behind one interface."""

from .extractors import (
    DEFAULT_PROVIDERS,
    Extracted,
    ExtractionProvider,
    HtmlExtractor,
    PdfExtractor,
    PlainTextExtractor,
    extract_bytes,
)

__all__ = [
    "DEFAULT_PROVIDERS",
    "Extracted",
    "ExtractionProvider",
    "HtmlExtractor",
    "PdfExtractor",
    "PlainTextExtractor",
    "extract_bytes",
]
