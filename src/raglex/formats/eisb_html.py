"""Irish Statute Book HTML parser — for everything the eISB has no XML for.

XML is not universal on the eISB: **Statutory Instruments, SROs, pre-1922 Acts and the
Constitution all 404 on ``/xml``** (verified), and SIs alone are the dominant mass of
the corpus. For those, HTML is not a fallback — it is the only structured source. This
parser reads the ``print`` representation by preference, because it is a single flat
document holding the whole instrument, where the paginated ``html`` view would have to
be crawled section by section.

The markup is layout-driven rather than semantic: the text sits in
``<div class="act-content" id="act">`` as a three-column ``<table>``, one ``<tr>`` per
provision, with the actual words in the third ``<td>``. There is no ``@number``
attribute anywhere — a provision's number is the leading token of its own text ("1.",
"(a)", "(i)") — and nesting is encoded only in the inline ``style`` (``margin-left`` /
``text-indent``). So both are recovered here:

* **Provision boundaries** come from the ``<a name="secN">`` anchors the print view
  emits for Acts, and, where there are none (SIs), from a leading "N." token at the
  outermost indent level.
* **Nesting depth** comes from ``margin-left``, bucketed to a level so "(a)" sits under
  its section and "(i)" under its paragraph.
* **Cross-references** use the *legacy* path scheme (``/2013/en/act/pub/0015/print.html#sec36``),
  not ELI — documents link each other that way — so the link resolver understands both
  and mints edges at section granularity.

An SI's trailing **Explanatory Note** is captured as its own segment and flagged
non-operative: it is useful context and useful to search, but it is expressly not part
of the instrument.
"""

from __future__ import annotations

import re

from ..core.models import (
    ExtractedVia,
    RelationshipType,
    ResolutionStatus,
    Segment,
    TypedRelation,
)
from ..core.segmentation import SEP
from .base import ParsedDoc, register
from .eisb_xml import eli_id

# Legacy path scheme — the form documents actually cross-reference each other with.
#   /2013/en/act/pub/0015/print.html#sec36   /1993/en/si/0266.html
_LEGACY_ACT = re.compile(
    r"/(?P<year>\d{4})/(?P<lang>en|ga)/act/(?P<sub>pub|prv)/(?P<num>\d+)"
    r"(?:/[^#?\s]*)?(?:#sec(?P<sec>\d+))?", re.IGNORECASE)
_LEGACY_SI = re.compile(
    r"/(?P<year>\d{4})/(?P<lang>en|ga)/(?P<type>si|sro)/(?P<num>\d+[a-z]?)", re.IGNORECASE)
# ELI links appear too, on newer pages.
_ELI_HREF = re.compile(
    r"/eli/(?P<year>\d{4})/(?P<type>act|si|sro|prv|ca)/(?P<num>[0-9]+[a-z]?)", re.IGNORECASE)

# Structural anchors the print view emits: sec1, sched, sched2, part3, part3-chap1.
# Where they exist they are authoritative, and beat any guess made from the text.
_STRUCT_ANCHOR = re.compile(r"^(?:[a-z0-9]+-)?(?P<kind>sec|sched(?:ule)?|part|chap(?:ter)?)"
                            r"(?P<num>\d*[a-z]?)$", re.IGNORECASE)
_ANCHOR_LABEL = {"sec": "s.", "sched": "Schedule", "schedule": "Schedule",
                 "part": "Part", "chap": "Chapter", "chapter": "Chapter"}
_ANCHOR_KIND = {"sec": "section", "sched": "schedule", "schedule": "schedule",
                "part": "part", "chap": "chapter", "chapter": "chapter"}
_ANCHOR_LEVEL = {"part": 0, "chap": 1, "chapter": 1, "sec": 1, "sched": 0, "schedule": 0}
# A provision's number is the leading token of its own text — there is no attribute.
_LEADING_NUM = re.compile(r"^(?P<num>\d+[A-Z]?)\s*[.—-]")
_MARGIN = re.compile(r"margin-left:\s*([\d.]+)em")

_EXPLANATORY = re.compile(r"^\s*EXPLANATORY\s+NOTE", re.IGNORECASE)
_HEADINGS = re.compile(r"^\s*(SCHEDULE|FIRST SCHEDULE|SECOND SCHEDULE|THIRD SCHEDULE|"
                       r"PART\s+[IVXLC\d]+|CHAPTER\s+[IVXLC\d]+)\b", re.IGNORECASE)


def href_target(href: str) -> tuple[str, str | None] | None:
    """A cross-reference href → (stable_id, pinpoint anchor), for either the legacy path
    scheme or an ELI path. Returns None for anything else (in-page anchors, off-site
    links), so a dead link never becomes a permanently-unresolvable edge."""
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return None
    m = _LEGACY_ACT.search(href)
    if m:
        typ = "act" if m.group("sub").lower() == "pub" else "prv"
        lang = m.group("lang").lower()
        sec = m.group("sec")
        return eli_id(typ, m.group("year"), m.group("num"), lang), (f"s. {int(sec)}" if sec else None)
    m = _LEGACY_SI.search(href)
    if m:
        return eli_id(m.group("type"), m.group("year"), m.group("num"), m.group("lang").lower()), None
    m = _ELI_HREF.search(href)
    if m:
        return eli_id(m.group("type"), m.group("year"), m.group("num")), None
    return None


def _level(style: str) -> int:
    """Nesting depth from the inline layout, the only place the source records it.
    Buckets rather than exact ems: the stylesheet uses a handful of indents (0, 1.5,
    3.0, 4.5…) and a provision's depth is what matters, not its typography."""
    m = _MARGIN.search(style or "")
    if not m:
        return 0
    try:
        return min(int(float(m.group(1)) // 1.5), 4)
    except ValueError:
        return 0


def parse_eisb_html(data: bytes) -> ParsedDoc:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    title = None
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)
    # 404s are served as a real HTTP 404 but check the body too — a probe that trusts
    # the status alone records phantom formats when a soft-404 slips through.
    if title and title.lower().startswith(("404", "not found")):
        return ParsedDoc()

    content = soup.find("div", class_="act-content") or soup.find(id="act")
    if content is None:
        return ParsedDoc(title=title)
    for tag in content(["script", "style"]):
        tag.decompose()

    relations: list[TypedRelation] = []
    blocks: list[tuple[str, str, str, int]] = []  # label, kind, text, level
    label, kind, level = title or "text", "section", 0
    buffer: list[str] = []
    in_note = False
    pending: tuple[str, str, int] | None = None  # a structural anchor awaiting its text

    # Acts served as `print` carry structural anchors; SIs and pre-1922 material don't.
    # When they exist they define the provision boundaries, and the text-shape guesses
    # below are switched off — otherwise the contents list at the top of a print page
    # (which repeats every heading) fragments the document a second time.
    anchored = any(_STRUCT_ANCHOR.match(a.get("name") or a.get("id") or "")
                   for a in content.find_all("a"))

    def flush() -> None:
        text = "\n".join(b for b in buffer if b).strip()
        if text:
            blocks.append((label, kind, text, level))
        buffer.clear()

    for elem in content.find_all(["p", "a"]):
        if elem.name == "a":
            m = _STRUCT_ANCHOR.match(elem.get("name") or elem.get("id") or "")
            if m:
                anchor_kind = m.group("kind").lower()
                num = m.group("num")
                stem = _ANCHOR_LABEL[anchor_kind]
                pending = (f"{stem} {num}".strip() if num else stem,
                           _ANCHOR_KIND[anchor_kind], _ANCHOR_LEVEL[anchor_kind])
            continue

        text = elem.get_text(" ", strip=True)
        if not text:
            continue
        depth = _level(elem.get("style") or "")

        if _EXPLANATORY.match(text):
            flush()
            label, kind, level, in_note = "Explanatory Note", "note", 0, True
        elif pending:
            flush()
            label, kind, level = pending
            pending, in_note = None, False
        elif anchored or in_note:
            pass  # inside an anchored provision — text shape adds nothing
        elif depth == 0 and _LEADING_NUM.match(text) and buffer:
            # SI / pre-1922 shape: no anchors, so an outdented "N." starts a provision
            flush()
            label = f"s. {_LEADING_NUM.match(text).group('num')}"
            kind, level = "section", 0
        elif _HEADINGS.match(text) and len(text) < 120:
            flush()
            label, kind, level = " ".join(text.split()), "schedule", 0

        buffer.append(text)
        for link in elem.find_all("a", href=True):
            target = href_target(link["href"])
            if target is None:
                continue
            dst_id, anchor = target
            relations.append(TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string=link["href"], dst_id=dst_id,
                src_anchor=label, dst_anchor=anchor,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))
    flush()

    if not blocks:
        return ParsedDoc(title=title)

    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for lbl, knd, text, lvl in blocks:
        if parts:
            cursor += len(SEP)
        segments.append(Segment(label=lbl, char_start=cursor, char_end=cursor + len(text),
                                kind=knd, level=lvl))
        parts.append(text)
        cursor += len(text)

    seen: set[tuple] = set()
    deduped = []
    for r in relations:
        key = (r.dst_id, r.dst_anchor, r.src_anchor)
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return ParsedDoc(text=SEP.join(parts) or None, segments=segments,
                     relations=deduped, title=title,
                     metadata={"has_explanatory_note": any(k == "note" for _, k, _, _ in blocks)})


register("eisb-html", parse_eisb_html)
