"""Citation-extraction stage (§5) — text → hanging typed edges.

Runs the grammar extractor over a document's stored text and writes one *hanging*
edge per citation: ``relationship_type=mentions``, ``dst_id`` = the grammar's
candidate (resolvable form), ``dst_anchor`` = the pinpoint (article/section),
``extracted_via='regex'``, ``resolution_status='pending'``. The §5b resolver then
links each candidate to a node when it's harvested — so a judgment that cites
"Article 17 GDPR" gets a pinpoint edge to ``32016R0679`` the moment the GDPR is in
the corpus, and meanwhile sits in the harvest worklist.

Idempotent: clears this source's prior ``regex`` edges before re-extracting,
leaving structured (adapter) and manual edges untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from ..core.models import ExtractedVia, RelationshipType, ResolutionStatus, TypedRelation
from ..storage.catalogue import Catalogue
from ..storage.textstore import TextStore
from .extractor import CitationExtractor, extract_citations


@dataclass(slots=True)
class ExtractStats:
    documents: int = 0
    citations: int = 0

    def summary(self) -> str:
        return f"[cite-extract] documents={self.documents} citations={self.citations}"


# A CJEU case is identified by an EU ECLI (C = Court of Justice, T = General Court,
# F = Civil Service Tribunal) or the CELLAR source.
def _is_cjeu(doc) -> bool:
    ecli = (doc["ecli"] or "")
    return ecli.startswith(("ECLI:EU:C", "ECLI:EU:T", "ECLI:EU:F")) or doc["source"] == "eu-cellar"


# UK-referral signals on a preliminary_reference edge: the country marker the CELLAR
# adapter embeds, or a UK-specific referring court. Tuned for *recall* — a missed UK
# court would wrongly suppress a genuine UK-statute link, whereas a false positive only
# reverts to the un-guarded behaviour.
_UK_REFERRAL_RE = re.compile(
    r"country:\s*(?:the\s+)?united\s+kingdom"
    r"|\bunited\s+kingdom\b"
    r"|\b(?:england|wales|scotland|northern\s+ireland)\b"
    r"|\bupper\s+tribunal\b|first-tier\s+tribunal"
    r"|court\s+of\s+session|inner\s+house|outer\s+house"
    r"|employment\s+appeal\s+tribunal|special\s+immigration\s+appeals",
    re.IGNORECASE,
)


# the name-based UK-statute grammars gated by the CJEU guard (NOT the explicit
# legislation.gov.uk URI grammar — an explicit URL is unambiguous, not a heuristic).
_UK_NAME_HEURISTICS = {"uk_statute_named", "uk_act_section"}


_UK_COUNTRY_RE = re.compile(r"united\s+kingdom|\bgreat\s+britain\b|\bGB\b|\bUK\b", re.IGNORECASE)


def _uk_referred_preliminary(catalogue: Catalogue, stable_id: str) -> bool:
    """Was this CJEU case a preliminary ruling referred by a UK court? Prefer the
    authoritative ``origin_country`` from the stored metadata (``meta_json``); else read
    the persisted ``preliminary_reference`` edges (referring court text + embedded country)."""
    origin = catalogue.document_meta(stable_id).get("origin_country")
    if origin and _UK_COUNTRY_RE.search(origin):
        return True
    for r in catalogue.relations_for(stable_id):
        if r["relationship_type"] == str(RelationshipType.PRELIMINARY_REFERENCE):
            if r["raw_citation_string"] and _UK_REFERRAL_RE.search(r["raw_citation_string"]):
                return True
    return False


def extract_document(
    catalogue: Catalogue, textstore: TextStore, stable_id: str,
    *, llm: CitationExtractor | None = None, aliases: dict[str, str] | None = None,
) -> int:
    """Extract citations from one document's text. Records every occurrence in the
    ``citations`` table (the audit/observation layer, with char spans for treatment
    classification §1.3a), then collapses them to **deduped** hanging edges in the
    ``relations`` graph (one per distinct candidate+pinpoint). Returns citation count."""
    doc = catalogue.get_document(stable_id)
    if doc is None or not doc["payload_hash"]:
        return 0
    try:
        text = textstore.get(doc["payload_hash"])
    except OSError:
        return 0
    if aliases is None:
        aliases = catalogue.named_alias_map()  # user shorthand rules (propagate)
    cites = extract_citations(text, llm=llm, aliases=aliases)

    # CJEU precision guard: a UK statute *name* ("<Title> Act <year>", "DPA 1998 s.5")
    # only resolves to UK legislation inside a CJEU judgment that was a UK-referred
    # preliminary ruling. Elsewhere in CJEU text an "X Act YYYY" shape is usually foreign
    # law in translation, so we keep the textual mention but drop the UK candidate
    # (→ name-only). Explicit legislation.gov.uk URLs/CELEX are unaffected — they're
    # unambiguous, not a heuristic.
    if _is_cjeu(doc) and not _uk_referred_preliminary(catalogue, stable_id):
        cites = [replace(c, candidate_id=None) if c.method in _UK_NAME_HEURISTICS else c
                 for c in cites]

    # respect human corrections: drop citations the user has rejected (§1.3a). The
    # suppressed edges are manual, so they survive the clear below and keep their veto.
    sup_ids, sup_raws = catalogue.suppressed_targets(stable_id)
    if sup_ids or sup_raws:
        cites = [c for c in cites if c.candidate_id not in sup_ids and c.raw not in sup_raws]

    # idempotent re-run: clear this source's prior observations + machine edges
    # (both literal-regex and the heuristic carry-forward 'inferred' edges)
    catalogue.clear_citations(stable_id)
    catalogue.clear_relations(stable_id, extracted_via=str(ExtractedVia.REGEX))
    catalogue.clear_relations(stable_id, extracted_via=str(ExtractedVia.INFERRED))

    catalogue.add_citations(stable_id, [
        {
            "raw": c.raw, "entity_kind": c.entity_kind, "candidate_id": c.candidate_id,
            "pinpoint": c.pinpoint, "char_start": c.char_start, "char_end": c.char_end,
            "method": c.method, "confidence": c.confidence,
        }
        for c in cites
    ])

    # collapse repeated citations of the same target into one edge
    edges: dict[tuple[str | None, str | None], TypedRelation] = {}
    for c in cites:
        key = (c.candidate_id, c.pinpoint)
        # carry-forward edges are heuristic guesses → mark them 'inferred' so the
        # graph keeps them distinguishable (and the UI can flag them as uncertain).
        via = ExtractedVia.INFERRED if c.method == "carry_forward" else ExtractedVia.REGEX
        if key not in edges:
            edges[key] = TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string=c.raw,
                dst_id=c.candidate_id,
                dst_anchor=c.pinpoint,
                extracted_via=via,
                resolution_status=ResolutionStatus.PENDING,
                context_start=c.char_start,  # representative span for §1.3a
                context_end=c.char_end,
            )
    catalogue.add_relations(stable_id, list(edges.values()))
    return len(cites)


def extract_corpus(
    catalogue: Catalogue, textstore: TextStore, *, stable_id: str | None = None,
    limit: int | None = None, llm: CitationExtractor | None = None,
) -> ExtractStats:
    """Extract over one document or the whole corpus (docs with text). Pass ``llm``
    to add the narrative-citation pass on top of the grammars (§5)."""
    stats = ExtractStats()
    aliases = catalogue.named_alias_map()  # load the user rules once for the whole run
    if stable_id:
        targets = [stable_id]
    else:
        rows = catalogue.list_documents(limit=limit or 100000)
        targets = [r["stable_id"] for r in rows if r["has_text"]]
    for sid in targets:
        n = extract_document(catalogue, textstore, sid, llm=llm, aliases=aliases)
        if n:
            stats.documents += 1
            stats.citations += n
    return stats
