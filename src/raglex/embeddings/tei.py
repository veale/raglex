"""Open-weight model serving over an OpenAI-compatible endpoint (design §1b).

The production plan for embeddings is: open weights (Qwen3-Embedding, BGE-M3),
bulk-embedded ONCE offline (the HPC shard pipeline in ``offline.py``), then served
for query traffic by any OpenAI-compatible inference server — HuggingFace
**text-embeddings-inference** (TEI), vLLM, llama.cpp, LM Studio — typically as one
more container on the same host as the API.

The one invariant that matters: **vectors only compare within a family**, and the
family must be the same whether a vector was computed on a GPU cluster or by the
serving container. So this provider's family label is the canonical
``("hf", <model id>, <revision>, <dims>)`` — named after the *weights*, not the
transport — and the offline export stamps the identical family in its manifest.
Swap TEI for vLLM tomorrow and the index still matches.

Instruction-following models (Qwen3-Embedding) want a task prefix on the QUERY
side only; that prefix is part of the retrieval behaviour, so it's config here
(``RAGLEX_EMBED_INSTRUCTION``) with a legal-retrieval default for models that
expect one, and is recorded in the offline manifest for reproducibility.
"""

from __future__ import annotations

import logging
import os

from .provider import _FamilyMixin

log = logging.getLogger("raglex.embeddings.tei")

DEFAULT_URL = "http://raglex-tei:8080"

# The legal task instruction (design §1b). Applied to queries only, and only for
# models that are instruction-tuned (auto-detected, or forced via env).
LEGAL_INSTRUCTION = (
    "Given a legal research question, retrieve the statutory provisions, case-law "
    "passages, and regulatory guidance that answer it"
)
_INSTRUCTION_MODELS = ("qwen3-embedding", "e5-mistral", "gte-qwen", "instruct")


def wants_instruction(model: str) -> bool:
    m = (model or "").lower()
    return any(tag in m for tag in _INSTRUCTION_MODELS)


def query_prefix(model: str, instruction: str | None = None) -> str:
    """The prefix prepended to query text for instruction-tuned embedders —
    '' for models (BGE-M3, e5 doc side) that don't use one."""
    if instruction is None:
        instruction = os.environ.get("RAGLEX_EMBED_INSTRUCTION")
    if instruction is None and wants_instruction(model):
        instruction = LEGAL_INSTRUCTION
    if not instruction:
        return ""
    return f"Instruct: {instruction}\nQuery: "


class TEIEmbeddingProvider(_FamilyMixin):
    """Embeddings from any OpenAI-compatible server (POST /v1/embeddings).

    Family = ("hf", model, revision, dims): canonical for open weights, shared
    with the offline HPC pipeline, independent of the serving stack.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        model: str = "Qwen/Qwen3-Embedding-0.6B",
        model_version: str | None = None,
        dimensions: int = 1024,
        max_input_tokens: int = 8192,
        batch_size: int = 32,
        instruction: str | None = None,
    ) -> None:
        self.name = "hf"
        self.model = model
        self.model_version = (model_version
                              or os.environ.get("RAGLEX_EMBED_MODEL_VERSION") or "1")
        self.dimensions = dimensions
        self.max_input_tokens = max_input_tokens
        self.batch_size = batch_size
        self.url = (url or os.environ.get("RAGLEX_TEI_URL") or DEFAULT_URL).rstrip("/")
        self._prefix = query_prefix(model, instruction)

    def embed(self, texts: list[str], *, input_type: str | None = None) -> list[list[float]]:
        import httpx

        prefix = self._prefix if input_type == "query" else ""
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = [prefix + t for t in texts[i: i + self.batch_size]]
            resp = httpx.post(
                f"{self.url}/v1/embeddings",
                json={"model": self.model, "input": batch},
                timeout=120,
            )
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            vectors = [d["embedding"] for d in data]
            if len(vectors) != len(batch):
                raise RuntimeError(f"server returned {len(vectors)} vectors for {len(batch)} inputs")
            if vectors and len(vectors[0]) != self.dimensions:
                raise RuntimeError(
                    f"server returned {len(vectors[0])}-dim vectors but this family is "
                    f"{self.dimensions}-dim — fix RAGLEX_EMBED_DIMENSIONS (a change starts a NEW family)")
            out.extend(vectors)
        return out

    def health(self) -> bool:
        import httpx

        for path in ("/health", "/v1/models"):
            try:
                if httpx.get(self.url + path, timeout=5).status_code < 500:
                    return True
            except Exception:  # noqa: BLE001 — probe the next path
                continue
        log.warning("embedding server unreachable at %s", self.url)
        return False


class TEIReranker:
    """Cross-encoder reranking via TEI's native /rerank endpoint (bge-reranker-v2-m3
    et al.). Degrades to the fused RRF order when unreachable — a reranker improves
    ordering, so its absence must cost quality, never availability."""

    def __init__(self, *, url: str | None = None, top_n: int = 50) -> None:
        self.url = (url or os.environ.get("RAGLEX_RERANK_URL")
                    or os.environ.get("RAGLEX_TEI_URL") or DEFAULT_URL).rstrip("/")
        self.top_n = top_n

    def rerank(self, query: str, candidates: list) -> list:
        import httpx

        if not candidates:
            return candidates
        head, tail = candidates[: self.top_n], candidates[self.top_n:]
        try:
            resp = httpx.post(
                f"{self.url}/rerank",
                json={"query": query, "texts": [c.chunk_text for c in head]},
                timeout=60,
            )
            resp.raise_for_status()
            rows = resp.json()
            scores = [0.0] * len(head)
            for r in rows:
                scores[r["index"]] = r["score"]
        except Exception as exc:  # noqa: BLE001
            log.warning("rerank unavailable (%s); keeping RRF order", exc)
            return candidates
        from .remote import _rescored

        ranked = sorted(zip(head, scores), key=lambda p: p[1], reverse=True)
        return [_rescored(c, s) for c, s in ranked] + tail
