"""Formex 4 parser for EU legislation (the Publications Office's native markup).

Why Formex and not AKN4EU: AKN4EU exists in the EU's drafting pipeline, but CELLAR
does not serve it by content negotiation (those requests 400/404). Formex is what
CELLAR reliably returns, and for an act its content member (root ``<ACT>``, *not*
the ``.doc.xml`` bibliographic notice) carries the full ``<ARTICLE>`` structure —
99 articles for the GDPR. We segment per article (``<TI.ART>`` "Article N" +
``<STI.ART>`` heading), the citable unit, so the act becomes a structured,
resolvable node (§5b) with a nicely-renderable hierarchy.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from ..core.segmentation import assemble, element_text, flow_text, localname
from .base import ParsedDoc, register


def unzip_formex_content(raw: bytes) -> bytes | None:
    """Unpack a CELLAR Formex zip and return the *content* member (root ``ACT`` /
    largest), skipping the ``.doc.xml`` bibliographic notice."""
    # any XML-looking payload passes through: a stored instance need not carry the
    # declaration (a judgment whose first bytes are "<JUDGMENT" is still Formex)
    if raw.lstrip()[:1] == b"<":
        return raw
    if raw[:2] != b"PK":
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith((".xml", ".fmx"))]
            # prefer non-notice members, then the largest (the enacting terms)
            content = [n for n in members if ".doc." not in n.lower()] or members
            if not content:
                return None
            best = max(content, key=lambda n: zf.getinfo(n).file_size)
            return zf.read(best)
    except zipfile.BadZipFile:
        return None


def unzip_formex_contents(raw: bytes) -> list[bytes]:
    """Every content member in publication order.

    CELLAR splits long OJ acts into files. For the UCPD, the largest member ends at
    Article 21 while Annex I lives in a second member; selecting only the largest file
    silently loses the annex. Bibliographic ``.doc.xml`` notices are excluded.
    """
    if raw.lstrip()[:1] == b"<":
        return [raw]
    if raw[:2] != b"PK":
        return []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = sorted(
                n for n in zf.namelist()
                if n.lower().endswith((".xml", ".fmx"))
                and ".doc." not in n.lower()
            )
            return [zf.read(name) for name in names]
    except zipfile.BadZipFile:
        return []


# Title children (the article's own num + heading) — its label, dropped from the
# body; numbered paragraphs and list points start new lines (read as a list).
_FMX_SKIP = {"ti.art", "sti.art"}
_FMX_LINES = {"parag", "item"}


def _label(article: ET.Element) -> str:
    ti = next((c for c in article.iter() if localname(c.tag) == "TI.ART"), None)
    sti = next((c for c in article.iter() if localname(c.tag) == "STI.ART"), None)
    num = " ".join(element_text(ti).split()) if ti is not None else "Article"
    heading = " ".join(element_text(sti).split()) if sti is not None else ""
    return f"{num} {heading}".strip()


def _recital_blocks(root: ET.Element) -> list[tuple[str, str, str]]:
    """The preamble's recitals (Formex ``CONSID``: ``(N) Whereas …``) — the
    interpretive backbone of an EU instrument, and previously dropped entirely.
    Each becomes its own ``recital`` segment, labelled by its number."""
    blocks: list[tuple[str, str, str]] = []
    for consid in (e for e in root.iter() if localname(e.tag) == "CONSID"):
        no = next((c for c in consid.iter() if localname(c.tag) == "NO.P"), None)
        num = " ".join(element_text(no).split()).strip("()") if no is not None else ""
        label = f"Recital {num}" if num else "Recital"
        body = flow_text(consid, line_tags={"item"})
        body = re.sub(r"^\(\d+\)\s*", "", body)  # drop the leading marker (it's the label)
        if body:
            blocks.append((label, "recital", body))
    return blocks


def _annex_blocks(root: ET.Element) -> list[tuple[str, str, str]]:
    """Annexes are siblings of the enacting terms, not ``ARTICLE`` descendants.

    The old parser assembled only recitals + articles, silently dropping the UCPD's
    Annex I blacklist (and every other schedule). Preserve each annex as a citable
    segment, including table/list contents.
    """
    blocks: list[tuple[str, str, str]] = []
    # Enacted Formex uses ANNEX; dated sector-0 texts use CONS.ANNEX. Treat both
    # as the same citable structure (the UCPD consolidation otherwise had 21
    # articles and silently stopped before both annexes).
    annexes = [
        e for e in root.iter()
        if localname(e.tag) == "ANNEX" or localname(e.tag).endswith(".ANNEX")
    ]
    for index, annex in enumerate(annexes, 1):
        title_node = next(
            (e for e in annex.iter() if localname(e.tag) in {"TITLE", "TI.ANNEX"}),
            None,
        )
        title = " ".join(element_text(title_node).split()) if title_node is not None else ""
        label = title or (f"Annex {index}" if len(annexes) > 1 else "Annex")
        body = flow_text(
            annex, skip_tags={"title", "ti.annex"},
            line_tags={"parag", "item", "row"},
        )
        if body:
            blocks.append((label, "annex", body))
    return blocks


def _is_case_law(root: ET.Element) -> bool:
    """A judgment/opinion instance rather than an act: it wraps its reasoning in
    ``CONTENTS.JUDGMENT`` (or its root says so) and has no ``ENACTING.TERMS``."""
    if any(localname(e.tag) == "ENACTING.TERMS" for e in root.iter()):
        return False
    if localname(root.tag).upper() in {"JUDGMENT", "OPINION", "ORDER", "VIEW"}:
        return True
    return any(localname(e.tag) in ("CONTENTS.JUDGMENT", "JURISDICTION", "NP.ECR")
               for e in root.iter())


def parse_formex_legislation(raw: bytes) -> ParsedDoc:
    members = unzip_formex_contents(raw)
    if not members:
        return ParsedDoc()
    roots: list[ET.Element] = []
    for data in members:
        try:
            roots.append(ET.fromstring(data))
        except ET.ParseError:
            continue
    if not roots:
        return ParsedDoc()
    if len(roots) == 1:
        root = roots[0]
        data = members[0]
    else:
        root = ET.Element("ACT")
        for member_root in roots:
            root.append(member_root)
        data = ET.tostring(root, encoding="utf-8")

    # CJEU CASE LAW is Formex too, and this parser is registered for every Formex
    # instance. Run against a judgment it reads only what it recognises — the recitals
    # the judgment QUOTES — and throws the reasoning away: a re-parse cut Dun &
    # Bradstreet (C-203/22) from 57,012 characters to 3,822, six "recital" segments and
    # no judgment at all. A judgment goes to the case-law reader, which is the same
    # function the CELLAR adapter uses at harvest, so both paths produce the same text.
    if _is_case_law(root):
        from ..adapters.eu_cellar import extract_formex

        text, segments = extract_formex(data)
        title = None
        ti = next((e for e in root.iter() if localname(e.tag) == "TITLE"), None)
        if ti is not None:
            title = " ".join(element_text(ti).split()) or None
        return ParsedDoc(text=text or None, segments=segments, title=title)

    title = None
    ti = next((e for e in root.iter() if localname(e.tag) == "TITLE"), None)
    if ti is not None:
        title = " ".join(element_text(ti).split()) or None

    articles = [e for e in root.iter() if localname(e.tag) == "ARTICLE"]
    # recitals first (preamble), then the enacting articles — in document order
    blocks: list[tuple[str, str, str]] = _recital_blocks(root)
    blocks += [(_label(a), "article", flow_text(a, skip_tags=_FMX_SKIP, line_tags=_FMX_LINES))
               for a in articles]
    blocks += _annex_blocks(root)
    if not blocks:  # not an act we recognise — whole-document text
        blocks = [(title or "document", "section", element_text(root))]

    text, segments = assemble(blocks)
    return ParsedDoc(text=text or None, segments=segments, title=title)


register("formex-legislation", parse_formex_legislation)
