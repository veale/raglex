"""Pluggable embedding providers (§6d).

The embedding *model* must not be hardwired: you A/B legal-domain vs multilingual
models (§6a), swap when better models ship, and route some work through an
aggregator. So embedding generation sits behind a small provider interface, and
concrete providers are config, not code (Appendix B `embedding_providers`).

Two providers ship here:
- ``HashingEmbeddingProvider`` — deterministic, dependency-free, offline. A
  hashed bag-of-words vector: shared terms → similar vectors. It is a *real*
  vector good enough to exercise chunking, hybrid fusion, and GraphRAG without an
  API key or model download. It is **not** semantically strong (no synonyms /
  cross-lingual) — production swaps in a legal-domain or multilingual model.
- ``OpenRouterEmbeddingProvider`` — the real, OpenAI-shaped OpenRouter embeddings
  API (§6d), used when ``OPENROUTER_API_KEY`` is set.

Adding a provider = a class + a config row; everything downstream reads a
``pending_embedding`` queue and writes vectors, so a swap re-embeds from raw with
no schema change. Vectors are ONLY comparable within one (provider, model,
dimensions) family — recorded on every row (§6d).
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol, runtime_checkable

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str  # 'local-hashing', 'openrouter', 'voyage', ...
    model: str  # provider-specific model slug
    model_version: str
    dimensions: int  # vector width this (provider, model) emits
    max_input_tokens: int  # per-input cap; longer chunks are pre-split (§6b)

    def embed(self, texts: list[str], *, input_type: str | None = None) -> list[list[float]]:
        """Batch in, vectors out (one per input)."""
        ...

    def health(self) -> bool:
        """Cheap reachability/credential check — gates the provider (§6d)."""
        ...

    @property
    def family(self) -> tuple[str, str, str, int]:
        """The comparability key: vectors only compare within one family (§6d)."""
        ...


class _FamilyMixin:
    name: str
    model: str
    model_version: str
    dimensions: int

    @property
    def family(self) -> tuple[str, str, str, int]:
        return (self.name, self.model, self.model_version, self.dimensions)


def _l2_normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


class HashingEmbeddingProvider(_FamilyMixin):
    """Deterministic hashed bag-of-words embedding (zero-dep default)."""

    def __init__(self, dimensions: int = 256) -> None:
        self.name = "local-hashing"
        self.model = "hashing-bow"
        self.model_version = "v1"
        self.dimensions = dimensions
        self.max_input_tokens = 8192

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for token in _TOKEN_RE.findall(text.lower()):
            h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "big") % self.dimensions
            sign = 1.0 if h[4] & 1 else -1.0  # signed hashing reduces collisions
            vec[idx] += sign
        return _l2_normalise(vec)

    def embed(self, texts: list[str], *, input_type: str | None = None) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def health(self) -> bool:
        return True


class OpenRouterEmbeddingProvider(_FamilyMixin):
    """OpenRouter embeddings (§6d) — OpenAI-shaped, one key spans many models.

    POST https://openrouter.ai/api/v1/embeddings  body {model, input}. Key from
    the env (never config rows). Lazily imports httpx so the default path has no
    network dependency.
    """

    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        *,
        model: str = "openai/text-embedding-3-small",
        dimensions: int = 1536,
        model_version: str = "1",
        max_input_tokens: int = 8191,
        api_key_env: str = "OPENROUTER_API_KEY",
    ) -> None:
        self.name = "openrouter"
        self.model = model
        self.model_version = model_version
        self.dimensions = dimensions
        self.max_input_tokens = max_input_tokens
        self._api_key_env = api_key_env

    def _key(self) -> str | None:
        return os.environ.get(self._api_key_env)

    def health(self) -> bool:
        return bool(self._key())

    def embed(self, texts: list[str], *, input_type: str | None = None) -> list[list[float]]:
        import httpx

        key = self._key()
        if not key:
            raise RuntimeError(f"{self._api_key_env} not set")
        resp = httpx.post(
            f"{self.BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": self.model, "input": texts},
            timeout=60,
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


def _mcp_provider(**kwargs):
    """The ML sidecar (§6d). Imported lazily so the default path never pays for it."""
    from .remote import MCPEmbeddingProvider

    dims = os.environ.get("RAGLEX_EMBED_DIMENSIONS")
    if dims and "dimensions" not in kwargs:
        kwargs["dimensions"] = int(dims)
    return MCPEmbeddingProvider(**kwargs)


# Provider registry — config, not code (Appendix B embedding_providers).
_PROVIDERS = {
    "local-hashing": HashingEmbeddingProvider,
    "openrouter": OpenRouterEmbeddingProvider,
    "mcp": _mcp_provider,
}


def get_provider(name: str = "local-hashing", **kwargs) -> EmbeddingProvider:
    try:
        cls = _PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS))
        raise KeyError(f"unknown embedding provider {name!r}; known: {known}") from None
    return cls(**kwargs)


def get_reranker(name: str | None = None):
    """The §6c precision stage. ``mcp`` routes to the sidecar's cross-encoder; anything
    else (or nothing configured) keeps the fused RRF order."""
    name = name or os.environ.get("RAGLEX_RERANKER") or "identity"
    if name == "mcp":
        from .remote import MCPReranker

        return MCPReranker()
    from ..retrieval.hybrid import IdentityReranker

    return IdentityReranker()
