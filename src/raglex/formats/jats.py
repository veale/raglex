"""JATS — the markup the Parliament's research service publishes its own studies in.

An EPRS briefing or a policy department study is served three ways: a PDF, an HTML
page, and ``…_EN.xml``, which is `JATS <https://jats.nlm.nih.gov/>`_ — the journal
article standard, the same one PubMed Central uses. That is a considerable piece of
luck for a corpus of law commentary, because it means these documents arrive with
their own section hierarchy, their abstracts, and — the part that matters most — their
**endnotes**, which is where a briefing does its citing.

Two decisions worth stating.

*Sections nest, and the nesting is kept.* "Key WHO challenges › Funding › Financing
gap" is three levels in the source and three levels in the segments, so a reader can
pincite the sub-section a claim actually sits in rather than the chapter around it.

*A link to a legal source is part of the citation, not decoration.* These documents
cite in a modern web style — the footnote reads "Executive order 14155 on withdrawing
the United States from the world", and the identifier lives only in the ``xlink:href``.
Dropping the href would throw away a reference to Regulation 2016/679 or to
``TA-10-2025-0159`` that the grammars would otherwise resolve. So the href is appended
in parentheses for links to legal sources — EUR-Lex, CURIA, ELI, the Parliament's own
documents — and left out for the hundreds of ordinary web references, which would
otherwise bury the text in URLs.
"""

from __future__ import annotations

import re
from datetime import date
from xml.etree import ElementTree as ET

from ..core.segmentation import assemble, localname
from .base import ParsedDoc, register

#: Hosts whose URLs carry a resolvable legal identifier. Everything else is left as the
#: prose the author wrote.
_LEGAL_HREF = re.compile(
    r"(?:eur-lex\.europa\.eu|curia\.europa\.eu|data\.europa\.eu/eli|"
    r"europarl\.europa\.eu/(?:doceo|thinktank|RegData)|"
    r"echr\.coe\.int|hudoc\.echr\.coe\.int|legislation\.gov\.uk)", re.I)

#: Presentation and apparatus that is not the document's prose.
_DROP = {"graphic", "inline-graphic", "media", "alt-text", "xref", "label",
         "processing-meta", "permissions", "funding-group"}
#: Block elements that start a new line inside a section's flow.
_BLOCK = {"p", "list-item", "title", "caption", "td", "th", "disp-quote", "license-p"}


def _href(node: ET.Element) -> str | None:
    for key, value in node.attrib.items():
        if localname(key) == "href":
            return value
    return None


def _flow(elem: ET.Element, *, skip_title: bool = False, depth: int = 0) -> str:
    """The readable text of one unit, block elements on their own lines.

    ``skip_title`` drops the section's own heading, which is the segment label and would
    otherwise open the body with a repeat of it. Nested ``<sec>`` are NOT walked: each
    becomes its own segment, so including them here would store the text twice.
    """
    parts: list[str] = []

    def emit(text: str) -> None:
        if text and text.strip():
            parts.append(text.strip())

    def walk(node: ET.Element, top: bool) -> None:
        name = localname(node.tag).lower()
        if name in _DROP or (name == "sec" and not top):
            emit(node.tail or "")
            return
        if top and skip_title and name == "title":
            emit(node.tail or "")
            return
        if name in _BLOCK:
            parts.append("\n")
        emit(node.text or "")
        for child in node:
            walk(child, False)
        if name == "ext-link":
            target = _href(node) or ""
            if target and _LEGAL_HREF.search(target):
                emit(f"({target})")
        if name in _BLOCK:
            parts.append("\n")
        emit(node.tail or "")

    emit(elem.text or "")
    for child in elem:
        walk(child, True)
    text = " ".join(parts)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Joining on spaces puts one before the punctuation that closed an inline element —
    # "chronically insufficient funding ." — which is in almost every sentence these
    # documents write, because they link so heavily. Tabs and spaces only: a newline
    # before punctuation is a list marker, not a slip.
    return re.sub(r"[ \t]+([,.;:!?)\]])", r"\1", text).strip()


def _title_of(sec: ET.Element) -> str:
    node = next((c for c in sec if localname(c.tag) == "title"), None)
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _sections(parent: ET.Element, depth: int = 0) -> list[tuple[str, str, str, int]]:
    """Every ``<sec>``, in document order, carrying its nesting depth as the outline
    level. An untitled section takes a positional label so it is still addressable."""
    out: list[tuple[str, str, str, int]] = []
    position = 0
    for sec in parent:
        if localname(sec.tag) != "sec":
            continue
        position += 1
        label = _title_of(sec) or f"Section {position}"
        body = _flow(sec, skip_title=True, depth=depth)
        if body:
            out.append((label[:160], "section", body, depth))
        out += _sections(sec, depth + 1)
    return out


def _abstract_blocks(meta: ET.Element) -> list[tuple[str, str, str, int]]:
    """The document's own summary. EPRS writes several — ``summary`` is the full one,
    ``snippet`` and ``promotional_text`` are trailers for it, and ``author`` is a byline
    rather than an abstract at all. Only the substantive one becomes a segment; the rest
    are metadata."""
    out: list[tuple[str, str, str, int]] = []
    for node in meta:
        if localname(node.tag) != "abstract":
            continue
        kind = (node.get("abstract-type") or "").lower()
        if kind not in ("", "summary", "executive_summary"):
            continue
        body = _flow(node, skip_title=True)
        # the inner <sec><title>Summary</title> repeats the label
        body = re.sub(r"^(?:Summary|Executive summary)\s*\n?", "", body).strip()
        if body:
            out.append(("Summary", "abstract", body, 0))
    return out


def _footnote_blocks(root: ET.Element) -> list[tuple[str, str, str, int]]:
    """Endnotes, as one segment. This is where a briefing puts its authorities, so it
    is kept whole and adjacent — splitting one note per segment would scatter a single
    citation trail across fifty tiny units."""
    notes: list[str] = []
    for group in root.iter():
        if localname(group.tag) != "fn":
            continue
        label = next((c for c in group if localname(c.tag) == "label"), None)
        marker = " ".join("".join(label.itertext()).split()) if label is not None else ""
        body = _flow(group)
        if body:
            notes.append(f"{marker} {body}".strip())
    return [("Endnotes", "note", "\n".join(notes), 0)] if notes else []


def _text_of(parent: ET.Element, *names: str) -> str | None:
    for name in names:
        node = next((e for e in parent.iter() if localname(e.tag) == name), None)
        if node is not None:
            value = " ".join("".join(node.itertext()).split())
            if value:
                return value
    return None


def _pub_date(meta: ET.Element) -> date | None:
    node = next((e for e in meta.iter() if localname(e.tag) == "pub-date"), None)
    if node is None:
        return None
    part = {localname(c.tag): (c.text or "").strip() for c in node}
    try:
        return date(int(part["year"]), int(part["month"]), int(part["day"]))
    except (KeyError, TypeError, ValueError):
        try:
            return date(int(part["year"]), 12, 31)
        except (KeyError, TypeError, ValueError):
            return None


def _keywords(root: ET.Element) -> dict[str, list[str]]:
    """``kwd-group`` by type. The EuroVoc group is a literal "placeholder" in every
    document sampled — the real subject terms are on the web page, and the adapter reads
    them there — so a placeholder group is dropped rather than stored as a subject."""
    out: dict[str, list[str]] = {}
    for group in root.iter():
        if localname(group.tag) != "kwd-group":
            continue
        kind = group.get("kwd-group-type") or "keywords"
        values = [" ".join("".join(k.itertext()).split()) for k in group
                  if localname(k.tag) == "kwd"]
        values = [v for v in values if v and v.lower() != "placeholder"]
        if values:
            out.setdefault(kind, []).extend(values)
    return out


def parse_jats_article(raw: bytes) -> ParsedDoc:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return ParsedDoc()
    if localname(root.tag) != "article":
        found = next((e for e in root.iter() if localname(e.tag) == "article"), None)
        if found is None:
            return ParsedDoc()
        root = found

    meta = next((e for e in root.iter() if localname(e.tag) == "article-meta"), None)
    body = next((e for e in root if localname(e.tag) == "body"), None)
    back = next((e for e in root if localname(e.tag) == "back"), None)

    blocks: list[tuple[str, str, str, int]] = []
    if meta is not None:
        blocks += _abstract_blocks(meta)
    if body is not None:
        blocks += _sections(body)
        # a document with no <sec> at all is still a document
        if not any(b[1] == "section" for b in blocks):
            whole = _flow(body)
            if whole:
                blocks.append(("Text", "section", whole, 0))
    for container in (back, root):
        notes = _footnote_blocks(container) if container is not None else []
        if notes:
            blocks += notes
            break

    if not blocks:
        return ParsedDoc()

    metadata: dict = {"keywords": _keywords(root)}
    if meta is not None:
        # "PE: 789.356" — the Parliament's own document number, and how a briefing is
        # cited internally.
        pe = _text_of(meta, "article-id")
        if pe:
            metadata["pe_number"] = pe.replace("PE:", "PE").strip()
        category = next(
            (e for e in meta.iter()
             if localname(e.tag) == "subj-group"
             and e.get("subj-group-type") == "document_category"), None)
        if category is not None:
            value = _text_of(category, "subject")
            if value:
                metadata["publication_type"] = value
        licence = _text_of(meta, "copyright-statement")
        if licence:
            metadata["licence"] = licence

    text, segments = assemble(blocks)
    return ParsedDoc(
        text=text or None, segments=segments,
        title=_text_of(meta, "article-title") if meta is not None else None,
        decision_date=_pub_date(meta) if meta is not None else None,
        metadata=metadata)


register("jats-article", parse_jats_article)
