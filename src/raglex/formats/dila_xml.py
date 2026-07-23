"""DILA OPENDATA XML parsers — the French bulk formats (Légifrance/Judilibre corpora).

The ``echanges.dila.gouv.fr/OPENDATA`` archives are raw XML against DILA's DTDs. Two
shapes matter for RagLex, both handled here (read defensively by local-name because the
funds share generic DTDs with per-fund variations):

- **Jurisprudence** (CASS, CAPP, JADE, CONSTIT, CNIL) — a ``<TEXTE_JURI_*>`` document
  per decision: ``<META_JURI>`` carries the ECLI, title, date, jurisdiction, number and
  solution; ``<BLOC_TEXTUEL><CONTENU>`` the decision text; ``<LIEN>`` the citations.
- **LEGI article** — an ``<ARTICLE>`` per consolidated article: ``<META_ARTICLE>`` the
  number, ``<ETAT>`` and the ``<DATE_DEBUT>``/``<DATE_FIN>`` version window (point-in-
  time), ``<CONTEXTE>`` the code it sits in, ``<BLOC_TEXTUEL><CONTENU>`` the text.

These IDs (ECLI, Légifrance CID/LEGIARTI) match what the live PISTE adapters and the
citation extractor mint, so a bulk seed resolves the pending citations the corpus holds.
Verify against a real archive before a backfill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from xml.etree import ElementTree as ET

from ..core.models import (
    ExtractedVia,
    RelationshipType,
    ResolutionStatus,
    Segment,
    TypedRelation,
)
from ..core.segmentation import assemble, localname
from .base import ParsedDoc, register


def _find(root: ET.Element, tag: str) -> ET.Element | None:
    return next((e for e in root.iter() if localname(e.tag).upper() == tag.upper()), None)


def _text(root: ET.Element, tag: str) -> str | None:
    el = _find(root, tag)
    if el is None:
        return None
    return " ".join("".join(el.itertext()).split()) or None


def _iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _render_br(el: ET.Element) -> str:
    """Serialise a ``<CONTENU>`` block preserving its ``<br/>`` structure as line breaks.
    JADE decision bodies are HTML-in-XML whose only paragraphing is ``<br/>`` — a double
    ``<br/><br/>`` between paragraphs, a single one for a line break. ``itertext()`` drops
    them, which is what collapsed the whole decision into one blob; here each ``<br/>``
    becomes a newline, then a blank line (double-br) reads as a paragraph break."""
    buf: list[str] = []

    def walk(e: ET.Element) -> None:
        if e.text:
            buf.append(e.text)
        for ch in e:
            if localname(ch.tag).lower() == "br":
                buf.append("\n")
            else:
                walk(ch)
            if ch.tail:
                buf.append(ch.tail)

    walk(el)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(buf).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _contenu_text(root: ET.Element) -> str:
    """Every ``<CONTENU>`` block → readable text with its paragraph breaks intact."""
    parts = [_render_br(c) for c in root.iter() if localname(c.tag).upper() == "CONTENU"]
    return "\n\n".join(p for p in parts if p)


@dataclass(slots=True)
class DilaJuri:
    doc_id: str | None = None
    ecli: str | None = None
    title: str | None = None
    date: date | None = None
    jurisdiction: str | None = None
    number: str | None = None
    solution: str | None = None
    formation: str | None = None
    nature: str | None = None
    text: str | None = None
    relations: list[TypedRelation] = field(default_factory=list)


# Légifrance ids (article- or text-level) and ECLIs are resolvable destinations; empty
# strings and bare numbers are not.
_LEGIFRANCE_ID = re.compile(r"^(?:LEGI(?:ARTI|TEXT)|JORF(?:ARTI|TEXT)|CETATEXT|CONSTEXT|CNILTEXT)\d+$")


def _lien_dst(lien: ET.Element) -> str | None:
    """The resolvable destination of a ``<LIEN>``: its most specific Légifrance id
    (the article-level ``id`` over the text-level ``cidtexte``) or an ECLI."""
    for attr in ("id", "cidtexte", "ecli"):
        val = (lien.get(attr) or "").strip()
        if val.startswith("ECLI:") or _LEGIFRANCE_ID.match(val):
            return val
    return None


def _juri_relations(root: ET.Element) -> list[TypedRelation]:
    """``<LIEN>`` citation links → typed edges. ``sens="source"`` means the decision
    cites the target; the Légifrance id (when present) is a resolvable destination
    against fr-legislation, otherwise the edge dangles on the citation text (§5b). Deduped."""
    rels: list[TypedRelation] = []
    seen: set[str] = set()
    for lien in root.iter():
        if localname(lien.tag).upper() != "LIEN":
            continue
        label = " ".join("".join(lien.itertext()).split())
        dst = _lien_dst(lien)
        if not (label or dst):
            continue
        key = dst or label
        if key in seen:
            continue
        seen.add(key)
        rels.append(TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string=label or dst,
            dst_id=dst,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))
    return rels


def parse_dila_juri(root: ET.Element) -> DilaJuri:
    # CNIL deliberations (<META_CNIL>) date on DATE_TEXTE and have no ECLI/jurisdiction;
    # the judicial/administrative funds (<META_JURI>) use DATE_DEC.
    return DilaJuri(
        doc_id=_text(root, "ID"),
        ecli=_text(root, "ECLI"),
        title=_text(root, "TITRE") or _text(root, "TITREFULL"),
        date=_iso(_text(root, "DATE_DEC") or _text(root, "DATE_TEXTE")),
        jurisdiction=_text(root, "JURIDICTION"),
        number=_text(root, "NUMERO") or _text(root, "NUMERO_AFFAIRE"),
        solution=_text(root, "SOLUTION"),
        formation=_text(root, "FORMATION"),
        nature=_text(root, "NATURE_DELIB") or _text(root, "NATURE"),
        text=_contenu_text(root) or None,
        relations=_juri_relations(root),
    )


@dataclass(slots=True)
class DilaArticle:
    art_id: str | None = None
    num: str | None = None
    etat: str | None = None
    date_debut: date | None = None
    date_fin: date | None = None
    code_cid: str | None = None
    code_title: str | None = None
    text: str | None = None
    segments: list[Segment] = field(default_factory=list)


def _code_context(root: ET.Element) -> tuple[str | None, str | None]:
    """(code_cid, code_title) from an article's ``<CONTEXTE>``. The title lives in a
    ``<TITRE_TXT>`` (with a ``c_titre_court`` short form and an ``id_txt`` that is the
    LEGITEXT id for codified articles) — NOT in a ``@titre`` attribute."""
    ctx = _find(root, "CONTEXTE")
    if ctx is None:
        return None, None
    texte = _find(ctx, "TEXTE")
    cid = texte.get("cid") if texte is not None else None
    title = None
    for tt in ctx.iter():
        if localname(tt.tag).upper() != "TITRE_TXT":
            continue
        # prefer the consolidated LEGITEXT identity where the article is codified
        if (tt.get("id_txt") or "").startswith("LEGITEXT"):
            cid = tt.get("id_txt")
        title = tt.get("c_titre_court") or " ".join("".join(tt.itertext()).split()) or title
        if (tt.get("id_txt") or "").startswith("LEGITEXT"):
            break
    return cid, title


def parse_dila_article(root: ET.Element) -> DilaArticle:
    num = _text(root, "NUM")
    body = _contenu_text(root)
    label = f"Article {num}" if num else (_text(root, "ID") or "article")
    text, segments = assemble([(label, "article", body)] if body else [])
    code_cid, code_title = _code_context(root)
    return DilaArticle(
        art_id=_text(root, "ID"),
        num=num,
        etat=_text(root, "ETAT"),
        date_debut=_iso(_text(root, "DATE_DEBUT")),
        date_fin=_iso(_text(root, "DATE_FIN")),
        code_cid=code_cid,
        code_title=code_title,
        text=text or None,
        segments=segments,
    )


def dila_root_kind(root: ET.Element) -> str:
    """'juri' | 'article' | 'unknown' from the document's root/shape. 'juri' covers the
    decision funds: the judicial/administrative ``TEXTE_JURI_*`` (<META_JURI>) and the
    CNIL deliberations ``TEXTE_CNIL`` (<META_CNIL>)."""
    name = localname(root.tag).upper()
    if name == "ARTICLE" or _find(root, "META_ARTICLE") is not None:
        return "article"
    if (name.startswith("TEXTE_JURI") or name == "TEXTE_CNIL"
            or _find(root, "META_JURI") is not None or _find(root, "META_CNIL") is not None):
        return "juri"
    return "unknown"


def parse_dila(data: bytes) -> ParsedDoc:
    """Format-registry entry point — dispatches on the document shape."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc()
    kind = dila_root_kind(root)
    if kind == "article":
        art = parse_dila_article(root)
        return ParsedDoc(text=art.text, segments=art.segments,
                         title=(f"Article {art.num}" if art.num else None),
                         decision_date=art.date_debut,
                         metadata={"etat": art.etat, "code_cid": art.code_cid})
    if kind == "juri":
        j = parse_dila_juri(root)
        text, segments = assemble([("decision", "section", j.text)] if j.text else [])
        return ParsedDoc(text=text or None, segments=segments, relations=j.relations,
                         title=j.title, decision_date=j.date,
                         metadata={"ecli": j.ecli, "jurisdiction": j.jurisdiction})
    return ParsedDoc()


register("dila-xml", parse_dila)
