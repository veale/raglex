"""Treatment classification (§1.3a) — mentions → how one case treats another.

A bare citation throws away the single most valuable signal in case law: *how* a
court treats the authority it cites. This reclassifies a ``mentions`` edge into a
real treatment (``follows`` / ``distinguishes`` / ``overrules`` / ``applies`` /
``considers``) by reading the prose around the citation — the span the extractor
stored on the edge (``context_start``/``context_end``).

Two implementations behind one interface (the design's "run an LLM over the
surrounding text chunk to reclassify" is then a drop-in, batched per §5):
- ``HeuristicTreatmentClassifier`` — fast, deterministic cue-phrase matching;
- an LLM classifier slots in with the same ``classify(context, ...)`` signature.

Only *case* citations get a treatment; a citation to a statute stays as-is
(its `interprets`/`mentions` already says the right thing).
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from ..core.models import RelationshipType

# Cue phrases → treatment, ordered by strength (first match wins). Word-stems so
# "followed"/"following"/"follows" all hit.
_CUES: list[tuple[re.Pattern[str], RelationshipType]] = [
    (re.compile(r"\boverrul", re.I), RelationshipType.OVERRULES),
    (re.compile(r"\b(depart(ed|ing|s)? from|declin\w+ to follow|disapprov)", re.I), RelationshipType.OVERRULES),
    (re.compile(r"\bdistinguish", re.I), RelationshipType.DISTINGUISHES),
    (re.compile(r"\bfollow(ed|ing|s)?\b", re.I), RelationshipType.FOLLOWS),
    (re.compile(r"\b(approv|endors|affirm)", re.I), RelationshipType.FOLLOWS),
    (re.compile(r"\bappl(y|ied|ies|ying)\b", re.I), RelationshipType.APPLIES),
    (re.compile(r"\b(consider|discuss|cited|referr?ed to|not(?:e|ed|ing))\b", re.I), RelationshipType.CONSIDERS),
]


@runtime_checkable
class TreatmentClassifier(Protocol):
    def classify(self, context: str, *, entity_kind: str | None = None) -> RelationshipType | None:
        """Return a treatment for the citation given its surrounding prose, or None
        to leave the edge unchanged."""
        ...

    def classify_batch(
        self, contexts: list[tuple[str, str | None]]
    ) -> list[RelationshipType | None]:
        """Classify many ``(context, entity_kind)`` pairs at once (the LLM path
        sends one request per batch, §5). Length-aligned with the input."""
        ...


class HeuristicTreatmentClassifier:
    """Cue-phrase classifier — cheap and deterministic; the always-available first
    pass before (optional) LLM reclassification."""

    def classify(self, context: str, *, entity_kind: str | None = None) -> RelationshipType | None:
        """``context`` should be biased to the text immediately *before* the
        citation (treatment verbs precede it). When several cues appear, the one
        nearest the citation (latest in the context) wins — so "followed X … but
        distinguished Y" assigns each citation its own verb."""
        if entity_kind not in (None, "case", "opinion"):
            return None  # treatment only applies to case-law citations
        best: RelationshipType | None = None
        best_pos = -1
        for pattern, treatment in _CUES:
            for m in pattern.finditer(context):
                if m.start() > best_pos:
                    best_pos, best = m.start(), treatment
        return best

    def classify_batch(
        self, contexts: list[tuple[str, str | None]]
    ) -> list[RelationshipType | None]:
        return [self.classify(c, entity_kind=k) for c, k in contexts]


# Treatments the LLM is allowed to assign, by their wire name → enum.
_LLM_LABELS = {
    "overrules": RelationshipType.OVERRULES,
    "distinguishes": RelationshipType.DISTINGUISHES,
    "follows": RelationshipType.FOLLOWS,
    "applies": RelationshipType.APPLIES,
    "considers": RelationshipType.CONSIDERS,
    "mentions": RelationshipType.MENTIONS,
}

_LLM_SYSTEM = (
    "You are a legal-citation analyst. For each excerpt, a court is citing another "
    "case at the point marked «CITED». Decide how the citing court treats that "
    "authority. Choose exactly one label from: overrules, distinguishes, follows, "
    "applies, considers, mentions. Use 'overrules' for departing from / declining "
    "to follow / disapproving; 'follows' for approving/affirming/endorsing; "
    "'considers' for neutral discussion; 'mentions' when there is no discernible "
    "treatment."
)


class LLMTreatmentClassifier:
    """Optional LLM pass for the cases the cue phrases miss — implicit treatment
    ("the reasoning in X cannot stand"), treatment expressed across a clause, etc.
    Batched (§5) and resilient: if the model is unavailable or unsure it returns
    ``None`` for that item, so the heuristic result stands (see the stage). Only
    case/opinion citations are sent."""

    def __init__(self, client=None, *, fallback: "TreatmentClassifier | None" = None) -> None:
        from ..llm import get_llm_client

        self._client = client or get_llm_client()
        self._fallback = fallback or HeuristicTreatmentClassifier()

    def classify(self, context: str, *, entity_kind: str | None = None) -> RelationshipType | None:
        return self.classify_batch([(context, entity_kind)])[0]

    def classify_batch(
        self, contexts: list[tuple[str, str | None]]
    ) -> list[RelationshipType | None]:
        out: list[RelationshipType | None] = [None] * len(contexts)
        # Only case-law citations are eligible; collect those indices.
        eligible = [i for i, (_, k) in enumerate(contexts) if k in (None, "case", "opinion")]
        if not eligible or not self._client.available():
            # Degrade to the heuristic entirely (still useful, never worse).
            return self._fallback.classify_batch(contexts)
        prompts = [f"…{contexts[i][0]} «CITED»…" for i in eligible]
        results = self._client.json_batch(
            _LLM_SYSTEM, prompts,
            instruction='For each excerpt return {"index": i, "treatment": "<label>"}.',
        )
        for slot, entry in zip(eligible, results):
            label = (entry or {}).get("treatment") if isinstance(entry, dict) else None
            treatment = _LLM_LABELS.get(str(label).strip().lower()) if label else None
            if treatment is None:
                # model abstained → keep the deterministic answer for this item
                treatment = self._fallback.classify(contexts[slot][0], entity_kind=contexts[slot][1])
            out[slot] = treatment
        return out
