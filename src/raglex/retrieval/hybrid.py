"""Hybrid retrieval (§6c) — FTS + vector, fused by Reciprocal Rank Fusion.

Pure vector search fumbles exact tokens (an ECLI, "Article 17 GDPR", an acronym);
pure FTS misses paraphrase and cross-lingual matches. Since both indexes live in
one store, run both for every query and fuse with **RRF**: each result's score is
the sum over rankers of ``1/(k + rank)``. RRF needs no score normalisation (it
uses ranks), is a few lines, and reliably beats either ranker alone.

The cross-encoder reranker (§6c) is the precision stage that consumes the fused
candidates; here it's a swappable interface with an identity default so the
pipeline runs without the extra model, and a real BGE/Cohere reranker drops in.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..embeddings.provider import EmbeddingProvider
from ..storage.catalogue import Catalogue

RRF_K = 60  # the standard RRF constant


@dataclass(slots=True)
class Candidate:
    doc_id: str
    chunk_id: int
    score: float
    chunk_text: str = ""
    structural_unit: str | None = None
    char_start: int | None = None
    char_end: int | None = None


def _cosine(a: list[float], b: list[float]) -> float:
    # vectors are L2-normalised on write, but stay general here
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def vector_search(
    catalogue: Catalogue,
    provider: EmbeddingProvider,
    query: str,
    *,
    limit: int = 100,
    filters: dict | None = None,
) -> list[Candidate]:
    """Semantic half: delegates to the catalogue's family-scoped vector search
    (pgvector on Postgres, in-process cosine on SQLite — same row shape)."""
    qvec = provider.embed([query], input_type="query")[0]
    rows = catalogue.vector_search(
        qvec, provider.name, provider.model, provider.model_version,
        dimensions=provider.dimensions, limit=limit, filters=filters,
    )
    return [
        Candidate(
            doc_id=r["doc_id"], chunk_id=r["chunk_id"], score=r["score"],
            chunk_text=r["chunk_text"], structural_unit=r["structural_unit"],
            char_start=r["char_start"], char_end=r["char_end"],
        )
        for r in rows
    ]


def fts_search(
    catalogue: Catalogue,
    provider: EmbeddingProvider,
    query: str,
    *,
    limit: int = 100,
    filters: dict | None = None,
) -> list[Candidate]:
    """Lexical half: FTS5 bm25 (lower rank = better; surfaced as a Candidate list)."""
    hits = catalogue.fts_chunks(
        query, provider.name, provider.model, provider.model_version,
        limit=limit, filters=filters,
    )
    return [Candidate(doc_id=d, chunk_id=c, score=-rank) for d, c, rank in hits]


def rrf_fuse(*ranked_lists: list[Candidate], k: int = RRF_K, limit: int = 50) -> list[Candidate]:
    """Reciprocal Rank Fusion over any number of ranked candidate lists (§6c)."""
    fused: dict[tuple[str, int], float] = {}
    keep: dict[tuple[str, int], Candidate] = {}
    for ranked in ranked_lists:
        for rank, cand in enumerate(ranked):
            key = (cand.doc_id, cand.chunk_id)
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank + 1)
            # keep whichever Candidate has the richer text payload
            if key not in keep or (not keep[key].chunk_text and cand.chunk_text):
                keep[key] = cand
    out: list[Candidate] = []
    for key, score in sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]:
        cand = keep[key]
        out.append(
            Candidate(
                doc_id=cand.doc_id, chunk_id=cand.chunk_id, score=score,
                chunk_text=cand.chunk_text, structural_unit=cand.structural_unit,
                char_start=cand.char_start, char_end=cand.char_end,
            )
        )
    return out


@runtime_checkable
class Reranker(Protocol):
    def rerank(self, query: str, candidates: list[Candidate]) -> list[Candidate]:
        """Re-score fused candidates to a precision ordering (§6c)."""
        ...


class IdentityReranker:
    """No-op default — keeps RRF order. A BGE/Cohere cross-encoder drops in here
    (§6c) without touching the rest of the pipeline."""

    def rerank(self, query: str, candidates: list[Candidate]) -> list[Candidate]:
        return candidates
