"""Embeddings (§6): pluggable providers (§6d), structural-unit chunking (§6b),
and the re-runnable embed stage (§6)."""

from .chunking import Chunk, ChunkConfig, chunk_document
from .embed import EmbedStage, EmbedStats
from .provider import (
    EmbeddingProvider,
    HashingEmbeddingProvider,
    OpenRouterEmbeddingProvider,
    get_provider,
)

__all__ = [
    "Chunk",
    "ChunkConfig",
    "chunk_document",
    "EmbedStage",
    "EmbedStats",
    "EmbeddingProvider",
    "HashingEmbeddingProvider",
    "OpenRouterEmbeddingProvider",
    "get_provider",
]
