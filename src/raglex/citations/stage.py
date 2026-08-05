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

import json
import logging
import multiprocessing
import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import date

from ..core.models import DocType, ExtractedVia, RelationshipType, ResolutionStatus, TypedRelation
from ..storage.catalogue import Catalogue
from ..storage.textstore import TextStore
from .extractor import CitationExtractor, extract_citations
from .intituling import parse_intituling
from .us_cases import AMBIGUOUS_METHOD

log = logging.getLogger(__name__)

# US reporters are plausible only in US material and common-law judgments. Without
# this jurisdiction gate, compact strings such as ``159 P. 1`` in EU/French material
# manufacture Pacific Reporter cases from Official Journal/page notation. Keep the
# list explicit: mixed/civil-law corpora do not opt in merely because they use English.
_COMMON_LAW_CASE_SOURCES = (
    "uk-", "ie-", "ca-caselaw", "au-caselaw", "nz-caselaw", "in-caselaw",
    "sg-caselaw", "hk-caselaw", "za-caselaw", "africa-caselaw",
    "caribbean-caselaw", "pacific-caselaw", "offshore-caselaw",
    "bailii", "westlaw",
)
_CASE_DOC_TYPES = {str(DocType.JUDGMENT), str(DocType.DECISION), str(DocType.OPINION)}


def _allows_us_reporters(doc) -> bool:
    source = str(doc["source"] or "").lower()
    if source.startswith("us-"):
        return True
    return (str(doc["doc_type"]) in _CASE_DOC_TYPES
            and source.startswith(_COMMON_LAW_CASE_SOURCES))


# A legislative document's own identity, as the carry-forward pass wants it. Inside
# the GDPR a bare "Article 8" means the GDPR's Article 8 — a self-reference, not a
# pointer at whichever directive the recitals last cross-referred to (which is what
# it resolved to before: "Article 8 of 32009L0022", flagged by a reader). Only
# legislation has a home; a judgment's bare provisions really do belong to whatever
# statute it was discussing.
_CELEX_KIND = {"R": "regulation", "L": "directive", "D": "decision", "E": "treaty"}


def _meta_of(doc) -> dict:
    try:
        raw = doc["meta_json"]
    except (KeyError, IndexError):
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _home_of(doc) -> tuple[str | None, str | None]:
    if doc["doc_type"] != str(DocType.LEGISLATION):
        # Some guidance has one explicit governing instrument throughout (for
        # example the Commission's UCPD notice). Adapters may declare that fact so
        # an orphan "Article 5" returns to the document's subject after a sentence
        # discussing another directive. This is deliberately opt-in; mixed-law
        # registers and weekly bulletins must never guess a home.
        default = _meta_of(doc).get("citation_default_instrument")
        if isinstance(default, dict) and default.get("id"):
            return str(default["id"]), str(default.get("kind") or "named")
        # Existing guidance may predate that metadata field. A title which itself
        # deterministically cites exactly one instrument is equally explicit:
        # "Guidelines … under Article 50 of the AI Act" cannot sensibly let later
        # orphaned Article 50 references drift to the last directive in a footnote.
        if doc["doc_type"] == str(DocType.GUIDANCE):
            from .extractor import grammar_citations

            title_cites = [
                c for c in grammar_citations(str(doc["title"] or ""))
                if c.candidate_id and c.entity_kind in {
                    "act", "regulation", "directive", "decision", "treaty",
                    "eu_instrument", "named",
                }
            ]
            candidates = list(dict.fromkeys(c.candidate_id for c in title_cites))
            if len(candidates) == 1:
                host = next(c for c in title_cites if c.candidate_id == candidates[0])
                return candidates[0], host.entity_kind or "named"
        return None, None
    sid = str(doc["stable_id"] or "")
    if not sid:
        return None, None
    m = re.match(r"^\d{5}([A-Z])\d{4}$", sid)
    if m:
        return sid, _CELEX_KIND.get(m.group(1), "eu_instrument")
    return sid, "act" if sid.startswith(("ukpga/", "asp/", "nia/", "anaw/")) else "named"


def _reference_date(doc) -> str:
    """Date on which a citing document should be matched to consolidated law."""
    meta = _meta_of(doc)
    for value in (
        meta.get("updated_at"), meta.get("public_updated_at"), doc["decision_date"],
    ):
        value = str(value or "")[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
    return date.today().isoformat()


def _attach_applicable_versions(catalogue: Catalogue, doc, edges: dict) -> dict:
    """Keep the literal base-law citation and add its applicable dated expression.

    The derived edge is a different relationship type and ``inferred`` provenance:
    it says which held text was current on the source's date, not that the author
    literally printed the consolidation identifier.
    """
    reference_date = _reference_date(doc)
    source_relations = list(edges.values())
    # Adapter-declared and manually curated law links survive citation re-extraction
    # and need the same temporal companion even if the body grammar found nothing.
    versionable_types = {
        str(RelationshipType.MENTIONS), str(RelationshipType.INTERPRETS),
        str(RelationshipType.APPLIES), str(RelationshipType.CONSIDERS),
        str(RelationshipType.FOLLOWS), str(RelationshipType.DISTINGUISHES),
        str(RelationshipType.OVERRULES), str(RelationshipType.CITES_FOR_FACT),
    }
    for row in catalogue.relations_for(str(doc["stable_id"])):
        if row["relationship_type"] not in versionable_types:
            continue
        source_relations.append(TypedRelation(
            relationship_type=RelationshipType(row["relationship_type"]),
            raw_citation_string=row["raw_citation_string"],
            dst_id=row["dst_id"] or row["candidate_id"],
            dst_anchor=row["dst_anchor"],
            extracted_via=ExtractedVia(row["extracted_via"]),
            resolution_status=ResolutionStatus(row["resolution_status"]),
            context_start=row["context_start"],
            context_end=row["context_end"],
        ))
    applicable_by_base = catalogue.applicable_legislative_versions(
        [
            rel.dst_id for rel in source_relations
            if str(rel.relationship_type) in versionable_types and rel.dst_id
        ],
        reference_date,
    )
    for rel in source_relations:
        if str(rel.relationship_type) not in versionable_types or not rel.dst_id:
            continue
        applicable = applicable_by_base.get(rel.dst_id)
        if not applicable:
            continue
        version_id, version_date = applicable
        key = ("applicable_version", version_id, rel.dst_anchor)
        edges.setdefault(
            key,
            TypedRelation(
                relationship_type=RelationshipType.APPLICABLE_VERSION,
                raw_citation_string=rel.raw_citation_string,
                dst_id=version_id,
                dst_anchor=rel.dst_anchor,
                src_anchor=(
                    f"applicable on {reference_date}; consolidation {version_date}"
                ),
                extracted_via=ExtractedVia.INFERRED,
                resolution_status=ResolutionStatus.RESOLVED,
                context_start=rel.context_start,
                context_end=rel.context_end,
            ),
        )
    return edges


def _is_us_source(doc) -> bool:
    """A US document — the only place an AMBIGUOUS reporter abbreviation is trusted.

    A single-letter series between two numbers ("12 F. 13") is a real citation in an
    American law report and page/folio notation nearly everywhere else, so the
    common-law allowance above is too generous for that class: a Canadian judgment
    citing US authority does so in F.2d/F.3d, while its French half is full of
    ``p.``-shaped noise. National material only, then — the rule the Pacific Reporter
    phantoms ("10 p. 100" = "10 pour cent") earned the hard way."""
    return str(doc["source"] or "").lower().startswith("us-")


# --- runaway-extraction guard -------------------------------------------------
# Python's `re` holds the GIL for the whole of a single match attempt, so one
# pathological document (a backtracking-prone grammar meeting adversarial text)
# doesn't just stall its own job — it starves every thread in the process, the
# API's event loop included (the 2026-07 outage: one 747KB annexure table of
# names pinned the whole server for hours). The grammar pass therefore runs in a
# persistent spawn'd worker process with a hard wall-clock budget: a runaway
# document costs one killed worker and a warning, never the service. The worker
# is reused across documents (spawn + grammar import are paid once per life).


# Worker opcodes. Both messages are ``(op, payload)``, so a shape mismatch is a
# KeyError-ish failure inside the try below rather than a torn-down worker.
_OP_EXTRACT = "extract"
_OP_ATTACH = "attach"


def _extract_worker(conn) -> None:  # pragma: no cover — exercised via the guard
    from raglex.citations.extractor import attach_stored_shorthands as _attach
    from raglex.citations.extractor import extract_citations as _extract

    held_text = ""    # the document this worker last extracted, kept for _OP_ATTACH
    while True:
        try:
            item = conn.recv()
        except (EOFError, KeyboardInterrupt):
            return
        if item is None:
            return
        try:
            # Unpack INSIDE the guard. A caller that sends the wrong shape used to kill
            # the worker on this line, and the parent read that as "the worker crashed
            # (OOM?), extract this one in the parent" — a silent, correct-but-serial
            # fallback that hid a protocol mismatch for a day: no parallelism, a spawn
            # burnt per document, and the runaway-regex budget (which only the worker
            # has) no longer covering the pass.
            op, payload = item
            if op == _OP_ATTACH:
                # Phase two of the same document: apply the corpus-wide shorthands the
                # parent selected. The text is already here, so only the (small) cite
                # list and the applicable shorthand rows cross the pipe. This is the
                # single most expensive thing in the whole pass and it is pure CPU on
                # text, so it belongs on a worker core, not on the parent's.
                cites, stored, exclude = payload
                conn.send(("ok", _attach(held_text, cites, stored,
                                         exclude=frozenset(exclude))))
                continue
            text, aliases, home_id, home_kind = payload
            held_text = text
            defs: list[dict] = []
            cites = _extract(text, aliases=aliases, defs_out=defs,
                             home_id=home_id, home_kind=home_kind)
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

    @staticmethod
    def budget_for(text: str | None) -> float:
        """The wall-clock budget for ONE document, scaled by its length.

        The guard exists to kill RUNAWAY extraction — catastrophic backtracking, which is
        super-linear and never finishes. A flat 90s does not distinguish that from a
        document which is merely enormous, so it silently dropped the biggest documents
        in the corpus: a CMA market investigation whose text is 19 MB (the grammar pass
        alone is 48s), the Communications Act at 2 MB, half the Law Commission's reports.
        Those documents cite the most, and they were the ones getting no citations at all.

        Linear in length above the base, with a ceiling, so a genuinely pathological
        document still dies — it just has to be pathological rather than long.
        """
        base = _ExtractionGuard.timeout_s()
        ceiling = float(os.environ.get("RAGLEX_EXTRACT_TIMEOUT_MAX_S") or 900)
        return min(base + len(text or "") / 100_000.0, max(base, ceiling))

    def extract(self, text: str, aliases: dict[str, str] | None,
                home: tuple[str | None, str | None] = (None, None)):
        """``extract_citations`` under a wall-clock budget, as ``(citations, shorthand
        definitions)``; None = budget blown. The definitions ride back with the result
        because the extractor already collected them — recomputing them in the parent
        cost ~4% of a whole-corpus rescan."""
        if os.environ.get("RAGLEX_EXTRACT_INPROC"):  # tests / debugging escape hatch
            return self._inproc(text, aliases, home)
        with self._lock:
            try:
                self._ensure()
                self._conn.send((_OP_EXTRACT, (text, aliases, home[0], home[1])))
            except Exception:  # spawn unavailable / worker torn down mid-send
                self._kill()
                return self._inproc(text, aliases, home)
            if not self._conn.poll(self.budget_for(text)):
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
                return self._inproc(text, aliases, home)
            if status == "err":
                raise RuntimeError(f"extraction worker: {payload}")
            return payload

    @staticmethod
    def _inproc(text: str, aliases: dict[str, str] | None,
                home: tuple[str | None, str | None] = (None, None)):
        defs: list[dict] = []
        return extract_citations(text, aliases=aliases, defs_out=defs,
                                 home_id=home[0], home_kind=home[1]), defs

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
    # how many ids the parallel pass actually completed (documents counts only the
    # ones that yielded citations, matching extract_corpus's historical meaning)
    processed: int = 0
    cancelled: bool = False

    def summary(self) -> str:
        return f"[cite-extract] documents={self.documents} citations={self.citations}"


# --- the parallel bulk path ---------------------------------------------------
# One guarded worker preserves the single-runaway containment; N of them buy the
# other N-1 cores back. The grammar pass is CPU-bound pure Python (the GIL made
# thread pools useless), documents are independent, and the DB half of the stage
# needs the shared connection anyway — so the shape is: a pool of spawn'd workers
# doing regex, and the PARENT doing what it always did (guards + writes) as
# results stream back. Each worker keeps the guard's semantics: its own pipe, its
# own wall-clock budget per document, killed and respawned on a runaway.

def _pool_size(workers: int | None) -> int:
    if workers is not None:
        return max(1, int(workers))
    raw = os.environ.get("RAGLEX_EXTRACT_WORKERS", "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return max(1, (os.cpu_count() or 2) - 1)   # leave one core for the writer/API


class _PoolWorker:
    """One guarded worker: spawn process + pipe + the doc currently in flight.

    A document occupies its worker for two phases — ``extract`` (the grammar pass) and
    optionally ``attach`` (the corpus-wide shorthand scan over the same text). ``phase``
    says which reply to expect; ``pending`` carries what the parent needs to fall back
    in-process if the worker dies mid-attach.
    """

    __slots__ = ("proc", "conn", "item", "deadline", "phase", "pending")

    def __init__(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_extract_worker, args=(child,), daemon=True)
        self.proc.start()
        child.close()
        self.item = None        # (stable_id, doc_row, text) while busy
        self.deadline = 0.0
        self.phase = _OP_EXTRACT
        self.pending = None     # (cites, stored, exclude, defs) while attaching

    def kill(self) -> None:
        try:
            if self.proc.is_alive():
                self.proc.terminate()
                self.proc.join(timeout=5)
        finally:
            try:
                self.conn.close()
            except OSError:
                pass


def extract_documents_parallel(
    catalogue: Catalogue, textstore: TextStore, ids, *,
    aliases: dict[str, str] | None = None,
    llm: CitationExtractor | None = None,
    run_id: str | None = None,
    workers: int | None = None,
    commit_every: int = 50,
    on_progress=None, cancel_check=None,
    stage: str = "extracting citations",
    report_every: int | None = None,
    checkpoint_fn=None,
    post_fn=None,
) -> ExtractStats:
    """Extract citations over ``ids`` using a pool of guarded worker processes.

    The drop-in bulk form of calling :func:`extract_document` in a loop, with three
    changes that matter at import scale:

    * the grammar pass runs on N cores instead of one (workers default to
      ``cpu_count-1``, overridable via ``RAGLEX_EXTRACT_WORKERS``);
    * DB writes overlap the regex work (the parent writes finished documents while
      the workers chew the next ones);
    * commits are batched (``commit_every``) instead of several per document — safe
      because every driver of this path resumes off the durable
      ``last_extracted_at`` stamp / citation rows, so a crash merely re-extracts at
      most one uncommitted batch, idempotently.

    Progress/checkpoint events are only emitted **after** a commit, so a resumed
    job can never trust a checkpoint whose rows were lost. ``checkpoint_fn(done,
    last_id)`` builds the caller's checkpoint payload; ``post_fn(stable_id)`` runs
    in the parent after each finished document (the harvest path's per-document
    treatment classification).

    Serial fallbacks, preserving exact single-worker semantics: an ``llm``
    extractor (unpicklable, network-bound), ``workers=1`` on a 1-core box, or
    ``RAGLEX_EXTRACT_INPROC`` (tests).
    """
    import time as _time
    from multiprocessing.connection import wait as _mpwait

    ids = list(ids)
    total = len(ids)
    stats = ExtractStats()
    if not ids:
        return stats
    if aliases is None:
        aliases = catalogue.named_alias_map()
    if report_every is None:
        # per-document progress is right for a 30-item watch, wrong for a 1.7m-doc
        # bulk seed (each callback yields the GIL in the job runner)
        report_every = 1 if total <= 2000 else 200

    def _emit(done: int, sid: str, *, with_checkpoint: bool = True) -> None:
        if on_progress and (done == 1 or done % report_every == 0 or done == total):
            payload = {"stage": stage, "done": done, "total": total, "item": sid}
            # a checkpoint must never run ahead of committed rows — between batch
            # commits the event carries progress only, never a resume point
            if checkpoint_fn is not None and with_checkpoint:
                payload["_checkpoint"] = checkpoint_fn(done, sid)
            on_progress(**payload)

    n_workers = _pool_size(workers)
    # A pool only pays once there's enough work to amortise the spawns (~100ms each):
    # a 5-document watch tick or a unit test is faster — and identical — serial.
    if (llm is not None or n_workers <= 1 or total < 32
            or os.environ.get("RAGLEX_EXTRACT_INPROC")):
        # serial path — identical to the historical loop, one commit per document
        for i, sid in enumerate(ids, 1):
            if cancel_check and cancel_check():
                stats.cancelled = True
                break
            try:
                n = extract_document(catalogue, textstore, sid, llm=llm,
                                     aliases=aliases, run_id=run_id)
            except Exception:  # noqa: BLE001 — one bad doc must not sink the batch
                log.exception("[cite-extract] %s failed", sid)
                n = 0
            if post_fn is not None:
                post_fn(sid)
            stats.processed += 1
            if n:
                stats.documents += 1
                stats.citations += n
            _emit(i, sid)
        return stats

    pool = [_PoolWorker() for _ in range(n_workers)]
    queue = iter(ids)
    done = 0
    since_commit = 0
    cancelled = False

    def _load_next(worker: _PoolWorker) -> bool:
        """Feed the next usable document to a free worker; False when exhausted."""
        for sid in queue:
            doc = catalogue.get_document(sid)
            if doc is None or not doc["payload_hash"]:
                _count_done(sid, 0)
                continue
            try:
                text = textstore.get(doc["payload_hash"])
            except OSError:
                _count_done(sid, 0)
                continue
            try:
                # the worker's protocol is (op, (text, aliases, home_id, home_kind)) —
                # the instrument a bare "Article 50(2)" belongs to travels with the
                # document. Source-scoped shorthands travel with the document, not the
                # run: a bulk rescan mixes sources in one pool.
                worker.conn.send((_OP_EXTRACT,
                                  (text, aliases_for_document(doc, aliases, text),
                                   *_home_of(doc))))
            except (OSError, ValueError):
                return False        # worker torn down — caller respawns
            worker.item = (sid, doc, text)
            worker.phase = _OP_EXTRACT
            worker.pending = None
            worker.deadline = _time.monotonic() + _ExtractionGuard.budget_for(text)
            return True
        return False

    def _count_done(sid: str, n: int) -> None:
        nonlocal done, since_commit
        done += 1
        stats.processed += 1
        if n:
            stats.documents += 1
            stats.citations += n
        since_commit += 1
        committed = False
        if since_commit >= commit_every or done == total:
            catalogue.commit()
            since_commit = 0
            committed = True
        _emit(done, sid, with_checkpoint=committed)

    def _write(sid: str, doc, text: str, cites) -> None:
        """The parent's remaining half: guards already applied, now persist."""
        try:
            n = _finish_writes(catalogue, doc, text, cites,
                               stable_id=sid, run_id=run_id, commit=False)
        except Exception:  # noqa: BLE001
            log.exception("[cite-extract] %s failed in finish", sid)
            n = 0
        if post_fn is not None:
            post_fn(sid)
        _count_done(sid, n)

    def _finish(sid: str, doc, text: str, payload) -> None:
        """Whole-document finish in the parent — the crash/serial fallback path."""
        cites, raw_defs = payload
        try:
            n = _finish_document(catalogue, doc, text, cites, raw_defs,
                                 stable_id=sid, run_id=run_id, commit=False)
        except Exception:  # noqa: BLE001
            log.exception("[cite-extract] %s failed in finish", sid)
            n = 0
        if post_fn is not None:
            post_fn(sid)
        _count_done(sid, n)

    def _after_extract(w: _PoolWorker, sid: str, doc, text: str, payload) -> None:
        """Grammar pass came back. Apply the parent-side guards, then hand the
        shorthand scan BACK to this worker rather than doing it here.

        That hand-back is the point of the two-phase protocol. The scan is the single
        most expensive step in the pass (~93% of the parent's serial half), it is pure
        CPU over text the worker still holds, and it is *pipelined* — the parent posts
        the request and returns to the wait loop, so this document's shorthand scan
        overlaps every other worker's grammar pass and the parent's own writes. A
        blocking round-trip here would move the cost without removing it.
        """
        cites, raw_defs = payload
        try:
            cites = _guard_cites(catalogue, doc, cites, stable_id=sid)
            plan = _shorthand_plan(catalogue, cites, raw_defs, doc)
        except Exception:  # noqa: BLE001
            log.exception("[cite-extract] %s failed in guards", sid)
            _count_done(sid, 0)
            return
        if plan is None:
            _write(sid, doc, text, cites)
            return
        stored, exclude, defs = plan
        if stored:
            try:
                w.conn.send((_OP_ATTACH, (cites, stored, list(exclude))))
            except (OSError, ValueError):
                pass            # worker gone — fall through and do it in the parent
            else:
                w.item = (sid, doc, text)
                w.phase = _OP_ATTACH
                # the guarded cites ride along so a worker death mid-scan costs only
                # the scan, not the grammar pass that produced them
                w.pending = (cites, stored, exclude, defs)
                w.deadline = _time.monotonic() + _ExtractionGuard.budget_for(text)
                return
            from .extractor import attach_stored_shorthands
            cites = attach_stored_shorthands(text, cites, stored, exclude=exclude)
            cites = _gate_domestic_statute_names(doc, cites)
        _learn_fresh_shorthands(catalogue, defs, sid)
        _write(sid, doc, text, cites)

    def _after_attach(w: _PoolWorker, sid: str, doc, text: str, cites) -> None:
        """Shorthand scan came back: re-gate (the store is corpus-wide and will happily
        bind a UK act into an Irish judgment), learn this document's own, then write."""
        _cites, _stored, _exclude, defs = w.pending or ([], (), frozenset(), [])
        cites = _gate_domestic_statute_names(doc, cites)
        _learn_fresh_shorthands(catalogue, defs, sid)
        _write(sid, doc, text, cites)

    try:
        for w in pool:
            if not _load_next(w):
                break
        while any(w.item is not None for w in pool):
            if cancel_check and cancel_check():
                cancelled = True
            busy = [w for w in pool if w.item is not None]
            next_deadline = min(w.deadline for w in busy)
            timeout = max(0.05, next_deadline - _time.monotonic())
            ready = _mpwait([w.conn for w in busy], timeout=timeout)
            now = _time.monotonic()
            for w in busy:
                if w.conn in ready:
                    sid, doc, text = w.item
                    phase = w.phase
                    try:
                        status, payload = w.conn.recv()
                    except (EOFError, OSError):
                        # worker CRASHED mid-document (OOM, broken spawn env) — run
                        # this one in the parent, like the single guard does, and
                        # replace the worker
                        log.warning("[cite-extract] worker died on %s — in-process", sid)
                        pending = w.pending
                        w.kill()
                        pool[pool.index(w)] = w = _PoolWorker()
                        if phase == _OP_ATTACH:
                            # It died on the shorthand scan, so the grammar pass and the
                            # guards are already done and are carried in ``pending`` —
                            # redo only the scan here, not the whole document.
                            from .extractor import attach_stored_shorthands
                            cites, stored, exclude, defs = pending
                            try:
                                cites = attach_stored_shorthands(
                                    text, cites, stored, exclude=exclude)
                                cites = _gate_domestic_statute_names(doc, cites)
                            except Exception:  # noqa: BLE001
                                log.exception("[cite-extract] %s failed in attach", sid)
                            _learn_fresh_shorthands(catalogue, defs, sid)
                            _write(sid, doc, text, cites)
                        else:
                            defs: list[dict] = []
                            _hid, _hkind = _home_of(doc)
                            cites = extract_citations(
                                text, aliases=aliases_for_document(doc, aliases, text),
                                defs_out=defs, home_id=_hid, home_kind=_hkind)
                            _finish(sid, doc, text, (cites, defs))
                    else:
                        w.item = None
                        w.phase = _OP_EXTRACT
                        if status == "err":
                            log.warning("[cite-extract] %s: %s", sid, payload)
                            _count_done(sid, 0)
                        elif phase == _OP_ATTACH:
                            _after_attach(w, sid, doc, text, payload)
                        else:
                            _after_extract(w, sid, doc, text, payload)
                elif w.deadline <= now:
                    # runaway document: kill this worker only, stamp the doc so
                    # staleness-scoped reruns converge (the guard's exact semantics)
                    sid = w.item[0]
                    log.warning("[cite-extract] %s: %s pass exceeded %.0fs budget "
                                "(%.1f MB) — skipped", sid, w.phase,
                                _ExtractionGuard.budget_for(w.item[2]),
                                len(w.item[2] or "") / 1e6)
                    w.kill()
                    pool[pool.index(w)] = w = _PoolWorker()
                    catalogue.mark_extracted(sid, run_id=run_id, commit=False)
                    _count_done(sid, 0)
                if w.item is None and not cancelled:
                    _load_next(w)
        stats.cancelled = cancelled
    finally:
        catalogue.commit()
        for w in pool:
            w.kill()
    return stats


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

# Shorthands that are unambiguous INSIDE one source and dangerous outside it. In an
# Investigatory Powers Tribunal judgment "RIPA" and "IPA" mean the two Acts the Tribunal
# exists to apply, every single time they appear; in the wider corpus "IPA" is an
# insolvency practitioners' association, an independent police authority, and a beer.
# Scoping the expansion to the source is what lets the certainty be used without
# spending it somewhere it isn't true.
# The year-suffixed forms are listed too: a judgment that writes "RIPA 2000" or
# "IPA 2016" once and the bare acronym thereafter must have both caught, and an alias
# matches the phrase it is given rather than a prefix of it.
_SOURCE_ALIASES: dict[str, dict[str, str]] = {
    "uk-ipt": {
        "RIPA": "ukpga/2000/23",
        "RIPA 2000": "ukpga/2000/23",
        "IPA": "ukpga/2016/25",
        "IPA 2016": "ukpga/2016/25",
    },
}


# Conventional abbreviations that are only safe once the document has NAMED the Act in
# full. A judgment writes "the Data Protection Act 2018 ('the DPA')" and then uses the
# letters for forty pages; without this every one of those is an unresolved mention. The
# full name appearing in the same document is what makes the expansion sound — a bare
# "DPA" on its own could as easily be a deferred prosecution agreement, and outside this
# gate it is left alone.
_UNLOCKED_BY_FULL_NAME: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("data protection act 2018", "ukpga/2018/12", ("DPA", "DPA 18", "DPA 2018")),
    ("data protection act 1998", "ukpga/1998/29", ("DPA 98", "DPA 1998")),
    ("investigatory powers act 2016", "ukpga/2016/25", ("IPA", "IPA 16", "IPA 2016")),
    ("regulation of investigatory powers act 2000", "ukpga/2000/23",
     ("RIPA", "RIPA 00", "RIPA 2000")),
    ("freedom of information act 2000", "ukpga/2000/36", ("FOIA", "FOIA 2000")),
    ("human rights act 1998", "ukpga/1998/42", ("HRA", "HRA 1998")),
)


# A code of practice defines its parent Act once and then says "the Act". Usually it
# does so in the text, in brackets, and the extractor's document-scoped host pass binds
# it (see ``_HOST_NOUNS``). Where the wording defeats that bracket, the harvester
# already knows the answer with certainty and recorded it: ``meta_json.statutory_basis``
# names the instrument the code is made under. Using it needs no NLP — but only as a
# FALLBACK, because a definition the document actually makes is better evidence than
# metadata about the document, and its position bounds where the binding starts.
_QUOTE = "\"'“”‘’"
_HOST_DEFINED_IN_TEXT = re.compile(
    rf"[(\[]\s*[{_QUOTE}]?\s*the\s+(?:(?:18|19|20)\d{{2}}\s+)?"
    rf"(?:Act|Code|Regulations?|Order|Rules)\s*[{_QUOTE}]?\s*[)\]]",
    re.IGNORECASE)


def _statutory_basis_alias(doc, text: str | None) -> dict[str, str]:
    """``{"the Act": <candidate>}`` from the harvester's recorded ``statutory_basis``,
    when the document does not define the phrase itself."""
    if doc is None or not text or _HOST_DEFINED_IN_TEXT.search(text):
        return {}
    try:
        meta = json.loads(doc["meta_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    basis = (meta.get("statutory_basis") or "").strip()
    if not basis:
        return {}
    from .extractor import grammar_citations
    # Resolve the recorded name the same way the text would have been read, so the
    # alias can only ever point where a citation of that name would have pointed.
    for c in grammar_citations(basis):
        if c.candidate_id and (c.entity_kind or "") in ("act", "regulation"):
            return {"the Act": c.candidate_id}
    return {}


def aliases_for_document(doc, aliases: dict[str, str] | None,
                         text: str | None = None) -> dict[str, str] | None:
    """The corpus-wide shorthand rules, plus what this document's SOURCE guarantees, plus
    the conventional abbreviations its own text has earned by naming the Act in full,
    plus the parent Act a code of practice records in its metadata.

    All of these are per-document and must stay that way: a bulk rescan mixes sources
    and subjects in one pool, so an alias map built once for the run would carry one
    judgment's certainties into the next.
    """
    extra: dict[str, str] = {}
    extra.update(_SOURCE_ALIASES.get((doc["source"] or "") if doc is not None else "", {}))
    extra.update(_statutory_basis_alias(doc, text))
    if text:
        lowered = text.lower()
        for full_name, target, abbreviations in _UNLOCKED_BY_FULL_NAME:
            if full_name in lowered:
                extra.update(dict.fromkeys(abbreviations, target))
    if not extra:
        return aliases
    # The document's own source wins: inside an IPT judgment "IPA" is the 2016 Act
    # whether or not the full name happens to appear.
    return {**(aliases or {}), **extra,
            **_SOURCE_ALIASES.get((doc["source"] or "") if doc is not None else "", {})}

# UK domestic legislation, by the shape of its identifier. Gating on the TARGET rather
# than on the grammar that produced it is what makes the guard below complete: a UK act
# can also arrive by a learned shorthand ("the 2018 Act"), a corpus-wide named alias, or
# a bare "section 45" carried forward, none of which carry a uk_* method name.
_UK_LEGISLATION_ID_RE = re.compile(
    r"^(?:ukpga|ukla|ukcm|uksi|ukmo|ukci|asp|ssi|anaw|asc|wsi|nia|nisr|apni|aosp|aep|mnia)/",
    re.IGNORECASE,
)
# The one way a non-UK document may name UK legislation and be believed: it gave the
# legislation.gov.uk URI, which is an identifier, not a name that happens to collide.
_UK_EXPLICIT_METHODS = {"uk_legislation_uri"}


def _is_uk_legislation_id(candidate_id: str | None) -> bool:
    return bool(candidate_id and _UK_LEGISLATION_ID_RE.match(candidate_id))


def _gate_domestic_statute_names(doc, cites: list) -> list:
    """Drop UK domestic-legislation candidates from hosts that cannot mean them.

    Applied TWICE per document, and it has to be. The corpus-wide shorthand store is
    consulted after the first pass, and it re-attached exactly what the guard had just
    removed: "the 2018 Act", learned from an English judgment, bound ukpga/2018/12 into
    487 Irish judgments in a 4,000-row sample, and the bare "s. 50A" pinpoints then
    carried forward off it. A guard that runs once is a guard the next stage undoes.
    """
    if not (_is_irish_host(doc) or _is_eu_guidance(doc)):
        return cites
    return [
        replace(c, candidate_id=None)
        if (c.method in _UK_NAME_HEURISTICS
            or (_is_uk_legislation_id(c.candidate_id)
                and c.method not in _UK_EXPLICIT_METHODS))
        else c
        for c in cites
    ]


def _is_irish_host(doc) -> bool:
    """Is this document IRISH — by any route, not just an Irish court?

    Inside one, an "<X> Act 1963" name is almost always an Act of the Oireachtas, so the
    UK statute-name heuristics must not link it to UK legislation (EU instruments and
    case citations of any jurisdiction are unaffected — those are fine cross-border).

    Keyed on the SOURCE PREFIX, not a list of sources. Naming ``ie-caselaw`` alone left
    every Irish regulator out of the gate: a Data Protection Commission inquiry citing
    "section 133 of the Data Protection Act 2018" — the Oireachtas Act, in an Irish
    statutory inquiry — linked to the UK Act instead, and so did ie-ccpc-mergers,
    ie-revenue-tdm, ie-tax-appeals and ie-dpc-guidance. Six sources missed because the
    gate enumerated one.
    """
    from .courts import IRISH_COURTS

    source = str(doc["source"] or "").lower()
    court = (doc["court"] or "").lower()
    stable_id = str(doc["stable_id"] or "").lower()
    prefix = stable_id.split("/", 1)[0]
    return (source.startswith("ie-") or stable_id.startswith("ie/")
            or court in IRISH_COURTS or prefix in IRISH_COURTS)


# Kept as the old name for any caller that still uses it.
_is_irish_case = _is_irish_host


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
#   - READ  — the applicable store is loaded once per process and cached (the ≥3-document
#             gate keeps it small), so application costs zero queries.
#   - WRITE — insert-only, and pre-filtered against a process-local set of SETTLED pairs
#             (already over the threshold, so nothing more can be learned about them).
#             That is the hot set — "GDPR" settles after three documents and the other
#             700k never write for it again — while a pair still short of the threshold
#             does write, because that write is how the third document gets counted.
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
        self._settled: set[tuple[str, str]] = set()

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
            self._settled |= {(n, c) for c, rows in by_cand.items() for n, _k, _a in rows}
            self._loaded_at = time.monotonic()
            self._loaded = True
            return by_cand, by_name

    def unseen(self, defs: list[dict]) -> list[dict]:
        """The definitions still worth writing — those whose pair has NOT yet reached the
        popularity threshold. A settled pair can learn nothing from another document, so
        skipping it is what keeps a steady-state rescan off the write path for exactly
        the names every document defines."""
        with self._lock:
            return [d for d in defs
                    if (d["shorthand"], d["candidate_id"]) not in self._settled]

    def note_settled(self, pairs: list[tuple[str, str]]) -> None:
        """Record pairs that have just reached the threshold — stop writing for them.

        Deliberately NOT folded into the live map: a pair becomes applicable at the next
        reload (≤15 minutes), not the instant it crosses. Applying it immediately would
        mean the rest of a rescan sees a different rule set than a re-run of the same
        documents would, and the whole point of the threshold is that the corpus, not one
        run's ordering, decides."""
        with self._lock:
            self._settled |= set(pairs)


_SHORTHANDS = _ShorthandStore()


def reset_shorthand_cache() -> None:
    """Forget the cached store — for tests, which build several corpora in one process,
    and for a long-lived server that should pick up a rebuilt table promptly."""
    global _SHORTHANDS
    _SHORTHANDS = _ShorthandStore()


# --- whose law is it? -------------------------------------------------------
# Supranational systems every national court cites in its own right. These are NEVER
# excluded by the jurisdiction gate below, and that exception is the whole reason the
# gate is safe: "the ECHR" is held against BOTH echr/convention and ukpga/1998/42, and
# in a UK judgment it means the Convention — so a rule that simply preferred the
# document's own jurisdiction would confidently mint the Human Rights Act. Leaving the
# supranational owner in keeps such a name contested, and contested names don't travel.
_SUPRANATIONAL = {"EU", "COE"}
_CELEX_ID_RE = re.compile(r"^[0-9]{5}[A-Z]{1,2}[0-9]{4}$")


def _candidate_jurisdiction(candidate_id: str | None) -> str | None:
    """The legal system a candidate belongs to, or None when it can't be told.

    None is not "no jurisdiction" — it is "unknown", and the gate treats it as
    inexcludable, so an unrecognised id keeps a name contested rather than resolving it.
    """
    cid = (candidate_id or "").strip()
    if not cid:
        return None
    if _CELEX_ID_RE.match(cid) or cid.lower().startswith(("european/", "celex/")) \
            or cid.upper().startswith("ECLI:EU:"):
        return "EU"
    if cid.lower().startswith("echr/") or cid.upper().startswith("ECLI:CE:ECHR"):
        return "COE"
    if _is_uk_legislation_id(cid):
        return "GB"
    if "/" not in cid:
        return None
    head = cid.split("/", 1)[0].lower()
    if head in ("ca", "us", "au", "nz", "ie", "sg", "hk", "in", "za"):
        return head.upper()
    # The court registry only, and only for a genuinely COURT-SHAPED head. ``classify``
    # falls back to prefix matching, which happily reads "mystery-source-id" as Malaysia
    # — and a wrong jurisdiction here is worse than none, because it lets the gate
    # EXCLUDE an owner and so resolve a name it should have left contested.
    if not re.fullmatch(r"[a-z]{2,12}", head):
        return None
    from .courts import classify

    court = classify(head)
    return court.jurisdiction if court else None


def _host_jurisdiction(doc) -> str | None:
    """The citing document's own system, read from its identifier and then its source."""
    if doc is None:
        return None
    try:
        own = _candidate_jurisdiction(doc["stable_id"])
    except (KeyError, TypeError):
        own = None
    if own:
        return own
    src = (doc["source"] or "").lower() if doc is not None else ""
    for code in ("uk", "ie", "ca", "au", "nz", "sg", "hk", "in", "za", "us"):
        if src.startswith(f"{code}-"):
            return "GB" if code == "uk" else code.upper()
    return None


def _stored_shorthands_for(catalogue: Catalogue, cites: list, doc=None) -> list[tuple]:
    """Stored shorthands applicable to this document: those whose parent candidate the
    document ALREADY cites, and about which the store is UNANIMOUS.

    Ambiguity guard — a shorthand registered against more than one candidate is never
    applied. The rule used to be weaker: a contested name applied whenever exactly one
    of its candidates was cited here, on the reasoning that the document had then
    disambiguated it. It hadn't. A document citing act X says nothing about what an
    abbreviation it never defines means, so the test fires on coincidence — and the
    store is contested precisely where it has mislearned. Measured on the live corpus:
    "PACE" was held against six different acts (1967, 1987, 1988, 2000, 2016 and an SI),
    not one of them the Police and Criminal Evidence Act 1984, and in KJF v Surrey
    Police — which never spells PACE out — "s. 8(1) of PACE" was recorded as a citation
    of RIPA s.8(1), because RIPA was the one owner that judgment happened to cite. That
    edge then propagates through provision mappings into "predecessor case law" on the
    successor Act. A missing edge is recoverable; a confident wrong one is not.

    Where an abbreviation genuinely has two referents that matter (DPA 1998 / DPA 2018,
    RIPA / IPA), the deterministic route is ``_UNLOCKED_BY_FULL_NAME`` above, which
    requires the document to name the Act in full — evidence, rather than coincidence.
    A document that DEFINES its own shorthand never needed the store: that is the
    in-document pass, which runs first and wins the span."""
    cited = {c.candidate_id for c in cites if c.candidate_id}
    if not cited:
        return []
    by_cand, by_name = _SHORTHANDS.load(catalogue)
    if not by_cand:
        return []
    host = _host_jurisdiction(doc)
    out: list[tuple] = []
    for cid in cited:
        for name, kind, abbrev in by_cand.get(cid, ()):
            owners = by_name.get(name) or {cid}
            if len(owners) > 1 and not _resolved_by_jurisdiction(owners, cid, host):
                continue
            out.append((name, cid, kind, abbrev))
    return out


def _resolved_by_jurisdiction(owners: set[str], cid: str, host: str | None) -> bool:
    """Whether the citing document's own legal system settles a contested name.

    This is the ONE disambiguator admitted after the coincidence test was removed,
    because unlike "the document happens to cite this one" it is evidence independent
    of the citation: a UK judgment writing "Human Rights Act" cannot mean the Canadian
    Human Rights Act, whoever else it cites. Only FOREIGN DOMESTIC owners are excluded
    — supranational ones stay (see ``_SUPRANATIONAL``), as do owners whose system can't
    be identified, so anything the gate cannot positively rule out keeps the name
    contested. Measured on the live corpus: 549 of 6,457 contested names become usable
    in a UK document and 881 in a Canadian one, while "the ECHR", "ECtHR" and all six
    all-UK owners of "PACE" stay withheld.
    """
    if not host:
        return False
    plausible = {
        o for o in owners
        if (j := _candidate_jurisdiction(o)) is None or j in _SUPRANATIONAL or j == host
    }
    return plausible == {cid}


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
    aliases = aliases_for_document(doc, aliases, text)
    if llm is None:
        guarded = _GUARD.extract(text, aliases, _home_of(doc))
        cites, raw_defs = guarded if guarded is not None else (None, [])
        if cites is None:
            # budget blown: keep whatever rows a previous run left, stamp so
            # staleness-scoped reruns converge instead of re-hitting the doc
            log.warning("[cite-extract] %s: grammar pass exceeded %.0fs budget "
                        "(%.1f MB) — skipped",
                        stable_id, _GUARD.budget_for(text), len(text or "") / 1e6)
            catalogue.mark_extracted(stable_id, run_id=run_id)
            return 0
    else:  # the llm extractor is not picklable (and may call the network) — unguarded
        raw_defs = []
        home_id, home_kind = _home_of(doc)
        cites = extract_citations(text, llm=llm, aliases=aliases, defs_out=raw_defs,
                                  home_id=home_id, home_kind=home_kind)

    return _finish_document(catalogue, doc, text, cites, raw_defs,
                            stable_id=stable_id, run_id=run_id)


def _finish_document(catalogue: Catalogue, doc, text: str, cites, raw_defs,
                     *, stable_id: str, run_id: str | None = None,
                     commit: bool = True) -> int:
    """Everything after the grammar pass: the jurisdiction guards, shorthand store,
    suppression veto, and the citation/edge writes. Split out of extract_document so
    the parallel bulk path can run the (CPU-bound, picklable) grammar pass in a pool
    of worker processes and feed the results through here in the parent — the guards
    need catalogue lookups and the writes need the one shared connection, so this
    half stays serial by design. ``commit=False`` lets a bulk caller batch many
    documents into one transaction (the run is restartable off the
    ``last_extracted_at`` stamp, so per-document durability buys nothing there)."""
    cites = _guard_cites(catalogue, doc, cites, stable_id=stable_id)
    plan = _shorthand_plan(catalogue, cites, raw_defs, doc)
    if plan is not None:
        from .extractor import attach_stored_shorthands

        stored, exclude, defs = plan
        if stored:
            # an in-document definition always beats a stored one, so exclude the names
            # this document defines for itself (already linked by the extractor's pass)
            cites = attach_stored_shorthands(text, cites, stored, exclude=exclude)
            # Re-gate: the store is corpus-wide, so it will happily bind a UK act into
            # an Irish judgment under a name both jurisdictions use.
            cites = _gate_domestic_statute_names(doc, cites)
        _learn_fresh_shorthands(catalogue, defs, stable_id)
    return _finish_writes(catalogue, doc, text, cites, stable_id=stable_id,
                          run_id=run_id, commit=commit)


def _guard_cites(catalogue: Catalogue, doc, cites: list, *, stable_id: str) -> list:
    """Every jurisdiction/precision guard that runs BEFORE the shorthand store.

    Pure list work plus two narrow catalogue lookups, so it is cheap enough to stay in
    the parent on the parallel path — unlike the shorthand scan that follows it.
    """
    if not _allows_us_reporters(doc):
        cites = [c for c in cites if not c.method.startswith("us_reporter")]
    elif not _is_us_source(doc):
        cites = [c for c in cites if c.method != AMBIGUOUS_METHOD]

    # Inside LEGISLATION, a bare "Article 3" / "paragraph 2" is almost always the
    # instrument referring to ITSELF, not to the directive it last named. The
    # extractor now resolves that directly, given the document's own id (see
    # ``_home_of`` and ``_attach_carry_forward``), so the guesses no longer need
    # dropping wholesale — a self-reference links home and a same-sentence
    # cross-reference links out. The blanket drop remains only for a legislative
    # document whose own id isn't in citable form, where there is no home to
    # attribute to and the heuristic is back to guessing.
    if doc["doc_type"] == str(DocType.LEGISLATION) and not _home_of(doc)[0]:
        cites = [c for c in cites if c.method != "carry_forward"]
    if _meta_of(doc).get("disable_carry_forward"):
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
        # A bare Schedule/Annex in a judgment very commonly names an evidential bundle,
        # order or quoted document.  Carrying it onto the last statute creates impossible
        # pinpoints (CPR Part 8 and the GDPR do not acquire Schedules 1–5 merely because
        # they were the last law named).  Explicit ``Schedule N to/of <Act>`` citations
        # are recognised by the literal grammars and are unaffected.
        cites = [c for c in cites if not (
            c.method == "carry_forward"
            and c.raw.lower().lstrip().startswith(("schedule", "sched", "sch.", "annex"))
        )]

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
    # Ireland re-enacted much of its statute book under names the UK also uses, and the
    # 2018 Data Protection Acts are the worst case: both are "Data Protection Act 2018",
    # both commenced in May 2018, both implement the GDPR. An Irish judgment saying
    # "section 117 of the Data Protection Act 2018" means the Oireachtas Act every time,
    # so ANY route to UK domestic legislation is refused here, not just the two statute-
    # name grammars — a learned shorthand, a corpus-wide alias and a carried-forward
    # "section 45" all reached ukpga/2018/12 without ever carrying a uk_* method name.
    # An explicit legislation.gov.uk URI still resolves: that is an identifier, and an
    # Irish court citing one means it.
    # …and the EU guidance guard (EDPB / A29WP / OSS decisions): an EU-level document must
    # not link a *domestic* statute by NAME (cross-jurisdiction collision), but its EU-law
    # (CELEX), CJEU/ECHR (ECLI) and English/Irish case-law (neutral-citation) links are
    # all unambiguous and kept. Domestic (ICO etc.) guidance is deliberately NOT gated —
    # there a "Data Protection Act 2018" reference IS to the national statute.
    cites = _gate_domestic_statute_names(doc, cites)

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
        # In German judgments ``S. 100`` means Seite 100, not section 100.
        if (doc["source"].startswith("de-") and c.method == "carry_forward"
                and re.match(r"(?i)^S\.?\s*\d", c.raw or "")):
            continue
        if not (c.candidate_id or "").startswith("de/gesetz/"):
            filtered.append(c)
            continue
        known = de_known.get(c.candidate_id)
        if known is None:
            known = catalogue.find_document_id(c.candidate_id) is not None
            de_known[c.candidate_id] = known
        if known:
            filtered.append(c)
    # Canonicalise the narrow UKUT AAC/ACC spelling and legacy zero-padding variants
    # while the catalogue is available.  The set-based resolver intentionally handles
    # exact identifiers and aliases only; leaving ``ukut/acc/2014/310`` on the edge
    # would therefore stay pending even though ``ukut/aac/2014/0310`` is held.
    if catalogue is not None:
        ukut = list(dict.fromkeys(
            c.candidate_id for c in filtered
            if c.candidate_id and re.fullmatch(
                r"ukut/(?:aac|acc)/\d{4}/\d+", c.candidate_id, re.IGNORECASE)
        ))
        canonical = catalogue.find_existing(ukut) if ukut else {}
        if canonical:
            filtered = [replace(c, candidate_id=canonical.get(c.candidate_id, c.candidate_id))
                        for c in filtered]
    return filtered


def _shorthand_plan(catalogue: Catalogue, cites: list, raw_defs: list, doc=None):
    """``(stored, exclude, defs)`` for the corpus-wide shorthand pass, or None.

    Split out of :func:`_finish_document` so the parallel path can compute it in the
    parent (it needs the catalogue-backed store, but only does cached dict lookups)
    and then hand the *expensive* half — the full-text scan in
    ``attach_stored_shorthands`` — back to the worker that still holds the text.
    """
    if not (_shorthands_enabled() and any(c.candidate_id for c in cites)):
        return None
    # The definitions come from the extractor, but the jurisdiction guards ran AFTER it
    # and may have stripped a candidate (a UK statute name inside a CJEU judgment). Keep
    # only definitions whose target survived, or the store would learn precisely the
    # links those guards exist to prevent.
    live = {c.candidate_id for c in cites if c.candidate_id}
    defs = [d for d in raw_defs if d["candidate_id"] in live]
    stored = _stored_shorthands_for(catalogue, cites, doc)
    return stored, {d["shorthand"] for d in defs}, defs


def _learn_fresh_shorthands(catalogue: Catalogue, defs: list, stable_id: str) -> None:
    """Harvest the shorthands this document defines, for the next one to use."""
    fresh = _SHORTHANDS.unseen(defs)
    if not fresh:
        return
    try:
        result = catalogue.add_learned_shorthands(fresh, doc_id=stable_id)
        _SHORTHANDS.note_settled(result.get("settled") or [])
    except Exception as exc:  # noqa: BLE001 — learning is best-effort
        log.debug("[cite-extract] %s: shorthand store write failed: %s", stable_id, exc)


def _finish_writes(catalogue: Catalogue, doc, text: str, cites, *, stable_id: str,
                   run_id: str | None = None, commit: bool = True) -> int:
    """The DB half: metadata, suppression veto, idempotent clears, citations + edges.

    Needs the one shared connection, so it stays in the parent on the parallel path.
    Measured at a few milliseconds a document — it is not the bottleneck it looks like.
    """
    # The bench and counsel are printed on a judgment's first page and nowhere in its
    # metadata, so lift them while we already have the text in hand (the reader shows them
    # under the title). Only for cases, only when not already known, and never a guess —
    # parse_intituling returns nothing rather than something wrong.
    if doc["doc_type"] in _CASE_DOC_TYPES:
        try:
            meta_now = catalogue.document_meta(stable_id)
            if not meta_now.get("coram"):
                found = parse_intituling(text)
                if found:
                    catalogue.set_document_meta(stable_id, {**meta_now, **found},
                                                commit=commit)
        except Exception as exc:  # noqa: BLE001 — metadata is a bonus, never the job
            log.debug("[cite-extract] %s: intituling parse failed: %s", stable_id, exc)

    # respect human corrections: drop citations the user has rejected (§1.3a). The
    # suppressed edges are manual, so they survive the clear below and keep their veto.
    sup_ids, sup_raws = catalogue.suppressed_targets(stable_id)
    if sup_ids or sup_raws:
        cites = [c for c in cites if c.candidate_id not in sup_ids and c.raw not in sup_raws]

    # idempotent re-run: clear this source's prior observations + machine edges
    # (both literal-regex and the heuristic carry-forward 'inferred' edges)
    catalogue.clear_citations(stable_id, commit=commit)
    catalogue.clear_relations(stable_id, extracted_via=str(ExtractedVia.REGEX),
                              commit=commit)
    catalogue.clear_relations(stable_id, extracted_via=str(ExtractedVia.INFERRED),
                              relationship_type=str(RelationshipType.MENTIONS),
                              commit=commit)
    catalogue.clear_relations_of_type(
        stable_id, str(RelationshipType.APPLICABLE_VERSION), commit=commit,
    )

    catalogue.add_citations(stable_id, [
        {
            "raw": c.raw, "entity_kind": c.entity_kind, "candidate_id": c.candidate_id,
            "pinpoint": c.pinpoint, "char_start": c.char_start, "char_end": c.char_end,
            "method": c.method, "confidence": c.confidence,
        }
        for c in cites
    ], commit=commit)

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
    edges = _attach_applicable_versions(catalogue, doc, edges)
    catalogue.add_relations(stable_id, list(edges.values()), commit=commit)
    # durable "last rescanned at" stamp — set even when the document cited nothing, so a
    # staleness-scoped rescan can skip it next time (§5).
    catalogue.mark_extracted(stable_id, run_id=run_id, commit=commit)
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
