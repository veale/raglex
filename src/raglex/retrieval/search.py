"""Search orchestration (§6c) — the full query flow.

  1. partition pre-filter (jurisdiction / year / doc_type / topic);
  2. hybrid FTS + vector retrieval, fused by RRF → top-N candidates;
  3. cross-encoder rerank the candidates → precision top-k;
  4. for each top chunk, 1-hop graph expansion pulling typed-neighbour summaries;
  5. assemble (chunks + neighbours + metadata) for the LLM / UI, with citations
     back to exact char spans and ECLIs.

The reranker is swappable (identity default, §6c); the embedding provider is the
configured §6d provider. This module is what the ops/research UI and an LLM
answerer both call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..embeddings.provider import EmbeddingProvider
from ..storage.catalogue import Catalogue
from .graphrag import Expansion, expand
from .hybrid import (
    Candidate,
    IdentityReranker,
    Reranker,
    fts_search,
    rrf_fuse,
    vector_search,
)


@dataclass(slots=True)
class SearchHit:
    doc_id: str
    chunk_id: int
    score: float
    chunk_text: str
    structural_unit: str | None
    char_start: int | None
    char_end: int | None
    # document-level metadata for display/citation
    ecli: str | None = None
    title: str | None = None
    court: str | None = None
    source: str | None = None
    neighbours: Expansion | None = None


@dataclass(slots=True)
class SearchEngine:
    catalogue: Catalogue
    provider: EmbeddingProvider
    reranker: Reranker = field(default_factory=IdentityReranker)

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        candidate_pool: int = 100,
        filters: dict | None = None,
        expand_graph: bool = True,
        relationship_types: list[str] | None = None,
    ) -> list[SearchHit]:
        # 2) hybrid retrieval over the (pre-filtered) slice, fused by RRF
        vec = vector_search(
            self.catalogue, self.provider, query, limit=candidate_pool, filters=filters
        )
        lex = fts_search(
            self.catalogue, self.provider, query, limit=candidate_pool, filters=filters
        )
        fused = rrf_fuse(vec, lex, limit=candidate_pool)

        # 3) rerank → precision top-k
        top = self.reranker.rerank(query, fused)[:k]

        # 4/5) assemble with doc metadata + optional 1-hop graph expansion
        hits: list[SearchHit] = []
        for cand in top:
            hits.append(self._assemble(cand, expand_graph, relationship_types))
        return hits

    def _assemble(
        self, cand: Candidate, expand_graph: bool, relationship_types: list[str] | None
    ) -> SearchHit:
        doc = self.catalogue.get_document(cand.doc_id)
        neighbours = None
        if expand_graph:
            neighbours = expand(
                self.catalogue, cand.doc_id, relationship_types=relationship_types
            )
        return SearchHit(
            doc_id=cand.doc_id,
            chunk_id=cand.chunk_id,
            score=cand.score,
            chunk_text=cand.chunk_text,
            structural_unit=cand.structural_unit,
            char_start=cand.char_start,
            char_end=cand.char_end,
            ecli=doc["ecli"] if doc else None,
            title=doc["title"] if doc else None,
            court=doc["court"] if doc else None,
            source=doc["source"] if doc else None,
            neighbours=neighbours,
        )
