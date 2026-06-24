"""GraphRAG expansion (§6c) — the graph feeds the LLM, not just the UI.

When a retrieved chunk is about to enter an LLM's context, walk the relations
graph **1 hop** from its parent document and pull the neighbours' summaries too.
The typed edges (§1.3a) make this *selective*: for "is this still good law?" pull
``overrules``/``distinguishes`` neighbours; for "what's the reasoning" pull
``applies``/``considers``. This is exactly why edges are typed and why the graph
and vectors share a store — the relationship type tells the retriever *which*
neighbours matter for *this* question.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..storage.catalogue import Catalogue


def _col(row, name: str):
    """Tolerant column read — a row may predate a column (sqlite3.Row has no .get)."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


@dataclass(slots=True)
class Neighbour:
    dst_id: str
    relationship_type: str
    direction: str  # 'out' (this doc cites it) | 'in' (it cites this doc)
    title: str | None = None
    court: str | None = None
    # Richer edge data (§1.3a/§1.9): the pinpoint anchors and how the edge's
    # treatment was inferred — so the UI can show "analyses → Article 17" and
    # distinguish a regex/LLM-classified treatment from an adapter-supplied one.
    src_anchor: str | None = None
    dst_anchor: str | None = None
    extracted_via: str | None = None


@dataclass(slots=True)
class Expansion:
    doc_id: str
    neighbours: list[Neighbour] = field(default_factory=list)


def expand(
    catalogue: Catalogue,
    doc_id: str,
    *,
    relationship_types: list[str] | None = None,
    limit: int = 10,
) -> Expansion:
    """1-hop typed-neighbour expansion around one document (resolved edges only,
    so every neighbour is a real node). Optionally restrict by relationship type."""
    exp = Expansion(doc_id=doc_id)
    rt_filter = set(relationship_types) if relationship_types else None

    # outgoing: this document's authorities / what it engages
    for row in catalogue.relations_for(doc_id):
        if row["resolution_status"] != "resolved" or not row["dst_id"]:
            continue
        if rt_filter and row["relationship_type"] not in rt_filter:
            continue
        nb = catalogue.get_document(row["dst_id"])
        exp.neighbours.append(
            Neighbour(
                dst_id=row["dst_id"],
                relationship_type=row["relationship_type"],
                direction="out",
                title=nb["title"] if nb else None,
                court=nb["court"] if nb else None,
                src_anchor=_col(row, "src_anchor"),
                dst_anchor=_col(row, "dst_anchor"),
                extracted_via=_col(row, "extracted_via"),
            )
        )
        if len(exp.neighbours) >= limit:
            return exp

    # incoming: what cites/treats this document (citing cases, commentary)
    for row in catalogue.relations_to(doc_id):
        if rt_filter and row["relationship_type"] not in rt_filter:
            continue
        src = catalogue.get_document(row["src_id"])
        exp.neighbours.append(
            Neighbour(
                dst_id=row["src_id"],
                relationship_type=row["relationship_type"],
                direction="in",
                title=src["title"] if src else None,
                court=src["court"] if src else None,
                src_anchor=_col(row, "src_anchor"),
                dst_anchor=_col(row, "dst_anchor"),
                extracted_via=_col(row, "extracted_via"),
            )
        )
        if len(exp.neighbours) >= limit:
            break
    return exp
