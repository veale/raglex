"""LawMaker "view" HTML parser — Queensland, NSW and Tasmania.

Qld, NSW and Tas run the same Lawlab/LawMaker platform with an identical URL grammar
and an identical, clean, server-rendered ``whole/html`` view — so one parser serves all
three. The content sits in ``<div id="fragview">`` as flat Word-exported markup where
the class on each block is its structural role (verified against the *Multicultural
Recognition Act 2016*, Qld):

* ``TopHeadingSpan`` — the Act's short title.
* ``LongTitle`` / ``LongTitleParagraph`` — the long title ("An Act to …").
* ``PartHeadingParagraph`` — a Part heading (``HeadingNumber`` + ``PartHeadingName``);
  ``ChapterHeadingParagraph`` / ``DivisionHeadingParagraph`` likewise.
* ``HeadingParagraph`` — a **section** heading: ``HeadingStyle`` holds the section
  number, ``HeadingName`` the heading. The provision anchor is ``<a name="sec.N">``.
* ``FlatParagraph`` / ``Paragraph`` / ``ListNumber`` — body blocks.
* ``ScheduleHeadingParagraph`` — a Schedule.

Segments follow the Part → section hierarchy so a hit maps to a citable provision, and
internal cross-reference links (``/link?...doc.id=act-2016-001...``) become edges at
document granularity via ``lawmaker_target``.
"""

from __future__ import annotations

import re
from html import unescape

from ..core.models import (
    ExtractedVia,
    RelationshipType,
    ResolutionStatus,
    Segment,
    TypedRelation,
)
from ..core.segmentation import SEP
from .base import ParsedDoc, register

_TAG_RE = re.compile(r"<[^>]+>")
# Match the leaf blocks only — <p>, <blockquote>, the title <span>. LawMaker wraps each
# heading <p> in a <div> of the SAME class; matching <div> too would let the wrapper's
# non-greedy body swallow the title/long-title spans that sit before the first heading.
_BLOCK_RE = re.compile(
    r'<(?P<tag>p|blockquote|span)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>',
    re.S | re.I)
_CLASS_RE = re.compile(r'class=[\'"]([^\'"]*)[\'"]', re.I)
_FRAGVIEW_RE = re.compile(r'id=[\'"]fragview[\'"]', re.I)
_LINK_DOCID_RE = re.compile(r'doc\.id=(?P<type>act|sl|sr|si)-(?P<year>\d{4})-(?P<num>\d+)', re.I)
_HEADINGSTYLE_RE = re.compile(r'class=[\'"]HeadingStyle[\'"][^>]*>(.*?)</[Bb]>', re.S | re.I)


def _text(html: str) -> str:
    return unescape(_TAG_RE.sub("", html)).replace("\xa0", " ")


def _norm(s: str) -> str:
    return " ".join(s.split())


def au_id(jurisdiction: str, typ: str, year: str | int, number: str) -> str:
    """The corpus stable_id for an Australian instrument: ``au/qld/act/2016/1``.

    Jurisdiction is a first-class key — "Australian legislation" is nine separate
    registers — so it is baked into the id right after the country, ahead of the FRBR
    path. The number keeps any letter suffix and drops zero-padding so the LawMaker
    ``act-2016-001`` and a bare ``act/2016/1`` citation are the same node."""
    num = str(number).strip().lstrip("0").lower() or str(number).strip().lower()
    return f"au/{jurisdiction.lower()}/{typ.lower()}/{year}/{num}"


def lawmaker_target(href: str, jurisdiction: str) -> str | None:
    """A LawMaker cross-reference link → the ``au/{juris}/{type}/{year}/{number}``
    stable_id of the document it points at. State legislation is jurisdiction-scoped, so
    the jurisdiction of the *citing* document is carried in (an intra-register link is
    always same-jurisdiction)."""
    m = _LINK_DOCID_RE.search(href or "")
    if not m:
        return None
    typ = {"act": "act", "sl": "sl", "sr": "sr", "si": "si"}[m.group("type").lower()]
    return au_id(jurisdiction, typ, m.group("year"), m.group("num"))


def parse_lawmaker_html(data: bytes, *, jurisdiction: str = "") -> ParsedDoc:
    html = data.decode("utf-8", errors="replace")
    fv = _FRAGVIEW_RE.search(html)
    if fv:
        html = html[fv.end():]

    title: str | None = None
    long_title: str | None = None
    blocks: list[tuple[str, str, str, int]] = []
    relations: list[TypedRelation] = []
    seen_targets: set[str] = set()
    label, kind, level = "document", "section", 0
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(b for b in buffer if b).strip()
        if text:
            blocks.append((label, kind, text, level))
        buffer.clear()

    def add_links(raw_html: str, src_label: str) -> None:
        for href in re.findall(r'href=[\'"]([^\'"]+)[\'"]', raw_html):
            dst = lawmaker_target(href, jurisdiction)
            if dst and dst not in seen_targets:
                seen_targets.add(dst)
                relations.append(TypedRelation(
                    relationship_type=RelationshipType.MENTIONS,
                    raw_citation_string=href, dst_id=dst, src_anchor=src_label,
                    extracted_via=ExtractedVia.STRUCTURED,
                    resolution_status=ResolutionStatus.PENDING))

    for m in _BLOCK_RE.finditer(html):
        cls_m = _CLASS_RE.search(m.group("attrs"))
        classes = (cls_m.group(1) if cls_m else "").lower()
        body = _norm(_text(m.group("body")))
        if not body:
            continue

        if "topheadingspan" in classes and title is None:
            title = body
            continue
        if "longtitle" in classes:
            if long_title is None:
                long_title = body
            continue
        # Only act on the outermost block for each role (LawMaker nests <p> in <div> of
        # the same class); a section heading's <p> and its wrapper <div> both match, so
        # dedupe by ignoring a block identical to the one just buffered.
        if any(k in classes for k in ("partheading", "chapterheading", "divisionheading")):
            flush()
            label, kind, level = body, "part", 0
            buffer.append(body)
        elif "scheduleheading" in classes:
            flush()
            label, kind, level = body, "schedule", 0
            buffer.append(body)
        elif "headingparagraph" in classes and "partheading" not in classes:
            num_m = _HEADINGSTYLE_RE.search(m.group("body"))
            num = _norm(_text(num_m.group(1))) if num_m else ""
            flush()
            label = f"s. {num} {body[len(num):].strip()}".strip() if num and body.startswith(num) else f"s. {body}"
            kind, level = "section", 1
            buffer.append(body)
            add_links(m.group("body"), label)
        elif any(k in classes for k in ("flatparagraph", "paragraph", "listnumber", "note")):
            # a body block — but skip if it's the wrapper duplicate of the last append
            if not buffer or buffer[-1] != body:
                buffer.append(body)
            add_links(m.group("body"), label)
    flush()

    if not blocks:
        return ParsedDoc(title=title)

    # Collapse the wrapper/inner duplication: consecutive identical lines within a block.
    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for lbl, knd, text, lvl in blocks:
        lines = text.split("\n")
        deduped = [ln for i, ln in enumerate(lines) if i == 0 or ln != lines[i - 1]]
        text = "\n".join(deduped)
        if parts:
            cursor += len(SEP)
        segments.append(Segment(label=lbl, char_start=cursor, char_end=cursor + len(text),
                                kind=knd, level=lvl))
        parts.append(text)
        cursor += len(text)

    return ParsedDoc(
        text=SEP.join(parts) or None,
        segments=segments,
        relations=relations,
        title=title,
        metadata={"long_title": long_title},
    )


register("lawmaker-html", lambda data: parse_lawmaker_html(data))
