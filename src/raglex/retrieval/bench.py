"""Known-item retrieval benchmark (design §5) — the corpus supervises itself.

Every resolved citation is a free evaluation query: take the *sentence
neighbourhood* where a document cites an authority, mask the citation string
itself, and ask the search engine to find the cited document. Tens of thousands
of such queries exist with zero annotation cost, and while the supervision is
weak, its biases are stable across the systems being compared — which is all an
A/B between embedding families needs.

``raglex bench`` runs a sample through the CONFIGURED provider/family and reports
recall@k and MRR (overall + per entity kind). Run it once per candidate family
(swap RAGLEX_EMBED_* between runs); the JSON reports land in ``data/bench/`` so
promotion to production default is a comparison of two files, not a vibe.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from ..storage.catalogue import Catalogue
from ..storage.textstore import TextStore
from .search import SearchEngine

CONTEXT_CHARS = 320  # the query window either side of the masked citation


def _candidate_rows(catalogue: Catalogue, sample: int) -> list[dict]:
    """Random resolved citations with char spans — the raw pool of eval items."""
    rows = catalogue.conn.execute(
        """
        SELECT src_id, raw, candidate_id, entity_kind, char_start, char_end
        FROM citations
        WHERE candidate_id IS NOT NULL AND char_start IS NOT NULL
        ORDER BY RANDOM() LIMIT ?
        """,
        (sample,),
    ).fetchall()
    return [dict(r) for r in rows]


def run_bench(
    catalogue: Catalogue,
    textstore: TextStore | None,
    provider,
    *,
    queries: int = 200,
    k: int = 10,
    seed: int = 7,
) -> dict:
    random.seed(seed)
    engine = SearchEngine(catalogue, provider)
    family = f"{provider.name}/{provider.model}@{provider.model_version}:{provider.dimensions}"

    pool = _candidate_rows(catalogue, sample=queries * 10)
    random.shuffle(pool)

    hits_at_k = 0
    mrr_sum = 0.0
    ran = 0
    by_kind: dict[str, dict] = {}
    text_cache: dict[str, str | None] = {}

    def _text(doc_id: str) -> str | None:
        if doc_id not in text_cache:
            row = catalogue.get_document(doc_id)
            t = None
            if row and row["payload_hash"] and textstore:
                try:
                    t = textstore.get(row["payload_hash"])
                except OSError:
                    t = None
            text_cache[doc_id] = t
        return text_cache[doc_id]

    for row in pool:
        if ran >= queries:
            break
        target = catalogue.find_document_id(row["candidate_id"])
        if not target:
            continue
        tdoc = catalogue.get_document(target)
        if not tdoc or not tdoc["has_embedding"]:
            continue  # can't be retrieved → not a fair query for any system
        text = _text(row["src_id"])
        cs, ce = row["char_start"], row["char_end"]
        if not text or cs is None or ce is None or ce > len(text):
            continue
        # the citing sentence-neighbourhood, with the citation string masked out —
        # otherwise lexical search trivially wins on the citation token itself
        query = (text[max(0, cs - CONTEXT_CHARS):cs] + " … " +
                 text[ce:ce + CONTEXT_CHARS]).strip()
        if len(query.split()) < 12:
            continue  # too little context to be a meaningful query

        result = engine.search(query, k=k, expand_graph=False)
        rank = None
        seen: list[str] = []
        for h in result:
            if h.doc_id in seen:
                continue
            seen.append(h.doc_id)
            if h.doc_id == target or (tdoc["ecli"] and h.doc_id == tdoc["ecli"]):
                rank = len(seen)
                break
        ran += 1
        kind = (row["entity_kind"] or "unknown").lower()
        bucket = by_kind.setdefault(kind, {"n": 0, "hits": 0, "mrr": 0.0})
        bucket["n"] += 1
        if rank is not None:
            hits_at_k += 1
            mrr_sum += 1.0 / rank
            bucket["hits"] += 1
            bucket["mrr"] += 1.0 / rank

    for b in by_kind.values():
        n = b["n"] or 1
        b["recall_at_k"] = round(b.pop("hits") / n, 4)
        b["mrr"] = round(b["mrr"] / n, 4)

    return {
        "family": family,
        "k": k,
        "queries": ran,
        "recall_at_k": round(hits_at_k / ran, 4) if ran else None,
        "mrr": round(mrr_sum / ran, 4) if ran else None,
        "by_entity_kind": by_kind,
        "seed": seed,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
