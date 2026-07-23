"""Paragraph parser for Courts Service of Ireland judgment PDFs.

Unlike the New Zealand parser (:mod:`raglex.formats.nzsc_pdf`), this one is deliberately
lean: the case's **identity metadata** — neutral citation, court, judge, delivery date,
parties — comes from the ``/view/judgments/…`` detail page, which carries an authoritative
labelled block (``Neutral Citation``, ``Record Number``, …). The filename is *not*
trusted (``2025_IESC_31_.pdf`` on the site actually contains "[2026] IESC 31"), so the PDF
is needed only for the judgment **text and its citable structure**, not for keying.

Irish senior-court judgments number their paragraphs "1.", "2.", … — the pinpoint unit an
"at [42]"/"at para 42" citation resolves to. PyMuPDF frequently emits the hanging number
on its own line ("1. \\nMandamus is a discretionary remedy…"), so we coalesce a bare
"N." line into the paragraph it heads before segmenting. Numbering is gated on a
*sequential* run (each accepted number is the previous + 1) so a stray "1." inside body
prose — a sub-list, a "1978" year fragment — never opens a spurious paragraph.

Text extraction goes through the shared extractor (§5c), so a scanned/OCR-only judgment
still yields text (and ``needs_ocr``) and simply carries no paragraph segments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..core.models import Segment

# A hanging paragraph number heading a paragraph: "1.", "42.", tolerant of leading space.
_PARA_NUM_RE = re.compile(r"^\s*(\d{1,3})\.\s")
# The same number alone on its own line (PyMuPDF splits the hanging number from its text).
_LONE_NUM_RE = re.compile(r"^\s*(\d{1,3})\.\s*$")


@dataclass(slots=True)
class ParsedIrishJudgment:
    text: str = ""
    segments: list[Segment] = field(default_factory=list)
    needs_ocr: bool = False


def _coalesce_hanging_numbers(text: str) -> str:
    """Join a paragraph number sitting alone on a line ("1.") onto the text line that
    follows it, so the "N." anchor starts the paragraph line (what the segmenter keys on).
    A trailing lone number with nothing after it is left untouched."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _LONE_NUM_RE.match(lines[i]) and i + 1 < len(lines) and lines[i + 1].strip():
            out.append(f"{lines[i].strip()} {lines[i + 1].strip()}")
            i += 2
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _paragraph_segments(text: str) -> list[Segment]:
    """Segments for the "N." numbered paragraphs, char offsets into ``text``. Only a
    number that continues the sequence (first seen, or previous + 1) opens a paragraph, so
    an in-prose "1." can't start a spurious one. A paragraph runs to just before the next
    accepted number."""
    starts: list[tuple[int, int]] = []  # (line-start char offset, paragraph number)
    expected: int | None = None
    cursor = 0
    for line in text.split("\n"):
        m = _PARA_NUM_RE.match(line)
        if m:
            num = int(m.group(1))
            if expected is None and num == 1:
                starts.append((cursor, num))
                expected = 2
            elif expected is not None and num == expected:
                starts.append((cursor, num))
                expected += 1
        cursor += len(line) + 1  # +1 for the stripped "\n"
    segments: list[Segment] = []
    for i, (start, num) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(text)
        segments.append(Segment(label=f"[{num}]", char_start=start,
                                char_end=max(start, end), kind="paragraph", level=0))
    return segments


def parse_ie_pdf(data: bytes) -> ParsedIrishJudgment:
    """Parse a Courts Service of Ireland judgment PDF → body text + numbered-paragraph
    segments. Text comes from the shared extractor; a document with no recoverable
    paragraph numbering (a scan, or an unnumbered ruling) simply yields no segments."""
    from ..extraction import extract_bytes

    extracted = extract_bytes(data, ext="pdf", mime="application/pdf")
    text = _coalesce_hanging_numbers(extracted.text or "")
    return ParsedIrishJudgment(
        text=text,
        segments=_paragraph_segments(text),
        needs_ocr=extracted.needs_ocr,
    )
