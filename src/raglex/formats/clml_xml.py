"""CLML (legislation.gov.uk ``data.xml``) parser — needed for **assimilated EU
regulations**.

legislation.gov.uk publishes two XML representations. For UK Acts the Akoma Ntoso
``data.akn`` carries the full body, so :mod:`raglex.formats.akoma_ntoso` handles
them. But for assimilated EU legislation (``eur/…``) the AKN body is *empty* — only
the table of contents and recitals are present — while the operative articles live
solely in the older Crown Legislation Markup Language (CLML) ``data.xml``. Without
this parser those regs extract to one undifferentiated block ("no separation").

CLML EU-reg shape::

    <Legislation><EUBody>
      <EUChapter><Number>CHAPTER I</Number><Title>General provisions</Title>
        <P1group><Title>Subject-matter …</Title>
          <P1><Pnumber>Article 1</Pnumber>
            <P1para><P2><Pnumber>1</Pnumber><P2para><Text>…</Text></P2para></P2>…

So ``EUChapter`` (and ``Part``/``Pblock``) are heading containers, ``P1group``/``P1``
is the citable unit (an Article), and ``P2``/``P3``/``point`` are its sub-provisions.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from ..core.models import Segment
from ..core.segmentation import SEP, element_text, localname
from .base import ParsedDoc, register

# Heading containers: emit their Number+Title as a header line, descend for units.
_HEADING = {"eupart", "euchapter", "eutitle", "eusection", "part", "chapter", "pblock", "group"}
# Citable unit wrappers (an Article/section lives in a P1group's P1).
_UNIT = {"p1group"}
# Sub-provisions that each start a new line so a provision reads as a list.
_LINE = {"p2", "p3", "p4", "p5", "p6", "para"}
# The article body lives in these; a P1's own Pnumber/Title are its label, not body.
_BODY = {"p1para", "p1text", "text"}
# Container names normalised so the reader sees the same kinds AKN emits.
_KIND = {"euchapter": "chapter", "eupart": "part", "eutitle": "title", "eusection": "section"}


def _child_text(elem: ET.Element, name: str) -> str | None:
    for c in elem:
        if localname(c.tag).lower() == name.lower():
            return element_text(c) or None
    return None


def _flow(elem: ET.Element, out: list[str]) -> None:
    """Flat text of a provision, sub-units on their own line, provision numbers
    kept inline but spaced off the prose they run into ("1This …" → "1 This …")."""
    name = localname(elem.tag).lower()
    if name in _LINE and out and not out[-1].endswith("\n"):
        out.append("\n")
    if name == "pnumber":
        t = element_text(elem)
        if t:
            out.append(t + " ")
        return
    if elem.text and elem.text.strip():
        out.append(elem.text)
    for c in elem:
        _flow(c, out)
        if c.tail and c.tail.strip():
            out.append(c.tail)


def _flow_text(elem: ET.Element) -> str:
    out: list[str] = []
    _flow(elem, out)
    lines = [" ".join(ln.split()) for ln in "".join(out).split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _header(elem: ET.Element) -> str:
    num = _child_text(elem, "Number") or ""
    title = _child_text(elem, "Title") or ""
    return f"{num} {title}".strip()


def _walk(elem: ET.Element, level: int, blocks: list[tuple[str, str, str, int]]) -> None:
    for child in elem:
        name = localname(child.tag).lower()
        if name in _UNIT:
            gtitle = _child_text(child, "Title") or ""
            p1 = next((c for c in child if localname(c.tag).lower() == "p1"), None)
            if p1 is None:
                continue
            pnum = _child_text(p1, "Pnumber") or ""
            label = f"{pnum} {gtitle}".strip() or pnum or "provision"
            paras = [c for c in p1 if localname(c.tag).lower() in _BODY]
            body = "\n".join(_flow_text(pp) for pp in paras) if paras else _flow_text(p1)
            if body.strip():
                blocks.append((label, "section", body.strip(), level))
        elif name in _HEADING:
            header = _header(child)
            if header:
                blocks.append((header, _KIND.get(name, name), header, level))
            _walk(child, level + 1, blocks)
        else:
            _walk(child, level, blocks)


def _title(root: ET.Element) -> str | None:
    for name in ("Title", "ShortTitle", "DocumentTitle"):
        for e in root.iter():
            if localname(e.tag).lower() == name.lower():
                txt = " ".join(element_text(e).split())
                if txt.strip():
                    return txt
    return None


def parse_clml(data: bytes) -> ParsedDoc:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc()

    body = next((e for e in root.iter()
                 if localname(e.tag).lower() in ("eubody", "body", "primary")), root)
    blocks: list[tuple[str, str, str, int]] = []
    _walk(body, 0, blocks)
    if not blocks:  # unrecognised shape — fall back to whole-document text
        blocks = [(_title(root) or "document", "section", element_text(body), 0)]

    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for label, kind, text, level in blocks:
        text = text.strip()
        if not text:
            continue
        if parts:
            cursor += len(SEP)
        segments.append(Segment(label=label, char_start=cursor, char_end=cursor + len(text),
                                kind=kind, level=level))
        parts.append(text)
        cursor += len(text)

    return ParsedDoc(text=SEP.join(parts) or None, segments=segments, title=_title(root))


register("clml", parse_clml)
