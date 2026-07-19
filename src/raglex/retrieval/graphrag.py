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
    # network authority of the neighbour (PageRank roll-up) — what ranked it in
    authority: float = 0.0


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
    pool: int = 200,
) -> Expansion:
    """1-hop typed-neighbour expansion around one document (resolved edges only,
    so every neighbour is a real node). Optionally restrict by relationship type.

    Ranked, not first-come (design §3c): up to ``pool`` candidate edges per
    direction are gathered through the *bounded* neighbour queries (safe on a
    100k-citation node, where the old unbounded scan wasn't), then the ``limit``
    slots go to the neighbours with the highest network authority (PageRank
    roll-up) — so a provision's landmark interpreting case beats its fortieth
    trivial mentioner. With an empty roll-up every authority is 0 and the order
    degrades to the old arrival order."""
    exp = Expansion(doc_id=doc_id)
    rt_filter = set(relationship_types) if relationship_types else None

    half = max(1, pool // 2)
    rows: list[tuple] = []  # (neighbour_id, direction, row)
    for row in catalogue.neighbours_out(doc_id, limit=half):
        if rt_filter and row["relationship_type"] not in rt_filter:
            continue
        rows.append((row["dst_id"], "out", row))
    for row in catalogue.neighbours_in(doc_id, limit=half):
        if rt_filter and row["relationship_type"] not in rt_filter:
            continue
        rows.append((row["src_id"], "in", row))

    auth = catalogue.authority_for([nid for nid, _d, _r in rows])

    def _rank(item) -> float:
        arow = auth.get(item[0])
        return arow["pagerank"] if arow else 0.0

    seen: set[tuple[str, str]] = set()  # (neighbour, relationship) — dedupe repeat edges
    for nid, direction, row in sorted(rows, key=_rank, reverse=True):
        key = (nid, row["relationship_type"])
        if key in seen:
            continue
        seen.add(key)
        nb = catalogue.get_document(nid)
        exp.neighbours.append(
            Neighbour(
                dst_id=nid,
                relationship_type=row["relationship_type"],
                direction=direction,
                title=nb["title"] if nb else None,
                court=nb["court"] if nb else None,
                src_anchor=_col(row, "src_anchor"),
                dst_anchor=_col(row, "dst_anchor"),
                extracted_via=_col(row, "extracted_via"),
                authority=_rank((nid, direction, row)),
            )
        )
        if len(exp.neighbours) >= limit:
            break
    return exp
