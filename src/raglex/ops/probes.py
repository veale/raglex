"""Corpus-integrity probes (§8) — invariant checks over the citation network.

The C-604/22 incident (2026-07): the CJEU's own citation form — "…, C-597/19,
EU:C:2021:492, paragraph 107)" — had its trailing paragraph *also* consumed by
the carry-forward heuristic and pinned to the last-named directive, minting a
phantom legislation edge per case citation. The extractor bug was one line; the
lesson is structural: **every inference pass needs a standing probe that counts
its failure modes on the live corpus**, because a systematic extraction error
looks exactly like data until something downstream (here: the whole EU pinpoint
network) is visibly wrong.

Each probe is a SQL invariant with a count and a small sample of violating rows,
so a run reads as a health report and a regression suite over the *data* (the
code's tests can't see what a year of harvesting accumulated). Wire-in points:
``raglex probes`` (CLI), ``facade.run_probes`` (UI/MCP).

Probes marked ``repair=`` have a targeted, deletion-bounded fixer — always run
the probe first, read the samples, then repair; never repair blind.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("raglex.ops.probes")

SAMPLE = 5


@dataclass(slots=True)
class ProbeResult:
    name: str
    description: str
    severity: str          # 'critical' | 'warn' | 'info'
    count: int
    samples: list[dict] = field(default_factory=list)
    repairable: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "severity": self.severity, "count": self.count,
                "samples": self.samples, "repairable": self.repairable}


def _rows(cat, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in cat.conn.execute(sql, params).fetchall()]


def _one(cat, sql: str, params=()) -> int:
    return cat.conn.execute(sql, params).fetchone()["n"]


# --------------------------------------------------------------------------
# The probes. Each returns a ProbeResult; SQL must run on BOTH backends
# (no ILIKE, no backend-specific casts).
# --------------------------------------------------------------------------

# a paragraph carry-forward sitting ≤10 chars after a case citation is that
# judgment's pinpoint wrongly re-attributed to legislation (the C-604/22 bug).
# NB LIKE patterns are bound as parameters, never inlined — a literal % in SQL
# text trips psycopg's placeholder scan (see PgConnShim.execute).
_CASE_PARA_CF = """
FROM citations c2
JOIN citations c1 ON c1.src_id = c2.src_id
  AND c1.entity_kind IN ('case', 'opinion')
  AND c2.char_start - c1.char_end BETWEEN 0 AND 10
WHERE c2.method = 'carry_forward' AND LOWER(c2.raw) LIKE ?
"""
_PARA_PAT = ("para%",)


def probe_case_paragraph_carry_forward(cat) -> ProbeResult:
    n = _one(cat, f"SELECT COUNT(*) AS n {_CASE_PARA_CF}", _PARA_PAT)
    samples = _rows(cat, f"SELECT c2.src_id, c2.raw, c2.candidate_id, c2.pinpoint "
                         f"{_CASE_PARA_CF} LIMIT {SAMPLE}", _PARA_PAT)
    return ProbeResult(
        "case_paragraph_carry_forward",
        "carry-forward 'paragraph N' immediately after a case citation — the "
        "judgment's own pinpoint mis-attributed to the last-named legislation",
        "critical", n, samples, repairable=True)


def probe_para_pinpoint_on_eu_instrument(cat) -> ProbeResult:
    # EU instruments are cited by Article/Recital; a bare 'para N' pinpoint on a
    # directive/eu_instrument edge is almost always a mis-carried case pinpoint
    sql = ("FROM citations WHERE entity_kind IN ('directive', 'eu_instrument') "
           "AND pinpoint LIKE ? AND method = 'carry_forward'")
    n = _one(cat, f"SELECT COUNT(*) AS n {sql}", ("para %",))
    samples = _rows(cat, f"SELECT src_id, raw, candidate_id, pinpoint {sql} LIMIT {SAMPLE}",
                    ("para %",))
    return ProbeResult(
        "para_pinpoint_on_eu_instrument",
        "carried-forward 'para N' pinpoints on EU instruments (Articles/Recitals "
        "are how those are actually cited) — residue of mis-carried case pinpoints",
        "warn", n, samples, repairable=True)


def probe_self_citation(cat) -> ProbeResult:
    # Two very different populations share the src==dst shape (the live run
    # proved it): STRUCTURED self-edges are an instrument's own internal
    # cross-references (an SI citing its schedule paragraphs by URI) — adapter
    # data, not an error, but excluded from ranking/counting; the rest are
    # extraction noise (a judgment citing its own header citation).
    breakdown = _rows(cat, "SELECT extracted_via, COUNT(*) AS n FROM relations "
                           "WHERE src_id = dst_id GROUP BY extracted_via")
    noise = sum(b["n"] for b in breakdown if b["extracted_via"] != "structured")
    samples = _rows(cat, "SELECT src_id, relationship_type, extracted_via, "
                         "raw_citation_string FROM relations WHERE src_id = dst_id "
                         f"AND extracted_via <> 'structured' LIMIT {SAMPLE}")
    return ProbeResult(
        "self_citation",
        "NON-structured self-edges (extraction noise). Structured self-edges are "
        "internal cross-references and are reported but not repairable: "
        f"breakdown={breakdown}",
        "warn", noise, samples, repairable=True)


def probe_year_pinpoint(cat) -> ProbeResult:
    # 'para 2016' etc. — a year that leaked through the pinpoint guards.
    # SQL LIKE '_' also matches letters ('para 193C' met the old pattern), so
    # the precise test is a Python regex over a broad SQL candidate set.
    import re as _re

    cands = _rows(cat, "SELECT src_id, raw, candidate_id, pinpoint FROM citations "
                       "WHERE pinpoint IS NOT NULL AND LENGTH(pinpoint) = 9 "
                       "AND pinpoint LIKE ? LIMIT 5000", ("para %",))
    hits = [c for c in cands if _re.fullmatch(r"para (?:19|20)\d\d", c["pinpoint"])]
    return ProbeResult(
        "year_pinpoint",
        "pinpoints that are exactly year-shaped (para 19xx/20xx) — likely "
        "citation-year leakage (NB a real para 2016 exists in mega-judgments; "
        "eyeball before acting)",
        "warn", len(hits), hits[:SAMPLE])


def probe_kind_mismatch(cat) -> ProbeResult:
    # a citation extracted as CASE that resolves to a legislation document (or
    # statute-kind → judgment): the grammar and the resolver disagree about what
    # the target is — one of them is wrong
    sql = """
    FROM citations c JOIN documents d ON d.stable_id = c.candidate_id
    WHERE (c.entity_kind IN ('case', 'opinion') AND d.doc_type = 'legislation')
       OR (c.entity_kind IN ('act', 'directive', 'regulation', 'eu_instrument', 'treaty')
           AND d.doc_type IN ('judgment', 'decision'))
    """
    n = _one(cat, f"SELECT COUNT(*) AS n {sql}")
    samples = _rows(cat, f"SELECT c.src_id, c.raw, c.entity_kind, c.candidate_id, "
                         f"d.doc_type {sql} LIMIT {SAMPLE}")
    return ProbeResult(
        "kind_mismatch",
        "citations whose extracted kind (case vs statute) contradicts the "
        "resolved target's doc_type — grammar or resolution is wrong",
        "warn", n, samples)


def probe_resolved_dst_missing(cat) -> ProbeResult:
    sql = ("FROM relations r WHERE r.resolution_status = 'resolved' "
           "AND r.dst_id IS NOT NULL "
           "AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.stable_id = r.dst_id)")
    n = _one(cat, f"SELECT COUNT(*) AS n {sql}")
    samples = _rows(cat, f"SELECT r.src_id, r.dst_id, r.relationship_type {sql} LIMIT {SAMPLE}")
    return ProbeResult(
        "resolved_dst_missing",
        "edges marked resolved whose target document does not exist — a broken "
        "invariant (resolution must only ever point at real nodes)",
        "critical", n, samples)


def probe_pending_but_held(cat) -> ProbeResult:
    sql = ("FROM relations r JOIN documents d ON d.stable_id = r.candidate_id "
           "WHERE r.resolution_status = 'pending'")
    n = _one(cat, f"SELECT COUNT(*) AS n {sql}")
    samples = _rows(cat, f"SELECT r.src_id, r.candidate_id {sql} LIMIT {SAMPLE}")
    return ProbeResult(
        "pending_but_held",
        "hanging edges whose candidate is already a held document — resolver "
        "lag; a resolve pass should clear these",
        "info", n, samples)


def probe_alias_dangling(cat) -> ProbeResult:
    sql = ("FROM citation_aliases a WHERE NOT EXISTS "
           "(SELECT 1 FROM documents d WHERE d.stable_id = a.dst_id OR d.ecli = a.dst_id) "
           "AND NOT EXISTS (SELECT 1 FROM citation_aliases a2 WHERE a2.alias = a.dst_id)")
    n = _one(cat, f"SELECT COUNT(*) AS n {sql}")
    samples = _rows(cat, f"SELECT a.alias, a.dst_id, a.source {sql} LIMIT {SAMPLE}")
    return ProbeResult(
        "alias_dangling",
        "aliases pointing at neither a document nor another alias — dead rungs "
        "in the resolution ladder (harmless until something relies on one)",
        "info", n, samples)


def probe_never_extracted(cat) -> ProbeResult:
    # the live audit found judgments with full text and ZERO citation rows —
    # extraction never ran (an import path that skipped it). By source, because
    # a hot source = a whole import batch missed; the rescan job is the fix.
    rows = _rows(cat, """
        SELECT d.source, COUNT(*) AS n FROM documents d
        WHERE d.has_text = 1 AND d.doc_type IN ('judgment', 'decision')
          AND d.last_extracted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM citations c WHERE c.src_id = d.stable_id)
        GROUP BY d.source ORDER BY n DESC
        """)
    total = sum(r["n"] for r in rows)
    return ProbeResult(
        "never_extracted",
        "judgments with text but no citation extraction ever run — invisible to "
        "the whole graph until a rescan covers them (jobs → Rescan stale)",
        "warn", total, rows[:SAMPLE])


def probe_duplicate_spans(cat) -> ProbeResult:
    sql = ("FROM (SELECT src_id, char_start, char_end, COUNT(*) AS c FROM citations "
           "WHERE char_start IS NOT NULL GROUP BY src_id, char_start, char_end "
           "HAVING COUNT(*) > 1) t")
    n = _one(cat, f"SELECT COUNT(*) AS n {sql}")
    samples = _rows(cat, f"SELECT t.src_id, t.char_start, t.char_end, t.c {sql} LIMIT {SAMPLE}")
    return ProbeResult(
        "duplicate_spans",
        "multiple citation rows on the identical char span of one document — "
        "double extraction (each inflates counts once)",
        "info", n, samples)


PROBES = (
    probe_case_paragraph_carry_forward,
    probe_para_pinpoint_on_eu_instrument,
    probe_self_citation,
    probe_year_pinpoint,
    probe_kind_mismatch,
    probe_resolved_dst_missing,
    probe_pending_but_held,
    probe_alias_dangling,
    probe_never_extracted,
    probe_duplicate_spans,
)


def run_probes(cat, *, only: list[str] | None = None) -> list[ProbeResult]:
    out: list[ProbeResult] = []
    for probe in PROBES:
        name = probe.__name__.removeprefix("probe_")
        if only and name not in only:
            continue
        try:
            out.append(probe(cat))
        except Exception as exc:  # noqa: BLE001 — one broken probe mustn't hide the rest
            out.append(ProbeResult(name, f"probe failed: {exc}", "critical", -1))
    return out


# --------------------------------------------------------------------------
# Repairs — targeted, deletion-bounded, and matched 1:1 to a probe.
# --------------------------------------------------------------------------

def repair_case_paragraph_carry_forward(cat) -> dict:
    """Remove the phantom legislation edges the C-604/22 bug minted: the
    poisoned ``carry_forward`` citation rows (para-cue, ≤10 chars after a case
    citation) and the ``inferred`` relations built from them. Bounded strictly
    to rows matching the probe's own join — nothing else is touched. Re-runnable.

    After repair, run rebuild-citation-counts (the roll-up still carries the
    phantom occurrences until rebuilt)."""
    poisoned = _rows(cat, f"SELECT c2.citation_id, c2.src_id, c2.candidate_id, c2.pinpoint "
                          f"{_CASE_PARA_CF}", _PARA_PAT)
    deleted_edges = 0
    with cat._atomic():
        for i in range(0, len(poisoned), 500):
            batch = poisoned[i:i + 500]
            qs = ",".join("?" * len(batch))
            cat.conn.execute(
                f"DELETE FROM citations WHERE citation_id IN ({qs})",
                [p["citation_id"] for p in batch])
        # drop the inferred edges those citations minted: match on the exact
        # (src, host-candidate, pinpoint-anchor) triple, inferred provenance only
        seen: set[tuple] = set()
        for p in poisoned:
            key = (p["src_id"], p["candidate_id"], p["pinpoint"])
            if key in seen or not p["candidate_id"]:
                continue
            seen.add(key)
            cur = cat.conn.execute(
                "DELETE FROM relations WHERE src_id = ? AND extracted_via = 'inferred' "
                "AND dst_anchor = ? AND (candidate_id = ? OR dst_id = ?)",
                (p["src_id"], p["pinpoint"], p["candidate_id"], p["candidate_id"]))
            deleted_edges += cur.rowcount
    return {"citations_deleted": len(poisoned), "inferred_edges_deleted": deleted_edges}


def repair_self_citation(cat) -> dict:
    """Delete NON-structured self-edges only (extraction noise — a document
    citing its own header citation). Structured self-edges are adapter-supplied
    internal cross-references (an SI's own schedule paragraphs) and are kept —
    the live 2026-07 probe run showed 429k of them; deleting those would have
    destroyed real data. Ranking/counting exclude src==dst separately."""
    cur = cat.conn.execute(
        "DELETE FROM relations WHERE src_id = dst_id AND extracted_via <> 'structured'")
    cat.conn.commit()
    return {"self_edges_deleted": cur.rowcount}


REPAIRS = {
    "case_paragraph_carry_forward": repair_case_paragraph_carry_forward,
    # the para-on-EU-instrument probe is the same disease seen from the other
    # side; the carry-forward repair clears the adjacent cases, and what remains
    # deserves eyes before deletion — so no blind repair for it.
    "self_citation": repair_self_citation,
}


def run_repair(cat, name: str) -> dict:
    if name not in REPAIRS:
        raise KeyError(f"no repair for {name!r}; repairable: {sorted(REPAIRS)}")
    out = REPAIRS[name](cat)
    log.info("repair %s: %s", name, out)
    return out
