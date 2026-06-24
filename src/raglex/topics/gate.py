"""Two-stage topic gate (§4, §1.7).

Stage 1 — ``cheap_match`` at discovery, over a stub's cheap fields (title/court):
returns True (keep), False (drop), or None ("fetch and confirm"). Drops obvious
off-topic before any fetch. In-scope-by-construction sources skip the gate.

Stage 2 — ``confirm`` post-fetch, over full text: a weighted term score plus the
tags it matched, thresholded to drop weak matches.

This is the §4 logic that build step 4 re-expresses as editable rules (§4a); the
shapes here (tags, weighted terms, thresholds) map straight onto that engine.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from ..core.models import Stub
from .vocab import IN_SCOPE_COURTS, VOCABULARIES


def fold(text: str) -> str:
    """Case-fold and accent-fold (§4a `literal` semantics) so 'données' matches
    'donnees' and 'DSGVO' matches 'dsgvo'."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


@dataclass(frozen=True, slots=True)
class TopicResult:
    keep: bool
    score: float
    tags: tuple[str, ...]


def _court_in_scope(court: str | None) -> bool:
    if not court:
        return False
    token = fold(court).strip()
    return token in IN_SCOPE_COURTS or any(h in token for h in IN_SCOPE_COURTS)


def cheap_match(stub: Stub) -> bool | None:
    """Stage 1 gate. True = keep, False = drop, None = fetch-and-confirm (§4)."""
    if _court_in_scope(stub.court):
        return True  # in-scope by construction — skip the gate, tag later

    haystack = fold(" ".join(p for p in (stub.title, stub.court) if p))
    if not haystack:
        return None  # nothing cheap to judge on — fetch and confirm

    for vocab in VOCABULARIES.values():
        for term in vocab:
            if fold(term) in haystack:
                return True
    # We have a title and it matched nothing topical. Don't hard-drop on title
    # alone (titles are terse); defer to stage 2.
    return None


def score_text(text: str) -> tuple[float, dict[str, float]]:
    """Weighted term score over folded text, plus per-tag subtotals (§4 stage 2)."""
    folded = fold(text)
    per_tag: dict[str, float] = {}
    for tag, vocab in VOCABULARIES.items():
        subtotal = 0.0
        for term, weight in vocab.items():
            if fold(term) in folded:
                subtotal += weight
        if subtotal:
            per_tag[tag] = subtotal
    total = sum(per_tag.values())
    return total, per_tag


def confirm(text: str, *, threshold: float = 3.0) -> TopicResult:
    """Stage 2 gate (§4). Keep when the weighted score clears ``threshold``; the
    matched tags become the document's topic tags."""
    total, per_tag = score_text(text)
    tags = tuple(t for t, sub in sorted(per_tag.items(), key=lambda kv: -kv[1]) if sub > 0)
    return TopicResult(keep=total >= threshold, score=total, tags=tags)
