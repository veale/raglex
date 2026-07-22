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

import logging
import multiprocessing
import os
import re
import threading
from dataclasses import dataclass, replace

from ..core.models import DocType, ExtractedVia, RelationshipType, ResolutionStatus, TypedRelation
from ..storage.catalogue import Catalogue
from ..storage.textstore import TextStore
from .extractor import CitationExtractor, extract_citations

log = logging.getLogger(__name__)


# --- runaway-extraction guard -------------------------------------------------
# Python's `re` holds the GIL for the whole of a single match attempt, so one
# pathological document (a backtracking-prone grammar meeting adversarial text)
# doesn't just stall its own job — it starves every thread in the process, the
# API's event loop included (the 2026-07 outage: one 747KB annexure table of
# names pinned the whole server for hours). The grammar pass therefore runs in a
# persistent spawn'd worker process with a hard wall-clock budget: a runaway
# document costs one killed worker and a warning, never the service. The worker
# is reused across documents (spawn + grammar import are paid once per life).


def _extract_worker(conn) -> None:  # pragma: no cover — exercised via the guard
    from raglex.citations.extractor import extract_citations as _extract

    while True:
        try:
            item = conn.recv()
        except (EOFError, KeyboardInterrupt):
            return
        if item is None:
            return
        text, aliases = item
        try:
            defs: list[dict] = []
            cites = _extract(text, aliases=aliases, defs_out=defs)
            conn.send(("ok", (cites, defs)))
        except Exception as exc:  # surfaced to the caller as RuntimeError
            conn.send(("err", f"{type(exc).__name__}: {exc}"))


class _ExtractionGuard:
    """One guarded worker per process, shared by every job thread (extraction was
    GIL-serialised before, so funnelling through one worker loses no parallelism)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc = None
        self._conn = None

    @staticmethod
    def timeout_s() -> float:
        return float(os.environ.get("RAGLEX_EXTRACT_TIMEOUT_S") or 90)

    def extract(self, text: str, aliases: dict[str, str] | None):
        """``extract_citations`` under a wall-clock budget, as ``(citations, shorthand
        definitions)``; None = budget blown. The definitions ride back with the result
        because the extractor already collected them — recomputing them in the parent
        cost ~4% of a whole-corpus rescan."""
        if os.environ.get("RAGLEX_EXTRACT_INPROC"):  # tests / debugging escape hatch
            return self._inproc(text, aliases)
        with self._lock:
            try:
                self._ensure()
                self._conn.send((text, aliases))
            except Exception:  # spawn unavailable / worker torn down mid-send
                self._kill()
                return self._inproc(text, aliases)
            if not self._conn.poll(self.timeout_s()):
                self._kill()
                return None
            try:
                status, payload = self._conn.recv()
            except (EOFError, OSError):
                # worker CRASHED (broken spawn env, OOM…) — that's not the runaway
                # case (a runaway hangs → timeout above), so run this document
                # in-process rather than mis-report it as "exceeded budget".
                self._kill()
                log.warning("[cite-extract] worker died mid-document — extracting in-process")
                return self._inproc(text, aliases)
            if status == "err":
                raise RuntimeError(f"extraction worker: {payload}")
            return payload

    @staticmethod
    def _inproc(text: str, aliases: dict[str, str] | None):
        defs: list[dict] = []
        return extract_citations(text, aliases=aliases, defs_out=defs), defs

    def _ensure(self) -> None:
        if self._proc is None or not self._proc.is_alive():
            ctx = multiprocessing.get_context("spawn")  # fork is unsafe in a threaded server
            self._conn, child = ctx.Pipe()
            self._proc = ctx.Process(target=_extract_worker, args=(child,), daemon=True)
            self._proc.start()
            child.close()

    def _kill(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout=5)
        if self._conn is not None:
            self._conn.close()
        self._proc = self._conn = None


_GUARD = _ExtractionGuard()


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


def _is_irish_case(doc) -> bool:
    """Is this document a judgment of an Irish court? Inside one, an "<X> Act 1963"
    name is almost always an Act of the Oireachtas, so the UK statute-name heuristics
    must not link it to UK legislation (EU instruments and case citations of any
    jurisdiction are unaffected — those are fine cross-border). Symmetrically, an
    Irish-statute name grammar (once Irish legislation is populated) must be gated
    to Irish hosts, so a UK judgment never links Irish acts by name."""
    from .courts import IRISH_COURTS

    court = (doc["court"] or "").lower()
    prefix = (doc["stable_id"] or "").split("/", 1)[0].lower()
    return doc["source"] == "ie-caselaw" or court in IRISH_COURTS or prefix in IRISH_COURTS


# EU regulatory guidance / DPA decisions (EDPB, Article 29 WP, the one-stop-shop
# register). These link cleanly to EU legislation (CELEX), CJEU + ECHR case law (ECLI,
# case numbers) and — usefully — English & Irish case-law neutral citations, all of
# which are unambiguous identifiers. But a bare *domestic* statute NAME ("Data
# Protection Act 2018") in an EU-level document is a cross-jurisdiction name collision
# (an EDPB guideline referencing "the Data Protection Act" could mean any member
# state's), so keep the textual mention but drop the domestic-legislation candidate
# (→ name-only) — exactly the guard the CJEU and Irish judgments already use.
_EU_GUIDANCE_SOURCES = frozenset({"edpb", "edpb-oss", "a29wp"})


def _is_eu_guidance(doc) -> bool:
    return doc["source"] in _EU_GUIDANCE_SOURCES


def _is_eu_material(doc) -> bool:
    """EU-origin texts in which bare "the Charter" unambiguously means CFREU."""
    source = (doc["source"] or "").lower()
    return (_is_cjeu(doc) or source.startswith("eu-") or source in
            _EU_GUIDANCE_SOURCES | {"dma-cases"})


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


# --- corpus-wide shorthand store ---------------------------------------------
# A shorthand a document defines ("Suncor Energy Inc v … 2021 FC 138 [Suncor]") is
# useful in the NEXT document too — but only there, and only under gates, or a bare
# "FCA" would link the Federal Courts Act into every judgment that uses the letters.
# The gates: the citing document must already cite the parent by some other means; a
# case short-name still needs a pincite; an ambiguous shorthand is never guessed.
#
# Both halves run inside the whole-corpus rescan (~700k documents, parallel workers),
# so neither may add a per-document query or a hot-row write:
#   - READ  — the whole store is loaded once per process and cached (it is small: a
#             shorthand per few hundred documents), so application costs zero queries.
#   - WRITE — insert-only, and pre-filtered against a process-local set of pairs
#             already known, so a re-extraction of a settled corpus issues no writes.
_SHORTHAND_TTL_S = 900.0


def _shorthands_enabled() -> bool:
    return (os.environ.get("RAGLEX_SHORTHAND_GLOBAL") or "1") not in ("0", "false", "no")


class _ShorthandStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._loaded = False
        self._by_candidate: dict[str, list[tuple]] = {}
        self._by_name: dict[str, set[str]] = {}
        self._known: set[tuple[str, str]] = set()

    def load(self, catalogue: Catalogue) -> tuple[dict, dict]:
        import time

        with self._lock:
            # `_loaded`, not "is the map non-empty": an EMPTY store is a legitimate
            # steady state (a fresh corpus), and re-querying it would put one COUNT-ish
            # scan per document back into the rescan hot loop.
            if self._loaded and time.monotonic() - self._loaded_at < _SHORTHAND_TTL_S:
                return self._by_candidate, self._by_name
            try:
                by_cand = catalogue.learned_shorthand_map()
            except Exception:  # noqa: BLE001 — a missing/locked table must not fail extraction
                by_cand = {}
            by_name: dict[str, set[str]] = {}
            for cid, rows in by_cand.items():
                for name, _kind, _abbrev in rows:
                    by_name.setdefault(name, set()).add(cid)
            self._by_candidate, self._by_name = by_cand, by_name
            self._known |= {(n, c) for c, rows in by_cand.items() for n, _k, _a in rows}
            self._loaded_at = time.monotonic()
            self._loaded = True
            return by_cand, by_name

    def unseen(self, defs: list[dict]) -> list[dict]:
        """The definitions this process has not already stored — the filter that keeps a
        steady-state rescan from issuing one INSERT per document per shorthand."""
        with self._lock:
            return [d for d in defs
                    if (d["shorthand"], d["candidate_id"]) not in self._known]

    def note_stored(self, defs: list[dict]) -> None:
        """Record freshly written pairs AND fold them into the live map, so a shorthand
        learned early in a rescan is usable by the very next document rather than waiting
        out the reload TTL."""
        with self._lock:
            for d in defs:
                key = (d["shorthand"], d["candidate_id"])
                if key in self._known:
                    continue
                self._known.add(key)
                self._by_candidate.setdefault(d["candidate_id"], []).append(
                    (d["shorthand"], d.get("entity_kind"), bool(d.get("is_abbrev"))))
                self._by_name.setdefault(d["shorthand"], set()).add(d["candidate_id"])


_SHORTHANDS = _ShorthandStore()


def reset_shorthand_cache() -> None:
    """Forget the cached store — for tests, which build several corpora in one process,
    and for a long-lived server that should pick up a rebuilt table promptly."""
    global _SHORTHANDS
    _SHORTHANDS = _ShorthandStore()


def _stored_shorthands_for(catalogue: Catalogue, cites: list) -> list[tuple]:
    """Stored shorthands applicable to this document: those whose parent candidate the
    document ALREADY cites, minus anything ambiguous.

    Ambiguity guard — a shorthand registered against more than one candidate is never
    guessed. It applies only when exactly one of its candidates is cited here (then the
    document itself has disambiguated it); otherwise it is dropped."""
    cited = {c.candidate_id for c in cites if c.candidate_id}
    if not cited:
        return []
    by_cand, by_name = _SHORTHANDS.load(catalogue)
    if not by_cand:
        return []
    out: list[tuple] = []
    for cid in cited:
        for name, kind, abbrev in by_cand.get(cid, ()):
            owners = by_name.get(name) or {cid}
            if len(owners) > 1 and len(owners & cited) != 1:
                continue
            out.append((name, cid, kind, abbrev))
    return out


def extract_document(
    catalogue: Catalogue, textstore: TextStore, stable_id: str,
    *, llm: CitationExtractor | None = None, aliases: dict[str, str] | None = None,
    run_id: str | None = None,
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
    if llm is None:
        guarded = _GUARD.extract(text, aliases)
        cites, raw_defs = guarded if guarded is not None else (None, [])
        if cites is None:
            # budget blown: keep whatever rows a previous run left, stamp so
            # staleness-scoped reruns converge instead of re-hitting the doc
            log.warning("[cite-extract] %s: grammar pass exceeded %.0fs budget — skipped",
                        stable_id, _GUARD.timeout_s())
            catalogue.mark_extracted(stable_id, run_id=run_id)
            return 0
    else:  # the llm extractor is not picklable (and may call the network) — unguarded
        raw_defs = []
        cites = extract_citations(text, llm=llm, aliases=aliases, defs_out=raw_defs)

    # Inside LEGISLATION, a bare "Article 3" / "paragraph 2" is almost always the
    # instrument referring to ITSELF, not to the directive it last named — the
    # carry-forward heuristic was built for judgments citing statutes, and applied
    # to an act's own text it mislinks self-references to whatever instrument the
    # recitals mentioned last. Drop the guesses; literal citations are unaffected.
    if doc["doc_type"] == str(DocType.LEGISLATION):
        cites = [c for c in cites if c.method != "carry_forward"]

    # Inside a JUDGMENT, a bare "paragraph N" refers to the judgment's own
    # numbered paragraphs ("in paragraph 77 above") or a cited case's — never to
    # legislation, whose paragraphs are cited literally ("para 2 of Schedule 1",
    # caught by the full grammar). The adjacency guard in the extractor catches
    # the case-citation form; this drops the rest of the class at the doc level
    # (the 2026-07 probe residue: 385k judgment-source para edges). Section /
    # Article carry-forwards — the heuristic's real purpose — are unaffected.
    if doc["doc_type"] in (str(DocType.JUDGMENT), str(DocType.DECISION), str(DocType.OPINION)):
        cites = [c for c in cites
                 if not (c.method == "carry_forward" and c.raw.lower().startswith("para"))]

    # CJEU precision guard: a UK statute *name* ("<Title> Act <year>", "DPA 1998 s.5")
    # only resolves to UK legislation inside a CJEU judgment that was a UK-referred
    # preliminary ruling. Elsewhere in CJEU text an "X Act YYYY" shape is usually foreign
    # law in translation, so we keep the textual mention but drop the UK candidate
    # (→ name-only). Explicit legislation.gov.uk URLs/CELEX are unaffected — they're
    # unambiguous, not a heuristic.
    if _is_cjeu(doc) and not _uk_referred_preliminary(catalogue, stable_id):
        cites = [replace(c, candidate_id=None) if c.method in _UK_NAME_HEURISTICS else c
                 for c in cites]

    # Irish precision guard: inside an Irish judgment, "<Title> Act 1963" names an Act
    # of the Oireachtas, not the UK statute of the same shape — keep the mention, drop
    # the UK candidate (→ name-only). EU instruments and case citations (UK or Irish)
    # resolve normally. The bare "section N" carry-forward follows automatically: with
    # no UK candidate there is no legislation antecedent to attach to.
    if _is_irish_case(doc):
        cites = [replace(c, candidate_id=None) if c.method in _UK_NAME_HEURISTICS else c
                 for c in cites]

    # EU guidance guard (EDPB / A29WP / OSS decisions): an EU-level document must not
    # link a *domestic* statute by NAME (cross-jurisdiction collision), but its EU-law
    # (CELEX), CJEU/ECHR (ECLI) and English/Irish case-law (neutral-citation) links are
    # all unambiguous and kept. Domestic (ICO etc.) guidance is deliberately NOT gated —
    # there a "Data Protection Act 2018" reference IS to the national statute.
    if _is_eu_guidance(doc):
        cites = [replace(c, candidate_id=None) if c.method in _UK_NAME_HEURISTICS else c
                 for c in cites]

    # Bare "the Charter" is EU-local shorthand: in a national text it may mean a
    # domestic constitutional charter. Explicit "EU Charter", CFREU and the formal
    # name remain globally unambiguous.
    if not _is_eu_material(doc):
        cites = [replace(c, candidate_id=None)
                 if c.method == "eu_treaty_12012P"
                 and re.search(r"(?i)\bthe\s+Charter\s*$", c.raw.strip()) else c
                 for c in cites]

    # bundesrecht intentionally accepts abbreviation-shaped tails. At corpus scale
    # those need a resolver gate: ``§ 1 Pachtgegenstand`` is a contract heading,
    # whereas ``§ 8 MarkenG`` resolves to an imported GII law alias. Apply the gate
    # in every host jurisdiction so translations cannot create German phantom laws.
    de_known: dict[str, bool] = {}
    filtered = []
    for c in cites:
        if c.method != "de_law_reference" or not c.candidate_id:
            filtered.append(c)
            continue
        known = de_known.get(c.candidate_id)
        if known is None:
            known = catalogue.find_document_id(c.candidate_id) is not None
            de_known[c.candidate_id] = known
        if known:
            filtered.append(c)
    cites = filtered

    # Corpus-wide shorthands: apply the ones learned elsewhere whose parent this
    # document already cites, then harvest the ones IT defines for the next document.
    # Both are no-ops for a document that cites nothing resolvable.
    if _shorthands_enabled() and any(c.candidate_id for c in cites):
        from .extractor import attach_stored_shorthands

        # The definitions come from the extractor, but the jurisdiction guards above ran
        # AFTER it and may have stripped a candidate (a UK statute name inside a CJEU
        # judgment). Keep only definitions whose target survived, or the store would
        # learn precisely the links those guards exist to prevent.
        live = {c.candidate_id for c in cites if c.candidate_id}
        defs = [d for d in raw_defs if d["candidate_id"] in live]
        stored = _stored_shorthands_for(catalogue, cites)
        if stored:
            # an in-document definition always beats a stored one, so exclude the names
            # this document defines for itself (already linked by the extractor's pass)
            cites = attach_stored_shorthands(
                text, cites, stored, exclude={d["shorthand"] for d in defs})
        fresh = _SHORTHANDS.unseen(defs)
        if fresh:
            try:
                catalogue.add_learned_shorthands(fresh, doc_id=stable_id)
                _SHORTHANDS.note_stored(fresh)
            except Exception as exc:  # noqa: BLE001 — learning is best-effort
                log.debug("[cite-extract] %s: shorthand store write failed: %s", stable_id, exc)

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
    edges = _drop_self_citations(catalogue, stable_id, edges)
    catalogue.add_relations(stable_id, list(edges.values()))
    # durable "last rescanned at" stamp — set even when the document cited nothing, so a
    # staleness-scoped rescan can skip it next time (§5).
    catalogue.mark_extracted(stable_id, run_id=run_id)
    return len(cites)


def _drop_self_citations(catalogue: Catalogue, stable_id: str, edges: dict) -> dict:
    """A judgment's header prints the document's OWN identity — its neutral citation
    ("Neutral Citation Number: [2000] EWCA Civ 18") or, for a law-report-sourced text,
    the report citation it was published at ("12 QBD 271" opening an ICLR page).
    Extracted naively those become outgoing edges: a self-loop once the alias exists,
    or a phantom "cited but unfetchable" entry until then. Drop every edge whose target
    resolves to the citing document itself (one batched lookup; the citation
    *observations* stay, so the reader can still see the span — it just isn't an edge)."""
    from ..resolve.matchers import normalise_candidate
    from ..core.text import fold

    keys = {ek: (normalise_candidate(rel.dst_id, rel.raw_citation_string)
                 or fold(rel.raw_citation_string or ""))
            for ek, rel in edges.items()}
    hits = catalogue.find_existing([k for k in keys.values() if k])
    return {ek: rel for ek, rel in edges.items()
            if not keys[ek] or hits.get(keys[ek]) != stable_id}


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
