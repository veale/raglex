"""Treatment-classification stage (§1.3a).

Walks citing documents' *bare* (`mentions`) citation edges, reads the prose around
each (the span the extractor stored), and reclassifies the treatment. Only bare
edges are touched — authoritative typed edges (e.g. NL FormeleRelaties `applies`)
are left untouched. Re-runnable and idempotent.

Classification goes through the ``TreatmentClassifier`` interface, so the
heuristic (default) and the LLM classifier are interchangeable. The whole corpus's
eligible contexts are gathered and handed to ``classify_batch`` in one go, so the
LLM path makes as few requests as possible (§5) rather than one per citation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.models import ExtractedVia, RelationshipType
from ..storage.catalogue import Catalogue
from ..storage.textstore import TextStore
from .classifier import HeuristicTreatmentClassifier, TreatmentClassifier

# Treatment verbs precede the citation ("the court followed X"), so read the text
# immediately before it; a trailing window risks picking up the *next* citation's
# verb when several sit in one sentence.
_BEFORE = 200
_AFTER = 0
# Trim the preceding window at the previous sentence boundary so one sentence's
# verb ("…distinguished X.") doesn't bleed onto the next sentence's citation
# ("It also noted Y").
_SENTENCE_END = re.compile(r"[.;:?!]\s+(?=[A-Z(\[])")


def _sentence_context(text: str, start: int, end: int) -> str:
    window = text[max(0, start - _BEFORE): end + _AFTER]
    boundaries = list(_SENTENCE_END.finditer(window[: _BEFORE if start >= _BEFORE else start]))
    return window[boundaries[-1].end():] if boundaries else window


@dataclass(slots=True)
class TreatmentStats:
    edges_examined: int = 0
    reclassified: int = 0

    def summary(self) -> str:
        return f"[treatment] examined={self.edges_examined} reclassified={self.reclassified}"


def _collect(catalogue: Catalogue, textstore: TextStore, src_id: str):
    """Yield ``(relation_id, context, entity_kind)`` for each bare, span-carrying
    edge of one document. Authoritative typed edges are skipped here."""
    doc = catalogue.get_document(src_id)
    if doc is None or not doc["payload_hash"]:
        return
    try:
        text = textstore.get(doc["payload_hash"])
    except OSError:
        return
    kind_by_start = {c["char_start"]: c["entity_kind"] for c in catalogue.citations_for(src_id)}
    for rel in catalogue.relations_for(src_id):
        if rel["relationship_type"] != str(RelationshipType.MENTIONS):
            continue  # never override an authoritative typed edge
        start, end = rel["context_start"], rel["context_end"]
        if start is None:
            continue
        context = _sentence_context(text, start, end)
        yield rel["relation_id"], context, kind_by_start.get(start)


def classify_document(
    catalogue: Catalogue,
    textstore: TextStore,
    src_id: str,
    *,
    classifier: TreatmentClassifier | None = None,
    method: str = str(ExtractedVia.REGEX),
) -> int:
    clf = classifier or HeuristicTreatmentClassifier()
    rows = list(_collect(catalogue, textstore, src_id))
    if not rows:
        return 0
    treatments = clf.classify_batch([(ctx, kind) for _, ctx, kind in rows])
    n = 0
    for (relation_id, _, _), treatment in zip(rows, treatments):
        if treatment and treatment != RelationshipType.MENTIONS:
            catalogue.set_relationship_type(relation_id, str(treatment), extracted_via=method)
            n += 1
    return n


def classify_corpus(
    catalogue: Catalogue, textstore: TextStore, *, classifier: TreatmentClassifier | None = None,
    stable_id: str | None = None, method: str = str(ExtractedVia.REGEX),
) -> TreatmentStats:
    """Classify treatments across the corpus (or one document). Gathers every
    eligible context first so ``classify_batch`` runs once over the lot (the LLM
    classifier batches its requests internally), then writes the reclassifications."""
    clf = classifier or HeuristicTreatmentClassifier()
    targets = [stable_id] if stable_id else [
        r["stable_id"] for r in catalogue.list_documents(limit=100000) if r["has_text"]
    ]
    gathered: list[tuple[int, str, str | None]] = []
    for sid in targets:
        gathered.extend(_collect(catalogue, textstore, sid))

    stats = TreatmentStats(edges_examined=len(gathered))
    if not gathered:
        return stats
    treatments = clf.classify_batch([(ctx, kind) for _, ctx, kind in gathered])
    for (relation_id, _, _), treatment in zip(gathered, treatments):
        if treatment and treatment != RelationshipType.MENTIONS:
            catalogue.set_relationship_type(relation_id, str(treatment), extracted_via=method)
            stats.reclassified += 1
    return stats
