"""Helpers for turning a source's structural units into flat text + offset-correct
``Segment``s (§6b).

The pattern every adapter uses: pull out *labelled blocks* in document order (one
per numbered paragraph / section / zone), then ``assemble`` them into a single
flat text (for FTS/display) plus segments whose char spans index exactly into that
text. Producing both together is what keeps a retrieval hit mappable back to the
citable unit it came from.
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator
from xml.etree import ElementTree as ET

from .models import Segment

SEP = "\n\n"


def flow_text(
    elem: ET.Element,
    *,
    skip_tags: frozenset[str] | set[str] = frozenset(),
    line_tags: frozenset[str] | set[str] = frozenset(),
) -> str:
    """Body text of a structural unit, formatted like law instead of one flat blob.

    Unlike ``element_text`` (which joins everything with single spaces), this:
    - **omits the unit's own label children** (``skip_tags`` — e.g. AKN ``num``/
      ``heading``, Formex ``TI.ART``/``STI.ART``), since those are the segment label
      and would otherwise be duplicated in the body; only top-level children are
      skipped, so nested numbering ("(1)", "(a)") is kept;
    - **starts a new line before each sub-unit** (``line_tags`` — numbered
      paragraphs, lettered points), so enumerated provisions read as a list rather
      than running together.

    Tag names are matched case-insensitively on the local name."""
    out: list[str] = []

    def visit(e: ET.Element) -> None:
        if localname(e.tag).lower() in line_tags and out and not out[-1].endswith("\n"):
            out.append("\n")
        if e.text and e.text.strip():
            out.append(e.text.strip())
            out.append(" ")
        for child in e:
            visit(child)
            if child.tail and child.tail.strip():
                out.append(child.tail.strip())
                out.append(" ")

    for child in elem:
        if localname(child.tag).lower() in skip_tags:
            continue
        visit(child)

    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(out).split("\n")]
    return "\n".join(ln for ln in lines if ln)


def assemble(blocks: Iterable[tuple[str, str, str]]) -> tuple[str, list[Segment]]:
    """Join ``(label, kind, text)`` blocks into flat text + aligned segments.

    Offsets account for the ``SEP`` joiner so ``text[seg.char_start:seg.char_end]``
    is exactly the block's text."""
    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for label, kind, raw in blocks:
        block = (raw or "").strip()
        if not block:
            continue
        if parts:
            cursor += len(SEP)
        segments.append(Segment(label=label or kind, char_start=cursor,
                                char_end=cursor + len(block), kind=kind))
        parts.append(block)
        cursor += len(block)
    return SEP.join(parts), segments


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def iter_text(elem: ET.Element) -> Iterator[str]:
    """Depth-first text of an element, whitespace-trimmed per node."""
    if elem.text and elem.text.strip():
        yield elem.text.strip()
    for child in elem:
        yield from iter_text(child)
        if child.tail and child.tail.strip():
            yield child.tail.strip()


def element_text(elem: ET.Element) -> str:
    return " ".join(iter_text(elem))


def blocks_by_localname(
    root: ET.Element,
    unit_names: set[str],
    *,
    kind: str = "paragraph",
    label_attr: str | None = None,
    label_child: str | None = None,
    counter_label: str = "para",
) -> list[tuple[str, str, str]]:
    """Collect one block per element whose local-name is in ``unit_names``, in
    document order. The label comes from ``label_attr`` (an attribute by suffix),
    or ``label_child`` (a child element's text, e.g. Formex ``<NO.P>``), else a
    running counter ("para 1", "para 2", …)."""
    blocks: list[tuple[str, str, str]] = []
    n = 0
    for elem in root.iter():
        if localname(elem.tag) not in unit_names:
            continue
        text = element_text(elem)
        if not text.strip():
            continue
        n += 1
        label = None
        if label_attr:
            for key, value in elem.attrib.items():
                if key.rsplit("}", 1)[-1] == label_attr:
                    label = value
                    break
        if label is None and label_child:
            child = next((c for c in elem.iter() if localname(c.tag) == label_child), None)
            if child is not None and (child.text or "").strip():
                label = child.text.strip()
        blocks.append((label or f"{counter_label} {n}", kind, text))
    return blocks


# Judgments imported as FLAT TEXT (the Canadian A2AJ corpus, BAILII long tail)
# still carry their paragraph numbers in the prose — "[15] The applicant…" at
# line starts. Synthesising segments from those makes pinpoints ("at para 15")
# land, the minimap work, and peeks scroll — without re-importing anything.
_NUM_PARA_RE = re.compile(r"^\s{0,8}\[(\d{1,4})\]\s", re.MULTILINE)
# The dotted form — "1.", "2." at a line start — used by the High Court of
# Australia and other courts that don't bracket their paragraph numbers. Much
# noisier than the bracket form (a line can open "51." for all sorts of reasons),
# so it's a FALLBACK, gated harder: see the density guard below.
_DOT_PARA_RE = re.compile(r"^[ \t]{0,8}(\d{1,4})\.[ \t]+\S", re.MULTILINE)
# A bare paragraph number alone on its own line — the CJEU/Formex judgment layout
# ("…case-law cited).\n60\nTherefore, …"). The most ambiguous form of all, so it's
# the LAST fallback and leans hardest on the strict-sequence + density guards.
_BARE_NUM_LINE_RE = re.compile(r"^[ \t]{0,8}(\d{1,4})[ \t]*$", re.MULTILINE)
# A real numbered paragraph is at most this long on average; beyond it the
# "numbering" is a sparse scatter of stray "N." line-openers, not paragraphs
# (a 1997 HCA judgment with no paragraph numbers matched 26 across 375k chars —
# ~14k chars each — while a genuinely numbered judgment runs a few hundred).
_MAX_MEAN_PARA_CHARS = 6000


def sequential_para_marks(marks: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Given ``(number, offset)`` marks in document order, keep the longest run that
    starts at 1 (or 2) and advances by exactly one — the real paragraph numbering.
    Out-of-sequence numbers (a quoted instrument's own sub-list, a mis-numbered
    heading) are dropped, so they stay inside the enclosing paragraph. Shared by the
    BAILII/HUDOC importer and the flat-text synthesiser."""
    kept: list[tuple[int, int]] = []
    last = 0
    for n, at in marks:
        if (not kept and n in (1, 2)) or (kept and n == last + 1):
            kept.append((n, at))
            last = n
    return kept


def _sequential_marks(text: str, rx: "re.Pattern[str]") -> list[tuple[int, int, int]]:
    """The longest run of line-start numbers that starts at 1/2 and advances by
    exactly one each time (carrying each mark's match span). Off-sequence numbers (a
    quoted judgment's own paragraphs; stray "s 51.") are skipped, so they stay inside
    the host paragraph rather than splitting it — the Perreault v Canada trap."""
    marks: list[tuple[int, int, int]] = []
    last = 0
    for m in rx.finditer(text):
        n = int(m.group(1))
        if (not marks and n in (1, 2)) or (marks and n == last + 1):
            marks.append((n, m.start(), m.end()))
            last = n
    return marks


def synthesise_numbered_segments(text: str, *, min_paras: int = 3) -> list[Segment]:
    """Derive ``Segment``s from numbered paragraphs in flat text.

    Prefers the ``[N]`` bracket form; falls back to the ``N.`` dotted form (High
    Court of Australia and others) only when brackets aren't the style, because
    the dotted form is far more ambiguous. Both use the strict-sequential guard
    (a candidate is accepted only if it advances the run by one), and the dotted
    fallback additionally requires the paragraphs to be plausibly dense — a
    document whose "paragraphs" average many thousands of characters isn't
    numbered, it just has stray line-opening numbers. Returns [] when fewer than
    ``min_paras`` sequential paragraphs are found."""
    if not text:
        return []
    marks = _sequential_marks(text, _NUM_PARA_RE)
    label_fmt = "[{}]"
    if len(marks) < min_paras:
        # the ambiguous fallbacks (dotted "N.", then bare "N" on its own line), each
        # gated by a strict from-1 sequence AND a plausible mean paragraph length
        for rx, fmt in ((_DOT_PARA_RE, "{}."), (_BARE_NUM_LINE_RE, "{}")):
            cand = _sequential_marks(text, rx)
            if len(cand) < max(min_paras, 5):
                continue
            span = cand[-1][1] - cand[0][1]
            if span <= 0 or span / len(cand) > _MAX_MEAN_PARA_CHARS:
                continue
            marks, label_fmt = cand, fmt
            break
        else:
            return []
    segs: list[Segment] = []
    # preamble (intituling) before the first paragraph → unlabelled header segment
    if marks[0][1] > 0 and text[:marks[0][1]].strip():
        segs.append(Segment(label="", kind="header", level=0,
                            char_start=0, char_end=marks[0][1]))
    for i, (n, start, _bs) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        segs.append(Segment(label=label_fmt.format(n), kind="paragraph", level=1,
                            char_start=start, char_end=end))
    return segs
