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
