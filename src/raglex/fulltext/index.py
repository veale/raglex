"""Building the free-text index, and answering a query against it.

Two halves, and the second is where "literal" is actually delivered.

**Building** walks the gated scope, reads each document's stored text and writes a
tsvector. No model, no GPU: measured at roughly 20-30 MB/s of text per core, so the
whole UK+EU slice is about an hour, dominated by reading text rather than by
tokenising it.

**Searching** narrows with the tsvector and then, for a quoted phrase, checks the
literal characters against the document's own text. That second step is what makes a
quotation mean what it says: Postgres stems, so ``"duty of care"`` retrieves
documents reading "duties of care", and only the text itself can tell them apart. It
is affordable because the document store is local — measured at 0.046 ms a document
against 22.57 ms over the NFS mount it used to sit behind, a 495x difference — so
verifying a few hundred candidates costs about as much as the ranking did.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from .query import ParsedQuery, Phrase, parse, to_tsquery

log = logging.getLogger(__name__)

# How many documents the tsvector stage may hand to verification. Each costs ~0.05 ms
# to read locally, so a few thousand is still inside a typing-speed budget; beyond
# that the honest answer is "narrow your query" rather than a slow page.
CANDIDATE_BUDGET = 4000


@dataclass(slots=True)
class Hit:
    doc_id: str
    rank: float
    #: character offset of the first literal match, when there was one to find
    char_start: int | None = None
    snippet: str = ""
    #: (start, end) offsets WITHIN ``snippet`` of the matched words, so the reader
    #: sees what matched rather than a window of text they have to re-scan
    highlights: list[tuple[int, int]] = field(default_factory=list)


@dataclass(slots=True)
class SearchResult:
    hits: list[Hit] = field(default_factory=list)
    #: EVERY matching document id, best-ranked first — not just the page. The facet
    #: counts have to describe the whole result set (a reader told "912 documents"
    #: and shown a breakdown of 40 of them has been misled), and holding the full
    #: set is also what lets a facet click narrow instantly, with no second query.
    matched: list[str] = field(default_factory=list)
    #: documents matching the tsquery — the real total, not the page size
    total: int = 0
    #: how many survived literal verification, when exact mode was on
    verified: int | None = None
    candidates: int = 0
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    took_ms: int = 0
    tsquery: str | None = None


# -- literal verification ------------------------------------------------------
def _literal_re(phrase: Phrase) -> "re.Pattern[str]":
    """A pattern for the phrase as WRITTEN, tolerant only of whitespace.

    The words are matched with word boundaries so "care" does not match "careless",
    and the gaps between them accept any run of whitespace or punctuation — which is
    what makes the pattern survive line wrapping, and the double spaces and stray
    hyphens that PDF extraction leaves behind."""
    gap = r"[\s\W]{0,4}" if phrase.distance <= 1 else r"(?:\W+\w+){0,%d}\W+" % (
        phrase.distance - 1)
    body = gap.join(re.escape(w) for w in phrase.words)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


def find_literal(text: str, phrase: Phrase) -> int | None:
    """Offset of the phrase in ``text``, or None. Case-insensitive: a reader quoting
    a sentence should not have to reproduce its capitalisation."""
    m = _literal_re(phrase).search(text or "")
    return m.start() if m else None


def verify(text: str, parsed: ParsedQuery) -> int | None:
    """Does this document really contain what was quoted? Returns the offset of the
    first required phrase (for the snippet), or None if the document fails.

    Every positive literal must be present and every excluded one absent. Exclusion
    is applied HERE rather than in the tsquery because a stemmed NOT over-excludes:
    ``-"duty of care"`` would drop a document containing only "duties of care", which
    does not contain the string it was asked to exclude, and a document that was
    never retrieved cannot be recovered."""
    first: int | None = None
    for ph in parsed.literals:
        at = find_literal(text, ph)
        if at is None:
            return None
        if first is None or at < first:
            first = at
    for ph in parsed.excluded:
        if find_literal(text, ph) is not None:
            return None
    return first if first is not None else 0


def highlight_spans(fragment: str, parsed: ParsedQuery) -> list[tuple[int, int]]:
    """Where, inside a snippet, the query actually matched.

    Quoted phrases are marked as whole phrases (that is what the reader asked for);
    otherwise each searched word is marked wherever it appears. Prefix terms mark the
    whole word they complete, so ``neglig*`` highlights "negligence", not "neglig"."""
    from .query import And, Near, Not, Or, Term

    spans: list[tuple[int, int]] = []
    for ph in parsed.literals:
        for m in _literal_re(ph).finditer(fragment):
            spans.append((m.start(), m.end()))

    def walk(node) -> None:
        if isinstance(node, Not):
            return
        if isinstance(node, Term):
            pat = rf"\b{re.escape(node.word)}" + ("\\w*" if node.prefix else r"\b")
            for m in re.finditer(pat, fragment, re.IGNORECASE):
                spans.append((m.start(), m.end()))
        elif isinstance(node, Phrase):
            for m in _literal_re(node).finditer(fragment):
                spans.append((m.start(), m.end()))
        elif isinstance(node, Near):
            walk(node.left)
            walk(node.right)
        elif isinstance(node, (And, Or)):
            for c in node.children:
                walk(c)

    walk(parsed.node)
    if not spans:
        return []
    # merge overlaps so the reader never sees nested marks
    spans.sort()
    merged = [spans[0]]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def snippet(text: str, at: int | None, *, width: int = 320) -> str:
    """A window of text around the match, cut on word boundaries."""
    if not text:
        return ""
    if at is None:
        at = 0
    start = max(0, at - width // 3)
    end = min(len(text), start + width)
    frag = text[start:end]
    if start:
        frag = frag.split(" ", 1)[-1]
    if end < len(text):
        frag = frag.rsplit(" ", 1)[0]
    return " ".join(frag.split()) + ("…" if end < len(text) else "")


# -- searching -----------------------------------------------------------------
def search(cat, ts, query: str, *, filters: dict | None = None, exact: bool = True,
           limit: int = 25, offset: int = 0,
           budget: int = CANDIDATE_BUDGET) -> SearchResult:
    """Answer a free-text query over the gated scope.

    ``exact`` decides what a quoted string means: the literal characters (verified
    against the text), or Postgres's stemmed phrase match, which also finds
    "duties of care" for ``"duty of care"``."""
    t0 = time.perf_counter()
    parsed = parse(query, exact=exact)
    tsq = to_tsquery(parsed, exact=exact)
    out = SearchResult(notes=list(parsed.notes), tsquery=tsq)
    if tsq is None:
        if not out.notes and query.strip():
            out.notes.append("Nothing in that query can be looked up.")
        out.took_ms = int(1000 * (time.perf_counter() - t0))
        return out

    out.total = cat.fts_total(tsq, filters=filters)
    want = limit + offset
    rows = cat.fts_search(tsq, filters=filters, limit=budget)
    out.candidates = len(rows)
    out.truncated = len(rows) >= budget

    needs_check = bool(parsed.literals or parsed.excluded)
    seen: set[str] = set()
    matched: list[tuple[str, float, int | None]] = []
    # one batched lookup for the whole candidate set, not one query per document
    hashes = _hashes_for(cat, list({r["doc_id"] for r in rows})) if needs_check else {}
    for r in rows:
        doc_id = r["doc_id"]
        if doc_id in seen:
            continue
        seen.add(doc_id)
        if not needs_check:
            matched.append((doc_id, float(r.get("rank") or 0), None))
            continue
        # EVERY candidate is verified, not merely enough of them to fill a page:
        # the facets and the count must describe the real result set. At 0.046 ms a
        # document (local store, measured) a full 4,000-candidate budget is ~0.2 s.
        ph = hashes.get(doc_id)
        if not ph:
            continue
        try:
            text = ts.get(ph)
        except OSError:
            continue
        at = verify(text, parsed)
        if at is None:
            continue
        matched.append((doc_id, float(r.get("rank") or 0), at))
    if needs_check:
        out.verified = len(matched)
        # the honest count is what survived verification; the tsquery total counts
        # the stemmed matches, which for an exact search overstates
        out.total = len(matched) if not out.truncated else max(len(matched), out.total)
    out.matched = [m[0] for m in matched]

    # snippets are built only for the page being shown — the text was read during
    # verification but 4,000 documents is 124 MB, far too much to hold
    for doc_id, rank, at in matched[offset:want]:
        text = _text_of(cat, ts, doc_id)
        if text is None:
            continue
        where = at if at is not None else _first_term_at(text, parsed)
        frag = snippet(text, where)
        out.hits.append(Hit(doc_id=doc_id, rank=rank, char_start=where, snippet=frag,
                            highlights=highlight_spans(frag, parsed)))
    out.took_ms = int(1000 * (time.perf_counter() - t0))
    return out


def _first_term_at(text: str, parsed: ParsedQuery) -> int | None:
    from .query import And, Near, Not, Or, Term

    def walk(node) -> str | None:
        if isinstance(node, Term):
            return node.word
        if isinstance(node, Phrase):
            return node.words[0] if node.words else None
        if isinstance(node, Not):
            return None
        if isinstance(node, Near):
            return walk(node.left) or walk(node.right)
        if isinstance(node, (And, Or)):
            for c in node.children:
                if (w := walk(c)):
                    return w
        return None

    word = walk(parsed.node)
    if not word:
        return None
    m = re.search(rf"\b{re.escape(word)}", text, re.IGNORECASE)
    return m.start() if m else None


def _hashes_for(cat, doc_ids: list[str]) -> dict[str, str]:
    """doc_id → payload_hash for a whole candidate set, in batched queries.

    This used to be one ``get_document`` per candidate, which is what made the first
    real search take 34 seconds: reading the text is 0.046 ms but asking Postgres
    where it lives, four thousand times over, is not. Verification is now bounded by
    the file reads it was supposed to be bounded by."""
    out: dict[str, str] = {}
    for i in range(0, len(doc_ids), 900):
        chunk = doc_ids[i:i + 900]
        rows = cat.conn.execute(
            "SELECT stable_id, payload_hash FROM documents "
            f"WHERE stable_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
        for r in rows:
            if r["payload_hash"]:
                out[r["stable_id"]] = r["payload_hash"]
    return out


def _text_of(cat, ts, doc_id: str) -> str | None:
    doc = cat.get_document(doc_id)
    if not doc or not doc["payload_hash"]:
        return None
    try:
        return ts.get(doc["payload_hash"])
    except OSError:
        return None


# -- building ------------------------------------------------------------------
def build(cat, ts, *, sources: list[str] | None = None, limit: int = 1_000_000,
          reindex: bool = False, on_progress=None, cancel_check=None) -> dict:
    """Index the gated scope. Resumable: a document already indexed is skipped unless
    ``reindex``, so an interrupted run continues rather than starting over."""
    st = {"scanned": 0, "indexed": 0, "parts": 0, "skipped": 0, "unreadable": 0,
          "chars": 0}
    where = "WHERE has_text = 1 AND search_excluded = 0"
    params: list[object] = []
    if sources:
        where += f" AND source IN ({','.join('?' * len(sources))})"
        params.extend(sources)
    rows = cat.conn.execute(
        f"SELECT stable_id, payload_hash FROM {'documents'} {where} "
        "ORDER BY stable_id LIMIT ?", params + [limit]).fetchall()
    done = set() if reindex else cat.fts_indexed_ids()
    total = len(rows)
    for n, r in enumerate(rows, 1):
        if cancel_check and cancel_check():
            break
        sid = r["stable_id"]
        st["scanned"] += 1
        if sid in done:
            st["skipped"] += 1
            continue
        if not r["payload_hash"]:
            continue
        try:
            text = ts.get(r["payload_hash"])
        except OSError:
            st["unreadable"] += 1
            continue
        if not text.strip():
            continue
        try:
            st["parts"] += cat.put_doc_fts(sid, text, commit=False)
        except Exception:  # noqa: BLE001 — one bad document must not end the run
            log.warning("[fts] %s failed to index", sid, exc_info=True)
            continue
        st["indexed"] += 1
        st["chars"] += len(text)
        if st["indexed"] % 500 == 0:
            cat.conn.commit()
            if on_progress:
                on_progress(stage="building free-text index", done=n, total=total,
                            item=sid)
    cat.conn.commit()
    return st
