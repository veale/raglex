"""Chunking (§6b) — how text becomes vectors.

Retrieval quality is won or lost here. The principle: **chunk on the document's
own structural units, not on token count**. Legal documents are deeply structured
(numbered paragraphs, articles, France's zones), so we split on those seams, then
*size-normalise* within them — merge tiny units up to a floor, split oversized
units at sentence boundaries up to a ceiling — with a little overlap.

Each chunk also carries a compact **contextual header** prepended to its
*embedding input only* (not the stored display text): e.g.
``[NL · Hoge Raad · 2024 · data_protection · para] <chunk>`` pulls the vector
toward the right jurisdiction/topic neighbourhood and improves filtered retrieval
(§6b.4). The clean text is stored separately for display, citation, and char-span
mapping back into the text projection (§6b.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..core.models import Segment

# A legal-aware sentence splitter: don't break on the '.' inside "No. 12.3",
# "art. 1128", "Art.", "para.", numbered lists, or single initials.
_ABBREV = {
    "no", "nos", "art", "arts", "para", "paras", "pp", "p", "cf", "eg", "ie",
    "v", "vs", "ecli", "ehrr", "ewca", "ewhc", "uksc", "ch", "r",
}
_SENT_END_RE = re.compile(r"([.!?])\s+(?=[A-Z(\[])")


@dataclass(slots=True)
class Chunk:
    doc_id: str
    chunk_id: int
    text: str  # clean display/citation text
    embed_input: str  # text + contextual header (what actually gets embedded)
    structural_unit: str  # 'paragraph' | 'section' | 'block'
    char_start: int
    char_end: int


@dataclass(slots=True)
class ChunkConfig:
    min_tokens: int = 64
    target_tokens: int = 350
    max_tokens: int = 512
    overlap_tokens: int = 40


def _approx_tokens(text: str) -> int:
    # Word-count proxy; good enough to size chunks without a tokenizer dependency.
    return len(text.split())


def _split_sentences(text: str) -> list[str]:
    """Sentence split that respects legal abbreviations and numeric points."""
    out: list[str] = []
    start = 0
    for m in _SENT_END_RE.finditer(text):
        # guard: don't split right after a known abbreviation or a bare number
        prefix = text[start:m.start() + 1]
        last_word = re.findall(r"\b([A-Za-z]+)\.?$", prefix.strip())
        if last_word and last_word[-1].lower() in _ABBREV:
            continue
        if re.search(r"\b\d+\.$", prefix.strip()):  # "12." numbered point
            continue
        out.append(text[start:m.start() + 1].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out or [text.strip()]


def _structural_units(
    text: str, segments: list[Segment] | None = None
) -> list[tuple[str, tuple[str, ...], int, int]]:
    """Return (label, ancestor_path, char_start, char_end) units on the document's
    own seams.

    Structure-first (§6b, mirroring academic-mcp): if the adapter handed us
    ``segments`` — the source's native units (numbered paragraphs, Formex
    sections, France's zones) — chunk on *those*, and derive each unit's
    **ancestor path** (Part › Chapter › …) from the segment levels, so the
    contextual header can carry the hierarchy the multi-level literature says a
    provision's meaning depends on. Otherwise derive units from the flat text's
    paragraph breaks (empty paths). Either way char offsets index back into
    ``text``."""
    if segments:
        units: list[tuple[str, tuple[str, ...], int, int]] = []
        # stack of (level, label) — the open ancestors at this point in the walk
        stack: list[tuple[int, str]] = []
        for s in segments:
            level = s.level or 0
            while stack and stack[-1][0] >= level:
                stack.pop()
            path = tuple(lbl for _lvl, lbl in stack)
            units.append((s.label or s.kind, path, s.char_start, s.char_end))
            if s.label:
                stack.append((level, s.label))
        return units
    units = []
    pos = 0
    for block in re.split(r"(\n\s*\n)", text):
        if not block or block.isspace():
            pos += len(block)
            continue
        start = pos
        units.append(("paragraph", (), start, start + len(block)))
        pos += len(block)
    if not units:
        units.append(("block", (), 0, len(text)))
    return units


def _header(meta: dict | None, unit: str, path: tuple[str, ...] = ()) -> str:
    """The contextual header prepended to the *embedding input only* (§6b.4).

    Now carries the document title and the structural ancestor path — e.g.
    ``[UK · uksc · 2024 · Data Protection Act 2018 · Part 2 › Chapter 2 · s.45]``
    — so an enumerated leaf embeds with the hierarchy that gives it meaning
    (design §2.1). Kept compact: title truncated, path capped at 3 levels."""
    title = (meta or {}).get("title") or ""
    if len(title) > 80:
        title = title[:77] + "…"
    crumb = " › ".join(path[-3:]) if path else None
    if not meta:
        bits = [crumb, unit]
    else:
        bits = [
            meta.get("jurisdiction") or meta.get("source"),
            meta.get("court"),
            str(meta["year"]) if meta.get("year") else None,
            ",".join(meta["tags"]) if meta.get("tags") else None,
            title or None,
            crumb,
            unit,
        ]
    return "[" + " · ".join(b for b in bits if b) + "] "


def chunk_document(
    doc_id: str,
    text: str,
    *,
    segments: list[Segment] | None = None,
    meta: dict | None = None,
    config: ChunkConfig | None = None,
) -> list[Chunk]:
    """Structure-aware chunking with size normalisation + contextual headers.

    ``segments`` are the adapter's native structural units (§6b); when present they
    are the primary cut, so a chunk's ``structural_unit`` is the citable label
    ("[42]", "motivations") and its char span maps back into the source text."""
    cfg = config or ChunkConfig()
    if not text or not text.strip():
        return []

    # 1) structural split — on the source's own units when the adapter gave them
    raw_units = _structural_units(text, segments)

    # 2) merge tiny adjacent units up to the token floor (the first unit's path wins)
    merged: list[tuple[str, tuple[str, ...], int, int]] = []
    for unit, path, start, end in raw_units:
        if merged:
            plabel, ppath, pstart, pend = merged[-1]
            if _approx_tokens(text[pstart:pend]) < cfg.min_tokens:
                merged[-1] = (plabel, ppath, pstart, end)  # absorb into previous
                continue
        merged.append((unit, path, start, end))

    # 3) split oversized units at sentence boundaries up to the ceiling, with a
    #    small sentence-level overlap (clamped below the target so it can't stall)
    chunks: list[Chunk] = []
    cid = 0
    eff_overlap = min(cfg.overlap_tokens, max(1, cfg.target_tokens // 3))
    for unit, path, start, end in merged:
        body = text[start:end]
        if _approx_tokens(body) <= cfg.max_tokens:
            cid = _emit(chunks, doc_id, cid, body, unit, start, meta, path)
            continue
        sentences = _split_sentences(body)
        spans = _sentence_spans(body, sentences)
        i = 0
        while i < len(sentences):
            j, toks = i, 0
            while j < len(sentences) and toks < cfg.target_tokens:
                toks += _approx_tokens(sentences[j])
                j += 1
            seg = " ".join(sentences[i:j])
            cid = _emit(chunks, doc_id, cid, seg, unit, start + spans[i][0], meta, path)
            if j >= len(sentences):
                break
            # step back a few sentences for overlap, but always make progress
            k, otoks = j, 0
            while k > i + 1 and otoks < eff_overlap:
                k -= 1
                otoks += _approx_tokens(sentences[k])
            i = max(k, i + 1)
    return chunks


# The reserved chunk id for a document-LEVEL vector (design §2.2). Lives in the
# same embeddings table/family as the leaf chunks; retrieval's containment rule
# keeps it from duplicating results when its own leaves also hit.
DOC_CHUNK_ID = -1


def doc_proxy_chunk(doc_id: str, text: str, *, meta: dict | None = None) -> Chunk | None:
    """One document-level chunk answering "which case/instrument is about this" —
    a *synthetic proxy*, never the raw document (mean-pooling 400 paragraphs is
    vector soup): title + the opening (facts/subject are front-loaded) + the tail
    (judgments put the holding/dispositif at the end). When a real headnote or an
    LLM summary exists upstream, callers can pass it via ``meta['summary']`` and
    it wins outright."""
    if not text or not text.strip():
        return None
    summary = (meta or {}).get("summary")
    if summary:
        proxy = summary
    else:
        head = text[:1400].strip()
        tail = text[-900:].strip() if len(text) > 2600 else ""
        title = (meta or {}).get("title") or ""
        proxy = "\n".join(p for p in (title, head, "…", tail) if p) if tail \
            else "\n".join(p for p in (title, head) if p)
    return Chunk(
        doc_id=doc_id,
        chunk_id=DOC_CHUNK_ID,
        text=proxy,
        embed_input=_header(meta, "doc") + proxy,
        structural_unit="doc",
        char_start=0,
        char_end=min(len(text), 1400),
    )


def _sentence_spans(body: str, sentences: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for sent in sentences:
        idx = body.find(sent, cursor)
        if idx < 0:
            idx = cursor
        spans.append((idx, idx + len(sent)))
        cursor = idx + len(sent)
    return spans


def _emit(chunks, doc_id, cid, body, unit, char_start, meta, path=()) -> int:
    body = body.strip()
    if not body:
        return cid
    chunks.append(
        Chunk(
            doc_id=doc_id,
            chunk_id=cid,
            text=body,
            embed_input=_header(meta, unit, path) + body,  # header in embedding input only
            structural_unit=unit,
            char_start=char_start,
            char_end=char_start + len(body),
        )
    )
    return cid + 1
