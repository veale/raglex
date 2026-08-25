"""Static editions of a held law and the documents mentioning it.

The output deliberately has no dependency on the RagLex API.  The selected law, its
provision index is embedded in the HTML. A direct download can remain one self-contained
file; a published bundle writes the heavy citing-document rows as fetchable sidecars so a
reader opening one paragraph does not first download and parse the entire citation graph.
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
# v5 caches the data payload; the page renders from it on download.
# v6 names each predecessor instrument (direct vs via-previous-law counts) and records
# the law's own jurisdiction, which the bundle index groups by.
# v7 drops the row caps on the projected/inherited mention queries. The SHAPE is
# unchanged, but every v6 payload for a consolidation was built from a truncated set and
# under-counts its citers, so they must not be re-rendered — hence a version bump rather
# than a comment.
# v8 carries the document's language. Same reasoning: a v7 payload has no language to
# read, so re-rendering one keeps declaring lang="en" over French and German text for
# ever. The bump is what makes the fix reach editions already cached.
_CACHE_VERSION = 8

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


# An EU instrument's official title is a paragraph ("Regulation (EU) 2016/679 of the
# European Parliament and of the Council of 27 April 2016 on the protection of natural
# persons…"). Inside a sentence — "12 mentions of a similar provision in …" — only the
# form and the number identify it, so the citable stem is all we keep.
_EU_INSTRUMENT_RE = re.compile(
    r"\b(?P<body>Council|Commission|European Parliament and Council)?\s*"
    r"(?P<mode>Framework|Implementing|Delegated)?\s*"
    r"(?P<kind>Directive|Regulation|Decision|Recommendation)\s+"
    r"(?P<paren>\((?:EU|EC|EEC|ECSC|Euratom)(?:\s*,\s*Euratom)?\)\s*)?"
    r"(?:No\s*)?"
    r"(?P<number>\d{1,4}/\d{1,4}(?:/(?:EU|EC|EEC|JHA|CFSP|Euratom|ECSC))?)\b",
    re.I,
)
# Common-law drafting names its instruments the other way round, and the name IS short.
_ACT_RE = re.compile(
    r"^(.{0,90}?\b(?:Act|Regulations|Order|Rules|Measure|Ordinance)\s+\d{4})\b")


def _short_instrument_title(title: str | None, fallback: str = "") -> str:
    """``Directive 95/46/EC`` from its full, wordy title.

    Falls back progressively: an Act-style short title, then the title cut at the first
    ``of``/``on``/comma clause, then the raw title — never nothing, because this label is
    what a reader clicks.
    """
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if not text:
        return fallback
    match = _EU_INSTRUMENT_RE.search(text)
    if match:
        parts = [
            match.group("body"), match.group("mode"), match.group("kind"),
            (match.group("paren") or "").strip(), match.group("number"),
        ]
        return " ".join(part.strip() for part in parts if part and part.strip())
    act = _ACT_RE.match(text)
    if act:
        return act.group(1)
    if len(text) > 64:
        clause = re.split(r",| of the | of \d| on the | establishing ", text, maxsplit=1)[0]
        text = clause.strip() if 8 < len(clause.strip()) < len(text) else text
    return text if len(text) <= 90 else text[:87].rstrip() + "…"


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

    _tags = {"a", "em", "strong", "i", "b", "u", "br", "small"}

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


def sanitise_editorial_html(text: str) -> str:
    """Operator-authored markup, stripped to the handful of tags an editorial note needs."""
    parser = _AttributionSanitiser()
    parser.feed(text or "")
    parser.close()
    return "".join(parser.parts)


def editorial_paragraphs(text: str | None, css_class: str) -> list[str]:
    """Prose typed into one of the settings textareas, as paragraphs.

    A textarea has no markup, so the line breaks ARE the structure a writer intends: a
    blank line starts a new paragraph, a single newline is a line break inside one. Split
    before sanitising is wrong (the sanitiser is what escapes the text), so split first
    and sanitise each block.
    """
    out: list[str] = []
    for block in re.split(r"\n[ \t]*\n", (text or "").strip()):
        body = sanitise_editorial_html(block.strip()).strip()
        if not body:
            continue
        out.append(f'<p class="{css_class}">{body.replace(chr(10), "<br>")}</p>')
    return out


def _attribution_html() -> str:
    return sanitise_editorial_html(
        os.environ.get("RAGLEX_STATIC_EXPORT_ATTRIBUTION") or _DEFAULT_ATTRIBUTION)


def _subset_sentence(subset: list | None) -> str:
    """What a subset edition must say about itself, before anything else it says.

    A page headed "Consolidated version of the Treaty on the Functioning of the European
    Union" that contains four Articles is a trap unless it admits it. Named in full where
    the list is short enough to read, counted where it is not."""
    labels = [str(label).strip() for label in (subset or []) if str(label).strip()]
    if not labels:
        return ""
    if len(labels) <= 8:
        named = ", ".join(labels[:-1]) + (" and " if len(labels) > 1 else "") + labels[-1]
        body = f"This edition contains {named} only"
    else:
        body = f"This edition contains {len(labels)} selected provisions only"
    return (f'<p class="attribution subset-note"><strong>{html.escape(body)}</strong>'
            " — not the whole instrument. Citing documents are those citing these"
            " provisions.</p>")


def _attribution_block(note: str | None = None, subset: list | None = None) -> str:
    """The attribution paragraph, plus — when this edition is one item of a bundle — its
    own line directly beneath, in the same style, under the title. The way back to the
    bundle's index is not here: it sits in the contents column, where a reader looks for
    navigation."""
    paragraphs = [f'<p class="attribution">{_attribution_html()}</p>']
    subset_line = _subset_sentence(subset)
    if subset_line:
        paragraphs.append(subset_line)
    paragraphs.extend(editorial_paragraphs(note, "attribution"))
    return "\n      ".join(paragraphs)


#: An EU instrument's official title ends with a legislative footnote about the EEA
#: Agreement. It is part of the citation of record and says nothing about the law, and on
#: a page whose whole top line is the title it costs a line every time.
_EEA_RELEVANCE = re.compile(r"\s*\(\s*Text with EEA relevance\.?\s*\)", re.IGNORECASE)


def _display_title(title: str | None) -> str:
    """The instrument's name as a heading, rather than as a catalogue entry."""
    return _EEA_RELEVANCE.sub("", str(title or "")).strip()


def _sidebar_head(title: str | None, index_link: dict | None) -> str:
    """The contents column's own heading: which law this is, and — for one edition of a
    set — the way back to the set. This replaced a ``[ contents ]`` button, which existed
    only to hide the one piece of navigation the page has."""
    lines = [f'<p class="contents-title">{html.escape(_display_title(title))}</p>']
    if index_link and index_link.get("href"):
        href = html.escape(str(index_link["href"]), quote=True)
        label = html.escape(str(index_link.get("title") or "the index"))
        lines.append(
            f'<p class="contents-back"><a href="{href}">Back to {label}</a></p>')
    return "\n        ".join(lines)


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
    from .citations.reanchor import aligned_span

    aligned = aligned_span(
        text, relation["raw_citation_string"], start, relation["context_end"])
    if aligned is None:
        # Preserve useful context but decline to mark bytes we cannot verify.
        start = max(0, min(int(start), len(text)))
        end = start
    else:
        start, end = aligned
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


def _provision_from_text(text: str, anchor: str) -> dict | None:
    """One provision read straight out of the prose, for a document with no usable
    structural index.

    Bounded by the NEXT heading of the same family ("Article 13" after "Article 12",
    "s. 8" after "s. 7"), so it stops where the provision does rather than running on.
    """
    match = re.match(r"\s*([A-Za-z.\s]{1,20}?)\s*(\d+[A-Za-z]?)\s*$", anchor or "")
    if not text or not match:
        return None
    word, number = match.group(1).strip(), match.group(2)
    stem = re.escape(word).replace(r"\ ", r"\s+")
    start_re = re.compile(rf"(?m)^[ \t]*{stem}\s+{re.escape(number)}\b", re.I)
    found = start_re.search(text)
    if found is None:
        return None
    next_re = re.compile(rf"(?m)^[ \t]*{stem}\s+\d+[A-Za-z]?\b", re.I)
    following = next_re.search(text, found.end())
    body = text[found.start():following.start() if following else len(text)].strip()
    if not body:
        return None
    label = body.splitlines()[0].strip()
    return {"label": label, "text": body, "key": _anchor_key(label) or _slug(label)}


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
        paragraphs.append({"text": chunk.strip(), "indent": indent, "marks": marks})

    # Marks that matched no drafting line are NOT dumped on the last paragraph. They are
    # the ones whose sub-provision could not be located — a citation to "s. 11(4)" of a
    # section that runs (1), (2), (2A), (3), or a pinpoint the extractor mangled — and
    # bunching them at the section foot produced a meaningless row of "[1 mention]"
    # badges, one per misreading, each opening a list of one. Every such citation is
    # already counted under the section's own key (a pinpoint indexes under both its
    # exact anchor and its parent), so dropping the badge here silently rolls it into
    # the broader provision, which is the only place it can honestly be read.
    return paragraphs


@dataclass(frozen=True, slots=True)
class StaticExport:
    html: bytes
    filename: str
    stable_id: str
    documents: int
    mentions: int
    generated_at: str
    title: str = ""


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
        note: str | None = None,
        only: list[str] | None = None,
    ) -> StaticExport:
        """The finished page. ``note`` is the per-edition editorial line a bundle adds
        beneath the shared attribution; ``only`` cuts it down to named provisions."""
        data = self.build_data(
            stable_id, max_snippets=max_snippets, progress=progress, only=only)
        display_title = data["law"]["title"]
        return StaticExport(
            html=render_static_html(data, note=note).encode("utf-8"),
            filename=f"{_slug(display_title)[:80]}.html",
            stable_id=stable_id,
            documents=data["stats"]["documents"],
            mentions=data["stats"]["mentions"],
            generated_at=data["generated_at"],
            title=display_title,
        )

    _COMPARE_CHARS = 24_000

    @classmethod
    def _comparisons(
        cls, cat, textstore, provision_mappings: list[dict],
        previous_laws: dict[str, dict], sections: list[dict],
    ) -> dict[str, list[dict]]:
        """``{current anchor key: [the mapped provision, in full]}``.

        A mapping is an editorial claim that two provisions correspond. Stating the
        claim and its confidence is not the same as showing the reader the two texts, so
        the page carries both — keyed the same way the mention index is, so the dialog
        can offer the comparison exactly where the claim was made.
        """
        by_key: dict[str, list[dict]] = {}
        current_text = {section["key"]: section for section in sections}
        cache: dict[str, dict[str, dict]] = {}

        raw_text: dict[str, str] = {}

        def provisions_of(doc_id: str) -> dict[str, dict]:
            if doc_id in cache:
                return cache[doc_id]
            found: dict[str, dict] = {}
            row = cat.get_document(doc_id)
            if row is not None and row["payload_hash"]:
                try:
                    text = textstore.get(row["payload_hash"])
                    segments = textstore.get_segments(row["payload_hash"])
                except OSError:
                    text, segments = None, []
                if text:
                    raw_text[doc_id] = text
                    if not segments:
                        segments = synthesise_numbered_segments(text)
                    for section in _law_sections(text, segments):
                        found.setdefault(section["key"], section)
            cache[doc_id] = found
            return found

        for mapping in provision_mappings:
            current_anchor = str(mapping.get("current_anchor") or "")
            previous_anchor = str(mapping.get("previous_anchor") or "")
            previous_id = str(mapping.get("previous_doc_id") or "")
            key = _anchor_key(current_anchor) or _slug(current_anchor)
            if not key or not previous_id:
                continue
            previous_key = _anchor_key(previous_anchor) or _slug(previous_anchor)
            previous_section = provisions_of(previous_id).get(previous_key)
            if previous_section is None:
                # A predecessor held without a usable structural index still has its
                # text; read the provision out of the prose rather than showing nothing.
                previous_section = _provision_from_text(
                    raw_text.get(previous_id) or "", previous_anchor)
            here = current_text.get(key)
            law = previous_laws.get(previous_id) or {}
            by_key.setdefault(key, []).append({
                "mapping_id": mapping.get("mapping_id"),
                "mapping_type": mapping.get("mapping_type"),
                "confidence": mapping.get("confidence"),
                "note": mapping.get("note"),
                "current_anchor": current_anchor,
                "current_label": (here or {}).get("label") or current_anchor,
                "current_text": ((here or {}).get("text") or "")[:cls._COMPARE_CHARS],
                "previous_id": previous_id,
                "previous_label": law.get("label")
                or _short_instrument_title(
                    str(mapping.get("previous_title") or previous_id),
                    fallback=previous_id),
                "previous_title": law.get("title") or mapping.get("previous_title"),
                "previous_anchor": previous_anchor,
                "previous_provision_label": (
                    (previous_section or {}).get("label") or previous_anchor),
                "previous_text": (
                    (previous_section or {}).get("text") or "")[:cls._COMPARE_CHARS],
            })
        return by_key

    def _pending_summary(self, stable_id: str) -> dict:
        """Live CJEU proceedings on this instrument, grouped for a one-line summary.

        The reader's box lists them; a static edition cannot afford that much of its
        first screen, and cannot fetch more later. So the page carries counts per kind of
        proceeding ("21 preliminary references (4 with an AG opinion), 6 actions for
        annulment…") with the full list behind a dialog that filters to whichever count
        you click. Empty dict when nothing is pending, so the page omits the line.

        Blocking, and it RAISES rather than returning {} — because "nothing is pending"
        and "I could not find out" render identically, and the edition is a file that
        will not be built again for weeks. The v8 GDPR edition of 2026-08-22 published
        with no pending line at all while 29 references were live before the Court: the
        default call handed back a warming placeholder (2.5s sync_wait; the scan takes
        3.5s on the GDPR's identity set alone) and the placeholder carries no ``pending``
        key, so it read as an empty list. Every other edition in that build was small
        enough to answer in time, which is why nothing looked wrong.
        """
        data = self.facade.pending_references(stable_id, limit=500, blocking=True)
        if data.get("error") == "not found":
            return {}          # build_data raises its own, clearer KeyError below
        if data.get("_warming") or "pending" not in data:
            raise RuntimeError(
                f"pending proceedings for {stable_id} were not computed "
                f"(keys: {sorted(data)!r}) — refusing to publish an edition that would "
                f"read as though nothing is before the Court")
        rows = data.get("pending") or []
        if not rows:
            return {}
        groups: dict[str, dict] = {}
        for row in rows:
            label = row.get("procedure_label") or "Pending proceeding"
            group = groups.setdefault(label, {"label": label, "n": 0, "with_ag": 0,
                                              "preliminary": bool(row.get("preliminary"))})
            group["n"] += 1
            group["with_ag"] += 1 if row.get("ag_opinion") else 0
        return {
            "total": len(rows),
            # References first, then the biggest of the rest — the same order as the list.
            "groups": sorted(groups.values(),
                             key=lambda g: (not g["preliminary"], -g["n"], g["label"])),
            "cases": [{
                "id": r.get("stable_id"),
                "case": r.get("case_number") or r.get("stable_id"),
                "title": re.sub(r"^Pending:\s*", "", str(r.get("title") or "")),
                "label": r.get("procedure_label") or "Pending proceeding",
                "date": r.get("date"),
                "court": r.get("referring_court"),
                "ag": bool(r.get("ag_opinion")),
                # The Opinion's own id, so the page can link straight to it. Its CELEX by
                # preference — the corpus holds many of these under an ECLI, which EUR-Lex
                # also resolves, but the CELEX is the descriptor that was actually matched
                # (an urgent reference gets a View, CV, not an Opinion, CC).
                "ag_id": ((r.get("ag_opinion") or {}).get("celex")
                          or (r.get("ag_opinion") or {}).get("stable_id")),
                "anchors": r.get("anchors") or [],
            } for r in rows],
        }

    def build_data(
        self,
        stable_id: str,
        *,
        max_snippets: int = 4,
        progress: Callable[[int, int], None] | None = None,
        only: list[str] | None = None,
    ) -> dict:
        """Everything the page shows, as data. Kept separate from rendering because it is
        the expensive half (thousands of source texts) and therefore the half worth
        caching: attribution, editorial notes and template changes then re-render for
        free from a payload built hours ago.

        ``only`` is a list of provision labels ("Article 101", "s. 5") and makes a
        SUBSET edition: those provisions, and only the mentions of those provisions. It
        is a size decision, not a display one. The TFEU carries 191,183 incoming edges
        and the TEU 47,968 — a whole-instrument edition of either is tens of megabytes
        of excerpts, most of them about Articles nobody opened the page for. Filtering
        in the browser would not help, because by then the bytes have already been
        downloaded; the cut has to happen here, before the excerpts are ever read.
        """
        max_snippets = max(1, min(int(max_snippets), 12))
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        # Computed BEFORE the catalogue is opened below: it opens its own connection,
        # and taking a second one from inside the first can only ever hurt (a
        # single-connection pool would deadlock outright).
        pending_summary = self._pending_summary(stable_id)

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
            inherited_recitals = self.facade._inherited_recitals(
                cat, textstore, stable_id, include_citations=False)
            if inherited_recitals:
                from types import SimpleNamespace

                recital_sections = _law_sections(
                    inherited_recitals["text"],
                    [SimpleNamespace(**{
                        key: segment[key]
                        for key in (
                            "label", "kind", "level", "char_start", "char_end",
                        )
                    }) for segment in inherited_recitals["segments"]],
                )
                for section in recital_sections:
                    section["inherited_recital"] = True
                    section["source_stable_id"] = \
                        inherited_recitals["source_stable_id"]
                sections = [*recital_sections, *sections]

            # The subset cut, taken BEFORE any citer is read: everything downstream —
            # counts, groups, excerpts, comparisons — is derived from the relations left
            # standing here, so a four-Article edition of the TFEU costs four Articles'
            # worth of work and bytes rather than 358.
            wanted_keys: set[str] | None = None
            if only:
                wanted_keys = {
                    key for key in
                    (_anchor_key(str(label)) or _slug(str(label)) for label in only
                     if str(label or "").strip())
                    if key
                }
                kept = [s for s in sections if s["key"] in wanted_keys]
                if not kept:
                    known = ", ".join(s["label"] for s in sections[:8])
                    raise ValueError(
                        f"none of {only!r} name a provision of {stable_id}; "
                        f"it begins {known}")
                sections = kept

            relations = [
                dict(relation) for relation in cat.relations_to(stable_id)
                if relation["extracted_via"] != "inferred"
                and relation["src_id"] != stable_id
            ]
            direct_keys = {
                (r["src_id"], r["dst_anchor"], r["context_start"], r["context_end"])
                for r in relations
            }
            # Unbounded, unlike the reader's paged view: an edition PRINTS the counts,
            # so a bound here is not a page size but a silent deletion — and because the
            # projection is ordered by the citer's PageRank, what a bound deletes is
            # precisely the obscure long tail an offline edition exists to preserve.
            for inherited in cat.version_inherited_mentions_for(
                    stable_id, limit=None):
                projected = dict(inherited)
                key = (
                    projected["src_id"], projected["dst_anchor"],
                    projected["context_start"], projected["context_end"],
                )
                if key in direct_keys:
                    continue
                projected["dst_id"] = stable_id
                projected["is_version_inherited"] = True
                relations.append(projected)
            provision_mappings = [dict(row) for row in cat.provision_mappings(stable_id)]
            # Every predecessor instrument a mention can arrive through, named. The page
            # says "12 mentions of a similar provision in Directive 95/46/EC", not "via
            # previous law", so each route needs an identity, not just a count.
            previous_laws: dict[str, dict] = {}
            for inherited in cat.inherited_mentions_for(stable_id, limit=None):
                projected = dict(inherited)
                # Index the literal old-law citation under the CURRENT provision while
                # retaining its route/provenance for the separate filter and explanation.
                projected["dst_anchor"] = projected["inherited_current_anchor"]
                projected["is_inherited"] = True
                previous_id = str(projected.get("inherited_from_id") or "").strip()
                if previous_id and previous_id not in previous_laws:
                    previous_title = projected.get("inherited_from_title") or previous_id
                    previous_laws[previous_id] = {
                        "id": previous_id,
                        "title": str(previous_title),
                        "label": _short_instrument_title(
                            str(previous_title), fallback=previous_id),
                    }
                relations.append(projected)
            # The two provisions a mapping ASSERTS are similar, both in full, so a
            # reader confronted with "12 mentions of a similar provision in Directive
            # 95/46/EC" can judge the claim instead of taking it on trust.
            comparisons = self._comparisons(
                cat, textstore, provision_mappings, previous_laws, sections)
            relations = self.facade._collapse_version_citers(cat, relations)
            if wanted_keys is not None:
                # An anchorless mention cites the instrument, not a provision of it, so
                # it has no place in an edition that IS a provision — and it is the bulk
                # of the weight the subset exists to shed.
                relations = [
                    relation for relation in relations
                    if relation["dst_anchor"]
                    and (_anchor_key(relation["dst_anchor"])
                         or _slug(relation["dst_anchor"])) in wanted_keys
                ]

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
            # One round trip for every citer, like authority_for above — not a
            # single-row query per citer. A heavily-cited Act has thousands of them,
            # and the export walks every held document, so the N+1 was paid per
            # edition per run.
            source_rows = cat.get_documents(list(by_source))

            groups: list[dict] = []
            source_items = list(by_source.items())
            total_sources = len(source_items)
            for position, (source_id, source_relations) in enumerate(source_items, 1):
                row = source_rows.get(source_id)
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
                # {anchor key: {predecessor id: mentions}} — only for the documents that
                # actually arrive by a mapping, so an ordinary citer carries nothing.
                previous_by_key: dict[str, dict[str, int]] = {}
                version_mentions_by_key: dict[str, int] = {
                    "all": sum(bool(r.get("is_version_inherited"))
                               for r in source_relations)}
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
                    previous_id = (
                        str(relation.get("inherited_from_id") or "").strip()
                        if relation.get("is_inherited") else ""
                    )
                    if previous_id:
                        for key in ("all", *keys):
                            bucket = previous_by_key.setdefault(key, {})
                            bucket[previous_id] = bucket.get(previous_id, 0) + 1
                    for key in keys:
                        mentions_by_key[key] = mentions_by_key.get(key, 0) + 1
                        if relation.get("is_inherited"):
                            inherited_mentions_by_key[key] = \
                                inherited_mentions_by_key.get(key, 0) + 1
                        if relation.get("is_version_inherited"):
                            version_mentions_by_key[key] = \
                                version_mentions_by_key.get(key, 0) + 1
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
                    "previous_mentions_by_key": previous_by_key,
                    "version_mentions_by_key": version_mentions_by_key,
                    "has_inherited": bool(inherited_mentions_by_key["all"]),
                    "has_version_inherited": bool(version_mentions_by_key["all"]),
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
        # Documents, not mentions — the same unit as ``counts``, so a badge reading
        # "[41 direct mentions] [12 mentions of a similar provision in …]" always adds up
        # against the section's own total.
        direct_counts: dict[str, int] = {}
        previous_counts: dict[str, dict[str, int]] = {}
        for group in groups:
            previous_for_group = group["previous_mentions_by_key"]
            for key, total in group["mentions_by_key"].items():
                if total > group["inherited_mentions_by_key"].get(key, 0):
                    direct_counts[key] = direct_counts.get(key, 0) + 1
            for key, by_law in previous_for_group.items():
                for law_id in by_law:
                    per_law = previous_counts.setdefault(law_id, {})
                    per_law[key] = per_law.get(key, 0) + 1
        # Order the routes the way the page lists them: the most-used predecessor first.
        ordered_previous = sorted(
            previous_laws.values(),
            key=lambda law: (-(previous_counts.get(law["id"], {}).get("all", 0)),
                             law["label"]),
        )
        for section in sections:
            marks = [
                {"key": key, "label": label, "count": counts.get(key, 0)}
                for key, label in anchor_marks_by_base.get(section["key"], {}).items()
                if key.startswith("exact:") and counts.get(key, 0)
            ]
            section["paragraphs"] = _section_paragraphs(section, marks)

        target_cite = _oscola_cite(target, target_meta)
        target_links = _public_links(self.facade, target, target_meta)
        target_jurisdiction = self.facade._doc_bucket(
            target["source"], target["court"])
        jurisdictions = {g["jurisdiction"] for g in groups if g["jurisdiction"]}
        if target_jurisdiction:
            jurisdictions.add(target_jurisdiction)
        data = {
            "generated_at": generated_at,
            "law": {
                "stable_id": stable_id,
                "title": display_title,
                "short_title": _short_instrument_title(
                    display_title, fallback=stable_id),
                "jurisdiction": target_jurisdiction,
                # The renderer already reads this for <html lang> and schema.org
                # inLanguage — it was just never set, so every export declared itself
                # English. A 2.6 MB Code des postes with 70,209 accented characters and
                # lang="en" is wrong for a screen reader, for hyphenation, and for
                # anything indexing it.
                "language": target["language"] or None,
                "cite": target_cite.get("text") or target["title"] or stable_id,
                "source": self.facade.source_label(target["source"]),
                "links": target_links,
                # A static page must say which publisher edition it contains.  The
                # bundle refresh checks legislation.gov.uk immediately before build;
                # carry both that check date and the publisher's currency date into
                # the self-contained page instead of leaving them only in the job log.
                "currency": {
                    "as_at": ((target_meta.get("currency") or {}).get("as_at")),
                    "up_to_date": ((target_meta.get("currency") or {}).get(
                        "up_to_date")),
                    "unapplied_count": ((target_meta.get("currency") or {}).get(
                        "unapplied_count")),
                    "source_last_modified": target_meta.get("source_last_modified"),
                    "checked_at": generated_at,
                } if (
                    target_meta.get("currency") or target_meta.get("source_last_modified")
                ) else None,
                "sections": sections,
                "provision_mappings": provision_mappings,
                "inherited_recitals": (
                    {
                        **{
                            key: inherited_recitals[key]
                            for key in (
                                "count", "source_stable_id", "source_title",
                                "source_url", "base_stable_id", "source_is_base_act",
                                "unchanged", "virtual", "note",
                            )
                        },
                        # The source's citable stem. Printing the official title in full
                        # put a 40-word sentence — "…on the protection of natural persons
                        # with regard to the processing of personal data and on the free
                        # movement of such data, and repealing Directive 95/46/EC (Text
                        # with EEA relevance)" — inside a one-line provenance note.
                        "source_label": _short_instrument_title(
                            inherited_recitals.get("source_title") or "",
                            fallback=str(inherited_recitals.get("source_stable_id") or ""),
                        ),
                    }
                    if inherited_recitals else None
                ),
            },
            # What is still before the Court, as a SUMMARY plus the list behind it. A
            # static edition is a snapshot, so this is dated by "generated_at" like
            # everything else here — but a reader holding the offline text still needs to
            # know the instrument has live questions on it, and which provisions they are
            # about. Unlike the EU↔UK counterpart link, this needs no server: it is data.
            "pending": pending_summary,
            "groups": groups,
            "index": index,
            "counts": counts,
            "inherited_counts": inherited_counts,
            "direct_counts": direct_counts,
            "previous_laws": ordered_previous,
            "previous_counts": previous_counts,
            "comparisons": comparisons,
            "stats": {
                "documents": len(groups),
                "mentions": len(relations),
                "with_public_url": sum(bool(group["links"]) for group in groups),
                "with_snippet": sum(bool(group["snippets"]) for group in groups),
            },
            "flags": _flag_assets(jurisdictions),
        }
        if wanted_keys is not None:
            # Said out loud, and carried in the payload rather than inferred from a short
            # section list: a reader must never take a subset edition for the instrument.
            data["subset"] = [s["label"] for s in sections]
        return data

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


def subset_token(only: list[str] | None) -> str:
    """A stable, order-independent name for a subset selection, or "" for the whole
    instrument. Part of the cache identity: a four-Article edition of the TFEU and the
    whole TFEU are different documents, and without this the one written second would
    silently serve as the other."""
    labels = sorted({str(label).strip() for label in (only or []) if str(label).strip()})
    if not labels:
        return ""
    return "-only-" + hashlib.sha256(
        "\n".join(labels).encode("utf-8")).hexdigest()[:10]


def static_export_cache_path(
    config: Config, stable_id: str, *, max_snippets: int = 4,
    only: list[str] | None = None,
) -> Path:
    """Where the built PAYLOAD for one edition lives. It holds the expensive half \u2014 the
    law, its citing documents and their excerpts \u2014 and the page is rendered from it on
    download, so editing the attribution or an editorial note costs nothing."""
    identity = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:12]
    return (
        config.data_dir / "exports" / "cache"
        / (
            f"{_slug(stable_id)[:80]}-{identity}"
            f"-v{_CACHE_VERSION}-snippets-{max(1, min(int(max_snippets), 12))}"
            f"{subset_token(only)}.data.json"
        )
    )


def static_export_manifest_path(path: Path) -> Path:
    return path.with_name(path.name.removesuffix(".data.json") + ".meta.json")


def static_export_status(
    config: Config, stable_id: str, *, max_snippets: int = 4,
    only: list[str] | None = None,
) -> dict:
    path = static_export_cache_path(
        config, stable_id, max_snippets=max_snippets, only=only)
    manifest_path = static_export_manifest_path(path)
    if not path.is_file() or not manifest_path.is_file():
        return {"ready": False, "stable_id": stable_id, "max_snippets": max_snippets}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"ready": False, "stable_id": stable_id, "max_snippets": max_snippets}
    return {**manifest, "ready": True, "_path": str(path)}


def cached_export_page_title(
    status: dict, *, short: str | None = None, index_title: str | None = None
) -> str:
    """``Crossreferenced AI Act - UCL Digital Laws`` — the short name of the law this
    edition crossreferences, and the set it was published in. Falls back through the
    operator's own shorthand, the instrument's short title, and finally its full one."""
    name = (short or "").strip() or str(
        status.get("short_title") or status.get("title") or status.get("stable_id") or ""
    ).strip()
    label = f"Crossreferenced {name}".strip()
    index_title = (index_title or "").strip()
    return f"{label} - {index_title}" if index_title else label


def render_cached_export(
    status: dict, *, note: str | None = None, index_link: dict | None = None,
    page_title: str | None = None, short_title: str | None = None,
    canonical: str | None = None,
) -> bytes:
    """Render the page for a cached edition, applying the CURRENT attribution, this
    edition's own note, and (for a bundle item) the way back to its index page.
    ``status`` is a ready :func:`static_export_status`."""
    page, _assets = _render_cached_export(
        status, note=note, index_link=index_link, page_title=page_title,
        short_title=short_title, canonical=canonical)
    return page


def render_cached_export_assets(
    status: dict, *, asset_prefix: str, note: str | None = None,
    index_link: dict | None = None, page_title: str | None = None,
    short_title: str | None = None, canonical: str | None = None,
) -> tuple[bytes, dict[str, bytes]]:
    """Published page plus independently fetchable citation-data sidecars."""
    return _render_cached_export(
        status, note=note, index_link=index_link, page_title=page_title,
        short_title=short_title, canonical=canonical, asset_prefix=asset_prefix)


def _render_cached_export(
    status: dict, *, note: str | None = None, index_link: dict | None = None,
    page_title: str | None = None, short_title: str | None = None,
    canonical: str | None = None, asset_prefix: str | None = None,
) -> tuple[bytes, dict[str, bytes]]:
    payload = Path(status["_path"]).read_text(encoding="utf-8")
    assets: dict[str, bytes] = {}
    page = render_static_page(
        title=status.get("title") or status["stable_id"],
        data_json=payload,
        note=note,
        index_link=index_link,
        page_title=page_title or cached_export_page_title(status),
        short_title=short_title or status.get("short_title") or None,
        canonical=canonical,
        asset_prefix=asset_prefix,
        _asset_sink=assets,
    ).encode("utf-8")
    return page, assets


def public_base_url() -> str | None:
    """The site's own address, when it has been configured.

    A canonical link and a sitemap both need ABSOLUTE urls, and there is no honest way to
    guess one from a folder of files. Unset means the SEO block omits them rather than
    inventing a hostname — a wrong canonical is worse than none, because it tells a search
    engine the real page is somewhere it isn't.
    """
    for key in ("RAGLEX_STATIC_BASE_URL", "RAGLEX_PUBLIC_URL"):
        value = (os.environ.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value.rstrip("/")
    return None


def build_static_export_cache(
    facade: Facade,
    stable_id: str,
    *,
    max_snippets: int = 4,
    on_progress: Callable[..., None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    only: list[str] | None = None,
) -> dict:
    """Build and atomically publish the cached payload the download API renders from."""
    max_snippets = max(1, min(int(max_snippets), 12))

    def progress(done: int, total: int) -> None:
        if cancel_check and cancel_check():
            raise RuntimeError("static export cancelled")
        if on_progress:
            on_progress(
                stage="reading excerpts", done=done, total=total,
                item=f"{done:,} of {total:,} citing documents",
            )

    data = StaticLawExporter(facade=facade).build_data(
        stable_id, max_snippets=max_snippets, progress=progress, only=only)
    title = data["law"]["title"]
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path = static_export_cache_path(
        facade.config, stable_id, max_snippets=max_snippets, only=only)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    manifest = {
        "stable_id": stable_id,
        "max_snippets": max_snippets,
        "title": title,
        # The index page groups by jurisdiction and prints both totals; carrying them in
        # the manifest keeps that page cheap — it never re-reads a multi-megabyte payload.
        "jurisdiction": data["law"].get("jurisdiction") or "",
        "short_title": data["law"].get("short_title") or "",
        "filename": f"{_slug(title)[:80]}.html",
        "documents": data["stats"]["documents"],
        "mentions": data["stats"]["mentions"],
        # What is still before the Court, for the index page — which reads this manifest
        # and never the payload, and would otherwise have to open tens of megabytes per
        # edition to count them.
        "pending": int((data.get("pending") or {}).get("total") or 0),
        "bytes": len(payload.encode("utf-8")),
        "generated_at": data["generated_at"],
        # Present only on a subset edition, so the index page and the status line can
        # say which provisions this one holds instead of implying the whole instrument.
        **({"subset": data["subset"]} if data.get("subset") else {}),
    }
    manifest_path = static_export_manifest_path(path)
    manifest_temporary = manifest_path.with_suffix(".tmp")
    manifest_temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_temporary.replace(manifest_path)
    return manifest


# Fields the cached payload carries for the builder's own bookkeeping — the manifest, the
# status line, a future consumer — but that the page script never reads. They are dropped
# on the way INTO the page, not on the way into the cache: the cache stays the full record
# (``static_export_status`` reads ``stats`` straight out of it), and every edition already
# built gets the smaller file on its next render instead of an hours-long rebuild.
_PAGE_UNUSED_GROUP_FIELDS = frozenset({
    "doc_type", "has_inherited", "has_version_inherited", "target_keys",
    "whole_instrument", "relationships", "has_text", "raw_ext", "source", "mentions",
})
_PAGE_UNUSED_SNIPPET_FIELDS = frozenset({"raw"})
_PAGE_UNUSED_SECTION_FIELDS = frozenset({"kind"})
_PAGE_UNUSED_TOP_FIELDS = frozenset({"stats", "inherited_counts"})


def _slim_snippet(snippet: dict, link_urls: list[str]) -> dict:
    """One excerpt, without the bytes the page never reads.

    An excerpt's "try exact passage" link is its document's own URL with a text fragment
    on the end, so every excerpt of the same document repeats that URL in full — 5.6 MB of
    it on the GDPR edition. Where the head is a URL the row already carries, it becomes
    ``[index, fragment]`` and the script joins the two back together; anything else is
    left exactly as it was.
    """
    row = {k: v for k, v in snippet.items() if k not in _PAGE_UNUSED_SNIPPET_FIELDS}
    url = row.get("passage_url")
    if isinstance(url, str) and url:
        for index, head in enumerate(link_urls):
            if head and url.startswith(head) and len(url) > len(head):
                row["passage_url"] = [index, url[len(head):]]
                break
    return row


def _slim_for_page(data: dict) -> dict:
    """The same page, with the bytes it never reads left out.

    Two thirds of an edition is citing-document rows and the law's own text, and both
    carried a duplicate: every section shipped its text twice (once whole, once split into
    the ``paragraphs`` the script actually renders), and every excerpt shipped its raw
    form beside the cleaned one. On a 2.2 MB edition that is ~470 kB — a fifth of the file
    — for nothing. Shallow-copied, never mutated in place: ``build`` renders from the same
    dict it then reads its own statistics out of.
    """
    slim = {k: v for k, v in data.items() if k not in _PAGE_UNUSED_TOP_FIELDS}

    law = dict(slim.get("law") or {})
    law.pop("provision_mappings", None)   # the page reads the derived ``comparisons``
    sections = []
    for section in law.get("sections") or []:
        row = {k: v for k, v in section.items()
               if k not in _PAGE_UNUSED_SECTION_FIELDS}
        # ``section.text`` is only ever a fallback for a section that arrived without
        # paragraphs; where they exist it is the same prose a second time.
        if row.get("paragraphs"):
            row.pop("text", None)
        sections.append(row)
    if sections:
        law["sections"] = sections
    slim["law"] = law

    groups = []
    for group in slim.get("groups") or []:
        row = {k: v for k, v in group.items()
               if k not in _PAGE_UNUSED_GROUP_FIELDS}
        snippets = row.get("snippets")
        if snippets:
            urls = [str(link.get("url") or "") for link in (row.get("links") or [])]
            row["snippets"] = [
                _slim_snippet(snippet, urls) for snippet in snippets
            ]
        groups.append(row)
    if groups:
        slim["groups"] = groups
    return slim


def _escape_for_script(payload: str) -> str:
    # A source title or snippet containing ``</script>`` must remain inert data.
    return (
        payload
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _json_for_script(data: dict) -> str:
    return _escape_for_script(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def _slim_payload_json(data_json: str) -> str:
    """``_slim_for_page`` for a payload that is already a string.

    The cached path used to hand the payload straight through, on the reasoning that a
    payload of tens of megabytes should not be re-parsed. It is worth one parse: the round
    trip runs at roughly a hundred megabytes a second, and it takes a fifth off the file
    every reader downloads and every browser then has to parse itself. A payload this
    cannot make sense of is passed through untouched — a smaller page is never worth a
    failed build."""
    try:
        return _json_dump(_slim_for_page(json.loads(data_json)))
    except Exception:  # noqa: BLE001 — see above: optimisation, not correctness
        return data_json


_PAGE_GROUP_CHUNK_SIZE = 400
_PRIORITY_PACK_LIMIT = 80


_PROJECTED_GROUP_MAPS = (
    "mentions_by_key", "inherited_mentions_by_key", "previous_mentions_by_key",
    "version_mentions_by_key", "labels_by_key",
)


def _project_group_for_key(group: dict, key: str) -> dict:
    """The complete display row for ONE provision, without unrelated excerpts.

    A single guidance document can mention ninety GDPR provisions and carry sixty-four
    excerpts. Copying that whole row into a paragraph-priority pack made a 45-document
    Article 24(2) result weigh 812 KB. Keeping just that paragraph's maps and excerpts is
    56 KB, while the canonical background chunk remains the one deduplicated full copy.
    """
    out = {name: value for name, value in group.items()
           if name not in {*_PROJECTED_GROUP_MAPS, "snippets", "snippet_indices"}}
    for field in _PROJECTED_GROUP_MAPS:
        source = group.get(field) or {}
        out[field] = {key: source[key]} if key in source else {}
    snippets = group.get("snippets") or []
    old_indices = (group.get("snippet_indices") or {}).get(key) or []
    out["snippets"] = [snippets[index] for index in old_indices
                       if isinstance(index, int) and 0 <= index < len(snippets)]
    out["snippet_indices"] = {key: list(range(len(out["snippets"])))}
    return out


def _section_priority_keys(base_key: str, index: dict) -> list[str]:
    """Keys whose results belong to the same visible Article/Recital disclosure."""
    if base_key.startswith("art:"):
        stem = "exact:art" + base_key.split(":", 1)[1] + "("
        bare = "bare:" + base_key
    elif base_key.startswith("rec:"):
        stem = "exact:rec" + base_key.split(":", 1)[1] + "("
        bare = "bare:" + base_key
    else:
        return [base_key] if base_key in index else []
    related = [base_key, bare]
    related.extend(key for key in index if key.startswith(stem))
    return [key for key in dict.fromkeys(related) if key in index]


def _deferred_page_payloads(
    data_json: str, *, asset_prefix: str | None = None,
) -> tuple[str, str, dict[str, bytes]]:
    """Small immediately parsed payload plus idle/on-demand citing-document chunks.

    A GDPR edition carries nearly 30,000 citing documents and about 115 MB of excerpts.
    Parsing that monolith before replacing each provision's ``Loading…`` marker froze the
    browser before the law became interactive. The law, counts and three leading citers
    per provision are now the small core. A standalone download keeps bounded blocks in
    the HTML; a published bundle writes cacheable sidecars and tiny provision-first packs.
    """
    try:
        page = _slim_for_page(json.loads(data_json))
    except Exception:  # noqa: BLE001 — correctness fallback for an unexpected cache
        return _escape_for_script(data_json), "", {}
    groups = list(page.pop("groups", []) or [])
    index = page.get("index") or {}

    # Provision headings need names immediately, but only the leading three. Everything
    # else belongs to the dialog and can wait for its chunk. Preserve the existing index
    # order (already authority-ranked) and count DOCUMENTS, as the prose does.
    visible_keys = {str(section.get("key") or "")
                    for section in ((page.get("law") or {}).get("sections") or [])}
    annotations: dict[str, dict] = {}
    for key in visible_keys:
        leaders = []
        direct_count = 0
        for group_index in index.get(key) or []:
            try:
                group = groups[int(group_index)]
            except (IndexError, TypeError, ValueError):
                continue
            total = int((group.get("mentions_by_key") or {}).get(key) or 0)
            inherited = int((group.get("inherited_mentions_by_key") or {}).get(key) or 0)
            if total <= inherited:
                continue
            direct_count += 1
            if len(leaders) < 3:
                leaders.append({k: group.get(k) for k in ("id", "cite", "title")})
        if direct_count:
            annotations[key] = {"direct": leaders, "direct_count": direct_count}
    page["annotation_citers"] = annotations
    page["groups"] = []

    # Put anchored documents first in the byte stream. Whole-instrument-only rows are
    # useful when somebody asks for All mentions, but they must not delay Article 5.
    anchored: list[tuple[int, dict]] = []
    general: list[tuple[int, dict]] = []
    for group_index, group in enumerate(groups):
        keys = set((group.get("mentions_by_key") or {}).keys()) - {"all", "whole"}
        (anchored if keys else general).append((group_index, group))
    ordered = anchored + general
    chunks = [ordered[i:i + _PAGE_GROUP_CHUNK_SIZE]
              for i in range(0, len(ordered), _PAGE_GROUP_CHUNK_SIZE)]
    group_chunks = [0] * len(groups)
    for chunk_index, chunk in enumerate(chunks):
        for group_index, _group in chunk:
            group_chunks[group_index] = chunk_index
    page["group_chunks"] = group_chunks
    page["chunk_count"] = len(chunks)
    page["anchored_chunk_count"] = (
        (len(anchored) + _PAGE_GROUP_CHUNK_SIZE - 1) // _PAGE_GROUP_CHUNK_SIZE)

    assets: dict[str, bytes] = {}
    if asset_prefix:
        prefix = asset_prefix.rstrip("/")
        page["chunk_urls"] = []
        for chunk_index, chunk in enumerate(chunks):
            filename = f"{prefix}/c{chunk_index:03d}.json"
            page["chunk_urls"].append(filename)
            assets[filename] = _json_dump(chunk).encode("utf-8")

        # One small file per visible Article/Recital. It contains the first page of that
        # section plus each of its subparagraphs. A click on Article 24(2) therefore also
        # warms 24(1) and 24(3), without creating a thousand tiny files or duplicating the
        # full multi-provision source rows.
        priority_urls: dict[str, str] = {}
        visible_keys = [str(section.get("key") or "") for section in
                        ((page.get("law") or {}).get("sections") or [])]
        for base_key in dict.fromkeys(visible_keys):
            related = _section_priority_keys(base_key, index)
            if not related:
                continue
            packs = {}
            for key in related:
                ids = list(index.get(key) or [])
                selected = ids[:_PRIORITY_PACK_LIMIT]
                packs[key] = {
                    "complete": len(selected) == len(ids),
                    "rows": [[group_index, _project_group_for_key(groups[group_index], key)]
                             for group_index in selected],
                }
            digest = hashlib.sha256(base_key.encode("utf-8")).hexdigest()[:12]
            filename = f"{prefix}/p-{digest}.json"
            assets[filename] = _json_dump({"packs": packs}).encode("utf-8")
            for key in related:
                priority_urls[key] = filename
        page["priority_pack_urls"] = priority_urls
        chunk_html = ""
    else:
        chunk_html = "\n".join(
            f'  <script id="raglex-chunk-{chunk_index}" type="application/json">'
            f'{_json_for_script(chunk)}</script>'
            for chunk_index, chunk in enumerate(chunks)
        )
    return _json_for_script(page), chunk_html, assets


def _json_dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


#: Jurisdiction code → the name a search actually uses. "uk" is not a word anybody types
#: into a search box; "United Kingdom" is, and it is what schema.org wants for
#: legislationJurisdiction.
_JURISDICTION_NAMES = {
    "uk": "United Kingdom", "eu": "European Union", "ie": "Ireland",
    "fr": "France", "de": "Germany", "nl": "Netherlands", "be": "Belgium",
    "us": "United States", "ca": "Canada", "au": "Australia", "nz": "New Zealand",
    "hk": "Hong Kong", "sg": "Singapore", "in": "India", "za": "South Africa",
}


def _meta_description(data: dict) -> str:
    """One sentence that says what this page IS, for the search result snippet.

    Written from the instrument's own opening words rather than a template, because a
    result that reads "Data Protection Act 2018 — Part 1 Preliminary. This Act makes
    provision about the processing of personal data…" earns a click and
    "Citations for ukpga/2018/12" does not. Falls back to naming the document when it has
    no readable opening.
    """
    law = data.get("law") or {}
    title = _display_title(law.get("title")) or law.get("stable_id") or "Document"
    opening = ""
    for section in (law.get("sections") or []):
        body = " ".join(str(section.get("text") or "").split())
        if len(body) > 40:
            opening = body
            break
    if not opening:
        counts = (data.get("counts") or {}).get("all") or 0
        return (f"{title} — full text with the documents that cite it"
                + (f" ({counts:,} citing documents)." if counts else "."))
    room = 300 - len(title) - 3
    if len(opening) > room:
        opening = opening[:room].rsplit(" ", 1)[0] + "…"
    return f"{title} — {opening}"


def _meta_keywords(data: dict) -> str:
    """Terms this page is genuinely about: the instrument, its jurisdiction, and the
    provisions it contains. Not a keyword stuff — search engines discount that — but the
    citable labels ("s. 45", "Article 15") are what people actually search for."""
    law = data.get("law") or {}
    terms: list[str] = []
    for value in (law.get("short_title"), law.get("cite"), law.get("stable_id")):
        if value and str(value) not in terms:
            terms.append(str(value))
    juris = _JURISDICTION_NAMES.get(str(law.get("jurisdiction") or "").lower())
    if juris:
        terms.append(juris)
    short = law.get("short_title") or ""
    for section in (law.get("sections") or [])[:24]:
        label = str(section.get("label") or "").strip()
        if label and label.lower() not in ("opening text", "document"):
            terms.append(f"{short} {label}".strip())
    return ", ".join(dict.fromkeys(t for t in terms if t))[:900]


def _structured_data(data: dict, canonical: str | None) -> str:
    """schema.org JSON-LD, so a search engine knows this is legislation and not a blog.

    ``Legislation`` is the type for an instrument and carries exactly the fields the
    corpus already holds — identifier, jurisdiction, the official source. A judgment or
    a report has no equivalent schema.org type, so those get ``Article`` with the same
    provenance. The breadcrumb makes the set → instrument path explicit rather than
    leaving it to be guessed from a URL.
    """
    law = data.get("law") or {}
    title = _display_title(law.get("title")) or law.get("stable_id") or "Document"
    kinds = {str(s.get("kind") or "") for s in (law.get("sections") or [])}
    is_legislation = bool(kinds & {"section", "article", "regulation", "paragraph",
                                   "schedule", "part", "chapter"})
    node: dict = {
        "@context": "https://schema.org",
        "@type": "Legislation" if is_legislation else "Article",
        "name": title,
        "headline": title[:110],
        "description": _meta_description(data),
        "inLanguage": law.get("language") or "en",
        "isAccessibleForFree": True,
    }
    if canonical:
        node["url"] = canonical
        node["mainEntityOfPage"] = canonical
    if law.get("stable_id"):
        node["identifier"] = law["stable_id"]
    if law.get("cite"):
        node["alternateName"] = law["cite"]
    if is_legislation:
        node["legislationIdentifier"] = law.get("cite") or law.get("stable_id")
        juris = _JURISDICTION_NAMES.get(str(law.get("jurisdiction") or "").lower())
        if juris:
            node["legislationJurisdiction"] = juris
    # The official copy this text was taken from: provenance a reader (and a crawler
    # judging authoritativeness) should be able to follow.
    links = [l for l in (law.get("links") or []) if l.get("url")]
    if links:
        node["sameAs"] = [l["url"] for l in links[:5]]
        node["publisher"] = {"@type": "Organization", "name": links[0].get("label") or "Source"}
    nodes = [node]
    crumbs = [{"@type": "ListItem", "position": 1, "name": title}]
    if canonical:
        crumbs[0]["item"] = canonical
    nodes.append({"@context": "https://schema.org", "@type": "BreadcrumbList",
                  "itemListElement": crumbs})
    return "\n".join(
        f'  <script type="application/ld+json">{_escape_for_script(json.dumps(n, ensure_ascii=False))}</script>'
        for n in nodes)


def _seo_head(data: dict, *, page_title: str, canonical: str | None) -> str:
    """The <head> block: what this page is, for machines. Nothing here is visible."""
    law = data.get("law") or {}
    desc = _meta_description(data)
    esc = lambda v: html.escape(str(v or ""), quote=True)  # noqa: E731
    out = [
        f'  <meta name="description" content="{esc(desc)}">',
        f'  <meta name="keywords" content="{esc(_meta_keywords(data))}">',
        '  <meta name="robots" content="index, follow, max-snippet:-1, '
        'max-image-preview:large">',
    ]
    if canonical:
        out.append(f'  <link rel="canonical" href="{esc(canonical)}">')
        out.append(f'  <meta property="og:url" content="{esc(canonical)}">')
    out += [
        f'  <meta property="og:title" content="{esc(page_title)}">',
        f'  <meta property="og:description" content="{esc(desc)}">',
        '  <meta property="og:type" content="article">',
        '  <meta property="og:site_name" content="RagLex">',
        '  <meta name="twitter:card" content="summary">',
        f'  <meta name="twitter:title" content="{esc(page_title)}">',
        f'  <meta name="twitter:description" content="{esc(desc)}">',
    ]
    # The instrument's own citation, for the citation-aware crawlers that read it.
    if law.get("cite"):
        out.append(f'  <meta name="citation_title" content="{esc(law.get("title"))}">')
    out.append(_structured_data(data, canonical))
    return "\n".join(out)


def _prerendered_law(data: dict) -> tuple[str, str]:
    """The contents nav and the law's text as REAL HTML, mirroring what the script builds.

    The page is otherwise a JSON payload plus a renderer, so with JavaScript switched off
    — or to a crawler that does not run it — the whole instrument is a blank <main>. The
    text is already in the file; this is the same text as markup, so it can be read,
    indexed and linked to.

    The script clears both containers before it renders, so a browser never sees this: it
    is replaced within a frame by the identical interactive version. Nothing is served to
    a crawler that is not served to a reader — it is the same content, rendered twice, and
    the duplicate costs almost nothing over the wire because it gzips against the copy in
    the payload.
    """
    law = data.get("law") or {}
    sections = law.get("sections") or []
    nav: list[str] = []
    source_bits = [
        '<a href="{url}" target="_blank" rel="noopener noreferrer">{label} →</a>'.format(
            url=html.escape(str(link.get("url") or ""), quote=True),
            label=html.escape(str(link.get("label") or "source")),
        )
        for link in (law.get("links") or []) if link.get("url")
    ]
    currency = law.get("currency") or {}
    currency_line = ""
    if currency.get("as_at"):
        as_at = html.escape(str(currency["as_at"]))
        checked = html.escape(str(
            currency.get("checked_at") or data.get("generated_at") or "")[:10])
        effects = ""
        unapplied = int(currency.get("unapplied_count") or 0)
        if currency.get("up_to_date") is False and unapplied:
            effects = (
                f" · {unapplied:,} publisher-recorded "
                f"{'effect' if unapplied == 1 else 'effects'} not yet applied to the text"
            )
        currency_line = (
            f'<br><span class="muted">Publisher text current to '
            f'<time datetime="{as_at}">{as_at}</time>; checked for updates '
            f"{checked}{effects}.</span>"
        )
    source_note = (
        '<p class="source-note">' + " · ".join(source_bits) + currency_line + "</p>"
        if source_bits or currency_line else ""
    )
    body: list[str] = [source_note] if source_note else []
    for section in sections:
        sid = html.escape(str(section.get("id") or ""), quote=True)
        label = html.escape(str(section.get("label") or ""))
        level = min(2, int(section.get("level") or 0))
        mention_count = int((data.get("counts") or {}).get(section.get("key")) or 0)
        loading_label = (
            f"Loading {mention_count:,} "
            f"{'mention' if mention_count == 1 else 'mentions'}"
            if mention_count else "Checking for mentions"
        )
        # This copy of the law is painted before the browser reaches the (potentially
        # enormous) embedded JSON payload.  The interactive renderer atomically replaces
        # it later, so this is the one place a useful per-provision loading state can be
        # shown while a single-file edition is still downloading/parsing.  CSS only shows
        # it when the tiny head script confirms that JavaScript is enabled.
        loading = (
            '<p class="mentions-loading" role="status" '
            f'aria-label="{html.escape(loading_label, quote=True)}">'
            '<span aria-hidden="true">[ '
            f'{html.escape(loading_label)}<span class="loading-dots">...</span> ]'
            '</span></p>'
        )
        nav.append(f'<a href="#{sid}"><span>{label}</span></a>')
        paras = section.get("paragraphs") or [{"text": section.get("text") or "",
                                               "indent": 0}]
        lines = "".join(
            '<p class="law-paragraph indent-{i}">{t}</p>'.format(
                i=min(3, int(p.get("indent") or 0)),
                t=html.escape(str(p.get("text") or "")))
            for p in paras if str(p.get("text") or "").strip())
        body.append(
            f'<section id="{sid}" class="law-section level-{level}">'
            f"<h2>{label}</h2>{loading}{lines}</section>")
    return "\n".join(nav), "\n".join(body)


def render_static_page(
    *, title: str, data_json: str, note: str | None = None,
    index_link: dict | None = None, page_title: str | None = None,
    short_title: str | None = None, canonical: str | None = None,
    asset_prefix: str | None = None, _asset_sink: dict[str, bytes] | None = None,
) -> str:
    """Render the page around an already-serialised payload.

    ``page_title`` is what the browser tab and a bookmark carry, and ``short_title`` is
    what the contents column calls this law. The <h1> is the instrument's full official
    name, which is far too long for either, so a bundle passes the short name it gave the
    law and the name of the set it belongs to.
    """
    page = _HTML_TEMPLATE
    resolved_title = page_title or f"{title} — citations"
    # The head metadata and the crawlable copy are built from the payload, so they can
    # never drift from what the page actually shows.
    try:
        parsed = json.loads(data_json)
    except (ValueError, TypeError):
        parsed = {}
    core_json, chunk_html, assets = _deferred_page_payloads(
        data_json, asset_prefix=asset_prefix)
    if _asset_sink is not None:
        _asset_sink.update(assets)
    nav_html, law_html = _prerendered_law(parsed) if parsed else ("", "")
    lang = html.escape(
        str(((parsed.get("law") or {}).get("language")) or "en"), quote=True)
    replacements = {
        "__PAGE_TITLE__": html.escape(resolved_title, quote=True),
        "__HTML_LANG__": lang,
        "__SEO_HEAD__": _seo_head(parsed, page_title=resolved_title,
                                  canonical=canonical) if parsed else "",
        "__PRERENDER_NAV__": nav_html,
        "__PRERENDER_LAW__": law_html,
        "__TITLE__": html.escape(_display_title(title)),
        "__SIDEBAR_HEAD__": _sidebar_head(short_title or title, index_link),
        "__ATTRIBUTION_BLOCK__": _attribution_block(note, parsed.get("subset")),
        "__STYLE__": _STYLE,
        "__SCRIPT__": _SCRIPT,
        "__DATA__": core_json,
        "__CHUNKS__": chunk_html,
    }
    for token, value in replacements.items():
        page = page.replace(token, value)
    return page


def render_static_html(
    data: dict, *, note: str | None = None, index_link: dict | None = None
) -> str:
    return render_static_page(
        title=data["law"]["title"],
        data_json=_json_dump(data),   # slimmed inside render_static_page
        note=note,
        index_link=index_link,
        short_title=data["law"].get("short_title") or None,
    )


_HTML_TEMPLATE = """<!doctype html>
<html lang="__HTML_LANG__">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>__PAGE_TITLE__</title>
__SEO_HEAD__
  <script>
    // Opt in before the large inline payload arrives. With JavaScript disabled these
    // markers stay hidden; if the renderer fails, load converts them from "loading" to
    // an honest error instead of leaving an endless animation behind.
    document.documentElement.classList.add("mentions-pending");
    window.addEventListener("load", function () {
      var root = document.documentElement;
      if (!root.classList.contains("mentions-pending")) return;
      root.classList.replace("mentions-pending", "mentions-failed");
      document.querySelectorAll(".mentions-loading").forEach(function (marker) {
        marker.setAttribute("aria-label", "Mentions could not be loaded");
        marker.textContent = "[ Mentions could not be loaded. ]";
      });
    });
  </script>
  <style>__STYLE__</style>
</head>
<body>
  <header class="page-head">
    <div>
      <h1>__TITLE__</h1>
      __ATTRIBUTION_BLOCK__
      <!-- One line, filled in by the script when the instrument has live proceedings:
           counts per kind, each clickable to open the list filtered to it. -->
      <p class="pending-line" id="pending-line" hidden></p>
    </div>
  </header>
  <div class="page">
    <aside class="contents" id="contents">
      <!-- What this is, and the way back to the set it belongs to — the contents column
           is the page's only navigation, so it names itself before it lists anything. -->
      <div class="contents-head">
        __SIDEBAR_HEAD__
      </div>
      <!-- Rendered server-side and replaced by the script on load, so the instrument is
           readable, indexable and linkable without JavaScript. Identical content. -->
      <nav id="contents-nav" aria-label="Law contents">__PRERENDER_NAV__</nav>
    </aside>
    <main id="law">__PRERENDER_LAW__</main>
  </div>
  <dialog id="mentions-dialog" aria-labelledby="mentions-title">
    <div class="dialog-head">
      <div>
        <h2 id="mentions-title">Mentions</h2>
      </div>
      <button id="dialog-close" type="button">[ close ]</button>
    </div>
    <div id="route-tokens" class="route-tokens" aria-label="Which law was actually cited"></div>
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
    </div>
    <div id="mentions-load" class="mentions-load" role="status" hidden>
      <span id="mentions-load-label">Loading citation details…</span>
      <progress id="mentions-load-progress" max="1" value="0"></progress>
    </div>
    <div id="results"></div>
    <button id="more-results" class="more" type="button">+ show more</button>
  </dialog>
  <!-- The pending list, opened from the summary line and filterable to one kind of
       proceeding by clicking its count. -->
  <dialog id="pending-dialog" aria-labelledby="pending-title">
    <div class="dialog-head">
      <div>
        <h2 id="pending-title">Before the Court</h2>
        <p id="pending-sub" class="compare-sub"></p>
      </div>
      <button id="pending-close" type="button">[ close ]</button>
    </div>
    <div id="pending-filters" class="facet-tokens" aria-label="Filter by kind of proceeding"></div>
    <div id="pending-body"></div>
  </dialog>
  <!-- Second level, over the mentions list: the two provisions a mapping claims are
       similar, side by side, so the reader judges the claim rather than trusting it. -->
  <dialog id="compare-dialog" aria-labelledby="compare-title">
    <div class="dialog-head">
      <div>
        <h2 id="compare-title">Compare provisions</h2>
        <p id="compare-sub" class="compare-sub"></p>
      </div>
      <button id="compare-close" type="button">[ close ]</button>
    </div>
    <div id="compare-body" class="compare-body"></div>
  </dialog>
  <!-- The small core runs before the large citation blocks: provision annotations become
       interactive while the remainder of this single file is still arriving. -->
  <script id="raglex-data" type="application/json">__DATA__</script>
  <script>__SCRIPT__</script>
__CHUNKS__
  <script>window.dispatchEvent(new Event("raglex-chunks-ready"));</script>
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
  --link: #142b7a;
  --link-visited: #5d3267;
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
  line-height: 1.42;
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
a { color: var(--link); text-decoration-thickness: 1px; text-underline-offset: .16em; }
a:visited { color: var(--link-visited); }
.page-head {
  display: grid;
  grid-template-columns: var(--sidebar) minmax(0, 52rem);
  gap: 2.7rem;
  padding: 1.5rem 3rem 1rem;
  border-bottom: 1px solid var(--ink);
}
.page-head h1 {
  margin: 0;
  max-width: 48rem;
  font-size: clamp(1.7rem, 2.4vw, 2.15rem);
  font-weight: 400;
  line-height: 1.08;
  text-wrap: balance;
}
.attribution {
  max-width: 52rem;
  margin: .55rem 0 0;
  color: var(--quiet);
  font-size: 1.05rem;
  line-height: 1.4;
}
.attribution + .attribution { margin-top: .3rem; }
/* Only an individual law uses this template. Its introductory material gets the combined
   contents-and-text measure; the bundle index and sources page have separate templates. */
.page-head > div {
  grid-column: 1 / -1;
  max-width: calc(var(--sidebar) + 2.7rem + 52rem);
}
.page-head h1, .page-head .attribution { max-width: none; }
/* A page with no contents column at all — the bundle's index — is one column, centred on
   the same measure the text would have had. (This was the [ contents ] button's collapsed
   state; the button is gone, but the layout it left behind is what the index is.) */
body.no-sidebar .page-head, body.no-sidebar .page {
  grid-template-columns: minmax(0, 52rem);
  padding-left: max(2rem, calc((100vw - 52rem) / 2));
}
body.no-sidebar .contents { display: none; }
body.no-sidebar .page-head > div { grid-column: 1; }
.page {
  display: grid;
  grid-template-columns: var(--sidebar) minmax(0, 52rem);
  gap: 2.7rem;
  padding: 0 3rem 3rem;
  align-items: start;
}
.contents {
  position: sticky;
  top: 0;
  max-height: 100vh;
  overflow: auto;
  padding: 0 .8rem 1.2rem 0;
  border-right: 1px solid var(--rule);
}
.contents nav { margin-top: 0; }
.contents-head {
  position: sticky;
  top: 0;
  z-index: 2;
  margin-bottom: .55rem;
  padding: 1rem 0 .5rem;
  background: var(--paper);
  border-bottom: 1px solid var(--rule);
}
.contents-title { margin: 0; font-size: 1.05rem; line-height: 1.25; text-wrap: balance; }
.contents-back { margin: .3rem 0 0; font-size: .95rem; line-height: 1.25; }
.contents nav a, .contents nav button {
  display: flex;
  width: 100%;
  justify-content: space-between;
  gap: .75rem;
  padding: .16rem .9rem .16rem 0;
  border: 0;
  background: transparent;
  color: var(--link);
  text-align: left;
  text-decoration: none;
  line-height: 1.22;
}
.contents a:visited { color: var(--link-visited); }
.contents a:hover, .contents nav button:hover { text-decoration: underline; outline: 0; }
.contents .count { color: inherit; font-variant-numeric: tabular-nums; }
.contents .all-mentions { border-bottom: 1px solid var(--ink); }
.contents .all-mentions + :not(.all-mentions) { margin-top: .45rem; }
main { min-width: 0; padding-top: .9rem; }
.law-section {
  position: relative;
  padding: .55rem 0 .35rem;
  scroll-margin-top: 1rem;
}
.law-section h2 {
  margin: 0 0 .3rem;
  max-width: 43rem;
  font-size: 1.2rem;
  line-height: 1.22;
  font-weight: 600;
}
.law-text {
  margin: 0;
  max-width: 46rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.law-section.level-1 { margin-left: 1.4rem; }
.law-section.level-2 { margin-left: 2.8rem; }
.law-paragraph {
  max-width: 46rem;
  margin: 0 0 .35rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.law-paragraph:last-child { margin-bottom: 0; }
.law-paragraph.indent-1 { margin-left: 1.45rem; }
.law-paragraph.indent-2 { margin-left: 2.9rem; }
.law-paragraph.indent-3 { margin-left: 4.35rem; }
/* A mention badge does what a link does — it takes you somewhere — so it is inked like
   one, everywhere it appears: a provision heading, a numbered paragraph, the route row
   of the dialog, a comparison offered beside a result. */
.mention-ref {
  display: inline;
  margin-left: .45rem;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--link);
  font-weight: 700;
  font-size: .9rem;
  white-space: nowrap;
}
.mention-ref:hover, .mention-ref:focus-visible {
  border: 0;
  outline: 0;
  color: var(--link);
  text-decoration: underline;
}
/* The provision's "Mentioned by …" line: small, quiet, and set as prose, so naming three
   authorities under a heading costs the law no room. */
.mentions-line {
  max-width: 46rem;
  margin: 0 0 .5rem;
  color: var(--quiet);
  font-size: .88rem;
  line-height: 1.45;
}
.mentions-line .via-line { display: inline; }
/* The law itself is before the large JSON block in the file, so these are visible while
   that block downloads and parses. They are deliberately plain text in the page's Times
   face: a Word-like status line, not an application spinner. */
.mentions-loading {
  display: none;
  max-width: 46rem;
  margin: 0 0 .5rem;
  color: var(--quiet);
  font-size: .88rem;
  line-height: 1.45;
  font-style: italic;
}
.mentions-pending .mentions-loading,
.mentions-failed .mentions-loading { display: block; }
.mentions-failed .mentions-loading { color: #7d322a; font-style: normal; }
.loading-dots {
  display: inline-block;
  width: 0;
  overflow: hidden;
  vertical-align: bottom;
  white-space: nowrap;
  animation: loading-dots 1.2s steps(3, end) infinite;
}
@keyframes loading-dots { to { width: .9em; } }
@media (prefers-reduced-motion: reduce) {
  .loading-dots { width: .9em; animation: none; }
}
.cite-link {
  color: var(--link);
  cursor: pointer;
  /* a long instrument name must break like the prose around it, not sit as one
     unbreakable box that pushes the commas onto their own lines */
  overflow-wrap: break-word;
}
.cite-link:hover, .cite-link:focus-visible {
  border: 0;
  outline: 0;
  color: var(--link);
  text-decoration: underline;
}
.cite-link.see-all { font-weight: 700; white-space: nowrap; }
/* the row a name was followed to */
.result.focused { background: #faf6e2; box-shadow: 0 0 0 .35rem #faf6e2; }
.source-note {
  margin: 0 0 .9rem;
  padding: 0;
  color: var(--quiet);
}
dialog {
  width: min(58rem, calc(100vw - 2rem));
  max-height: calc(100vh - 2rem);
  padding: 0 1.4rem 1.1rem;
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
  padding: .9rem 0 .6rem;
  border-bottom: 1px solid var(--ink);
  background: var(--paper-raised);
}
.dialog-head h2 { margin: 0; font-size: 1.45rem; font-weight: 500; line-height: 1.2; }
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
  gap: .5rem 1rem;
  padding: .7rem 0;
  border-bottom: 1px solid var(--faint-rule);
}
.filters label { color: var(--quiet); font-size: 1rem; }
.filters select {
  margin-left: .35rem;
  padding: .3rem .4rem;
  font-size: 1rem;
}
.mentions-load {
  padding: .9rem 0;
  color: var(--quiet);
}
.mentions-load progress {
  display: block;
  width: min(28rem, 100%);
  height: .75rem;
  margin-top: .35rem;
  accent-color: var(--ink);
}
.facet-tokens { display: flex; flex: 1 1 30rem; flex-wrap: wrap; gap: .35rem .5rem; }
/* The same breakdown the provision heading offers, repeated inside the dialog: which
   law the citing document actually named. Sits above the ordinary facets because it
   changes what the list MEANS, not merely which rows of it are shown. */
.route-tokens {
  display: flex;
  flex-wrap: wrap;
  gap: .35rem .5rem;
  padding: .65rem 0 0;
}
.route-tokens:empty { display: none; }
.route-token {
  padding: .22rem .5rem;
  border: 1px solid var(--rule);
  background: transparent;
  color: var(--quiet);
  font-size: .95rem;
}
.route-token:hover, .route-token.on { border-color: var(--ink); outline: 0; color: var(--ink); }
.route-token.on { background: #f1f1f1; }
.route-token b { color: var(--ink); font-variant-numeric: tabular-nums; font-weight: 700; }
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
/* The comparison overlay sits above the mentions dialog, so it is wider and its
   backdrop is darker — a reader must be able to tell which layer they are on. */
#compare-dialog { width: min(72rem, calc(100vw - 2rem)); }
#compare-dialog::backdrop { background: rgba(24, 23, 20, .58); }
.compare-sub { margin: .25rem 0 0; color: var(--quiet); font-size: 1rem; }

/* What is still before the Court. A snapshot page states it as one line — counts per
   kind of proceeding, each a filter into the list — because it cannot spend the first
   screen on a list it also cannot refresh. */
/* Marked in highlighter yellow, the way a reader would mark it in Word, and clipped to
   the words rather than the column: this is the one line on the page that is true only
   on the day it was built, and it should not read as part of the instrument's own
   typography. */
.pending-line { margin: .6rem 0 0; font-size: 1rem; line-height: 1.7; }
.pending-highlight {
  padding: .18rem .34rem;
  background: #ffff00;
  color: #000;
  -webkit-box-decoration-break: clone;
  box-decoration-break: clone;
}
.pending-count, .pending-all {
  background: none; border: 0; padding: 0; font: inherit; color: inherit;
  cursor: pointer; text-decoration: underline;
}
.pending-count:hover, .pending-all:hover { text-decoration-thickness: 2px; }
.pending-count:focus-visible, .pending-all:focus-visible { outline: 2px solid #000; }
.pending-all { font-weight: 600; white-space: nowrap; }
/* Two lines and a hairline: the case, then what it turns on and where to read it. */
.pending-row { padding: .38rem 0; border-bottom: 1px solid var(--faint-rule); }
.pending-row:last-child { border-bottom: 0; }
.pending-case { margin: 0; line-height: 1.35; }
.pending-kind { color: var(--quiet); }
.pending-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0 1.2rem;
  margin: 0;
  color: var(--quiet);
  font-size: 1rem;
}
/* The source link sits at the end of the line whether or not provisions precede it. */
.pending-meta a { margin-left: auto; white-space: nowrap; }
.compare-body {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 2rem;
  padding-top: .8rem;
}
.compare-col { min-width: 0; }
.compare-col + .compare-col { border-left: 1px solid var(--faint-rule); padding-left: 2rem; }
.compare-col h3 { margin: 0 0 .15rem; font-size: 1.1rem; font-weight: 600; }
.compare-col .compare-source { margin: 0 0 .55rem; color: var(--quiet); font-size: .95rem; }
.compare-text {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 60vh;
  overflow: auto;
}
.compare-missing { color: #7d322a; }
.compare-note {
  grid-column: 1 / -1;
  margin: .9rem 0 0;
  padding-top: .6rem;
  border-top: 1px solid var(--faint-rule);
  color: var(--quiet);
  font-size: .95rem;
}
.compare-link { margin: .35rem 0 0; font-size: 1rem; }
.result {
  padding: .85rem 0 .95rem;
  border-bottom: 1px solid var(--faint-rule);
  /* the dialog head is sticky, so a row scrolled to by name must clear it */
  scroll-margin-top: 4.5rem;
}
.result h3 { margin: 0; font-size: 1.08rem; font-weight: 500; line-height: 1.3; }
.result time { color: var(--quiet); white-space: nowrap; }
.result-head { display: flex; justify-content: space-between; gap: 1.2rem; }
.result-meta, .result-targets, .source-links {
  margin: .2rem 0 0;
  color: var(--quiet);
  font-size: 1rem;
}
.snippet {
  margin: .55rem 0 0;
  padding: .15rem 0 .15rem 1rem;
  border-left: 2px solid var(--ink);
}
.snippet p { margin: 0; }
.snippet .where { color: var(--quiet); font-size: 1rem; }
mark { padding: 0 .08em; background: var(--mark); color: inherit; }
.source-links a + a::before { content: " · "; color: var(--quiet); text-decoration: none; }
.no-source { color: #7d322a; }
.more { display: block; margin: 1rem auto 0; padding: .3rem .7rem; background: transparent; }
/* An author `display` beats the browser's own [hidden] rule, so anything the script
   hides by setting .hidden — "+ show more" with nothing more to show — stayed on
   screen. Say it once, for everything. */
[hidden] { display: none !important; }
.empty { padding: 1.4rem 0; color: var(--quiet); }
@media (max-width: 760px) {
  body { font-size: 16px; }
  .page-head { display: block; padding: 1.2rem 1.1rem; }
  .page { display: block; padding: 0 1.1rem 3rem; }
  .contents {
    position: relative;
    max-height: 45vh;
    margin-bottom: 1rem;
    padding: 0;
    border-right: 0;
    border-bottom: 1px solid var(--ink);
  }
  body.no-sidebar .page-head, body.no-sidebar .page { padding-left: 1.1rem; }
  .result-head { display: block; }
  .filters { display: block; }
  .filters label { display: block; margin-top: .8rem; }
  .filters select { width: 100%; min-width: 0; margin: .3rem 0 0; }
  .law-paragraph.indent-1 { margin-left: .8rem; }
  .law-paragraph.indent-2 { margin-left: 1.6rem; }
  .law-paragraph.indent-3 { margin-left: 2.4rem; }
}
@media print {
  .contents, .mention-ref, dialog { display: none !important; }
  /* The names are the SENTENCE, not decoration on it — hiding the buttons would print
     "Mentioned by , and 18 more". They stay, as ink; only the way in goes. */
  .cite-link { color: var(--ink); text-decoration: none; }
  .cite-link.see-all { display: none; }
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
  if (!Array.isArray(data.groups)) data.groups = [];

  // Published editions fetch bounded sidecars; a downloaded standalone page retains the
  // same blocks inline. In either form only one canonical block is admitted at an idle
  // callback. A provision click ABORTS the background request, loads its tiny Article pack
  // first, and only then fills any remainder from canonical blocks.
  const parsedChunks = new Set();
  const chunkLoads = new Map();
  const priorityLoads = new Map();
  const loadedPriorityUrls = new Set();
  const completePriorityKeys = new Set();
  let chunksReadyResolve;
  const chunksReady = new Promise((resolve) => { chunksReadyResolve = resolve; });
  let foregroundLoads = 0;
  let backgroundController = null;
  let foregroundController = null;
  const backgroundPriorityChunks = [];
  const chunkCount = Number(data.chunk_count || 0);
  const externalChunks = Array.isArray(data.chunk_urls) && data.chunk_urls.length > 0;
  if (!chunkCount || externalChunks) chunksReadyResolve();
  window.addEventListener("raglex-chunks-ready", () => {
    chunksReadyResolve();
    window.setTimeout(scheduleBackgroundChunks, 1500);
  }, { once: true });
  if (externalChunks) window.setTimeout(scheduleBackgroundChunks, 1500);

  function mergeProjectedGroup(groupIndex, incoming) {
    const index = Number(groupIndex);
    const existing = data.groups[index];
    if (!existing) { data.groups[index] = incoming; return; }
    const merged = { ...existing, ...incoming };
    for (const field of ["mentions_by_key", "inherited_mentions_by_key",
      "previous_mentions_by_key", "version_mentions_by_key", "labels_by_key"]) {
      merged[field] = { ...(existing[field] || {}), ...(incoming[field] || {}) };
    }
    const priorSnippets = existing.snippets || [];
    const extraSnippets = incoming.snippets || [];
    merged.snippets = [...priorSnippets, ...extraSnippets];
    merged.snippet_indices = { ...(existing.snippet_indices || {}) };
    for (const [key, indices] of Object.entries(incoming.snippet_indices || {})) {
      merged.snippet_indices[key] = indices.map((value) => priorSnippets.length + Number(value));
    }
    data.groups[index] = merged;
  }

  async function parseChunk(chunkIndex, signal = null) {
    if (parsedChunks.has(chunkIndex)) return;
    if (chunkLoads.has(chunkIndex)) return chunkLoads.get(chunkIndex);
    const load = (async () => {
      let rows;
      const url = (data.chunk_urls || [])[chunkIndex];
      if (url) {
        const response = await fetch(url, { signal, cache: "force-cache" });
        if (!response.ok) throw new Error(`Citation block ${chunkIndex} returned ${response.status}`);
        rows = await response.json();
      } else {
        const node = document.getElementById(`raglex-chunk-${chunkIndex}`);
        if (!node) throw new Error(`Citation block ${chunkIndex} is missing`);
        rows = JSON.parse(node.textContent || "[]");
        // The parsed object is now authoritative. Do not retain both representations.
        node.textContent = "";
      }
      for (const [groupIndex, group] of rows) data.groups[Number(groupIndex)] = group;
      parsedChunks.add(chunkIndex);
    })();
    chunkLoads.set(chunkIndex, load);
    try { await load; }
    finally { if (chunkLoads.get(chunkIndex) === load) chunkLoads.delete(chunkIndex); }
  }

  async function loadPriorityPack(key, signal) {
    const url = (data.priority_pack_urls || {})[key];
    if (!url || loadedPriorityUrls.has(url)) return;
    if (priorityLoads.has(url)) return priorityLoads.get(url);
    const load = (async () => {
      const response = await fetch(url, { signal, cache: "force-cache" });
      if (!response.ok) throw new Error(`Priority citation pack returned ${response.status}`);
      const payload = await response.json();
      for (const [packKey, pack] of Object.entries(payload.packs || {})) {
        for (const [groupIndex, group] of pack.rows || []) {
          mergeProjectedGroup(groupIndex, group);
        }
        if (pack.complete) completePriorityKeys.add(packKey);
      }
      loadedPriorityUrls.add(url);
    })();
    priorityLoads.set(url, load);
    try { await load; }
    finally { if (priorityLoads.get(url) === load) priorityLoads.delete(url); }
  }

  function nextBackgroundChunk() {
    while (backgroundPriorityChunks.length) {
      const candidate = backgroundPriorityChunks.shift();
      if (!parsedChunks.has(candidate)) return candidate;
    }
    for (let i = 0; i < chunkCount; i += 1) {
      if (!parsedChunks.has(i)) return i;
    }
    return null;
  }

  function scheduleBackgroundChunks() {
    if (!chunkCount || foregroundLoads || backgroundController
        || nextBackgroundChunk() == null) return;
    const run = async (deadline) => {
      if (foregroundLoads) { scheduleBackgroundChunks(); return; }
      const next = nextBackgroundChunk();
      if (next == null) return;
      // Exactly one bounded parse per callback. Even a generous idle deadline should not
      // turn into a 100 MB burst that competes with somebody reading the law.
      if (!deadline || deadline.didTimeout || deadline.timeRemaining() > 3) {
        backgroundController = new AbortController();
        try { await parseChunk(next, backgroundController.signal); }
        catch (error) {
          if (error.name !== "AbortError") console.error(error);
        } finally {
          backgroundController = null;
        }
      }
      if (!foregroundLoads) scheduleBackgroundChunks();
    };
    if ("requestIdleCallback" in window) {
      window.requestIdleCallback(run, { timeout: 5000 });
    } else {
      window.setTimeout(() => run(null), 75);
    }
  }

  function stopBackgroundLoading() {
    if (backgroundController) backgroundController.abort();
  }

  function prioritizeArticleRemainder(key) {
    const priorityUrl = (data.priority_pack_urls || {})[key];
    if (!priorityUrl) return;
    const related = Object.entries(data.priority_pack_urls || {})
      .filter(([, url]) => url === priorityUrl).map(([relatedKey]) => relatedKey);
    const chunks = [];
    for (const relatedKey of related) {
      for (const groupIndex of data.index[relatedKey] || []) {
        const chunkIndex = Number((data.group_chunks || [])[Number(groupIndex)]);
        if (Number.isFinite(chunkIndex) && !parsedChunks.has(chunkIndex)) chunks.push(chunkIndex);
      }
    }
    for (const chunkIndex of [...new Set(chunks)]) {
      if (!backgroundPriorityChunks.includes(chunkIndex)) {
        backgroundPriorityChunks.push(chunkIndex);
      }
    }
  }

  async function ensureGroups(ids, progress, signal) {
    if (!chunkCount) return;
    await chunksReady;
    const wanted = [...new Set((ids || []).map(
      (id) => Number((data.group_chunks || [])[Number(id)]))
      .filter((id) => Number.isFinite(id) && !parsedChunks.has(id)))];
    let done = 0;
    progress?.(done, wanted.length);
    for (const chunkIndex of wanted) {
      try {
        await parseChunk(chunkIndex, signal);
      } catch (error) {
        // A just-aborted background request may still own this block for one microtask.
        // Retry it under the foreground controller; a genuine foreground abort propagates.
        if (error.name !== "AbortError" || signal?.aborted) throw error;
        await parseChunk(chunkIndex, signal);
      }
      done += 1;
      progress?.(done, wanted.length);
      // Let the browser paint the bar and service input before parsing the next block.
      await new Promise((resolve) => window.requestAnimationFrame(resolve));
    }
  }

  // The law and its contents list are ALSO rendered into the HTML server-side, so the
  // page is readable and indexable without JavaScript. Build the interactive copy away
  // from the live DOM, then swap it in atomically: the per-provision loading messages
  // remain visible throughout a large render, and a failed render leaves the readable
  // law in place instead of a half-empty page.
  const renderedLaw = document.createDocumentFragment();
  $("contents-nav").textContent = "";

  const sourceNote = document.createElement("p");
  sourceNote.className = "source-note";
  const lawLinks = data.law.links.length
    ? data.law.links.map((link) =>
        `<a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)} →</a>`).join(" · ")
    : `<span class="no-source">No public copy recorded</span>`;
  const inheritedRecitals = data.law.inherited_recitals;
  const currency = data.law.currency || {};
  const currencyNote = currency.as_at
    ? `<br><span class="muted">Publisher text current to <time datetime="${esc(currency.as_at)}">${esc(currency.as_at)}</time>; checked for updates ${esc((currency.checked_at || data.generated_at).slice(0, 10))}${currency.up_to_date === false && Number(currency.unapplied_count || 0) > 0
        ? ` · ${number(currency.unapplied_count)} publisher-recorded ${Number(currency.unapplied_count) === 1 ? "effect" : "effects"} not yet applied to the text`
        : ""}.</span>`
    : "";
  // The short form, with the official title on hover — a provenance note is a
  // footnote, not a place to reprint 40 words of instrument title.
  const recitalSourceName = inheritedRecitals?.source_label
    || inheritedRecitals?.source_title || inheritedRecitals?.source_stable_id || "";
  const recitalSource = inheritedRecitals?.source_url
    ? `<a href="${esc(inheritedRecitals.source_url)}" title="${esc(inheritedRecitals.source_title || "")}" target="_blank" rel="noopener noreferrer">${esc(recitalSourceName)} →</a>`
    : `<span title="${esc(inheritedRecitals?.source_title || "")}">${esc(recitalSourceName)}</span>`;
  sourceNote.innerHTML = lawLinks + currencyNote + (inheritedRecitals
    ? `<br><span class="muted">${esc(inheritedRecitals.note)} Source: ${recitalSource}</span>`
    : "");
  renderedLaw.appendChild(sourceNote);

  // Each predecessor instrument this law inherits mentions from, named — "Directive
  // 95/46/EC", never "previous law". Routes are keyed by its stable id.
  const previousLaws = data.previous_laws || [];
  const previousCounts = data.previous_counts || {};
  const directCounts = data.direct_counts || {};
  const lawById = new Map(previousLaws.map((law) => [law.id, law]));
  const lawLabel = (id) => (lawById.get(id) || {}).label || id;
  const lawTitle = (id) => (lawById.get(id) || {}).title || id;
  const directFor = (key) => Number(directCounts[key] || 0);
  const previousFor = (key) => previousLaws
    .map((law) => ({ law, count: Number((previousCounts[law.id] || {})[key] || 0) }))
    .filter((row) => row.count > 0);
  const viaPhrase = (count, id) =>
    `${number(count)} ${count === 1 ? "mention" : "mentions"} of a similar provision in ${lawLabel(id)}`;

  const nav = $("contents-nav");
  const all = document.createElement("button");
  all.type = "button";
  all.className = "all-mentions";
  all.innerHTML = `<span>All mentions</span><span class="count">${number(data.counts.all)}</span>`;
  all.addEventListener("click", () => openMentions("all", "All mentions"));
  nav.appendChild(all);
  for (const { law, count } of previousFor("all")) {
    const previous = document.createElement("button");
    previous.type = "button";
    previous.className = "all-mentions";
    previous.title = lawTitle(law.id);
    previous.innerHTML = `<span>Via ${esc(law.label)}</span><span class="count">${number(count)}</span>`;
    previous.addEventListener("click", () => openMentions(
      "all", `Mentions of a similar provision in ${law.label}`, law.id));
    nav.appendChild(previous);
  }

  // The badge row a provision heading (or a numbered paragraph) carries. With no
  // predecessor it is the plain total; with one, the total splits so a reader can tell
  // what was said about THIS text from what was said about the text it replaced.
  function mentionBadges(key, label, count) {
    const previous = previousFor(key);
    if (!previous.length) {
      return count
        ? [{ route: "all", text: `${number(count)} ${count === 1 ? "mention" : "mentions"}`,
             title: `Documents mentioning ${label}`, label }]
        : [];
    }
    const badges = [];
    const direct = directFor(key);
    if (direct) {
      badges.push({
        route: "direct",
        text: `${number(direct)} direct ${direct === 1 ? "mention" : "mentions"}`,
        title: `Documents citing ${label} of this law itself`,
        label: `${label} — cited directly`,
      });
    }
    for (const { law, count: n } of previous) {
      badges.push({
        route: law.id,
        text: viaPhrase(n, law.id),
        title: lawTitle(law.id),
        label: `${label} — via ${law.label}`,
      });
    }
    return badges;
  }

  // How many citers a provision names before it stops naming and starts counting.
  const NAMED_CITERS = 3;

  // The name a citer is given IN THE LINE. A case's OSCOLA citation is already short, but
  // an instrument's is its full official title — "Regulation (EU) No 604/2013 of the
  // European Parliament and of the Council of 26 June 2013 establishing the criteria and
  // mechanisms for determining…", 250 characters. Three of those in one sentence wrap
  // over a dozen lines and strand the commas at the edges, which is what made the line
  // look like a broken list instead of prose. Shortened HERE rather than in the payload,
  // so an already-built edition picks it up on a plain re-render.
  // the trailing "/EC" is part of the number a pre-2015 directive is cited by
  // ("Directive 95/46/EC"), so it belongs in the label, not on the cutting-room floor
  const EU_INSTRUMENT = /\b((?:Council|Commission|European Parliament and(?: of)? the Council)?\s*(?:Implementing|Delegated)?\s*(?:Regulation|Directive|Decision))\s*(\((?:EU|EC|EEC|Euratom)\))?\s*(?:No\.?\s*)?(\d{1,4}\/\d{2,4}(?:\/[A-Z]{2,7})?)/i;
  const ACT_TITLE = /^(.{3,70}? Act \d{4})\b/;
  const CITE_MAX = 72;
  function shortCite(g) {
    const raw = String(g.cite || g.title || "").replace(/\s+/g, " ").trim();
    if (raw.length <= CITE_MAX) return raw;
    const eu = EU_INSTRUMENT.exec(raw);
    if (eu) return [eu[1], eu[2], eu[3]].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
    const act = ACT_TITLE.exec(raw);
    if (act) return act[1];
    return raw.slice(0, CITE_MAX - 3).trimEnd() + "…";
  }

  // "Mentioned by FT v DW EU:C:2023:811, Proceedings brought by J.M EU:C:2023:501 and 18
  // more. See all mentions" — the line a reader can actually use, in place of a badge
  // that only ever said how many there were.
  //
  // Documents reaching this provision through a PREDECESSOR instrument are kept out of
  // that sentence and given their own: they are not authority on this text, they are
  // authority on the text it replaced, and running the two together is the mistake the
  // whole inherited-mentions split exists to prevent.
  function mentionsLine(key, label) {
    const ids = data.index[key] || [];
    if (!ids.length) return null;
    const annotation = (data.annotation_citers || {})[key] || {};
    const direct = annotation.direct || [];
    const directCount = Number(annotation.direct_count || 0);

    const p = document.createElement("p");
    p.className = "mentions-line";
    const add = (text) => p.appendChild(document.createTextNode(text));
    // A named citer opens the list AND scrolls to its own row — the name is a way in,
    // not a decoration.
    //
    // An ANCHOR, deliberately, not a button. A button is an atomic inline box: browsers
    // will not break its text across lines whatever its display, so each name that was
    // longer than the remaining line got pushed onto a line of its own and the commas
    // between them were stranded at the edges. That is what made a sentence look like a
    // ragged list. An anchor wraps like the prose it sits in.
    const link = (cls, text, title, onClick) => {
      const a = document.createElement("a");
      a.className = cls;
      a.textContent = text;
      a.setAttribute("role", "link");
      a.tabIndex = 0;
      if (title) a.title = title;
      a.addEventListener("click", onClick);
      a.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); }
      });
      p.appendChild(a);
      return a;
    };
    const cite = (g, text) => link(
      "cite-link", text || shortCite(g),
      g.cite || g.title || "",                       // the full name, on hover
      () => openMentions(key, label, "all", g.id));

    if (direct.length) {
      add("Mentioned by ");
      const named = direct.slice(0, NAMED_CITERS);
      named.forEach((g, i) => {
        if (i) add(i === named.length - 1 && directCount <= NAMED_CITERS ? " and " : ", ");
        cite(g);
      });
      const rest = directCount - named.length;
      if (rest > 0) add(` and ${number(rest)} more`);
      add(". ");
      link("cite-link see-all", "See all mentions", "",
           () => openMentions(key, label, "all"));
    }

    // …and then, per predecessor, the ones that only got here through it.
    for (const { law, count: n } of previousFor(key)) {
      const sentence = document.createElement("span");
      sentence.className = "via-line";
      sentence.appendChild(document.createTextNode(
        directCount ? " Also mentioned by " : "Mentioned by "));
      const b = document.createElement("a");
      b.className = "cite-link";
      b.setAttribute("role", "link");
      b.tabIndex = 0;
      b.textContent = `${number(n)} ${n === 1 ? "document" : "documents"} citing a similar provision in ${law.label}`;
      b.title = lawTitle(law.id);
      const open = () => openMentions(key, `${label} — via ${law.label}`, law.id);
      b.addEventListener("click", open);
      b.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
      sentence.appendChild(b);
      sentence.appendChild(document.createTextNode("."));
      p.appendChild(sentence);
    }
    return p.childNodes.length ? p : null;
  }

  function appendBadges(host, key, label, count) {
    for (const badge of mentionBadges(key, label, count)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "mention-ref";
      button.textContent = `[${badge.text}]`;
      button.title = badge.title;
      button.addEventListener("click", () => openMentions(key, badge.label, badge.route));
      host.appendChild(button);
    }
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
    article.className = `law-section level-${Math.min(2, Number(section.level || 0))}${section.inherited_recital ? " inherited-recital" : ""}`;
    const heading = document.createElement("h2");
    heading.textContent = section.label;
    article.appendChild(heading);
    // At the PROVISION, who cites it is worth naming; inside it, it is not. So the
    // heading carries a prose line of the leading citers and the subsections keep their
    // terse [N mentions] badge — a case name set against a numbered sub-paragraph breaks
    // the law's own shape, which is the thing the reader came for.
    const line = mentionsLine(section.key, section.label);
    if (line) article.appendChild(line);
    for (const paragraph of section.paragraphs || [{ text: section.text, indent: 0, marks: [] }]) {
      const body = document.createElement("p");
      body.className = `law-paragraph indent-${Math.min(3, Number(paragraph.indent || 0))}`;
      body.append(document.createTextNode(paragraph.text));
      for (const mark of paragraph.marks || []) {
        appendBadges(body, mark.key, mark.label, Number(mark.count || 0));
      }
      article.appendChild(body);
    }
    renderedLaw.appendChild(article);
  }
  $("law").replaceChildren(renderedLaw);
  document.documentElement.classList.remove("mentions-pending");

  const kindNames = {
    cases: "case law", administrative: "admin decisions",
    legislation: "legislation", guidance: "guidance & reports",
    preparatory: "preparatory documents", explanatory: "explanatory notes",
    other: "other"
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

  const state = {
    key: "all", label: "All mentions", limit: 40, facet: null, route: "all",
    loadToken: 0
  };
  async function openMentions(key, label, route = "all", focusId = null) {
    const loadToken = ++state.loadToken;
    if (foregroundController) foregroundController.abort();
    stopBackgroundLoading();
    const controller = new AbortController();
    foregroundController = controller;
    foregroundLoads += 1;
    state.key = key;
    state.label = label;
    state.limit = 40;
    state.facet = null;
    // A route that carries no documents for this provision would show an empty list;
    // fall back to everything rather than to a dead end.
    state.route = routeIsLive(key, route) ? route : "all";
    $("mentions-title").textContent = label;
    $("sort-filter").value = "authority";
    $("route-tokens").innerHTML = "";
    $("facet-tokens").innerHTML = "";
    $("results").innerHTML = "";
    $("more-results").hidden = true;
    const loader = $("mentions-load");
    const bar = $("mentions-load-progress");
    loader.hidden = false;
    if (!$("mentions-dialog").open) $("mentions-dialog").showModal();
    try {
      await chunksReady;
      bar.max = 1;
      bar.value = 0;
      $("mentions-load-label").textContent = "Loading priority citation details…";
      try {
        await loadPriorityPack(key, controller.signal);
      } catch (error) {
        // A rapid second click in the same Article can inherit the first click's promise
        // just as that first controller is being aborted. Re-own the pack immediately.
        if (error.name !== "AbortError" || controller.signal.aborted) throw error;
        await loadPriorityPack(key, controller.signal);
      }
      if (loadToken !== state.loadToken) return;

      const ready = (data.index[key] || []).filter((id) => data.groups[id]).length;
      if (ready) {
        renderResults();
        bar.value = completePriorityKeys.has(key) ? 1 : 0;
        $("mentions-load-label").textContent = completePriorityKeys.has(key)
          ? `Citation details ready — ${number(ready)} documents.`
          : `${number(ready)} documents ready; loading the remainder…`;
      }

      if (!completePriorityKeys.has(key)) {
        await ensureGroups(data.index[key] || [], (done, total) => {
          bar.max = Math.max(1, total);
          bar.value = done;
          $("mentions-load-label").textContent = total
            ? `Loading remaining citation details — ${number(done)} of ${number(total)} blocks…`
            : "Citation details ready.";
        }, controller.signal);
      }
      prioritizeArticleRemainder(key);
    } catch (error) {
      if (loadToken !== state.loadToken || error.name === "AbortError") return;
      $("mentions-load-label").textContent = "Citation details could not be loaded.";
      console.error(error);
      return;
    } finally {
      if (foregroundController === controller) foregroundController = null;
      foregroundLoads = Math.max(0, foregroundLoads - 1);
      if (!foregroundLoads) scheduleBackgroundChunks();
    }
    if (loadToken !== state.loadToken) return;
    loader.hidden = true;
    // Following a name must land ON that document, not at the top of a list it happens
    // to be somewhere inside — so page far enough to include it before rendering.
    if (focusId) {
      const at = (data.index[key] || []).findIndex(
        (id) => (data.groups[id] || {}).id === focusId);
      if (at >= state.limit) state.limit = at + 20;
    }
    renderResults();
    if (focusId) {
      const row = $("results").querySelector(`[data-doc="${CSS.escape(focusId)}"]`);
      if (row) {
        row.classList.add("focused");
        row.scrollIntoView({ block: "start" });
      }
    }
  }

  function routeIsLive(key, route) {
    if (route === "all") return true;
    if (route === "direct") return directFor(key) > 0;
    return Number((previousCounts[route] || {})[key] || 0) > 0;
  }

  // The dialog repeats the heading's breakdown as its own filter, so the split stays
  // legible once a reader is inside the list.
  function renderRouteTokens() {
    const previous = previousFor(state.key);
    const box = $("route-tokens");
    if (!previous.length) { box.innerHTML = ""; return; }
    const total = Number(data.counts[state.key] || 0);
    const direct = directFor(state.key);
    const routes = [{ route: "all", text: "everything", count: total, title: "" }];
    if (direct) {
      routes.push({ route: "direct", text: "this law directly", count: direct, title: "" });
    }
    for (const { law, count } of previous) {
      routes.push({
        route: law.id, count,
        text: `a similar provision in ${law.label}`,
        title: lawTitle(law.id),
      });
    }
    box.innerHTML = routes.map((row) =>
      `<button type="button" class="route-token${state.route === row.route ? " on" : ""}"`
      + ` data-route="${esc(row.route)}" title="${esc(row.title)}">`
      + `<b>${number(row.count)}</b> ${esc(row.text)}</button>`).join("")
      // The comparison is offered once for the whole list too, not only per row.
      + compareLinksHtml(state.key, null, "See for yourself —");
    for (const button of box.querySelectorAll("[data-route]")) {
      button.addEventListener("click", () => {
        state.route = button.dataset.route;
        state.limit = 40;
        renderResults();
      });
    }
    bindCompareButtons(box);
  }

  function snippetsFor(group) {
    const ids = group.snippet_indices[state.key] || [];
    return ids.map((id) => group.snippets[id]).filter(Boolean);
  }

  const previousMentions = (group) =>
    (group.previous_mentions_by_key || {})[state.key] || {};

  function selectedGroups() {
    const ids = data.index[state.key] || [];
    const rows = ids.map((id) => data.groups[id]).filter((group) => {
      if (!group) return false;
      const inherited = Number(group.inherited_mentions_by_key[state.key] || 0);
      const total = Number(group.mentions_by_key[state.key] || 0);
      if (state.route === "direct" && total <= inherited) return false;
      if (state.route !== "all" && state.route !== "direct"
          && !Number(previousMentions(group)[state.route] || 0)) return false;
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

  // -- comparing a provision with the one a mapping says it succeeded ---------
  const comparisons = data.comparisons || {};
  const comparisonsFor = (key, lawId) => (comparisons[key] || [])
    .filter((row) => !lawId || row.previous_id === lawId);

  function openCompare(row) {
    $("compare-title").textContent =
      `${row.current_label} — compared with ${row.previous_provision_label}`;
    const claim = [
      row.mapping_type === "equivalent"
        ? "recorded as a parallel provision in force alongside this one"
        : "recorded as the provision this one succeeded",
      row.confidence != null ? `confidence ${row.confidence}` : null,
    ].filter(Boolean).join(" · ");
    $("compare-sub").textContent = claim;
    const column = (heading, source, text) =>
      `<div class="compare-col"><h3>${esc(heading)}</h3>`
      + `<p class="compare-source">${esc(source)}</p>`
      + (text
        ? `<p class="compare-text">${esc(text)}</p>`
        : `<p class="compare-text compare-missing">This provision's text is not held in `
          + `this edition, so it cannot be shown here.</p>`)
      + "</div>";
    $("compare-body").innerHTML =
      column(row.current_label, data.law.short_title || data.law.title, row.current_text)
      + column(row.previous_provision_label,
               row.previous_title || row.previous_label, row.previous_text)
      + (row.note ? `<p class="compare-note">${esc(row.note)}</p>` : "");
    $("compare-dialog").showModal();
  }

  // One handle per comparison offered anywhere on the page; the button carries its index.
  const compareIndex = [];
  function compareLinksHtml(key, lawId, prefix) {
    const rows = comparisonsFor(key, lawId);
    if (!rows.length) return "";
    const links = rows.map((row) => {
      compareIndex.push(row);
      return `<button type="button" class="mention-ref" data-compare="${compareIndex.length - 1}">`
        + `[compare with ${esc(row.previous_provision_label)} of `
        + `${esc(row.previous_label)}]</button>`;
    }).join(" ");
    return `<p class="compare-link">${esc(prefix)} ${links}</p>`;
  }

  function bindCompareButtons(root) {
    for (const button of root.querySelectorAll("[data-compare]")) {
      button.addEventListener("click", () => {
        const row = compareIndex[Number(button.dataset.compare)];
        if (row) openCompare(row);
      });
    }
  }

  $("compare-close").addEventListener("click", () => $("compare-dialog").close());
  $("compare-dialog").addEventListener("click", (event) => {
    if (event.target === $("compare-dialog")) $("compare-dialog").close();
  });

  // --- what is still before the Court -------------------------------------
  // A snapshot page cannot spend its first screen on a list, and cannot fetch one
  // later, so the summary IS the affordance: counts per kind of proceeding, each one
  // a filter into the same dialog.
  const pending = data.pending || null;
  let pendingFilter = "";

  // "Action for annulment" pluralises on its head noun, not its tail — "action for
  // annulments" is not a thing — and "Preliminary reference (urgent, PPU)" must keep its
  // parenthetical, and its capitals, outside the inflection.
  function pluralKind(label) {
    const [, head, tail] = /^(.*?)(\s*\([^)]*\))?$/.exec(String(label || ""));
    const at = head.search(/\s+(?:for|of|to|against|by|under)\s+/);
    const stem = at < 0 ? head : head.slice(0, at);
    const rest = at < 0 ? "" : head.slice(at);
    const plural = /[^aeiou]y$/i.test(stem) ? stem.slice(0, -1) + "ies"
      : /(?:s|x|z|ch|sh)$/i.test(stem) ? stem + "es"
      : stem + "s";
    return plural + rest + (tail || "");
  }
  // Only the first letter drops out of title case: "(urgent, PPU)" is an abbreviation and
  // an AG is an AG.
  const lowerFirst = (text) => String(text || "").charAt(0).toLowerCase()
    + String(text || "").slice(1);
  const kindPhrase = (label, n) =>
    `${number(n)} pending ${lowerFirst(n === 1 ? label : pluralKind(label))}`;

  function renderPendingLine() {
    if (!pending || !pending.groups || !pending.groups.length) return;
    // "12 pending preliminary references (3 with an AG Opinion); 4 pending actions for
    // annulment. See all 16" — every count opens the list filtered to its own kind.
    const parts = pending.groups.map((g) => {
      const ag = g.with_ag
        ? ` (${number(g.with_ag)} with ${g.with_ag === 1 ? "an AG Opinion" : "AG Opinions"})`
        : "";
      return `<button class="pending-count" data-kind="${esc(g.label)}"`
        + ` title="Show only these">${esc(kindPhrase(g.label, g.n))}${esc(ag)}</button>`;
    });
    const line = $("pending-line");
    line.innerHTML = '<span class="pending-highlight">'
      + "<strong>Before the Court:</strong> " + parts.join("; ") + ". "
      + `<button class="pending-all" data-kind="">See all ${number(pending.total)}</button>`
      + "</span>";
    line.hidden = false;
    for (const button of line.querySelectorAll("[data-kind]")) {
      button.addEventListener("click", () => openPending(button.dataset.kind || ""));
    }
  }

  // A document of the Court is identified by its CELEX number, which is all EUR-Lex needs
  // to show it. No URL is carried for these, and none needs to be: the number IS the
  // address.
  const CELEX_ID = /^\d{5}[A-Z]{1,2}\d{4}(?:\(\d+\))?$/i;
  function celexUrl(value) {
    const id = String(value || "").split("/").pop().trim();
    return CELEX_ID.test(id)
      ? "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:" + encodeURIComponent(id)
      : "";
  }
  // …and EUR-Lex resolves an ECLI just as well, which is how the corpus holds many of the
  // Court's own documents.
  const ECLI_ID = /^ECLI:[A-Z]{2}:[A-Z]:\d{4}:\d+$/i;
  function ecliUrl(value) {
    const id = String(value || "").trim();
    return ECLI_ID.test(id)
      ? "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=ecli:" + encodeURIComponent(id)
      : "";
  }
  const externalLink = (url, text) =>
    `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>`;

  // An Opinion delivered is the strongest public signal of where a pending reference is
  // going, and it is readable NOW — months before the judgment. So it is a link, not a
  // note. A payload built before the Opinion's own id was carried falls back to the
  // ordinary Opinion descriptor (CN → CC) for the same case, which is what it is in all
  // but the urgent procedure.
  function agOpinionHtml(row) {
    const text = "AG Opinion delivered";
    const notice = String(row.id || "").split("/").pop().trim();
    const url = celexUrl(row.ag_id) || ecliUrl(row.ag_id)
      || (/^6\d{4}CN\d{4}$/i.test(notice)
          ? celexUrl(notice.slice(0, 5) + "CC" + notice.slice(7)) : "");
    return url ? externalLink(url, text) : esc(text);
  }

  // "C-287/26 Bundesverband … v RR (Preliminary reference)", then the provisions it turns
  // on and the way out to the notice. Two lines, and the kind of proceeding is said in
  // brackets rather than in a bordered pill: this page sets everything else in plain
  // prose, and a row of little boxes reads as furniture from somewhere else.
  // What the OJ notice's title carries besides the parties: the case number the line
  // already opens with, the Court's fictitious-name disclaimer as a whole sentence, and
  // the lodging date. The anonymised NAME the Court gives such a case ("Waldfelber") is
  // kept — it is how the case will be cited.
  const NAME_NOISE = [
    [/\s*The name of the present case is a fictitious name\.?\s*(?:It does not correspond to the real name of any party to the proceedings\.?)?/i, ""],
    [/\(\s*Case\s+[CT][-‑]\d+\/\d+\s*,\s*([^)]+)\)/i, "($1)"],
    [/\s*\(\s*(?:Case\s+)?[CT][-‑]\d+\/\d+[^)]*\)\s*$/i, ""],
    [/,\s*lodged on\s+\d{1,2}\s+\w+\s+\d{4}\.?\s*$/i, ""],
  ];

  function pendingRowHtml(row) {
    let name = String(row.title || "");
    for (const [pattern, replacement] of NAME_NOISE) name = name.replace(pattern, replacement);
    name = name.trim();
    // Escaped piece by piece: one of these is a link, so the whole cannot be.
    const kind = [esc(row.label), row.court ? esc(row.court) : "",
                  row.ag ? agOpinionHtml(row) : ""].filter(Boolean).join(", ");
    const anchors = (row.anchors || []).map(esc).join(", ");
    const url = celexUrl(row.id);
    const link = url ? externalLink(url, "EUR-Lex →") : "";
    return `<div class="pending-row"><p class="pending-case"><strong>${esc(row.case)}</strong>`
      + (name ? ` <em>${esc(name)}</em>` : "")
      + (kind ? ` <span class="pending-kind">(${kind})</span>` : "")
      + "</p>"
      + (anchors || link
         ? `<p class="pending-meta">${anchors ? `<span>${anchors}</span>` : ""}${link}</p>`
         : "")
      + "</div>";
  }

  function openPending(kind) {
    if (!pending) return;
    pendingFilter = kind || "";
    const rows = pendingFilter
      ? (pending.cases || []).filter((r) => r.label === pendingFilter)
      : (pending.cases || []);
    const asAt = `, as at ${data.generated_at.slice(0, 10)}`;
    $("pending-sub").textContent = (pendingFilter
      ? kindPhrase(pendingFilter, rows.length)
      : `${number(rows.length)} pending proceeding${rows.length === 1 ? "" : "s"}`) + asAt;
    $("pending-filters").innerHTML = [{ label: "", n: pending.total }]
      .concat(pending.groups || [])
      .map((g) => `<button class="facet-token${(g.label || "") === pendingFilter ? " on" : ""}"`
        + ` data-filter="${esc(g.label || "")}">${g.label ? esc(g.label) : "All"} ${g.n}</button>`)
      .join(" ");
    for (const button of $("pending-filters").querySelectorAll("[data-filter]")) {
      button.addEventListener("click", () => openPending(button.dataset.filter));
    }
    $("pending-body").innerHTML = rows.map(pendingRowHtml).join("")
      || '<p class="where">Nothing pending in this category.</p>';
    if (!$("pending-dialog").open) $("pending-dialog").showModal();
  }

  $("pending-close").addEventListener("click", () => $("pending-dialog").close());
  $("pending-dialog").addEventListener("click", (event) => {
    if (event.target === $("pending-dialog")) $("pending-dialog").close();
  });
  renderPendingLine();

  // The "try exact passage" link, as stored: either the whole URL, or [which of this
  // document's links it extends, what it adds] — the same URL, minus the copy of the
  // document's address that every one of its excerpts would otherwise carry.
  function passageUrl(snippet, group) {
    const stored = snippet.passage_url;
    if (!Array.isArray(stored)) return stored || "";
    const link = ((group || {}).links || [])[stored[0]];
    return link ? String(link.url) + stored[1] : "";
  }

  function snippetHtml(snippet, group) {
    const text = String(snippet.text || "");
    const mark = snippet.mark;
    let passage = esc(text);
    if (mark && mark.length === 2) {
      passage = esc(text.slice(0, mark[0])) + "<mark>" +
        esc(text.slice(mark[0], mark[1])) + "</mark>" + esc(text.slice(mark[1]));
    }
    const where = snippet.where ? `<p class="where">${esc(snippet.where)}</p>` : "";
    const url = passageUrl(snippet, group);
    const link = url
      ? `<p class="source-links"><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">try exact passage →</a></p>`
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
    // Which law this document actually named, spelled out per predecessor — the row is
    // otherwise indistinguishable from one citing the current text.
    const viaBits = Object.entries(previousMentions(group))
      .sort((a, b) => b[1] - a[1])
      .map(([id, n]) => viaPhrase(n, id));
    const direct = mentionsForSelection
      - Object.values(previousMentions(group)).reduce((sum, n) => sum + Number(n || 0), 0);
    const countBits = viaBits.length
      ? [direct > 0
          ? `${number(direct)} direct ${direct === 1 ? "mention" : "mentions"}` : null,
         ...viaBits]
      : [`${number(mentionsForSelection)} ${mentionsForSelection === 1 ? "mention" : "mentions"}`];
    const details = [group.jurisdiction, kindNames[group.kind] || group.kind,
      group.court, group.source_label, ...countBits,
      group.version_mentions_by_key[state.key] ? "includes citations to the base act" : null]
      .filter(Boolean).map(esc).join(" · ");
    const excerpts = excerptsForSelection.length
      ? excerptsForSelection.map((snippet) => snippetHtml(snippet, group)).join("")
      : `<p class="result-meta">RagLex has the reference, but no excerpt is available.</p>`;
    // This row is in the list because it cited a DIFFERENT law. Offer the two texts
    // side by side, per predecessor, so the reader can judge the mapping themselves.
    const compare = Object.keys(previousMentions(group))
      .map((lawId) => compareLinksHtml(
        state.key, lawId,
        "This document cited the earlier provision —"))
      .join("");
    return `<article class="result" data-doc="${esc(group.id)}">
      <div class="result-head"><h3>${heading}</h3>${group.date ? `<time>${esc(group.date.slice(0, 4))}</time>` : ""}</div>
      <p class="result-meta">${flagHtml(group.jurisdiction)} ${details}</p>${targets}${compare}${excerpts}
      <p class="source-links">${links}</p>
    </article>`;
  }

  function renderFacetTokens() {
    const facets = new Map();
    for (const id of data.index[state.key] || []) {
      const group = data.groups[id];
      if (!group || !group.jurisdiction || !group.kind) continue;
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
    compareIndex.length = 0;   // rebuilt with the markup that references it
    renderRouteTokens();
    renderFacetTokens();
    const rows = selectedGroups();
    const visible = rows.slice(0, state.limit);
    $("results").innerHTML = visible.length
      ? visible.map(resultHtml).join("")
      : `<p class="empty">No documents match these filters.</p>`;
    bindCompareButtons($("results"));
    $("more-results").hidden = visible.length >= rows.length;
  }

  function closeMentions() {
    state.loadToken += 1;
    if (foregroundController) foregroundController.abort();
    $("mentions-dialog").close();
  }
  $("dialog-close").addEventListener("click", closeMentions);
  $("mentions-dialog").addEventListener("click", (event) => {
    if (event.target === $("mentions-dialog")) closeMentions();
  });
  $("more-results").addEventListener("click", () => { state.limit += 40; renderResults(); });
  $("sort-filter").addEventListener("change", () => {
    state.limit = 40;
    renderResults();
  });
})();
"""
