"""Official ECHR Convention PDF → article/sub-provision segments.

The Court publishes the current English treaty text as a born-digital PDF.  Its text
layer is unusually regular: article headings contain an en dash, while paragraph and
letter markers occupy their own lines.  Parsing that structure gives the reader real
``Article 5(1)(a)`` anchors and avoids the editorial boilerplate that came with the old
Wikisource import.
"""

from __future__ import annotations

import io
import re

from ..core.segmentation import assemble

OFFICIAL_ECHR_CONVENTION_URL = "https://rm.coe.int/1680a2353d"

_ARTICLE = re.compile(r"^Article\s+(\d+)\s*[–—-]\s*(.+)$", re.IGNORECASE)
_SECTION = re.compile(r"^Section\s+[IVXLC]+\s*[–—-]", re.IGNORECASE)
_FOOTNOTE = re.compile(
    r"\n\s*\d+\s*\n\s*(?:Text|Article heading|Paragraph|Sentence|Words)\s+"
    r"(?:amended|inserted|deleted|replaced).*",
    re.IGNORECASE | re.DOTALL,
)


def _pdf_pages(data: bytes) -> list[str]:
    try:
        import fitz

        with fitz.open(stream=data, filetype="pdf") as doc:
            return [page.get_text("text") for page in doc]
    except ImportError:  # pragma: no cover - the normal import image includes PyMuPDF
        from pypdf import PdfReader

        return [(page.extract_text() or "") for page in PdfReader(io.BytesIO(data)).pages]


def parse_echr_convention_pages(pages: list[str]) -> tuple[str, list]:
    """Parse already-extracted official PDF pages (split out for deterministic tests)."""
    lines: list[str] = []
    for page_no, raw_page in enumerate(pages, 1):
        page = _FOOTNOTE.sub("", raw_page)
        page = re.sub(r"\n\s*_{8,}\s*\n", "\n", page)
        page = re.sub(r"\n\s*_{3,}\s*\(\*\).*", "", page, flags=re.DOTALL)
        page_lines = [re.sub(r"\s+", " ", line).strip() for line in page.splitlines()]
        dropped_page_number = False
        for line in page_lines:
            if not line or line.startswith("ETS 5 – Human Rights"):
                continue
            if not dropped_page_number and line == str(page_no):
                dropped_page_number = True
                continue
            lines.append(line)

    # The cover/title apparatus is not treaty text.  Start on the authentic preamble.
    try:
        start = next(i for i, line in enumerate(lines)
                     if line.lower().startswith("the governments signatory"))
    except StopIteration as exc:
        raise ValueError("official ECHR PDF has no recognisable preamble") from exc
    lines = lines[start:]

    blocks: list[tuple[str, str, str, int]] = []
    label = "Preamble"
    kind = "section"
    level = 0
    buf: list[str] = []
    article: str | None = None
    paragraph: str | None = None

    def flush() -> None:
        nonlocal buf
        body = re.sub(r"\s+", " ", " ".join(buf)).strip()
        if body:
            blocks.append((label, kind, body, level))
        buf = []

    for line in lines:
        heading = _ARTICLE.match(line)
        if heading:
            flush()
            article = str(int(heading.group(1)))
            paragraph = None
            label = f"Article {article}"
            kind, level = "article", 0
            buf = [f"Article {article} — {heading.group(2).strip()}"]
            continue
        if _SECTION.match(line):
            continue
        if article and re.fullmatch(r"\d{1,2}", line):
            flush()
            paragraph = str(int(line))
            label = f"Article {article}({paragraph})"
            kind, level = "paragraph", 1
            continue
        if article and paragraph and re.fullmatch(r"[a-z]", line):
            flush()
            label = f"Article {article}({paragraph})({line.lower()})"
            kind, level = "point", 2
            continue
        buf.append(line)
    flush()

    article_numbers = {
        int(match.group(1))
        for block in blocks
        if (match := re.match(r"Article\s+(\d+)", block[0]))
    }
    if not set(range(1, 19)).issubset(article_numbers) or max(article_numbers, default=0) < 59:
        raise ValueError(
            f"official ECHR parse looks incomplete ({len(article_numbers)} articles)"
        )
    return assemble(blocks)


def parse_echr_convention_pdf(data: bytes) -> tuple[str, list]:
    return parse_echr_convention_pages(_pdf_pages(data))
