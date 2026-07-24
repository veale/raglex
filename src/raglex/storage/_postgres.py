"""PostgreSQL backend bits for the catalogue (§7 — the production spine).

The catalogue is written once in portable SQL with ``?`` placeholders; this module
provides the Postgres pieces that genuinely diverge from SQLite: a connection shim
(translates ``?`` → ``%s``, splits the DDL, yields dict rows), the Postgres DDL
(``pgvector`` for ANN vector search, ``tsvector`` + GIN for FTS, partitioning-ready
tables), and helpers. Everything relational stays identical to SQLite.

This is the path the design's §7 recommends at scale: relational + FTS + vector in
one Postgres instance, with the same one-backup story.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("raglex.storage.postgres")


def is_postgres_dsn(dsn: str) -> bool:
    return dsn.startswith("postgresql://") or dsn.startswith("postgres://")


def vector_literal(vec: list[float]) -> str:
    """pgvector text input form: ``[v1,v2,...]``."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class PgConnShim:
    """Make a psycopg connection look enough like a sqlite3 connection for the
    shared catalogue code: ``execute(sql, params)`` (translating ``?`` → ``%s``),
    ``executescript``, ``commit``, ``close``. Rows are dicts (``row['col']`` and
    ``dict(row)`` both work, matching ``sqlite3.Row`` usage).

    ``close()`` returns the connection to the pool rather than tearing it down — the
    catalogue is opened and closed per request, and the UI polls several endpoints a
    second, so dialling Postgres afresh each time was pure overhead."""

    def __init__(self, raw, pool=None) -> None:
        self.raw = raw
        self._pool = pool

    def execute(self, sql: str, params=()):  # noqa: ANN001
        # With no params, pass None so psycopg skips placeholder processing —
        # otherwise a literal % in the SQL (LIKE 'para%') raises "only '%s' …
        # allowed as placeholders" even though nothing is being bound.
        if not params:
            return self.raw.execute(sql, None)
        # With params, psycopg DOES parse placeholders, so any literal % in the
        # SQL (LIKE 'dpa-%') must be doubled or it raises the same error — the
        # bug that 500'd every Explore drill with a kind filter. Escape first,
        # then translate the catalogue's portable ? placeholders.
        return self.raw.execute(sql.replace("%", "%%").replace("?", "%s"), params)

    # "CREATE INDEX IF NOT EXISTS <name>" — the statements that must be pre-checked
    # against the catalog rather than executed: Postgres acquires the table's SHARE
    # lock BEFORE noticing the index already exists, so on a busy table (a resumed
    # bulk resolve holding row-exclusive locks on `relations` for minutes at a time)
    # every process start queued behind the writer and the API sat silent, unbound —
    # the CREATE INDEX flavour of the check-before-ALTER startup deadlock.
    _CREATE_INDEX_RE = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(?P<name>[a-z0-9_]+)", re.IGNORECASE)

    def _index_exists(self, name: str) -> bool:
        cur = self.raw.execute(
            "SELECT 1 FROM pg_class WHERE relname = %s AND relkind = 'i'", (name,))
        return cur.fetchone() is not None

    def executemany(self, sql: str, rows) -> None:
        """Batch INSERT — one round trip per batch instead of per row (the citation
        writes were the parallel extractor's parent-side bottleneck). Same escape +
        placeholder translation as execute()."""
        rows = list(rows)
        if not rows:
            return
        with self.raw.cursor() as cur:
            cur.executemany(sql.replace("%", "%%").replace("?", "%s"), rows)

    def executescript(self, script: str) -> None:
        # Tolerate the concurrent-startup race: the api and scheduler containers (and
        # several api threads) all run the CREATE-IF-NOT-EXISTS DDL at boot, and
        # Postgres's IF NOT EXISTS check is not atomic — two simultaneous CREATE
        # TABLEs can both pass the check and one loses with a UniqueViolation on the
        # pg_type/pg_class catalog. The object exists either way, which is all IF NOT
        # EXISTS ever promised, so that specific failure is safely ignored.
        from psycopg import errors as _pgerr

        for stmt in script.split(";"):
            if not stmt.strip():
                continue
            # an index that already exists is skipped WITHOUT the table lock the
            # real statement would take just to discover the same thing
            m = self._CREATE_INDEX_RE.search(stmt)
            if m and self._index_exists(m.group("name")):
                continue
            try:
                self.raw.execute(stmt)
            except (_pgerr.UniqueViolation, _pgerr.DuplicateTable,
                    _pgerr.DuplicateObject) as exc:
                if not re.search(r"\bCREATE\b", stmt, re.IGNORECASE):
                    raise
                # someone else just created it — the desired end state
                log.debug("startup DDL race (already created): %s", exc)
                self.raw.rollback()
        self.raw.commit()

    def commit(self) -> None:
        self.raw.commit()  # no-op under autocommit; kept so the shared code path is happy

    def transaction(self):
        """An explicit all-or-nothing block for multi-statement writes (the connection is
        otherwise autocommit, so each statement commits on its own)."""
        return self.raw.transaction()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.putconn(self.raw)
            self.raw = None
            return
        self.raw.close()


# One pool per DSN per process. Bounded: the API's threads (request handlers + job
# workers) are the only consumers, and an unbounded pool against a 15GB box is how you
# find out what max_connections is.
_POOLS: dict[str, object] = {}
_POOL_LOCK = None


def _get_pool(dsn: str):
    global _POOL_LOCK
    import threading

    if _POOL_LOCK is None:
        _POOL_LOCK = threading.Lock()
    with _POOL_LOCK:
        pool = _POOLS.get(dsn)
        if pool is not None:
            return pool
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError:
            _POOLS[dsn] = False  # psycopg_pool absent → fall back to direct connections
            return False
        import os

        # statement_timeout: a runaway query dies instead of pinning a pool worker
        # until every connection is wedged and the whole API reads as a
        # NetworkError (the freeze-until-restart failure mode). Long-running
        # batch work (rescan, rebuilds) runs through its OWN exec'd processes /
        # job threads and can override via RAGLEX_PG_STATEMENT_TIMEOUT_MS.
        # 3 min: above the heaviest legitimate in-process batch statement (the
        # citation-counts INSERT-SELECT runs ~65s live) but far below "wedged".
        timeout_ms = int(os.environ.get("RAGLEX_PG_STATEMENT_TIMEOUT_MS") or 180000)
        pool = ConnectionPool(
            dsn,
            min_size=int(os.environ.get("RAGLEX_PG_POOL_MIN") or 2),
            max_size=int(os.environ.get("RAGLEX_PG_POOL_MAX") or 16),
            kwargs={"row_factory": dict_row, "autocommit": True,
                    "options": f"-c statement_timeout={timeout_ms}"},
            open=True,
        )
        _POOLS[dsn] = pool
        return pool


def connect(dsn: str) -> PgConnShim:
    # autocommit=True: a read-only open then never sits 'idle in transaction' holding a
    # lock between statements (e.g. while a long Python loop processes a result set, or a
    # harvest waits on the network) — which once queued behind a schema-migration ALTER and
    # stalled the whole `documents` table. Multi-statement writes that must be atomic use an
    # explicit transaction (see Catalogue._atomic / PgConnShim.transaction).
    pool = _get_pool(dsn)
    if pool:
        return PgConnShim(pool.getconn(), pool=pool)

    import psycopg
    from psycopg.rows import dict_row

    return PgConnShim(psycopg.connect(dsn, row_factory=dict_row, autocommit=True))


# Postgres DDL. Booleans are INTEGER (0/1) so inserts match the SQLite path
# verbatim; JSON columns are TEXT (we store json.dumps) for the same reason — the
# canonical schema/postgres.sql uses BOOLEAN/JSONB, but the runtime favours one
# shared code path. Vectors use pgvector; chunk FTS uses tsvector + GIN.
PG_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

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
    meta_json        TEXT,
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
-- keyset pagination of a whole source in stable_id order (the reparse-source job).
CREATE INDEX IF NOT EXISTS documents_source_stable_idx ON documents (source, stable_id);
CREATE INDEX IF NOT EXISTS documents_ecli_idx ON documents (ecli);
CREATE INDEX IF NOT EXISTS documents_payload_hash_idx ON documents (payload_hash);
CREATE INDEX IF NOT EXISTS documents_landing_url_idx ON documents (landing_url);

CREATE TABLE IF NOT EXISTS relations (
    relation_id        BIGSERIAL PRIMARY KEY,
    src_id             TEXT NOT NULL,
    dst_id             TEXT,
    raw_citation_string TEXT,
    candidate_id       TEXT,
    raw_fold           TEXT,
    resolution_status  TEXT NOT NULL DEFAULT 'pending',
    relationship_type  TEXT NOT NULL DEFAULT 'mentions',
    extracted_via      TEXT NOT NULL DEFAULT 'structured',
    context_chunk_id   TEXT,
    src_anchor         TEXT,
    dst_anchor         TEXT,
    context_start      INTEGER,
    context_end        INTEGER
);
CREATE INDEX IF NOT EXISTS relations_src_idx ON relations (src_id);
CREATE INDEX IF NOT EXISTS relations_dst_idx ON relations (dst_id);
CREATE INDEX IF NOT EXISTS idx_relations_status ON relations (resolution_status);

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

-- The hanging-reference worklist, pre-aggregated (§5b/§8). The live GROUP BY over the
-- pending relations slice is ~96s (4.3M rows), which made the Unresolved page crawl and
-- auto-drain never start. Rebuilt with citation_counts, read top-by-citing_count.
CREATE TABLE IF NOT EXISTS pending_reference_stats (
    ref           TEXT PRIMARY KEY,
    candidate     TEXT,
    raw           TEXT,
    anchor        TEXT,
    methods       TEXT,
    occurrences   BIGINT NOT NULL DEFAULT 0,
    citing_count  BIGINT NOT NULL DEFAULT 0,
    echr_citing   INTEGER NOT NULL DEFAULT 0,
    rebuilt_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_reference_stats_citing_idx ON pending_reference_stats (citing_count DESC);

-- Per-source resolved-outgoing roll-up read by the Explore homepage instead of a
-- minutes-long live GROUP BY over relations x documents. Rebuilt with citation_counts.
CREATE TABLE IF NOT EXISTS source_stats (
    source            TEXT PRIMARY KEY,
    resolved_outgoing BIGINT NOT NULL DEFAULT 0,
    rebuilt_at        TEXT NOT NULL
);

-- The Explore homepage's base aggregate (documents by source/type/court/year).
-- Replaces two live full-table scans per cache warm at 4.9M docs.
CREATE TABLE IF NOT EXISTS corpus_shape_stats (
    source     TEXT NOT NULL,
    doc_type   TEXT NOT NULL,
    court      TEXT,
    yr         TEXT,
    n          BIGINT NOT NULL DEFAULT 0,
    with_text  BIGINT NOT NULL DEFAULT 0,
    embedded   BIGINT NOT NULL DEFAULT 0,
    rebuilt_at TEXT NOT NULL
);

-- Legislation-type rail roll-up for the Explore drill. The per-document Python
-- classification pass took ~6 minutes per homepage cache warm at 1.9M rows.
CREATE TABLE IF NOT EXISTS leg_type_stats (
    source       TEXT NOT NULL,
    label        TEXT NOT NULL,
    n            BIGINT NOT NULL DEFAULT 0,
    years_json   TEXT NOT NULL DEFAULT '{}',
    filters_json TEXT NOT NULL DEFAULT '[]',
    rebuilt_at   TEXT NOT NULL,
    PRIMARY KEY (source, label)
);

-- Per-document citation-network statistics (PageRank over the resolved mentions
-- graph, treatments deliberately unweighted — not reliable yet). Rebuilt wholesale.
-- NB executescript splits this DDL on semicolons WITHOUT stripping comments, so
-- comment text here must never contain one.
CREATE TABLE IF NOT EXISTS doc_authority (
    doc_id           TEXT PRIMARY KEY,
    pagerank         REAL NOT NULL DEFAULT 0,
    pagerank_decayed REAL NOT NULL DEFAULT 0,
    percentile       REAL,
    in_degree        INTEGER NOT NULL DEFAULT 0,
    out_degree       INTEGER NOT NULL DEFAULT 0,
    rebuilt_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS doc_authority_pr_idx ON doc_authority (pagerank DESC);

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

CREATE TABLE IF NOT EXISTS citation_aliases (
    alias    TEXT PRIMARY KEY,
    dst_id   TEXT NOT NULL,
    source   TEXT
);
-- probed by dst_id per just-harvested document (resolve_pending_for) — without
-- this index each probe seq-scanned 5M alias rows
CREATE INDEX IF NOT EXISTS citation_aliases_dst_idx ON citation_aliases (dst_id);

-- Corpus-wide store of shorthands learned in one document and applied (under the
-- gates in citations/stage.py) in others. See the SQLite DDL for why this is not
-- citation_aliases and why it carries no occurrence counter.
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

CREATE TABLE IF NOT EXISTS citations (
    citation_id   BIGSERIAL PRIMARY KEY,
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

CREATE TABLE IF NOT EXISTS document_assets (
    asset_id     BIGSERIAL PRIMARY KEY,
    doc_id       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    path         TEXT,
    mime         TEXT,
    payload_hash TEXT,
    added_by     TEXT NOT NULL DEFAULT 'user',
    title        TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS document_assets_doc_idx ON document_assets (doc_id);

CREATE TABLE IF NOT EXISTS tag_rules (
    rule_id            BIGSERIAL PRIMARY KEY,
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
    assigned_by_rule_id BIGINT,
    rule_version       INTEGER,
    method             TEXT NOT NULL,
    confidence         REAL,
    assigned_at        TEXT NOT NULL,
    PRIMARY KEY (doc_id, tag, method)
);

CREATE TABLE IF NOT EXISTS rule_runs (
    run_id         BIGSERIAL PRIMARY KEY,
    rule_id        BIGINT NOT NULL,
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

-- Per-run harvest history (§keep-current) — the row-per-run log the Maintain diagnosis
-- view reads. See the sqlite DDL in catalogue.py for the rationale.
CREATE TABLE IF NOT EXISTS source_runs (
    run_id       BIGSERIAL PRIMARY KEY,
    source_key   TEXT NOT NULL,
    watch_id     BIGINT,
    trigger      TEXT NOT NULL DEFAULT 'manual',
    backfill     INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    discovered   INTEGER NOT NULL DEFAULT 0,
    stored       INTEGER NOT NULL DEFAULT 0,
    deduped      INTEGER NOT NULL DEFAULT 0,
    refreshed    INTEGER NOT NULL DEFAULT 0,
    errors       INTEGER NOT NULL DEFAULT 0,
    not_found    INTEGER NOT NULL DEFAULT 0,
    rate_limited INTEGER NOT NULL DEFAULT 0,
    watermark    TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_runs_key ON source_runs(source_key, run_id DESC);

CREATE TABLE IF NOT EXISTS watches (
    watch_id         BIGSERIAL PRIMARY KEY,
    name             TEXT NOT NULL,
    spec_json        TEXT NOT NULL,
    cadence_minutes  INTEGER NOT NULL DEFAULT 1440,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_run_at      TEXT,
    last_result_json TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichment_misses (
    kind         TEXT NOT NULL,
    key          TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE TABLE IF NOT EXISTS effects_refresh (
    stable_id     TEXT PRIMARY KEY,
    outstanding   INTEGER NOT NULL DEFAULT 0,
    affecting     TEXT,
    checks        INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_checked  TEXT,
    next_check_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    doc_id          TEXT NOT NULL,
    chunk_id        INTEGER NOT NULL,
    vector          vector NOT NULL,
    chunk_text      TEXT NOT NULL,
    tsv             tsvector,
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
CREATE INDEX IF NOT EXISTS embeddings_family_idx ON embeddings (provider, model, model_version);
CREATE INDEX IF NOT EXISTS embeddings_tsv_idx ON embeddings USING GIN (tsv);

-- Human-confirmable resolution suggestions ("Possibly: …?" tick/cross) — see catalogue._DDL.
CREATE TABLE IF NOT EXISTS match_suggestions (
    ref            TEXT NOT NULL,
    suggested_id   TEXT NOT NULL,
    kind           TEXT NOT NULL,
    reason         TEXT,
    extracted_parties TEXT,
    context        TEXT,
    held           INTEGER NOT NULL DEFAULT 1,
    score          REAL,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (ref, suggested_id)
);
CREATE INDEX IF NOT EXISTS match_suggestions_status_idx ON match_suggestions (status);

-- Reader passages flagged for refinement-logic review — see catalogue._DDL.
CREATE TABLE IF NOT EXISTS refinement_flags (
    flag_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id         TEXT NOT NULL,
    anchor         TEXT,
    selected_text  TEXT NOT NULL,
    context        TEXT,
    current_links  TEXT,
    note           TEXT,
    status         TEXT NOT NULL DEFAULT 'open',
    created_at     TEXT NOT NULL
);
"""
