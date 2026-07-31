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
    # How many edges of this type join the two documents, and their pinpoint pairs.
    # One row per (neighbour, relationship) is the right shape for a graph view, but it
    # used to hide the arity: four per-provision ``supersedes`` edges to the same act
    # appeared as one, so a caller verifying its own work read a false negative on
    # three of the four it had just written.
    passages: int = 1
    anchor_pairs: list[tuple[str | None, str | None]] = field(default_factory=list)


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
    # The type restriction goes to SQL, not to a post-filter: these queries are bounded
    # and unordered, so filtering afterwards means "keep the matches among any hundred
    # edges" — which made a rare relationship invisible from the heavily-cited side.
    rt_filter = [t for t in (relationship_types or []) if t] or None

    half = max(1, pool // 2)
    rows: list[tuple] = []  # (neighbour_id, direction, row)
    for row in catalogue.neighbours_out(doc_id, limit=half, relationship_types=rt_filter):
        rows.append((row["dst_id"], "out", row))
    for row in catalogue.neighbours_in(doc_id, limit=half, relationship_types=rt_filter):
        rows.append((row["src_id"], "in", row))

    auth = catalogue.authority_for([nid for nid, _d, _r in rows])

    def _rank(item) -> float:
        arow = auth.get(item[0])
        return arow["pagerank"] if arow else 0.0

    # One row per (neighbour, relationship, direction), but every edge folded into it is
    # counted and its pinpoint pair kept — so the arity of a per-provision mapping is
    # visible rather than silently collapsed to one.
    by_key: dict[tuple[str, str, str], Neighbour] = {}
    for nid, direction, row in sorted(rows, key=_rank, reverse=True):
        key = (nid, row["relationship_type"], direction)
        pair = (_col(row, "src_anchor"), _col(row, "dst_anchor"))
        existing = by_key.get(key)
        if existing is not None:
            existing.passages += 1
            if pair not in existing.anchor_pairs and len(existing.anchor_pairs) < 50:
                existing.anchor_pairs.append(pair)
            continue
        if len(by_key) >= limit:
            continue
        nb = catalogue.get_document(nid)
        by_key[key] = Neighbour(
            dst_id=nid,
            relationship_type=row["relationship_type"],
            direction=direction,
            title=nb["title"] if nb else None,
            court=nb["court"] if nb else None,
            src_anchor=pair[0],
            dst_anchor=pair[1],
            extracted_via=_col(row, "extracted_via"),
            authority=_rank((nid, direction, row)),
            anchor_pairs=[pair],
        )
    exp.neighbours.extend(by_key.values())
    return exp
