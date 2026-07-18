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
from ..core.text import fold
from .matchers import Candidate, first_candidate

log = logging.getLogger("raglex.resolve")


def string_hash(raw: str) -> str:
    """Stable key for the pending queue — folded so trivial variants coalesce."""
    return sha256_bytes(fold(raw).encode("utf-8"))


@dataclass(slots=True)
class ResolveStats:
    resolved: int = 0
    still_pending: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"[resolve] resolved={self.resolved} still_pending={self.still_pending}"


class Resolver:
    """Resolution is now set-based over the persisted ``candidate_id``/``raw_fold``.

    The resolver used to cache every resolved citation string back into
    ``citation_aliases`` so the next identical string took the cheap alias rung. That
    lookup is now an indexed column on the edge itself, so the cache bought nothing and
    cost a million rows. The alias table keeps only the *semantic* mappings — CELEX→ECLI,
    chamber-less slugs, user shorthand — which are rules, not memoisation.
    """

    def __init__(self, catalogue: Catalogue) -> None:
        self.catalogue = catalogue

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
        re-running is how citations to newly-harvested targets become live edges.

        Three set-based UPDATEs off the persisted ``candidate_id``/``raw_fold`` and their
        partial indexes — the candidate ladder already ran, once, at extraction time."""
        stats = ResolveStats()
        stats.resolved = self.catalogue.resolve_pending()
        stats.still_pending = self.catalogue.count_pending_relations()
        log.info(stats.summary())
        return stats

    def run_for(self, stable_id: str, ecli: str | None = None) -> int:
        """Resolve only the edges that point at one just-harvested document. Nothing else
        can have become resolvable, so the whole-graph pass is wasted work on ingest."""
        return self.catalogue.resolve_pending_for(stable_id, ecli)
