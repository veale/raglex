-- RagLex canonical catalogue schema — PostgreSQL (the production spine, §7).
--
-- This is the authoritative DDL. The local/dev SQLite backend in
-- storage/catalogue.py mirrors these tables in portable form for running without
-- a live Postgres; pgvector/FTS/partitioning (steps 9–11) are layered on here.
--
-- Mirrors Appendix B. Everything-but-raw is a re-derivable projection (§1.2):
-- the corpus only ever adds and annotates, never hard-deletes (§1.4a).

-- ---------------------------------------------------------------------------
-- documents — polymorphic, versioned (§1.3, §1.4). Primary + secondary share it.
-- Partition BY doc_type | jurisdiction | year at scale (§7); declared at create.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    stable_id        TEXT PRIMARY KEY,            -- ECLI where it exists, else surrogate
    ecli             TEXT,
    source           TEXT NOT NULL,
    doc_type         TEXT NOT NULL,               -- judgment|decision|guidance|opinion|legislation|commentary|annotation|note|article
    title            TEXT,
    court            TEXT,
    decision_date    DATE,
    language         TEXT,
    source_language  TEXT,
    version          INTEGER NOT NULL DEFAULT 1,
    is_latest        BOOLEAN NOT NULL DEFAULT TRUE,
    landing_url      TEXT,
    raw_path         TEXT,                         -- pointer into the content-addressed store
    text_path        TEXT,
    meta_path        TEXT,
    payload_hash     TEXT,                         -- SHA-256 of raw bytes; content-hash dedup (§5)
    has_text         BOOLEAN NOT NULL DEFAULT FALSE,
    has_embedding    BOOLEAN NOT NULL DEFAULT FALSE,
    extracted_via    TEXT,                         -- structured|regex|llm|manual|scrape
    added_by         TEXT NOT NULL DEFAULT 'harvest', -- harvest|user|llm (§10)
    topic_tags       JSONB NOT NULL DEFAULT '[]',   -- denormalised cache of document_tags
    topic_score      REAL,
    upstream_status  TEXT NOT NULL DEFAULT 'live',  -- live|gone_404|withdrawn (NEVER deletes the row, §1.4a)
    upstream_status_at TIMESTAMPTZ,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS documents_source_idx ON documents (source);
CREATE INDEX IF NOT EXISTS documents_ecli_idx ON documents (ecli);
CREATE INDEX IF NOT EXISTS documents_payload_hash_idx ON documents (payload_hash);

-- ---------------------------------------------------------------------------
-- relations — ONE typed-edge table for the whole graph: citations AND commentary
-- links (§1.3a, §1.9). dst_id is NULLABLE: an edge exists from the moment a
-- citation string is extracted, before it resolves to a node (§5b).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS relations (
    relation_id        BIGSERIAL PRIMARY KEY,
    src_id             TEXT NOT NULL REFERENCES documents (stable_id),
    dst_id             TEXT,                       -- nullable until resolved (§5b)
    raw_citation_string TEXT,                      -- kept after resolution for audit
    resolution_status  TEXT NOT NULL DEFAULT 'pending', -- resolved|pending|ambiguous|unresolvable
    relationship_type  TEXT NOT NULL DEFAULT 'mentions',
    extracted_via      TEXT NOT NULL DEFAULT 'structured', -- structured|regex|llm|manual
    context_chunk_id   TEXT                        -- chunk the link was found in (for later LLM treatment classification §1.3a)
);
CREATE INDEX IF NOT EXISTS relations_src_idx ON relations (src_id);
CREATE INDEX IF NOT EXISTS relations_dst_idx ON relations (dst_id);

-- ---------------------------------------------------------------------------
-- pending_resolution — the §5b retry queue. A miss usually means the target
-- isn't in the corpus YET, so resolution is retried, never abandoned.
-- cite_count ranks "frequently cited but absent" → a harvest worklist (§8).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_resolution (
    string_hash      TEXT PRIMARY KEY,
    raw_citation_string TEXT NOT NULL,
    src_id           TEXT,
    hints_json       JSONB NOT NULL DEFAULT '{}',
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_attempt_at  TIMESTAMPTZ,
    cite_count       INTEGER NOT NULL DEFAULT 1
);

-- citation_aliases — maintained map "Schrems II" → ECLI:EU:C:2020:559 (§5b).
CREATE TABLE IF NOT EXISTS citation_aliases (
    alias    TEXT PRIMARY KEY,
    dst_id   TEXT NOT NULL,
    source   TEXT
);

-- ---------------------------------------------------------------------------
-- sources — per-source run-state + the counters the §8 alerting layer watches.
-- The DB is the orchestration state (§5): a crashed run resumes from the
-- watermark and the pending_* queues; no external orchestrator needed at first.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    key                  TEXT PRIMARY KEY,
    last_run             TIMESTAMPTZ,
    watermark            TEXT,                     -- incremental cursor; advance only after a clean run
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_yield_at        TIMESTAMPTZ,              -- "no new docs in X days" alert (§8)
    requires_js          BOOLEAN NOT NULL DEFAULT FALSE,
    requires_proxy       BOOLEAN NOT NULL DEFAULT FALSE
);

-- ---------------------------------------------------------------------------
-- Rule-based tagging engine (§4a) — tables ready from day one (build step 4).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tag_rules (
    rule_id            BIGSERIAL PRIMARY KEY,
    tag                TEXT NOT NULL,
    condition_tree_json JSONB NOT NULL,            -- boolean tree of predicates
    scope_json         JSONB NOT NULL DEFAULT '{}',
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    priority           INTEGER NOT NULL DEFAULT 0,
    version            INTEGER NOT NULL DEFAULT 1,  -- bumps on edit so document_tags records which fired
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    note               TEXT
);

CREATE TABLE IF NOT EXISTS document_tags (
    doc_id             TEXT NOT NULL REFERENCES documents (stable_id),
    tag                TEXT NOT NULL,
    assigned_by_rule_id BIGINT,                    -- NULL for method='manual'
    rule_version       INTEGER,
    method             TEXT NOT NULL,              -- literal|regex|grep_like|field|citation|graph|semantic|script|manual
    confidence         REAL,
    assigned_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, tag, method)
);

CREATE TABLE IF NOT EXISTS rule_runs (
    run_id         BIGSERIAL PRIMARY KEY,
    rule_id        BIGINT NOT NULL,
    rule_version   INTEGER NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    docs_evaluated INTEGER NOT NULL DEFAULT 0,
    docs_matched   INTEGER NOT NULL DEFAULT 0,
    scope_json     JSONB NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'running'
);
