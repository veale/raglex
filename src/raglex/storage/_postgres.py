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


def is_postgres_dsn(dsn: str) -> bool:
    return dsn.startswith("postgresql://") or dsn.startswith("postgres://")


def vector_literal(vec: list[float]) -> str:
    """pgvector text input form: ``[v1,v2,...]``."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class PgConnShim:
    """Make a psycopg connection look enough like a sqlite3 connection for the
    shared catalogue code: ``execute(sql, params)`` (translating ``?`` → ``%s``),
    ``executescript``, ``commit``, ``close``. Rows are dicts (``row['col']`` and
    ``dict(row)`` both work, matching ``sqlite3.Row`` usage)."""

    def __init__(self, raw) -> None:
        self.raw = raw

    def execute(self, sql: str, params=()):  # noqa: ANN001
        return self.raw.execute(sql.replace("?", "%s"), params)

    def executescript(self, script: str) -> None:
        for stmt in script.split(";"):
            if stmt.strip():
                self.raw.execute(stmt)
        self.raw.commit()

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        self.raw.close()


def connect(dsn: str) -> PgConnShim:
    import psycopg
    from psycopg.rows import dict_row

    raw = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
    return PgConnShim(raw)


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
    fetched_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_source_idx ON documents (source);
CREATE INDEX IF NOT EXISTS documents_ecli_idx ON documents (ecli);
CREATE INDEX IF NOT EXISTS documents_payload_hash_idx ON documents (payload_hash);

CREATE TABLE IF NOT EXISTS relations (
    relation_id        BIGSERIAL PRIMARY KEY,
    src_id             TEXT NOT NULL,
    dst_id             TEXT,
    raw_citation_string TEXT,
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

CREATE TABLE IF NOT EXISTS pending_resolution (
    string_hash      TEXT PRIMARY KEY,
    raw_citation_string TEXT NOT NULL,
    src_id           TEXT,
    hints_json       TEXT NOT NULL DEFAULT '{}',
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_attempt_at  TEXT,
    cite_count       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS citation_aliases (
    alias    TEXT PRIMARY KEY,
    dst_id   TEXT NOT NULL,
    source   TEXT
);

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
"""
