"""Explicit admin case removal and publisher refresh; ordinary harvest stays append-only."""
def preview(facade, citation: str) -> dict:
    from .facade import _TARGETED_HARVEST
    from .citations.snowball import _classify

    written = citation.strip()
    held, candidate = facade._resolve_held_id(written)
    with facade._open() as (cat, _, _ts):
        if held is None:
            from .core.text import fold_citation
            held = cat.find_document_id(fold_citation(written))
        row = cat.get_document(held) if held else None
        if row is not None and row["doc_type"] not in {"judgment", "decision", "opinion"}:
            return {"error": "This citation matches a document that is not a case."}
        ident = held or candidate
        if not ident:
            return {"error": "Case citation not recognised; use its full neutral citation or stable ID."}
        _form, _country, adapter = _classify(ident, "case")
        if row is None and adapter in {"uk-legislation", "eu-legislation", "eu-preparatory", "fr-legislation"}:
            return {"error": "This citation does not identify a case."}
        return {"citation": written, "stable_id": ident, "held": row is not None,
                "title": row["title"] if row else None,
                "source": row["source"] if row else adapter,
                "can_refetch": adapter in _TARGETED_HARVEST,
                "can_purge": row is not None,
                "upstream_status": row["upstream_status"] if row else None}


def purge(catalogue, stable_id: str) -> dict:
    """Remove the case's catalogue state, preserving other sources' citation evidence.

    No denylist is created: a later publisher republication may be harvested normally.
    Content-addressed filesystem objects are retained because other versions/documents
    may share them; no reader can address them through the removed catalogue entry.
    """
    cat = catalogue
    with cat._atomic():
        row = cat.get_document(stable_id)
        if row is None:
            return {"stable_id": stable_id, "purged": False, "reason": "not held"}
        # Incoming references are facts in other documents. Keep them as hanging
        # references rather than erasing what another court actually cited.
        detached = cat.conn.execute(
            "UPDATE relations SET dst_id=NULL, resolution_status='pending' WHERE dst_id=?",
            (stable_id,)).rowcount
        counts = {}
        for table, column in (
            ("relations", "src_id"), ("citations", "src_id"),
            ("citation_aliases", "dst_id"), ("learned_shorthands", "candidate_id"),
            ("learned_shorthand_docs", "doc_id"), ("learned_shorthand_docs", "candidate_id"),
            ("document_versions", "stable_id"), ("document_assets", "doc_id"),
            ("document_tags", "doc_id"), ("refinement_flags", "doc_id"),
            ("doc_fts", "doc_id"), ("doc_headings", "doc_id"),
            ("embeddings", "doc_id"), ("doc_authority", "doc_id"),
            ("effects_refresh", "stable_id"),
        ):
            n = cat.conn.execute(f"DELETE FROM {table} WHERE {column}=?", (stable_id,)).rowcount
            counts[table] = counts.get(table, 0) + n
        if cat.backend == "sqlite":
            cat.conn.execute("DELETE FROM chunks_fts WHERE doc_id=?", (stable_id,))
        cat.conn.execute("DELETE FROM provision_mappings WHERE current_doc_id=? OR previous_doc_id=?",
                         (stable_id, stable_id))
        cat.conn.execute("DELETE FROM match_suggestions WHERE suggested_id=?", (stable_id,))
        cat.conn.execute("DELETE FROM documents WHERE stable_id=?", (stable_id,))
    return {"stable_id": stable_id, "purged": True, "deleted": counts,
            "incoming_references_preserved": detached, "future_harvest_allowed": True}


def run_action(facade, *, citation: str, expected_id: str, action: str,
               on_progress=None, cancel_check=None) -> dict:
    from .facade import _TARGETED_HARVEST
    from .citations.snowball import _classify

    if action not in {"purge", "refetch"}:
        raise ValueError("Case action must be purge or refetch")
    match = preview(facade, citation)
    if match.get("error"):
        raise ValueError(match["error"])
    if match["stable_id"] != expected_id:
        raise ValueError("Citation now resolves to a different case; review the match again")
    if cancel_check and cancel_check():
        return {"cancelled": True}
    if on_progress:
        on_progress(stage="purging case" if action == "purge" else "fetching publisher case",
                    done=0, total=1, item=expected_id)
    if action == "purge":
        with facade._open() as (cat, _, _ts):
            result = purge(cat, expected_id)
    else:
        _form, _country, adapter_key = _classify(expected_id, "case")
        builder = _TARGETED_HARVEST.get(adapter_key)
        if builder is None:
            raise ValueError("This source does not support fetching one case by citation")
        adapter = builder(expected_id)
        if adapter is None:
            raise ValueError("The publisher could not identify this case")
        stubs = iter(adapter.discover(None, max_pages=1))
        stub = next(stubs, None)
        record = adapter.fetch(stub) if stub is not None else None
        if record is None or not (record.text or "").strip():
            raise ValueError("The publisher returned no readable case; the held copy was retained")
        if match["held"] and record.stable_id != expected_id:
            raise ValueError("The publisher returned a different case identity; the held copy was retained")
        if cancel_check and cancel_check():
            return {"cancelled": True}
        if on_progress:
            on_progress(stage="storing refreshed case", done=0, total=1, item=record.stable_id)
        from .pipeline import Pipeline
        from .pipeline.runner import RunStats
        from .resolve import Resolver
        record.ensure_payload_hash()
        with facade._open() as (cat, rs, ts):
            pipe = Pipeline(cat, rs, textstore=ts)
            pipe._ingest(record, RunStats(source=record.source), refresh_projection=True)
            if cat.get_document(record.stable_id) is None:
                raise ValueError("The fetched case could not be stored under its publisher identity")
            # A parser fix may change the projection while raw bytes stay identical.
            # An explicit refetch refreshes that projection as well as the metadata.
            ts.put(record.payload_hash, record.text, source=record.source)
            ts.put_segments(record.payload_hash, record.segments)
            facade._extract_ids(cat, ts, [record.stable_id])
            Resolver(cat).run_for_documents([record.stable_id])
            facade._index_freetext_ids_open(cat, ts, [record.stable_id])
        result = {"stable_id": record.stable_id, "refetched": True,
                  "chars": len(record.text)}
    facade._invalidate_caches()
    if on_progress:
        on_progress(stage="case action complete", done=1, total=1, item=expected_id)
    return result
