"""The Council of Europe Treaty Office's official-text PDF.

Registering the name matters more here than for most formats. A treaty's raw is a plain
PDF, so byte sniffing answers "pdf" and the generic extractor runs — which reads the text
but knows nothing of articles, numbered paragraphs, or the running header the Treaty
Office prints on every page. A whole-source reparse would therefore either flatten every
treaty to one block or (because ``_would_flatten`` refuses to do that) skip all of them,
and a parser fix would never reach the 200-odd treaties already held.

Re-harvesting is not the alternative: the PDF is byte-identical, so the pipeline's
content-hash dedup short-circuits before the parser is ever called. Reparse from
immutable raw is the only route a Treaty Office parser fix can travel.
"""

from __future__ import annotations

from .base import ParsedDoc, register

#: What the format name is stored as, and what ``reparse_source`` falls back to for the
#: treaties harvested before the adapter began recording it.
NAME = "coe-treaty-pdf"


def parse_coe_treaty_pdf(raw: bytes) -> ParsedDoc:
    # Lazy import: the format registry must not pull in the Treaty Office's network
    # adapter (and its browser tier) merely by being imported.
    from ..adapters.council_of_europe import strip_page_furniture, treaty_segments
    from ..extraction.ocr import text_or_ocr

    text, _needs_ocr, spans, _engine = text_or_ocr(raw, max_pages=220)
    text, spans = strip_page_furniture(text, spans)
    segments = treaty_segments(text)
    if not segments:
        from ..core.models import Segment
        segments = [Segment(label=f"p. {n}", char_start=start, char_end=end, kind="page")
                    for n, start, end in spans]
    return ParsedDoc(text=text or None, segments=segments)


register(NAME, parse_coe_treaty_pdf)
