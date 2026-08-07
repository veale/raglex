"""European Parliament resolutions — the two markups the same text arrives in.

A resolution is not an act, and the act parsers make nothing of it. It has no
``ENACTING.TERMS`` and no ``ARTICLE``: its structure is a preamble of *visas*
("having regard to …"), a run of lettered *recitals* ("A. whereas …"), and a run of
numbered *operative paragraphs* ("1. Recalls that …") under bold section headings.
Handed to ``formex-legislation`` a resolution matches none of that and falls through
to the whole-document branch — one 60 KB blob with no citable anchor in it, when
what a reader actually cites is "paragraph 12 of the resolution".

Two markups carry the identical text:

* **SDOCTA** (``TA.xsd``) is what the Parliament's own portal serves, within days of
  the vote — ``<VISA>``, ``<CONS>``, ``<ACTION>``, ``<ACTINIT>`` headings, and the
  annex as a second ``<TXTLST>``.
* **Formex** ``<GENERAL>`` is what CELLAR serves once the text reaches the OJ C
  series, which can be **more than a year later** (the 2017 robotics resolution was
  published in OJ C 252 of 18 July 2018) — ``<LIST TYPE="DASH">`` visas,
  ``<NP><NO.P>`` for both recitals and paragraphs, ``<GR.SEQ>`` headings, and the
  annex in a *separate zip member*.

Neither markup distinguishes a recital from an operative paragraph by tag, and the
drafting has moved over time — in 2017 the lettered recitals were ``<ACTION>``
elements under ``<DISPOSITIF>``, in 2025 they are ``<CONS>`` under ``<GRCONS>``. So
both parsers classify on the **marker** (``–`` / ``A.`` / ``1.``), which is stable
across every year sampled, rather than on the element that happens to hold it.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from ..core.segmentation import assemble, localname
from .base import ParsedDoc, register
from .formex import unzip_formex_contents

#: The marker that opens a "having regard to" visa. Sources use several dashes.
_DASHES = {"-", "–", "—", "−", "*", "•"}
#: "A.", "(B)", "AA." — a recital. Capped at three letters: recitals run past Z into
#: AA/AB, and the 2017 robotics resolution reaches AH, but nothing runs to four.
_LETTER = re.compile(r"^\(?([A-Z]{1,3})\)?[.)]?$")
#: "1.", "(12)", "23)" — an operative paragraph.
_NUMBER = re.compile(r"^\(?(\d{1,4})\)?[.)]?$")

#: Label children: the marker is the segment's label, so it must not be repeated in
#: the body. ``TI``/``STI`` are an annex's own heading, handled separately.
_SKIP = {"no.p", "ti", "sti"}
#: Rendered parenthetically rather than run into the sentence. A footnote in a visa
#: is where the OJ reference of the instrument being invoked actually lives, so it is
#: kept — it is often the only citation in that line the grammars can resolve.
_PAREN = {"footnote", "note"}
#: The Formex publication manifest that opens every member. It is a run of bare
#: numbers ("C 252 2018 EN 239 1 01 20170216 …") and prefixed the annex's text with
#: nonsense until it was excluded; nothing in it is part of the document.
_BIB = {"bib.instance", "bib.doc", "document.ref", "publication.ref"}


def _flat(elem: ET.Element, *, skip: set[str] = _SKIP) -> str:
    """Readable text of one unit: label children dropped, footnotes parenthesised."""
    parts: list[str] = []

    def walk(node: ET.Element, top: bool) -> None:
        name = localname(node.tag).lower()
        if name in _BIB:
            return
        # The tail belongs to the PARENT's prose whatever we do with the node itself,
        # so it is appended on every path. Dropping it for top-level children cost the
        # 2017 robotics resolution its subject: the title is one <P> holding an inline
        # <DATE>, and everything after "16 February 2017" is that element's tail.
        if not (top and name in skip):
            if name in _PAREN:
                inner = " ".join(_flat(node, skip=set()).split())
                if inner:
                    parts.append(f"({inner})")
            else:
                if node.text and node.text.strip():
                    parts.append(node.text.strip())
                for child in node:
                    walk(child, False)
        if node.tail and node.tail.strip():
            parts.append(node.tail.strip())

    if elem.text and elem.text.strip():
        parts.append(elem.text.strip())
    for child in elem:
        walk(child, True)
    text = re.sub(r"\s+", " ", " ".join(parts))
    # the joins above put a space beside punctuation that bounded an inline element
    return re.sub(r"\(\s+", "(", re.sub(r"\s+([,.;:)])", r"\1", text)).strip()


def _marker(elem: ET.Element) -> str:
    no = next((e for e in elem.iter() if localname(e.tag).upper() == "NO.P"), None)
    return " ".join((no.text or "").split()) if no is not None else ""


def classify(marker: str) -> tuple[str, str] | None:
    """``"A."`` → ``("Recital A", "recital")``; ``"12."`` → ``("Paragraph 12",
    "paragraph")``; a dash → the preamble. ``None`` for a marker we do not read."""
    mark = (marker or "").strip()
    if not mark or mark in _DASHES:
        return ("Preamble", "preamble")
    m = _LETTER.match(mark)
    if m:
        return (f"Recital {m.group(1)}", "recital")
    m = _NUMBER.match(mark)
    if m:
        return (f"Paragraph {int(m.group(1))}", "paragraph")
    return None


#: The typographic separator the OJ prints between the operative paragraphs and the
#: closing instruction to the President. It is a ``GR.SEQ`` title like any heading and
#: became a section segment labelled "o o o" — a rule, not a part of the resolution.
_SEPARATOR = re.compile(r"^[\W_oO°•∗*\s]+$")


def is_heading(text: str) -> bool:
    return bool(text) and not _SEPARATOR.match(text)


def _strip_marker(body: str, marker: str) -> str:
    """The marker is the label; drop it from the body if the source repeated it."""
    mark = (marker or "").strip()
    if mark and body.startswith(mark):
        return body[len(mark):].lstrip()
    return body


def _dedupe(blocks: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Make labels unique. A resolution with an annex genuinely restarts at
    "Paragraph 1", and two segments sharing a label make a pincite ambiguous."""
    seen: dict[str, int] = {}
    out: list[tuple[str, str, str]] = []
    for label, kind, text in blocks:
        seen[label] = seen.get(label, 0) + 1
        out.append((label if seen[label] == 1 else f"{label} ({seen[label]})", kind, text))
    return out


# --------------------------------------------------------------------------- SDOCTA


#: ``(A9-0188/2023 - Rapporteurs: Brando Benifei, Ioan-Dragoş Tudorache)`` — the
#: committee report the resolution adopts, and who wrote it.
_HIDDEN = re.compile(
    r"\(\s*(?P<report>A\d{1,2}-\d{4}/\d{4})\s*(?:-\s*Rapporteurs?\s*:\s*(?P<who>[^)]+))?\)",
    re.I)
#: ``P9_TA(2024)0138`` / ``P9_TA-PROV(2024)0138`` — the Parliament's own identifier.
_TA_ID = re.compile(r"\bP(\d{1,2})_TA(?:-PROV)?\((\d{4})\)(\d{3,4})\b")


def _sdocta_metadata(root: ET.Element) -> dict:
    meta: dict = {}
    ident = next((e for e in root if localname(e.tag).upper() == "IDENT"), None)
    if ident is not None:
        m = _TA_ID.search(_flat(ident, skip=set()))
        if m:
            meta["ep_document_id"] = f"P{m.group(1)}_TA({m.group(2)}){m.group(3)}"
    hidden = next((e for e in root.iter() if localname(e.tag).upper() == "HIDDEN"), None)
    if hidden is not None:
        m = _HIDDEN.search(_flat(hidden, skip=set()))
        if m:
            meta["report"] = m.group("report").upper()
            if m.group("who"):
                meta["rapporteurs"] = [x.strip() for x in re.split(r",| and ", m.group("who"))
                                       if x.strip()]
    return meta


def _sdocta_title(root: ET.Element) -> str | None:
    """The resolution's own title, not the portal's three-word display label.

    ``<SDOCTA><TI>`` is "Artificial Intelligence Act"; the citable title is the
    ``<TI>`` inside ``<RESOL>`` — "European Parliament legislative resolution of 13
    March 2024 on the proposal for a regulation … (2021/0106(COD))"."""
    for parent in ("RESOL", "SDOCTA"):
        for node in root.iter():
            if localname(node.tag).upper() != parent:
                continue
            ti = next((e for e in node if localname(e.tag).upper() == "TI"), None)
            if ti is not None:
                title = _flat(ti, skip=set())
                if title:
                    return title[:500]
    return None


def parse_ep_ta_xml(raw: bytes) -> ParsedDoc:
    """The Parliament's own ``SDOCTA`` rendition of an adopted text."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return ParsedDoc()
    if localname(root.tag).upper() != "SDOCTA":
        root = next((e for e in root.iter() if localname(e.tag).upper() == "SDOCTA"), root)

    blocks: list[tuple[str, str, str]] = []
    preamble: list[str] = []
    for node in root.iter():
        name = localname(node.tag).upper()
        if name == "PRINIT":                       # "The European Parliament,"
            text = _flat(node)
            if text:
                preamble.append(text)
        elif name in ("VISA", "CONS", "ACTION"):
            marker = _marker(node)
            kinds = classify(marker)
            if kinds is None:
                continue
            label, kind = kinds
            body = _strip_marker(_flat(node), marker)
            if not body:
                continue
            if kind == "preamble":
                preamble.append(body)
            else:
                blocks.append((label, kind, body))
        elif name == "ACTINIT":                    # a bold heading over the paragraphs
            heading = _flat(node)
            if is_heading(heading):
                blocks.append((heading[:120], "section", heading))
        elif name == "ANNEX":
            title_node = next((e for e in node if localname(e.tag).upper() in ("TI", "STI")),
                              None)
            label = (_flat(title_node, skip=set()) if title_node is not None else "") or "Annex"
            body = _flat(node)
            if body:
                blocks.append((label[:120], "annex", body))

    if preamble:
        blocks.insert(0, ("Preamble", "preamble", "\n".join(preamble)))
    if not blocks:
        return ParsedDoc()
    text, segments = assemble(_dedupe(blocks))
    return ParsedDoc(text=text or None, segments=segments,
                     title=_sdocta_title(root), metadata=_sdocta_metadata(root))


# --------------------------------------------------------------------------- Formex


#: The opening of a resolution's real title, as printed in the OJ C.
_TITLE_HEAD = re.compile(
    r"^European Parliament\s+(?:legislative\s+|non-legislative\s+)?"
    r"(?:resolution|decision|position|recommendation|declaration)\b", re.I)


def _formex_title(root: ET.Element) -> str | None:
    """``<TITLE><TI>`` holds several paragraphs: the ``P8_TA(…)`` identifier, a short
    display label ("Civil Law Rules on Robotics"), then the real title — which the OJ
    typesetting **splits across further paragraphs**. Taking the longest single one
    stopped at "European Parliament resolution of 16 February 2017" and dropped the
    subject, so join from the paragraph that opens the title to the end."""
    ti = next((e for e in root.iter() if localname(e.tag).upper() == "TI"), None)
    if ti is None:
        return None
    # NO.DOC.C is the OJ's own item number ("2018/C 252/25"), printed inside the title
    # block but not part of the title.
    lines = [x for x in (_flat(p, skip=set()) for p in ti
                         if localname(p.tag).upper() != "NO.DOC.C") if x]
    lines = lines or [_flat(ti, skip=set())]
    lines = [x for x in lines if not _TA_ID.fullmatch(x.strip())]
    if not lines:
        return None
    start = next((i for i, x in enumerate(lines) if _TITLE_HEAD.match(x)), None)
    if start is not None:
        return " ".join(lines[start:])[:500]
    return max(lines, key=len)[:500]


def _is_resolution(root: ET.Element) -> bool:
    """A Formex instance that is an EP resolution rather than an act or a judgment."""
    if any(localname(e.tag).upper() in ("ENACTING.TERMS", "ARTICLE") for e in root.iter()):
        return False
    return any(localname(e.tag).upper() in ("PREAMBLE.GEN", "PREAMBLE.INIT", "GR.SEQ")
               for e in root.iter())


def _formex_blocks(root: ET.Element) -> tuple[list[str], list[tuple[str, str, str]]]:
    preamble: list[str] = []
    blocks: list[tuple[str, str, str]] = []
    gen = next((e for e in root.iter() if localname(e.tag).upper() == "PREAMBLE.GEN"), None)
    if gen is not None:
        preamble.append(_flat(gen))
    for node in root.iter():
        name = localname(node.tag).upper()
        if name == "GR.SEQ":
            ti = next((e for e in node if localname(e.tag).upper() == "TITLE"), None)
            heading = _flat(ti, skip=set()) if ti is not None else ""
            if is_heading(heading):
                blocks.append((heading[:120], "section", heading))
        elif name == "NP":
            kinds = classify(_marker(node))
            if kinds is None:
                continue
            label, kind = kinds
            body = _strip_marker(_flat(node), _marker(node))
            if not body:
                continue
            if kind == "preamble":
                preamble.append(body)
            else:
                blocks.append((label, kind, body))
    return [x for x in preamble if x], blocks


def parse_formex_resolution(raw: bytes) -> ParsedDoc:
    """CELLAR's Formex rendition, published with the OJ C issue.

    Every content member is read, not just the largest: the annex to the 2017
    robotics resolution is a *second, smaller* member (``…01025201.xml``, root
    ``<ANNEX>``), and taking the largest silently drops the charter of robotics
    principles that is the whole point of that resolution."""
    members = unzip_formex_contents(raw)
    roots: list[ET.Element] = []
    for data in members:
        try:
            roots.append(ET.fromstring(data))
        except ET.ParseError:
            continue
    if not roots:
        return ParsedDoc()

    title = None
    preamble: list[str] = []
    blocks: list[tuple[str, str, str]] = []
    for root in roots:
        if localname(root.tag).upper() == "ANNEX" or any(
                localname(e.tag).upper() == "ANNEX" for e in root.iter()):
            annex = root if localname(root.tag).upper() == "ANNEX" else next(
                e for e in root.iter() if localname(e.tag).upper() == "ANNEX")
            label = _formex_title(annex) or "Annex"
            body = _flat(annex)
            if body:
                blocks.append((label[:120], "annex", body))
            continue
        if not _is_resolution(root):
            continue
        title = title or _formex_title(root)
        member_preamble, member_blocks = _formex_blocks(root)
        preamble += member_preamble
        blocks += member_blocks

    if preamble:
        blocks.insert(0, ("Preamble", "preamble", "\n".join(preamble)))
    if not blocks:
        return ParsedDoc()
    text, segments = assemble(_dedupe(blocks))
    return ParsedDoc(text=text or None, segments=segments, title=title)


register("ep-ta-xml", parse_ep_ta_xml)
register("formex-resolution", parse_formex_resolution)
