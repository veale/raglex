"""Citation-network statistics — PageRank authority over the resolved graph.

The design principle (mcp-search-retrieval-design.md §3): the graph carries the
part of legal relevance a vector can't, and the cheapest way to let it inform
ranking is a batch-computed authority prior fused into RRF as one more ranked
list. Everything here is dependency-free pure Python sized for ~1M nodes / ~10M
deduped edges as a scheduled job (tens of seconds), not per-request work.

Deliberately NOT treatment-weighted: the treatment classifier's output isn't
reliable yet, so every resolved, non-inferred edge counts the same. Two runs:

- ``pagerank``          — the classic landmark signal;
- ``pagerank_decayed``  — each edge discounted by the *citing* document's age
  (exponential half-life), so recently-cited authorities surface even when an
  old giant dominates the raw ranking.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DAMPING = 0.85
ITERATIONS = 30
HALF_LIFE_YEARS = 10.0
# an edge whose citing document has no date is treated as ~15 years old
UNKNOWN_AGE_YEARS = 15.0


@dataclass(slots=True)
class Graph:
    """Compact int-indexed digraph: ids interned once, edges as index pairs."""

    ids: list[str] = field(default_factory=list)
    index: dict[str, int] = field(default_factory=dict)
    src: list[int] = field(default_factory=list)
    dst: list[int] = field(default_factory=list)
    weight: list[float] = field(default_factory=list)  # decay weight per edge

    def intern(self, doc_id: str) -> int:
        i = self.index.get(doc_id)
        if i is None:
            i = len(self.ids)
            self.index[doc_id] = i
            self.ids.append(doc_id)
        return i

    def add_edge(self, src_id: str, dst_id: str, weight: float = 1.0) -> None:
        self.src.append(self.intern(src_id))
        self.dst.append(self.intern(dst_id))
        self.weight.append(weight)


def decay_weight(src_year: int | None, *, now_year: int,
                 half_life: float = HALF_LIFE_YEARS) -> float:
    """Exponential age discount for an edge, by the citing document's year."""
    age = (now_year - src_year) if src_year else UNKNOWN_AGE_YEARS
    if age < 0:
        age = 0.0
    return 0.5 ** (age / half_life)


def pagerank(
    g: Graph,
    *,
    damping: float = DAMPING,
    iterations: int = ITERATIONS,
    weighted: bool = False,
) -> list[float]:
    """Power-iteration PageRank; dangling mass redistributed uniformly.

    ``weighted=True`` is the *age-decayed* variant. Crucially the per-edge decay
    weight is NOT renormalised within each source node (all of a document's
    out-edges share its one age, so per-node normalisation would cancel the
    decay entirely): each edge passes ``rank/out_count × weight``, the decayed
    remainder leaks out of circulation, and the vector is renormalised to sum 1
    each iteration — so an old citing document genuinely confers less authority
    than a recent one, which is the whole point."""
    n = len(g.ids)
    if n == 0:
        return []
    out_count = [0.0] * n
    for s in g.src:
        out_count[s] += 1.0

    rank = [1.0 / n] * n
    base_src, base_dst, base_w = g.src, g.dst, g.weight
    for _ in range(iterations):
        nxt = [0.0] * n
        dangling = 0.0
        for i in range(n):
            if out_count[i] == 0.0:
                dangling += rank[i]
        if weighted:
            for e in range(len(base_src)):
                s = base_src[e]
                nxt[base_dst[e]] += rank[s] * base_w[e] / out_count[s]
        else:
            for e in range(len(base_src)):
                s = base_src[e]
                nxt[base_dst[e]] += rank[s] / out_count[s]
        teleport = (1.0 - damping) / n + damping * dangling / n
        for i in range(n):
            nxt[i] = teleport + damping * nxt[i]
        if weighted:  # renormalise the leaked (decayed) mass
            total = sum(nxt)
            if total > 0:
                inv = 1.0 / total
                for i in range(n):
                    nxt[i] *= inv
        rank = nxt
    return rank


def compute_authority(
    edges,
    years: dict[str, int],
    *,
    now_year: int,
    damping: float = DAMPING,
    iterations: int = ITERATIONS,
    half_life: float = HALF_LIFE_YEARS,
) -> list[tuple[str, float, float, float, int, int]]:
    """The full roll-up from an iterable of ``(src_id, dst_id)`` edge pairs
    (pre-deduped by the caller's SQL) and a citing-document year map.

    Returns rows ``(doc_id, pagerank, pagerank_decayed, percentile, in_degree,
    out_degree)`` for every node that appears in the graph. ``percentile`` is
    the 0–100 rank of raw pagerank **among cited documents** (in_degree > 0) —
    a doc that only ever cites others has no meaningful authority percentile
    and gets NULL (None)."""
    g = Graph()
    for src_id, dst_id in edges:
        g.add_edge(src_id, dst_id, decay_weight(years.get(src_id), now_year=now_year,
                                                half_life=half_life))
    n = len(g.ids)
    if n == 0:
        return []
    in_deg = [0] * n
    out_deg = [0] * n
    for e in range(len(g.src)):
        out_deg[g.src[e]] += 1
        in_deg[g.dst[e]] += 1

    pr = pagerank(g, damping=damping, iterations=iterations, weighted=False)
    prd = pagerank(g, damping=damping, iterations=iterations, weighted=True)

    # percentile of raw pagerank among cited docs only
    cited = sorted((pr[i] for i in range(n) if in_deg[i] > 0))
    m = len(cited)

    def _pct(score: float) -> float:
        # rank position via binary search (bisect by hand to stay import-light)
        lo, hi = 0, m
        while lo < hi:
            mid = (lo + hi) // 2
            if cited[mid] <= score:
                lo = mid + 1
            else:
                hi = mid
        return 100.0 * lo / m if m else 0.0

    out: list[tuple[str, float, float, float, int, int]] = []
    for i in range(n):
        pct = _pct(pr[i]) if in_deg[i] > 0 else None
        out.append((g.ids[i], pr[i], prd[i], pct, in_deg[i], out_deg[i]))
    return out
