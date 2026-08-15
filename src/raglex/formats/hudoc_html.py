"""HUDOC's DOCX-to-HTML judgment rendition.

The adapter records ``format='hudoc-html'`` so held judgments can pick up parser fixes
from immutable raw.  Registering that name is essential: HTML cannot be safely identified
by byte sniffing, and without a format parser a whole-source reparse quietly skips every
ECHR judgment.
"""

from __future__ import annotations

from .base import ParsedDoc, register


def parse_hudoc_html(raw: bytes) -> ParsedDoc:
    # Lazy import avoids making the format registry initialise the HUDOC network adapter.
    from ..adapters.echr import parse_body_html

    text, segments = parse_body_html(raw)
    return ParsedDoc(text=text, segments=segments)


register("hudoc-html", parse_hudoc_html)
