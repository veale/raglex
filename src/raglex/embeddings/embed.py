"""The embedding stage (§6) — a separate, re-runnable projection.

Reads a ``pending_embedding`` queue, chunks each document on its structural units
(§6b), embeds the chunks through the configured provider (§6d), and writes vectors
+ the FTS chunk index. Kept independent of harvest so a model swap re-embeds from
the stored text projection without re-harvesting (§1.2). Idempotent: a doc with no
vectors in the chosen family is (re)embedded; others are skipped.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..storage.catalogue import Catalogue
from ..storage.textstore import TextStore
from .chunking import ChunkConfig, chunk_document
from .provider import EmbeddingProvider

log = logging.getLogger("raglex.embeddings")


@dataclass(slots=True)
class EmbedStats:
    provider: str
    model: str
    documents: int = 0
    chunks: int = 0
    skipped_no_text: int = 0

    def summary(self) -> str:
        return (
            f"[embed] provider={self.provider} model={self.model} "
            f"documents={self.documents} chunks={self.chunks}"
        )


def _doc_meta(row) -> dict:
    """Contextual-header metadata for the chunker (§6b.4)."""
    year = (row["decision_date"] or "")[:4] or None
    tags = []
    try:
        tags = json.loads(row["topic_tags"] or "[]")
    except (json.JSONDecodeError, TypeError):
        tags = []
    return {
        "source": row["source"],
        "jurisdiction": row["source"],
        "court": row["court"],
        "year": year,
        "tags": tags,
    }


class EmbedStage:
    def __init__(
        self,
        catalogue: Catalogue,
        provider: EmbeddingProvider,
        *,
        textstore: TextStore | None = None,
        config: ChunkConfig | None = None,
        batch_size: int = 64,
    ) -> None:
        self.catalogue = catalogue
        self.provider = provider
        # If given, segments persisted by the pipeline are loaded for structure-
        # aware chunking (§6b); without it the chunker derives units from text.
        self.textstore = textstore
        self.config = config or ChunkConfig(max_tokens=min(512, provider.max_input_tokens))
        self.batch_size = batch_size

    def run(self, *, limit: int | None = None) -> EmbedStats:
        p = self.provider
        stats = EmbedStats(provider=p.name, model=p.model)
        pending = self.catalogue.pending_embedding(p.name, p.model, p.model_version)
        if limit is not None:
            pending = pending[:limit]
        for row in pending:
            text = self._load_text(row["text_path"])
            if not text:
                stats.skipped_no_text += 1
                continue
            segments = (
                self.textstore.get_segments(row["payload_hash"])
                if self.textstore and row["payload_hash"]
                else []
            )
            chunks = chunk_document(
                row["stable_id"], text, segments=segments, meta=_doc_meta(row), config=self.config
            )
            if not chunks:
                stats.skipped_no_text += 1
                continue
            # fresh family for this doc (re-derivable projection, §1.2)
            self.catalogue.clear_embeddings(row["stable_id"], p.name, p.model, p.model_version)
            self._embed_and_store(row, chunks, stats)
            self.catalogue.mark_embedded(row["stable_id"])
            stats.documents += 1
        self.catalogue.conn.commit()
        log.info(stats.summary())
        return stats

    def _embed_and_store(self, row, chunks, stats: EmbedStats) -> None:
        p = self.provider
        for i in range(0, len(chunks), self.batch_size):
            batch = chunks[i : i + self.batch_size]
            vectors = p.embed([c.embed_input for c in batch], input_type="document")
            for chunk, vector in zip(batch, vectors):
                self.catalogue.add_chunk(
                    chunk.doc_id,
                    chunk.chunk_id,
                    vector,
                    chunk.text,
                    provider=p.name,
                    model=p.model,
                    model_version=p.model_version,
                    dimensions=p.dimensions,
                    structural_unit=chunk.structural_unit,
                    source_language=row["source_language"],
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                )
                stats.chunks += 1

    @staticmethod
    def _load_text(text_path) -> str | None:
        if not text_path:
            return None
        try:
            return Path(text_path).read_text(encoding="utf-8")
        except OSError:
            return None
