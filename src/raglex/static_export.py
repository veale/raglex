"""Self-contained, static editions of a held law and the documents mentioning it.

The output deliberately has no dependency on the RagLex API.  The selected law, its
provision index, citing-document metadata and short citation-context excerpts are embedded
in one HTML file.  It therefore works from GitHub Pages and when opened directly from disk.
"""

from __future__ import annotations

import base64
import html
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urldefrag, urljoin, urlsplit

from .config import Config
from .core.segmentation import synthesise_numbered_segments
from .facade import Facade, _anchor_key, _oscola_cite, _row_meta


_PDF_META_KEYS = ("pdf_url", "download_url", "bailii_pdf_url")
_SOURCE_META_KEYS = ("url", "bailii_url", "gdprhub_url")
_CACHE_VERSION = 3

_DEFAULT_ATTRIBUTION = (
    'Document generated from a dataset held and maintained by '
    '<a href="https://profiles.ucl.ac.uk/53958-michael-veale">Michael Veale</a>, '
    'Professor of Technology Law and Policy, UCL Faculty of Laws. If you wish to study '
    'these instruments, consider our UCL Laws '
    '<a href="https://www.ucl.ac.uk/laws/study/master-laws-llm-courses/'
    'llm-law-and-technology">LLM in Law and Technology</a>.'
)

_FLAG_CODE = {
    "european union": "eu", "united kingdom": "gb", "uk": "gb", "gb": "gb",
    "eu": "eu", "council of europe": "eu", "ireland": "ie",
    "data protection commission (ireland)": "ie", "germany": "de", "france": "fr",
    "netherlands": "nl", "italy": "it", "spain": "es", "belgium": "be",
    "austria": "at", "poland": "pl", "greece": "gr", "romania": "ro",
    "hungary": "hu", "sweden": "se", "denmark": "dk", "finland": "fi",
    "norway": "no", "iceland": "is", "portugal": "pt", "czechia": "cz",
    "czech republic": "cz", "slovakia": "sk", "slovenia": "si", "croatia": "hr",
    "bulgaria": "bg", "lithuania": "lt", "latvia": "lv", "estonia": "ee",
    "luxembourg": "lu", "malta": "mt", "cyprus": "cy", "liechtenstein": "li",
    "australia": "au", "united states": "us", "canada": "ca",
    "new zealand": "nz", "singapore": "sg", "hong kong": "hk", "india": "in",
}


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (text or "").casefold()).strip("-")
    return value or "document"


def _exact_anchor_key(label: str | None) -> str | None:
    """A provision key which retains parenthetical sub-parts."""
    if not label:
        return None
    value = str(label).casefold().replace("\u00a0", " ")
    value = re.sub(r"\barticles?\b|\barts?\b", "art", value)
    value = re.sub(r"\brecitals?\b|\brecs?\b", "rec", value)
    value = re.sub(r"\bsections?\b|\bsecs?\b", "sec", value)
    value = re.sub(r"\bregulations?\b|\bregs?\b", "reg", value)
    value = re.sub(r"[^a-z0-9()]+", "", value)
    return f"exact:{value}" if value else None


def _anchor_suffix(label: str | None) -> str | None:
    if not label:
        return None
    match = re.search(r"\d+[a-z]?((?:\s*\([^()]+\))+)\s*$", str(label), re.I)
    return re.sub(r"\s+", "", match.group(1)) if match else None


class _AttributionSanitiser(HTMLParser):
    """Allow simple editorial markup without letting a setting inject script."""

    _tags = {"a", "em", "strong", "i", "b", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self._tags:
            return
        if tag == "a":
            href = next((value for name, value in attrs if name == "href"), "") or ""
            if not href.startswith(("https://", "http://", "mailto:")):
                self.parts.append("<a>")
                return
            self.parts.append(
                f'<a href="{html.escape(href, quote=True)}" '
                'target="_blank" rel="noopener noreferrer">'
            )
        else:
            self.parts.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._tags and tag != "br":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data))


def _attribution_html() -> str:
    parser = _AttributionSanitiser()
    parser.feed(os.environ.get("RAGLEX_STATIC_EXPORT_ATTRIBUTION") or _DEFAULT_ATTRIBUTION)
    parser.close()
    return "".join(parser.parts)


def _flag_assets(jurisdictions: set[str]) -> dict[str, str]:
    """Embed only the circle-flag SVG files this edition actually uses."""
    roots: list[Path] = []
    configured = os.environ.get("RAGLEX_FRONTEND_DIST")
    if configured:
        roots.append(Path(configured) / "flags")
    module_path = Path(__file__).resolve()
    if len(module_path.parents) > 2:
        roots.append(module_path.parents[2] / "frontend" / "public" / "flags")
    roots.append(Path.cwd() / "frontend" / "public" / "flags")
    result: dict[str, str] = {}
    for jurisdiction in jurisdictions:
        code = _FLAG_CODE.get(jurisdiction.casefold())
        if not code:
            continue
        path = next((root / f"{code}.svg" for root in roots if (root / f"{code}.svg").is_file()), None)
        if path is None:
            continue
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        result[jurisdiction] = f"data:image/svg+xml;base64,{encoded}"
    return result


def _date_text(value) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def _raw_extension(row) -> str | None:
    raw_path = row["raw_path"] if row is not None else None
    if not raw_path or "." not in str(raw_path):
        return None
    return str(raw_path).rsplit(".", 1)[-1].casefold()


def _absolute_url(value: str | None, landing_url: str | None) -> str | None:
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    return urljoin(landing_url or "", value)


def _is_pdf_url(url: str | None) -> bool:
    if not url:
        return False
    return urlsplit(url).path.casefold().endswith(".pdf")


def _domain_label(url: str, fallback: str) -> str:
    host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    labels = {
        "bailii.org": "BAILII",
        "caselaw.nationalarchives.gov.uk": "Find Case Law",
        "eur-lex.europa.eu": "EUR-Lex",
        "edpb.europa.eu": "EDPB",
        "ec.europa.eu": "European Commission",
        "gdprhub.eu": "GDPRhub",
        "lawcom.gov.uk": "Law Commission",
        "legislation.gov.uk": "legislation.gov.uk",
    }
    for domain, label in labels.items():
        if host == domain or host.endswith("." + domain):
            return label
    return host or fallback


def _text_fragment_url(url: str, snippet: dict) -> str | None:
    """A conservative scroll-to-text URL.

    A normal citation string is used on its own.  Very short matches (``15``) are padded
    with nearby words so they do not land on the first unrelated number on the page.
    Failure is harmless: browsers ignore an unmatched text directive and open ``url``.
    """
    if not url or _is_pdf_url(url):
        return None
    text = snippet.get("text") or ""
    mark = snippet.get("mark")
    if not text or not mark or len(mark) != 2:
        return None
    start, end = int(mark[0]), int(mark[1])
    matched = re.sub(r"\s+", " ", text[start:end]).strip()
    if len(matched) < 5:
        before = re.findall(r"\S+", text[:start])[-4:]
        after = re.findall(r"\S+", text[end:])[:6]
        matched = " ".join([*before, matched, *after]).strip()
    if not matched:
        return None
    # Keep directives stable and modest. Long OCR passages make brittle URLs and some
    # public-sector services reject unusually long request targets.
    matched = matched[:180].strip()
    base, _ = urldefrag(url)
    return f"{base}#:~:text={quote(matched, safe='')}"


def _page_url(url: str, anchor: str | None) -> str | None:
    if not _is_pdf_url(url) or not anchor:
        return None
    match = re.search(r"\b(?:p(?:age)?s?\.?\s*)(\d+)\b", anchor, re.IGNORECASE)
    return f"{urldefrag(url)[0]}#page={match.group(1)}" if match else None


def _snippet(text: str, relation, *, before: int = 120, after: int = 260) -> dict | None:
    start = relation["context_start"]
    if start is None or not text:
        return None
    start = max(0, min(int(start), len(text)))
    end = relation["context_end"]
    end = start if end is None else max(start, min(int(end), len(text)))
    left, right = max(0, start - before), min(len(text), end + after)
    window = text[left:right]
    leading = len(window) - len(window.lstrip())
    body = window.strip()
    mark_start = min(max(0, start - left - leading), len(body))
    mark_end = min(max(mark_start, end - left - leading), len(body))
    return {
        "text": body,
        "mark": [mark_start, mark_end] if mark_end > mark_start else None,
        "where": relation["src_anchor"],
        "raw": relation["raw_citation_string"],
    }


def _public_links(facade: Facade, row, meta: dict) -> list[dict]:
    landing = row["landing_url"] or None
    source = row["source"]
    out: list[dict] = []
    seen: set[str] = set()

    def add(url: str | None, label: str, kind: str) -> None:
        absolute = _absolute_url(url, landing)
        if not absolute or absolute in seen or not absolute.startswith(("http://", "https://")):
            return
        seen.add(absolute)
        out.append({"url": absolute, "label": label, "kind": kind})

    originals = meta.get("original_sources") or []
    if isinstance(originals, list):
        for original in originals:
            if not isinstance(original, dict):
                continue
            name = original.get("name") or "official source"
            language = original.get("language")
            suffix = f" ({language})" if language else ""
            add(original.get("url"), f"Official source — {name}{suffix}", "official")

    for key in _PDF_META_KEYS:
        value = meta.get(key)
        if value:
            absolute = _absolute_url(value, landing)
            service = _domain_label(absolute or "", facade.source_label(source))
            add(value, f"Document from {service}", "document")

    for key in _SOURCE_META_KEYS:
        value = meta.get(key)
        if value:
            absolute = _absolute_url(value, landing)
            service = _domain_label(absolute or "", facade.source_label(source))
            add(value, service, "source")

    if landing:
        add(landing, _domain_label(landing, facade.source_label(source)), "source")
    return out


def _law_sections(text: str, segments: list) -> list[dict]:
    """Turn structural offsets into display sections without dropping gap text."""
    if not text:
        return []
    ordered = sorted(segments, key=lambda s: (s.char_start, s.char_end))
    if not ordered:
        return [{"label": "Document", "kind": "document", "level": 0, "text": text,
                 "key": "document", "id": "document"}]

    out: list[dict] = []
    cursor = 0
    for segment in ordered:
        start = max(cursor, min(int(segment.char_start), len(text)))
        end = max(start, min(int(segment.char_end), len(text)))
        gap = text[cursor:start]
        if gap.strip():
            if out:
                out[-1]["text"] += gap
            else:
                out.append({
                    "label": "Opening text", "kind": "opening", "level": 0,
                    "text": gap, "key": "opening", "id": "opening-text",
                })
        label = segment.label or "Untitled provision"
        key = _anchor_key(label) or _slug(label)
        out.append({
            "label": label,
            "kind": segment.kind or "section",
            "level": int(segment.level or 0),
            "text": text[start:end],
            "key": key,
            "id": _slug(label),
        })
        cursor = end
    if cursor < len(text):
        out[-1]["text"] += text[cursor:]
    return out


def _section_paragraphs(
    section: dict,
    anchor_marks: list[dict],
) -> list[dict]:
    """Split legislation drafting lines and place exact sub-part counters."""
    text = section["text"]
    if not text:
        return []
    # EU/UK legislation generally stores numbered and lettered drafting paragraphs at
    # line starts. Keep wrapped prose together and retain the marker in the visible text.
    chunks = [
        chunk for chunk in re.split(
            r"(?m)(?=^(?:\d+[a-z]?\.\s+|\([a-zivxlcdm]+\)\s+))", text
        )
        if chunk
    ]
    by_suffix: dict[str, list[dict]] = {}
    fallback: list[dict] = []
    for mark in anchor_marks:
        suffix = _anchor_suffix(mark["label"])
        (by_suffix.setdefault(suffix, []) if suffix else fallback).append(mark)

    paragraphs: list[dict] = []
    current_number: str | None = None
    placed: set[str] = set()
    for chunk in chunks:
        stripped = chunk.lstrip()
        numeric = re.match(r"(\d+[a-z]?)\.\s+", stripped, re.I)
        parenthetical = re.match(r"\(([a-zivxlcdm]+)\)\s+", stripped, re.I)
        suffix = None
        indent = 0
        if numeric:
            current_number = numeric.group(1)
            suffix = f"({current_number})"
            indent = 1
        elif parenthetical:
            token = parenthetical.group(1)
            suffix = f"({current_number})({token})" if current_number else f"({token})"
            indent = 2 if len(token) == 1 and token.isalpha() else 3
        marks = list(by_suffix.get(suffix or "", []))
        # Some corpora cite "(a)" without the parent paragraph even though the drafting
        # line sits under "1."; accept that form if the fully-qualified path was absent.
        if parenthetical and not marks:
            marks = list(by_suffix.get(f"({parenthetical.group(1)})", []))
        placed.update(mark["key"] for mark in marks)
        paragraphs.append({"text": chunk.strip(), "indent": indent, "marks": marks})

    leftovers = [
        mark for mark in anchor_marks
        if mark["key"] not in placed
    ]
    if paragraphs and leftovers:
        paragraphs[-1]["marks"].extend(leftovers)
    return paragraphs


@dataclass(frozen=True, slots=True)
class StaticExport:
    html: bytes
    filename: str
    stable_id: str
    documents: int
    mentions: int
    generated_at: str


class StaticLawExporter:
    """Build one complete HTML edition from the held corpus."""

    def __init__(self, config: Config | None = None, *, facade: Facade | None = None) -> None:
        self.facade = facade or Facade(config or Config.from_env())

    def build(
        self,
        stable_id: str,
        *,
        max_snippets: int = 4,
        progress: Callable[[int, int], None] | None = None,
    ) -> StaticExport:
        max_snippets = max(1, min(int(max_snippets), 12))
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        with self.facade._open() as (cat, _rawstore, textstore):
            target = cat.get_document(stable_id)
            if target is None:
                raise KeyError(f"document not found: {stable_id}")
            target_meta = _row_meta(target)
            display_title = str(target["title"] or stable_id)
            target_text = None
            if target["payload_hash"]:
                try:
                    target_text = textstore.get(target["payload_hash"])
                except OSError:
                    target_text = None
            if not target_text:
                raise ValueError(f"document has no extracted text: {stable_id}")

            segments = textstore.get_segments(target["payload_hash"]) if target["payload_hash"] else []
            if not segments:
                segments = synthesise_numbered_segments(target_text)
            sections = _law_sections(target_text, segments)

            relations = [
                dict(relation) for relation in cat.relations_to(stable_id)
                if relation["extracted_via"] != "inferred"
                and relation["src_id"] != stable_id
            ]
            provision_mappings = [dict(row) for row in cat.provision_mappings(stable_id)]
            for inherited in cat.inherited_mentions_for(stable_id, limit=5000):
                projected = dict(inherited)
                # Index the literal old-law citation under the CURRENT provision while
                # retaining its route/provenance for the separate filter and explanation.
                projected["dst_anchor"] = projected["inherited_current_anchor"]
                projected["is_inherited"] = True
                relations.append(projected)

            def relation_keys(relation) -> list[str]:
                anchor = relation["dst_anchor"]
                if not anchor:
                    return ["whole"]
                base = _anchor_key(anchor) or _slug(anchor)
                exact = _exact_anchor_key(anchor)
                if _anchor_suffix(anchor) and exact:
                    return [base, exact]
                return [base, f"bare:{base}"]

            anchor_marks_by_base: dict[str, dict[str, str]] = {}
            for relation in relations:
                anchor = relation["dst_anchor"]
                if not anchor:
                    continue
                base = _anchor_key(anchor) or _slug(anchor)
                specific = (
                    _exact_anchor_key(anchor)
                    if _anchor_suffix(anchor)
                    else f"bare:{base}"
                )
                if specific:
                    anchor_marks_by_base.setdefault(base, {}).setdefault(specific, anchor)

            by_source: dict[str, list] = {}
            for relation in relations:
                by_source.setdefault(relation["src_id"], []).append(relation)
            authority = cat.authority_for(list(by_source))

            groups: list[dict] = []
            source_items = list(by_source.items())
            total_sources = len(source_items)
            for position, (source_id, source_relations) in enumerate(source_items, 1):
                row = cat.get_document(source_id)
                if row is None:
                    continue
                meta = _row_meta(row)
                text = None
                if row["payload_hash"]:
                    try:
                        text = textstore.get(row["payload_hash"])
                    except OSError:
                        text = None

                links = _public_links(self.facade, row, meta)
                landing = row["landing_url"] or None
                highlight_base = (
                    landing if landing and not _is_pdf_url(landing) else None
                )
                pdf_link = next(
                    (link["url"] for link in links if _is_pdf_url(link["url"])), None)

                excerpts: list[dict] = []
                span_indices: dict[tuple[int | None, int | None], int] = {}
                snippet_indices: dict[str, list[int]] = {"all": []}
                mentions_by_key: dict[str, int] = {"all": len(source_relations)}
                inherited_mentions_by_key: dict[str, int] = {
                    "all": sum(bool(r.get("is_inherited")) for r in source_relations)}
                labels_by_key: dict[str, set[str]] = {"all": set()}
                target_keys: set[str] = set()
                for relation in sorted(
                    source_relations,
                    key=lambda r: (
                        r["context_start"] is None,
                        r["context_start"] if r["context_start"] is not None else 0,
                    ),
                ):
                    keys = relation_keys(relation)
                    target_keys.update(keys)
                    label = relation["dst_anchor"]
                    if label:
                        labels_by_key["all"].add(label)
                    for key in keys:
                        mentions_by_key[key] = mentions_by_key.get(key, 0) + 1
                        if relation.get("is_inherited"):
                            inherited_mentions_by_key[key] = \
                                inherited_mentions_by_key.get(key, 0) + 1
                        if label:
                            labels_by_key.setdefault(key, set()).add(label)

                    span = (relation["context_start"], relation["context_end"])
                    excerpt_index = span_indices.get(span)
                    if excerpt_index is None:
                        excerpt = _snippet(text or "", relation)
                        if excerpt:
                            passage = _page_url(pdf_link or "", excerpt.get("where"))
                            if not passage and highlight_base:
                                passage = _text_fragment_url(highlight_base, excerpt)
                            excerpt["passage_url"] = passage
                            excerpt_index = len(excerpts)
                            excerpts.append(excerpt)
                            span_indices[span] = excerpt_index
                    if excerpt_index is None:
                        continue
                    for key in ["all", *keys]:
                        bucket = snippet_indices.setdefault(key, [])
                        if excerpt_index not in bucket and len(bucket) < max_snippets:
                            bucket.append(excerpt_index)

                oscola = _oscola_cite(row, meta)
                auth = authority.get(source_id) or {}
                groups.append({
                    "id": source_id,
                    "title": row["title"] or source_id,
                    "cite": oscola.get("text") or row["title"] or source_id,
                    "date": _date_text(row["decision_date"]),
                    "source": row["source"],
                    "source_label": self.facade.source_label(row["source"]),
                    "doc_type": row["doc_type"],
                    "kind": self.facade._doc_kind(
                        row["source"], row["doc_type"], row["court"]),
                    "jurisdiction": self.facade._doc_bucket(row["source"], row["court"]),
                    "court": (
                        self.facade.court_label(row["court"], row["source"])
                        if row["court"] else None
                    ),
                    "mentions": len(source_relations),
                    "mentions_by_key": mentions_by_key,
                    "inherited_mentions_by_key": inherited_mentions_by_key,
                    "has_inherited": bool(inherited_mentions_by_key["all"]),
                    "labels_by_key": {
                        key: sorted(labels) for key, labels in labels_by_key.items()
                    },
                    "target_keys": sorted(target_keys),
                    "whole_instrument": any(
                        not relation["dst_anchor"] for relation in source_relations),
                    "relationships": sorted({
                        relation["relationship_type"] for relation in source_relations
                    }),
                    "pagerank": float(auth.get("pagerank") or 0),
                    "links": links,
                    "snippets": excerpts,
                    "snippet_indices": snippet_indices,
                    "has_text": bool(text),
                    "raw_ext": _raw_extension(row),
                })
                if progress and (position == total_sources or position % 250 == 0):
                    progress(position, total_sources)

        groups.sort(key=lambda g: (-g["pagerank"], g["date"] or "", g["cite"]))
        index: dict[str, list[int]] = {"all": list(range(len(groups))), "whole": []}
        inherited_index: dict[str, list[int]] = {"all": []}
        for i, group in enumerate(groups):
            if group["has_inherited"]:
                inherited_index["all"].append(i)
            if group["whole_instrument"]:
                index["whole"].append(i)
            for key in group["target_keys"]:
                if key == "whole":
                    continue
                index.setdefault(key, []).append(i)
                if group["inherited_mentions_by_key"].get(key):
                    inherited_index.setdefault(key, []).append(i)

        counts = {key: len(ids) for key, ids in index.items()}
        inherited_counts = {key: len(ids) for key, ids in inherited_index.items()}
        for section in sections:
            marks = [
                {"key": key, "label": label, "count": counts.get(key, 0)}
                for key, label in anchor_marks_by_base.get(section["key"], {}).items()
                if key.startswith("exact:") and counts.get(key, 0)
            ]
            section["paragraphs"] = _section_paragraphs(section, marks)

        target_cite = _oscola_cite(target, target_meta)
        target_links = _public_links(self.facade, target, target_meta)
        jurisdictions = {g["jurisdiction"] for g in groups if g["jurisdiction"]}
        data = {
            "generated_at": generated_at,
            "law": {
                "stable_id": stable_id,
                "title": display_title,
                "cite": target_cite.get("text") or target["title"] or stable_id,
                "source": self.facade.source_label(target["source"]),
                "links": target_links,
                "sections": sections,
                "provision_mappings": provision_mappings,
            },
            "groups": groups,
            "index": index,
            "inherited_index": inherited_index,
            "counts": counts,
            "inherited_counts": inherited_counts,
            "stats": {
                "documents": len(groups),
                "mentions": len(relations),
                "with_public_url": sum(bool(group["links"]) for group in groups),
                "with_snippet": sum(bool(group["snippets"]) for group in groups),
            },
            "flags": _flag_assets(jurisdictions),
        }
        rendered = render_static_html(data)
        filename = f"{_slug(display_title)[:80]}.html"
        return StaticExport(
            html=rendered.encode("utf-8"),
            filename=filename,
            stable_id=stable_id,
            documents=len(groups),
            mentions=len(relations),
            generated_at=generated_at,
        )

    def write(
        self,
        stable_id: str,
        output: str | Path,
        *,
        max_snippets: int = 4,
        progress: Callable[[int, int], None] | None = None,
    ) -> StaticExport:
        result = self.build(
            stable_id, max_snippets=max_snippets, progress=progress)
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.html)
        return result


def static_export_cache_path(
    config: Config, stable_id: str, *, max_snippets: int = 4
) -> Path:
    identity = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:12]
    return (
        config.data_dir / "exports" / "cache"
        / (
            f"{_slug(stable_id)[:80]}-{identity}"
            f"-v{_CACHE_VERSION}-snippets-{max(1, min(int(max_snippets), 12))}.html"
        )
    )


def static_export_manifest_path(path: Path) -> Path:
    return path.with_suffix(".json")


def static_export_status(
    config: Config, stable_id: str, *, max_snippets: int = 4
) -> dict:
    path = static_export_cache_path(config, stable_id, max_snippets=max_snippets)
    manifest_path = static_export_manifest_path(path)
    if not path.is_file() or not manifest_path.is_file():
        return {"ready": False, "stable_id": stable_id, "max_snippets": max_snippets}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"ready": False, "stable_id": stable_id, "max_snippets": max_snippets}
    return {**manifest, "ready": True, "_path": str(path)}


def build_static_export_cache(
    facade: Facade,
    stable_id: str,
    *,
    max_snippets: int = 4,
    on_progress: Callable[..., None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """Build and atomically publish the cached artifact used by the download API."""
    max_snippets = max(1, min(int(max_snippets), 12))

    def progress(done: int, total: int) -> None:
        if cancel_check and cancel_check():
            raise RuntimeError("static export cancelled")
        if on_progress:
            on_progress(
                stage="reading excerpts", done=done, total=total,
                item=f"{done:,} of {total:,} citing documents",
            )

    result = StaticLawExporter(facade=facade).build(
        stable_id, max_snippets=max_snippets, progress=progress)
    path = static_export_cache_path(
        facade.config, stable_id, max_snippets=max_snippets)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".html.tmp")
    temporary.write_bytes(result.html)
    temporary.replace(path)
    manifest = {
        "stable_id": stable_id,
        "max_snippets": max_snippets,
        "filename": result.filename,
        "documents": result.documents,
        "mentions": result.mentions,
        "bytes": len(result.html),
        "generated_at": result.generated_at,
    }
    manifest_path = static_export_manifest_path(path)
    manifest_temporary = manifest_path.with_suffix(".json.tmp")
    manifest_temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_temporary.replace(manifest_path)
    return manifest


def _json_for_script(data: dict) -> str:
    # A source title or snippet containing ``</script>`` must remain inert data.
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_static_html(data: dict) -> str:
    law = data["law"]
    title = law["title"]
    page = _HTML_TEMPLATE
    replacements = {
        "__PAGE_TITLE__": html.escape(f"{title} — citations", quote=True),
        "__TITLE__": html.escape(title),
        "__ATTRIBUTION__": _attribution_html(),
        "__STYLE__": _STYLE,
        "__SCRIPT__": _SCRIPT,
        "__DATA__": _json_for_script(data),
    }
    for token, value in replacements.items():
        page = page.replace(token, value)
    return page


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>__PAGE_TITLE__</title>
  <style>__STYLE__</style>
</head>
<body>
  <header class="page-head">
    <button class="contents-toggle" id="contents-toggle" type="button" aria-expanded="true">[ contents ]</button>
    <div>
      <h1>__TITLE__</h1>
      <p class="attribution">__ATTRIBUTION__</p>
    </div>
  </header>
  <div class="page">
    <aside class="contents" id="contents">
      <nav id="contents-nav" aria-label="Law contents"></nav>
    </aside>
    <main id="law"></main>
  </div>
  <dialog id="mentions-dialog" aria-labelledby="mentions-title">
    <div class="dialog-head">
      <div>
        <h2 id="mentions-title">Mentions</h2>
      </div>
      <button id="dialog-close" type="button">[ close ]</button>
    </div>
    <div class="filters">
      <div id="facet-tokens" class="facet-tokens" aria-label="Filter citing documents"></div>
      <label>Order
        <select id="sort-filter">
          <option value="authority">Most influential</option>
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
          <option value="passages">Most passages</option>
        </select>
      </label>
      <label>Mentions
        <select id="route-filter">
          <option value="all">Direct + previous iterations</option>
          <option value="direct">This law directly</option>
          <option value="previous">Previous functionally similar provisions</option>
        </select>
      </label>
    </div>
    <div id="results"></div>
    <button id="more-results" class="more" type="button">+ show more</button>
  </dialog>
  <script id="raglex-data" type="application/json">__DATA__</script>
  <script>__SCRIPT__</script>
</body>
</html>
"""


_STYLE = r"""
:root {
  color-scheme: light only;
  --paper: #ffffff;
  --paper-raised: #ffffff;
  --ink: #181714;
  --quiet: #625e55;
  --rule: #aaa396;
  --faint-rule: #d7d0c3;
  --mark: #eadf8c;
  --sidebar: 19rem;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: Times, "Times New Roman", serif;
  font-size: 17px;
  line-height: 1.52;
}
button, input, select {
  border: 1px solid var(--rule);
  border-radius: 0;
  background: var(--paper-raised);
  color: var(--ink);
  font: inherit;
}
button { cursor: pointer; }
button:hover, button:focus-visible, input:focus, select:focus {
  border-color: var(--ink);
  outline: 1px solid var(--ink);
  outline-offset: 1px;
}
a { color: #142b7a; text-decoration-thickness: 1px; text-underline-offset: .16em; }
a:visited { color: #5d3267; }
.page-head {
  display: grid;
  grid-template-columns: var(--sidebar) minmax(0, 52rem);
  gap: 2.7rem;
  padding: 2.3rem 3rem 1.8rem;
  border-bottom: 1px solid var(--ink);
}
.page-head h1 {
  margin: 0;
  max-width: 48rem;
  font-size: clamp(1.8rem, 2.6vw, 2.35rem);
  font-weight: 400;
  line-height: 1.08;
  text-wrap: balance;
}
.attribution {
  max-width: 52rem;
  margin: 1rem 0 0;
  color: var(--quiet);
  font-size: 1.05rem;
  line-height: 1.45;
}
.contents-toggle {
  justify-self: start;
  align-self: start;
  padding: .25rem .5rem;
  border-color: transparent;
  background: transparent;
}
.page {
  display: grid;
  grid-template-columns: var(--sidebar) minmax(0, 52rem);
  gap: 2.7rem;
  padding: 0 3rem 5rem;
  align-items: start;
}
.contents {
  position: sticky;
  top: 0;
  max-height: 100vh;
  overflow: auto;
  padding: 1.6rem .8rem 2rem 0;
  border-right: 1px solid var(--rule);
}
.contents nav { margin-top: 0; }
.contents a, .contents button {
  display: flex;
  width: 100%;
  justify-content: space-between;
  gap: .75rem;
  padding: .35rem .9rem .35rem 0;
  border: 0;
  border-bottom: 1px dotted var(--faint-rule);
  background: transparent;
  text-align: left;
  text-decoration: none;
  line-height: 1.25;
}
.contents a:hover, .contents button:hover { text-decoration: underline; outline: 0; }
.contents .count { color: var(--quiet); font-variant-numeric: tabular-nums; }
.contents .all-mentions { margin-bottom: .8rem; border-bottom: 1px solid var(--ink); }
main { min-width: 0; padding-top: 1.5rem; }
.law-section {
  position: relative;
  padding: 1.2rem 0 1.7rem;
  border-bottom: 1px solid var(--faint-rule);
  scroll-margin-top: 1rem;
}
.law-section h2 {
  margin: 0 0 .75rem;
  max-width: 43rem;
  font-size: 1.25rem;
  line-height: 1.25;
  font-weight: 600;
}
.law-section.level-1 { margin-left: 1.4rem; }
.law-section.level-2 { margin-left: 2.8rem; }
.law-text {
  margin: 0;
  max-width: 46rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.law-paragraph {
  max-width: 46rem;
  margin: 0 0 .8rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.law-paragraph.indent-1 { margin-left: 1.45rem; }
.law-paragraph.indent-2 { margin-left: 2.9rem; }
.law-paragraph.indent-3 { margin-left: 4.35rem; }
.mention-ref {
  display: inline;
  margin-left: .45rem;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--quiet);
  font-weight: 700;
  font-size: .9rem;
  white-space: nowrap;
}
.mention-ref:hover {
  border: 0;
  outline: 0;
  color: var(--ink);
  text-decoration: underline;
}
.source-note {
  margin: 1.5rem 0 2rem;
  padding: 0;
  color: var(--quiet);
}
dialog {
  width: min(58rem, calc(100vw - 2rem));
  max-height: calc(100vh - 2rem);
  padding: 0 1.4rem 1.5rem;
  border: 1px solid var(--ink);
  border-radius: 0;
  background: var(--paper-raised);
  color: var(--ink);
  box-shadow: 5px 5px 0 rgba(24, 23, 20, .28);
}
dialog::backdrop { background: rgba(24, 23, 20, .42); }
.dialog-head {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  justify-content: space-between;
  gap: 2rem;
  padding: 1.25rem 0 .9rem;
  border-bottom: 1px solid var(--ink);
  background: var(--paper-raised);
}
.dialog-head h2 { margin: 0; font-size: 1.6rem; font-weight: 500; }
.dialog-head button {
  align-self: start;
  padding: .2rem .45rem;
  border: 0;
  background: transparent;
}
.dialog-head button:hover { border: 0; outline: 0; text-decoration: underline; }
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: .65rem 1rem;
  padding: 1rem 0;
  border-bottom: 1px solid var(--faint-rule);
}
.filters label { color: var(--quiet); font-size: 1rem; }
.filters select {
  margin-left: .35rem;
  padding: .3rem .4rem;
  font-size: 1rem;
}
.facet-tokens { display: flex; flex: 1 1 30rem; flex-wrap: wrap; gap: .35rem .5rem; }
.facet-token {
  display: inline-flex;
  align-items: center;
  gap: .35rem;
  padding: .18rem .4rem;
  border: 1px solid var(--rule);
  background: transparent;
  color: var(--quiet);
  font-size: .9rem;
}
.facet-token:hover, .facet-token.on {
  border-color: var(--ink);
  outline: 0;
  color: var(--ink);
}
.facet-token.on { background: #f1f1f1; }
.facet-token b { color: var(--ink); font-variant-numeric: tabular-nums; }
.flag-icon { width: 1em; height: 1em; border-radius: 50%; vertical-align: -.12em; }
.result {
  padding: 1.25rem 0 1.45rem;
  border-bottom: 1px solid var(--rule);
}
.result h3 { margin: 0; font-size: 1.08rem; font-weight: 500; line-height: 1.35; }
.result time { color: var(--quiet); white-space: nowrap; }
.result-head { display: flex; justify-content: space-between; gap: 1.2rem; }
.result-meta, .result-targets, .source-links {
  margin: .3rem 0 0;
  color: var(--quiet);
  font-size: 1rem;
}
.snippet {
  margin: .85rem 0 0;
  padding: .3rem 0 .3rem 1rem;
  border-left: 2px solid var(--ink);
}
.snippet p { margin: 0; }
.snippet .where { color: var(--quiet); font-size: 1rem; }
mark { padding: 0 .08em; background: var(--mark); color: inherit; }
.source-links a + a::before { content: " · "; color: var(--quiet); text-decoration: none; }
.no-source { color: #7d322a; }
.more { display: block; margin: 1.4rem auto 0; padding: .3rem .7rem; background: transparent; }
.empty { padding: 2rem 0; color: var(--quiet); }
body.sidebar-closed .page-head, body.sidebar-closed .page { grid-template-columns: minmax(0, 52rem); }
body.sidebar-closed .contents { display: none; }
body.sidebar-closed .page-head, body.sidebar-closed .page { padding-left: max(2rem, calc((100vw - 52rem) / 2)); }
body.sidebar-closed .page-head > div { grid-column: 1; }
@media (max-width: 760px) {
  body { font-size: 16px; }
  .page-head { display: block; padding: 1.2rem 1.1rem; }
  .contents-toggle { margin-bottom: .8rem; }
  .page { display: block; padding: 0 1.1rem 3rem; }
  .contents {
    position: relative;
    max-height: 45vh;
    margin-bottom: 1rem;
    padding: 1rem 0;
    border-right: 0;
    border-bottom: 1px solid var(--ink);
  }
  body.sidebar-closed .page-head, body.sidebar-closed .page { padding-left: 1.1rem; }
  .result-head { display: block; }
  .filters { display: block; }
  .filters label { display: block; margin-top: .8rem; }
  .filters select { width: 100%; min-width: 0; margin: .3rem 0 0; }
  .law-paragraph.indent-1 { margin-left: .8rem; }
  .law-paragraph.indent-2 { margin-left: 1.6rem; }
  .law-paragraph.indent-3 { margin-left: 2.4rem; }
}
@media print {
  .contents-toggle, .contents, .mention-ref, dialog { display: none !important; }
  .page-head, .page { display: block; padding-left: 0; padding-right: 0; }
  body { background: white; }
}
"""


_SCRIPT = r"""
(() => {
  "use strict";
  const data = JSON.parse(document.getElementById("raglex-data").textContent);
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  const number = (n) => Number(n || 0).toLocaleString();

  const sourceNote = document.createElement("p");
  sourceNote.className = "source-note";
  const lawLinks = data.law.links.length
    ? data.law.links.map((link) =>
        `<a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)} →</a>`).join(" · ")
    : `<span class="no-source">No public copy recorded</span>`;
  sourceNote.innerHTML = lawLinks;
  $("law").appendChild(sourceNote);

  const nav = $("contents-nav");
  const all = document.createElement("button");
  all.type = "button";
  all.className = "all-mentions";
  all.innerHTML = `<span>All mentions</span><span class="count">${number(data.counts.all)}</span>`;
  all.addEventListener("click", () => openMentions("all", "All mentions"));
  nav.appendChild(all);
  if (Number(data.inherited_counts.all || 0)) {
    const previous = document.createElement("button");
    previous.type = "button";
    previous.className = "all-mentions";
    previous.innerHTML = `<span>Previous iterations</span><span class="count">${number(data.inherited_counts.all)}</span>`;
    previous.addEventListener("click", () => openMentions("all", "Previous, functionally similar iterations", "previous"));
    nav.appendChild(previous);
  }

  for (const section of data.law.sections) {
    const count = data.counts[section.key] || 0;
    const link = document.createElement("a");
    link.href = `#${section.id}`;
    link.dataset.search = section.label.toLowerCase();
    link.innerHTML = `<span>${esc(section.label)}</span>${count ? `<span class="count">${number(count)}</span>` : ""}`;
    nav.appendChild(link);

    const article = document.createElement("section");
    article.id = section.id;
    article.className = `law-section level-${Math.min(2, Number(section.level || 0))}`;
    const heading = document.createElement("h2");
    heading.textContent = section.label;
    if (count) {
      const mentions = document.createElement("button");
      mentions.type = "button";
      mentions.className = "mention-ref";
      mentions.textContent =
        `[${number(count)} ${count === 1 ? "mention" : "mentions"}]`;
      mentions.addEventListener("click", () => openMentions(section.key, section.label));
      heading.appendChild(mentions);
    }
    const inheritedCount = Number(data.inherited_counts[section.key] || 0);
    if (inheritedCount) {
      const previous = document.createElement("button");
      previous.type = "button";
      previous.className = "mention-ref";
      previous.textContent = `[${number(inheritedCount)} via previous law]`;
      previous.addEventListener("click", () =>
        openMentions(section.key, `${section.label} — previous iterations`, "previous"));
      heading.appendChild(previous);
    }
    article.appendChild(heading);
    for (const paragraph of section.paragraphs || [{ text: section.text, indent: 0, marks: [] }]) {
      const body = document.createElement("p");
      body.className = `law-paragraph indent-${Math.min(3, Number(paragraph.indent || 0))}`;
      body.append(document.createTextNode(paragraph.text));
      for (const mark of paragraph.marks || []) {
        const mentions = document.createElement("button");
        mentions.type = "button";
        mentions.className = "mention-ref";
        mentions.textContent =
          `[${number(mark.count)} ${mark.count === 1 ? "mention" : "mentions"}]`;
        mentions.addEventListener("click", () => openMentions(mark.key, mark.label));
        body.appendChild(mentions);
      }
      article.appendChild(body);
    }
    $("law").appendChild(article);
  }

  $("contents-toggle").addEventListener("click", () => {
    document.body.classList.toggle("sidebar-closed");
    const open = !document.body.classList.contains("sidebar-closed");
    $("contents-toggle").setAttribute("aria-expanded", String(open));
    $("contents-toggle").textContent = open ? "[ contents ]" : "[ show contents ]";
  });

  const kindNames = {
    cases: "case law", administrative: "admin decisions",
    legislation: "legislation", guidance: "guidance & reports",
    preparatory: "preparatory documents", other: "other"
  };
  const jurisdictionNames = {
    "European Union": "EU", "United Kingdom": "UK", "Ireland": "Irish",
    "France": "French", "Germany": "German", "Netherlands": "Dutch",
    "Belgium": "Belgian", "Spain": "Spanish", "Italy": "Italian",
    "Austria": "Austrian", "Poland": "Polish", "Portugal": "Portuguese",
    "Sweden": "Swedish", "Denmark": "Danish", "Finland": "Finnish",
    "Norway": "Norwegian", "Australia": "Australian", "Canada": "Canadian",
    "United States": "US", "New Zealand": "New Zealand", "Bulgaria": "Bulgarian",
    "Council of Europe": "Council of Europe", "Croatia": "Croatian",
    "Cyprus": "Cypriot", "Czechia": "Czech", "Estonia": "Estonian",
    "Greece": "Greek", "Hungary": "Hungarian", "Iceland": "Icelandic",
    "Latvia": "Latvian", "Liechtenstein": "Liechtenstein",
    "Lithuania": "Lithuanian", "Luxembourg": "Luxembourg", "Malta": "Maltese",
    "Romania": "Romanian", "Slovakia": "Slovak", "Slovenia": "Slovenian"
  };
  const flagHtml = (jurisdiction) => data.flags[jurisdiction]
    ? `<img class="flag-icon" src="${data.flags[jurisdiction]}" alt="">` : "";

  const state = { key: "all", label: "All mentions", limit: 40, facet: null, route: "all" };
  function openMentions(key, label, route = "all") {
    state.key = key;
    state.label = label;
    state.limit = 40;
    state.facet = null;
    state.route = route;
    $("mentions-title").textContent = label;
    $("sort-filter").value = "authority";
    $("route-filter").value = route;
    renderResults();
    $("mentions-dialog").showModal();
  }

  function snippetsFor(group) {
    const ids = group.snippet_indices[state.key] || [];
    return ids.map((id) => group.snippets[id]).filter(Boolean);
  }

  function selectedGroups() {
    const ids = data.index[state.key] || [];
    const rows = ids.map((id) => data.groups[id]).filter((group) => {
      const inherited = Number(group.inherited_mentions_by_key[state.key] || 0);
      const total = Number(group.mentions_by_key[state.key] || 0);
      if (state.route === "previous" && !inherited) return false;
      if (state.route === "direct" && total <= inherited) return false;
      if (!state.facet) return true;
      return `${group.jurisdiction}|${group.kind}` === state.facet;
    });
    const sort = $("sort-filter").value;
    rows.sort((a, b) => {
      if (sort === "newest") return String(b.date || "").localeCompare(String(a.date || "")) || b.pagerank - a.pagerank;
      if (sort === "oldest") return String(a.date || "9999").localeCompare(String(b.date || "9999")) || b.pagerank - a.pagerank;
      if (sort === "passages") {
        return Number(b.mentions_by_key[state.key] || 0)
          - Number(a.mentions_by_key[state.key] || 0) || b.pagerank - a.pagerank;
      }
      return b.pagerank - a.pagerank || String(b.date || "").localeCompare(String(a.date || ""));
    });
    return rows;
  }

  function snippetHtml(snippet) {
    const text = String(snippet.text || "");
    const mark = snippet.mark;
    let passage = esc(text);
    if (mark && mark.length === 2) {
      passage = esc(text.slice(0, mark[0])) + "<mark>" +
        esc(text.slice(mark[0], mark[1])) + "</mark>" + esc(text.slice(mark[1]));
    }
    const where = snippet.where ? `<p class="where">${esc(snippet.where)}</p>` : "";
    const link = snippet.passage_url
      ? `<p class="source-links"><a href="${esc(snippet.passage_url)}" target="_blank" rel="noopener noreferrer">try exact passage →</a></p>`
      : "";
    return `<div class="snippet">${where}<p>${passage}</p>${link}</div>`;
  }

  function resultHtml(group) {
    const excerptsForSelection = snippetsFor(group);
    const labelsForSelection = group.labels_by_key[state.key] || [];
    const mentionsForSelection = Number(group.mentions_by_key[state.key] || 0);
    const primary = group.links[0];
    const heading = primary
      ? `<a href="${esc(primary.url)}" target="_blank" rel="noopener noreferrer">${esc(group.cite)}</a>`
      : esc(group.cite);
    const links = group.links.length
      ? group.links.map((link) =>
          `<a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)} →</a>`).join("")
      : `<span class="no-source">No public copy recorded</span>`;
    const targets = labelsForSelection.length
      ? `<p class="result-targets">References ${labelsForSelection.map(esc).join(" · ")}</p>` : "";
    const details = [group.jurisdiction, kindNames[group.kind] || group.kind,
      group.court, group.source_label,
      `${number(mentionsForSelection)} ${mentionsForSelection === 1 ? "mention" : "mentions"}`,
      group.inherited_mentions_by_key[state.key] ? "includes previous-law lineage" : null]
      .filter(Boolean).map(esc).join(" · ");
    const excerpts = excerptsForSelection.length
      ? excerptsForSelection.map(snippetHtml).join("")
      : `<p class="result-meta">RagLex has the reference, but no excerpt is available.</p>`;
    return `<article class="result">
      <div class="result-head"><h3>${heading}</h3>${group.date ? `<time>${esc(group.date.slice(0, 4))}</time>` : ""}</div>
      <p class="result-meta">${flagHtml(group.jurisdiction)} ${details}</p>${targets}${excerpts}
      <p class="source-links">${links}</p>
    </article>`;
  }

  function renderFacetTokens() {
    const facets = new Map();
    for (const id of data.index[state.key] || []) {
      const group = data.groups[id];
      if (!group.jurisdiction || !group.kind) continue;
      const key = `${group.jurisdiction}|${group.kind}`;
      const current = facets.get(key) || {
        jurisdiction: group.jurisdiction, kind: group.kind, count: 0
      };
      current.count += 1;
      facets.set(key, current);
    }
    const rows = [...facets.entries()].sort((a, b) =>
      b[1].count - a[1].count ||
      `${a[1].jurisdiction} ${a[1].kind}`.localeCompare(
        `${b[1].jurisdiction} ${b[1].kind}`));
    const box = $("facet-tokens");
    box.innerHTML = rows.map(([key, facet]) => {
      const place = jurisdictionNames[facet.jurisdiction] || facet.jurisdiction;
      const kind = facet.kind === "administrative" && facet.count === 1
        ? "admin decision" : kindNames[facet.kind] || facet.kind;
      return `<button type="button" class="facet-token${state.facet === key ? " on" : ""}" data-facet="${esc(key)}">`
        + `${flagHtml(facet.jurisdiction)}<span>${esc(place)} ${esc(kind)}</span>`
        + `<b>${number(facet.count)}</b></button>`;
    }).join("");
    for (const button of box.querySelectorAll("button")) {
      button.addEventListener("click", () => {
        const key = button.dataset.facet;
        state.facet = state.facet === key ? null : key;
        state.limit = 40;
        renderResults();
      });
    }
  }

  function renderResults() {
    renderFacetTokens();
    const rows = selectedGroups();
    const visible = rows.slice(0, state.limit);
    $("results").innerHTML = visible.length
      ? visible.map(resultHtml).join("")
      : `<p class="empty">No documents match these filters.</p>`;
    $("more-results").hidden = visible.length >= rows.length;
  }

  $("dialog-close").addEventListener("click", () => $("mentions-dialog").close());
  $("mentions-dialog").addEventListener("click", (event) => {
    if (event.target === $("mentions-dialog")) $("mentions-dialog").close();
  });
  $("more-results").addEventListener("click", () => { state.limit += 40; renderResults(); });
  $("sort-filter").addEventListener("change", () => {
    state.limit = 40;
    renderResults();
  });
  $("route-filter").addEventListener("change", () => {
    state.route = $("route-filter").value;
    state.limit = 40;
    renderResults();
  });
})();
"""
