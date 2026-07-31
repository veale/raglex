"""Re-anchor stored citation offsets to a document's *current* text.

When a source is reparsed from its immutable raw (a parser upgrade — the fr-dila/de-rii
paragraphing fixes, the BWB lid/li fix), the regenerated text gains or loses whitespace
(inserted newlines, collapsed runs). The extracted ``citations`` rows, however, keep the
``char_start``/``char_end`` computed on the *old* text, so the reader — which slices
``text[char_start:char_end]`` — highlights the wrong span (a few characters adrift, the
error accumulating down the document).

The fix does **not** need re-extraction: the citation ``raw`` strings, their candidates,
pinpoints and resolved targets are all still correct — only the offsets moved. Because the
reparse changes *whitespace*, not content or its order, each ``raw`` can be re-located in
the new text by a whitespace-flexible, in-order sweep:

  * **whitespace-flexible** — every whitespace run in ``raw`` matches ``\\s+``, so a
    citation the reparse split across an inserted newline still matches;
  * **in order, with a monotonic cursor** — citations are re-anchored left to right, each
    search starting where the previous match ended, so repeated strings ("the Government"
    twice) map to the right occurrence and can't cross-match.

A ``raw`` that no longer appears at/after the cursor (content the parser genuinely moved or
dropped, not just re-spaced) falls back to the whole-document occurrence nearest its old
position; if it appears nowhere it is left untouched and counted ``unlocatable`` — never
mis-anchored. Only rows whose offsets actually changed are returned, so the write is minimal.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence

_WS = re.compile(r"\s+")


def _pattern(raw: str) -> "re.Pattern[str]":
    """A regex matching ``raw`` with each internal whitespace run relaxed to ``\\s+`` —
    so an inserted newline inside the citation doesn't defeat the match."""
    toks = [t for t in _WS.split(raw.strip()) if t]
    return re.compile(r"\s+".join(re.escape(t) for t in toks))


def aligned_span(
    text: str, raw: str | None, start: int | None, end: int | None, *,
    radius: int = 5000,
) -> tuple[int, int] | None:
    """Return the real span of ``raw`` near a possibly stale stored offset.

    Readers and static exports must never mark arbitrary nearby words just because a
    legacy reparse shifted the relation copy of a citation span.  The durable re-anchor
    job repairs those rows in bulk; this cheap read-side guard makes every surface correct
    immediately, including a source not yet swept.  An already-correct span is O(length
    of citation); only a mismatch searches a bounded neighbourhood.  ``None`` means the
    raw citation cannot be confirmed nearby, so callers should show an unmarked preview
    rather than a confidently wrong highlight.
    """
    if not text or start is None or not (raw or "").strip():
        return None
    old_start = max(0, min(int(start), len(text)))
    old_end = old_start if end is None else max(old_start, min(int(end), len(text)))
    pat = _pattern(str(raw))
    if pat.fullmatch(text[old_start:old_end]):
        return old_start, old_end
    lo = max(0, old_start - max(0, int(radius)))
    hi = min(len(text), old_end + max(0, int(radius)))
    matches = list(pat.finditer(text, lo, hi))
    if not matches:
        return None
    best = min(matches, key=lambda match: abs(match.start() - old_start))
    return best.start(), best.end()


def reanchor(text: str, rows: Sequence[Mapping]) -> tuple[list[tuple[int, int, int]], int]:
    """Re-locate each citation ``raw`` in ``text``.

    ``rows`` are mapping-like with ``citation_id``, ``raw``, ``char_start``, ``char_end``.
    Returns ``(updates, unlocatable)`` where ``updates`` is ``[(citation_id, start, end), …]``
    for the rows whose offsets changed, and ``unlocatable`` counts rows whose ``raw`` no
    longer appears at all (left as-is).
    """
    if not text:
        return [], 0
    updates: list[tuple[int, int, int]] = []
    unlocatable = 0
    cursor = 0
    ordered = sorted(
        rows, key=lambda r: (r["char_start"] if r["char_start"] is not None else -1))
    for r in ordered:
        raw = (r["raw"] or "").strip()
        if not raw:
            continue
        pat = _pattern(raw)
        m = pat.search(text, cursor)
        if m is None:
            # content the parser moved/removed — take the whole-document occurrence
            # nearest the old position, without rewinding the cursor.
            old = r["char_start"] if r["char_start"] is not None else 0
            best = None
            for cand in pat.finditer(text):
                if best is None or abs(cand.start() - old) < abs(best.start() - old):
                    best = cand
            if best is None:
                unlocatable += 1
                continue
            m = best
        ns, ne = m.start(), m.end()
        cursor = max(cursor, ne)
        if ns != r["char_start"] or ne != r["char_end"]:
            updates.append((r["citation_id"], ns, ne))
    return updates, unlocatable
