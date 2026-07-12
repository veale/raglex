-- RagLex catalogue schema — PostgreSQL (the production spine, §7).
--
-- REFERENCE COPY. The DDL the application actually executes lives in
-- `src/raglex/storage/_postgres.py` (PG_DDL) and is applied on startup; the local/dev
-- SQLite backend in `storage/catalogue.py` mirrors these tables in portable form.
-- This file is the annotated, idiomatic-Postgres rendering of the same shapes — it
-- documents intent (BOOLEAN, JSONB, DATE, foreign keys) where the runtime favours one
-- shared code path with SQLite (INTEGER 0/1, TEXT-encoded JSON and timestamps).
--
-- If you add a table, add it in BOTH runtime DDLs and here. This file previously
-- described eight fewer tables than production had, which made it worse than useless
-- to anyone reading it to understand the system.
--
-- Mirrors Appendix B. Everything-but-raw is a re-derivable projection (§1.2):
-- the corpus only ever adds and annotates, never hard-deletes (§1.4a).

CREATE EXTENSION IF NOT EXISTS vector;

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
    meta_json        JSONB,                        -- adapter-supplied metadata bag (record.extra)
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
--
-- candidate_id / raw_fold are computed ONCE at write time. Resolution, the hanging-
-- reference worklist and the coverage aggregates all key off them, so each is indexed
-- SQL rather than the matcher ladder re-run over millions of edges on every read.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS relations (
    relation_id        BIGSERIAL PRIMARY KEY,
    src_id             TEXT NOT NULL REFERENCES documents (stable_id),
    dst_id             TEXT,                       -- nullable until resolved (§5b)
    raw_citation_string TEXT,                      -- kept after resolution for audit
    candidate_id       TEXT,                       -- normalised target id; NULL = name-only
    raw_fold           TEXT,                       -- case/accent-folded raw; named-alias join key
    resolution_status  TEXT NOT NULL DEFAULT 'pending', -- resolved|pending|ambiguous|unresolvable|suppressed
    relationship_type  TEXT NOT NULL DEFAULT 'mentions',
    extracted_via      TEXT NOT NULL DEFAULT 'structured', -- structured|regex|llm|manual|inferred
    context_chunk_id   TEXT,                       -- chunk the link was found in (§1.3a)
    src_anchor         TEXT,                       -- pinpoint in the source (§1.9)
    dst_anchor         TEXT,                       -- pinpoint in the target (§1.9)
    context_start      INTEGER,                    -- char span of the citation, for treatment
    context_end        INTEGER
);
CREATE INDEX IF NOT EXISTS relations_src_idx ON relations (src_id);
CREATE INDEX IF NOT EXISTS relations_dst_idx ON relations (dst_id);
CREATE INDEX IF NOT EXISTS idx_relations_status ON relations (resolution_status);
-- The pending slice is the only hot one (~6% of edges) and it is what the resolver
-- and the worklist scan; partial indexes keep them off the other 94%.
CREATE INDEX IF NOT EXISTS relations_pending_candidate_idx ON relations (candidate_id)
    WHERE resolution_status = 'pending';
CREATE INDEX IF NOT EXISTS relations_pending_fold_idx ON relations (raw_fold)
    WHERE resolution_status = 'pending';

-- citation_aliases — the *rules* map: "Schrems II" → ECLI:EU:C:2020:559, a CELEX → its
-- ECLI-keyed judgment, a chamber-less UK slug → the real one (§5b). NOT a memo cache of
-- every resolved string: candidate_id already makes that lookup O(1).
CREATE TABLE IF NOT EXISTS citation_aliases (
    alias    TEXT PRIMARY KEY,
    dst_id   TEXT NOT NULL,
    source   TEXT                                  -- named|celex-ecli|chamber-alias
);

-- ---------------------------------------------------------------------------
-- citations — the raw extraction *observations* (§5), one per occurrence, with entity
-- kind, candidate, pinpoint, char span, method + confidence. Many collapse to one
-- `relations` edge; these are kept as the auditable record.
-- ---------------------------------------------------------------------------
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
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS citations_src_idx ON citations (src_id);

-- Rolled-up citation frequencies — the substrate for the §5a snowball. Aggregating the
-- 10M-row `citations` table live costs ~13s, so it is rebuilt on a cadence instead of
-- on page load. No PK: entity_kind is nullable and the table is rebuilt wholesale.
CREATE TABLE IF NOT EXISTS citation_counts (
    candidate_id  TEXT NOT NULL,
    entity_kind   TEXT,
    method        TEXT,
    sample        TEXT,
    occurrences   INTEGER NOT NULL DEFAULT 0,
    documents     INTEGER NOT NULL DEFAULT 0,
    rebuilt_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS citation_counts_occ_idx ON citation_counts (occurrences DESC);
-- point lookups by target id (search results' cited_by column) — without this the
-- per-row probe degrades to a filter scan of the whole roll-up
CREATE INDEX IF NOT EXISTS citation_counts_cand_idx ON citation_counts (candidate_id);

-- Version history (§1.4): a document is a series of versions. The `documents` row is
-- "latest"; prior versions are archived here before it advances. Raw + text are
-- content-addressed and immutable, so the old pointers stay valid.
CREATE TABLE IF NOT EXISTS document_versions (
    stable_id     TEXT NOT NULL,
    version       INTEGER NOT NULL,
    payload_hash  TEXT,
    raw_path      TEXT,
    text_path     TEXT,
    title         TEXT,
    decision_date DATE,
    extracted_via TEXT,
    archived_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stable_id, version)
);

-- Files attached to any document (§1.9): commentary PDF, annotation, note, LLM summary,
-- scanned exhibit. added_by keeps human and machine material separable (§10).
CREATE TABLE IF NOT EXISTS document_assets (
    asset_id     BIGSERIAL PRIMARY KEY,
    doc_id       TEXT NOT NULL,
    kind         TEXT NOT NULL,                    -- commentary|annotation|note|summary|exhibit
    path         TEXT,
    mime         TEXT,
    payload_hash TEXT,
    added_by     TEXT NOT NULL DEFAULT 'user',
    title        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS document_assets_doc_idx ON document_assets (doc_id);

-- ---------------------------------------------------------------------------
-- sources — per-source run-state + the counters the §8 alerting layer watches.
-- The DB is the orchestration state (§5): a crashed run resumes from the
-- watermark and the pending queues; no external orchestrator needed.
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

-- Saved harvest plans (§5a) — seed + degrees + cadence, run by the scheduler.
CREATE TABLE IF NOT EXISTS watches (
    watch_id         BIGSERIAL PRIMARY KEY,
    name             TEXT NOT NULL,
    spec_json        JSONB NOT NULL,
    cadence_minutes  INTEGER NOT NULL DEFAULT 1440,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at      TIMESTAMPTZ,
    last_result_json JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Background jobs (§8) — durable and cross-process. A job is a row named by (kind,
-- params), NOT a closure: any process can re-launch it, the scheduler's own work shows
-- up in the UI's jobs panel, and `cancel` crosses container boundaries.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    label         TEXT NOT NULL,
    params_json   JSONB NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'running', -- running|done|error|cancelled|interrupted
    progress_json JSONB NOT NULL DEFAULT '{}',
    log_json      JSONB NOT NULL DEFAULT '[]',
    result_json   JSONB,
    origin        TEXT NOT NULL DEFAULT 'api',     -- api|scheduler
    cancel        BOOLEAN NOT NULL DEFAULT FALSE,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at  TIMESTAMPTZ,                     -- stall detection
    finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, started_at);

-- Enrichment cool-downs. A key whose external lookup came back EMPTY is skipped for a
-- while so we don't burn quota re-asking. `kind` separates the two meanings that must
-- never be conflated: `harvest-miss` = the source said no such document (long TTL);
-- `harvest-retry` = we couldn't tell — timeout, 5xx (short TTL, hours). Conflating them
-- is how one bad afternoon at a source writes a whole worklist off for three months.
CREATE TABLE IF NOT EXISTS enrichment_misses (
    kind         TEXT NOT NULL,                    -- harvest-miss|harvest-retry|cjeu_title|changes-feed
    key          TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, key)
);

-- Outstanding-effects re-check queue (§0): legislation whose XML carried unapplied
-- amendments (the editorial lag). Only suspected-stale instruments are re-pulled, on a
-- backing-off cadence of weeks. A zero count deletes the row.
CREATE TABLE IF NOT EXISTS effects_refresh (
    stable_id     TEXT PRIMARY KEY,
    outstanding   INTEGER NOT NULL DEFAULT 0,
    affecting     JSONB,                           -- amending instruments (also amended_by edges)
    checks        INTEGER NOT NULL DEFAULT 0,      -- drives the backoff
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked  TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------------------
-- Rule-based tagging engine (§4a).
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
    method             TEXT NOT NULL,              -- literal|regex|grep_like|field|citation|graph|semantic|script|manual|eurlex
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

-- ---------------------------------------------------------------------------
-- Embeddings + chunk FTS (§6b/§6c/§6d). pgvector for ANN cosine, tsvector+GIN for the
-- lexical half. (provider, model, model_version, dimensions) is the comparability
-- FAMILY — vectors only compare within one, so a model swap is a new family, never an
-- overwrite. The HNSW index is a PARTIAL EXPRESSION index per dimension, because the
-- column holds several families; see Catalogue.create_vector_index.
-- ---------------------------------------------------------------------------
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

-- Human-confirmable resolution suggestions ("Possibly: …?" tick/cross): sub-threshold or
-- ambiguous matches the automatic matchers refuse, surfaced for a person to decide.
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

-- Reader passages flagged "for improved refinement" — location, selection, what it links
-- to now, and what the user says it should do; reviewed later to improve linking logic.
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
