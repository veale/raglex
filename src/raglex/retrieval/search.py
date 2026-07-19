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
    doc_type: str | None = None
    decision_date: str | None = None
    neighbours: Expansion | None = None
    # why-ranked: 1-based rank in each contributing signal (None = didn't appear),
    # plus the authority percentile — the UI's "why this result" chips and the MCP
    # explain field both read this.
    signals: dict | None = None


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
        # 2b) the authority prior (design §3a): re-rank the candidate union by the
        # PageRank roll-up and fuse it as one more ranked list. RRF needs no score
        # normalisation, so the graph signal joins the text signals in one line.
        # Docs without an authority row contribute nothing (they're simply absent
        # from the third list) — a cold roll-up degrades to plain hybrid search.
        auth_scores = self.catalogue.authority_for(
            [c.doc_id for c in vec] + [c.doc_id for c in lex])
        auth = _authority_ranked(vec, lex, auth_scores)
        fused = rrf_fuse(vec, lex, auth, limit=candidate_pool)

        # 3) rerank → precision top-k
        top = self.reranker.rerank(query, fused)[:k]

        # why-ranked: 1-based rank of each (doc, chunk) in each contributing list
        vec_rank = {(c.doc_id, c.chunk_id): i + 1 for i, c in enumerate(vec)}
        lex_rank = {(c.doc_id, c.chunk_id): i + 1 for i, c in enumerate(lex)}
        auth_rank = {c.doc_id: i + 1 for i, c in enumerate(auth)}

        # 4/5) assemble with doc metadata + optional 1-hop graph expansion
        hits: list[SearchHit] = []
        for cand in top:
            hit = self._assemble(cand, expand_graph, relationship_types)
            key = (cand.doc_id, cand.chunk_id)
            arow = auth_scores.get(cand.doc_id)
            hit.signals = {
                "semantic_rank": vec_rank.get(key),
                "lexical_rank": lex_rank.get(key),
                "authority_rank": auth_rank.get(cand.doc_id),
                "authority_percentile": arow.get("percentile") if arow else None,
            }
            hits.append(hit)
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
            doc_type=doc["doc_type"] if doc else None,
            decision_date=str(doc["decision_date"]) if doc and doc["decision_date"] else None,
            neighbours=neighbours,
        )


def _authority_ranked(
    vec: list[Candidate], lex: list[Candidate], auth_scores: dict[str, dict]
) -> list[Candidate]:
    """The candidate union ordered by document PageRank (one entry per doc — its
    best-placed chunk), docs without authority omitted. This is the third RRF
    list: a pure prior, so it must never *introduce* candidates, only re-weight
    ones the text signals already found."""
    best: dict[str, Candidate] = {}
    for cand in list(vec) + list(lex):
        if cand.doc_id in auth_scores and cand.doc_id not in best:
            best[cand.doc_id] = cand
    return sorted(
        best.values(),
        key=lambda c: auth_scores[c.doc_id].get("pagerank") or 0.0,
        reverse=True,
    )
