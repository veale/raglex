"""Légifrance JSON parser (DILA's ``lf-engine-app`` responses → text + segments).

Légifrance serves consolidated French legislation as JSON, not markup: a *code* or
*text* (``/consult/legiPart``, ``/consult/code``) comes back as a tree of **sections**
each holding **articles**, and a single article (``/consult/getArticle``) as one
article object with its HTML ``content`` and a full **version history**
(``articleVersions`` with ``dateDebut``/``dateFin``/``etat``).

This module is the "one markup family → text + segments" parser for that JSON: each
article becomes a native chunk unit (``Segment`` labelled "Article L1234-5"), exactly
as Formex ``NP.ECR`` paragraphs and AKN sections do (§6b). The ELI, CID and version
metadata are lifted so ``fr_legislation`` can key by ELI and map each article version
onto ``document_versions`` (point-in-time — "what did Art. 1382 Code civil say in
1992?").

Parsing is pure (dict in, structures out) so it is testable against a recorded JSON
response with no network. The field names below follow the documented DILA shapes but
are read defensively (several aliases each) because the service is still evolving.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime

from ..core.models import Segment
from ..core.segmentation import assemble
from .base import ParsedDoc, register


def strip_html(html: str | None) -> str:
    """Article bodies are HTML fragments; reduce to readable flat text."""
    if not html:
        return ""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    # <br> and block elements become line breaks so enumerations read as a list
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text("\n")
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def _epoch_ms_to_date(value) -> date | None:
    """Légifrance dates are epoch-milliseconds ints *or* ISO strings depending on the
    endpoint — accept both."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(value / 1000).date()
        except (ValueError, OverflowError, OSError):
            return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _first(obj: dict, *keys):
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", []):
            return v
    return None


@dataclass(frozen=True, slots=True)
class ArticleVersion:
    """One version of an article from ``articleVersions`` — the point-in-time series."""
    version_id: str | None
    etat: str | None            # VIGUEUR | ABROGE | MODIFIE …
    date_debut: date | None
    date_fin: date | None


@dataclass(slots=True)
class LegifranceDoc:
    """Structured view of a legiPart / getArticle response, before it becomes a Record."""
    title: str | None = None
    cid: str | None = None            # the stable CID (…C…) — a surrogate id where ELI is absent
    eli: str | None = None
    etat: str | None = None
    date_debut: date | None = None
    date_fin: date | None = None
    text: str | None = None
    segments: list[Segment] = field(default_factory=list)
    versions: list[ArticleVersion] = field(default_factory=list)
    nature: str | None = None         # CODE | LOI | DECRET | DELIBERATION …
    num: str | None = None            # article number for a single-article fetch


def _article_label(art: dict) -> str:
    num = _first(art, "num", "numeroArticle")
    return f"Article {num}" if num else (_first(art, "id", "cid") or "article")


def _collect_articles(node: dict, out: list[tuple[str, str, str]]) -> None:
    """Walk a section tree in document order, emitting (label, kind, text) per article."""
    for art in node.get("articles") or []:
        body = strip_html(_first(art, "content", "texteHtml", "texte"))
        if body:
            out.append((_article_label(art), "article", body))
    for sub in node.get("sections") or []:
        title = _first(sub, "title", "intitule")
        if title:
            out.append((str(title), "section", str(title)))
        _collect_articles(sub, out)


def _article_versions(art: dict) -> list[ArticleVersion]:
    versions: list[ArticleVersion] = []
    for v in art.get("articleVersions") or []:
        versions.append(ArticleVersion(
            version_id=_first(v, "id", "versionId"),
            etat=_first(v, "etat", "etatJuridique"),
            date_debut=_epoch_ms_to_date(_first(v, "dateDebut", "debut")),
            date_fin=_epoch_ms_to_date(_first(v, "dateFin", "fin")),
        ))
    return versions


def parse_legifrance_obj(obj: dict) -> LegifranceDoc:
    """A parsed Légifrance JSON response → a :class:`LegifranceDoc` (pure).

    Handles both a whole text/code (``legiPart``/``consult`` with ``sections``) and a
    single article (``getArticle`` with a top-level ``article``)."""
    # a getArticle response nests the payload under "article"
    art = obj.get("article") if isinstance(obj.get("article"), dict) else None
    root = art or obj

    doc = LegifranceDoc(
        title=_first(root, "title", "titre", "titreLong", "nom"),
        cid=_first(root, "cid", "textCid", "cidTexte"),
        eli=_first(root, "eli", "eliText", "eliId"),
        etat=_first(root, "etat", "etatJuridique"),
        date_debut=_epoch_ms_to_date(_first(root, "dateDebut", "dateDebutVersion", "debut")),
        date_fin=_epoch_ms_to_date(_first(root, "dateFin", "dateFinVersion", "fin")),
        nature=_first(root, "nature", "natureText"),
        num=_first(root, "num", "numeroArticle"),
    )

    if art is not None:
        body = strip_html(_first(art, "content", "texteHtml", "texte"))
        blocks = [(_article_label(art), "article", body)] if body else []
        doc.versions = _article_versions(art)
    else:
        blocks: list[tuple[str, str, str]] = []
        # a legiPart carries its articles inside a section tree; some responses put the
        # tree under "sections", others wrap it in a single root "section".
        _collect_articles(root, blocks)
        if not blocks and isinstance(root.get("section"), dict):
            _collect_articles(root["section"], blocks)

    text, segments = assemble(blocks)
    doc.text = text or None
    doc.segments = segments
    return doc


def parse_legifrance(data: bytes) -> ParsedDoc:
    """Format-registry entry point (bytes JSON → ParsedDoc)."""
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ParsedDoc()
    if not isinstance(obj, dict):
        return ParsedDoc()
    doc = parse_legifrance_obj(obj)
    return ParsedDoc(
        text=doc.text,
        segments=doc.segments,
        title=doc.title,
        decision_date=doc.date_debut,
        metadata={"cid": doc.cid, "eli": doc.eli, "etat": doc.etat, "nature": doc.nature},
    )


register("legifrance-json", parse_legifrance)
