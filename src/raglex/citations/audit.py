"""Grammar audit (§5) — finding what extraction MISSES, not just what it mangles.

Over-inclusion leaves evidence in the database, so `ops/probes.py` can hunt it
with SQL invariants. A *miss* leaves nothing — you cannot query what was never
extracted. Two instruments close that gap:

1. **Unconsumed-cue scanning** (`scan_unconsumed`): grep the raw text for
   citation-shaped residue — cue patterns that almost always sit inside a real
   citation (`ECLI:`, `[1998] 2 WLR`, `C-311/18`, `No. 12345/04`, ` v `,
   `§ 35`…) — and report every match that no extracted-citation span covers.
   One uncovered cue is an anecdote; the same cue uncovered across a whole
   source/court is a systematic grammar gap ("this reporter format never
   matched"). ``raglex audit-misses`` samples the corpus and aggregates.

2. **Structured-vs-text cross-validation** (`audit_structured_recall`): sources
   that hand us typed relations (CELLAR, Rechtspraak) are ground truth for what
   a document cites; a structured edge whose target never appears in the text
   extraction is a measured recall failure with a known answer.

Neither *proves* a miss — an uncovered " v " may be prose, a structured edge may
come from metadata the text never states — so both report samples-in-context
for a human (or an LLM pass) to adjudicate, and their value is in the *trend*:
run after every grammar change; the counts should only go down.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# Cue patterns that are strong citation signals when they appear in legal text.
# Deliberately high-precision shapes (a bare " v " is too noisy alone — it needs
# capitalised parties around it). Each hit NOT covered by an extracted span is a
# candidate miss.
CUES: dict[str, re.Pattern] = {
    "ecli": re.compile(r"\bECLI:[A-Z]{2}:[A-Z0-9]+:\d{4}:[A-Z0-9.]+"),
    "eu_case_no": re.compile(r"\b[CTF]-\d{1,4}/\d{2}\b"),
    "old_eu_case": re.compile(r"\bCase\s+\d{1,4}/\d{2}\b"),
    "neutral_cite": re.compile(r"\[(?:19|20)\d{2}\]\s+[A-Z]{2,7}(?:\s+[A-Za-z]{2,8})?\s+\d{1,5}\b"),
    "report_cite": re.compile(
        r"\[(?:18|19|20)\d{2}\]\s+\d{0,2}\s?(?:AC|WLR|All\s?ER|QB|KB|Ch|Fam|CMLR|ECR|EHRR|Cr\s?App\s?R|Lloyd's\s?Rep)\b"),
    "us_style": re.compile(r"\b\d{1,4}\s+(?:U\.?S\.?|F\.\s?(?:2d|3d|Supp)|S\.\s?Ct\.?)\s+\d{1,5}\b"),
    "echr_appno": re.compile(r"\b[Nn]os?\.\s*\d{3,5}/\d{2}\b"),
    "section_of": re.compile(
        r"\b(?:[Ss]ection|[Aa]rticle|[Rr]egulation)\s+\d+[A-Z]?"
        r"(?:\(\d+\))?\s+of\s+(?:the\s+)?[A-Z][A-Za-z]"),
    "party_v_party": re.compile(
        r"\b[A-Z][A-Za-z&.'()-]{2,40}\s+v\.?\s+[A-Z][A-Za-z&.'()-]{2,40}"),
    "ecr_report": re.compile(r"\[(?:19|20)\d{2}\]\s+ECR\s+(?:I|II)?[-–]?\d+"),
    "celex_ref": re.compile(r"\b[123567]\d{4}[LRDE]\d{4}\b"),
}


@dataclass(slots=True)
class UnconsumedCue:
    cue: str
    text: str
    char_start: int
    context: str


@dataclass(slots=True)
class DocAudit:
    doc_id: str
    covered: int = 0           # cue hits inside an extracted-citation span
    unconsumed: list[UnconsumedCue] = field(default_factory=list)

    @property
    def miss_rate(self) -> float:
        total = self.covered + len(self.unconsumed)
        return len(self.unconsumed) / total if total else 0.0


# Cues that legitimately sit BESIDE the citation they belong to rather than
# inside it: a case name precedes its citation ("Smith v Jones [1998] 2 WLR 448"
# — the grammar consumes the report cite, never the name). For these, an
# extracted span *starting shortly after* the cue counts as coverage.
_LOOKAHEAD_CUES = {"party_v_party": 120, "old_eu_case": 30}


def scan_unconsumed(doc_id: str, text: str, spans: list[tuple[int, int]],
                    *, pad: int = 2, context: int = 70) -> DocAudit:
    """Every cue hit in ``text`` not covered (± ``pad`` chars) by an extracted
    span. ``spans`` = the (char_start, char_end) of the document's extracted
    citations — from the DB or a fresh extraction run."""
    audit = DocAudit(doc_id=doc_id)
    spans = sorted(spans)

    def _covered(s: int, e: int, lookahead: int = 0) -> bool:
        for a, b in spans:
            if a - pad <= s and e <= b + pad:
                return True
            # an overlapping-not-containing hit still counts as engaged
            if a - pad <= s <= b + pad or a - pad <= e <= b + pad:
                return True
            # name-then-cite: a span opening within the lookahead window
            if lookahead and e <= a <= e + lookahead:
                return True
        return False

    for name, pat in CUES.items():
        la = _LOOKAHEAD_CUES.get(name, 0)
        for m in pat.finditer(text):
            if _covered(m.start(), m.end(), la):
                audit.covered += 1
            else:
                lo = max(0, m.start() - context)
                audit.unconsumed.append(UnconsumedCue(
                    cue=name, text=m.group(0), char_start=m.start(),
                    context=text[lo:m.end() + context].replace("\n", " ")))
    return audit


def audit_sample(catalogue, textstore, *, sample: int = 200, doc_type: str | None = None,
                 source: str | None = None, seed: int = 7) -> dict:
    """Sample documents, scan each for unconsumed cues against its STORED
    citations, and aggregate — the standing recall health-check. Aggregation is
    by (cue, source): one uncovered hit is noise, a hot cell is a grammar gap."""
    import random

    random.seed(seed)
    clauses = ["has_text = 1", "text_path IS NOT NULL"]
    params: list = []
    if doc_type:
        clauses.append("doc_type = ?")
        params.append(doc_type)
    if source:
        clauses.append("source = ?")
        params.append(source)
    rows = catalogue.conn.execute(
        f"SELECT stable_id, source, payload_hash FROM documents WHERE {' AND '.join(clauses)}",
        params).fetchall()
    rows = random.sample(list(rows), min(sample, len(rows)))

    by_cell: Counter = Counter()
    covered_by_cell: Counter = Counter()
    worst: list[tuple[float, str, list[UnconsumedCue]]] = []
    never_extracted: Counter = Counter()   # has text, ZERO citation rows — the
    scanned = 0                            # pipeline never ran, not a grammar gap
    for r in rows:
        try:
            text = textstore.get(r["payload_hash"])
        except OSError:
            continue
        spans = [(c["char_start"], c["char_end"]) for c in catalogue.citations_for(r["stable_id"])
                 if c["char_start"] is not None]
        if not spans:
            never_extracted[r["source"]] += 1
            continue  # a grammar can't miss on a doc extraction never touched
        audit = scan_unconsumed(r["stable_id"], text, spans)
        scanned += 1
        for u in audit.unconsumed:
            by_cell[(u.cue, r["source"])] += 1
        covered_by_cell.update({(None, r["source"]): audit.covered})
        if audit.unconsumed:
            worst.append((audit.miss_rate, r["stable_id"], audit.unconsumed[:3]))

    worst.sort(reverse=True)
    return {
        "scanned": scanned,
        "never_extracted": dict(never_extracted.most_common()),
        "hot_cells": [
            {"cue": cue, "source": src, "unconsumed": n}
            for (cue, src), n in by_cell.most_common(20)
        ],
        "worst_documents": [
            {"doc_id": d, "miss_rate": round(rate, 3),
             "examples": [{"cue": u.cue, "text": u.text, "context": u.context} for u in us]}
            for rate, d, us in worst[:10]
        ],
        "total_unconsumed": sum(by_cell.values()),
        "total_covered": sum(covered_by_cell.values()),
    }


def audit_structured_recall(catalogue, *, source: str, sample: int = 300, seed: int = 7) -> dict:
    """Recall against ground truth: for documents from a source that supplies
    typed relations (CELLAR, Rechtspraak), how many structured targets does the
    TEXT extraction also find? A structured edge with no text-extracted
    counterpart is a measured miss (or metadata the text never states — the
    samples let a human tell which)."""
    import random

    random.seed(seed)
    docs = [r["stable_id"] for r in catalogue.conn.execute(
        "SELECT DISTINCT src_id AS stable_id FROM relations "
        "WHERE extracted_via = 'structured' AND dst_id IS NOT NULL "
        "AND src_id IN (SELECT stable_id FROM documents WHERE source = ? AND has_text = 1)",
        (source,)).fetchall()]
    docs = random.sample(docs, min(sample, len(docs)))
    total = found = 0
    missed_samples: list[dict] = []
    for sid in docs:
        structured = {r["dst_id"] for r in catalogue.conn.execute(
            "SELECT DISTINCT dst_id FROM relations WHERE src_id = ? "
            "AND extracted_via = 'structured' AND dst_id IS NOT NULL AND dst_id <> src_id",
            (sid,)).fetchall()}
        if not structured:
            continue
        text_found = {c["candidate_id"] for c in catalogue.citations_for(sid)
                      if c["candidate_id"]}
        # a text candidate may be an alias of the structured id — count via resolution
        for dst in structured:
            total += 1
            if dst in text_found or catalogue.get_document(dst) and (
                    (catalogue.get_document(dst)["ecli"] or "") in text_found):
                found += 1
            elif len(missed_samples) < 10:
                missed_samples.append({"src": sid, "structured_dst": dst})
    return {
        "source": source, "documents": len(docs),
        "structured_edges": total, "also_found_in_text": found,
        "text_recall": round(found / total, 3) if total else None,
        "missed_samples": missed_samples,
    }
