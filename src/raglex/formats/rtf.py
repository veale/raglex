"""RTF format parser for BAILII judgment files (pure, no extra dependencies).

BAILII serves its older case-law as Rich Text Format (.rtf), typically Microsoft
Word RTF exported by the editorial team. The files are simple: paragraph runs,
numbered paragraphs, and plain ASCII/Latin-1 text — no embedded images, no complex
tables. A regex-based strip is reliable for this corpus; a full RTF parser would
add a heavy dependency for marginal gain.

Paragraph numbering (``[1]``, ``[2]``, …) is preserved as structural segments so
the reader can deep-link to a numbered paragraph — the same citable unit as the
TNA Akoma Ntoso judgments.
"""

from __future__ import annotations

import re

from ..core.models import Segment
from ..core.segmentation import SEP
from .base import ParsedDoc, register

# RTF control words that map to whitespace / line structure
_PAR_RE = re.compile(r"\\pard?\b|\\sect\b")
_LINE_RE = re.compile(r"\\line\b")
_TAB_RE = re.compile(r"\\tab\b")
# Hex-escaped Latin-1: \'XX  →  the character at that code point
_HEX_RE = re.compile(r"\\'([0-9a-fA-F]{2})")
# Unicode escapes: \uNNNN followed by a low-ANSI fallback for readers that can't
# render the code point. The fallback is one unit — a literal "?", or a \'XX hex
# escape (e.g. \'3f), or a plain char. We must consume the \'XX form here, before
# _HEX_RE decodes it, or it survives and duplicates the character.
_UNI_RE = re.compile(r"\\u(-?\d+)(?:\\'[0-9a-fA-F]{2}|\?)?")
# All other control words. An RTF control word is letters only; its numeric
# parameter (if any) immediately follows the letters — "\fs24", "\li-360" — and a
# single trailing space is the delimiter RTF consumes. Crucially, a space *before*
# the digits means those digits are document text, not a parameter ("{\b 1985}" is
# the text "1985" in bold), so the digit group must be glued to the letters with no
# optional whitespace between them — otherwise years / paragraph numbers / amounts
# get silently eaten.
_CTRL_RE = re.compile(r"\\[a-zA-Z]+(?:-?\d+)? ?")
# Stray backslash-symbol sequences
_CTRL_SYM_RE = re.compile(r"\\[^a-zA-Z\s]")
# Paragraph number: "[1]", "[12]" etc. at the start of a line (BAILII convention)
_PARA_NUM_RE = re.compile(r"^\[(\d+)\]", re.MULTILINE)
# Multiple blank lines → single blank line
_BLANK_RE = re.compile(r"\n{3,}")


def _uni_char(m: re.Match) -> str:
    n = int(m.group(1))
    if n < 0:
        n += 65536
    try:
        return chr(n)
    except (ValueError, OverflowError):
        return ""


def strip_rtf(data: bytes) -> str:
    """Strip RTF control sequences and return clean plain text (pure).

    Order matters: paragraph markers → newlines before removing other controls,
    so structural whitespace is preserved."""
    # RTF files use Windows-1252 (a superset of Latin-1) for non-ASCII bytes.
    text = data.decode("cp1252", errors="replace")

    # Replace paragraph/section breaks with double newlines
    text = _PAR_RE.sub("\n\n", text)
    text = _LINE_RE.sub("\n", text)
    text = _TAB_RE.sub("  ", text)

    # Decode character escapes before stripping control words so \'e9 → é.
    # Unicode escapes first: they may carry a \'XX fallback that must be swallowed
    # as part of the escape rather than decoded as a standalone character.
    text = _UNI_RE.sub(_uni_char, text)
    text = _HEX_RE.sub(lambda m: chr(int(m.group(1), 16)), text)

    # Remove all remaining RTF control words and group markers
    text = _CTRL_RE.sub("", text)
    text = _CTRL_SYM_RE.sub("", text)
    text = text.replace("{", "").replace("}", "")

    # Normalise whitespace
    text = re.sub(r" {2,}", " ", text)
    text = _BLANK_RE.sub("\n\n", text)
    return text.strip()


def parse_rtf(data: bytes) -> ParsedDoc:
    """Parse a BAILII RTF judgment into text + numbered-paragraph segments (pure)."""
    text = strip_rtf(data)
    if not text:
        return ParsedDoc()

    # Segment on BAILII paragraph numbers: lines starting with "[N]"
    segments: list[Segment] = []
    last_num: int | None = None
    last_start: int = 0
    cursor = 0

    parts: list[str] = []
    for line in text.split("\n"):
        m = _PARA_NUM_RE.match(line.strip())
        if m:
            num = int(m.group(1))
            # flush the previous paragraph's accumulated text
            if last_num is not None and parts:
                para_text = "\n".join(parts).strip()
                if para_text:
                    segments.append(Segment(
                        label=f"[{last_num}]", char_start=last_start,
                        char_end=last_start + len(para_text),
                        kind="paragraph", level=0,
                    ))
            last_num = num
            last_start = cursor
            parts = [line]
        else:
            parts.append(line)
        cursor += len(line) + 1  # +1 for the \n

    # flush the last paragraph
    if last_num is not None and parts:
        para_text = "\n".join(parts).strip()
        if para_text:
            segments.append(Segment(
                label=f"[{last_num}]", char_start=last_start,
                char_end=last_start + len(para_text),
                kind="paragraph", level=0,
            ))

    return ParsedDoc(text=text, segments=segments)


register("rtf", parse_rtf)
