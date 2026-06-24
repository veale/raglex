"""Entity resolution stage (§5b) — turn citation strings into graph nodes.

This is the step that converts a pile of documents into a *traversable graph*.
Extraction leaves a ``raw_citation_string`` on each edge (resolution_status =
pending); this resolver maps it to a stable ``dst_id`` and flips the edge live.

The resolution ladder, cheapest/highest-confidence first:
  1. known-alias lookup (the maintained "Schrems II" → ECLI map);
  2. structured pattern match (ECLI / CELEX / UK neutral citation / legislation URI);
then confirm the candidate is actually a node in the corpus. A candidate that
isn't present *yet* stays pending — usually the target just hasn't been harvested,
so resolution is **retried** after each ingest cycle and the citation becomes a
live edge the moment the target arrives. Unresolved strings rank by ``cite_count``
into a harvest worklist (§8). Fuzzy/semantic and LLM rungs are later build steps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..core.models import sha256_bytes
from ..storage.catalogue import Catalogue
from ..topics.gate import fold
from .matchers import Candidate, first_candidate

log = logging.getLogger("raglex.resolve")


def string_hash(raw: str) -> str:
    """Stable key for the pending queue — folded so trivial variants coalesce."""
    return sha256_bytes(fold(raw).encode("utf-8"))


@dataclass(slots=True)
class ResolveStats:
    resolved: int = 0
    still_pending: int = 0
    aliases_added: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"[resolve] resolved={self.resolved} still_pending={self.still_pending} "
            f"aliases_added={self.aliases_added}"
        )


class Resolver:
    def __init__(self, catalogue: Catalogue, *, grow_aliases: bool = True) -> None:
        self.catalogue = catalogue
        # When a structured match resolves, cache the colloquial string → id so a
        # future identical citation resolves by the cheap alias rung (§5b).
        self.grow_aliases = grow_aliases

    def candidate_for(self, raw: str) -> Candidate | None:
        """Run the ladder: alias first (cheapest), then structured patterns."""
        alias_dst = self.catalogue.get_alias(fold(raw))
        if alias_dst:
            return Candidate(value=alias_dst, method="alias")
        return first_candidate(raw)

    def resolve_one(self, raw: str) -> tuple[str | None, Candidate | None]:
        """Return (dst_id_if_present_in_corpus, candidate). dst_id is None when the
        candidate isn't a node yet — a pending state, not a failure (§5b)."""
        cand = self.candidate_for(raw)
        if cand is None:
            return None, None
        return self.catalogue.find_document_id(cand.value), cand

    def run(self) -> ResolveStats:
        """Resolve every pending edge once. Safe to re-run after each ingest (§5b):
        re-running is how citations to newly-harvested targets become live edges."""
        stats = ResolveStats()
        pending = self.catalogue.pending_relations()

        # 1. Work out each edge's candidate id once (adapter-supplied dst, else the
        #    matcher ladder over the raw string), collecting the distinct candidates.
        edges: list[tuple[int, str, str | None, object]] = []  # (relation_id, raw, candidate, Candidate|None)
        cand_set: set[str] = set()
        for rel in pending:
            raw = rel["raw_citation_string"]
            supplied = rel["dst_id"]
            if supplied:
                cand_obj, cand = None, supplied
            else:
                cand_obj = self.candidate_for(raw)
                cand = cand_obj.value if cand_obj else None
            edges.append((rel["relation_id"], raw, cand, cand_obj))
            if cand:
                cand_set.add(cand)

        # 2. ONE batch query: which candidates are present as documents (the per-edge
        #    find_document_id over 100k+ edges was the bottleneck, alongside the commits).
        present = self.catalogue.find_existing(cand_set)

        # 3. Resolve the edges whose candidate is present (deferred commit); the rest
        #    simply stay pending — the worklist is derived from the relations graph, so
        #    there's no per-edge worklist write to redo every run (and no count inflation).
        for relation_id, raw, cand, cand_obj in edges:
            dst = present.get(cand) if cand else None
            if dst is not None:
                self.catalogue.resolve_relation(relation_id, dst, commit=False)
                stats.resolved += 1
                if self.grow_aliases and raw and cand_obj and cand_obj.method != "alias":
                    folded = fold(raw)
                    if folded != dst.lower() and not self.catalogue.get_alias(folded):
                        self.catalogue.put_alias(folded, dst, source="resolver", commit=False)
                        stats.aliases_added += 1
            else:
                stats.still_pending += 1
        self.catalogue.commit()
        log.info(stats.summary())
        return stats
