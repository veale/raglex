"""Finlex judgment Akoma Ntoso — the cleanest structured case law in the corpus.

Finland publishes every judgment, DPA decision and Chancellor of Justice opinion as AKN
3.0 with a Finlex extension namespace, and the structure is genuinely used rather than
being a wrapper round a blob:

```xml
<akomaNtoso><judgment name="main">
  <meta>
    <identification><FRBRWork>
      <FRBRalias name="ecli" value="ECLI:FI:KKO:2024:1"/>
      <FRBRalias name="diaryNumber" value="S2022/290"/>
      <FRBRdate date="2024-01-03" name="dateIssued"/>
      <FRBRauthor href="#organization_fi.court-of-appeal-helsinki"/>
    <classification><keyword showAs="Konkurssi" value="konkurssi"/>
    <proprietary><finlex:legalBasis refersTo="#concept_…legal-basis.eu.gdpr"/>
  <header><p><docNumber>KKO:2024:1</docNumber></p>
  <judgmentBody>
    <introduction>…</introduction>
    <background><tblock><heading>Asian käsittely käräjäoikeudessa</heading>
                       <tblock><heading>Asian tausta</heading><p>…</p>
    <motivation>…</motivation>
    <decision>…</decision>
```

Why this needs its own parser rather than the shared ``akoma_ntoso`` one: that parser is
built for **legislation** — Part/Chapter/Section with ``num`` and ``heading`` children —
and its ``_UNIT_TAGS`` treats ``judgmentbody`` as one leaf unit, which would emit an
entire Supreme Court judgment as a single segment. Finland's structure is *nested
``tblock`` with ``heading``*, four levels deep in an ordinary KKO judgment, and that
nesting is exactly what a reader renders as an outline: "Asian käsittely
käräjäoikeudessa" › "Käräjäoikeuden tuomio 1.4.2022 nro 22/12878" › the reasoning under
it. Registering a second format is the rule the authoring contract states — the markup
family is the same, the *structure* is not.

## The four body zones are the judgment's own division

``introduction`` (the headnote and the provisions applied), ``background`` (the history
below), ``motivation`` (the reasons) and ``decision`` (the order). Not every judgment has
all four — a DPA decision is ``decision`` alone, an insurance-court one is
``introduction`` + ``decision`` — so they are emitted as top-level zone headings only
where they exist, under their Finnish names.

## Some judgments are a PDF with an XML wrapper

Where the AKN carries metadata and no ``judgmentBody`` prose, the text is genuinely
elsewhere (``main.pdf``). The parser reports that as an empty ``text`` with
``metadata['pdf_only']``, so the adapter can go and fetch the PDF instead of storing an
empty document.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from ..core.segmentation import assemble, localname
from .base import ParsedDoc, register

#: The judgmentBody zones, in the order Finlex emits them, with the name the court uses.
_ZONES = {
    "introduction": "Johdanto",
    "background": "Asian tausta ja käsittely",
    "motivation": "Perustelut",
    "decision": "Ratkaisu",
    "conclusions": "Lopputulos",
    "arguments": "Asianosaisten perustelut",
    "remedies": "Muutoksenhaku",
}
#: Inline elements whose text belongs to the paragraph around them.
_INLINE = {"b", "i", "u", "sup", "sub", "span", "a", "ref", "mref", "rref",
           "date", "docnumber", "doctitle", "docauthor", "organization", "person",
           "courttype", "num", "inline", "eol", "noteref"}
#: Block elements that start a new line inside a paragraph.
_BLOCK = {"p", "li", "item", "blocklist", "ul", "ol", "table", "tr", "th", "td",
          "listintroduction", "listwrapup"}


def _text_of(elem: ET.Element) -> str:
    out: list[str] = []

    def visit(e: ET.Element, top: bool = False) -> None:
        name = localname(e.tag).lower()
        if not top and name in _BLOCK and out and not out[-1].endswith("\n"):
            out.append("\n")
        if e.text:
            out.append(e.text)
        for child in e:
            visit(child)
            if child.tail:
                out.append(child.tail)

    visit(elem, top=True)
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in "".join(out).split("\n")]
    return "\n".join(line for line in lines if line)


def _heading_of(elem: ET.Element) -> str | None:
    child = next((c for c in elem if localname(c.tag).lower() == "heading"), None)
    return _text_of(child) if child is not None else None


def _walk(elem: ET.Element, level: int, blocks: list[tuple[str, str, str, int]],
          label: str) -> None:
    """Emit a heading block for each ``tblock`` and a paragraph block for each ``p``.

    The recursion depth becomes ``Segment.level``, which is what lets the reader render
    a KKO judgment's four-deep nesting as an outline instead of as a flat run of bold
    lines. A ``tblock`` with no heading is a grouping wrapper and contributes no block of
    its own, only its depth.
    """
    for child in elem:
        name = localname(child.tag).lower()
        if name == "heading":
            continue
        if name == "tblock":
            heading = _heading_of(child)
            if heading:
                blocks.append((heading, "heading", heading, level))
            _walk(child, level + (1 if heading else 0), blocks, heading or label)
            continue
        body = _text_of(child)
        if body:
            blocks.append((label, "paragraph", body, level + 1))


def _frbr(root: ET.Element) -> dict:
    meta: dict = {"aliases": {}}
    for elem in root.iter():
        name = localname(elem.tag).lower()
        if name == "frbralias":
            key = (elem.get("name") or "").strip()
            if key:
                meta["aliases"][key] = (elem.get("value") or "").strip()
        elif name == "frbrdate":
            kind = (elem.get("name") or "").strip()
            value = (elem.get("date") or "").strip()
            if kind and value:
                meta.setdefault("dates", {}).setdefault(kind, value)
        elif name == "frbrauthor":
            href = (elem.get("href") or "").strip()
            if href.startswith("#organization_") and "author" not in meta:
                meta["author"] = href.removeprefix("#organization_")
        elif name == "frbrlanguage":
            meta.setdefault("language", (elem.get("language") or "").strip())
        elif name == "frbrsubtype":
            meta.setdefault("subtype", (elem.get("value") or "").strip())
        elif name == "frbrnumber":
            meta.setdefault("number", (elem.get("value") or "").strip())
        elif name == "frbruri" and "work_uri" not in meta:
            value = (elem.get("value") or "").strip()
            # The FIRST FRBRuri is the Work's; the Expression and Manifestation ones
            # follow. Which is which matters: the Work URI is the identity that both
            # language versions of one judgment share.
            if value:
                meta["work_uri"] = value
    return meta


def finlex_metadata(data: bytes) -> dict:
    """Every identifier and classification a Finlex AKN document declares.

    Split out of ``parse_finlex_akn`` because Finland's **acts** carry the same ``meta``
    block (ELI alias, dateIssued, datePublished, author organisation) while their body is
    ordinary legislation the shared Akoma Ntoso parser already handles well. Reading the
    metadata twice from two parsers would be two places to keep in step.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {}
    return _metadata(root)


def _metadata(root: ET.Element) -> dict:
    meta = _frbr(root)
    keywords = [(e.get("showAs") or e.get("value") or "").strip()
                for e in root.iter() if localname(e.tag).lower() == "keyword"]
    concepts = [(e.get("href") or "").strip()
                for e in root.iter() if localname(e.tag).lower() == "tlcconcept"]
    legal_basis = [(e.get("refersTo") or "").strip().lstrip("#")
                   for e in root.iter() if localname(e.tag).lower() == "legalbasis"]
    # The document's own organisation registry. ``FRBRauthor`` points at an eId and the
    # matching ``TLCOrganization`` states its display name — "Itä-Suomen hovioikeus" for
    # ``fi.court-of-appeal-eastern-finland``. Reading it here means the corpus never needs
    # a hand-kept table of Finnish court names, and a court Finlex adds tomorrow arrives
    # with its name already correct.
    organisations = {(e.get("eId") or "").removeprefix("organization_"):
                     (e.get("showAs") or "").strip()
                     for e in root.iter() if localname(e.tag).lower() == "tlcorganization"}

    title = None
    doc_number = None
    for elem in root.iter():
        name = localname(elem.tag).lower()
        if name == "doctitle" and not title:
            title = _text_of(elem)
        elif name == "docnumber" and not doc_number:
            doc_number = _text_of(elem)
    return {k: v for k, v in {
        "ecli": meta.get("aliases", {}).get("ecli"),
        "eli": meta.get("aliases", {}).get("eli"),
        "diary_number": meta.get("aliases", {}).get("diaryNumber"),
        "case_number": meta.get("aliases", {}).get("caseNumber"),
        "decision_number": meta.get("aliases", {}).get("decisionNumber"),
        "archival_record": meta.get("aliases", {}).get("archivalRecord"),
        "doc_number": doc_number,
        "doc_title": title,
        "author": meta.get("author"),
        "author_name": organisations.get(meta.get("author") or ""),
        "organisations": {k: v for k, v in organisations.items() if k and v} or None,
        "language": meta.get("language"),
        "subtype": meta.get("subtype"),
        "number": meta.get("number"),
        "work_uri": meta.get("work_uri"),
        "dates": meta.get("dates"),
        "keywords": [k for k in keywords if k] or None,
        "concepts": [c for c in concepts if c] or None,
        "legal_basis": [b for b in legal_basis if b] or None,
    }.items() if v not in (None, "", [], {})}


def parse_finlex_akn(data: bytes) -> ParsedDoc:
    """A Finlex judgment AKN document → flat text, outline segments, and its metadata."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc(text=None)

    metadata = _metadata(root)
    title = metadata.get("doc_title")
    doc_number = metadata.get("doc_number")

    blocks: list[tuple[str, str, str, int]] = []
    zones: list[str] = []
    # A judgment's body is ``judgmentBody`` with named zones; a government proposal is an
    # AKN ``<doc>`` whose body is ``mainBody`` with no zones at all, and its ``preface``
    # carries the title block. Both are walked, because a proposal that fell through here
    # was stored from its PDF instead — same words, no outline, and the reader lost the
    # only structure the source published.
    for body in root.iter():
        name = localname(body.tag).lower()
        if name == "judgmentbody":
            for zone in body:
                zone_name = localname(zone.tag).lower()
                heading = _ZONES.get(zone_name)
                if heading:
                    zones.append(zone_name)
                    blocks.append((heading, "heading", heading, 0))
                _walk(zone, 1 if heading else 0, blocks, heading or "Ratkaisu")
        elif name == "mainbody" and not blocks:
            # ``preface`` is deliberately NOT walked: it is the title block, already read
            # into ``doc_title``/``doc_number``. Walking it made a proposal whose mainBody
            # is empty look like a document with 86 characters of text, which is worse
            # than reporting no text — the adapter would have stored the title and never
            # gone to main.pdf for the 150,000 characters that are the actual proposal.
            _walk(body, 0, blocks, title or doc_number or "")

    text, segments = assemble(blocks)
    if zones:
        metadata["zones"] = zones
    if not text:
        # An AKN carrying metadata and no body is a wrapper over ``main.pdf``. Saying so
        # is what lets the adapter go and get the PDF rather than store an empty record.
        metadata["pdf_only"] = True
    return ParsedDoc(text=text or None, segments=segments, title=title or doc_number,
                     metadata=metadata)


register("finlex-akn", parse_finlex_akn)
