"""Catalogue repository — the relational spine over the corpus.

The production spine is PostgreSQL (schema/postgres.sql, §7). This module is the
portable backend used for local/dev/test: it speaks the same table shapes against
stdlib ``sqlite3`` so the pipeline runs with zero external services. The method
surface — not the SQL dialect — is the contract the pipeline depends on, so a
psycopg-backed ``Catalogue`` is a drop-in later.

It implements the append-only discipline (§1.4a): documents are upserted and
disappearance is recorded as an ``upstream_status`` change, never a row deletion.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..core.models import Record, TypedRelation, UpstreamStatus
from . import _postgres

# SQLite-flavoured mirror of schema/postgres.sql (step-1 + tagging tables).
_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    stable_id        TEXT PRIMARY KEY,
    ecli             TEXT,
    source           TEXT NOT NULL,
    doc_type         TEXT NOT NULL,
    title            TEXT,
    court            TEXT,
    decision_date    TEXT,
    language         TEXT,
    source_language  TEXT,
    version          INTEGER NOT NULL DEFAULT 1,
    is_latest        INTEGER NOT NULL DEFAULT 1,
    landing_url      TEXT,
    raw_path         TEXT,
    text_path        TEXT,
    meta_path        TEXT,
    meta_json        TEXT,    -- adapter-supplied metadata bag (record.extra), as JSON
    payload_hash     TEXT,
    has_text         INTEGER NOT NULL DEFAULT 0,
    has_embedding    INTEGER NOT NULL DEFAULT 0,
    extracted_via    TEXT,
    added_by         TEXT NOT NULL DEFAULT 'harvest',
    topic_tags       TEXT NOT NULL DEFAULT '[]',
    topic_score      REAL,
    upstream_status  TEXT NOT NULL DEFAULT 'live',
    upstream_status_at TEXT,
    last_extracted_at TEXT,
    last_extraction_run_id TEXT,
    fetched_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_source_idx ON documents (source);
CREATE INDEX IF NOT EXISTS documents_ecli_idx ON documents (ecli);
CREATE INDEX IF NOT EXISTS documents_payload_hash_idx ON documents (payload_hash);
CREATE INDEX IF NOT EXISTS documents_landing_url_idx ON documents (landing_url);

CREATE TABLE IF NOT EXISTS relations (
    relation_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id             TEXT NOT NULL,
    dst_id             TEXT,
    raw_citation_string TEXT,
    -- The canonical id this edge points AT, normalised once at write time (§5b): the
    -- adapter's dst_id, else the matcher ladder over raw_citation_string, collapsed to
    -- Act level. Resolution, the hanging-reference worklist and the coverage aggregates
    -- all key off this, so they are indexed SQL rather than a regex ladder re-run over
    -- millions of edges on every read. NULL = recognised by name only, no identifier.
    candidate_id       TEXT,
    -- The case/accent-folded raw string — the join key for named aliases ("UK GDPR").
    raw_fold           TEXT,
    resolution_status  TEXT NOT NULL DEFAULT 'pending',
    relationship_type  TEXT NOT NULL DEFAULT 'mentions',
    extracted_via      TEXT NOT NULL DEFAULT 'structured',
    context_chunk_id   TEXT,
    -- pinpoint anchors (§1.9): which part of the source (e.g. a handbook's
    -- "pp. 45-47") relates to which part of the target (e.g. "Article 17") —
    -- the JuriConnect-style fragment link.
    src_anchor         TEXT,
    dst_anchor         TEXT,
    -- representative char span of the citation in the source text, so a later
    -- pass can read the surrounding prose and classify the *treatment* (§1.3a):
    -- mentions → follows / distinguishes / overrules / applies / considers.
    context_start      INTEGER,
    context_end        INTEGER
);
CREATE INDEX IF NOT EXISTS relations_src_idx ON relations (src_id);
CREATE INDEX IF NOT EXISTS relations_dst_idx ON relations (dst_id);
CREATE INDEX IF NOT EXISTS idx_relations_status ON relations (resolution_status);

-- Rolled-up citation frequencies (the substrate for the §5a snowball). Aggregating the
-- 10M-row `citations` table live costs ~13s, so it is rebuilt on a cadence instead.
-- No PK: entity_kind is nullable (an unclassified candidate), and the table is
-- rebuilt wholesale rather than upserted into.
CREATE TABLE IF NOT EXISTS citation_counts (
    candidate_id  TEXT NOT NULL,
    entity_kind   TEXT,
    method        TEXT,
    sample        TEXT,
    occurrences   INTEGER NOT NULL DEFAULT 0,
    documents     INTEGER NOT NULL DEFAULT 0,
    rebuilt_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS citation_counts_occ_idx ON citation_counts (occurrences DESC);
CREATE INDEX IF NOT EXISTS citation_counts_cand_idx ON citation_counts (candidate_id);

-- Per-source resolved-outgoing-edge roll-up. The Explore homepage's citation-density
-- figure used to be a live relations×documents GROUP BY on every cache refresh —
-- minutes of IO at 17M+ edges. Rebuilt alongside citation_counts on the same cadence.
CREATE TABLE IF NOT EXISTS source_stats (
    source            TEXT PRIMARY KEY,
    resolved_outgoing INTEGER NOT NULL DEFAULT 0,
    rebuilt_at        TEXT NOT NULL
);

-- The Explore homepage's base aggregate: documents by source/type/court/year with
-- text+embedding coverage. Two live full-table scans (46s + 32s cold at 4.9M docs)
-- ran inside every cache warm; the courts facet derives from these same rows.
CREATE TABLE IF NOT EXISTS corpus_shape_stats (
    source     TEXT NOT NULL,
    doc_type   TEXT NOT NULL,
    court      TEXT,
    yr         TEXT,
    n          INTEGER NOT NULL DEFAULT 0,
    with_text  INTEGER NOT NULL DEFAULT 0,
    embedded   INTEGER NOT NULL DEFAULT 0,
    rebuilt_at TEXT NOT NULL
);

-- Legislation-type rail roll-up (the Explore drill's Primary/Secondary/... split).
-- Classification is a per-document Python pass; at 1.9M legislation rows it took
-- ~6 minutes inside every homepage cache warm. Rebuilt with citation_counts.
CREATE TABLE IF NOT EXISTS leg_type_stats (
    source       TEXT NOT NULL,
    label        TEXT NOT NULL,
    n            INTEGER NOT NULL DEFAULT 0,
    years_json   TEXT NOT NULL DEFAULT '{}',
    filters_json TEXT NOT NULL DEFAULT '[]',
    rebuilt_at   TEXT NOT NULL,
    PRIMARY KEY (source, label)
);

-- Per-document citation-network statistics (PageRank over the resolved mentions
-- graph — treatment types deliberately NOT weighted, they aren't reliable yet).
-- Rebuilt wholesale by rebuild_authority() on a cadence, like citation_counts.
CREATE TABLE IF NOT EXISTS doc_authority (
    doc_id           TEXT PRIMARY KEY,
    pagerank         REAL NOT NULL DEFAULT 0,
    pagerank_decayed REAL NOT NULL DEFAULT 0,   -- citing-doc age discounted (half-life)
    percentile       REAL,                      -- 0..100 among cited documents
    in_degree        INTEGER NOT NULL DEFAULT 0,
    out_degree       INTEGER NOT NULL DEFAULT 0,
    rebuilt_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS doc_authority_pr_idx ON doc_authority (pagerank DESC);

-- Extracted citations (§5): the raw *observations* (one per occurrence) with
-- entity kind, candidate, pinpoint, char span (the context window for treatment
-- classification §1.3a), method + confidence. These feed the `relations` graph —
-- many citations of the same target collapse to one deduped edge — but are kept
-- as the auditable extraction record (re-derivable projection, §1.2).
CREATE TABLE IF NOT EXISTS citations (
    citation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id        TEXT NOT NULL,
    raw           TEXT NOT NULL,
    entity_kind   TEXT,
    candidate_id  TEXT,
    pinpoint      TEXT,
    char_start    INTEGER,
    char_end      INTEGER,
    method        TEXT,
    confidence    REAL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS citations_src_idx ON citations (src_id);

CREATE TABLE IF NOT EXISTS citation_aliases (
    alias    TEXT PRIMARY KEY,
    dst_id   TEXT NOT NULL,
    source   TEXT
);
-- "the aliases OF this document" — resolve_pending_for and the cited-by alias sweep
-- probe by dst_id, and at 5M alias rows the missing index was a full scan per
-- just-harvested document (40s per doc inside the bulk resolve phase).
CREATE INDEX IF NOT EXISTS citation_aliases_dst_idx ON citation_aliases (dst_id);

-- Shorthands LEARNED from one document and applied in others ("[Suncor]" defined in
-- one judgment, used bare in the next). Deliberately NOT `citation_aliases`: that map
-- is unconditional, applied to every document, which is exactly wrong here — a stored
-- "FCA" must only link inside a document that already cites the Federal Courts Act by
-- some other means. The gates live in citations/stage.py; this is just the store.
--
-- No occurrence counter: a per-document UPDATE of a hot row (every judgment defines
-- "GDPR") would serialise the parallel rescan workers against each other on a single
-- tuple. Rows are written once, ever (INSERT … ON CONFLICT DO NOTHING), so the write
-- path stays contention-free.
CREATE TABLE IF NOT EXISTS learned_shorthands (
    shorthand    TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    entity_kind  TEXT,
    is_abbrev    INTEGER NOT NULL DEFAULT 0,
    first_doc    TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (shorthand, candidate_id)
);
CREATE INDEX IF NOT EXISTS learned_shorthands_cand_idx ON learned_shorthands (candidate_id);

-- Version history (§1 principle 4): a document is a *series of versions*; the
-- catalogue points at "latest" (the documents row) but retains all. When upstream
-- content changes (payload_hash differs), the prior version is archived here
-- before the documents row advances — raw bytes + text are content-addressed and
-- immutable, so the old pointers stay valid.
CREATE TABLE IF NOT EXISTS document_versions (
    stable_id     TEXT NOT NULL,
    version       INTEGER NOT NULL,
    payload_hash  TEXT,
    raw_path      TEXT,
    text_path     TEXT,
    title         TEXT,
    decision_date TEXT,
    extracted_via TEXT,
    archived_at   TEXT NOT NULL,
    PRIMARY KEY (stable_id, version)
);

-- Files attached to any document (§1.9, Appendix B): a commentary PDF, an
-- annotated copy, a scanned exhibit, your own notes, an LLM summary. added_by
-- keeps human/machine material separable (§10).
CREATE TABLE IF NOT EXISTS document_assets (
    asset_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       TEXT NOT NULL,
    kind         TEXT NOT NULL,   -- commentary|annotation|note|summary|exhibit
    path         TEXT,
    mime         TEXT,
    payload_hash TEXT,
    added_by     TEXT NOT NULL DEFAULT 'user',
    title        TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS document_assets_doc_idx ON document_assets (doc_id);

CREATE TABLE IF NOT EXISTS tag_rules (
    rule_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tag                TEXT NOT NULL,
    condition_tree_json TEXT NOT NULL,
    scope_json         TEXT NOT NULL DEFAULT '{}',
    enabled            INTEGER NOT NULL DEFAULT 1,
    priority           INTEGER NOT NULL DEFAULT 0,
    version            INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    note               TEXT
);

CREATE TABLE IF NOT EXISTS document_tags (
    doc_id             TEXT NOT NULL,
    tag                TEXT NOT NULL,
    assigned_by_rule_id INTEGER,
    rule_version       INTEGER,
    method             TEXT NOT NULL,
    confidence         REAL,
    assigned_at        TEXT NOT NULL,
    PRIMARY KEY (doc_id, tag, method)
);

CREATE TABLE IF NOT EXISTS rule_runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id        INTEGER NOT NULL,
    rule_version   INTEGER NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    docs_evaluated INTEGER NOT NULL DEFAULT 0,
    docs_matched   INTEGER NOT NULL DEFAULT 0,
    scope_json     TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS sources (
    key                  TEXT PRIMARY KEY,
    last_run             TEXT,
    watermark            TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_yield_at        TEXT,
    requires_js          INTEGER NOT NULL DEFAULT 0,
    requires_proxy       INTEGER NOT NULL DEFAULT 0
);

-- Saved harvest plans (§5a) — a watch defines a seed (source + keywords, or a
-- seed rule like "docs citing the GDPR") and how many degrees to autosnowball,
-- run on a cadence by the scheduler. spec_json holds the full WatchSpec.
CREATE TABLE IF NOT EXISTS watches (
    watch_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    spec_json        TEXT NOT NULL,
    cadence_minutes  INTEGER NOT NULL DEFAULT 1440,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_run_at      TEXT,
    last_result_json TEXT,
    created_at       TEXT NOT NULL
);

-- Enrichment misses — keys (e.g. a CELEX) whose external lookup (e.g. the EUR-Lex
-- title webservice) returned nothing, so the scheduled backfill skips them instead
-- of burning daily quota retrying. Generic over enrichment ``kind``.
CREATE TABLE IF NOT EXISTS enrichment_misses (
    kind         TEXT NOT NULL,
    key          TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

-- Outstanding-effects re-check queue (§0). A piece of legislation lands here only
-- when its XML carried unapplied amendments (the editorial lag) — so the scheduler
-- re-pulls *only* instruments it suspects are stale, never the whole corpus, and on a
-- slow, backing-off cadence (weeks). When a re-pull shows zero outstanding effects the
-- row is deleted (the amendments have been incorporated). `checks` drives the backoff;
-- `affecting` is the JSON list of amending instruments (also minted as amended_by edges).
CREATE TABLE IF NOT EXISTS effects_refresh (
    stable_id     TEXT PRIMARY KEY,
    outstanding   INTEGER NOT NULL DEFAULT 0,
    affecting     TEXT,
    checks        INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_checked  TEXT,
    next_check_at TEXT NOT NULL
);

-- Embeddings (§6b/§6d). pgvector in production (§7); here vectors are JSON for a
-- portable brute-force cosine. provider/model/model_version/dimensions = the
-- "family"; vectors are ONLY comparable within one family, so a model swap is a
-- NEW family, never an overwrite. char_start/end map a chunk back into text.txt.
CREATE TABLE IF NOT EXISTS embeddings (
    doc_id          TEXT NOT NULL,
    chunk_id        INTEGER NOT NULL,
    vector          TEXT NOT NULL,
    chunk_text      TEXT NOT NULL,
    structural_unit TEXT,
    source_language TEXT,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    dimensions      INTEGER NOT NULL,
    char_start      INTEGER,
    char_end        INTEGER,
    PRIMARY KEY (doc_id, chunk_id, provider, model, model_version)
);
CREATE INDEX IF NOT EXISTS embeddings_family_idx
    ON embeddings (provider, model, model_version);

-- Background jobs (§8). In-process dicts died with the process, so a deploy erased a
-- running harvest's history and the scheduler's own work was invisible to the UI.
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    label         TEXT NOT NULL,
    params_json   TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'running',
    progress_json TEXT NOT NULL DEFAULT '{}',
    log_json      TEXT NOT NULL DEFAULT '[]',
    result_json   TEXT,
    origin        TEXT NOT NULL DEFAULT 'api',
    cancel        INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL,
    heartbeat_at  TEXT,
    finished_at   TEXT,
    root_job_id   TEXT,
    resumed_from  TEXT,
    resume_policy TEXT NOT NULL DEFAULT 'restart',
    attempt       INTEGER NOT NULL DEFAULT 1,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    restart_requested INTEGER NOT NULL DEFAULT 0,
    lease_heartbeat_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, started_at);

-- Human-confirmable resolution suggestions ("Possibly: …?" with tick/cross): sub-threshold
-- or ambiguous matches the automatic matchers refuse, surfaced for a person to decide.
-- ref is the worklist group key (candidate_id or raw_fold); rejected rows persist so a
-- re-run never re-suggests what a human already dismissed.
CREATE TABLE IF NOT EXISTS match_suggestions (
    ref            TEXT NOT NULL,
    suggested_id   TEXT NOT NULL,
    kind           TEXT NOT NULL,          -- case-name | legislation-nested | legislation-year | echr-name
    reason         TEXT,
    extracted_parties TEXT,                -- the auto-extracted case-name string(s), for audit
    context        TEXT,                   -- held title / neutral citation shown beside the tick
    held           INTEGER NOT NULL DEFAULT 1,  -- 0: gazetteer id not yet harvested
    score          REAL,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected
    created_at     TEXT NOT NULL,
    PRIMARY KEY (ref, suggested_id)
);
CREATE INDEX IF NOT EXISTS match_suggestions_status_idx ON match_suggestions (status);

-- Reader passages the user flagged as badly linked/refined ("flag for improved
-- refinement") — the raw material for a later LLM/engineering pass over linking logic.
CREATE TABLE IF NOT EXISTS refinement_flags (
    flag_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id         TEXT NOT NULL,
    anchor         TEXT,                   -- segment label the selection sits in
    selected_text  TEXT NOT NULL,
    context        TEXT,                   -- surrounding sentence(s)
    current_links  TEXT,                   -- JSON: citations/links overlapping the selection now
    note           TEXT,                   -- what the user says it SHOULD do
    status         TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    created_at     TEXT NOT NULL
);

-- FTS5 keyword index over chunk text — the lexical half of hybrid search (§6c).
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_text, doc_id UNINDEXED, chunk_id UNINDEXED, family UNINDEXED
);
"""

# Indexes created after the additive column migrations (they reference columns the
# original DDL didn't have). Partial on the pending slice: that's the only hot one —
# ~400k rows out of 6.6M, and it's what the resolver and the worklist scan.
_POST_MIGRATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS relations_pending_candidate_idx ON relations (candidate_id) "
    "WHERE resolution_status = 'pending'",
    "CREATE INDEX IF NOT EXISTS relations_pending_fold_idx ON relations (raw_fold) "
    "WHERE resolution_status = 'pending'",
    # The alias rung of every resolution pass compares lower(candidate_id) — an
    # expression the plain candidate_id index cannot serve, so each targeted
    # resolve_pending_for() probe degenerated into a scan of the ENTIRE pending set
    # (2-3s per call at 5.5M pending; the per-document bulk post-processing pathology).
    "CREATE INDEX IF NOT EXISTS relations_pending_candidate_lower_idx ON relations "
    "(lower(candidate_id)) WHERE resolution_status = 'pending'",
    # Serves the Corpus browser's ORDER BY decision_date DESC, stable_id LIMIT n
    # directly — without it every page load sorts the whole documents table. On a
    # large live table create it CONCURRENTLY by hand first; this statement then
    # no-ops (IF NOT EXISTS) instead of taking a write-blocking lock at startup.
    "CREATE INDEX IF NOT EXISTS documents_date_id_idx ON documents "
    "(decision_date DESC, stable_id)",
)


# DSNs whose schema this process has already ensured. Postgres DDL is idempotent but not
# free, and the catalogue is opened per request.
_PG_SCHEMA_READY: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_days_ago(days: int) -> str:
    """UTC ISO timestamp ``days`` in the past — the cutoff a staleness filter compares
    against. ISO-8601 strings sort lexicographically, so ``created_at >= cutoff`` works
    as a plain string comparison on both backends."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _family_key(provider: str, model: str, model_version: str) -> str:
    return f"{provider}/{model}/{model_version}"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _apply_filters(sql: str, params: list, filters: dict | None) -> tuple[str, list]:
    """Append a partition pre-filter (§6b.6) over the documents join: by source
    (jurisdiction), doc_type, topic tag, or minimum year — applied BEFORE both
    rankers so fusion runs over the relevant slice."""
    filters = filters or {}
    if filters.get("source"):
        vals = filters["source"]
        sql += f" AND d.source IN ({','.join('?' * len(vals))})"
        params.extend(vals)
    if filters.get("doc_type"):
        vals = filters["doc_type"]
        sql += f" AND d.doc_type IN ({','.join('?' * len(vals))})"
        params.extend(vals)
    if filters.get("year_from"):
        sql += " AND d.decision_date >= ?"
        params.append(f"{filters['year_from']}-01-01")
    if filters.get("tag"):
        sql += " AND EXISTS (SELECT 1 FROM document_tags t WHERE t.doc_id = d.stable_id AND t.tag = ?)"
        params.append(filters["tag"])
    return sql, params


def _isodate(value: date | None) -> str | None:
    return value.isoformat() if value else None


class Catalogue:
    """Relational spine over the corpus. Backend is chosen from the path/DSN: a
    ``postgresql://…`` DSN uses Postgres + pgvector + tsvector (the §7 production
    spine); anything else is the portable SQLite backend. The method surface is
    identical — only DDL, vector search, and FTS diverge by backend."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if _postgres.is_postgres_dsn(self.db_path):
            self.backend = "postgres"
            self.conn = _postgres.connect(self.db_path)
            # The catalogue is opened per request; running ~30 CREATE-IF-NOT-EXISTS
            # statements plus the migrations on every open is work no request should do.
            # The schema can only change when the process starts, so do it once.
            if self.db_path not in _PG_SCHEMA_READY:
                self.conn.executescript(_postgres.PG_DDL)
                self._migrate()
                _PG_SCHEMA_READY.add(self.db_path)
            # Iterative index scans (pgvector ≥ 0.8) so a partition pre-filter +
            # HNSW search doesn't under-return under heavy WHERE filtering (§7).
            try:
                self.conn.execute("SET hnsw.iterative_scan = relaxed_order")
            except Exception:
                pass
        else:
            self.backend = "sqlite"
            if self.db_path != ":memory:":
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.executescript(_DDL)
            self._migrate()
        self.conn.commit()

    @contextmanager
    def _maintenance_timeout(self, ms: int = 1_800_000):
        """Raise THIS connection's statement_timeout for a known-heavy singleton
        maintenance statement, restoring the pooled default afterwards.

        The pool's 3-minute default exists to kill runaway *request* queries before
        they wedge every worker; the counts/authority/source rollups are deliberate
        whole-graph aggregates that outgrew it at 17M+ relations (both died with
        'canceling statement due to statement timeout' after the French import).
        RESET restores the value from the pool's ``-c statement_timeout`` startup
        option, so the raised limit never leaks back into request-serving use."""
        if self.backend != "postgres":
            yield
            return
        self.conn.execute(f"SET statement_timeout = {int(ms)}")
        try:
            yield
        finally:
            try:
                self.conn.execute("RESET statement_timeout")
            except Exception:  # noqa: BLE001 — a dropped conn resets itself anyway
                pass

    @contextmanager
    def _atomic(self):
        """Run a multi-statement write as one all-or-nothing unit on either backend.
        Postgres connects in autocommit mode (so reads never linger 'idle in transaction'
        holding locks), so writes that must be atomic open an explicit transaction here;
        SQLite uses its implicit transaction plus a final commit."""
        if self.backend == "postgres":
            with self.conn.transaction():
                yield
        else:
            try:
                yield
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def _migrate(self) -> None:
        """Additive, idempotent column migrations for DBs created before a column existed
        (the DDL is CREATE-IF-NOT-EXISTS, which doesn't add columns to a live table).

        Crucially, **check the column exists before issuing any ALTER** on either backend.
        ``ALTER TABLE … ADD COLUMN IF NOT EXISTS`` still requests an ACCESS EXCLUSIVE lock on
        Postgres even when the column is already there — so on an already-migrated DB (the
        steady state) an unconditional ALTER needlessly grabs a table lock, and if a
        concurrent reader holds the table (e.g. the periodic ``pg_dump`` backup, which holds
        ACCESS SHARE on every table for the whole dump) the ALTER queues behind it and every
        subsequent read queues behind the ALTER — deadlocking the app against its own backup
        until the pool times out. Reading ``information_schema`` first means zero DDL, and
        zero locks, whenever there is nothing to migrate. A short ``lock_timeout`` bounds the
        rare genuine ALTER so it can never hang startup for a backup's duration."""
        for table, col, decl in (
            ("documents", "meta_json", "TEXT"),
            ("relations", "candidate_id", "TEXT"),
            ("relations", "raw_fold", "TEXT"),
            # when this document's citations were last (re-)extracted — the durable
            # "last rescanned at" stamp a staleness-scoped rescan skips against (§5).
            ("documents", "last_extracted_at", "TEXT"),
            # A durable per-pass marker: a resumed citation scan excludes documents
            # already stamped with its root run id, regardless of ordering/new inserts.
            ("documents", "last_extraction_run_id", "TEXT"),
            ("jobs", "root_job_id", "TEXT"),
            ("jobs", "resumed_from", "TEXT"),
            ("jobs", "resume_policy", "TEXT NOT NULL DEFAULT 'restart'"),
            ("jobs", "attempt", "INTEGER NOT NULL DEFAULT 1"),
            ("jobs", "checkpoint_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("jobs", "restart_requested", "INTEGER NOT NULL DEFAULT 0"),
            ("jobs", "lease_heartbeat_at", "TEXT"),
        ):
            try:
                if self.backend == "postgres":
                    exists = self.conn.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = ? AND column_name = ?", (table, col)).fetchone()
                    if not exists:
                        self.conn.execute("SET lock_timeout = '5s'")
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {decl}")
                        self.conn.execute("SET lock_timeout = 0")
                else:
                    cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                    if col not in cols:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except Exception:  # noqa: BLE001 — a migration mustn't block startup
                pass
        # Check-before-CREATE, same medicine as the ALTER above: Postgres takes the
        # table's SHARE lock BEFORE noticing an index already exists, so at startup
        # these no-ops queued behind any long-running relations UPDATE (a resumed
        # bulk resolve) and the API sat unbound for minutes. The catalog probe is
        # lock-free; a genuinely-needed CREATE is bounded by lock_timeout instead of
        # waiting out a whole resolve batch.
        for stmt in _POST_MIGRATE_INDEXES:
            try:
                name = re.search(r"IF NOT EXISTS\s+([a-z0-9_]+)", stmt, re.I).group(1)
                if self.backend == "postgres":
                    hit = self.conn.execute(
                        "SELECT 1 FROM pg_class WHERE relname = ? AND relkind = 'i'",
                        (name,)).fetchone()
                    if hit:
                        continue
                    self.conn.execute("SET lock_timeout = '5s'")
                    try:
                        self.conn.execute(stmt)
                    finally:
                        self.conn.execute("SET lock_timeout = 0")
                else:
                    self.conn.execute(stmt)
            except Exception:  # noqa: BLE001 — a migration mustn't block startup
                pass

    @staticmethod
    def reset_schema_cache() -> None:
        """Forget which DSNs have had their DDL applied — for tests that drop the schema
        out from under the process."""
        _PG_SCHEMA_READY.clear()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Catalogue":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- dedup -------------------------------------------------------------
    def payload_hash_seen(self, payload_hash: str) -> bool:
        """Content-hash dedup (§5): True if we already hold these exact bytes, so
        the caller can short-circuit before extraction/embedding."""
        row = self.conn.execute(
            "SELECT 1 FROM documents WHERE payload_hash = ? LIMIT 1", (payload_hash,)
        ).fetchone()
        return row is not None

    def get_document(self, stable_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM documents WHERE stable_id = ?", (stable_id,)
        ).fetchone()

    def document_id_by_landing_url(self, url: str | None) -> str | None:
        """The stable_id of a held document with this landing URL, if any. The dedup
        gate needs it for adapters whose discovery stub carries only a PROVISIONAL id
        (the NZ courts feed keys a stub by URL; the real id is the neutral citation
        read out of the fetched PDF), so a stub can't be matched by id before fetch —
        but its URL is stable and already stored."""
        if not url:
            return None
        row = self.conn.execute(
            "SELECT stable_id FROM documents WHERE landing_url = ? LIMIT 1", (url,)
        ).fetchone()
        return row["stable_id"] if row else None

    def all_stable_ids(self) -> set[str]:
        """Every document id (one cheap single-column scan) — used to diff before/after a
        harvest so only the *newly* added docs get the expensive extract/classify pass."""
        return {r["stable_id"] for r in self.conn.execute("SELECT stable_id FROM documents")}

    def document_meta(self, stable_id: str) -> dict:
        """The adapter-supplied metadata bag (``record.extra``) for a document, decoded
        from ``meta_json``. Empty dict if none/unparseable."""
        row = self.conn.execute(
            "SELECT meta_json FROM documents WHERE stable_id = ?", (stable_id,)
        ).fetchone()
        if not row or not row["meta_json"]:
            return {}
        try:
            return json.loads(row["meta_json"])
        except (ValueError, TypeError):
            return {}

    # Every table column that holds a document's stable_id (so a re-key cascades cleanly).
    # (table, column, only_when_equal) — candidate_id/dst_id hold an id only when it's a
    # resolved target, so they must repoint too.
    _DOC_ID_REFS = (
        ("citation_aliases", "dst_id"),
        ("learned_shorthands", "candidate_id"),
        ("relations", "src_id"), ("relations", "dst_id"), ("relations", "candidate_id"),
        ("citations", "src_id"), ("citations", "candidate_id"),
        ("embeddings", "doc_id"), ("document_tags", "doc_id"),
        ("document_assets", "doc_id"), ("document_versions", "stable_id"),
        ("refinement_flags", "doc_id"),
    )

    def rekey_document(self, old_id: str, new_id: str, *, commit: bool = True) -> str:
        """Move a document from ``old_id`` to ``new_id``, cascading **every** stable-id
        reference (aliases, relations, citations, embeddings, tags, assets, versions,
        flags). If ``new_id`` is free it's a plain RENAME; if it already names a document
        the old row is dropped and its references fold into ``new_id`` (a MERGE — used to
        collapse a duplicate). Returns ``'noop' | 'rename' | 'merge'``.

        Conflict-safe on the columns that carry a uniqueness constraint (a chunk/tag/alias
        the target already has): the old row's copy is dropped rather than duplicated."""
        if old_id == new_id:
            return "noop"
        merging = self.get_document(new_id) is not None
        with self._atomic():
            # repoint references, skipping any row whose move would collide with one the
            # target already owns (only possible when merging).
            for table, col in self._DOC_ID_REFS:
                if merging:
                    keycols = self._UNIQUE_KEYCOLS.get((table, col))
                    if keycols:
                        cols = ", ".join(keycols)
                        self.conn.execute(
                            f"DELETE FROM {table} WHERE {col} = ? AND ({cols}) IN "
                            f"(SELECT {cols} FROM {table} WHERE {col} = ?)",
                            (old_id, new_id))
                self.conn.execute(
                    f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (new_id, old_id))
            if merging:
                self.conn.execute("DELETE FROM documents WHERE stable_id = ?", (old_id,))
            else:
                self.conn.execute(
                    "UPDATE documents SET stable_id = ? WHERE stable_id = ?", (new_id, old_id))
        if commit:
            self.conn.commit()
        return "merge" if merging else "rename"

    # For a MERGE, the (col, uniqueness-key) that would clash if the target already holds
    # an equivalent row — those old rows are dropped instead of moved.
    _UNIQUE_KEYCOLS = {
        ("embeddings", "doc_id"): ("chunk_id", "provider", "model", "model_version"),
        ("document_tags", "doc_id"): ("tag",),
        ("document_versions", "stable_id"): ("version",),
        ("citation_aliases", "dst_id"): ("alias",),
        ("learned_shorthands", "candidate_id"): ("shorthand",),
    }

    def set_document_meta(self, stable_id: str, meta: dict, *, title_if_empty: str | None = None,
                          commit: bool = True) -> None:
        """Overwrite a document's ``meta_json`` bag (and, only when the row's title is
        empty, its title) **without touching its text, payload_hash or version**. Used to
        attach metadata / a secondary text pointer to a document harvested another way —
        keeping the authoritative text in place while recording all metadata in the DB."""
        if title_if_empty:
            self.conn.execute(
                "UPDATE documents SET meta_json = ?, title = COALESCE(NULLIF(title, ''), ?) "
                "WHERE stable_id = ?",
                (json.dumps(meta) if meta else None, title_if_empty, stable_id),
            )
        else:
            self.conn.execute(
                "UPDATE documents SET meta_json = ? WHERE stable_id = ?",
                (json.dumps(meta) if meta else None, stable_id),
            )
        if commit:
            self.conn.commit()

    # -- writes ------------------------------------------------------------
    # One body, two codes → the canonical one, applied at write time so every future
    # import converges (and a re-harvest can't resurrect the old code). IEDPC is
    # BAILII's database code for the Irish Data Protection Commissioner's case
    # studies — the same body the EDPB one-stop-shop register codes as ``dpa-ie``
    # (labelled "Data Protection Commission (Ireland)"). Extend as merges arise.
    _COURT_CANON = {"iedpc": "dpa-ie"}

    def upsert_document(
        self, record: Record, *, raw_path: str | None = None, text_path: str | None = None
    ) -> None:
        """Insert or update a document and (re)write its extracted edges.

        Append-only (§1.4a): we never DELETE a document. A changed payload_hash is
        a new version (§1.4); here we bump ``version`` and keep ``is_latest`` on the
        row (full version history lands with the versioning step).
        """
        existing = self.get_document(record.stable_id)
        version = record.version
        changed = existing is not None and existing["payload_hash"] != record.payload_hash
        # archive-old-version + upsert-doc + rewrite-edges is one atomic unit (so a crash
        # can't leave a doc with half its edges, or a lost version row).
        with self._atomic():
            if changed:
                # content changed upstream → archive the prior version, then advance
                self._archive_version(existing)
                version = (existing["version"] or 1) + 1

            self.conn.execute(
                """
            INSERT INTO documents (
                stable_id, ecli, source, doc_type, title, court, decision_date,
                language, source_language, version, is_latest, landing_url,
                raw_path, text_path, payload_hash, has_text, extracted_via, added_by,
                topic_tags, topic_score, upstream_status, fetched_at, meta_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(stable_id) DO UPDATE SET
                ecli=excluded.ecli, source=excluded.source, doc_type=excluded.doc_type,
                title=excluded.title, court=excluded.court,
                decision_date=excluded.decision_date, language=excluded.language,
                source_language=excluded.source_language, version=excluded.version,
                landing_url=excluded.landing_url, raw_path=excluded.raw_path,
                text_path=excluded.text_path, payload_hash=excluded.payload_hash,
                has_text=excluded.has_text, extracted_via=excluded.extracted_via,
                topic_tags=excluded.topic_tags, topic_score=excluded.topic_score,
                fetched_at=excluded.fetched_at, meta_json=excluded.meta_json
            """,
            (
                record.stable_id,
                record.ecli,
                record.source,
                str(record.doc_type),
                record.title,
                self._COURT_CANON.get((record.court or "").lower(), record.court),
                _isodate(record.decision_date),
                record.language,
                record.source_language,
                version,
                1,
                record.landing_url,
                raw_path,
                text_path,
                record.payload_hash,
                1 if record.text else 0,
                str(record.extracted_via),
                str(record.added_by),
                json.dumps(record.topic_tags),
                record.topic_score,
                str(UpstreamStatus.LIVE),
                _now(),
                json.dumps(record.extra) if record.extra else None,
            ),
        )
            # Edges are re-derived from the record each upsert (a re-derivable
            # projection, §1.2): clear this src's prior edges, then re-add —
            # batched, for the same reason as the extraction stage (an adapter
            # shipping its own citation network writes hundreds of edges per doc).
            self.conn.execute("DELETE FROM relations WHERE src_id = ?", (record.stable_id,))
            self.add_relations(record.stable_id, record.relations, commit=False)

    @staticmethod
    def _edge_keys(rel: TypedRelation) -> tuple[str | None, str | None]:
        """``(candidate_id, raw_fold)`` for an edge — the normalised target id and the
        folded raw string, computed once here so every later read is an indexed lookup
        instead of re-running the matcher ladder (§5b)."""
        # Imported lazily: resolve/ imports the catalogue, so a module-level import cycles.
        from ..resolve.matchers import normalise_candidate
        from ..core.text import fold

        raw = rel.raw_citation_string
        return normalise_candidate(rel.dst_id, raw), (fold(raw) if raw else None)

    def _add_relation(self, src_id: str, rel: TypedRelation) -> None:
        candidate_id, raw_fold = self._edge_keys(rel)
        self.conn.execute(
            """
            INSERT INTO relations (
                src_id, dst_id, raw_citation_string, candidate_id, raw_fold,
                resolution_status, relationship_type, extracted_via, src_anchor,
                dst_anchor, context_start, context_end
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                src_id,
                rel.dst_id,
                rel.raw_citation_string,
                candidate_id,
                raw_fold,
                str(rel.resolution_status),
                str(rel.relationship_type),
                str(rel.extracted_via),
                rel.src_anchor,
                rel.dst_anchor,
                rel.context_start,
                rel.context_end,
            ),
        )

    def add_relation(self, src_id: str, rel: TypedRelation) -> None:
        """Add a single typed edge (e.g. a manual link between two existing
        documents). Standalone — unlike the relations rewritten by upsert."""
        self._add_relation(src_id, rel)
        self.conn.commit()

    def add_relations(self, src_id: str, rels: list[TypedRelation], *,
                      commit: bool = True) -> None:
        """Bulk-add edges — used by the citation-extraction stage. One executemany,
        not a round trip per edge: a dense judgment (an NL decision with its LiDO
        graph) carries hundreds of edges, and per-row INSERTs left the parallel
        extractor's parent thread living inside psycopg while its workers starved
        (caught live by py-spy). ``commit=False`` lets the bulk extractor batch many
        documents into one transaction (restartable off the extraction stamps)."""
        rows = []
        for rel in rels:
            candidate_id, raw_fold = self._edge_keys(rel)
            rows.append((
                src_id, rel.dst_id, rel.raw_citation_string, candidate_id, raw_fold,
                str(rel.resolution_status), str(rel.relationship_type),
                str(rel.extracted_via), rel.src_anchor, rel.dst_anchor,
                rel.context_start, rel.context_end,
            ))
        self.conn.executemany(
            """
            INSERT INTO relations (
                src_id, dst_id, raw_citation_string, candidate_id, raw_fold,
                resolution_status, relationship_type, extracted_via, src_anchor,
                dst_anchor, context_start, context_end
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        if commit:
            self.conn.commit()

    # -- extracted citations (§5, the audit/observation layer) -------------
    def add_citations(self, src_id: str, rows: list[dict], *, commit: bool = True) -> None:
        """Bulk-record extracted citations (one commit; ``commit=False`` for the
        batched bulk extractor). One executemany, not a round trip per row — a
        citation-dense judgment writes hundreds of observation rows, and per-row
        execute was the parallel extractor's parent-side bottleneck."""
        now = _now()
        self.conn.executemany(
            """
            INSERT INTO citations (
                src_id, raw, entity_kind, candidate_id, pinpoint,
                char_start, char_end, method, confidence, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    src_id, r["raw"], r.get("entity_kind"), r.get("candidate_id"),
                    r.get("pinpoint"), r.get("char_start"), r.get("char_end"),
                    r.get("method"), r.get("confidence"), now,
                )
                for r in rows
            ],
        )
        if commit:
            self.conn.commit()

    def clear_citations(self, src_id: str, *, commit: bool = True) -> None:
        self.conn.execute("DELETE FROM citations WHERE src_id = ?", (src_id,))
        if commit:
            self.conn.commit()

    def mark_extracted(self, src_id: str, *, run_id: str | None = None,
                       commit: bool = True) -> None:
        """Stamp ``last_extracted_at`` = now for a document — the durable "last rescanned"
        signal a staleness-scoped rescan skips against (§5). Set on every extraction,
        including ones that produced no citations, so citation-less documents converge and
        aren't re-scanned every run."""
        if run_id:
            self.conn.execute(
                "UPDATE documents SET last_extracted_at = ?, last_extraction_run_id = ? "
                "WHERE stable_id = ?", (_now(), run_id, src_id))
        else:
            # An unrelated incremental extraction must not erase a long scan's durable
            # completion marker while that scan is resumable.
            self.conn.execute(
                "UPDATE documents SET last_extracted_at = ? WHERE stable_id = ?",
                (_now(), src_id))
        if commit:
            self.conn.commit()

    def citations_for(self, src_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM citations WHERE src_id = ? ORDER BY char_start", (src_id,)
        ).fetchall()

    def citations_to(self, candidate_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM citations WHERE candidate_id = ? ORDER BY src_id", (candidate_id,)
        ).fetchall()

    def source_date_ranges(self) -> list[sqlite3.Row]:
        """Per-source document count and decision-date span — the completeness lens:
        what's covered, and over which period (§8). ISO date strings sort
        lexicographically so MIN/MAX give the span directly."""
        return self.conn.execute(
            """
            SELECT source,
                   COUNT(*)            AS documents,
                   MIN(decision_date)  AS earliest,
                   MAX(decision_date)  AS latest,
                   SUM(CASE WHEN payload_hash IS NOT NULL THEN 1 ELSE 0 END) AS with_text
            FROM documents
            GROUP BY source
            ORDER BY documents DESC
            """
        ).fetchall()

    def enrichment_misses(self, kind: str, *, max_age_days: float = 30) -> set[str]:
        """Keys whose external lookup recently came back empty — skipped on the next
        run to save quota, but **only for ``max_age_days``** (fractional days allowed, so
        a merely-unreachable item can cool off for hours rather than months). Nothing is
        flagged forever: a miss expires and is retried later, so a transient/batch failure
        can never permanently stop an item being fetched."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        rows = self.conn.execute(
            "SELECT key FROM enrichment_misses WHERE kind = ? AND attempted_at >= ?",
            (kind, cutoff),
        ).fetchall()
        return {r["key"] for r in rows}

    def clear_enrichment_misses(self, kind: str) -> None:
        self.conn.execute("DELETE FROM enrichment_misses WHERE kind = ?", (kind,))
        self.conn.commit()

    def record_enrichment_misses(self, kind: str, keys) -> None:
        for k in set(keys):
            self.conn.execute(
                "INSERT INTO enrichment_misses (kind, key, attempted_at) VALUES (?,?,?) "
                "ON CONFLICT (kind, key) DO UPDATE SET attempted_at = excluded.attempted_at",
                (kind, k, _now()),
            )
        self.conn.commit()

    # -- outstanding-effects re-check queue (§0) ----------------------------
    @staticmethod
    def _effects_backoff_days(checks: int, base_days: int) -> int:
        """Slow, capped exponential backoff: re-check after base, 2×, 4×, 8× base —
        capped at ~24 weeks. Outstanding effects (esp. uncommenced provisions awaiting
        a commencement order) can sit for a long time, so checking weekly would waste
        fetches; this widens the gap each time nothing has changed."""
        return min(base_days * (2 ** min(checks, 3)), 168)

    def record_outstanding_effects(
        self, stable_id: str, outstanding: int, affecting, *, base_days: int = 21,
    ) -> None:
        """Upsert a legislation item's outstanding-effects state after a fetch.

        ``outstanding == 0`` → the editors have incorporated everything we knew about,
        so drop it from the queue. Otherwise (re)schedule the next re-check: a new item
        starts at ``base_days``; a re-check that *still* finds effects backs off further
        (so we don't pull it super-regularly). ``first_seen`` is preserved across re-checks."""
        if outstanding <= 0:
            self.clear_effects_refresh(stable_id)
            return
        aff_json = json.dumps(list(affecting or []))
        now = _now()
        row = self.conn.execute(
            "SELECT checks FROM effects_refresh WHERE stable_id = ?", (stable_id,)
        ).fetchone()
        if row is None:
            nxt = (datetime.now(timezone.utc)
                   + timedelta(days=self._effects_backoff_days(0, base_days))).isoformat()
            self.conn.execute(
                "INSERT INTO effects_refresh "
                "(stable_id, outstanding, affecting, checks, first_seen, last_checked, next_check_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (stable_id, outstanding, aff_json, 0, now, now, nxt),
            )
        else:
            checks = int(row["checks"]) + 1
            nxt = (datetime.now(timezone.utc)
                   + timedelta(days=self._effects_backoff_days(checks, base_days))).isoformat()
            self.conn.execute(
                "UPDATE effects_refresh SET outstanding = ?, affecting = ?, checks = ?, "
                "last_checked = ?, next_check_at = ? WHERE stable_id = ?",
                (outstanding, aff_json, checks, now, nxt, stable_id),
            )
        self.conn.commit()

    def clear_effects_refresh(self, stable_id: str) -> None:
        self.conn.execute("DELETE FROM effects_refresh WHERE stable_id = ?", (stable_id,))
        self.conn.commit()

    def due_effects_refresh(self, *, limit: int = 20) -> list[sqlite3.Row]:
        """Items whose next re-check time has arrived (and which still have effects).
        Oldest-due first, bounded — the scheduler pulls a small batch per tick so a
        burst of due items can't turn into a fetch storm."""
        return self.conn.execute(
            "SELECT * FROM effects_refresh WHERE outstanding > 0 AND next_check_at <= ? "
            "ORDER BY next_check_at LIMIT ?",
            (_now(), limit),
        ).fetchall()

    def list_effects_refresh(self, *, limit: int = 500) -> list[sqlite3.Row]:
        """The whole outstanding-effects queue, most-outstanding first (for the UI/MCP)."""
        return self.conn.execute(
            "SELECT * FROM effects_refresh ORDER BY outstanding DESC, next_check_at LIMIT ?",
            (limit,),
        ).fetchall()

    def count_pending_relations(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM relations WHERE resolution_status = 'pending'"
        ).fetchone()["n"]

    _CANDIDATE_FREQ_SQL = """
            SELECT candidate_id, entity_kind,
                   MIN(method)        AS method,
                   MIN(raw)           AS sample,
                   COUNT(*)           AS occurrences,
                   COUNT(DISTINCT src_id) AS documents
            FROM citations
            WHERE candidate_id IS NOT NULL
            GROUP BY candidate_id, entity_kind
    """

    def candidate_frequencies(self, *, live: bool = False) -> list[sqlite3.Row]:
        """Aggregate the citations audit table by distinct candidate: how often
        each is cited and from how many documents, with its kind + grammar. The
        substrate for the snowball (citations.snowball) — which references the
        corpus makes most that aren't yet nodes.

        Served from the ``citation_counts`` roll-up, which the scheduler rebuilds: the
        live aggregate is a ~13s scan of a 10M-row table, and the frontier does not move
        between ticks. ``live=True`` forces the scan (and is what rebuild uses)."""
        if not live:
            rows = self.conn.execute(
                "SELECT candidate_id, entity_kind, method, sample, occurrences, documents "
                "FROM citation_counts ORDER BY occurrences DESC"
            ).fetchall()
            if rows:
                return rows
            # never rolled up yet (fresh DB / test) → fall through to the live scan
        return self.conn.execute(
            self._CANDIDATE_FREQ_SQL + " ORDER BY occurrences DESC"
        ).fetchall()

    def rebuild_citation_counts(self) -> int:
        """Recompute the citation frequency roll-up. One pass; run on a cadence."""
        with self._maintenance_timeout(), self._atomic():
            self.conn.execute("DELETE FROM citation_counts")
            self.conn.execute(
                "INSERT INTO citation_counts "
                "(candidate_id, entity_kind, method, sample, occurrences, documents, rebuilt_at) "
                "SELECT candidate_id, entity_kind, method, sample, occurrences, documents, ? "
                f"FROM ({self._CANDIDATE_FREQ_SQL}) s",
                (_now(),),
            )
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM citation_counts"
        ).fetchone()["n"]

    def storage_size(self) -> dict:
        """Total database size in bytes plus the largest tables — the Maintain page's
        disk indicator. Catalog lookups only (instant), never a filesystem walk."""
        if self.backend == "postgres":
            total = self.conn.execute(
                "SELECT pg_database_size(current_database()) AS n").fetchone()["n"]
            tables = [dict(r) for r in self.conn.execute(
                """
                SELECT relname AS name, pg_total_relation_size(c.oid) AS bytes
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 8
                """).fetchall()]
        else:
            row = self.conn.execute(
                "SELECT (SELECT page_count FROM pragma_page_count()) * "
                "(SELECT page_size FROM pragma_page_size()) AS n").fetchone()
            total, tables = int(row["n"] or 0), []
        return {"database_bytes": int(total), "tables": tables}

    def refresh_source_stats(self) -> int:
        """Recompute the per-source resolved-outgoing roll-up (one heavy aggregate,
        on the citation-counts cadence — never inline in a page load)."""
        with self._maintenance_timeout(), self._atomic():
            self.conn.execute("DELETE FROM source_stats")
            self.conn.execute(
                "INSERT INTO source_stats (source, resolved_outgoing, rebuilt_at) "
                "SELECT d.source, COUNT(*), ? FROM relations r "
                "JOIN documents d ON d.stable_id = r.src_id "
                "WHERE r.resolution_status = 'resolved' AND r.src_id <> r.dst_id "
                "GROUP BY d.source",
                (_now(),),
            )
        return self.conn.execute("SELECT COUNT(*) AS n FROM source_stats").fetchone()["n"]

    def source_stats(self) -> dict[str, int]:
        """The roll-up, or {} when it has never been rebuilt (caller falls back live)."""
        return {r["source"]: r["resolved_outgoing"] for r in self.conn.execute(
            "SELECT source, resolved_outgoing FROM source_stats")}

    def refresh_corpus_shape_stats(self) -> int:
        """Recompute the homepage base aggregate (one heavy scan, on the counts
        cadence — never inline in a page load)."""
        with self._maintenance_timeout(), self._atomic():
            self.conn.execute("DELETE FROM corpus_shape_stats")
            self.conn.execute(
                "INSERT INTO corpus_shape_stats "
                "(source, doc_type, court, yr, n, with_text, embedded, rebuilt_at) "
                "SELECT source, doc_type, court, substr(decision_date, 1, 4), "
                "COUNT(*), SUM(has_text), SUM(has_embedding), ? "
                "FROM documents GROUP BY source, doc_type, court, substr(decision_date, 1, 4)",
                (_now(),),
            )
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM corpus_shape_stats").fetchone()["n"]

    def corpus_shape_stats(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT source, doc_type, court, yr, n, with_text, embedded "
            "FROM corpus_shape_stats").fetchall()

    def replace_leg_type_stats(self, rows: list[tuple]) -> int:
        """Overwrite the legislation-type rail roll-up. ``rows`` are
        ``(source, label, n, years_json, filters_json)``; the caller (facade) runs
        the taxonomy classification pass that produces them."""
        now = _now()
        with self._atomic():
            self.conn.execute("DELETE FROM leg_type_stats")
            for source, label, n, years_json, filters_json in rows:
                self.conn.execute(
                    "INSERT INTO leg_type_stats "
                    "(source, label, n, years_json, filters_json, rebuilt_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (source, label, n, years_json, filters_json, now))
        return len(rows)

    def leg_type_stats(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT source, label, n, years_json, filters_json FROM leg_type_stats"
        ).fetchall()

    def legislation_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE doc_type = 'legislation'"
        ).fetchone()["n"]

    def clear_relations(self, src_id: str, *, extracted_via: str,
                        commit: bool = True) -> None:
        """Drop a source's edges from one extraction method, so re-running that
        extractor is idempotent (a re-derivable projection, §1.2). Leaves
        structurally-extracted and manual edges intact."""
        self.conn.execute(
            "DELETE FROM relations WHERE src_id = ? AND extracted_via = ?",
            (src_id, extracted_via),
        )
        if commit:
            self.conn.commit()

    def clear_relations_of_type(self, src_id: str, relationship_type: str) -> None:
        """Drop a source's edges of one relationship type — so re-deriving them (e.g.
        re-scanning an act's affecting-side Changes feed) is idempotent without touching
        its other edges."""
        self.conn.execute(
            "DELETE FROM relations WHERE src_id = ? AND relationship_type = ?",
            (src_id, relationship_type),
        )
        self.conn.commit()

    def mark_effects_due(self, stable_id: str, affecting, *, count: int = 1) -> None:
        """Flag an *affected* instrument for re-pull NOW — used when a newly-imported
        amending act says it changes this one, so the change is incorporated even though
        the affected act might otherwise never be re-pulled. Sets the re-check to due
        immediately without disturbing an existing authoritative outstanding count; a
        fresh entry seeds ``count`` so the row survives until the act's own metadata
        (re)computes the real figure on re-pull."""
        now = _now()
        row = self.conn.execute(
            "SELECT stable_id FROM effects_refresh WHERE stable_id = ?", (stable_id,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO effects_refresh "
                "(stable_id, outstanding, affecting, checks, first_seen, last_checked, next_check_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (stable_id, max(count, 1), json.dumps(list(affecting or [])), 0, now, None, now),
            )
        else:
            self.conn.execute(
                "UPDATE effects_refresh SET next_check_at = ? WHERE stable_id = ?",
                (now, stable_id),
            )
        self.conn.commit()

    def _archive_version(self, row) -> None:
        """Retain the prior version before the documents row advances (§1.4)."""
        self.conn.execute(
            """
            INSERT INTO document_versions (
                stable_id, version, payload_hash, raw_path, text_path, title,
                decision_date, extracted_via, archived_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (stable_id, version) DO NOTHING
            """,
            (
                row["stable_id"], row["version"], row["payload_hash"], row["raw_path"],
                row["text_path"], row["title"], row["decision_date"], row["extracted_via"],
                _now(),
            ),
        )

    def list_versions(self, stable_id: str) -> list[sqlite3.Row]:
        """Archived prior versions (newest first); the documents row is 'latest'."""
        return self.conn.execute(
            "SELECT * FROM document_versions WHERE stable_id = ? ORDER BY version DESC",
            (stable_id,),
        ).fetchall()

    def mark_upstream_status(self, stable_id: str, status: UpstreamStatus) -> None:
        """Record a disappearance as a state change, never a deletion (§1.4a)."""
        self.conn.execute(
            "UPDATE documents SET upstream_status = ?, upstream_status_at = ? WHERE stable_id = ?",
            (str(status), _now(), stable_id),
        )
        self.conn.commit()

    def relations_for(self, src_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM relations WHERE src_id = ?", (src_id,)
        ).fetchall()

    # -- document assets (§1.9 attach/annotate) ----------------------------
    def add_asset(
        self,
        doc_id: str,
        kind: str,
        *,
        path: str | None = None,
        mime: str | None = None,
        payload_hash: str | None = None,
        added_by: str = "user",
        title: str | None = None,
    ) -> int:
        return self._insert_returning(
            """
            INSERT INTO document_assets (doc_id, kind, path, mime, payload_hash, added_by, title, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (doc_id, kind, path, mime, payload_hash, added_by, title, _now()),
            "asset_id",
        )

    def assets_for(self, doc_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM document_assets WHERE doc_id = ? ORDER BY created_at", (doc_id,)
        ).fetchall()

    def set_relationship_type(self, relation_id: int, relationship_type: str, *, extracted_via: str) -> None:
        """Reclassify an edge's treatment (§1.3a) — e.g. mentions → distinguishes —
        recording how it was inferred via ``extracted_via``."""
        self.conn.execute(
            "UPDATE relations SET relationship_type = ?, extracted_via = ? WHERE relation_id = ?",
            (relationship_type, extracted_via, relation_id),
        )
        self.conn.commit()

    # -- manual corrections (human curation wins, §4a/§1.3a) ----------------
    _DOC_FIELDS = {"doc_type", "title", "court", "source_language"}

    def update_document_fields(self, stable_id: str, fields: dict, *, curate: bool = True) -> bool:
        """Update a document's projected metadata (doc_type / title / court / language).
        ``curate`` (default) marks ``added_by='user'`` to record human correction; pass
        ``curate=False`` for system backfills (e.g. fetching a CJEU case name) that
        shouldn't masquerade as user curation."""
        sets = {k: v for k, v in fields.items() if k in self._DOC_FIELDS and v is not None}
        if not sets:
            return False
        cols = ", ".join(f"{k} = ?" for k in sets)
        tail = ", added_by = 'user'" if curate else ""
        self.conn.execute(
            f"UPDATE documents SET {cols}{tail} WHERE stable_id = ?",
            (*sets.values(), stable_id),
        )
        self.conn.commit()
        return True

    def get_relation(self, relation_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM relations WHERE relation_id = ?", (relation_id,)
        ).fetchone()

    def suppress_relation(self, relation_id: int) -> sqlite3.Row | None:
        """Flag a spurious citation edge as a human-confirmed false positive. Kept
        (not deleted) as a ``suppressed`` manual edge so re-extraction *respects* it
        — the extractor skips re-adding a citation the user has rejected."""
        rel = self.get_relation(relation_id)
        if rel is None:
            return None
        self.conn.execute(
            """
            UPDATE relations
            SET relationship_type = 'suppressed', resolution_status = 'suppressed',
                extracted_via = 'manual'
            WHERE relation_id = ?
            """,
            (relation_id,),
        )
        self.conn.commit()
        return rel

    def delete_relation(self, relation_id: int) -> None:
        self.conn.execute("DELETE FROM relations WHERE relation_id = ?", (relation_id,))
        self.conn.commit()

    def suppressed_targets(self, src_id: str) -> tuple[set, set]:
        """A source's user-rejected citations: ``(candidate_ids, raw_strings)`` — so
        the extractor can skip re-adding them on the next pass."""
        rows = self.conn.execute(
            "SELECT dst_id, raw_citation_string FROM relations "
            "WHERE src_id = ? AND relationship_type = 'suppressed'",
            (src_id,),
        ).fetchall()
        return ({r["dst_id"] for r in rows if r["dst_id"]},
                {r["raw_citation_string"] for r in rows if r["raw_citation_string"]})

    def remove_document_tag(self, doc_id: str, tag: str, *, method: str = "manual") -> bool:
        """Remove a tag a user added by mistake (the un-tag correction)."""
        cur = self.conn.execute(
            "DELETE FROM document_tags WHERE doc_id = ? AND tag = ? AND method = ?",
            (doc_id, tag, method),
        )
        self._refresh_topic_tags_cache(doc_id)
        self.conn.commit()
        return cur.rowcount > 0

    def relations_to(self, dst_id: str) -> list[sqlite3.Row]:
        """Incoming resolved edges — what cites/treats this document (citing cases,
        commentary). The other half of 1-hop graph expansion (§6c)."""
        return self.conn.execute(
            "SELECT * FROM relations WHERE dst_id = ? AND resolution_status = 'resolved' "
            "AND relationship_type <> 'cited_by'",  # reverse-oriented scaffold
            (dst_id,),
        ).fetchall()

    def authority_counts(self, ids: list[str]) -> dict[str, int]:
        """How often each id is itself cited — from the ``citation_counts`` roll-up, keyed by
        candidate_id (a stable_id, ECLI or CELEX). Used to rank citing documents by their own
        authority (most-cited first) for the "mentioned by" lists. Missing ids → absent."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return {}
        qs = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT candidate_id, MAX(occurrences) AS occ FROM citation_counts "
            f"WHERE candidate_id IN ({qs}) GROUP BY candidate_id", ids).fetchall()
        return {r["candidate_id"]: r["occ"] for r in rows}

    # -- citation-network statistics (the authority prior; design §3) --------
    # The ranking graph is the resolved, non-inferred, non-suppressed edge set.
    # Treatment types are deliberately NOT weighted — the classifier isn't
    # reliable yet, so every edge counts as a plain mention.
    _GRAPH_EDGE_SQL = (
        "SELECT DISTINCT src_id, dst_id FROM relations "
        "WHERE resolution_status = 'resolved' AND dst_id IS NOT NULL "
        "AND extracted_via <> 'inferred' AND relationship_type <> 'suppressed' "
        # cited_by edges are reverse-oriented harvest scaffolds (src=cited,
        # dst=citer) — counting them feeds PageRank backwards
        "AND relationship_type <> 'cited_by' "
        # self-loops excluded: an instrument's internal cross-references (429k
        # structured src==dst edges live) must not feed its own PageRank
        "AND src_id <> dst_id"
    )

    def rebuild_authority(self, *, on_progress=None) -> int:
        """Recompute the ``doc_authority`` roll-up (PageRank raw + age-decayed,
        degrees, percentile) over the whole resolved graph. A scheduled batch job,
        like ``rebuild_citation_counts`` — pure Python, no extra dependencies."""
        from datetime import date

        from ..retrieval.authority import compute_authority

        if on_progress:
            on_progress(stage="loading edges")
        with self._maintenance_timeout():
            edges = [(r["src_id"], r["dst_id"]) for r in self.conn.execute(self._GRAPH_EDGE_SQL)]
            years: dict[str, int] = {}
            for r in self.conn.execute(
                    "SELECT stable_id, decision_date FROM documents WHERE decision_date IS NOT NULL"):
                try:
                    years[r["stable_id"]] = int(str(r["decision_date"])[:4])
                except (ValueError, TypeError):
                    continue
        if on_progress:
            on_progress(stage="pagerank", total=len(edges))
        rows = compute_authority(edges, years, now_year=date.today().year)
        if on_progress:
            on_progress(stage="writing", total=len(rows))
        now = _now()
        with self._atomic():
            self.conn.execute("DELETE FROM doc_authority")
            chunk = 500
            for i in range(0, len(rows), chunk):
                batch = rows[i:i + chunk]
                ph = ",".join(["(?,?,?,?,?,?,?)"] * len(batch))
                params: list = []
                for doc_id, pr, prd, pct, ind, outd in batch:
                    params.extend((doc_id, pr, prd, pct, ind, outd, now))
                self.conn.execute(
                    "INSERT INTO doc_authority (doc_id, pagerank, pagerank_decayed, "
                    "percentile, in_degree, out_degree, rebuilt_at) VALUES " + ph, params)
        return len(rows)

    def authority_for(self, ids: list[str]) -> dict[str, dict]:
        """Authority rows for a set of document ids (missing → absent). Chunked —
        callers pass up to a heavily-cited authority's whole citer set."""
        ids = [i for i in dict.fromkeys(ids) if i]
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            qs = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT * FROM doc_authority WHERE doc_id IN ({qs})", chunk).fetchall()
            out.update({r["doc_id"]: dict(r) for r in rows})
        return out

    def neighbours_out(self, doc_id: str, *, limit: int = 200,
                       include_inferred: bool = False) -> list[sqlite3.Row]:
        """Bounded outgoing resolved edges — unlike ``relations_for`` this never
        returns an unbounded set, so it's safe on any node."""
        extra = "" if include_inferred else "AND extracted_via <> 'inferred' "
        return self.conn.execute(
            "SELECT * FROM relations WHERE src_id = ? AND resolution_status = 'resolved' "
            f"AND dst_id IS NOT NULL AND dst_id <> src_id {extra}LIMIT ?",
            (doc_id, limit)).fetchall()

    def neighbours_in(self, doc_id: str, *, limit: int = 200,
                      include_inferred: bool = False) -> list[sqlite3.Row]:
        """Bounded incoming resolved edges (a heavily-cited authority has 100k+)."""
        extra = "" if include_inferred else "AND extracted_via <> 'inferred' "
        return self.conn.execute(
            "SELECT * FROM relations WHERE dst_id = ? AND resolution_status = 'resolved' "
            "AND relationship_type <> 'cited_by' "  # reverse-oriented scaffold
            f"AND src_id <> dst_id {extra}LIMIT ?", (doc_id, limit)).fetchall()

    def co_cited_with(self, ids: list[str], *, limit: int = 15,
                      max_citers: int = 500) -> list[dict]:
        """Documents most often cited *together with* this one (in the same citing
        document) — the classic "related cases" signal. Bounded: at most
        ``max_citers`` citing documents are sampled, so a GDPR-scale node can't
        explode the join."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"""
            SELECT r.dst_id AS id, COUNT(DISTINCT r.src_id) AS n
            FROM relations r
            JOIN (SELECT DISTINCT src_id FROM relations
                  WHERE dst_id IN ({qs}) AND resolution_status = 'resolved'
                    AND extracted_via <> 'inferred' LIMIT ?) citers
              ON r.src_id = citers.src_id
            WHERE r.dst_id IS NOT NULL AND r.dst_id NOT IN ({qs})
              AND r.resolution_status = 'resolved' AND r.extracted_via <> 'inferred'
            GROUP BY r.dst_id ORDER BY n DESC LIMIT ?
            """,
            (*ids, max_citers, *ids, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def coupled_with(self, doc_id: str, *, limit: int = 15,
                     max_target_citers: int = 1500) -> list[dict]:
        """Documents that rely on the same authorities as this one (bibliographic
        coupling). Ubiquitous targets (cited by more than ``max_target_citers``
        documents — the GDPR problem) carry no discriminating signal and would
        blow up the join, so they're dropped before aggregating."""
        outs = [r["dst_id"] for r in self.conn.execute(
            "SELECT DISTINCT dst_id FROM relations WHERE src_id = ? "
            "AND resolution_status = 'resolved' AND dst_id IS NOT NULL "
            "AND extracted_via <> 'inferred'", (doc_id,)).fetchall()]
        if not outs:
            return []
        qs = ",".join("?" * len(outs))
        counts = self.conn.execute(
            f"SELECT dst_id, COUNT(DISTINCT src_id) AS n FROM relations "
            f"WHERE dst_id IN ({qs}) AND resolution_status = 'resolved' "
            f"GROUP BY dst_id", outs).fetchall()
        keep = [r["dst_id"] for r in counts if r["n"] <= max_target_citers][:100]
        if not keep:
            return []
        kqs = ",".join("?" * len(keep))
        rows = self.conn.execute(
            f"""
            SELECT r.src_id AS id, COUNT(DISTINCT r.dst_id) AS n
            FROM relations r
            WHERE r.dst_id IN ({kqs}) AND r.src_id <> ?
              AND r.resolution_status = 'resolved' AND r.extracted_via <> 'inferred'
            GROUP BY r.src_id ORDER BY n DESC LIMIT ?
            """,
            (*keep, doc_id, limit),
        ).fetchall()
        return [{**dict(r), "of": len(keep)} for r in rows]

    def top_citing_edges(self, ids: list[str], *, limit: int = 200,
                         sources: list[str] | None = None) -> list[sqlite3.Row]:
        """The strongest incoming edges for the cited-by panel: rows ranked by the
        CITING document's PageRank, bounded — one indexed query instead of
        materialising a mega-authority's 100k citers in Python (which pinned a
        pool connection for seconds per page view). ``src_pagerank`` rides along.

        ``sources`` restricts to citers from those adapter sources — the server-side
        slice behind the panel's jurisdiction facets. Without it, a mega-authority's
        bounded window fills with the top jurisdictions' heavyweights and the long
        tail (2,484 French GDPR citers, none in the global top slice) is unreachable."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        src_join, src_where, src_params = "", "", []
        if sources:
            qs2 = ",".join("?" * len(sources))
            src_join = "JOIN documents d ON d.stable_id = r.src_id"
            src_where = f"AND d.source IN ({qs2})"
            src_params = list(sources)
        return self.conn.execute(
            f"""
            SELECT r.*, COALESCE(a.pagerank, 0) AS src_pagerank
            FROM relations r LEFT JOIN doc_authority a ON a.doc_id = r.src_id
            {src_join}
            WHERE r.dst_id IN ({qs}) AND r.resolution_status = 'resolved'
              AND r.extracted_via <> 'inferred' AND r.src_id <> r.dst_id
              AND r.relationship_type <> 'cited_by'  -- reverse-oriented scaffold
              {src_where}
            ORDER BY src_pagerank DESC LIMIT ?
            """, (*ids, *src_params, limit)).fetchall()

    def citing_breakdown(self, ids: list[str]) -> list[sqlite3.Row]:
        """Distinct citing DOCUMENTS grouped by (source, court, doc_type), over the
        WHOLE resolved incoming set — the raw material for HONEST cited-by facets.
        The panel's loaded rows are the bounded top slice by PageRank; computing
        facet counts over that slice silently erased whole jurisdictions (a corpus
        holding 2,484 French decisions citing the GDPR read as "no French case
        law"). One indexed aggregate; the facade folds these rows into its
        jurisdiction × kind buckets."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        return self.conn.execute(
            f"""
            SELECT d.source, d.court, d.doc_type, COUNT(DISTINCT r.src_id) AS docs
            FROM relations r JOIN documents d ON d.stable_id = r.src_id
            WHERE r.dst_id IN ({qs}) AND r.resolution_status = 'resolved'
              AND r.extracted_via <> 'inferred' AND r.src_id <> r.dst_id
              AND r.relationship_type <> 'cited_by'
            GROUP BY d.source, d.court, d.doc_type
            """, ids).fetchall()

    def inferred_citer_count(self, ids: list[str]) -> int:
        """Distinct inferred-only citers (reported separately, never in cited-by)."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return 0
        qs = ",".join("?" * len(ids))
        return self.conn.execute(
            f"SELECT COUNT(DISTINCT src_id) AS n FROM relations "
            f"WHERE dst_id IN ({qs}) AND extracted_via = 'inferred' AND src_id <> dst_id",
            ids).fetchone()["n"]

    def citer_count_by_doc_type(self, ids: list[str], doc_type: str) -> int:
        """Distinct resolved incoming documents of one family (MCP/UI availability flag)."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return 0
        qs = ",".join("?" * len(ids))
        return self.conn.execute(
            f"""SELECT COUNT(DISTINCT r.src_id) AS n
                FROM relations r JOIN documents d ON d.stable_id = r.src_id
                WHERE r.dst_id IN ({qs}) AND r.resolution_status = 'resolved'
                  AND r.extracted_via <> 'inferred' AND r.relationship_type <> 'cited_by'
                  AND r.src_id <> r.dst_id AND d.doc_type = ?""",
            (*ids, doc_type)).fetchone()["n"]

    def cited_by_stats(self, ids: list[str], *, recent_years: int = 5) -> dict:
        """Aggregate cited-by numbers for the citator: distinct citing documents,
        total occurrences, and how many of those citers decided in the last N
        years (SQL aggregates — never materialises the row set in Python)."""
        from datetime import date

        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return {"documents": 0, "recent_documents": 0, "recent_years": recent_years}
        qs = ",".join("?" * len(ids))
        base = (f"FROM relations r WHERE r.dst_id IN ({qs}) "
                "AND r.resolution_status = 'resolved' AND r.extracted_via <> 'inferred' "
                # exclude the reverse-oriented cited_by discovery scaffold
                "AND r.relationship_type <> 'cited_by' "
                "AND r.src_id <> r.dst_id")
        total = self.conn.execute(
            f"SELECT COUNT(DISTINCT r.src_id) AS n {base}", ids).fetchone()["n"]
        cutoff = f"{date.today().year - recent_years:04d}-01-01"
        recent = self.conn.execute(
            f"SELECT COUNT(DISTINCT r.src_id) AS n {base} "
            "AND EXISTS (SELECT 1 FROM documents d WHERE d.stable_id = r.src_id "
            "AND d.decision_date >= ?)", (*ids, cutoff)).fetchone()["n"]
        return {"documents": total, "recent_documents": recent, "recent_years": recent_years}

    def source_court_for(self, ids: list[str]) -> dict[str, tuple[str, str]]:
        """``{stable_id: (source, court)}`` for many documents in one query — enough to
        bucket each into a jurisdiction without loading whole rows."""
        ids = [i for i in dict.fromkeys(ids) if i]
        out: dict[str, tuple[str, str]] = {}
        for i in range(0, len(ids), 800):
            chunk = ids[i: i + 800]
            qs = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                    f"SELECT stable_id, source, court FROM documents WHERE stable_id IN ({qs})",
                    chunk).fetchall():
                out[r["stable_id"]] = (r["source"] or "", r["court"] or "")
        return out

    def cited_by_counts(self, ids: list[str]) -> dict[str, int]:
        """``{doc_id: how many distinct documents cite it}`` for MANY ids at once.

        The cited-by panel annotates each citer with its own citation count, as a quiet
        cue to how much weight that citer carries. Asking per row would be 200 queries
        on one page view — the N+1 that pinned a pool connection per view before — so
        this is one grouped aggregate over the same partial index."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return {}
        qs = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"""
            SELECT r.dst_id AS id, COUNT(DISTINCT r.src_id) AS n
            FROM relations r
            WHERE r.dst_id IN ({qs})
              AND r.resolution_status = 'resolved' AND r.extracted_via <> 'inferred'
              AND r.relationship_type <> 'cited_by' AND r.src_id <> r.dst_id
            GROUP BY r.dst_id
            """, ids).fetchall()
        return {r["id"]: r["n"] for r in rows}

    def cited_by_types(self, ids: list[str]) -> dict[str, int]:
        """Who cites this document, broken down by the citing document's TYPE —
        the Explore drill-down's "what hangs off this instrument" line (cases /
        guidance / other legislation citing an act). One indexed aggregate."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return {}
        qs = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"""
            SELECT d.doc_type, COUNT(DISTINCT r.src_id) AS n
            FROM relations r JOIN documents d ON d.stable_id = r.src_id
            WHERE r.dst_id IN ({qs}) AND r.resolution_status = 'resolved'
              AND r.extracted_via <> 'inferred' AND r.src_id <> r.dst_id
              AND r.relationship_type <> 'cited_by'
            GROUP BY d.doc_type
            """, ids).fetchall()
        return {r["doc_type"]: r["n"] for r in rows}

    def cited_by_types_by_id(self, ids: list[str]) -> dict[str, dict[str, int]]:
        """``cited_by_types`` for many targets in one indexed aggregate, keyed by
        target id — the Explore drill batches its legislation rows through this
        instead of one query per instrument."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return {}
        qs = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"""
            SELECT r.dst_id, d.doc_type, COUNT(DISTINCT r.src_id) AS n
            FROM relations r JOIN documents d ON d.stable_id = r.src_id
            WHERE r.dst_id IN ({qs}) AND r.resolution_status = 'resolved'
              AND r.extracted_via <> 'inferred' AND r.src_id <> r.dst_id
              AND r.relationship_type <> 'cited_by'
            GROUP BY r.dst_id, d.doc_type
            """, ids).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["dst_id"], {})[r["doc_type"]] = r["n"]
        return out

    def top_citors(self, ids: list[str], *, limit: int = 8) -> list[dict]:
        """The most authoritative documents citing this one (by their own PageRank),
        for the citator's "most significant citing documents" list."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"""
            SELECT r.src_id AS id, COALESCE(MAX(a.pagerank), 0) AS pagerank, COUNT(*) AS n
            FROM relations r LEFT JOIN doc_authority a ON a.doc_id = r.src_id
            WHERE r.dst_id IN ({qs}) AND r.resolution_status = 'resolved'
              AND r.extracted_via <> 'inferred'
              AND r.relationship_type <> 'cited_by'  -- reverse-oriented scaffold
            GROUP BY r.src_id ORDER BY pagerank DESC LIMIT ?
            """,
            (*ids, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- entity resolution (§5b) -------------------------------------------
    def find_document_id(self, candidate: str) -> str | None:
        """Confirm a candidate exists as a node, by stable_id, ECLI, or an alias
        (e.g. a CELEX → ECLI mapping, so "C-311/18" resolves to its ECLI-keyed
        judgment). The invariant: a *resolved* edge points at a real document (§5b)."""
        row = self.conn.execute(
            "SELECT stable_id FROM documents WHERE stable_id = ? OR ecli = ? LIMIT 1",
            (candidate, candidate),
        ).fetchone()
        if row:
            return row["stable_id"]
        # alias fallback (CELEX/colloquial → id), then confirm the target exists
        alias_dst = self.get_alias(candidate) or self.get_alias(candidate.casefold())
        if alias_dst:
            row = self.conn.execute(
                "SELECT stable_id FROM documents WHERE stable_id = ? OR ecli = ? LIMIT 1",
                (alias_dst, alias_dst),
            ).fetchone()
            return row["stable_id"] if row else None
        return None

    def find_existing(self, candidates) -> dict:
        """Batch version of :meth:`find_document_id` — given many candidate ids, return
        ``{candidate: real_stable_id}`` for those that resolve to a present document
        (by stable_id/ECLI or via an alias), in a handful of queries instead of one
        per candidate (the resolver runs this over ~100k+ pending edges)."""
        cands = [c for c in dict.fromkeys(candidates) if c]
        out: dict[str, str] = {}
        if not cands:
            return out
        for i in range(0, len(cands), 800):
            chunk = cands[i: i + 800]
            qs = ",".join(["?"] * len(chunk))
            for row in self.conn.execute(
                f"SELECT stable_id, ecli FROM documents WHERE stable_id IN ({qs}) OR ecli IN ({qs})",
                (*chunk, *chunk),
            ).fetchall():
                if row["stable_id"] in candidates:
                    out[row["stable_id"]] = row["stable_id"]
                if row["ecli"] and row["ecli"] in candidates:
                    out[row["ecli"]] = row["stable_id"]
        # remaining candidates: resolve via aliases, then confirm the target exists
        remaining = [c for c in cands if c not in out]
        if remaining:
            folds = {c: c.casefold() for c in remaining}
            keys = list({*remaining, *folds.values()})
            alias_dst: dict[str, str] = {}
            for i in range(0, len(keys), 800):
                chunk = keys[i: i + 800]
                qs = ",".join(["?"] * len(chunk))
                for row in self.conn.execute(
                    f"SELECT alias, dst_id FROM citation_aliases WHERE alias IN ({qs})", chunk
                ).fetchall():
                    alias_dst[row["alias"]] = row["dst_id"]
            wanted = {c: (alias_dst.get(c) or alias_dst.get(folds[c])) for c in remaining}
            present = self.find_existing([d for d in wanted.values() if d]) if any(wanted.values()) else {}
            for c, dst in wanted.items():
                if dst and dst in present:
                    out[c] = present[dst]
        return out

    def pending_relations(self) -> list[sqlite3.Row]:
        """Edges still carrying a raw string but no resolved node (§5b)."""
        return self.conn.execute(
            """
            SELECT * FROM relations
            WHERE resolution_status = 'pending' AND raw_citation_string IS NOT NULL
            """
        ).fetchall()

    def backfill_alias_from_meta(self) -> dict:
        """Mint the resolution aliases that already-held documents imply but which were
        never recorded — ECHR application numbers and cited-CELEX variants → the ECLI.
        A one-off for a corpus harvested before the alias-minting existed; new harvests
        get these at ingest. Returns counts by kind."""
        import re as _re

        if self.backend == "postgres":
            appno_expr = "meta_json::jsonb ->> 'appno'"
        else:
            appno_expr = "json_extract(meta_json, '$.appno')"
        minted = {"echr_appno": 0, "fr_number": 0, "fr_code_article": 0,
                  "de_case": 0, "de_law": 0}
        rows = self.conn.execute(
            f"SELECT ecli, {appno_expr} AS appno FROM documents "
            "WHERE source = 'echr' AND ecli IS NOT NULL AND meta_json IS NOT NULL"
        ).fetchall()
        with self._atomic():
            for r in rows:
                if not r["appno"]:
                    continue
                for a in _re.split(r"[;,]", str(r["appno"])):
                    a = a.strip().casefold()
                    if a:
                        self.conn.execute(
                            "INSERT INTO citation_aliases (alias, dst_id, source) VALUES (?,?,?) "
                            "ON CONFLICT(alias) DO UPDATE SET dst_id = excluded.dst_id, source = excluded.source",
                            (a, r["ecli"], "echr-appno"),
                        )
                        minted["echr_appno"] += 1
            # French bulk imports predate adapter-declared aliases.  Reconstruct the
            # deterministic keys from their persisted metadata so the new extractor can
            # immediately link against the already-held million-document corpus.
            from ..citations.french import code_article_alias, decision_alias, pourvoi_alias

            fr_rows = self.conn.execute(
                "SELECT stable_id, source, doc_type, title, landing_url, meta_json FROM documents "
                "WHERE source LIKE 'fr-%' AND meta_json IS NOT NULL"
            )
            for r in fr_rows:
                try:
                    meta = json.loads(r["meta_json"] or "{}")
                except (ValueError, TypeError):
                    meta = {}
                aliases: list[tuple[str, str]] = []
                native = _re.search(
                    r"/(?:juri|ceta|cons)/id/((?:JURI|CETA|CONS|CNIL)TEXT\d+)",
                    r["landing_url"] or "", _re.I)
                if native and native.group(1).upper() != r["stable_id"]:
                    aliases.append((native.group(1).upper(), "fr-legifrance-id"))
                number, fond = meta.get("number"), str(meta.get("fond") or "").upper()
                if number and (r["source"] == "fr-judilibre" or fond in ("CASS", "INCA")):
                    aliases.append((pourvoi_alias(str(number)), "fr-pourvoi"))
                elif number and fond in ("JADE", "CONSTIT", "CNIL"):
                    aliases.append((decision_alias(str(number)), "fr-decision"))
                if r["doc_type"] == "legislation":
                    m = _re.match(r"(.+?)\s+[—-]\s+Article\s+(.+)$", r["title"] or "", _re.I)
                    alias = code_article_alias(m.group(1), m.group(2)) if m else None
                    if alias:
                        aliases.append((alias, "fr-code-article"))
                for alias, source in aliases:
                    self.conn.execute(
                        "INSERT INTO citation_aliases (alias, dst_id, source) VALUES (?,?,?) "
                        "ON CONFLICT(alias) DO NOTHING", (alias.casefold(), r["stable_id"], source)
                    )
                    minted["fr_code_article" if source == "fr-code-article" else "fr_number"] += 1
            from ..citations.german import case_alias, law_id

            de_rows = self.conn.execute(
                "SELECT stable_id, source, doc_type, court, meta_json FROM documents "
                "WHERE source LIKE 'de-%' AND meta_json IS NOT NULL"
            )
            for r in de_rows:
                try:
                    meta = json.loads(r["meta_json"] or "{}")
                except (ValueError, TypeError):
                    meta = {}
                aliases: list[tuple[str, str]] = []
                jurabk = meta.get("jurabk")
                if jurabk and r["doc_type"] == "legislation":
                    aliases.append((law_id(str(jurabk)), "de-law"))
                dockets = meta.get("file_numbers") or meta.get("aktenzeichen") or []
                if isinstance(dockets, str):
                    dockets = [dockets]
                for docket in dockets:
                    if docket and r["court"]:
                        aliases.append((case_alias(r["court"], str(docket)), "de-case"))
                for alias, source in aliases:
                    self.conn.execute(
                        "INSERT INTO citation_aliases (alias, dst_id, source) VALUES (?,?,?) "
                        "ON CONFLICT(alias) DO NOTHING", (alias.casefold(), r["stable_id"], source)
                    )
                    minted["de_law" if source == "de-law" else "de_case"] += 1
        return minted

    def backfill_dutch_aliases(self) -> dict:
        """Mint Dutch aliases for records imported before the Dutch graph support.

        Kept separate from the general historical migration so deploying Dutch support
        does not repeat the multi-million-row French pass.
        """
        import re as _re
        from ..citations.dutch import law_name_alias, ljn_alias

        minted = {"ljn": 0, "bwb": 0, "law_name": 0}
        rows = self.conn.execute(
            "SELECT stable_id, source, doc_type, title, meta_json FROM documents "
            "WHERE source IN ('nl-rechtspraak','nl-legislation')"
        )
        with self._atomic():
            for r in rows:
                aliases: list[tuple[str, str]] = []
                if r["source"] == "nl-rechtspraak":
                    tail = r["stable_id"].rsplit(":", 1)[-1]
                    if _re.fullmatch(r"[A-Z]{2}\d{4}", tail, _re.I):
                        aliases.append((ljn_alias(tail), "ljn"))
                elif r["doc_type"] == "legislation":
                    base = r["stable_id"].split("@", 1)[0].upper()
                    if _re.fullmatch(r"BWB[RV]\d{7}", base):
                        aliases.append((f"jci1.3:c:{base}", "bwb"))
                    if r["title"]:
                        aliases.append((law_name_alias(r["title"]), "law_name"))
                for alias, kind in aliases:
                    self.conn.execute(
                        "INSERT INTO citation_aliases (alias, dst_id, source) VALUES (?,?,?) "
                        "ON CONFLICT(alias) DO NOTHING",
                        (alias.casefold(), r["stable_id"], f"nl-{kind}"),
                    )
                    minted[kind] += 1
        return minted

    def held_key_set(self) -> set[str]:
        """Every string that identifies a held document — stable_id, ECLI, and the aliases
        pointing at one (CELEX/chamber-less/named). The snowball tests ~165k frontier
        candidates for held-ness; doing that as a set membership after two cheap scans is
        seconds, where 165k point lookups (or 200 batched OR-queries + recursion) was a
        minute-plus. Alias keys are folded, matching how citations resolve."""
        held: set[str] = set()
        for r in self.conn.execute("SELECT stable_id, ecli FROM documents"):
            held.add(r["stable_id"])
            if r["ecli"]:
                held.add(r["ecli"])
        # an alias counts as "held" only if its target is a held document
        for r in self.conn.execute("SELECT alias, dst_id FROM citation_aliases"):
            if r["dst_id"] in held:
                held.add(r["alias"])
        return held

    # -- set-based resolution (§5b) -----------------------------------------
    # Each pass flips whole classes of pending edges live in ONE statement. The old
    # per-edge Python loop re-derived a candidate id for 450k edges every scheduler
    # tick to usually resolve nothing; these run off the persisted candidate_id /
    # raw_fold and their partial indexes.
    _RESOLVE_PASSES = (
        # 1. the candidate IS a document (by stable_id or ECLI) — the common case
        """
        UPDATE relations SET dst_id = d.stable_id, resolution_status = 'resolved'
        FROM documents d
        WHERE relations.resolution_status = 'pending'
          AND relations.candidate_id IS NOT NULL
          AND (d.stable_id = relations.candidate_id OR d.ecli = relations.candidate_id)
        """,
        # 2. the candidate is an alias of a document (CELEX→ECLI, chamber-less slug)
        """
        UPDATE relations SET dst_id = d.stable_id, resolution_status = 'resolved'
        FROM citation_aliases a JOIN documents d
          ON (d.stable_id = a.dst_id OR d.ecli = a.dst_id)
        WHERE relations.resolution_status = 'pending'
          AND relations.candidate_id IS NOT NULL
          AND a.alias = lower(relations.candidate_id)
        """,
        # 3. the raw string is a named alias ("UK GDPR" → the assimilated regulation)
        """
        UPDATE relations SET dst_id = d.stable_id, resolution_status = 'resolved'
        FROM citation_aliases a JOIN documents d
          ON (d.stable_id = a.dst_id OR d.ecli = a.dst_id)
        WHERE relations.resolution_status = 'pending'
          AND relations.raw_fold IS NOT NULL
          AND a.alias = relations.raw_fold
        """,
    )

    def resolve_pending(self) -> int:
        """Flip every pending edge whose target is now a node. Returns the number
        resolved. Idempotent and safe to re-run after each ingest — that is how a
        citation to a freshly-harvested target becomes a live edge (§5b)."""
        total = 0
        with self._atomic():
            for sql in self._RESOLVE_PASSES:
                cur = self.conn.execute(sql)
                total += max(cur.rowcount, 0)
        return total

    def pending_relation_batch(self, after_id: int, *, through_id: int,
                               batch_size: int = 50000) -> tuple[int, int] | None:
        """Return ``(first_id, last_id)`` for the next bounded relation-id window.

        The window is based on *all* relations, not only rows that are currently
        pending. Otherwise a batch containing permanently-unresolvable references
        would be selected forever. A fixed ``through_id`` snapshots the graph at job
        start; edges arriving concurrently belong to the next run.
        """
        rows = self.conn.execute(
            """
            SELECT relation_id FROM relations
            WHERE relation_id > ? AND relation_id <= ?
            ORDER BY relation_id
            LIMIT ?
            """,
            (after_id, through_id, batch_size),
        ).fetchall()
        if not rows:
            return None
        return int(rows[0]["relation_id"]), int(rows[-1]["relation_id"])

    def max_relation_id(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(relation_id), 0) AS n FROM relations").fetchone()
        return int(row["n"] if row else 0)

    def resolve_pending_range(self, first_id: int, last_id: int) -> int:
        """Resolve pending edges inside one durable relation-id range.

        This is the bulk-import counterpart to ``resolve_pending_for``. Three
        set-based joins over 50k rows are fast and bounded; calling the target-side
        resolver once for each of 1.7m imported documents caused months of repeated
        scans over the same pending-edge indexes.
        """
        passes = (
            """
            UPDATE relations SET dst_id = d.stable_id, resolution_status = 'resolved'
            FROM documents d
            WHERE relations.relation_id >= ? AND relations.relation_id <= ?
              AND relations.resolution_status = 'pending'
              AND relations.candidate_id IS NOT NULL
              AND (d.stable_id = relations.candidate_id OR d.ecli = relations.candidate_id)
            """,
            """
            UPDATE relations SET dst_id = d.stable_id, resolution_status = 'resolved'
            FROM citation_aliases a JOIN documents d
              ON (d.stable_id = a.dst_id OR d.ecli = a.dst_id)
            WHERE relations.relation_id >= ? AND relations.relation_id <= ?
              AND relations.resolution_status = 'pending'
              AND relations.candidate_id IS NOT NULL
              AND a.alias = lower(relations.candidate_id)
            """,
            """
            UPDATE relations SET dst_id = d.stable_id, resolution_status = 'resolved'
            FROM citation_aliases a JOIN documents d
              ON (d.stable_id = a.dst_id OR d.ecli = a.dst_id)
            WHERE relations.relation_id >= ? AND relations.relation_id <= ?
              AND relations.resolution_status = 'pending'
              AND relations.raw_fold IS NOT NULL
              AND a.alias = relations.raw_fold
            """,
        )
        total = 0
        with self._atomic():
            for sql in passes:
                cur = self.conn.execute(sql, (first_id, last_id))
                total += max(cur.rowcount, 0)
        return total

    def resolve_pending_for(self, stable_id: str, ecli: str | None = None) -> int:
        """The incremental case: only edges pointing at THIS document (just harvested)
        can newly resolve, so a few indexed lookups replace a whole-graph pass.

        THREE SEPARATE UPDATES, never one OR — the same rule as resolve_pending_from
        and the search OR-join fix. OR-ing the direct-candidate hit with two alias
        subqueries stopped the planner decomposing onto the partial pending indexes:
        it evaluated hashed subplans across every pending row (3.2M) per document,
        which turned a bulk harvest's resolve phase into 40 seconds *per document*
        ("frozen at 1/4143"). Split, each pass is a handful of index probes: the
        direct pass hits relations_pending_candidate_idx, and the alias passes
        nested-loop from this document's few aliases (citation_aliases_dst_idx) into
        the pending lower(candidate_id)/raw_fold indexes."""
        keys = [k for k in (stable_id, ecli) if k]
        qs = ",".join("?" * len(keys))
        passes = (
            (
                f"""
                UPDATE relations SET dst_id = ?, resolution_status = 'resolved'
                WHERE resolution_status = 'pending' AND candidate_id IN ({qs})
                """,
                (stable_id, *keys),
            ),
            (
                f"""
                UPDATE relations SET dst_id = ?, resolution_status = 'resolved'
                FROM citation_aliases a
                WHERE a.dst_id IN ({qs})
                  AND relations.resolution_status = 'pending'
                  AND lower(relations.candidate_id) = a.alias
                """,
                (stable_id, *keys),
            ),
            (
                f"""
                UPDATE relations SET dst_id = ?, resolution_status = 'resolved'
                FROM citation_aliases a
                WHERE a.dst_id IN ({qs})
                  AND relations.resolution_status = 'pending'
                  AND relations.raw_fold = a.alias
                """,
                (stable_id, *keys),
            ),
        )
        total = 0
        with self._atomic():
            for sql, params in passes:
                cur = self.conn.execute(sql, params)
                total += max(cur.rowcount, 0)
        return total

    def resolve_pending_from(self, stable_id: str) -> int:
        """Resolve pending outgoing edges from one newly extracted document.

        Extraction already persisted ``candidate_id``/``raw_fold``.  Restricting the
        usual three resolution joins by ``src_id`` keeps ingest proportional to the new
        document instead of rescanning the multi-million-edge graph after every fetch.
        """
        passes = (
            """
            UPDATE relations SET dst_id = d.stable_id, resolution_status = 'resolved'
            FROM documents d
            WHERE relations.src_id = ?
              AND relations.resolution_status = 'pending'
              AND relations.candidate_id IS NOT NULL
              AND (d.stable_id = relations.candidate_id OR d.ecli = relations.candidate_id)
            """,
            """
            UPDATE relations SET dst_id = d.stable_id, resolution_status = 'resolved'
            FROM citation_aliases a JOIN documents d
              ON (d.stable_id = a.dst_id OR d.ecli = a.dst_id)
            WHERE relations.src_id = ?
              AND relations.resolution_status = 'pending'
              AND relations.candidate_id IS NOT NULL
              AND a.alias = lower(relations.candidate_id)
            """,
            """
            UPDATE relations SET dst_id = d.stable_id, resolution_status = 'resolved'
            FROM citation_aliases a JOIN documents d
              ON (d.stable_id = a.dst_id OR d.ecli = a.dst_id)
            WHERE relations.src_id = ?
              AND relations.resolution_status = 'pending'
              AND relations.raw_fold IS NOT NULL
              AND a.alias = relations.raw_fold
            """,
        )
        total = 0
        with self._atomic():
            for sql in passes:
                cur = self.conn.execute(sql, (stable_id,))
                total += max(cur.rowcount, 0)
        return total

    def backfill_edge_keys(self, *, batch: int = 20000, on_progress=None) -> int:
        """Populate ``candidate_id``/``raw_fold`` on edges written before those columns
        existed. Runs the matcher ladder once per DISTINCT raw string (a few hundred
        thousand) rather than once per edge (millions), then updates by string.

        The per-string UPDATE keys on ``raw_citation_string``, which isn't indexed in
        steady state (candidate_id is the hot column), so over millions of edges that would
        be a full scan each. Build a throwaway index for the duration and drop it after."""
        from ..resolve.matchers import normalise_candidate
        from ..core.text import fold

        temp_index = False
        try:
            # CONCURRENTLY can't run inside a txn; the connection is autocommit on PG.
            concurrently = "CONCURRENTLY" if self.backend == "postgres" else ""
            self.conn.execute(
                f"CREATE INDEX {concurrently} IF NOT EXISTS tmp_relations_rawcite "
                "ON relations (raw_citation_string)"
            )
            temp_index = True
        except Exception:  # noqa: BLE001 — the backfill is correct without it, just slower
            pass

        rows = self.conn.execute(
            "SELECT DISTINCT raw_citation_string AS raw, dst_id FROM relations "
            "WHERE raw_fold IS NULL AND raw_citation_string IS NOT NULL"
        ).fetchall()
        done = 0
        # ``CAST(? AS TEXT) IS NULL`` — a bare parameter beside IS NULL has no type for the
        # Postgres planner to infer ("could not determine data type of parameter"); the cast
        # gives it one. SQLite is untyped and tolerated the bare form.
        null_clause = "CAST(? AS TEXT) IS NULL" if self.backend == "postgres" else "? IS NULL"
        for i in range(0, len(rows), batch):
            with self._atomic():
                for r in rows[i: i + batch]:
                    raw, dst = r["raw"], r["dst_id"]
                    self.conn.execute(
                        "UPDATE relations SET candidate_id = ?, raw_fold = ? "
                        "WHERE raw_citation_string = ? AND raw_fold IS NULL "
                        f"AND (dst_id = ? OR (dst_id IS NULL AND {null_clause}))",
                        (normalise_candidate(dst, raw), fold(raw), raw, dst, dst),
                    )
                    done += 1
            if on_progress:
                on_progress(stage="backfilling edge keys", done=done, total=len(rows))
        # Edges with no raw string at all (adapter-supplied dst only) still want a candidate.
        with self._atomic():
            self.conn.execute(
                "UPDATE relations SET candidate_id = dst_id "
                "WHERE candidate_id IS NULL AND dst_id IS NOT NULL"
            )
        if temp_index:
            try:
                self.conn.execute("DROP INDEX IF EXISTS tmp_relations_rawcite")
            except Exception:  # noqa: BLE001
                pass
        return done

    def unheld_case_candidates(self, *, limit: int = 5000) -> list[sqlite3.Row]:
        """Distinct case references the corpus cites but does **not** hold, most-cited
        first — the target list for building outbound LII links.

        Only candidate-shaped references (a neutral-citation slug like ``nzhc/2012/2551``)
        qualify: a bare report-series string carries no court/year/number and so has no
        derivable URL. ``inferred`` carry-forwards are excluded for the same reason they
        never enter the worklist — too ambiguous to act on."""
        return self.conn.execute(
            """
            SELECT r.candidate_id                 AS candidate,
                   MIN(r.raw_citation_string)     AS raw,
                   COUNT(*)                       AS occurrences,
                   COUNT(DISTINCT r.src_id)       AS citing_count
            FROM relations r
            LEFT JOIN documents d ON d.stable_id = r.candidate_id
            WHERE r.resolution_status = 'pending'
              AND r.extracted_via <> 'inferred'
              AND r.candidate_id IS NOT NULL
              AND d.stable_id IS NULL
            GROUP BY r.candidate_id
            ORDER BY citing_count DESC, r.candidate_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def textless_case_documents(self, *, limit: int = 5000) -> list[sqlite3.Row]:
        """Held judgments with no extracted text — the name-only/stub records whose full
        text has to come from somewhere else. Ordered by how often they are cited, so the
        ones worth chasing first come first."""
        return self.conn.execute(
            """
            SELECT d.stable_id, d.title, d.court, d.source, d.landing_url,
                   (SELECT COUNT(*) FROM relations r
                     WHERE r.candidate_id = d.stable_id) AS citing_count
            FROM documents d
            WHERE d.is_latest = 1 AND d.has_text = 0 AND d.doc_type = 'judgment'
            ORDER BY citing_count DESC, d.stable_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def held_extraction_state(self, ids: list[str]) -> dict[str, bool]:
        """``{stable_id: has_extraction_stamp}`` for the HELD subset of ``ids`` — the
        pipeline's batched dedup prefilter. A bulk backfill's resume pass re-walks a
        source's whole catalogue mostly re-seeing held documents; one point SELECT
        per stub made that walk run at ~20 stubs/s against Postgres (a multi-hour
        no-op over a 300k-item TOC). One IN-query per chunk instead."""
        out: dict[str, bool] = {}
        ids = [i for i in dict.fromkeys(ids) if i]
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            qs = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                    f"SELECT stable_id, last_extracted_at FROM documents "
                    f"WHERE stable_id IN ({qs})", chunk).fetchall():
                out[r["stable_id"]] = bool(r["last_extracted_at"])
        return out

    def alias_targets(self, refs: list[str]) -> dict[str, str]:
        """``{ref: dst_id}`` for refs that resolve through ``citation_aliases`` — the
        pipeline prefilter's third rung, for adapters whose stub id is an upstream
        surrogate of a held document (de-rii's doknr → the ECLI it's held under).
        Keys are matched the way put_alias stores them (fold_citation), and the
        ORIGINAL ref spelling keys the result so the caller needn't re-fold."""
        from ..core.text import fold_citation

        refs = [r for r in dict.fromkeys(refs) if r]
        if not refs:
            return {}
        folded = {fold_citation(r) or r: r for r in refs}
        out: dict[str, str] = {}
        keys = list(folded)
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            qs = ",".join("?" * len(chunk))
            for row in self.conn.execute(
                    f"SELECT alias, dst_id FROM citation_aliases WHERE alias IN ({qs})",
                    chunk).fetchall():
                out[folded[row["alias"]]] = row["dst_id"]
        return out

    def document_ids_by_landing_urls(self, urls: list[str]) -> dict[str, str]:
        """``{landing_url: stable_id}`` for the held subset — the batched form of
        document_id_by_landing_url, for adapters whose stub id is provisional until
        the document is fetched (NZ)."""
        out: dict[str, str] = {}
        urls = [u for u in dict.fromkeys(urls) if u]
        for i in range(0, len(urls), 400):
            chunk = urls[i:i + 400]
            qs = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                    f"SELECT stable_id, landing_url FROM documents "
                    f"WHERE landing_url IN ({qs})", chunk).fetchall():
                out[r["landing_url"]] = r["stable_id"]
        return out

    def canadian_unenriched_documents(self, *, limit: int = 500) -> list[sqlite3.Row]:
        """Held Canadian judgments not yet checked against CanLII — the enrichment
        queue for ``canlii_enrich``, most-cited first (via the citation_counts rollup)
        so the metered API budget goes to the cases the corpus actually leans on.

        The marker is ``canlii_checked_at`` in ``meta_json`` — stamped whether the
        lookup hit or missed, so a case CanLII doesn't hold isn't re-asked every run.
        The LIKE pattern is bound as a parameter (a literal ``%`` in the SQL string
        breaks the postgres driver's paramstyle translation)."""
        return self.conn.execute(
            """
            SELECT d.stable_id, d.title, d.court, d.source
            FROM documents d
            LEFT JOIN citation_counts cc ON cc.candidate_id = d.stable_id
            WHERE d.is_latest = 1 AND d.doc_type = 'judgment'
              AND d.source IN ('ca-caselaw', 'ca-canlii')
              AND (d.meta_json IS NULL OR d.meta_json NOT LIKE ?)
            ORDER BY COALESCE(cc.occurrences, 0) DESC, d.stable_id
            LIMIT ?
            """,
            ("%canlii_checked_at%", limit),
        ).fetchall()

    def pending_reference_groups(self, *, min_citing: int = 1, limit: int | None = None,
                                 need_echr: bool = True) -> list[sqlite3.Row]:
        """One row per distinct hanging reference — the worklist, as a single GROUP BY
        instead of a 450k-row Python pass (§5b, §8).

        ``inferred`` edges are heuristic carry-forwards (a bare "Section 12" pinned to the
        last-named Act); useful as in-document pinpoints, too ambiguous to drive harvesting,
        so they never enter the worklist. ``echr_citing`` says whether any citing document
        is a Strasbourg one — a bare ``115/92`` is an ECtHR application number there and an
        old CJEU case number anywhere else, and nothing but the citing document tells them
        apart.

        Three knobs, because the unbounded form got expensive as the corpus grew — it
        aggregates 1.8M pending edges into ~517k groups and ships every one to Python:

        * ``need_echr=False`` drops the join to ``documents``, which exists *only* to
          compute ``echr_citing``. That join is a nested loop over 1.8M rows and costs
          about 10 of the query's 16 seconds; callers that don't read the flag shouldn't
          pay for it.
        * ``min_citing`` filters in SQL. 70% of these groups are cited exactly once, so a
          "most-cited" view can never show them — classifying them in Python is pure waste.
        * ``limit`` caps the scan. The ORDER BY is already ``citing_count DESC``, so a
          bounded read takes the top of the ranking rather than an arbitrary slice.
        """
        agg = "string_agg(DISTINCT r.extracted_via, ',')" if self.backend == "postgres" \
            else "group_concat(DISTINCT r.extracted_via)"
        echr_select = ("MAX(CASE WHEN d.source = 'echr' THEN 1 ELSE 0 END) AS echr_citing"
                       if need_echr else "0 AS echr_citing")
        join = "JOIN documents d ON d.stable_id = r.src_id" if need_echr else ""
        having = "HAVING COUNT(DISTINCT r.src_id) >= ?" if min_citing > 1 else ""
        params: list = [min_citing] if min_citing > 1 else []
        tail = ""
        if limit is not None:
            tail = "LIMIT ?"
            params.append(int(limit))
        return self.conn.execute(
            f"""
            SELECT COALESCE(r.candidate_id, r.raw_citation_string) AS ref,
                   MAX(r.candidate_id)          AS candidate,
                   MIN(r.raw_citation_string)   AS raw,
                   MIN(r.dst_anchor)            AS anchor,
                   {agg}                        AS methods,
                   COUNT(*)                     AS occurrences,
                   COUNT(DISTINCT r.src_id)     AS citing_count,
                   {echr_select}
            FROM relations r
            {join}
            WHERE r.resolution_status = 'pending'
              AND r.extracted_via <> 'inferred'
              AND COALESCE(r.candidate_id, r.raw_citation_string) IS NOT NULL
            GROUP BY COALESCE(r.candidate_id, r.raw_citation_string)
            {having}
            ORDER BY citing_count DESC
            {tail}
            """, params
        ).fetchall()

    def report_citation_contexts(self, *, limit: int = 5000) -> list[sqlite3.Row]:
        """Occurrences of law-report citations that are still unresolved — the raw string,
        the citing document and the char span — so the report matcher can read the case
        name the citing text puts next to each one. Report citations are candidate-less
        (method ``law_report*``) and recorded in the ``citations`` audit table."""
        # bind the LIKE pattern as a param — a literal '%' in the SQL collides with the
        # Postgres shim's ? → %s placeholder translation (the pg-like-placeholder gotcha).
        return self.conn.execute(
            "SELECT c.raw, c.src_id, c.char_start FROM citations c "
            "WHERE c.method LIKE ? AND c.candidate_id IS NULL "
            "ORDER BY c.src_id LIMIT ?",
            ("law_report%", limit),
        ).fetchall()

    def docs_with_citations(self, *, min_count: int = 2, limit: int | None = None) -> list[str]:
        """Documents holding at least ``min_count`` citation occurrences with char spans —
        the candidates for parallel-citation mining (a lone citation has no neighbour to be
        parallel to). One aggregate scan of the citations table."""
        sql = ("SELECT src_id FROM citations WHERE char_start IS NOT NULL "
               "GROUP BY src_id HAVING COUNT(*) >= ? ORDER BY src_id")
        params: list = [min_count]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [r["src_id"] for r in self.conn.execute(sql, params).fetchall()]

    def citation_occurrences(self, src_id: str) -> list[sqlite3.Row]:
        """One document's citation occurrences (raw string + char span), in reading order —
        so the miner can see which citations sit adjacent to each other in the text."""
        return self.conn.execute(
            "SELECT raw, char_start, char_end, candidate_id, entity_kind FROM citations "
            "WHERE src_id = ? AND char_start IS NOT NULL ORDER BY char_start",
            (src_id,),
        ).fetchall()

    def judgment_pool(self) -> list[sqlite3.Row]:
        """Harvested judgments as (stable_id, title, decision_date, source) — the candidate
        pool the report matcher scores a "[1998] AC 1" against by name + year (source →
        jurisdiction, so an ALR citation only scores against Australian candidates)."""
        return self.conn.execute(
            "SELECT stable_id, title, decision_date, source FROM documents "
            "WHERE doc_type = 'judgment' AND title IS NOT NULL"
        ).fetchall()

    def text_document_ids(self, *, limit: int | None = None,
                          doc_types: list[str] | None = None,
                          source: str | None = None,
                          only_unextracted: bool = False,
                          only_never_extracted: bool = False,
                          stale_days: int | None = None,
                          exclude_extraction_run_id: str | None = None) -> list[str]:
        """Document ids that have extractable text, in id order — the target set for a
        re-extraction. ``doc_types`` scopes it (e.g. ``['judgment']`` to skip the 122k
        legislation docs, which mostly cite only other legislation); ``source`` scopes it
        to one adapter (e.g. re-extract just the freshly-imported ``ca-caselaw`` after a
        new grammar lands, instead of the whole 700k-doc corpus). A single cheap
        single-column scan (no row bodies), so it streams 200k+ ids without their metadata.

        ``only_unextracted`` narrows it to documents that have **no citation rows at all** —
        the resume set. A bulk import that dies partway (or is OOM-killed) leaves thousands
        of documents with text but no edges, and a plain re-run would redo the whole corpus
        to reach them; this selects exactly the backlog, so re-running is cheap and
        convergent. It is deliberately "no rows at all" rather than a timestamp check: a
        document that genuinely cites nothing is re-tried each run, which is far cheaper
        than the alternative of re-extracting everything.

        ``stale_days`` narrows it to documents **not extracted within the last N days** —
        the "avoid re-doing everything on restart" set. Freshness is read from two
        signals, so it works *retroactively* on data that predates the durable stamp: the
        ``last_extracted_at`` column (set going forward on every extraction) OR the newest
        ``citations.created_at`` for the document (``extract_document`` clears+reinserts
        citation rows each run, so that timestamp already tracks the last extraction —
        including the rescan running right now). A document is skipped when *either* signal
        is within the window. A genuinely citation-less document that has never been
        stamped counts as stale (re-tried), same tradeoff as ``only_unextracted``."""
        sql = "SELECT d.stable_id FROM documents d WHERE d.has_text = 1"
        params: list = []
        if doc_types:
            sql += f" AND d.doc_type IN ({','.join('?' * len(doc_types))})"
            params.extend(doc_types)
        if source:
            sql += " AND d.source = ?"
            params.append(source)
        if exclude_extraction_run_id:
            sql += " AND (d.last_extraction_run_id IS NULL OR d.last_extraction_run_id <> ?)"
            params.append(exclude_extraction_run_id)
        if only_unextracted:
            sql += " AND NOT EXISTS (SELECT 1 FROM citations c WHERE c.src_id = d.stable_id)"
        if only_never_extracted:
            # Unlike ``only_unextracted`` this uses the durable completion stamp, so
            # a legitimately citation-free document is not selected again. This is
            # the exact recovery backlog after a bulk harvest stored text but the
            # process restarted before its extraction phase.
            sql += " AND d.last_extracted_at IS NULL"
        if stale_days is not None:
            cutoff = _iso_days_ago(stale_days)
            # fresh = stamped recently OR has a recently-created citation row → skip it.
            sql += (" AND (d.last_extracted_at IS NULL OR d.last_extracted_at < ?)"
                    " AND NOT EXISTS (SELECT 1 FROM citations c"
                    " WHERE c.src_id = d.stable_id AND c.created_at >= ?)")
            params.extend([cutoff, cutoff])
        # Never-extracted documents FIRST, then least-recently-extracted, so an
        # interrupted or time-boxed run always makes progress on what has no edges
        # yet before re-touching what already does. NULLS FIRST is honoured by both
        # backends (sqlite ≥3.30, postgres). A doc extracted before the durable
        # stamp existed reads as NULL and sorts early — it gets one stamped pass,
        # after which it orders by its real recency.
        sql += " ORDER BY d.last_extracted_at ASC NULLS FIRST, d.stable_id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [r["stable_id"] for r in self.conn.execute(sql, params)]

    def held_legislation_titles(self) -> list[sqlite3.Row]:
        """Every held piece of legislation as (stable_id, title) — the self-maintaining
        gazetteer the name-only statute matcher resolves against. Because it's derived from
        what's actually been harvested, it never goes stale and covers every Act fetched
        (unlike the bundled offline list)."""
        return self.conn.execute(
            "SELECT stable_id, title FROM documents "
            "WHERE doc_type = 'legislation' AND title IS NOT NULL"
        ).fetchall()

    def pending_statute_refs(self, *, limit: int | None = None) -> list[sqlite3.Row]:
        """Distinct still-pending, candidate-less citation strings that look like a named
        statute ("… Act 1984", "… Regulations 2004", "… Order 2015"), most-cited first —
        the references the name-only legislation matcher tries to resolve. LIKE patterns are
        bound as params (the pg literal-% gotcha).

        ``limit=None`` (default) returns ALL of them: the matcher does one cheap dict
        lookup per reference, so an arbitrary cap only leaves the tail unresolved — the
        live corpus has ~112k distinct such references, and the old 20k cap silently
        dropped ~92k of them, so most name-only legislation never got linked."""
        like = ["%Act 1%", "%Act 2%", "%Regulations 1%", "%Regulations 2%",
                "%Order 1%", "%Order 2%", "%Rules 1%", "%Rules 2%", "%Measure 1%", "%Measure 2%"]
        clause = " OR ".join(["raw_citation_string LIKE ?"] * len(like))
        tail = " LIMIT ?" if limit is not None else ""
        params: tuple = (*like, limit) if limit is not None else (*like,)
        return self.conn.execute(
            f"SELECT raw_citation_string AS raw, COUNT(*) AS n FROM relations "
            f"WHERE resolution_status = 'pending' AND candidate_id IS NULL "
            f"AND raw_citation_string IS NOT NULL AND ({clause}) "
            f"GROUP BY raw_citation_string ORDER BY n DESC{tail}",
            params,
        ).fetchall()

    def echr_pool(self) -> list[sqlite3.Row]:
        """Held ECtHR cases as (stable_id, title, decision_date, appno) — the pool the EHRR
        matcher scores "Soering v United Kingdom (1989) 11 EHRR 349" against by name+year."""
        appno = ("meta_json::jsonb ->> 'appno'" if self.backend == "postgres"
                 else "json_extract(meta_json, '$.appno')")
        return self.conn.execute(
            f"SELECT stable_id, title, decision_date, {appno} AS appno FROM documents "
            "WHERE source = 'echr' AND title IS NOT NULL"
        ).fetchall()

    def pending_echr_name_refs(self, *, limit: int = 500) -> list[str]:
        """Distinct still-pending ``echr:<case name>`` candidates, most-cited first — the
        ECtHR cases the corpus references (by name/EHRR) but doesn't hold, to be harvested
        from HUDOC by docname search. LIKE pattern bound as a param (pg literal-% gotcha)."""
        rows = self.conn.execute(
            "SELECT candidate_id, COUNT(*) AS n FROM relations "
            "WHERE resolution_status = 'pending' AND candidate_id LIKE ? "
            "GROUP BY candidate_id ORDER BY n DESC LIMIT ?",
            ("echr:%", limit),
        ).fetchall()
        return [r["candidate_id"] for r in rows]

    def echr_report_refs(self, *, limit: int = 8000) -> list[sqlite3.Row]:
        """ECtHR-by-name/EHRR citation occurrences (the grammar tags these ``echr_report``
        with a name-keyed ``echr:<name>`` candidate). The name is in the candidate and the
        year in the raw, so the EHRR matcher needs no text I/O."""
        return self.conn.execute(
            "SELECT DISTINCT raw, candidate_id FROM citations WHERE method = 'echr_report' "
            "LIMIT ?",
            (limit,),
        ).fetchall()

    def citing_documents(self, ref: str, *, limit: int = 10) -> list[str]:
        """Which documents cite one hanging reference (for the worklist row's detail)."""
        rows = self.conn.execute(
            "SELECT DISTINCT src_id FROM relations "
            "WHERE resolution_status = 'pending' AND extracted_via <> 'inferred' "
            "AND COALESCE(candidate_id, raw_citation_string) = ? ORDER BY src_id LIMIT ?",
            (ref, limit),
        ).fetchall()
        return [r["src_id"] for r in rows]

    def citing_documents_for(self, refs: list[str], *, per_ref: int = 10) -> dict[str, list[str]]:
        """Citing documents for MANY hanging references in one scan. Matching on the
        ``COALESCE(candidate_id, raw_citation_string)`` expression isn't indexable, so
        doing it once for the visible page beats one seq-scan of the pending edges per
        row (which made the worklist endpoint take 20s+)."""
        refs = [r for r in dict.fromkeys(refs) if r]
        if not refs:
            return {}
        out: dict[str, list[str]] = {r: [] for r in refs}
        for i in range(0, len(refs), 800):
            chunk = refs[i: i + 800]
            qs = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT DISTINCT COALESCE(candidate_id, raw_citation_string) AS ref, src_id "
                f"FROM relations WHERE resolution_status = 'pending' AND extracted_via <> 'inferred' "
                f"AND COALESCE(candidate_id, raw_citation_string) IN ({qs})",
                chunk,
            ).fetchall()
            for r in rows:
                bucket = out.get(r["ref"])
                if bucket is not None and len(bucket) < per_ref and r["src_id"]:
                    bucket.append(r["src_id"])
        return {k: sorted(v) for k, v in out.items()}

    def set_pending_candidate(self, ref: str, new_candidate: str) -> int:
        """Re-key the *pending* edges of a hanging reference to a new candidate id —
        the manual-resolution counterpart of automatic resolution (§5b). ``ref`` is
        either the existing candidate (``dst_id``) or, for a reference recognised by
        name only (no candidate), its raw citation string. Used when a user supplies
        the missing identifier (a neutral citation / ECLI) or points the reference at
        a freshly-imported document. Resolution then links it like any other."""
        # candidate_id is what resolution keys off (§5b), so re-key it alongside dst_id —
        # otherwise the user supplies the missing identifier and the edge stays pending
        # against its old, unresolvable candidate.
        cur = self.conn.execute(
            """
            UPDATE relations SET dst_id = ?, candidate_id = ?
            WHERE resolution_status = 'pending'
              AND (dst_id = ? OR candidate_id = ?
                   OR (dst_id IS NULL AND raw_citation_string = ?))
            """,
            (new_candidate, new_candidate, ref, ref, ref),
        )
        self.conn.commit()
        return cur.rowcount

    def commit(self) -> None:
        """Flush a batch of deferred writes (callers that pass ``commit=False`` in a
        tight loop call this once at the end — e.g. the resolver over thousands of
        pending edges, where a commit per edge dominates the runtime)."""
        self.conn.commit()

    def resolve_relation(self, relation_id: int, dst_id: str, *, commit: bool = True) -> None:
        """Turn a dangling edge into a live one. ``raw_citation_string`` is kept so
        a wrong match stays auditable and re-runnable (§5b)."""
        self.conn.execute(
            "UPDATE relations SET dst_id = ?, resolution_status = 'resolved' WHERE relation_id = ?",
            (dst_id, relation_id),
        )
        if commit:
            self.conn.commit()

    def aliases_to(self, targets: list[str]) -> list[sqlite3.Row]:
        """Every alias string pointing at any of ``targets`` (a doc's stable_id/ECLI) —
        the document's alternative citation forms (report cites, appnos, shorthands)."""
        targets = [t for t in targets if t]
        if not targets:
            return []
        qs = ",".join("?" * len(targets))
        return self.conn.execute(
            f"SELECT alias, source FROM citation_aliases WHERE dst_id IN ({qs}) ORDER BY alias",
            targets).fetchall()

    def get_alias(self, alias: str) -> str | None:
        row = self.conn.execute(
            "SELECT dst_id FROM citation_aliases WHERE alias = ?", (alias,)
        ).fetchone()
        if row:
            return row["dst_id"]
        # Reporter abbreviations are cited with and without full stops, and the two
        # fold to different keys — so "(1948) 1 K.B. 223" missed Wednesbury, which is
        # held under "(1948) 1 kb 223". Retry on the de-dotted key rather than
        # rewriting every stored alias: this also rescues aliases minted before the
        # write path normalised them.
        from ..core.text import fold_citation

        depunctuated = fold_citation(alias)
        if depunctuated == alias:
            return None
        row = self.conn.execute(
            "SELECT dst_id FROM citation_aliases WHERE alias = ?", (depunctuated,)
        ).fetchone()
        return row["dst_id"] if row else None

    # -- match suggestions (human-confirmable resolution, §5b) ----------------
    def put_suggestion(self, ref: str, suggested_id: str, *, kind: str, reason: str | None = None,
                       extracted_parties: str | None = None, context: str | None = None,
                       held: bool = True, score: float | None = None, commit: bool = True) -> bool:
        """Upsert a pending suggestion. Never resurrects one a human already accepted or
        rejected — a re-run of the suggester must not re-ask answered questions."""
        row = self.conn.execute(
            "SELECT status FROM match_suggestions WHERE ref = ? AND suggested_id = ?",
            (ref, suggested_id)).fetchone()
        if row and row["status"] != "pending":
            return False
        self.conn.execute(
            """
            INSERT INTO match_suggestions
                (ref, suggested_id, kind, reason, extracted_parties, context, held, score, status, created_at)
            VALUES (?,?,?,?,?,?,?,?,'pending',?)
            ON CONFLICT(ref, suggested_id) DO UPDATE SET
                kind = excluded.kind, reason = excluded.reason,
                extracted_parties = excluded.extracted_parties, context = excluded.context,
                held = excluded.held, score = excluded.score
            """,
            (ref, suggested_id, kind, reason, extracted_parties, context,
             1 if held else 0, score, _now()))
        if commit:
            self.conn.commit()
        return True

    def suggestions_for(self, refs: list[str]) -> dict[str, list[dict]]:
        """Pending suggestions for a set of worklist refs, best score first."""
        out: dict[str, list[dict]] = {}
        if not refs:
            return out
        order = "ORDER BY score DESC NULLS LAST" if self.backend == "postgres" else "ORDER BY score DESC"
        for i in range(0, len(refs), 200):
            chunk = refs[i: i + 200]
            qs = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                    f"SELECT * FROM match_suggestions WHERE status = 'pending' AND ref IN ({qs}) {order}",
                    chunk):
                out.setdefault(r["ref"], []).append(dict(r))
        return out

    def reference_occurrences(self, ref: str, ref_fold: str, *, limit: int = 6) -> list[dict]:
        """Where the corpus cites a hanging reference — src doc + the stored context
        span, for the suggestion-review popover ("show me the sentences that cite
        this before I confirm the match")."""
        rows = self.conn.execute(
            "SELECT src_id, raw_citation_string, context_start, context_end FROM relations "
            "WHERE (candidate_id = ? OR raw_fold = ?) AND resolution_status = 'pending' "
            "LIMIT ?", (ref, ref_fold, limit)).fetchall()
        return [dict(r) for r in rows]

    def set_suggestion_status(self, ref: str, suggested_id: str, status: str) -> int:
        cur = self.conn.execute(
            "UPDATE match_suggestions SET status = ? WHERE ref = ? AND suggested_id = ?",
            (status, ref, suggested_id))
        self.conn.commit()
        return cur.rowcount

    def count_pending_suggestions(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM match_suggestions WHERE status = 'pending'").fetchone()["n"]

    def pending_suggestions(self, *, limit: int = 500) -> list[dict]:
        """Every pending suggestion, best score first — the bulk-confirmation list the
        UI shows below the unfetchable frontier, so a human can sweep through all the
        naming candidates in one sitting instead of chasing them per-reference."""
        order = "ORDER BY score DESC NULLS LAST, ref" if self.backend == "postgres" \
            else "ORDER BY score DESC, ref"
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM match_suggestions WHERE status = 'pending' {order} LIMIT ?",
            (limit,))]

    # -- refinement flags (reader passages flagged for linking-logic review) --
    def add_refinement_flag(self, *, doc_id: str, selected_text: str, anchor: str | None = None,
                            context: str | None = None, current_links: str | None = None,
                            note: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO refinement_flags (doc_id, anchor, selected_text, context, current_links, note, status, created_at) "
            "VALUES (?,?,?,?,?,?,'open',?)",
            (doc_id, anchor, selected_text, context, current_links, note, _now()))
        self.conn.commit()

    def refinement_flags(self, *, status: str | None = "open", limit: int = 500) -> list[sqlite3.Row]:
        if status:
            return self.conn.execute(
                "SELECT * FROM refinement_flags WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        return self.conn.execute(
            "SELECT * FROM refinement_flags ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def set_refinement_flag(self, flag_id: int, status: str) -> int:
        cur = self.conn.execute(
            "UPDATE refinement_flags SET status = ? WHERE flag_id = ?", (status, flag_id))
        self.conn.commit()
        return cur.rowcount

    def delete_aliases_by_source(self, sources: tuple[str, ...], *, commit: bool = True) -> int:
        """Drop every alias a given minting pass wrote — used by passes that regenerate
        their aliases from scratch each run (parallel mining), so a bad alias from an
        earlier run self-heals instead of persisting forever."""
        qs = ",".join("?" * len(sources))
        cur = self.conn.execute(
            f"DELETE FROM citation_aliases WHERE source IN ({qs})", list(sources))
        if commit:
            self.conn.commit()
        return cur.rowcount

    def put_alias(self, alias: str, dst_id: str, source: str | None = None, *, commit: bool = True) -> None:
        # Store on the de-dotted key so "K.B." and "KB" citations converge on one row
        # rather than each minting its own (and only one of them resolving).
        from ..core.text import fold_citation

        alias = fold_citation(alias) or alias
        self.conn.execute(
            """
            INSERT INTO citation_aliases (alias, dst_id, source) VALUES (?,?,?)
            ON CONFLICT(alias) DO UPDATE SET dst_id = excluded.dst_id, source = excluded.source
            """,
            (alias, dst_id, source),
        )
        if commit:
            self.conn.commit()

    # -- learned shorthands (corpus-wide, but gated at application time) -------
    def add_learned_shorthands(self, rows: list[dict], *, doc_id: str | None = None,
                               commit: bool = True) -> int:
        """Record shorthand definitions a document established. Each row: shorthand,
        candidate_id, entity_kind, is_abbrev.

        Insert-only (``ON CONFLICT DO NOTHING``): re-extracting a document must not
        rewrite rows it already wrote, because this runs inside the whole-corpus rescan
        where ~700k documents share one table. Returns the number of rows written."""
        rows = [r for r in rows if r.get("shorthand") and r.get("candidate_id")]
        if not rows:
            return 0
        now = _now()
        written = 0
        for r in rows:
            cur = self.conn.execute(
                """
                INSERT INTO learned_shorthands
                    (shorthand, candidate_id, entity_kind, is_abbrev, first_doc, created_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(shorthand, candidate_id) DO NOTHING
                """,
                (r["shorthand"], r["candidate_id"], r.get("entity_kind"),
                 1 if r.get("is_abbrev") else 0, doc_id, now),
            )
            written += max(cur.rowcount, 0)
        if commit:
            self.conn.commit()
        return written

    def learned_shorthand_map(self, *, limit: int = 400000) -> dict[str, list[tuple]]:
        """``{candidate_id: [(shorthand, entity_kind, is_abbrev), …]}`` — the whole store,
        loaded once and cached by the stage. Keyed by candidate because application is
        gated on the citing document already citing that candidate, so the caller only
        ever looks up ids it has in hand."""
        out: dict[str, list[tuple]] = {}
        for r in self.conn.execute(
                "SELECT shorthand, candidate_id, entity_kind, is_abbrev "
                "FROM learned_shorthands LIMIT ?", (limit,)):
            out.setdefault(r["candidate_id"], []).append(
                (r["shorthand"], r["entity_kind"], bool(r["is_abbrev"])))
        return out

    def count_learned_shorthands(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM learned_shorthands").fetchone()["n"]

    def list_named_aliases(self) -> list[sqlite3.Row]:
        """User-defined shorthand → document mappings (e.g. "UK GDPR" → its id). These
        are *rules*: the extractor links every occurrence of the phrase, so they
        propagate across the whole corpus on (re-)extraction."""
        return self.conn.execute(
            "SELECT alias, dst_id, source FROM citation_aliases WHERE source LIKE ? ORDER BY alias",
            ("named%",),
        ).fetchall()

    def named_alias_map(self) -> dict:
        """``{phrase: target_id}`` for the user rules — loaded by the extractor."""
        return {r["alias"]: r["dst_id"] for r in self.list_named_aliases()}

    def delete_alias(self, alias: str) -> None:
        self.conn.execute("DELETE FROM citation_aliases WHERE alias = ?", (alias,))
        self.conn.commit()

    # `pending_resolution` used to mirror the hanging references as its own table. Nothing
    # has written to it since the worklist became a live aggregate over `relations`, so it
    # only ever accumulated stale rows (135k of them in production) that no read consulted.
    # The relations graph is the single source of truth for what is unresolved.

    def resolution_worklist(self, limit: int = 50) -> list[sqlite3.Row]:
        """Most-cited unresolved citations first — what to harvest next (§8). Derived
        live from the relations graph (one aggregate query), so it's always correct and
        the resolver needn't maintain a separate worklist table on every run."""
        return self.conn.execute(
            """
            SELECT raw_citation_string, COUNT(*) AS cite_count
            FROM relations
            WHERE resolution_status = 'pending' AND raw_citation_string IS NOT NULL
            GROUP BY raw_citation_string
            ORDER BY cite_count DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    # -- source run-state / watermarks (§5) --------------------------------
    # -- watches (saved harvest plans, §5a) --------------------------------
    def add_watch(self, name: str, spec_json: str, cadence_minutes: int, *, enabled: bool = True) -> int:
        return self._insert_returning(
            """
            INSERT INTO watches (name, spec_json, cadence_minutes, enabled, created_at)
            VALUES (?,?,?,?,?)
            """,
            (name, spec_json, cadence_minutes, 1 if enabled else 0, _now()),
            "watch_id",
        )

    def list_watches(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM watches ORDER BY watch_id").fetchall()

    def get_watch(self, watch_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM watches WHERE watch_id = ?", (watch_id,)
        ).fetchone()

    def update_watch(self, watch_id: int, fields: dict) -> bool:
        allowed = {"name", "spec_json", "cadence_minutes", "enabled", "last_run_at", "last_result_json"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.conn.execute(
            f"UPDATE watches SET {cols} WHERE watch_id = ?", (*sets.values(), watch_id)
        )
        self.conn.commit()
        return True

    def delete_watch(self, watch_id: int) -> None:
        self.conn.execute("DELETE FROM watches WHERE watch_id = ?", (watch_id,))
        self.conn.commit()

    def get_watermark(self, source_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT watermark FROM sources WHERE key = ?", (source_key,)
        ).fetchone()
        return row["watermark"] if row else None

    def _ensure_source(self, source_key: str) -> None:
        self.conn.execute(
            "INSERT INTO sources (key) VALUES (?) ON CONFLICT(key) DO NOTHING", (source_key,)
        )

    def set_watermark(self, source_key: str, watermark: str) -> None:
        """Advance the cursor only after a clean run (§5) so a crash re-pulls
        rather than skips."""
        self._ensure_source(source_key)
        self.conn.execute(
            "UPDATE sources SET watermark = ? WHERE key = ?", (watermark, source_key)
        )
        self.conn.commit()

    def record_run(
        self, source_key: str, *, yielded: bool, failed: bool
    ) -> None:
        """Update the counters the §8 alerting layer watches."""
        self._ensure_source(source_key)
        now = _now()
        if failed:
            self.conn.execute(
                "UPDATE sources SET last_run = ?, consecutive_failures = consecutive_failures + 1 WHERE key = ?",
                (now, source_key),
            )
        else:
            last_yield = "last_yield_at = ?, " if yielded else ""
            params = ([now] if yielded else []) + [now, source_key]
            self.conn.execute(
                f"UPDATE sources SET {last_yield}consecutive_failures = 0, last_run = ? WHERE key = ?",
                params,
            )
        self.conn.commit()

    def source_state(self, source_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sources WHERE key = ?", (source_key,)
        ).fetchone()

    # -- rule-based tagging engine (§4a) -----------------------------------
    def add_rule(
        self,
        tag: str,
        condition_tree: dict,
        *,
        scope: dict | None = None,
        priority: int = 0,
        note: str | None = None,
    ) -> int:
        return self._insert_returning(
            """
            INSERT INTO tag_rules (tag, condition_tree_json, scope_json, priority, created_at, note)
            VALUES (?,?,?,?,?,?)
            """,
            (tag, json.dumps(condition_tree), json.dumps(scope or {}), priority, _now(), note),
            "rule_id",
        )

    def list_rules(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM tag_rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.conn.execute(sql + " ORDER BY priority DESC, rule_id ASC").fetchall()

    def get_rule(self, rule_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM tag_rules WHERE rule_id = ?", (rule_id,)
        ).fetchone()

    def iter_documents(self, scope: dict | None = None) -> list[sqlite3.Row]:
        """Documents a rule should run over. ``scope`` restricts the slice for
        cheap re-runs (§4a), e.g. {'jurisdiction':[...], 'doc_type':[...]}."""
        sql = "SELECT * FROM documents"
        clauses: list[str] = []
        params: list[object] = []
        scope = scope or {}
        if "doc_type" in scope:
            vals = scope["doc_type"]
            clauses.append(f"doc_type IN ({','.join('?' * len(vals))})")
            params.extend(vals)
        if "source" in scope:
            vals = scope["source"]
            clauses.append(f"source IN ({','.join('?' * len(vals))})")
            params.extend(vals)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.conn.execute(sql, params).fetchall()

    def upsert_document_tag(
        self,
        doc_id: str,
        tag: str,
        *,
        method: str,
        assigned_by_rule_id: int | None = None,
        rule_version: int | None = None,
        confidence: float | None = None,
    ) -> bool:
        """Record a tag with provenance (§4a). A manual tag is never overwritten by
        a rule; returns True if a row was written/updated."""
        # Human curation wins (§4a): don't let a rule clobber a manual tag.
        if method != "manual":
            existing = self.conn.execute(
                "SELECT 1 FROM document_tags WHERE doc_id=? AND tag=? AND method='manual'",
                (doc_id, tag),
            ).fetchone()
            if existing:
                return False
        self.conn.execute(
            """
            INSERT INTO document_tags (
                doc_id, tag, assigned_by_rule_id, rule_version, method, confidence, assigned_at
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(doc_id, tag, method) DO UPDATE SET
                assigned_by_rule_id=excluded.assigned_by_rule_id,
                rule_version=excluded.rule_version, confidence=excluded.confidence,
                assigned_at=excluded.assigned_at
            """,
            (doc_id, tag, assigned_by_rule_id, rule_version, method, confidence, _now()),
        )
        self._refresh_topic_tags_cache(doc_id)
        self.conn.commit()
        return True

    def remove_rule_tags(self, rule_id: int, tag: str) -> None:
        """Clear a rule's prior tags before a re-run — tagging is a re-derivable
        projection (§4a), so editing a rule and re-running is the correction path.
        Manual tags (assigned_by_rule_id IS NULL) are left untouched."""
        affected = self.conn.execute(
            "SELECT DISTINCT doc_id FROM document_tags WHERE assigned_by_rule_id=? AND tag=?",
            (rule_id, tag),
        ).fetchall()
        self.conn.execute(
            "DELETE FROM document_tags WHERE assigned_by_rule_id=? AND tag=?", (rule_id, tag)
        )
        for row in affected:
            self._refresh_topic_tags_cache(row["doc_id"])
        self.conn.commit()

    def tags_for(self, doc_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM document_tags WHERE doc_id = ?", (doc_id,)
        ).fetchall()

    def documents_with_tag(self, tag: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT d.* FROM documents d
            JOIN document_tags t ON t.doc_id = d.stable_id
            WHERE t.tag = ?
            """,
            (tag,),
        ).fetchall()

    def _refresh_topic_tags_cache(self, doc_id: str) -> None:
        """documents.topic_tags is the denormalised cache of document_tags for fast
        faceting; document_tags is the source of truth + provenance (§4a)."""
        rows = self.conn.execute(
            "SELECT DISTINCT tag FROM document_tags WHERE doc_id = ? ORDER BY tag", (doc_id,)
        ).fetchall()
        tags = [r["tag"] for r in rows]
        self.conn.execute(
            "UPDATE documents SET topic_tags = ? WHERE stable_id = ?",
            (json.dumps(tags), doc_id),
        )

    def start_rule_run(self, rule_id: int, rule_version: int, scope: dict | None) -> int:
        return self._insert_returning(
            """
            INSERT INTO rule_runs (rule_id, rule_version, started_at, scope_json, status)
            VALUES (?,?,?,?, 'running')
            """,
            (rule_id, rule_version, _now(), json.dumps(scope or {})),
            "run_id",
        )

    def finish_rule_run(self, run_id: int, *, evaluated: int, matched: int) -> None:
        self.conn.execute(
            """
            UPDATE rule_runs SET finished_at=?, docs_evaluated=?, docs_matched=?, status='done'
            WHERE run_id=?
            """,
            (_now(), evaluated, matched, run_id),
        )
        self.conn.commit()

    # -- embeddings + chunk index (§6b/§6c) --------------------------------
    def pending_embedding(self, provider: str, model: str, model_version: str) -> list[sqlite3.Row]:
        """Documents with text but no vectors in this embedding family (§6). A
        model swap is a new family, so it naturally re-queues the whole corpus."""
        return self.conn.execute(
            """
            SELECT * FROM documents d
            WHERE d.has_text = 1 AND d.text_path IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM embeddings e
                WHERE e.doc_id = d.stable_id AND e.provider = ?
                  AND e.model = ? AND e.model_version = ?
              )
            """,
            (provider, model, model_version),
        ).fetchall()

    def _insert_returning(self, sql: str, params, id_col: str) -> int:
        """Portable last-insert-id: ``RETURNING`` works on SQLite ≥ 3.35 and PG."""
        row = self.conn.execute(f"{sql} RETURNING {id_col} AS _id", params).fetchone()
        self.conn.commit()
        return int(row["_id"])

    def clear_embeddings(self, doc_id: str, provider: str, model: str, model_version: str) -> None:
        """Drop a doc's vectors in one family before re-embedding (re-derivable)."""
        self.conn.execute(
            "DELETE FROM embeddings WHERE doc_id=? AND provider=? AND model=? AND model_version=?",
            (doc_id, provider, model, model_version),
        )
        if self.backend == "sqlite":
            self.conn.execute(
                "DELETE FROM chunks_fts WHERE doc_id=? AND family=?",
                (doc_id, _family_key(provider, model, model_version)),
            )
        self.conn.commit()

    def add_chunk(
        self,
        doc_id: str,
        chunk_id: int,
        vector: list[float],
        chunk_text: str,
        *,
        provider: str,
        model: str,
        model_version: str,
        dimensions: int,
        structural_unit: str | None = None,
        source_language: str | None = None,
        char_start: int | None = None,
        char_end: int | None = None,
    ) -> None:
        # Bulk-imported corpora (A2AJ Canadian parquet, Open Australian Legal Corpus
        # JSONL…) occasionally carry a literal NUL byte from whatever upstream tool
        # produced their text. psycopg refuses to bind it at all — "PostgreSQL text
        # fields cannot contain NUL (0x00) bytes" — which aborts the *entire* embed
        # job on the first offending chunk, not just that one document. Strip it here,
        # at the last point before it becomes a query parameter, so char offsets and
        # the embedding input (already sent to the provider by this point) are
        # untouched — only the bytes Postgres/SQLite can't store are dropped.
        if "\x00" in chunk_text:
            chunk_text = chunk_text.replace("\x00", "")
        if self.backend == "postgres":
            # pgvector for the vector; tsvector (GIN) for FTS — both in one table.
            self.conn.execute(
                """
                INSERT INTO embeddings (
                    doc_id, chunk_id, vector, chunk_text, tsv, structural_unit, source_language,
                    provider, model, model_version, dimensions, char_start, char_end
                ) VALUES (?,?,?::vector,?,to_tsvector('english',?),?,?,?,?,?,?,?,?)
                ON CONFLICT (doc_id, chunk_id, provider, model, model_version) DO UPDATE SET
                    vector=EXCLUDED.vector, chunk_text=EXCLUDED.chunk_text, tsv=EXCLUDED.tsv,
                    structural_unit=EXCLUDED.structural_unit, char_start=EXCLUDED.char_start,
                    char_end=EXCLUDED.char_end
                """,
                (
                    doc_id, chunk_id, _postgres.vector_literal(vector), chunk_text, chunk_text,
                    structural_unit, source_language, provider, model, model_version,
                    dimensions, char_start, char_end,
                ),
            )
            return
        self.conn.execute(
            """
            INSERT INTO embeddings (
                doc_id, chunk_id, vector, chunk_text, structural_unit, source_language,
                provider, model, model_version, dimensions, char_start, char_end
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (doc_id, chunk_id, provider, model, model_version) DO UPDATE SET
                vector=excluded.vector, chunk_text=excluded.chunk_text,
                structural_unit=excluded.structural_unit, char_start=excluded.char_start,
                char_end=excluded.char_end
            """,
            (
                doc_id, chunk_id, json.dumps(vector), chunk_text, structural_unit,
                source_language, provider, model, model_version, dimensions,
                char_start, char_end,
            ),
        )
        self.conn.execute(
            "INSERT INTO chunks_fts (chunk_text, doc_id, chunk_id, family) VALUES (?,?,?,?)",
            (chunk_text, doc_id, chunk_id, _family_key(provider, model, model_version)),
        )

    def mark_embedded(self, doc_id: str) -> None:
        self.conn.execute(
            "UPDATE documents SET has_embedding = 1 WHERE stable_id = ?", (doc_id,)
        )
        self.conn.commit()

    def embedded_docs_in_family(self, doc_ids: list[str], provider: str, model: str,
                                model_version: str) -> set[str]:
        """Which of these docs already hold vectors in the family — the offline
        importer's skip-what's-done check, so re-running an import is cheap."""
        out: set[str] = set()
        for i in range(0, len(doc_ids), 200):
            chunk = doc_ids[i:i + 200]
            qs = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT DISTINCT doc_id FROM embeddings WHERE doc_id IN ({qs}) "
                "AND provider = ? AND model = ? AND model_version = ?",
                (*chunk, provider, model, model_version)).fetchall()
            out.update(r["doc_id"] for r in rows)
        return out

    def vector_rows(
        self, provider: str, model: str, model_version: str, filters: dict | None = None
    ) -> list[sqlite3.Row]:
        """All chunk vectors in a family (+ optional partition pre-filter, §6b.6),
        for an in-process cosine scan. pgvector replaces this at scale."""
        sql = """
            SELECT e.doc_id, e.chunk_id, e.vector, e.chunk_text, e.structural_unit,
                   e.char_start, e.char_end
            FROM embeddings e JOIN documents d ON d.stable_id = e.doc_id
            WHERE e.provider=? AND e.model=? AND e.model_version=?
        """
        params: list[object] = [provider, model, model_version]
        sql, params = _apply_filters(sql, params, filters)
        return self.conn.execute(sql, params).fetchall()

    def vector_search(
        self,
        query_vector: list[float],
        provider: str,
        model: str,
        model_version: str,
        *,
        dimensions: int | None = None,
        limit: int = 100,
        filters: dict | None = None,
    ) -> list[dict]:
        """Semantic half of hybrid search (§6c), best-first. On Postgres this is a
        real pgvector cosine scan (`<=>`); on SQLite an in-process cosine over the
        family's vectors. Both return the same row shape."""
        if self.backend == "postgres":
            vec = _postgres.vector_literal(query_vector)
            # Cast to the fixed family dimension so the partial HNSW index
            # (created on the same expression) is used (§7).
            cast = f"::vector({int(dimensions)})" if dimensions else "::vector"
            sql = f"""
                SELECT e.doc_id, e.chunk_id, e.chunk_text, e.structural_unit,
                       e.char_start, e.char_end, 1 - (e.vector{cast} <=> ?{cast}) AS score
                FROM embeddings e JOIN documents d ON d.stable_id = e.doc_id
                WHERE e.provider=? AND e.model=? AND e.model_version=?
            """
            params: list[object] = [vec, provider, model, model_version]
            if dimensions:
                # match the partial HNSW index predicate so the planner can use it
                sql += " AND e.dimensions = ?"
                params.append(int(dimensions))
            sql, params = _apply_filters(sql, params, filters)
            sql += f" ORDER BY e.vector{cast} <=> ?{cast} LIMIT ?"
            params.extend([vec, limit])
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

        # SQLite: load the family's vectors and score in Python.
        rows = self.vector_rows(provider, model, model_version, filters)
        scored = []
        for r in rows:
            scored.append((
                _cosine(query_vector, json.loads(r["vector"])),
                {
                    "doc_id": r["doc_id"], "chunk_id": r["chunk_id"],
                    "chunk_text": r["chunk_text"], "structural_unit": r["structural_unit"],
                    "char_start": r["char_start"], "char_end": r["char_end"],
                },
            ))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [{**row, "score": score} for score, row in scored[:limit]]

    def fts_chunks(
        self,
        query: str,
        provider: str,
        model: str,
        model_version: str,
        *,
        limit: int = 100,
        filters: dict | None = None,
    ) -> list[tuple[str, int, float]]:
        """Lexical half of hybrid search (§6c), best-first. Postgres: tsvector +
        ts_rank; SQLite: FTS5 bm25. (RRF fuses by rank position, so the score's
        sign/scale is immaterial.)"""
        if self.backend == "postgres":
            sql = """
                SELECT e.doc_id, e.chunk_id, ts_rank(e.tsv, plainto_tsquery('english', ?)) AS rank
                FROM embeddings e JOIN documents d ON d.stable_id = e.doc_id
                WHERE e.tsv @@ plainto_tsquery('english', ?)
                  AND e.provider=? AND e.model=? AND e.model_version=?
            """
            params: list[object] = [query, query, provider, model, model_version]
            sql, params = _apply_filters(sql, params, filters)
            sql += " ORDER BY rank DESC LIMIT ?"
            params.append(limit)
            try:
                rows = self.conn.execute(sql, params).fetchall()
            except Exception:
                return []
            return [(r["doc_id"], r["chunk_id"], r["rank"]) for r in rows]

        family = _family_key(provider, model, model_version)
        sql = """
            SELECT f.doc_id, f.chunk_id, bm25(chunks_fts) AS rank
            FROM chunks_fts f JOIN documents d ON d.stable_id = f.doc_id
            WHERE chunks_fts MATCH ? AND f.family = ?
        """
        params = [query, family]
        sql, params = _apply_filters(sql, params, filters)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []  # malformed MATCH query (user input) → no lexical hits
        return [(r["doc_id"], r["chunk_id"], r["rank"]) for r in rows]

    def get_chunk(self, doc_id: str, chunk_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM embeddings WHERE doc_id=? AND chunk_id=? LIMIT 1",
            (doc_id, chunk_id),
        ).fetchone()

    def create_vector_index(self, dimensions: int, *, m: int = 16, ef_construction: int = 64) -> bool:
        """Build a pgvector **HNSW** index for one embedding dimension (§7). It's a
        *partial expression* index — ``(vector::vector(d)) WHERE dimensions=d`` —
        because the column holds multiple families/dims; vector_search casts to the
        same expression so the planner uses it. No-op on SQLite. Start m=16,
        ef_construction=64; raise only if measured recall is short (§7)."""
        if self.backend != "postgres":
            return False
        d = int(dimensions)
        self.conn.execute(
            f"CREATE INDEX IF NOT EXISTS embeddings_hnsw_{d} "
            f"ON embeddings USING hnsw ((vector::vector({d})) vector_cosine_ops) "
            f"WITH (m = {int(m)}, ef_construction = {int(ef_construction)}) "
            f"WHERE dimensions = {d}"
        )
        self.conn.commit()
        return True

    # -- observability / ops aggregates (§8) -------------------------------
    def _count_by(self, column: str) -> dict[str, int]:
        rows = self.conn.execute(
            f"SELECT {column} AS k, COUNT(*) AS n FROM documents GROUP BY {column} ORDER BY n DESC"
        ).fetchall()
        return {(r["k"] or "?"): r["n"] for r in rows}

    def corpus_counts(self) -> dict:
        """Document breakdowns for the §8 corpus stats / faceting."""
        total = self.conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        return {
            "total": total,
            "by_doc_type": self._count_by("doc_type"),
            "by_source": self._count_by("source"),
            "by_upstream_status": self._count_by("upstream_status"),
        }

    def queue_depths(self) -> dict:
        """Pipeline queue view (§8): where documents are stuck between stages."""
        q = lambda sql: self.conn.execute(sql).fetchone()["n"]
        return {
            "fetched_no_text": q("SELECT COUNT(*) AS n FROM documents WHERE has_text = 0"),
            "text_not_embedded": q(
                "SELECT COUNT(*) AS n FROM documents WHERE has_text = 1 AND has_embedding = 0"
            ),
            "unresolved_edges": q(
                "SELECT COUNT(*) AS n FROM relations WHERE resolution_status = 'pending'"
            ),
            # Edges recognised by name only — no identifier to resolve against, so they
            # can never leave the pending pile without a human or an LLM naming them.
            "unidentified_edges": q(
                "SELECT COUNT(*) AS n FROM relations "
                "WHERE resolution_status = 'pending' AND candidate_id IS NULL"
            ),
        }

    def resolution_stats(self) -> dict:
        """Citation-resolution coverage (§8): share of edges that point at a node.
        One grouped pass over ``relations`` (was two full COUNTs); with the
        ``idx_relations_status`` index this is an index-only scan."""
        rows = self.conn.execute(
            "SELECT resolution_status, COUNT(*) AS n FROM relations GROUP BY resolution_status"
        ).fetchall()
        by = {r["resolution_status"]: r["n"] for r in rows}
        total = sum(by.values())
        resolved = by.get("resolved", 0)
        return {
            "resolved": resolved,
            "total": total,
            "coverage": (resolved / total) if total else 0.0,
        }

    def tag_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT tag, COUNT(DISTINCT doc_id) AS n FROM document_tags
            GROUP BY tag ORDER BY n DESC
            """
        ).fetchall()
        return {r["tag"]: r["n"] for r in rows}

    @staticmethod
    def _doc_filter_clauses(*, source=None, doc_type=None, tag=None, query=None, court=None,
                            id_prefix=None, year_from=None, year_to=None, cites=None, cited_by=None,
                            cites_pinpoint=None):
        """Shared WHERE-clause builder for list/count/search/facets (so every surface filters
        with identical semantics). ``court`` matches the stored court token; ``id_prefix``
        matches one or more slug heads (comma-separated). ``query`` is tokenised — each
        whitespace-separated word must appear (as a substring) in the title or id, so
        *non-consecutive* words match ("erasure data" finds "…data … erasure"). ``year_from``/
        ``year_to`` bound the decision-date year. ``cites`` keeps documents that cite the given
        target (by id/ECLI/candidate); ``cited_by`` keeps documents cited BY the given source."""
        clauses: list[str] = []
        params: list[object] = []
        if source:
            clauses.append("d.source = ?"); params.append(source)
        if doc_type:
            clauses.append("d.doc_type = ?"); params.append(doc_type)
        if court:
            clauses.append("d.court = ?"); params.append(court)
        if id_prefix:
            heads = [h.strip() for h in str(id_prefix).split(",") if h.strip()]
            if heads:
                clauses.append("(" + " OR ".join("d.stable_id LIKE ?" for _ in heads) + ")")
                params.extend(f"{h}/%" for h in heads)
        if query:
            # Case-insensitive (Postgres LIKE is case-sensitive; SQLite's is not) AND tokenised
            # — every word must hit the title or id, in any order/position. No title index
            # exists, so lower() costs nothing here.
            for tok in str(query).split():
                clauses.append("(lower(d.title) LIKE ? OR lower(d.stable_id) LIKE ?)")
                like = f"%{tok.lower()}%"
                params.extend([like, like])
        if year_from:
            clauses.append("substr(d.decision_date, 1, 4) >= ?"); params.append(str(year_from))
        if year_to:
            clauses.append("substr(d.decision_date, 1, 4) <= ?"); params.append(str(year_to))
        if cites:
            sub = ("EXISTS (SELECT 1 FROM relations r WHERE r.src_id = d.stable_id "
                   "AND (r.dst_id = ? OR r.candidate_id = ?)")
            p = [cites, cites]
            if cites_pinpoint:  # cite a *specific* provision of the target (its dst_anchor)
                sub += " AND r.dst_anchor = ?"
                p.append(cites_pinpoint)
            clauses.append(sub + ")")
            params.extend(p)
        if cited_by:
            clauses.append(
                "EXISTS (SELECT 1 FROM relations r WHERE r.dst_id = d.stable_id AND r.src_id = ?)")
            params.append(cited_by)
        return clauses, params

    # sort key → ORDER BY. "cited" ranks by the citation-frequency roll-up (a LEFT JOIN,
    # added by search_documents); the rest sort the documents table directly.
    _SORT_SQL = {
        "date": "d.decision_date DESC NULLS LAST, d.stable_id",
        "date_asc": "d.decision_date ASC NULLS LAST, d.stable_id",
        "title": "lower(d.title), d.stable_id",
        "cited": "cited_by DESC, d.decision_date DESC",
        # network authority (PageRank roll-up); raw = landmark, decayed = currently live
        "authority": "authority DESC, cited_by DESC, d.decision_date DESC",
        "authority_recent": "authority_decayed DESC, cited_by DESC, d.decision_date DESC",
    }

    def _sort_clause(self, sort: str | None) -> str:
        key = self._SORT_SQL.get(sort or "date", self._SORT_SQL["date"])
        if self.backend == "sqlite":  # SQLite has no NULLS LAST
            key = key.replace(" DESC NULLS LAST", " DESC").replace(" ASC NULLS LAST", " ASC")
        return key

    def list_documents(
        self,
        *,
        source: str | None = None,
        doc_type: str | None = None,
        tag: str | None = None,
        query: str | None = None,
        court: str | None = None,
        id_prefix: str | None = None,
        year_from: str | None = None,
        year_to: str | None = None,
        cites: str | None = None,
        cited_by: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """Browse/filter documents — lets an agent iterate, e.g., a law's sections
        to augment each with secondary material."""
        # No DISTINCT: every filter is an EXISTS (including tag, below), so rows can't
        # fan out. ``SELECT DISTINCT d.*`` forced a full sort/hash of the whole table
        # before the LIMIT could apply — invisible at 20k documents, but at 4.9M it
        # spilled to disk for minutes per page load and took the Corpus browser down.
        # Without it, the (decision_date DESC, stable_id) index serves LIMIT directly.
        sql = "SELECT d.* FROM documents d"
        params: list[object] = []
        clauses, fparams = self._doc_filter_clauses(
            source=source, doc_type=doc_type, tag=None, query=query, court=court, id_prefix=id_prefix,
            year_from=year_from, year_to=year_to, cites=cites, cited_by=cited_by)
        if tag:
            clauses.insert(0, "EXISTS (SELECT 1 FROM document_tags t "
                              "WHERE t.doc_id = d.stable_id AND t.tag = ?)")
            params.append(tag)
        params.extend(fparams)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY d.decision_date DESC, d.stable_id LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self.conn.execute(sql, params).fetchall()

    def search_documents(self, *, sort: str | None = None, limit: int = 50, offset: int = 0,
                         **filters) -> list[sqlite3.Row]:
        """Like :meth:`list_documents` but sortable (incl. by citation frequency) and each row
        carries a ``cited_by`` count (occurrences from the roll-up) for display + ranking."""
        tag = filters.pop("tag", None)
        clauses, fparams = self._doc_filter_clauses(tag=None, **filters)
        # cited_by as a correlated scalar subquery: `candidate_id IN (stable_id, ecli)` is two
        # index probes per row. The old formulation — a LEFT JOIN with an OR join predicate +
        # GROUP BY — defeated the candidate_id index on Postgres and ran for minutes, piling up
        # on every autocomplete keystroke until the connection pool starved.
        sql = ("SELECT d.*, COALESCE((SELECT MAX(cc.occurrences) FROM citation_counts cc "
               "WHERE cc.candidate_id IN (d.stable_id, d.ecli)), 0) AS cited_by, "
               # authority prior (PageRank roll-up) — same per-row PK-probe pattern
               "COALESCE((SELECT a.pagerank FROM doc_authority a WHERE a.doc_id = d.stable_id), 0) AS authority, "
               "COALESCE((SELECT a.pagerank_decayed FROM doc_authority a WHERE a.doc_id = d.stable_id), 0) AS authority_decayed, "
               "(SELECT a.percentile FROM doc_authority a WHERE a.doc_id = d.stable_id) AS authority_percentile "
               "FROM documents d")
        params: list[object] = []
        if tag:
            clauses.insert(0, "EXISTS (SELECT 1 FROM document_tags t WHERE t.doc_id = d.stable_id AND t.tag = ?)")
            params.append(tag)
        params.extend(fparams)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {self._sort_clause(sort)} LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self.conn.execute(sql, params).fetchall()

    def count_documents(self, *, source: str | None = None, doc_type: str | None = None,
                        tag: str | None = None, query: str | None = None,
                        court: str | None = None, id_prefix: str | None = None,
                        year_from: str | None = None, year_to: str | None = None,
                        cites: str | None = None, cited_by: str | None = None,
                        cites_pinpoint: str | None = None) -> int:
        """Total documents matching the same filters as :meth:`list_documents` — for
        the Corpus page's true count + pagination."""
        # COUNT(*) + EXISTS, not JOIN + COUNT(DISTINCT): same no-fan-out reasoning as
        # list_documents, and a distinct-aggregation over millions of ids is what made
        # the Corpus page's total/pagination time out after the bulk imports.
        sql = "SELECT COUNT(*) AS n FROM documents d"
        params: list[object] = []
        clauses, fparams = self._doc_filter_clauses(
            source=source, doc_type=doc_type, tag=None, query=query, court=court, id_prefix=id_prefix,
            year_from=year_from, year_to=year_to, cites=cites, cited_by=cited_by, cites_pinpoint=cites_pinpoint)
        if tag:
            clauses.insert(0, "EXISTS (SELECT 1 FROM document_tags t "
                              "WHERE t.doc_id = d.stable_id AND t.tag = ?)")
            params.append(tag)
        params.extend(fparams)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.conn.execute(sql, params).fetchone()["n"]

    def document_facets(self, *, dims=("source", "doc_type", "court", "year"), top: int = 40,
                        **filters) -> dict:
        """Distribution of the filtered result set across facet dimensions — counts per
        source / doc_type / court, and a per-year histogram — so the search sidebar can show
        refine tick-boxes with live counts and a timeline. One GROUP BY per dimension over the
        same WHERE the results use (``tag`` becomes an EXISTS so it never fans out the count)."""
        tag = filters.pop("tag", None)
        clauses, fparams = self._doc_filter_clauses(tag=None, **filters)
        if tag:
            clauses.insert(0, "EXISTS (SELECT 1 FROM document_tags t WHERE t.doc_id = d.stable_id AND t.tag = ?)")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        def _params():
            p: list[object] = []
            if tag:
                p.append(tag)
            p.extend(fparams)
            return p

        out: dict = {}
        col = {"source": "d.source", "doc_type": "d.doc_type", "court": "d.court",
               "year": "substr(d.decision_date, 1, 4)"}
        if self.backend == "postgres":
            # ONE pass over the filtered set instead of one per dimension: with a free-text
            # filter (an unindexable LIKE scan) each pass costs seconds, so 4 passes made
            # the search page ~4× slower than the results query itself.
            exprs = [col[d] for d in dims]
            sets = ", ".join(f"({e})" for e in exprs)
            sql = (f"SELECT {', '.join(f'{e} AS k{i}' for i, e in enumerate(exprs))}, "
                   f"COUNT(DISTINCT d.stable_id) AS n FROM documents d{where} "
                   f"GROUP BY GROUPING SETS ({sets})")
            buckets: dict[str, list] = {d: [] for d in dims}
            for r in self.conn.execute(sql, _params()).fetchall():
                for i, dim in enumerate(dims):
                    if r[f"k{i}"] is not None:
                        buckets[dim].append((r[f"k{i}"], r["n"]))
                        break
            for dim in dims:
                rows = sorted(buckets[dim], key=lambda kv: -kv[1])
                if dim == "year":
                    out[dim] = {k: n for k, n in rows if k}
                else:
                    out[dim] = [{"key": k, "n": n} for k, n in rows if k][:top]
            return out
        for dim in dims:
            expr = col[dim]
            sql = (f"SELECT {expr} AS k, COUNT(DISTINCT d.stable_id) AS n FROM documents d"
                   f"{where} GROUP BY {expr} ORDER BY n DESC")
            rows = self.conn.execute(sql, _params()).fetchall()
            if dim == "year":
                out[dim] = {r["k"]: r["n"] for r in rows if r["k"]}
            else:
                out[dim] = [{"key": r["k"], "n": r["n"]} for r in rows if r["k"]][:top]
        return out

    def distinct_courts(self) -> list[sqlite3.Row]:
        """Every court token with a document count — for the advanced-search court field's
        autocomplete + the facet sidebar."""
        return self.conn.execute(
            "SELECT court AS k, COUNT(*) AS n FROM documents WHERE court IS NOT NULL AND court <> '' "
            "GROUP BY court ORDER BY n DESC").fetchall()

    def echr_formation_counts(self) -> list[sqlite3.Row]:
        """Held ECtHR cases grouped by HUDOC formation (``doctypebranch`` in meta_json) — so the
        Corpus Map can split ECHR into Grand Chamber / Chamber / Committee / Decision. Held only:
        a pending (not-yet-fetched) case carries no formation. ``meta_json`` is TEXT, so cast."""
        if self.backend == "postgres":
            expr = "meta_json::jsonb ->> 'doctypebranch'"
        else:
            expr = "json_extract(meta_json, '$.doctypebranch')"
        return self.conn.execute(
            f"SELECT {expr} AS branch, COUNT(*) AS n FROM documents "
            "WHERE source = 'echr' AND stable_id <> 'echr/convention' GROUP BY branch").fetchall()

    def outgoing_citation_targets(self, source: str) -> list[sqlite3.Row]:
        """Every citation edge OUT of documents in ``source`` — (dst_id, raw string) — for the
        Corpus Map's lazy "cites:" breakdown. Excludes inferred carry-forward edges (heuristic
        pinpoints, not real citations). One source at a time keeps the scan bounded."""
        return self.conn.execute(
            "SELECT r.dst_id, r.raw_citation_string AS raw FROM relations r "
            "JOIN documents d ON d.stable_id = r.src_id "
            "WHERE d.source = ? AND r.extracted_via != 'inferred'", (source,)).fetchall()

    def outgoing_citation_targets_for(
        self, source_types: list[tuple[str, str | None]],
    ) -> list[sqlite3.Row]:
        """Edges out of a corpus-map category's actual stored source/type pairs.

        Display categories (``fr-caselaw``) are deliberately not storage sources
        (``fr-dila``).  Keeping the mapping as source/type pairs also separates a
        register that supplies both legislation and decisions.
        """
        if not source_types:
            return []
        clauses, params = [], []
        for source, doc_type in source_types:
            if doc_type:
                clauses.append("(d.source = ? AND d.doc_type = ?)")
                params.extend((source, doc_type))
            else:
                clauses.append("d.source = ?")
                params.append(source)
        return self.conn.execute(
            "SELECT r.dst_id, r.raw_citation_string AS raw, COUNT(*) AS n FROM relations r "
            "JOIN documents d ON d.stable_id = r.src_id WHERE ("
            + " OR ".join(clauses) + ") AND r.extracted_via != 'inferred' "
            "GROUP BY r.dst_id, r.raw_citation_string",
            tuple(params),
        ).fetchall()

    def document_subtype_counts(self) -> list[sqlite3.Row]:
        """Held-document counts grouped by (source, doc_type, court, slug-prefix) — the raw
        material for the Corpus Map's per-sub-type "Held" column.

        The prefix keeps the **first two** slug segments (``uksi/2016/413`` → ``uksi/2016``,
        ``ca/act/a-1`` → ``ca/act``); for ids without a slash (CELEX/ECLI) it's the whole id.
        Two segments, not one, because id grammars put the document type in different
        places: UK ids lead with it (``uksi``) but the Commonwealth registers lead with the
        jurisdiction and put the type second (``ca/act``, ``hk/cap``, ``au/qld``). Grouping
        on one segment collapsed every Canadian Act and Regulation into a single "ca" row,
        so the map could only ever show them as "Other". Callers that want just the leading
        segment still split it off themselves. Backend-portable (different string fns)."""
        if self.backend == "postgres":
            prefix = ("split_part(stable_id, '/', 1) || "
                      "CASE WHEN split_part(stable_id, '/', 2) <> '' "
                      "THEN '/' || split_part(stable_id, '/', 2) ELSE '' END")
        else:
            head = ("substr(stable_id, 1, CASE WHEN instr(stable_id, '/') > 0 "
                    "THEN instr(stable_id, '/') - 1 ELSE length(stable_id) END)")
            rest = ("substr(stable_id, instr(stable_id, '/') + 1)")
            second = (f"CASE WHEN instr({rest}, '/') > 0 "
                      f"THEN substr({rest}, 1, instr({rest}, '/') - 1) ELSE {rest} END")
            prefix = (f"CASE WHEN instr(stable_id, '/') > 0 "
                      f"THEN {head} || '/' || {second} ELSE stable_id END")
        sql = (f"SELECT source, doc_type, court, {prefix} AS prefix, COUNT(*) AS n "
               "FROM documents GROUP BY source, doc_type, court, prefix")
        return self.conn.execute(sql).fetchall()

    # -- background jobs (§8) ----------------------------------------------
    # The registry used to be a dict in the API process. That made a deploy erase a
    # running harvest, made restart-after-freeze impossible across a restart, and — the
    # expensive one — made the scheduler's own work invisible: the auto-drain ran in a
    # different container, so nothing in the UI ever showed that it had been storing
    # zero documents for seventeen days.
    def create_job(self, job_id: str, kind: str, label: str, params: dict,
                   *, origin: str = "api", root_job_id: str | None = None,
                   resumed_from: str | None = None, resume_policy: str = "restart",
                   attempt: int = 1, checkpoint: dict | None = None) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO jobs (job_id, kind, label, params_json, status, origin, "
            "started_at, heartbeat_at, lease_heartbeat_at, root_job_id, resumed_from, resume_policy, attempt, checkpoint_json) "
            "VALUES (?,?,?,?,'running',?,?,?,?,?,?,?,?,?)",
            (job_id, kind, label, json.dumps(params or {}), origin, now, now, now,
             root_job_id or job_id, resumed_from, resume_policy, attempt,
             json.dumps(checkpoint or {})),
        )
        self.conn.commit()

    def pulse_job(self, job_id: str) -> None:
        """Prove the owning process is alive without pretending work progressed."""
        self.conn.execute(
            "UPDATE jobs SET lease_heartbeat_at = ? WHERE job_id = ? AND status = 'running'",
            (_now(), job_id),
        )
        self.conn.commit()

    def heartbeat_job(self, job_id: str, progress: dict, log_tail: list[str],
                      checkpoint: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE jobs SET progress_json = ?, log_json = ?, heartbeat_at = ?"
            + (", checkpoint_json = ?" if checkpoint is not None else "")
            + " WHERE job_id = ?",
            ((json.dumps(progress or {}), json.dumps(log_tail[-300:]), _now())
             + ((json.dumps(checkpoint),) if checkpoint is not None else ()) + (job_id,)),
        )
        self.conn.commit()

    def finish_job(self, job_id: str, status: str, result: dict | None,
                   log_tail: list[str] | None = None) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = ?, result_json = ?, finished_at = ?, heartbeat_at = ?"
            + (", log_json = ?" if log_tail is not None else "")
            + " WHERE job_id = ?",
            ((status, json.dumps(result, default=str) if result is not None else None,
              _now(), _now())
             + ((json.dumps(log_tail[-300:]),) if log_tail is not None else ())
             + (job_id,)),
        )
        self.conn.commit()

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    def list_jobs(self, *, limit: int = 60) -> list[sqlite3.Row]:
        """Running jobs first, then most-recent finished — what the global panel shows."""
        return self.conn.execute(
            "SELECT * FROM jobs ORDER BY (status = 'running') DESC, started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def running_jobs(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self.conn.execute(
                "SELECT * FROM jobs WHERE status = 'running' AND kind = ?", (kind,)
            ).fetchall()
        return self.conn.execute("SELECT * FROM jobs WHERE status = 'running'").fetchall()

    def request_job_cancel(self, job_id: str) -> bool:
        """Ask a job to stop. Works across processes — the worker polls this flag, so the
        UI can cancel a job running inside the scheduler container."""
        cur = self.conn.execute(
            "UPDATE jobs SET cancel = 1 WHERE job_id = ? AND status = 'running'", (job_id,)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def request_job_restart(self, job_id: str) -> bool:
        """Cancel cooperatively and queue exactly one replacement after it stops."""
        cur = self.conn.execute(
            "UPDATE jobs SET cancel = 1, restart_requested = 1 "
            "WHERE job_id = ? AND status = 'running'", (job_id,)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def job_cancelled(self, job_id: str) -> bool:
        row = self.conn.execute("SELECT cancel FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return bool(row and row["cancel"])

    def orphan_running_jobs(self, origin: str) -> int:
        """On startup, a job this process's predecessor left 'running' has no thread behind
        it — its worker died with the process. Mark it, rather than leaving a ghost that
        the panel shows as live forever."""
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'interrupted', finished_at = ? "
            "WHERE status = 'running' AND origin = ?",
            (_now(), origin),
        )
        self.conn.commit()
        return max(cur.rowcount, 0)

    def prune_jobs(self, *, keep: int = 200) -> None:
        self.conn.execute(
            "DELETE FROM jobs WHERE status <> 'running' AND job_id NOT IN "
            "(SELECT job_id FROM jobs WHERE status <> 'running' ORDER BY started_at DESC LIMIT ?)",
            (keep,),
        )
        self.conn.commit()

    def recent_job_results(self, kind: str, *, limit: int = 20) -> list[sqlite3.Row]:
        """The last N outcomes for one job kind — the substrate for "auto-drain has stored
        nothing for three days", the alert that would have caught the poisoned skip-list."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE kind = ? AND status = 'done' "
            "ORDER BY started_at DESC LIMIT ?",
            (kind, limit),
        ).fetchall()

    def all_sources(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sources ORDER BY key").fetchall()

    def source_doc_count(self, source_key: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE source = ?", (source_key,)
        ).fetchone()["n"]

    def llm_extracted_ratio(self, source_key: str) -> float:
        """Share of a source's docs extracted via LLM — a format-drift early
        warning when a structural source starts falling back to llm_extract (§8)."""
        row = self.conn.execute(
            """
            SELECT
              SUM(CASE WHEN extracted_via = 'llm' THEN 1 ELSE 0 END) AS llm,
              COUNT(*) AS total
            FROM documents WHERE source = ?
            """,
            (source_key,),
        ).fetchone()
        return (row["llm"] / row["total"]) if row["total"] else 0.0
