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

from ..core.models import (
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    TypedRelation,
    UpstreamStatus,
)
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
    -- what the interface sorts/filters on: decision_date, else the year the
    -- ECLI or the identifier carries (see effective_date()).
    effective_date   TEXT,
    date_provenance  TEXT,
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
    search_excluded  INTEGER NOT NULL DEFAULT 0,
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
-- keyset pagination of a whole source in stable_id order (the reparse-source job) —
-- without it an ORDER BY stable_id + source filter falls back to a full scan/sort.
CREATE INDEX IF NOT EXISTS documents_source_stable_idx ON documents (source, stable_id);
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
    -- Free text on a HAND-WRITTEN edge: why it was asserted, and by what authority
    -- (a correlation table, a recital, an editor's reading). Extraction never writes
    -- here; it exists so a manual assertion carries its own justification.
    note               TEXT,
    -- representative char span of the citation in the source text, so a later
    -- pass can read the surrounding prose and classify the *treatment* (§1.3a):
    -- mentions → follows / distinguishes / overrules / applies / considers.
    context_start      INTEGER,
    context_end        INTEGER
);
CREATE INDEX IF NOT EXISTS relations_src_idx ON relations (src_id);
CREATE INDEX IF NOT EXISTS relations_dst_idx ON relations (dst_id);
-- The resolved-citation join in inherited_mentions_for matches "dst_id IN (...) OR
-- candidate_id IN (...)". Only dst_id was indexed, so that OR could not become a bitmap
-- union and Postgres walked the whole table in relation_id order: 20M rows filtered to
-- find 4,519, ~9s inside every GDPR page load.
CREATE INDEX IF NOT EXISTS relations_candidate_idx ON relations (candidate_id);
CREATE INDEX IF NOT EXISTS idx_relations_status ON relations (resolution_status);

-- Editorial, article-to-article functional lineage. This is deliberately separate
-- from citation aliases: a citation to the previous law remains literally true while
-- the reader may additionally surface it beside the corresponding current provision.
CREATE TABLE IF NOT EXISTS provision_mappings (
    mapping_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    current_doc_id      TEXT NOT NULL,
    current_anchor      TEXT NOT NULL,
    previous_doc_id     TEXT NOT NULL,
    previous_anchor     TEXT NOT NULL,
    mapping_type        TEXT NOT NULL DEFAULT 'functional_predecessor',
    -- Only inherit citations from documents decided on or before this date. A UK
    -- transposition of an EU directive inherits retained EU case law (pre-IP completion
    -- day) and not what Luxembourg decided afterwards.
    inherit_before      TEXT,
    -- Only let this jurisdiction's documents travel along the mapping (see _migrate).
    source_jurisdiction TEXT,
    note                TEXT,
    created_by          TEXT NOT NULL DEFAULT 'manual',
    confidence          REAL,
    created_at          TEXT NOT NULL,
    -- IDENTITY IS THE PAIR OF PROVISIONS, NOT THE CLAIM ABOUT THEM.
    -- mapping_type was part of this key, which made it immutable in practice:
    -- re-sending a mapping with a corrected type inserted a SECOND row beside
    -- the wrong one instead of fixing it, so hundreds of AI Act links written as
    -- 'functional_predecessor' could not be moved to 'equivalent' without
    -- deleting each by id first. One correspondence between two provisions, one
    -- row, whose type is an ordinary editable attribute.
    UNIQUE (current_doc_id, current_anchor, previous_doc_id, previous_anchor)
);
CREATE INDEX IF NOT EXISTS provision_mappings_current_idx
    ON provision_mappings (current_doc_id, current_anchor);
CREATE INDEX IF NOT EXISTS provision_mappings_previous_idx
    ON provision_mappings (previous_doc_id, previous_anchor);

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

-- The hanging-reference worklist, pre-aggregated (§5b/§8). The live form GROUP BYs the
-- pending ``relations`` slice — ~4.3M rows into ~930k groups, ~96s — which made the
-- Unresolved page crawl and the auto-drain "never start" (it built the whole worklist
-- before fetching a single item). Roll it up on the same cadence the other stats use; the
-- worklist/drain read the top of this table by citing_count in milliseconds.
CREATE TABLE IF NOT EXISTS pending_reference_stats (
    ref           TEXT PRIMARY KEY,
    candidate     TEXT,
    raw           TEXT,
    anchor        TEXT,
    methods       TEXT,
    occurrences   INTEGER NOT NULL DEFAULT 0,
    citing_count  INTEGER NOT NULL DEFAULT 0,
    echr_citing   INTEGER NOT NULL DEFAULT 0,
    rebuilt_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_reference_stats_citing_idx ON pending_reference_stats (citing_count DESC);

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
    source          TEXT NOT NULL,
    doc_type        TEXT NOT NULL,
    court           TEXT,
    yr              TEXT,
    upstream_status TEXT,
    n               INTEGER NOT NULL DEFAULT 0,
    with_text       INTEGER NOT NULL DEFAULT 0,
    embedded        INTEGER NOT NULL DEFAULT 0,
    rebuilt_at      TEXT NOT NULL
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
-- The other direction. Without it every "which citations point at this candidate" was a
-- sequential scan of the whole table — 41M rows and 11 GB on the live corpus — which is
-- what made re-keying a document take seconds instead of milliseconds: one scan per
-- referencing column, per document. A 4,000-document merge projected to 43 hours.
CREATE INDEX IF NOT EXISTS citations_candidate_idx ON citations (candidate_id);

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
-- ``doc_count`` is how many DISTINCT documents have independently established the pair,
-- and a shorthand only travels corpus-wide once it reaches SHORTHAND_MIN_DOCS. The
-- store originally recorded only ``first_doc`` and inserted ON CONFLICT DO NOTHING, so
-- it could not count at all — and could not, therefore, tell a name several drafters
-- agree on ("the CPIA") from one document's private misreading ("the BSB" for the Human
-- Rights Act).
--
-- Counting WITHOUT reintroducing the hot-row write the original design (rightly)
-- refused: the count is derived from ``learned_shorthand_docs`` below rather than
-- incremented, so re-extracting a document cannot inflate it, and once a pair is over
-- the threshold nothing is written for it again — which is precisely the hot case
-- ("GDPR" is settled after three documents and never updated by the other 700k).
CREATE TABLE IF NOT EXISTS learned_shorthands (
    shorthand    TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    entity_kind  TEXT,
    is_abbrev    INTEGER NOT NULL DEFAULT 0,
    first_doc    TEXT,
    doc_count    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (shorthand, candidate_id)
);
CREATE INDEX IF NOT EXISTS learned_shorthands_cand_idx ON learned_shorthands (candidate_id);

-- WHICH documents established a pair — the evidence behind doc_count, and the reason
-- the count is idempotent under re-extraction. Bounded, not a full log: rows stop being
-- written once the pair is over the threshold, so this holds a handful of ids per pair
-- rather than one per (document, definition) in the corpus.
CREATE TABLE IF NOT EXISTS learned_shorthand_docs (
    shorthand    TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    doc_id       TEXT NOT NULL,
    PRIMARY KEY (shorthand, candidate_id, doc_id)
);

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

-- Per-run harvest history (§keep-current). `sources` keeps only the aggregate last_run
-- + failure counter; a watch keeps only its LATEST result. This is the row-per-run log
-- the Maintain "keep-current diagnosis" view reads: how many each run discovered / stored
-- (new) / deduped / errored, whether it hit a rate limit, and what triggered it. Kept
-- bounded by a trim (keep the newest N per source) so it can't grow without limit.
CREATE TABLE IF NOT EXISTS source_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key   TEXT NOT NULL,
    watch_id     INTEGER,                 -- NULL for a direct (non-watch) harvest
    trigger      TEXT NOT NULL DEFAULT 'manual',  -- watch | manual | scheduler
    backfill     INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    discovered   INTEGER NOT NULL DEFAULT 0,
    stored       INTEGER NOT NULL DEFAULT 0,   -- genuinely NEW documents
    deduped      INTEGER NOT NULL DEFAULT 0,   -- already held (the steady state)
    refreshed    INTEGER NOT NULL DEFAULT 0,   -- held but re-fetched (upstream revision)
    errors       INTEGER NOT NULL DEFAULT 0,
    not_found    INTEGER NOT NULL DEFAULT 0,
    rate_limited INTEGER NOT NULL DEFAULT 0,
    watermark    TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_runs_key ON source_runs(source_key, run_id DESC);

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

-- Free-text index (§6c). Its own table, not a column on ``embeddings``: the
-- tsvector used to live there, so lexical search required the embedding pass to
-- have run — and it never had. On Postgres this is a real tsvector + GIN; here it
-- records the same spans so the portable backend can answer with FTS5/LIKE and the
-- job/coverage bookkeeping is identical on both.
CREATE TABLE IF NOT EXISTS doc_fts (
    doc_id      TEXT NOT NULL,
    part        INTEGER NOT NULL DEFAULT 0,
    char_start  INTEGER NOT NULL DEFAULT 0,
    char_end    INTEGER NOT NULL DEFAULT 0,
    words       INTEGER NOT NULL DEFAULT 0,
    tsv         TEXT NOT NULL DEFAULT '',
    indexed_at  TEXT NOT NULL,
    PRIMARY KEY (doc_id, part)
);

-- Structural labels are searchable content too. Keep them separately so adding an
-- article heading does not falsify the character offsets of the authoritative body.
CREATE TABLE IF NOT EXISTS doc_headings (
    doc_id      TEXT NOT NULL,
    heading_no  INTEGER NOT NULL,
    label       TEXT NOT NULL,
    char_start  INTEGER NOT NULL DEFAULT 0,
    tsv         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (doc_id, heading_no)
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

-- General user feedback (Bugs / Feature requests) from the app's feedback box, kept
-- alongside refinement_flags. metadata carries whatever page context the client sent.
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL DEFAULT 'bug',    -- bug | feature | error
    message      TEXT NOT NULL,
    page         TEXT,                           -- the view/route label the user was on
    url          TEXT,                           -- full in-app (hash) URL
    metadata     TEXT,                           -- JSON: doc_id, query, role, user-agent, …
    status       TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    created_at   TEXT NOT NULL,
    -- system-reported issues (kind='error') are deduplicated on this, and counted
    fingerprint  TEXT,
    seen_count   INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT
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
    # The same index over the FALLBACK date, which is what the corpus browser and every
    # date sort actually order by now (see effective_date).
    #
    # NULLS LAST is not decoration: "ORDER BY x DESC NULLS LAST" can only be served by an
    # index declared the same way, because a plain "(x DESC)" index is DESC NULLS FIRST.
    # Built without it, this index existed, was 588 MB, and the browse still did a
    # parallel seq scan + sort of 5.2M rows — the exact cost it was there to remove.
    "CREATE INDEX IF NOT EXISTS documents_effdate_nullslast_idx ON documents "
    "(effective_date DESC NULLS LAST, stable_id)",
    # The Unresolved page's with_citing lookup filters pending edges by
    # COALESCE(candidate_id, raw_citation_string) IN (…visible refs…) — an expression the
    # plain candidate_id index can't serve, so it seq-scanned ~1.8M pending rows per page
    # (the /unresolved 20s hang). A partial expression index on the pending slice makes it
    # an index probe. On the big live table build CONCURRENTLY by hand first; this no-ops.
    "CREATE INDEX IF NOT EXISTS relations_pending_ref_idx ON relations "
    "(COALESCE(candidate_id, raw_citation_string)) "
    "WHERE resolution_status = 'pending' AND extracted_via <> 'inferred'",
    # A consolidation/version lookup must not walk every ordinary citation to a heavily
    # cited act (GDPR/UCPD).  These tiny lineage-only indexes serve both the canonical-read
    # redirect and the batched extraction projection.  On a large live table build them
    # CONCURRENTLY first; startup's statements then become lock-free catalog no-ops.
    "CREATE INDEX IF NOT EXISTS relations_lineage_dst_idx ON relations (dst_id) "
    "WHERE relationship_type IN ('consolidates', 'point_in_time_of')",
    "CREATE INDEX IF NOT EXISTS relations_lineage_pending_candidate_idx "
    "ON relations (candidate_id) WHERE resolution_status = 'pending' AND dst_id IS NULL "
    "AND relationship_type IN ('consolidates', 'point_in_time_of')",
    # Search resolves a typed abbreviation against the shorthand store on every
    # keystroke (documents_by_shorthand). The primary key is (shorthand, candidate_id)
    # and cannot serve lower(shorthand), so without this each autocomplete keystroke
    # seq-scanned the million-row store — the shape that has starved the pool twice.
    "CREATE INDEX IF NOT EXISTS learned_shorthands_lower_idx "
    "ON learned_shorthands (lower(shorthand))",
)

_SQLITE_JSON_INDEXES = (
    # SQLite spelling of the DILA parent lookup; PostgreSQL's expression lives in
    # _PG_TRGM_INDEXES below alongside the other backend-specific indexes.
    "CREATE INDEX IF NOT EXISTS documents_fr_code_cid_idx ON documents "
    "((json_extract(meta_json, '$.code_cid'))) "
    "WHERE source = 'fr-dila' AND doc_type = 'legislation'",
)

# Postgres-only trigram indexes for substring search (§7): the corpus search matches a
# tokenised query as a SUBSTRING of the title, id, ECLI, or a citation alias
# (``lower(col) LIKE '%tok%'``) — which a btree can't serve, so without these it seq-scans
# ~5M documents (tens of seconds, the "search hangs" report). pg_trgm GIN turns each into an
# index scan. gin_trgm_ops needs the pg_trgm extension and has no SQLite analogue, so this
# runs only on Postgres. As with the date index above: on the big live table build these
# CONCURRENTLY by hand first and this plain CREATE no-ops; on a fresh/dev DB it builds inline.
_PG_TRGM_INDEXES = (
    "CREATE INDEX IF NOT EXISTS documents_title_trgm ON documents USING gin (lower(title) gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS documents_stable_id_trgm ON documents USING gin (lower(stable_id) gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS documents_ecli_trgm ON documents USING gin (lower(ecli) gin_trgm_ops)",
    # aliases are stored folded (lower-case), so index the column directly (no lower()).
    "CREATE INDEX IF NOT EXISTS citation_aliases_alias_trgm ON citation_aliases USING gin (alias gin_trgm_ops)",
    # PostgreSQL spelling of the DILA parent lookup above.  Keep this in the PG-only
    # set because SQLite's json_extract expression is not valid here.
    "CREATE INDEX IF NOT EXISTS documents_fr_code_cid_idx ON documents "
    "((meta_json::jsonb ->> 'code_cid')) "
    "WHERE source = 'fr-dila' AND doc_type = 'legislation'",
)


# DSNs whose schema this process has already ensured. Postgres DDL is idempotent but not
# free, and the catalogue is opened per request.
_PG_SCHEMA_READY: set[str] = set()


# One SQL spelling of "this anchor, ignoring how it was punctuated". Citation pinpoints
# arrive as "s. 13", segment labels as "s. 13 Compensation…", a caller may type
# "section 13", and German material uses "§ 13" — all the same provision. Every coarse
# anchor guard normalises through this, so a query can never miss an edge merely because
# the corpus and the caller spell the unit differently.
def anchor_norm_sql(column: str = "dst_anchor") -> str:
    return (f"lower(replace(replace(replace(COALESCE({column}, ''), ' ', ''), "
            "'.', ''), '§', ''))")


ANCHOR_NORM_SQL = anchor_norm_sql()


def _trailing_int(stable_id: str) -> int | None:
    """The last path segment as an int, or None when it isn't one. Ids are not all
    numeric at the tail ("ukut/aac/2019/b1", "ukftt/tc/2021/tc08273"), and treating
    that as an error rather than a non-match killed the caller's whole guard pass."""
    tail = (stable_id or "").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def decided_by_sql(alias: str = "d") -> str:
    """A date to compare a document against, in SQL, ``YYYY-MM-DD``.

    ``decision_date`` first — but 36,550 of the 60,058 EU case-law documents held
    (61%) have none, so a date filter over CJEU material would silently mis-handle most
    of it. An ECLI carries its year in the fourth field (``ECLI:EU:C:2020:559``), which
    is enough for a year-granularity cutoff, and 36,267 of those undated documents have
    one. A year-only document is treated as **31 December** of that year: for a cutoff
    that falls on a year boundary — which is what IP completion day is — that is the
    reading that neither includes a case it shouldn't nor drops one it should.

    Common law is the same problem in another dress: 68,495 held judgments carry no
    decision_date, and 68,158 of them (99.5%) carry the year in their own identifier —
    ``ewca/civ/1975/5`` is a 1975 judgment whatever the metadata failed to say. See
    :func:`effective_date`, which is this ladder in Python; the stored column it fills
    is what the interface sorts and filters on, because a CASE expression cannot use the
    (decision_date, stable_id) index and a 5M-row browse cannot afford a full sort.
    """
    # ECLI:EU:C:2020:559 — 1-based, the year begins at 11 ("ECLI:" 5 + "EU:" 3 + "C:" 2).
    ecli_year = f"substr({alias}.ecli, 11, 4)"
    return (
        f"CASE WHEN {alias}.decision_date IS NOT NULL THEN substr(CAST({alias}.decision_date AS TEXT), 1, 10) "
        f"     WHEN {alias}.ecli LIKE 'ECLI:EU:_:____:%' AND {ecli_year} BETWEEN '1950' AND '2099' "
        f"       THEN {ecli_year} || '-12-31' "
        f"     WHEN {alias}.effective_date IS NOT NULL THEN {alias}.effective_date "
        "     ELSE NULL END"
    )


# A four-digit year sitting in an identifier: ``ewca/civ/1975/5``, ``uksc/2024/12``,
# ``nswca/2017/103``. Bounded so a docket number cannot pass for a year.
_ID_YEAR_RE = re.compile(r"/((?:1[6-9]|20)\d{2})/")
_ECLI_YEAR_RE = re.compile(r"^ECLI:[A-Z]{2}:[A-Z0-9]+:((?:1[89]|20)\d{2}):", re.I)


def effective_date(decision_date, ecli: str | None,
                   stable_id: str | None) -> tuple[str | None, str]:
    """``(YYYY-MM-DD, provenance)`` — the date the interface should USE for a document.

    The ladder, and why it is in this order:

    1. ``decision_date`` — the judgment date, when the source gave one. It stays first
       even though the identifier usually agrees, because where they disagree (1,759 of
       433,607 dated common-law judgments, 0.4%) it is the metadata that is right: a
       judgment given in December is often numbered in the following year.
    2. the ECLI's year field — the existing EU rung.
    3. **the year in the identifier itself** — a neutral citation is a year plus a
       number, so ``ewca/civ/1975/5`` dates itself. This is what makes 68,158 otherwise
       undated common-law judgments sortable, filterable and citable.

    A year-only estimate becomes 31 December, matching :func:`decided_by_sql`: within
    the year the ordering is arbitrary anyway, and at a year boundary this is the
    reading that neither admits a case it shouldn't nor drops one it should. The
    provenance travels with it so the reader can say "1975 (from the citation)" rather
    than presenting an inference as a judgment date."""
    if decision_date:
        return str(decision_date)[:10], "decision_date"
    m = _ECLI_YEAR_RE.match(ecli or "")
    if m:
        return f"{m.group(1)}-12-31", "ecli"
    m = _ID_YEAR_RE.search(f"/{(stable_id or '').strip('/')}/")
    if m:
        return f"{m.group(1)}-12-31", "identifier"
    return None, "none"


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


class FtsQueryError(RuntimeError):
    """Postgres refused the compiled tsquery. Carried up rather than swallowed: an
    empty result set is a claim about the corpus, and this is a claim about the
    query."""


def _apply_filters(sql: str, params: list, filters: dict | None) -> tuple[str, list]:
    """Append a partition pre-filter (§6b.6) over the documents join: by source
    (jurisdiction), doc_type, topic tag, or minimum year — applied BEFORE both
    rankers so fusion runs over the relevant slice."""
    filters = filters or {}
    # A regulator item can remain held for dedup/audit while being deliberately
    # invisible to both lexical and semantic retrieval.
    sql += " AND d.search_excluded = 0"
    if filters.get("source"):
        vals = filters["source"]
        sql += f" AND d.source IN ({','.join('?' * len(vals))})"
        params.extend(vals)
    if filters.get("source_or_court"):
        scope = filters["source_or_court"]
        sources = list(scope.get("sources") or [])
        courts = list(scope.get("courts") or [])
        alternatives: list[str] = []
        if sources:
            alternatives.append(f"d.source IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        if courts:
            alternatives.append(f"d.court IN ({','.join('?' * len(courts))})")
            params.extend(courts)
        sql += " AND (" + " OR ".join(alternatives or ["1 = 0"]) + ")"
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


def _fts_parts(text: str, cap: int, *, word_cap: int = 10_000) -> list[tuple[int, int]]:
    """Split text below PostgreSQL's character *and positional-word* ceilings.

    A tsvector retains at most 16,383 word positions.  A 160k-character judgment can
    therefore fit comfortably below the 1 MB vector limit while every phrase position
    near its conclusion collapses onto 16,383.  Keep generous headroom for punctuation
    that PostgreSQL tokenises into more lexemes than Python's whitespace count.
    """
    n = len(text or "")
    if not n:
        return [(0, 0)]
    spans: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + cap, n)
        words = 0
        for match in re.compile(r"\S+").finditer(text, start, end):
            words += 1
            if words > word_cap:
                end = match.start()
                break
        if end < n:
            window = text.rfind("\n\n", start + (end - start) // 2, end)
            if window > start:
                end = window + 2
        spans.append((start, end))
        start = end
    return spans


def _isodate(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _json_meta(meta_json: str | None) -> dict:
    """A row's ``meta_json`` decoded, ``{}`` when absent or unparseable — for the scans
    that read metadata off many rows at once (``document_meta`` is the single-row form)."""
    if not meta_json:
        return {}
    try:
        decoded = json.loads(meta_json)
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


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
            # The date the INTERFACE uses: the judgment date where the source gave
            # one, else the year carried by the ECLI or by the identifier itself.
            # Stored rather than computed because every sort, filter and facet
            # reads it, and a CASE expression cannot use the date index.
            ("documents", "effective_date", "TEXT"),
            ("documents", "date_provenance", "TEXT"),
            ("relations", "candidate_id", "TEXT"),
            ("relations", "raw_fold", "TEXT"),
            # when this document's citations were last (re-)extracted — the durable
            # "last rescanned at" stamp a staleness-scoped rescan skips against (§5).
            ("documents", "last_extracted_at", "TEXT"),
            # A durable per-pass marker: a resumed citation scan excludes documents
            # already stamped with its root run id, regardless of ordering/new inserts.
            ("documents", "last_extraction_run_id", "TEXT"),
            # Retain citation-free regulator items for content-hash dedup/audit, but
            # omit them from retrieval and embedding.
            ("documents", "search_excluded", "INTEGER NOT NULL DEFAULT 0"),
            ("jobs", "root_job_id", "TEXT"),
            ("jobs", "resumed_from", "TEXT"),
            ("jobs", "resume_policy", "TEXT NOT NULL DEFAULT 'restart'"),
            ("jobs", "attempt", "INTEGER NOT NULL DEFAULT 1"),
            ("jobs", "checkpoint_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("jobs", "restart_requested", "INTEGER NOT NULL DEFAULT 0"),
            ("jobs", "lease_heartbeat_at", "TEXT"),
            # upstream_status in the homepage roll-up → the §8 stats endpoint derives its
            # by_upstream_status breakdown from the roll-up instead of a full documents scan
            # (four such scans blew the statement timeout after the fr-dila 1.7M import).
            ("corpus_shape_stats", "upstream_status", "TEXT"),
            # A learned shorthand switched off by hand in the admin panel. Blocking
            # rather than deleting is what makes the decision stick: the row is
            # insert-only and the next rescan of any document that defines the name
            # would simply learn it again.
            ("learned_shorthands", "blocked", "INTEGER NOT NULL DEFAULT 0"),
            # How many DISTINCT documents established this pair — the popularity gate.
            # Existing rows arrive as 0 and are back-filled from the citations table
            # (backfill_learned_shorthand_doc_counts); until that runs they are
            # document-local, which is the safe direction.
            ("learned_shorthands", "doc_count", "INTEGER NOT NULL DEFAULT 0"),
            # System-reported problems land in the same queue as user feedback and
            # refinement flags (kind='error'), so one review surface covers all three.
            # A systemic failure repeats — 13,862 identical harvest failures in one run —
            # so an issue is identified by a FINGERPRINT and counted, never re-inserted.
            ("feedback", "fingerprint", "TEXT"),
            ("feedback", "seen_count", "INTEGER NOT NULL DEFAULT 1"),
            ("feedback", "last_seen_at", "TEXT"),
            # Why a HAND-WRITTEN edge exists. Provision mappings have always had a note,
            # which is what makes an editorial convention greppable and a mistake
            # recoverable; a manual edge had nowhere to record its reasoning at all, so a
            # wrong one left no trace of what was intended.
            ("relations", "note", "TEXT"),
            # A provision mapping may only inherit citations from documents decided ON
            # OR BEFORE this date. Set automatically for a UK transposition (retained EU
            # case law ends at IP completion day) and overridable per mapping.
            ("provision_mappings", "inherit_before", "TEXT"),
            # Which jurisdiction's work may travel along a mapping. An assimilated UK
            # regulation is word-for-word its EU original, so the EU material on an
            # article is genuinely about the same provision — but the rest of Europe's
            # is not: every member state's courts and DPAs cite the GDPR too, and
            # projecting all of it onto the UK instrument would bury it. NULL keeps the
            # historical behaviour (anything may pass).
            ("provision_mappings", "source_jurisdiction", "TEXT"),
        ):
            migration_error: Exception | None = None
            try:
                if self.backend == "postgres":
                    exists = self.conn.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = ? AND column_name = ?", (table, col)).fetchone()
                    if not exists:
                        self.conn.execute("SET lock_timeout = '5s'")
                        try:
                            self.conn.execute(
                                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {decl}")
                        finally:
                            self.conn.execute("SET lock_timeout = 0")
                else:
                    cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                    if col not in cols:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except Exception as exc:  # noqa: BLE001 — startup continues, but visibly
                migration_error = exc
            if migration_error is not None:
                # A timed-out ALTER used to disappear here. Re-check the schema: an
                # independent process may have completed the same migration while we
                # waited, in which case there is no issue. Otherwise put the failure in
                # the operator's normal feedback/error queue instead of declaring the
                # API healthy with a column every subsequent query expects.
                try:
                    if self.backend == "postgres":
                        still_missing = self.conn.execute(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = ? AND column_name = ?", (table, col)
                        ).fetchone() is None
                    else:
                        still_missing = col not in {
                            r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                    if still_missing:
                        self.record_issue(
                            fingerprint=f"schema-migration:{table}.{col}",
                            message=(f"schema migration did not add {table}.{col}: "
                                     f"{type(migration_error).__name__}: {migration_error}"),
                            page="startup:migrate",
                            metadata=json.dumps({"table": table, "column": col,
                                                 "declaration": decl}),
                        )
                except Exception:  # noqa: BLE001 — feedback schema may itself be migrating
                    pass
        try:
            self._migrate_mapping_identity()
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
        # Postgres-only trigram indexes (substring search). Ensure the extension, then the
        # same check-before-create guard so a startup never blocks on a write lock.
        if self.backend == "postgres":
            try:
                self.conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            except Exception:  # noqa: BLE001 — no CREATE privilege / already present
                pass
            for stmt in _PG_TRGM_INDEXES:
                try:
                    name = re.search(r"IF NOT EXISTS\s+([a-z0-9_]+)", stmt, re.I).group(1)
                    if self.conn.execute(
                            "SELECT 1 FROM pg_class WHERE relname = ? AND relkind = 'i'",
                            (name,)).fetchone():
                        continue
                    self.conn.execute("SET lock_timeout = '5s'")
                    try:
                        self.conn.execute(stmt)
                    finally:
                        self.conn.execute("SET lock_timeout = 0")
                except Exception:  # noqa: BLE001 — a migration mustn't block startup
                    pass
        else:
            for stmt in _SQLITE_JSON_INDEXES:
                try:
                    self.conn.execute(stmt)
                except Exception:  # noqa: BLE001 — JSON1 may be absent in old SQLite
                    pass

    _MAPPING_PAIR_COLS = ("current_doc_id", "current_anchor",
                          "previous_doc_id", "previous_anchor")

    def _migrate_mapping_identity(self) -> None:
        """Drop ``mapping_type`` out of the provision-mapping unique key.

        It was part of the key, so the type of an existing mapping could not be changed:
        re-sending the pair with a corrected type inserted a second, contradictory row
        beside the first. A build of several hundred AI Act correspondences written as
        'functional_predecessor' therefore had no way back to 'equivalent' short of
        deleting every row by id. Identity is the pair of provisions; the type is an
        attribute of it.

        Where a pair really does have two rows already, the SURVIVOR IS THE LATEST — the
        later write is the correction, which is the whole reason it was made."""
        pair = ", ".join(self._MAPPING_PAIR_COLS)
        keep = (f"SELECT MAX(mapping_id) FROM provision_mappings GROUP BY {pair}")
        if self.backend == "postgres":
            stale = [r for r in self.conn.execute(
                "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
                "WHERE conrelid = 'provision_mappings'::regclass AND contype = 'u'")
                if "mapping_type" in (r["def"] or "")]
            if not stale:
                return
            self.conn.execute("SET lock_timeout = '5s'")
            try:
                self.conn.execute(
                    f"DELETE FROM provision_mappings WHERE mapping_id NOT IN ({keep})")
                for r in stale:
                    self.conn.execute("ALTER TABLE provision_mappings "
                                      f'DROP CONSTRAINT "{r["conname"]}"')
                self.conn.execute(
                    "ALTER TABLE provision_mappings ADD CONSTRAINT "
                    f"provision_mappings_pair_key UNIQUE ({pair})")
            finally:
                self.conn.execute("SET lock_timeout = 0")
            self.conn.commit()
            return
        # SQLite cannot drop a constraint, so the table is rebuilt from the current DDL.
        stale = False
        for idx in self.conn.execute("PRAGMA index_list(provision_mappings)").fetchall():
            if not idx["unique"]:
                continue
            cols = [c["name"] for c in
                    self.conn.execute(f"PRAGMA index_info({idx['name']})")]
            stale = stale or "mapping_type" in cols
        if not stale:
            return
        create = re.search(
            r"CREATE TABLE IF NOT EXISTS provision_mappings \(.*?\n\);", _DDL, re.S)
        cols = [r["name"] for r in
                self.conn.execute("PRAGMA table_info(provision_mappings)")]
        names = ", ".join(cols)
        self.conn.execute("DROP INDEX IF EXISTS provision_mappings_current_idx")
        self.conn.execute("DROP INDEX IF EXISTS provision_mappings_previous_idx")
        self.conn.execute("ALTER TABLE provision_mappings RENAME TO _pm_old")
        self.conn.executescript(create.group(0))
        self.conn.execute(
            f"INSERT INTO provision_mappings ({names}) SELECT {names} FROM _pm_old "
            f"WHERE mapping_id IN (SELECT MAX(mapping_id) FROM _pm_old GROUP BY {pair})")
        self.conn.execute("DROP TABLE _pm_old")
        self.conn.execute("CREATE INDEX IF NOT EXISTS provision_mappings_current_idx "
                          "ON provision_mappings (current_doc_id, current_anchor)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS provision_mappings_previous_idx "
                          "ON provision_mappings (previous_doc_id, previous_anchor)")
        self.conn.commit()

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

    def get_documents(self, stable_ids: list[str]) -> dict:
        """``{stable_id: row}`` for many documents in one round trip.

        The cited-by page enriched each of its rows (title, court, OSCOLA, jurisdiction)
        with a separate ``get_document`` — up to 200 sequential single-row queries for
        one page view, which on a heavily-cited authority is the difference between a
        fast response and a timeout.
        """
        ids = list(dict.fromkeys(str(i) for i in stable_ids if i))
        out: dict = {}
        # Stay under SQLite's 999-bind ceiling; Postgres is happier with modest IN lists.
        for start in range(0, len(ids), 800):
            chunk = ids[start:start + 800]
            qs = ",".join("?" * len(chunk))
            for row in self.conn.execute(
                    f"SELECT * FROM documents WHERE stable_id IN ({qs})", chunk):
                out[str(row["stable_id"])] = row
        return out

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

    def note_refetch(self, stable_id: str, *, source_language: str | None = None,
                     commit: bool = True) -> None:
        """Record that a held document was re-fetched, even though its bytes were
        unchanged and it therefore never reached :meth:`upsert_document`.

        ``fetched_at`` is otherwise written ONLY on a store, which quietly breaks any
        backoff that keys on it: the pending-CJEU feed re-fetches a decision to see
        whether an English rendition has appeared, gets the same bytes, dedups — and the
        row still shows the last date it *changed*, not the last date it was *checked*.
        So the "not for another 14 days" window opens once and then never re-arms, and
        the feed re-downloads the same thousands of documents on every run for ever.

        ``source_language`` is the other half: a document whose language was never
        recorded can never satisfy an "is it English yet?" test, so it stays due for ever
        no matter how often it is checked. The rendition we just downloaded is
        authoritative about that, so write it down.
        """
        sets = ["fetched_at = ?"]
        params: list = [_now()]
        if source_language:
            sets.append("source_language = COALESCE(source_language, ?)")
            params.append(source_language)
            sets.append("language = COALESCE(language, ?)")
            params.append(source_language)
        self.conn.execute(
            f"UPDATE documents SET {', '.join(sets)} WHERE stable_id = ?",
            (*params, stable_id))
        if commit:
            self.conn.commit()

    def reprefix_documents(self, old_prefixes: tuple[str, ...], new_prefix: str, *,
                           commit: bool = True) -> dict[str, int]:
        """Bulk-move every document whose id starts ``<old>/`` to ``<new_prefix>/<rest>``,
        cascading the same references :meth:`rekey_document` does — but as ONE statement
        per referencing column instead of one per document.

        Why this exists: ``rekey_document`` issues ~11 statements per document, and the
        big referencing tables are not indexed on every id column (``citations`` holds
        41M rows with no plain index on ``candidate_id``). Re-keying 822 documents one at
        a time therefore meant ~9,000 sequential scans of multi-million-row tables — four
        hours, and slowing. Set-based, it is 11 scans total. This is the same lesson the
        resolve path already learned: never do per-document work that can be done per
        batch.

        **Rename only.** It cannot merge, so the caller must have established that no
        target id is already taken (:meth:`get_document`), and it does not touch the
        uniqueness-key conflict handling that a merge needs. Callers with collisions must
        use ``rekey_document`` for those and this for the rest.
        """
        if not old_prefixes:
            return {}
        counts: dict[str, int] = {}
        # "uk-cma/government/x" → "govuk/government/x". The prefixes are unambiguous
        # because the pattern includes the separator: "uk-cma/%" cannot match
        # "uk-cma-guidance/…". substr() is 1-based in both SQLite and Postgres, so the
        # remainder starts at len(prefix) + 2 (past the prefix and its slash).
        #
        # References are moved BEFORE the documents row, matching rekey_document's order.
        # The LIKE pattern is a bound PARAMETER, never a literal in the SQL — a literal %
        # breaks the Postgres driver's paramstyle translation.
        with self._atomic():
            for table, col in (*self._DOC_ID_REFS, ("documents", "stable_id")):
                for prefix in old_prefixes:
                    cur = self.conn.execute(
                        f"UPDATE {table} SET {col} = ? || substr({col}, ?) "
                        f"WHERE {col} LIKE ?",
                        (f"{new_prefix}/", len(prefix) + 2, f"{prefix}/%"))
                    moved = getattr(cur, "rowcount", 0) or 0
                    if moved > 0:
                        counts[f"{table}.{col}"] = counts.get(f"{table}.{col}", 0) + moved
        if commit:
            self.conn.commit()
        return counts

    # For a MERGE, the (col, uniqueness-key) that would clash if the target already holds
    # an equivalent row — those old rows are dropped instead of moved.
    _UNIQUE_KEYCOLS = {
        ("embeddings", "doc_id"): ("chunk_id", "provider", "model", "model_version"),
        ("document_tags", "doc_id"): ("tag",),
        ("document_versions", "stable_id"): ("version",),
        ("citation_aliases", "dst_id"): ("alias",),
        ("learned_shorthands", "candidate_id"): ("shorthand",),
    }

    def record_rendition(self, stable_id: str, source: str, foreign_id: str, *,
                         commit: bool = True) -> None:
        """Note that another register also publishes this authority, WITHOUT minting a
        second document for it (see Pipeline._reconcile_identity). The corpus keeps one
        node per judgment; this records where else it can be read."""
        # The foreign id points AT the held document, so the pipeline's prefilter can
        # answer "held?" for it without paying a fetch. Without this the reconciliation
        # is invisible to the next run: the other register's id was never registered
        # anywhere the crawl looks, so every pass re-downloaded every rendition only to
        # fold it away again — permanently, for as long as the source stayed scheduled.
        # overwrite=False, for the same reason adapter aliases use it: a key that
        # already names a held document keeps naming it.
        self.put_alias(foreign_id, stable_id, source="de-rendition",
                       commit=False, overwrite=False)
        meta = self.document_meta(stable_id)
        rends = [r for r in (meta.get("renditions") or []) if isinstance(r, dict)]
        if any(r.get("source") == source and r.get("id") == foreign_id for r in rends):
            if commit:
                self.commit()
            return
        rends.append({"source": source, "id": foreign_id})
        meta["renditions"] = rends
        self.set_document_meta(stable_id, meta, commit=commit)

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

    # A CN/TN notice is retired by the DECIDING document only.  An Advocate General's
    # Opinion (CC/CA) and a View (CV/CP) are filed in the same case and often months
    # before judgment — they do not end the case, so they must never suppress the
    # notice.  Only a judgment (CJ/TJ) or an order (CO/TO) closes it.
    _DECISION_CELEX_RE = re.compile(r"^6\d{4}[CTF][JO]\d{4}$", re.IGNORECASE)

    def retire_pending_eu_notice(self, notice_id: str, decision_id: str) -> bool:
        """Hide a CN/TN application notice once its full English decision is held.

        The notice remains stored for audit/dedup and is linked from the resolving
        decision with ``supersedes``.  Returns False when the notice is not held (or is
        not a CN/TN identifier), making the operation safe on every scheduled pass.

        Retirement also hands the guessed judgment CELEX back to the real decision: while
        pending, the notice deliberately owns that alias (so a citation of "C-801/24"
        resolves to something), and any edge that resolved through it — most visibly an
        AG Opinion's ``opinion_in`` — is re-pointed at the decision here.  Left alone, an
        Opinion went on announcing a retired notice as its judgment.
        """
        if not re.fullmatch(r"6\d{4}[CT]N\d{4}", notice_id or "", re.IGNORECASE):
            return False
        notice = self.get_document(notice_id)
        decision = self.get_document(decision_id)
        if notice is None or decision is None or not decision["has_text"] \
                or str(decision["source_language"] or "").lower() != "en":
            return False
        # The decision's own CELEX decides whether it CAN retire: a document stored
        # under its ECLI carries it in meta. Where there is no CELEX to read, fall back
        # to what the document IS — a judgment or an order closes the case; an Opinion
        # (doc_type "opinion") and another notice never do, whatever else is true of
        # them.
        decision_celex = str((self.document_meta(decision_id) or {}).get("celex")
                             or decision_id)
        if re.fullmatch(r"6\d{4}[CTF][A-Z]\d{4}", decision_celex, re.IGNORECASE):
            if not self._DECISION_CELEX_RE.match(decision_celex):
                return False
        elif str(decision["doc_type"]) not in ("judgment", "decision"):
            return False
        notice_celex = str((self.document_meta(notice_id) or {}).get("celex") or notice_id)
        meta = self.document_meta(notice_id)
        meta.update({
            "pending": False,
            "resolved_by": decision_id,
            "search_exclusion_reason": "superseded_by_full_english_decision",
        })
        with self._atomic():
            existing = self.conn.execute(
                "SELECT 1 FROM relations WHERE src_id = ? AND dst_id = ? "
                "AND relationship_type = 'supersedes' LIMIT 1",
                (decision_id, notice_id),
            ).fetchone()
            if existing is None:
                self._add_relation(decision_id, TypedRelation(
                    relationship_type=RelationshipType.SUPERSEDES,
                    raw_citation_string=notice_id, dst_id=notice_id,
                    extracted_via=ExtractedVia.STRUCTURED,
                    resolution_status=ResolutionStatus.RESOLVED,
                ))
            self.conn.execute(
                "UPDATE documents SET search_excluded = 1, meta_json = ? WHERE stable_id = ?",
                (json.dumps(meta), notice_id),
            )
            # The alias the notice was holding in trust, and the edges that took it.
            for alias in {a.casefold() for a in ((meta.get("aliases") or [])
                                                 + [decision_celex]) if a}:
                self.conn.execute(
                    "UPDATE citation_aliases SET dst_id = ?, source = 'celex-ecli' "
                    "WHERE alias = ? AND dst_id = ?",
                    (decision_id, alias, notice_id),
                )
            self.conn.execute(
                "UPDATE relations SET dst_id = ? WHERE dst_id = ? AND src_id <> ? "
                "AND LOWER(raw_citation_string) <> LOWER(?) "
                "AND relationship_type <> 'supersedes'",
                (decision_id, notice_id, decision_id, notice_celex),
            )
        return True

    def resolved_pending_eu_notices(self, limit: int = 5000) -> list[tuple[str, str]]:
        """``(notice_id, decision_id)`` for every still-visible CN/TN notice whose
        deciding document is already held in full English.

        Retirement used to depend on the pending feed happening to re-enumerate the
        resolving decision while the notice was in the corpus.  Anything harvested the
        other way round — the judgment arriving through the ordinary CJEU feed — stayed
        "Pending:" indefinitely (220 notices on the live corpus, some two years old, one
        of them fronting a judgment we hold).  This is the order-independent sweep: it
        pairs on the case identity the notice itself asserts (same year, court and case
        number; J or O descriptor), which is exactly the pairing the dossier gives.
        """
        notices = self.conn.execute(
            "SELECT stable_id, meta_json FROM documents "
            "WHERE doc_type = 'note' AND search_excluded = 0 AND source = 'eu-cellar' "
            "LIMIT ?", (limit,)).fetchall()
        wanted: dict[str, str] = {}   # decision CELEX → notice id
        for row in notices:
            celex = str((_json_meta(row["meta_json"]) or {}).get("celex")
                        or row["stable_id"]).upper()
            if not re.fullmatch(r"6\d{4}[CTF]N\d{4}", celex):
                continue
            for descriptor in ("J", "O"):
                wanted[celex[:6] + descriptor + celex[7:]] = row["stable_id"]
        if not wanted:
            return []
        # The CELEX of a decision held under its ECLI lives in meta, which no index can
        # answer — but the whole EU slice is ~64k rows against a 5M-document corpus, and
        # this runs once a day, so one scan of that slice is cheaper than 8,000 lookups.
        pairs: list[tuple[str, str]] = []
        for row in self.conn.execute(
            "SELECT stable_id, meta_json FROM documents WHERE source = 'eu-cellar' "
            "AND has_text = 1 AND LOWER(COALESCE(source_language, '')) = 'en'"
        ).fetchall():
            celex = str((_json_meta(row["meta_json"]) or {}).get("celex")
                        or row["stable_id"]).upper()
            notice_id = wanted.get(celex)
            if notice_id and notice_id != row["stable_id"]:
                pairs.append((notice_id, row["stable_id"]))
        return list(dict.fromkeys(pairs))

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
                effective_date, date_provenance,
                language, source_language, version, is_latest, landing_url,
                raw_path, text_path, payload_hash, has_text, search_excluded,
                extracted_via, added_by,
                topic_tags, topic_score, upstream_status, fetched_at, meta_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(stable_id) DO UPDATE SET
                ecli=excluded.ecli, source=excluded.source, doc_type=excluded.doc_type,
                title=excluded.title, court=excluded.court,
                decision_date=excluded.decision_date,
                effective_date=excluded.effective_date,
                date_provenance=excluded.date_provenance, language=excluded.language,
                source_language=excluded.source_language, version=excluded.version,
                landing_url=excluded.landing_url, raw_path=excluded.raw_path,
                text_path=excluded.text_path, payload_hash=excluded.payload_hash,
                has_text=excluded.has_text,
                -- A re-harvest must never UN-hide a document that was hidden by a
                -- curatorial decision the adapter knows nothing about. A CN/TN notice
                -- retired because its full English judgment landed is re-enumerated by
                -- the pending feed forever after; taking excluded.search_excluded
                -- verbatim resurrected it into search on the next pass (67 notices on
                -- the live corpus). Exclusion is therefore sticky: a record can SET it,
                -- only an explicit unretire clears it.
                search_excluded=CASE WHEN documents.search_excluded = 1 THEN 1
                                     ELSE excluded.search_excluded END,
                extracted_via=excluded.extracted_via,
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
                # the interface's date, derived once on write rather than per query
                *effective_date(_isodate(record.decision_date), record.ecli,
                                record.stable_id),
                record.language,
                record.source_language,
                version,
                1,
                record.landing_url,
                raw_path,
                text_path,
                record.payload_hash,
                1 if record.text else 0,
                1 if record.extra.get("search_excluded") else 0,
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

    def upsert_manual_relation(
        self, src_id: str, rel: TypedRelation, *, note: str | None = None,
    ) -> dict:
        """Write ONE hand-authored edge, in place if it already exists.

        Its identity is ``(src_id, dst_id/candidate, relationship_type, src_anchor,
        dst_anchor)``. Re-running the same assertion previously minted a second row —
        while the neighbouring provision-mapping call correctly updated in place, so one
        of a pair of adjacent operations was safe to re-run and the other silently
        duplicated. Returns the row's id and whether it was created or updated, because
        a caller checking its own work cannot otherwise tell.
        """
        candidate_id, raw_fold = self._edge_keys(rel)
        target = rel.dst_id or candidate_id
        existing = self.conn.execute(
            """
            SELECT relation_id FROM relations
            WHERE src_id = ?
              AND COALESCE(dst_id, candidate_id, '') = ?
              AND relationship_type = ?
              AND COALESCE(src_anchor, '') = ?
              AND COALESCE(dst_anchor, '') = ?
              AND extracted_via = 'manual'
            ORDER BY relation_id
            LIMIT 1
            """,
            (src_id, target or "", str(rel.relationship_type),
             rel.src_anchor or "", rel.dst_anchor or ""),
        ).fetchone()
        if existing:
            relation_id = int(existing["relation_id"])
            self.conn.execute(
                "UPDATE relations SET dst_id = ?, candidate_id = ?, raw_fold = ?, "
                "raw_citation_string = ?, resolution_status = ?, "
                "note = COALESCE(?, note) WHERE relation_id = ?",
                (rel.dst_id, candidate_id, raw_fold, rel.raw_citation_string,
                 str(rel.resolution_status), note, relation_id),
            )
            self.conn.commit()
            return {"relation_id": relation_id, "created": False}
        self._add_relation(src_id, rel)
        row = self.conn.execute(
            """
            SELECT relation_id FROM relations
            WHERE src_id = ? AND COALESCE(dst_id, candidate_id, '') = ?
              AND relationship_type = ? AND extracted_via = 'manual'
            ORDER BY relation_id DESC LIMIT 1
            """,
            (src_id, target or "", str(rel.relationship_type)),
        ).fetchone()
        relation_id = int(row["relation_id"]) if row else 0
        if relation_id and note:
            self.conn.execute(
                "UPDATE relations SET note = ? WHERE relation_id = ?",
                (note, relation_id))
        self.conn.commit()
        return {"relation_id": relation_id, "created": True}

    def delete_manual_relation(self, relation_id: int) -> dict:
        """Retract ONE hand-written edge, and only if it is hand-written.

        The alternative — ``correct_citation(suppress=True)`` — suppresses the whole
        relation, which on a document pair that also has a genuine extracted citation
        takes the real one down as collateral. A manual assertion must be removable
        without touching what the extractor found.
        """
        row = self.conn.execute(
            "SELECT relation_id, src_id, dst_id, candidate_id, relationship_type, "
            "src_anchor, dst_anchor, extracted_via, note FROM relations "
            "WHERE relation_id = ?",
            (relation_id,),
        ).fetchone()
        if row is None:
            return {"deleted": False, "error": f"no relation {relation_id}"}
        if str(row["extracted_via"]) != "manual":
            return {"deleted": False, "relation_id": relation_id,
                    "extracted_via": str(row["extracted_via"]),
                    "error": "only a manual edge can be deleted here; use "
                             "correct_citation(suppress=True) for an extracted one"}
        with self._atomic():
            self.conn.execute(
                "DELETE FROM relations WHERE relation_id = ? AND extracted_via = 'manual'",
                (relation_id,))
        return {"deleted": True, "relation_id": relation_id,
                "src_id": str(row["src_id"]),
                "dst_id": row["dst_id"] or row["candidate_id"],
                "relationship_type": str(row["relationship_type"]),
                "src_anchor": row["src_anchor"], "dst_anchor": row["dst_anchor"]}

    def manual_relations(
        self, *, src_id: str | None = None, dst_id: str | None = None,
        limit: int = 500,
    ) -> list:
        """Every hand-written edge touching a document, each with its own relation_id —
        the list a caller needs in order to retract one."""
        clauses, params = ["extracted_via = 'manual'"], []
        if src_id:
            clauses.append("src_id = ?")
            params.append(src_id)
        if dst_id:
            clauses.append("COALESCE(dst_id, candidate_id) = ?")
            params.append(dst_id)
        params.append(max(1, min(int(limit), 5000)))
        return self.conn.execute(
            "SELECT relation_id, src_id, dst_id, candidate_id, relationship_type, "
            "src_anchor, dst_anchor, note, resolution_status FROM relations "
            f"WHERE {' AND '.join(clauses)} ORDER BY relation_id LIMIT ?",
            tuple(params),
        ).fetchall()

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

    # -- provision lineage -------------------------------------------------
    def upsert_provision_mappings(
        self, current_doc_id: str, previous_doc_id: str, mappings: list[dict],
        *, created_by: str = "manual", replace: bool = False,
    ) -> int:
        """Store current-provision → previous-provision functional mappings.

        These are editorial mappings, not citation aliases and not synthetic citation
        edges. The previous citation remains intact and can be surfaced as inherited
        context without claiming that its author cited the current law.

        Re-sending a pair UPDATES it, ``mapping_type`` included — the correspondence is
        identified by the two provisions, and the type is a claim ABOUT it that an editor
        (or an agent that got it wrong the first time) may correct in place.
        """
        now = _now()
        with self._atomic():
            if replace:
                self.conn.execute(
                    "DELETE FROM provision_mappings WHERE current_doc_id = ? "
                    "AND previous_doc_id = ?",
                    (current_doc_id, previous_doc_id),
                )
            for item in mappings:
                self.conn.execute(
                    """
                    INSERT INTO provision_mappings (
                        current_doc_id, current_anchor, previous_doc_id,
                        previous_anchor, mapping_type, inherit_before,
                        source_jurisdiction, note,
                        created_by, confidence, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT (
                        current_doc_id, current_anchor, previous_doc_id,
                        previous_anchor
                    ) DO UPDATE SET
                        mapping_type = excluded.mapping_type,
                        inherit_before = excluded.inherit_before,
                        source_jurisdiction = excluded.source_jurisdiction,
                        note = excluded.note,
                        created_by = excluded.created_by,
                        confidence = excluded.confidence
                    """,
                    (
                        current_doc_id, item["current_anchor"], previous_doc_id,
                        item["previous_anchor"],
                        item.get("mapping_type") or "functional_predecessor",
                        item.get("inherit_before"),
                        item.get("source_jurisdiction"),
                        item.get("note"), created_by, item.get("confidence"), now,
                    ),
                )
        return len(mappings)

    def retype_provision_mappings(
        self, current_doc_id: str, *, to_type: str,
        previous_doc_id: str | None = None, from_type: str | None = None,
        mapping_ids: list[int] | None = None,
        current_anchor: str | None = None, previous_anchor: str | None = None,
        dry_run: bool = False,
    ) -> int:
        """Change the CLAIM on existing mappings without rewriting them.

        The correspondences are the expensive part — hundreds of anchor pairs, each
        resolved against both laws' segments. Getting the type wrong across a whole build
        (every AI Act link written as 'functional_predecessor' when they are companion
        provisions) should not mean re-deriving the table; it is one attribute. Returns
        the number of rows affected, and with ``dry_run`` the number that WOULD be."""
        where = ["current_doc_id = ?", "mapping_type <> ?"]
        params: list[object] = [current_doc_id, to_type]
        if previous_doc_id:
            where.append("previous_doc_id = ?")
            params.append(previous_doc_id)
        if from_type:
            where.append("mapping_type = ?")
            params.append(from_type)
        if mapping_ids:
            clean_ids = list(dict.fromkeys(int(x) for x in mapping_ids))
            where.append(f"mapping_id IN ({','.join('?' for _ in clean_ids)})")
            params.extend(clean_ids)
        if current_anchor:
            where.append("lower(trim(current_anchor)) = lower(trim(?))")
            params.append(current_anchor)
        if previous_anchor:
            where.append("lower(trim(previous_anchor)) = lower(trim(?))")
            params.append(previous_anchor)
        clause = " AND ".join(where)
        if dry_run:
            return self.conn.execute(
                f"SELECT COUNT(*) AS n FROM provision_mappings WHERE {clause}",
                params).fetchone()["n"]
        cur = self.conn.execute(
            f"UPDATE provision_mappings SET mapping_type = ? WHERE {clause}",
            [to_type, *params])
        self.conn.commit()
        return max(cur.rowcount, 0)

    def _mapping_doc_ids(self, current_doc_id: str) -> list[str]:
        """This document plus the base act it is a version of.

        A mapping is written against the act ("DPA 1998 → DPA 2018"), not against one
        dated expression of it. When a later consolidation or point-in-time snapshot of
        that act is held, opening it must carry the same lineage forward — otherwise the
        editorial work vanishes the moment the text is updated, which is exactly when a
        reader most needs to know where a provision came from.
        """
        base = self.consolidation_base_for(current_doc_id)
        return [current_doc_id, base] if base and base != current_doc_id else [current_doc_id]

    def provision_mappings(self, current_doc_id: str) -> list:
        ids = self._mapping_doc_ids(current_doc_id)
        qs = ",".join("?" * len(ids))
        return self.conn.execute(
            f"""
            SELECT pm.*, d.title AS previous_title,
                   CASE WHEN pm.current_doc_id = ? THEN 0 ELSE 1 END
                       AS mapping_from_base_act
            FROM provision_mappings pm
            LEFT JOIN documents d ON d.stable_id = pm.previous_doc_id
            WHERE pm.current_doc_id IN ({qs})
            ORDER BY pm.current_anchor, pm.previous_doc_id, pm.previous_anchor
            """,
            (current_doc_id, *ids),
        ).fetchall()

    def delete_provision_mapping(self, mapping_id: int) -> bool:
        with self._atomic():
            changed = self.conn.execute(
                "DELETE FROM provision_mappings WHERE mapping_id = ?", (mapping_id,)
            ).rowcount
        return bool(changed)

    def inherited_mentions_for(
        self, current_doc_id: str, *, current_anchor: str | None = None,
        limit: int | None = 600,
    ) -> list:
        """Literal citations to mapped previous provisions, decorated with lineage.

        Anchor matching folds case, spaces and punctuation and nothing else. Citation
        extraction emits canonical ``Article N`` / ``s. N`` anchors, so looser numeric
        matching would leak citations between unrelated provisions — but an exact string
        comparison was too tight in the other direction: the corpus stores ``Sch. 2`` and
        an editor writes ``Sch 2``, and a full-stop silently voided the mapping.

        BOTH SIDES travel their version family. A mapping written against the base act
        applies to its dated versions (:meth:`_mapping_doc_ids`), and — the half that
        made the DPA retrofit surface nothing at all — a mapping written against a dated
        snapshot of the OLD act picks up the citations filed against its base. The 260
        DPA 1998 → 2018 mappings name ``ukpga/1998/29@2015-01-01``; every judgment cites
        ``ukpga/1998/29``. Nothing joined, and the reader showed an empty panel.
        """
        ids = self._mapping_doc_ids(current_doc_id)
        id_qs = ",".join("?" * len(ids))
        mapping_params: list = list(ids)
        anchor_sql = ""
        if current_anchor:
            anchor_sql = " AND lower(trim(pm.current_anchor)) = lower(trim(?))"
            mapping_params.append(current_anchor)
        # Which previous documents are in play, and therefore whose version families
        # need expanding. Small — a law has a handful of mapped predecessors, not a
        # handful of thousands — so this stays one extra round trip, not a per-row join.
        previous_ids = [
            str(row["previous_doc_id"]) for row in self.conn.execute(
                f"SELECT DISTINCT previous_doc_id FROM provision_mappings pm "
                f"WHERE pm.current_doc_id IN ({id_qs}){anchor_sql}",
                tuple(mapping_params),
            ).fetchall() if row["previous_doc_id"]
        ]
        if not previous_ids:
            return []
        family: dict[str, list[str]] = {}
        for previous_id in previous_ids:
            related = {previous_id}
            base = self.consolidation_base_for(previous_id)
            if base:
                related.add(base)
            for version_id, _date in self.legislative_versions(base or previous_id):
                related.add(version_id)
            family[previous_id] = sorted(related)
        # One branch per mapped predecessor, so a family stays tied to the mapping row it
        # belongs to. A flat "target IN (everything)" would let a citation of one old act
        # satisfy a mapping written about a different one whenever their anchors agree —
        # and "s. 7" agrees across half the statute book.
        clauses, join_params = [], []
        for previous_id, group in family.items():
            qs = ",".join("?" * len(group))
            clauses.append(f"(pm.previous_doc_id = ? AND "
                           f"(r.dst_id IN ({qs}) OR r.candidate_id IN ({qs})))")
            join_params.extend([previous_id, *group, *group])
        # A citation of "s. 33A(1)" is a citation of the provision a mapping of "s. 33A"
        # speaks about, exactly as a provision heading represents its family everywhere
        # else in the reader. Exact-only matching lost 241 of the DPA 1998's pinpointed
        # citations — every one of them to a subsection of a provision that IS mapped.
        # The "(" is load-bearing: it stops "s. 3" claiming "s. 33A(1)".
        anchor_match = (
            f"({anchor_norm_sql('r.dst_anchor')} = {anchor_norm_sql('pm.previous_anchor')}"
            f" OR {anchor_norm_sql('r.dst_anchor')} LIKE "
            f"{anchor_norm_sql('pm.previous_anchor')} || '(%')"
        )
        family_sql = " OR ".join(clauses)
        decided = decided_by_sql("s")
        from ..core.jurisdictions import sql_lock_match

        jurisdiction_match, lock_params = sql_lock_match("s.source",
                                                         "pm.source_jurisdiction")
        # ``limit=None`` means every row, for the callers that count rather than page
        # (see :meth:`version_inherited_mentions_for`).
        params = [*join_params, *mapping_params, *lock_params]
        if limit is not None:
            params.append(max(1, int(limit)))
        return self.conn.execute(
            f"""
            SELECT r.*, pm.mapping_id, pm.current_anchor AS inherited_current_anchor,
                   pm.previous_doc_id AS inherited_from_id,
                   pm.previous_anchor AS inherited_from_anchor,
                   pm.note AS mapping_note, pm.created_by AS mapping_created_by,
                   pm.confidence AS mapping_confidence,
                   -- what the mapping CLAIMS: an earlier iteration (history) or a
                   -- companion instrument's parallel provision (in force alongside)
                   pm.mapping_type AS mapping_type,
                   pm.inherit_before AS inherit_before,
                   pm.source_jurisdiction AS source_jurisdiction,
                   d.title AS inherited_from_title
            FROM provision_mappings pm
            JOIN relations r
              ON ({family_sql})
             AND {anchor_match}
            LEFT JOIN documents d ON d.stable_id = pm.previous_doc_id
            JOIN documents s ON s.stable_id = r.src_id
            WHERE pm.current_doc_id IN ({id_qs})
              {anchor_sql}
              -- A UK transposition inherits RETAINED EU case law only: what Luxembourg
              -- decided after IP completion day does not govern the domestic provision,
              -- and presenting it as inherited authority would be a legal error, not an
              -- untidy result. An undated citer is excluded rather than assumed current.
              AND (pm.inherit_before IS NULL
                   OR ({decided} IS NOT NULL AND {decided} <= pm.inherit_before))
              -- …and only the jurisdiction the mapping admits. An assimilated UK
              -- regulation is word-for-word its EU original, so Luxembourg's reading of
              -- an article is genuinely about the same words — but every member state
              -- reads the GDPR too, and letting all of it through would bury the UK
              -- instrument under the rest of Europe rather than inform it.
              AND (pm.source_jurisdiction IS NULL OR {jurisdiction_match})
              AND r.src_id <> pm.current_doc_id
              AND r.relationship_type IN (
                'mentions','interprets','applies','considers','follows',
                'distinguishes','overrules','cites_for_fact'
              )
              AND r.extracted_via <> 'inferred'
            ORDER BY r.relation_id
            {"" if limit is None else "LIMIT ?"}
            """,
            tuple(params),
        ).fetchall()

    def consolidation_base_for(self, version_id: str) -> str | None:
        """The base act behind a dated version of it, if ``version_id`` is one.

        Read this from the durable lineage edge rather than reconstructing an identifier:
        non-EU adapters also publish consolidations, and their ids do not follow CELEX.

        BOTH lineage predicates count. An EU consolidation carries ``consolidates``; a
        legislation.gov.uk / NZ / eISB point-in-time snapshot carries
        ``point_in_time_of``. They are separate edges because they mean different things
        for *reading* (a snapshot is never opened by default), but for *citation
        inheritance* they are the same fact: the citers of ukpga/1998/29 are the citers
        of ukpga/1998/29@2015-01-01, which otherwise showed none at all.
        """
        row = self.conn.execute(
            """
            SELECT COALESCE(dst_id, candidate_id) AS base_id
            FROM relations
            WHERE src_id = ?
              AND relationship_type IN ('consolidates', 'point_in_time_of')
              AND COALESCE(dst_id, candidate_id) IS NOT NULL
            ORDER BY (resolution_status = 'resolved') DESC, relation_id
            LIMIT 1
            """,
            (version_id,),
        ).fetchone()
        return str(row["base_id"]) if row and row["base_id"] else None

    def consolidation_families_for(
        self, version_ids: list[str],
    ) -> dict[str, tuple[str, str | None]]:
        """Batch ``version_id -> (base_id, version_date)`` for citing-side collapse."""
        ids = list(dict.fromkeys(str(i) for i in version_ids if i))
        if not ids:
            return {}
        # PostgreSQL's extended-query protocol caps one statement at 65,535 bind
        # parameters. Mega-authorities (the ECHR, GDPR) can have more distinct incoming
        # rows than that; version-family collapse must not make their reader fail merely
        # by building one enormous ``IN`` clause.
        rows = []
        for start in range(0, len(ids), 40_000):
            chunk = ids[start:start + 40_000]
            qs = ",".join("?" * len(chunk))
            rows.extend(self.conn.execute(
                f"""
                SELECT r.src_id, r.src_id AS stable_id,
                       COALESCE(r.dst_id, r.candidate_id) AS base_id,
                       d.meta_json, r.dst_anchor
                FROM relations r JOIN documents d ON d.stable_id = r.src_id
                WHERE r.src_id IN ({qs})
                  AND r.relationship_type IN ('consolidates', 'point_in_time_of')
                  AND COALESCE(r.dst_id, r.candidate_id) IS NOT NULL
                """,
                chunk,
            ).fetchall())
        out: dict[str, tuple[str, str | None]] = {}
        for row in rows:
            out[str(row["src_id"])] = (
                str(row["base_id"]), self._version_date(row),
            )
        return out

    def applicable_consolidation(
        self, base_id: str, on_date: str | None = None,
    ) -> tuple[str, str] | None:
        """Latest held *readable* consolidation applicable on ``on_date``.

        Point-in-time snapshots are intentionally excluded: opening an ordinary act may
        default to its current consolidated text, but must never jump to an arbitrary
        historical snapshot merely because one has been fetched.

        A TEXTLESS version is skipped, not returned. Consolidations are published
        language by language, so a version can be held only as a metadata record with no
        text to fetch at all — the DSA's sole consolidation (02022R2065-20221027) exists
        in eight languages, none of them English, so every read of the DSA redirected to
        a blank page while the base act sat complete beside it. A transient harvest
        failure leaves an identical shape. Falling back to the newest version that does
        have text (or, failing that, to the base act) keeps the read useful; nothing is
        concealed, because ``legislative_status`` still reports every held version.
        """
        cutoff = str(on_date or date.today().isoformat())[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff):
            cutoff = date.today().isoformat()
        rows = self.conn.execute(
            """
            SELECT d.stable_id, d.meta_json, d.has_text, r.dst_anchor
            FROM relations r JOIN documents d ON d.stable_id = r.src_id
            WHERE (r.dst_id = ? OR r.candidate_id = ?)
              AND r.relationship_type = 'consolidates'
            """,
            (base_id, base_id),
        ).fetchall()
        versions = sorted({
            (str(row["stable_id"]), version_date)
            for row in rows
            if row["has_text"]
            and (version_date := self._version_date(row)) and version_date <= cutoff
        }, key=lambda item: (item[1], item[0]))
        return versions[-1] if versions else None

    def latest_readable_version(self, stable_id: str, on_date: str | None = None
                                ) -> str | None:
        """The id an instrument should be READ (or published) under today, or None when
        that is the id given.

        Same rule as :meth:`applicable_consolidation` — newest held expression that has
        text and is not future-dated — but it accepts a dated expression as well as a
        base act, and it covers the version series that carry no ``consolidates`` edge.
        Only the CELLAR consolidations have those edges (10,098 of them); the Dutch
        ``BWBR…@date`` series and the assimilated ``european/…@date`` series have none,
        so an edge-only lookup silently reported "already current" for every one of them.
        """
        from ..core.text import fold
        from ..eu_law import consolidation_base, is_consolidation

        given = str(stable_id or "")
        if not given:
            return None
        # A RETIRED id resolves to whatever it was folded into. Merging the assimilated
        # duplicates left every stored reference to eur/2016/679 pointing at a document
        # that no longer exists — including a configured static edition, which then
        # failed its build with "document not found" rather than following the move. The
        # merge minted the alias precisely so nothing would break; this is what reads it.
        if self.get_document(given) is None:
            survivor = self.get_alias(fold(given))
            if survivor and survivor != given and self.get_document(survivor) is not None:
                return self.latest_readable_version(survivor, on_date) or survivor
        # A CELLAR consolidation names its base act in a different SECTOR (02002L0058-…
        # consolidates 32002L0058), so the family is found through the edge, not the id.
        base = consolidation_base(given) if is_consolidation(given) else None
        if base is None:
            stem, version = self.version_base_and_date(given)
            base = stem if version else given
        current = self.applicable_consolidation(base, on_date)
        if current is not None:
            return current[0] if current[0] != given else None
        # No edges for this family: fall back to the id shape. Both the base row and its
        # dated siblings are considered, and the same collapse rule picks between them.
        rows = self.conn.execute(
            "SELECT stable_id, has_text, meta_json, NULL AS dst_anchor FROM documents "
            "WHERE stable_id = ? OR stable_id LIKE ?",
            (base, f"{base}@%"),
        ).fetchall()
        if not rows:
            return None
        best = self.collapse_version_rows(rows, on_date=on_date)
        winner = str(best[0]["stable_id"]) if best else given
        return winner if winner != given else None

    def version_inherited_mentions_for(
        self, version_id: str, *, limit: int | None = 5000,
        anchor_exact: str | None = None, anchor_prefixes: list[str] | None = None,
        anchor_like: list[str] | None = None,
    ) -> list:
        """Literal mentions of a consolidation's base act, projected onto the version.

        The original relation is never rewritten.  Its anchor is retained, so matching
        Articles/sections share their citers automatically and a citation to a provision
        introduced only in the consolidation (for example Article 1a) can surface there
        even though that anchor is absent from the enacted text.  Direct citations to the
        dated version are combined by callers and take precedence when deduplicating.

        ``limit=None`` means every row. Interactive readers pass a bound because they
        show a page at a time and report the true total separately; anything that COUNTS
        — the static export above all — must not, because the ordering here is
        PageRank-descending and a bound therefore deletes the long tail specifically
        rather than a random slice. The GDPR consolidation has 87,558 projectable
        mentions: capped at 20,000, its exported editions lost three quarters of every
        provision's citers, and the loss was invisible because the survivors were the
        famous ones.
        """
        base_id = self.consolidation_base_for(version_id)
        if not base_id:
            return []
        anchor_sql = ""
        anchor_params: list[str] = []
        if anchor_exact:
            # ``replace`` + ``lower`` is deliberately portable between SQLite and
            # PostgreSQL.  The facade repeats the full whitespace-normalised comparison
            # after this indexed-size prefilter, so tabs/newlines cannot create a false
            # positive here.
            anchor_sql = (
                " AND lower(replace(COALESCE(r.dst_anchor, ''), ' ', '')) = ?"
            )
            anchor_params.append(anchor_exact.lower().replace(" ", ""))
        elif anchor_prefixes or anchor_like:
            # This is only a coarse SQL prefilter: ``art6%`` may also retrieve Article 60,
            # which the facade's exact family matcher then rejects.  It nevertheless turns
            # a 350k-edge mega-authority request into a small query. Normalised, and one
            # branch per spelling of the unit, so "s. 13" / "section 13" / "§ 13" all hit.
            # ``anchor_like`` adds whole patterns for the pinpoints no prefix can reach —
            # a paragraph RANGE ("para 135-140") answering a request for one paragraph.
            norm = ANCHOR_NORM_SQL.replace("dst_anchor", "r.dst_anchor")
            likes = " OR ".join([f"{norm} LIKE ?"]
                                * (len(anchor_prefixes or []) + len(anchor_like or [])))
            anchor_sql = f" AND ({likes})"
            anchor_params.extend(prefix + "%" for prefix in (anchor_prefixes or []))
            anchor_params.extend(anchor_like or [])
        return self.conn.execute(
            f"""
            SELECT r.*, ? AS version_inherited_from_id,
                   ? AS version_inherited_current_id,
                   d.title AS version_inherited_from_title,
                   COALESCE(a.pagerank, 0) AS src_pagerank
            FROM relations r
            LEFT JOIN documents d ON d.stable_id = ?
            LEFT JOIN doc_authority a ON a.doc_id = r.src_id
            WHERE (r.dst_id = ? OR r.candidate_id = ?)
              AND r.src_id NOT IN (?, ?)
              AND r.relationship_type IN (
                'mentions','interprets','applies','considers','follows',
                'distinguishes','overrules','cites_for_fact'
              )
              AND r.extracted_via <> 'inferred'
              {anchor_sql}
            ORDER BY src_pagerank DESC, r.relation_id
            {"" if limit is None else "LIMIT ?"}
            """,
            tuple((
                base_id, version_id, base_id, base_id, base_id,
                version_id, base_id,
            ) + tuple(anchor_params) + (
                () if limit is None else (max(1, int(limit)),)
            )),
        ).fetchall()

    def version_combined_citer_count(self, version_id: str) -> int | None:
        """Distinct direct-or-base citers for a consolidation; ``None`` for a base act."""
        base_id = self.consolidation_base_for(version_id)
        if not base_id:
            return None
        row = self.conn.execute(
            """
            SELECT COUNT(DISTINCT src_id) AS n
            FROM relations
            WHERE (dst_id IN (?, ?) OR candidate_id = ?)
              AND src_id NOT IN (?, ?)
              AND relationship_type IN (
                'mentions','interprets','applies','considers','follows',
                'distinguishes','overrules','cites_for_fact'
              )
              AND extracted_via <> 'inferred'
            """,
            (version_id, base_id, base_id, version_id, base_id),
        ).fetchone()
        return int(row["n"] or 0)

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

    def clear_citations(self, src_id: str, *, keep_manual: bool = True,
                        commit: bool = True) -> None:
        """Drop a document's citation observations before a re-extraction. ``keep_manual``
        (default) spares rows the user authored by hand (``method='manual'``): a highlight-
        to-link anchored citation must survive every later rescan, exactly as a manually-
        suppressed edge already survives ``clear_relations``."""
        if keep_manual:
            self.conn.execute(
                "DELETE FROM citations WHERE src_id = ? AND (method IS NULL OR method != 'manual')",
                (src_id,))
        else:
            self.conn.execute("DELETE FROM citations WHERE src_id = ?", (src_id,))
        if commit:
            self.conn.commit()

    def add_manual_citation(self, src_id: str, *, candidate_id: str, raw: str,
                            char_start: int, char_end: int, pinpoint: str | None = None,
                            entity_kind: str | None = None, commit: bool = True) -> None:
        """Record a user-authored anchored citation at a text span (highlight-to-link).
        Idempotent per (src, span, target): re-linking the same selection replaces the row
        rather than stacking duplicates."""
        self.conn.execute(
            "DELETE FROM citations WHERE src_id = ? AND method = 'manual' "
            "AND char_start = ? AND char_end = ? AND candidate_id = ?",
            (src_id, char_start, char_end, candidate_id))
        self.conn.execute(
            """INSERT INTO citations (src_id, raw, entity_kind, candidate_id, pinpoint,
                   char_start, char_end, method, confidence, created_at)
               VALUES (?,?,?,?,?,?,?, 'manual', 1.0, ?)""",
            (src_id, raw, entity_kind, candidate_id, pinpoint, char_start, char_end, _now()))
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

    def citations_for_many(self, src_ids: "list[str]") -> list[sqlite3.Row]:
        """The offset-bearing citation rows for a batch of documents, in one query — the
        re-anchor pass reads a whole reparse chunk's citations at once instead of a point
        SELECT per document."""
        if not src_ids:
            return []
        qs = ",".join("?" * len(src_ids))
        return self.conn.execute(
            f"SELECT citation_id, src_id, raw, char_start, char_end FROM citations "
            f"WHERE src_id IN ({qs}) ORDER BY src_id, char_start", list(src_ids)).fetchall()

    def relation_spans_for_many(self, src_ids: "list[str]") -> list[sqlite3.Row]:
        """The offset-bearing EDGE rows for a batch of documents, shaped like
        ``citations_for_many`` so one re-anchor pass can walk either table. A citation's
        position is stored twice — here and on ``citations`` — and both have to move when
        the text is regenerated."""
        if not src_ids:
            return []
        qs = ",".join("?" * len(src_ids))
        return self.conn.execute(
            f"SELECT relation_id AS citation_id, src_id, raw_citation_string AS raw, "
            f"context_start AS char_start, context_end AS char_end FROM relations "
            f"WHERE src_id IN ({qs}) AND context_start IS NOT NULL "
            f"ORDER BY src_id, context_start", list(src_ids)).fetchall()

    def reanchor_citation_offsets(self, updates: "list[tuple[int, int, int]]", *,
                                  commit: bool = True) -> int:
        """Rewrite ``(citation_id, char_start, char_end)`` offsets in one batched
        ``executemany`` — the whole point of the re-anchor path is that nothing else about
        the edge (raw, candidate, pinpoint, resolved target) changes, so only these two
        columns are touched."""
        if not updates:
            return 0
        self.conn.executemany(
            "UPDATE citations SET char_start = ?, char_end = ? WHERE citation_id = ?",
            [(s, e, cid) for cid, s, e in updates])
        if commit:
            self.conn.commit()
        return len(updates)

    def reanchor_relation_offsets(self, updates: "list[tuple[int, int, int]]", *,
                                  commit: bool = True) -> int:
        """The OTHER copy of every citation's span.

        A citation's position is stored twice: ``citations.char_start/char_end``, which the
        reader highlights from, and ``relations.context_start/context_end``, which the
        "all mentions" previews mark from. Re-anchoring only the first left the two 52
        characters apart on reparsed eu-cellar judgments — the judgment page highlighted
        the citation and the preview of the same citation highlighted the words before it.

        Each update is ``(relation_id, new_start, new_end)``."""
        if not updates:
            return 0
        self.conn.executemany(
            "UPDATE relations SET context_start = ?, context_end = ? WHERE relation_id = ?",
            [(s, e, rid) for rid, s, e in updates])
        if commit:
            self.conn.commit()
        return len(updates)

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

    def db_health(self) -> dict:
        """A read-only diagnostic for "the whole thing is sluggish": planner-stat freshness,
        bloat (dead tuples), seq-scan-heavy tables (missing-index hints), unused indexes,
        buffer-cache hit ratio, connection pressure, and the longest-running queries.

        Postgres-only substance; SQLite returns a minimal stub. Everything here reads system
        catalogs/stat views — no locks, no writes."""
        if self.backend != "postgres":
            return {"backend": self.backend, "note": "detailed health metrics are Postgres-only",
                    **self.storage_size()}
        c = self.conn

        def rows(sql, params=()):
            try:
                return [dict(r) for r in c.execute(sql, params).fetchall()]
            except Exception as exc:  # noqa: BLE001 — a missing view must not break the report
                return [{"error": str(exc)}]

        def one(sql, params=()):
            r = rows(sql, params)
            return r[0] if r else {}

        cache = one(
            "SELECT sum(heap_blks_hit) AS hit, sum(heap_blks_read) AS read "
            "FROM pg_statio_user_tables")
        hit, read = (cache.get("hit") or 0), (cache.get("read") or 0)
        cache_hit_ratio = round(hit / (hit + read), 4) if (hit + read) else None

        # tables ordered by how often the planner falls back to a full scan — the classic
        # missing-index tell — but only where the table is big enough for it to matter.
        seq_heavy = rows(
            """
            SELECT relname AS table, seq_scan, idx_scan, n_live_tup AS live_rows,
                   n_dead_tup AS dead_rows,
                   CASE WHEN n_live_tup > 0 THEN round(n_dead_tup::numeric / n_live_tup, 3) END AS dead_ratio,
                   to_char(last_analyze, 'YYYY-MM-DD HH24:MI') AS last_analyze,
                   to_char(last_autoanalyze, 'YYYY-MM-DD HH24:MI') AS last_autoanalyze,
                   to_char(last_autovacuum, 'YYYY-MM-DD HH24:MI') AS last_autovacuum
            FROM pg_stat_user_tables
            WHERE n_live_tup > 10000 AND seq_scan > COALESCE(idx_scan, 0)
            ORDER BY seq_scan DESC LIMIT 12
            """)
        bloated = rows(
            """
            SELECT relname AS table, n_dead_tup AS dead_rows, n_live_tup AS live_rows,
                   round(n_dead_tup::numeric / NULLIF(n_live_tup, 0), 3) AS dead_ratio
            FROM pg_stat_user_tables
            WHERE n_dead_tup > 50000 AND n_dead_tup > n_live_tup * 0.2
            ORDER BY n_dead_tup DESC LIMIT 12
            """)
        unused_indexes = rows(
            """
            SELECT relname AS table, indexrelname AS index, idx_scan AS scans,
                   pg_size_pretty(pg_relation_size(indexrelid)) AS size
            FROM pg_stat_user_indexes s
            JOIN pg_index i ON i.indexrelid = s.indexrelid
            WHERE idx_scan < 50 AND NOT i.indisunique AND NOT i.indisprimary
              AND pg_relation_size(indexrelid) > 5 * 1024 * 1024
            ORDER BY pg_relation_size(indexrelid) DESC LIMIT 12
            """)
        conns = one(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE state = 'active') AS active, "
            "count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn "
            "FROM pg_stat_activity WHERE datname = current_database()")
        long_running = rows(
            """
            SELECT pid, state, round(extract(epoch FROM (now() - query_start))) AS seconds,
                   left(regexp_replace(query, '\\s+', ' ', 'g'), 140) AS query
            FROM pg_stat_activity
            WHERE datname = current_database() AND state <> 'idle'
              AND query_start < now() - interval '30 seconds'
              AND pid <> pg_backend_pid()
            ORDER BY query_start ASC LIMIT 8
            """)
        max_conns = one("SHOW max_connections").get("max_connections")

        hints = []
        if cache_hit_ratio is not None and cache_hit_ratio < 0.98:
            hints.append(f"buffer-cache hit ratio is {cache_hit_ratio:.1%} (<98%) — "
                         "shared_buffers may be undersized for the working set")
        stale = [t["table"] for t in seq_heavy
                 if not t.get("last_analyze") and not t.get("last_autoanalyze")]
        if stale:
            hints.append("never-analyzed large tables (stale planner stats → bad plans): "
                         + ", ".join(stale[:6]) + " — run db_maintenance(analyze=true)")
        if bloated:
            hints.append("bloated tables (dead tuples > 20% of live): "
                         + ", ".join(t["table"] for t in bloated[:6])
                         + " — run db_maintenance(vacuum=true) or tune autovacuum")
        if seq_heavy:
            hints.append("sequential-scan-heavy large tables (possible missing index): "
                         + ", ".join(t["table"] for t in seq_heavy[:6]))
        if unused_indexes:
            hints.append(f"{len(unused_indexes)} large rarely-used index(es) — dead weight on "
                         "writes/VACUUM; review before dropping")

        return {
            "backend": "postgres",
            "cache_hit_ratio": cache_hit_ratio,
            "connections": {**conns, "max": int(max_conns) if max_conns else None},
            "seq_scan_heavy_tables": seq_heavy,
            "bloated_tables": bloated,
            "unused_indexes": unused_indexes,
            "long_running_queries": long_running,
            "hints": hints or ["no obvious issues — planner stats fresh, low bloat, healthy cache"],
            **self.storage_size(),
        }

    def db_analyze(self, *, vacuum: bool = False) -> dict:
        """Refresh planner statistics (ANALYZE), optionally reclaiming bloat too
        (VACUUM ANALYZE). ANALYZE is cheap and safe; the biggest single lever for a corpus
        that grew a lot between the planner's last look. VACUUM is heavier but online (no
        exclusive lock). No-op on SQLite beyond its own ANALYZE."""
        if self.backend != "postgres":
            self.conn.execute("ANALYZE")
            self.conn.commit()
            return {"backend": self.backend, "analyzed": True, "vacuumed": False}
        # VACUUM/ANALYZE cannot run inside a transaction block; use autocommit on the raw conn.
        raw = getattr(self.conn, "raw", None)
        stmt = "VACUUM (ANALYZE)" if vacuum else "ANALYZE"
        if raw is not None:
            old = raw.autocommit
            try:
                raw.autocommit = True
                with raw.cursor() as cur:
                    cur.execute(stmt)
            finally:
                raw.autocommit = old
        else:
            self.conn.execute(stmt)
            self.conn.commit()
        return {"backend": "postgres", "analyzed": True, "vacuumed": bool(vacuum)}

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
                "(source, doc_type, court, yr, upstream_status, n, with_text, embedded, rebuilt_at) "
                "SELECT source, doc_type, court, substr(decision_date, 1, 4), upstream_status, "
                "COUNT(*), SUM(has_text), SUM(has_embedding), ? "
                "FROM documents GROUP BY source, doc_type, court, substr(decision_date, 1, 4), upstream_status",
                (_now(),),
            )
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM corpus_shape_stats").fetchone()["n"]

    def corpus_shape_stats(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT source, doc_type, court, yr, upstream_status, n, with_text, embedded "
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
                        relationship_type: str | None = None,
                        commit: bool = True) -> None:
        """Drop a source's edges from one extraction method, so re-running that
        extractor is idempotent (a re-derivable projection, §1.2). Leaves
        structurally-extracted and manual edges intact."""
        sql = "DELETE FROM relations WHERE src_id = ? AND extracted_via = ?"
        params: tuple = (src_id, extracted_via)
        if relationship_type is not None:
            sql += " AND relationship_type = ?"
            params += (relationship_type,)
        self.conn.execute(sql, params)
        if commit:
            self.conn.commit()

    def clear_relations_of_type(self, src_id: str, relationship_type: str, *,
                                commit: bool = True) -> None:
        """Drop a source's edges of one relationship type — so re-deriving them (e.g.
        re-scanning an act's affecting-side Changes feed) is idempotent without touching
        its other edges."""
        self.conn.execute(
            "DELETE FROM relations WHERE src_id = ? AND relationship_type = ?",
            (src_id, relationship_type),
        )
        if commit:
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

    @staticmethod
    def _version_date(row) -> str | None:
        """Best available ISO date for a held point-in-time legislative expression."""
        sid = str(row["stable_id"] or "")
        match = re.search(r"-(\d{4})(\d{2})(\d{2})$", sid)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        match = re.search(r"@(\d{4}-\d{2}-\d{2})$", sid)
        if match:
            return match.group(1)
        try:
            meta = json.loads(row["meta_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        currency = meta.get("currency") if isinstance(meta.get("currency"), dict) else {}
        for value in (
            meta.get("as_at"), meta.get("point_in_time"), meta.get("updated_to"),
            currency.get("as_at"), row["dst_anchor"],
        ):
            value = str(value or "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value[:10]):
                return value[:10]
        return None

    def legislative_versions(self, base_id: str) -> list[tuple[str, str]]:
        """Held dated expressions of ``base_id``, ordered oldest to newest.

        Both resolved and still-pending lineage edges count: a version and its base
        are already present when this query runs, so resolver lag must not prevent a
        temporally accurate citation link.  Keep the resolved and pending probes
        separate: an ``OR`` across ``dst_id`` and ``candidate_id`` made PostgreSQL scan
        the multi-million-row relations table for every citation target.
        """
        rows = self.conn.execute(
            """
            SELECT d.stable_id, d.meta_json, r.dst_anchor
            FROM relations r JOIN documents d ON d.stable_id = r.src_id
            WHERE r.dst_id = ?
              AND r.relationship_type IN ('consolidates', 'point_in_time_of')
            UNION ALL
            SELECT d.stable_id, d.meta_json, r.dst_anchor
            FROM relations r JOIN documents d ON d.stable_id = r.src_id
            WHERE r.candidate_id = ?
              AND r.dst_id IS NULL
              AND r.resolution_status = 'pending'
              AND r.relationship_type IN ('consolidates', 'point_in_time_of')
            """,
            (base_id, base_id),
        ).fetchall()
        versions = {
            (str(row["stable_id"]), version_date)
            for row in rows
            if (version_date := self._version_date(row))
        }
        return sorted(versions, key=lambda item: (item[1], item[0]))

    def applicable_legislative_version(
        self, base_id: str, on_date: str | None = None,
    ) -> tuple[str, str] | None:
        """Latest held consolidation in force on ``on_date`` (today if undated)."""
        cutoff = str(on_date or date.today().isoformat())[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff):
            cutoff = date.today().isoformat()
        eligible = [
            item for item in self.legislative_versions(base_id)
            if item[1] <= cutoff
        ]
        return eligible[-1] if eligible else None

    def applicable_legislative_versions(
        self, base_ids: list[str], on_date: str | None = None,
    ) -> dict[str, tuple[str, str]]:
        """Batch form of :meth:`applicable_legislative_version`.

        Citation extraction can encounter hundreds of targets in one document. Asking
        the lineage table once per citation saturated production PostgreSQL as sector-0
        coverage grew; one set query makes targets without versions cheap too.
        """
        ids = list(dict.fromkeys(str(i) for i in base_ids if i))
        if not ids:
            return {}
        cutoff = str(on_date or date.today().isoformat())[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff):
            cutoff = date.today().isoformat()
        out: dict[str, tuple[str, str]] = {}
        # Stay below SQLite's common 999-bind ceiling as well as keeping PostgreSQL plans
        # compact for citation-dense Commission documents.
        for start in range(0, len(ids), 800):
            chunk = ids[start:start + 800]
            qs = ",".join("?" * len(chunk))
            # Two indexable branches are substantially faster than COALESCE(dst_id,
            # candidate_id), which disables both btree probes and caused a full relations
            # scan for every citation-dense document in production.
            rows = self.conn.execute(
                f"""
                SELECT r.dst_id AS base_id,
                       d.stable_id, d.meta_json, r.dst_anchor
                FROM relations r JOIN documents d ON d.stable_id = r.src_id
                WHERE r.dst_id IN ({qs})
                  AND r.relationship_type IN ('consolidates', 'point_in_time_of')
                UNION ALL
                SELECT r.candidate_id AS base_id,
                       d.stable_id, d.meta_json, r.dst_anchor
                FROM relations r JOIN documents d ON d.stable_id = r.src_id
                WHERE r.candidate_id IN ({qs})
                  AND r.dst_id IS NULL
                  AND r.resolution_status = 'pending'
                  AND r.relationship_type IN ('consolidates', 'point_in_time_of')
                """,
                [*chunk, *chunk],
            ).fetchall()
            for row in rows:
                version_date = self._version_date(row)
                if not version_date or version_date > cutoff:
                    continue
                base_id = str(row["base_id"])
                candidate = (str(row["stable_id"]), version_date)
                if base_id not in out or (version_date, candidate[0]) > \
                        (out[base_id][1], out[base_id][0]):
                    out[base_id] = candidate
        return out

    def refresh_applicable_version_links(
        self, base_id: str, *, commit: bool = True,
    ) -> int:
        """Rebuild citing-document → applicable-version edges for one base law.

        Called when a new consolidation lands. It retrofits earlier citations rather
        than waiting for every citing document to be re-extracted.
        """
        versions = self.legislative_versions(base_id)
        if not versions:
            return 0
        version_ids = [item[0] for item in versions]
        placeholders = ",".join("?" * len(version_ids))
        self.conn.execute(
            f"DELETE FROM relations WHERE relationship_type = 'applicable_version' "
            f"AND dst_id IN ({placeholders})",
            version_ids,
        )
        rows = self.conn.execute(
            """
            SELECT r.src_id, r.raw_citation_string, r.dst_anchor,
                   r.context_start, r.context_end, d.decision_date, d.meta_json
            FROM relations r JOIN documents d ON d.stable_id = r.src_id
            WHERE (r.dst_id = ? OR r.candidate_id = ?)
              AND r.relationship_type IN (
                'mentions', 'interprets', 'applies', 'considers', 'follows',
                'distinguishes', 'overrules', 'cites_for_fact'
              )
              AND r.src_id <> ?
            """,
            (base_id, base_id, base_id),
        ).fetchall()
        edges_by_source: dict[str, dict[tuple[str, str | None], TypedRelation]] = {}
        for row in rows:
            try:
                meta = json.loads(row["meta_json"] or "{}")
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            reference_date = (
                str(meta.get("updated_at") or meta.get("public_updated_at") or "")[:10]
                or str(row["decision_date"] or "")[:10]
                or date.today().isoformat()
            )
            # ``versions`` is already loaded above. Calling
            # applicable_legislative_version() here performed the same DB query once
            # per citation (2,566 times for UCPD), making a three-version import look
            # frozen for minutes. Select from the in-memory lineage instead.
            applicable_versions = [
                item for item in versions if item[1] <= reference_date
            ]
            applicable = applicable_versions[-1] if applicable_versions else None
            if not applicable:
                continue
            version_id, version_date = applicable
            key = (version_id, row["dst_anchor"])
            edges_by_source.setdefault(str(row["src_id"]), {}).setdefault(
                key,
                TypedRelation(
                    relationship_type=RelationshipType.APPLICABLE_VERSION,
                    raw_citation_string=row["raw_citation_string"],
                    dst_id=version_id,
                    dst_anchor=row["dst_anchor"],
                    src_anchor=f"applicable on {reference_date}; consolidation {version_date}",
                    extracted_via=ExtractedVia.INFERRED,
                    resolution_status=ResolutionStatus.RESOLVED,
                    context_start=row["context_start"],
                    context_end=row["context_end"],
                ),
            )
        count = 0
        for src_id, edges in edges_by_source.items():
            self.add_relations(src_id, list(edges.values()), commit=False)
            count += len(edges)
        if commit:
            self.conn.commit()
        return count

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

    def relation_src_of_type(self, dst_id: str, relationship_type: str) -> str | None:
        """The src of ONE incoming edge of a given type — for the structural links
        (``assimilated_version_of``) where the question is "does anything claim this
        relationship to this document?", not "what cites it?".

        ``relations_to`` answers that too, but by materialising every incoming edge:
        asking it whether the GDPR has a UK assimilated version pulled 169,841 rows
        across the wire to find one, ~1s added to every legislation page load.
        """
        row = self.conn.execute(
            "SELECT src_id FROM relations WHERE dst_id = ? AND relationship_type = ? "
            "AND resolution_status = 'resolved' LIMIT 1",
            (dst_id, relationship_type),
        ).fetchone()
        return row["src_id"] if row else None

    def relations_to(
        self, dst_id: str, *, anchor_exact: str | None = None,
        anchor_prefixes: list[str] | None = None,
        anchor_like: list[str] | None = None,
    ) -> list[sqlite3.Row]:
        """Incoming resolved edges — what cites/treats this document (citing cases,
        commentary). The other half of 1-hop graph expansion (§6c).

        Optional anchor filters are coarse database-side guards for provision-level
        readers.  Callers still perform canonical family matching, but no longer have
        to materialise every incoming edge to a mega-authority merely to show one
        Article or Recital.

        ``anchor_prefixes`` are ALREADY normalised (see ``ANCHOR_NORM_SQL``): lower case,
        no spaces, no punctuation, one entry per spelling of the unit. That matters —
        the guard used to compare a reconstructed ``"s 13"`` against a stored ``"s. 13"``
        and quietly dropped every UK section citation before the Python matcher ever saw
        it, so a provision-scoped query over the UK corpus always returned nothing.
        """
        anchor_sql = ""
        params: list[str] = [dst_id]
        if anchor_exact:
            anchor_sql = (
                " AND lower(replace(COALESCE(dst_anchor, ''), ' ', '')) = ?"
            )
            params.append(anchor_exact.lower().replace(" ", ""))
        elif anchor_prefixes or anchor_like:
            # ``anchor_like`` carries WHOLE patterns rather than prefixes, because a
            # range pinpoint cannot be reached by one: "para 135-140" answers a request
            # for paragraph 138 and shares no prefix with it. The guard admits every
            # range and lets the caller's numeric span test decide — coarse, which is
            # what a guard is for.
            clauses = ([f"{ANCHOR_NORM_SQL} LIKE ?"] * len(anchor_prefixes or [])
                       + [f"{ANCHOR_NORM_SQL} LIKE ?"] * len(anchor_like or []))
            anchor_sql = f" AND ({' OR '.join(clauses)})"
            params.extend(prefix + "%" for prefix in (anchor_prefixes or []))
            params.extend(anchor_like or [])
        return self.conn.execute(
            "SELECT * FROM relations WHERE dst_id = ? AND resolution_status = 'resolved' "
            "AND relationship_type <> 'cited_by'"  # reverse-oriented scaffold
            f"{anchor_sql}",
            tuple(params),
        ).fetchall()

    def cited_by_family_count(self, ids: list[str]) -> int:
        """Distinct citing documents after collapsing consolidated citing lineages."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return 0
        qs = ",".join("?" * len(ids))
        row = self.conn.execute(
            f"""
            WITH citers AS (
              SELECT DISTINCT r.src_id
              FROM relations r
              WHERE r.dst_id IN ({qs}) AND r.resolution_status = 'resolved'
                AND r.extracted_via <> 'inferred' AND r.src_id <> r.dst_id
                AND r.relationship_type IN (
                  'mentions','interprets','applies','considers','follows',
                  'distinguishes','overrules','cites_for_fact'
                )
            ),
            lineage AS (
              SELECT src_id, COALESCE(dst_id, candidate_id) AS base_id
              FROM relations
              WHERE relationship_type = 'consolidates'
                AND COALESCE(dst_id, candidate_id) IS NOT NULL
            )
            SELECT COUNT(DISTINCT COALESCE(lineage.base_id, citers.src_id)) AS n
            FROM citers LEFT JOIN lineage ON lineage.src_id = citers.src_id
            """,
            ids,
        ).fetchone()
        return int(row["n"] or 0)

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

    def _anchor_prefix_sql(self, anchor_prefixes: list[str] | None) -> tuple[str, list]:
        """``(sql, params)`` for a normalised anchor-prefix guard, or empty."""
        if not anchor_prefixes:
            return "", []
        likes = " OR ".join([f"{ANCHOR_NORM_SQL} LIKE ?"] * len(anchor_prefixes))
        return f" AND ({likes})", [prefix + "%" for prefix in anchor_prefixes]

    def _type_filter(self, relationship_types: list[str] | None) -> tuple[str, list]:
        """A relationship-type restriction expressed IN SQL.

        It has to be: these queries are bounded and unordered, so filtering the rows in
        Python afterwards asks for "any hundred edges" and then keeps the matching ones.
        On a heavily-cited instrument the four edges you asked about are essentially
        never in that hundred — which is exactly why a supersedes query returned nothing
        from the target side while the same edge was visible from the source side.
        """
        wanted = [str(t) for t in (relationship_types or []) if t]
        if not wanted:
            return "", []
        return f"AND relationship_type IN ({','.join('?' * len(wanted))}) ", wanted

    def neighbours_out(self, doc_id: str, *, limit: int = 200,
                       include_inferred: bool = False,
                       relationship_types: list[str] | None = None) -> list[sqlite3.Row]:
        """Bounded outgoing resolved edges — unlike ``relations_for`` this never
        returns an unbounded set, so it's safe on any node."""
        extra = "" if include_inferred else "AND extracted_via <> 'inferred' "
        types_sql, types = self._type_filter(relationship_types)
        return self.conn.execute(
            "SELECT * FROM relations WHERE src_id = ? AND resolution_status = 'resolved' "
            f"AND dst_id IS NOT NULL AND dst_id <> src_id {extra}{types_sql}LIMIT ?",
            (doc_id, *types, limit)).fetchall()

    def neighbours_in(self, doc_id: str, *, limit: int = 200,
                      include_inferred: bool = False,
                      relationship_types: list[str] | None = None) -> list[sqlite3.Row]:
        """Bounded incoming resolved edges (a heavily-cited authority has 100k+)."""
        extra = "" if include_inferred else "AND extracted_via <> 'inferred' "
        types_sql, types = self._type_filter(relationship_types)
        return self.conn.execute(
            "SELECT * FROM relations WHERE dst_id = ? AND resolution_status = 'resolved' "
            "AND relationship_type <> 'cited_by' "  # reverse-oriented scaffold
            f"AND src_id <> dst_id {extra}{types_sql}LIMIT ?",
            (doc_id, *types, limit)).fetchall()

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

    def documents_citing_all(self, ids: list[str], *, limit: int = 10000) -> list[str]:
        """Held source documents with a real resolved citation to EVERY target.

        This is the exact co-citation intersection used for doctrinal conflict mapping;
        inferred carry-forward edges are excluded because a guessed provision host must
        not manufacture the claim that a court considered two authorities together.
        """
        ids = list(dict.fromkeys(i for i in ids if i))
        if len(ids) < 2:
            return []
        qs = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT r.src_id FROM relations r JOIN documents d ON d.stable_id = r.src_id "
            f"WHERE r.dst_id IN ({qs}) AND r.resolution_status = 'resolved' "
            f"AND r.relationship_type <> 'cited_by' AND r.extracted_via <> 'inferred' "
            f"GROUP BY r.src_id HAVING COUNT(DISTINCT r.dst_id) = ? LIMIT ?",
            (*ids, len(ids), limit),
        ).fetchall()
        return [r["src_id"] for r in rows]

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
            SELECT d.source, d.court, d.doc_type,
                   -- the pending-notice split the facade turns into its own kinds:
                   -- a live reference is not "other EU material" (see _doc_kind)
                   -- both preliminary-ruling codes: the ordinary one and the urgent
                   -- procedure (PPU), which is still an Article 267 reference
                   CASE WHEN d.doc_type = 'note' AND d.search_excluded = 0
                             AND (d.meta_json LIKE '%"pending_procedure": "PREJ%'
                                  OR d.meta_json LIKE '%"pending_procedure": "REFER_PREL%')
                        THEN 1 ELSE 0 END AS prej,
                   CASE WHEN d.doc_type = 'note' AND d.search_excluded = 0
                             AND d.meta_json LIKE '%"pending": true%'
                        THEN 1 ELSE 0 END AS pending,
                   COUNT(DISTINCT r.src_id) AS docs
            FROM relations r JOIN documents d ON d.stable_id = r.src_id
            WHERE r.dst_id IN ({qs}) AND r.resolution_status = 'resolved'
              AND r.extracted_via <> 'inferred' AND r.src_id <> r.dst_id
              AND r.relationship_type <> 'cited_by'
            GROUP BY d.source, d.court, d.doc_type, prej, pending
            """, ids).fetchall()

    def pending_eu_citers(self, ids: list[str], *, limit: int = 400) -> list[sqlite3.Row]:
        """Live CN/TN notices citing this document, with the pinpoints each cites.

        The raw material for a statute's "pending before the Court" box: one row per
        (notice, anchor), so the box can say not just THAT a reference is pending but
        which articles and recitals it turns on.  Retired notices are excluded by the
        same ``search_excluded`` flag that hides them from search — a resolved reference
        belongs in the case law, not in a list of open questions.
        """
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        return self.conn.execute(
            f"""
            SELECT d.stable_id, d.title, d.court, d.decision_date, d.meta_json,
                   d.topic_tags, r.dst_anchor, r.relationship_type,
                   COUNT(*) AS occurrences
            FROM relations r JOIN documents d ON d.stable_id = r.src_id
            WHERE r.dst_id IN ({qs}) AND r.resolution_status = 'resolved'
              AND r.extracted_via <> 'inferred' AND r.src_id <> r.dst_id
              AND r.relationship_type <> 'cited_by'
              AND d.doc_type = 'note' AND d.search_excluded = 0
              AND d.meta_json LIKE '%"pending": true%'
            GROUP BY d.stable_id, d.title, d.court, d.decision_date, d.meta_json,
                     d.topic_tags, r.dst_anchor, r.relationship_type
            ORDER BY d.decision_date DESC, d.stable_id
            LIMIT ?
            """, (*ids, limit * 12)).fetchall()

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
        # The Administrative Appeals Chamber appears in citations as both ``(AAC)``
        # and the common typo ``(ACC)``, while older BAILII imports zero-pad the neutral
        # number (``.../0310``) and FCL does not (``.../310``).  They are the same
        # identifier.  Keep this fallback deliberately confined to UKUT AAC so it cannot
        # guess across genuinely independent EWHC/EWCA division number sequences.
        m = re.fullmatch(r"ukut/(?:aac|acc)/(\d{4})/(\d+)", candidate, re.IGNORECASE)
        if m:
            rows = self.conn.execute(
                "SELECT stable_id FROM documents WHERE stable_id LIKE ? OR stable_id LIKE ?",
                (f"ukut/aac/{m.group(1)}/%", f"ukut/acc/{m.group(1)}/%"),
            ).fetchall()
            wanted = int(m.group(2))
            # The LIKE admits any tail under that year, and not every UKUT AAC id ends
            # in a bare number — ".../b1" and similar suffixed forms exist — so a plain
            # int() raised ValueError out of find_existing and killed the whole
            # document's guard pass ("failed in guards"), losing its re-extraction.
            # A non-numeric tail simply isn't the number we're matching.
            hits = [r["stable_id"] for r in rows
                    if _trailing_int(r["stable_id"]) == wanted]
            if len(hits) == 1:
                return hits[0]
        return None

    def document_identity_ids(self, stable_id: str) -> list[str]:
        """Every node key under which this same identified Work may receive edges.

        Some secondary-source records predate rendition reconciliation and coexist with
        an official ECLI-keyed record.  Until those nodes are physically unified, the
        citator must treat their shared ECLI as one identity; otherwise lookup counts
        inbound edges to the ECLI while ``citing_documents`` on the secondary stable id
        claims there are none.
        """
        doc = self.get_document(stable_id)
        if doc is None:
            return [stable_id] if stable_id else []
        ids = [stable_id]
        ecli = doc["ecli"]
        if ecli:
            ids.append(ecli)
            ids.extend(r["stable_id"] for r in self.conn.execute(
                "SELECT stable_id FROM documents WHERE ecli = ?", (ecli,)).fetchall())
        return list(dict.fromkeys(i for i in ids if i))

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
            # Rare legacy UKUT spelling/padding variants are cheaper to settle one by
            # one than to complicate the set-based hot path for every jurisdiction.
            for c in remaining:
                if c not in out and re.fullmatch(
                        r"ukut/(?:aac|acc)/\d{4}/\d+", c, re.IGNORECASE):
                    held = self.find_document_id(c)
                    if held:
                        out[c] = held
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
                  "de_case": 0, "de_law": 0, "ca_french_neutral": 0,
                  "ni_division": 0, "westlaw_report": 0}
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
            # A report citation printed in a Westlaw export is the case's own
            # structured identity.  It outranks a parallel-citation cluster inferred
            # from running text: the latter once assigned Lewis v Daily Telegraph's
            # [1964] AC 234 to Morgan v Odhams merely because Morgan cited Lewis.
            from ..core.text import fold_citation

            wl_rows = self.conn.execute(
                "SELECT stable_id, meta_json FROM documents "
                "WHERE meta_json IS NOT NULL AND meta_json LIKE '%report_citations%'"
            )
            for r in wl_rows:
                try:
                    reports = (json.loads(r["meta_json"] or "{}").get("westlaw") or {}).get(
                        "report_citations") or []
                except (ValueError, TypeError, AttributeError):
                    reports = []
                for report in reports:
                    alias = fold_citation(str(report))
                    if not alias:
                        continue
                    cur = self.conn.execute(
                        "INSERT INTO citation_aliases (alias,dst_id,source) VALUES (?,?,?) "
                        "ON CONFLICT(alias) DO UPDATE SET dst_id=excluded.dst_id, "
                        "source=excluded.source WHERE citation_aliases.source IS NULL "
                        "OR citation_aliases.source LIKE 'parallel:%'",
                        (alias, r["stable_id"], "westlaw-report-alias"),
                    )
                    minted["westlaw_report"] += max(cur.rowcount, 0)
            # Canadian decisions are one Work with English and French neutral-citation
            # codes.  Older imports keyed the Work on SCC/FCA/FC/TCC/CMAC but left the
            # corresponding CSC/CAF/CF/CCI/CACM candidates dangling.  Rebuild those
            # deterministic aliases directly from stable_id — no document re-extraction.
            from ..citations.courts import CANADIAN_FRENCH_COURT_EQUIVALENTS

            reverse = {english.lower(): french.lower()
                       for french, english in CANADIAN_FRENCH_COURT_EQUIVALENTS.items()}
            ca_rows = self.conn.execute(
                "SELECT stable_id FROM documents WHERE source = 'ca-caselaw'"
            )
            for r in ca_rows:
                parts = r["stable_id"].split("/")
                if len(parts) != 3 or parts[0].lower() not in reverse:
                    continue
                alias = "/".join((reverse[parts[0].lower()], parts[1], parts[2]))
                cur = self.conn.execute(
                    "INSERT INTO citation_aliases (alias, dst_id, source) VALUES (?,?,?) "
                    "ON CONFLICT(alias) DO NOTHING",
                    (alias, r["stable_id"], "ca-french-neutral"),
                )
                minted["ca_french_neutral"] += max(cur.rowcount, 0)
            # BAILII files every NI High Court division under one ``nihc/<division>``
            # slug head while the judgments are CITED by division ("[2016] NIFam 6").
            # Rebuild the citation-form key from stable_id alone, the same way the
            # Canadian French-neutral aliases above are rebuilt — otherwise a Northern
            # Irish Chancery, Family, Master or Coroner's judgment is reachable only by
            # its BAILII path, and every citation of it hangs.
            from ..citations.courts import ni_division_alias

            ni_rows = self.conn.execute(
                "SELECT stable_id FROM documents WHERE stable_id LIKE 'nihc/%'")
            for r in ni_rows:
                alias = ni_division_alias(r["stable_id"])
                if not alias:
                    continue
                cur = self.conn.execute(
                    "INSERT INTO citation_aliases (alias, dst_id, source) VALUES (?,?,?) "
                    "ON CONFLICT(alias) DO NOTHING",
                    (alias, r["stable_id"], "ni-division-alias"),
                )
                minted["ni_division"] += max(cur.rowcount, 0)
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

    def _dedot_sql(self, col: str) -> str:
        """A SQL expression that reduces a ``fold()``-ed citation (dots KEPT) to the
        ``fold_citation()`` key report aliases are often stored under (abbreviation dots
        STRIPPED — a '.' after a lowercase letter and not before a digit — whitespace
        collapsed). See core.text.fold_citation. This closes the resolver's dotted-vs-
        de-dotted gap: an edge's raw_fold is "[1996] 3 s.c.r. 458" while its alias is
        "[1996] 3 scr 458", so the literal ``a.alias = raw_fold`` join (pass 3) never
        matched and the case — though HELD — stayed pending forever and kept reappearing
        on the retrieval export. Mirrors get_alias()'s de-dotted retry.

        Backend-specific: SQLite has no regexp_replace. Its literal ``replace(col,'.','')``
        drops every dot (fine at dev/test scale — report cites rarely carry decimals);
        Postgres reproduces the abbreviation-only rule exactly (ARE has no lookbehind, so
        the preceding lowercase letter is captured and re-emitted)."""
        if self.backend == "postgres":
            return (r"btrim(regexp_replace("
                    r"regexp_replace(%s, '([a-z])\.(?![0-9])', '\1', 'g'), "
                    r"'\s+', ' ', 'g'))" % col)
        return f"replace({col}, '.', '')"

    def resolve_pending(self) -> int:
        """Flip every pending edge whose target is now a node. Returns the number
        resolved. Idempotent and safe to re-run after each ingest — that is how a
        citation to a freshly-harvested target becomes a live edge (§5b)."""
        total = 0
        # pass 4: the de-dotted report-citation match (fold vs fold_citation mismatch),
        # built per-backend so it can't live in the static _RESOLVE_PASSES tuple.
        dedot_pass = f"""
            UPDATE relations SET dst_id = d.stable_id, resolution_status = 'resolved'
            FROM citation_aliases a JOIN documents d
              ON (d.stable_id = a.dst_id OR d.ecli = a.dst_id)
            WHERE relations.resolution_status = 'pending'
              AND relations.raw_fold IS NOT NULL
              AND a.alias = {self._dedot_sql('relations.raw_fold')}
              AND a.alias <> relations.raw_fold
        """
        with self._atomic():
            for sql in (*self._RESOLVE_PASSES, dedot_pass):
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
            # pass 4: de-dotted report-citation match (see resolve_pending / _dedot_sql).
            f"""
            UPDATE relations SET dst_id = d.stable_id, resolution_status = 'resolved'
            FROM citation_aliases a JOIN documents d
              ON (d.stable_id = a.dst_id OR d.ecli = a.dst_id)
            WHERE relations.relation_id >= ? AND relations.relation_id <= ?
              AND relations.resolution_status = 'pending'
              AND relations.raw_fold IS NOT NULL
              AND a.alias = {self._dedot_sql('relations.raw_fold')}
              AND a.alias <> relations.raw_fold
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

    def held_extraction_state(self, ids: list[str]) -> dict[str, tuple[bool, bool]]:
        """``{stable_id: (has_text, has_extraction_stamp)}`` for held ``ids``.

        ``has_text`` is part of the dedup decision, not merely dashboard metadata.  A
        metadata-only document is a placeholder, and a later full-text adapter must be
        allowed to fetch and supersede it.  Treating every primary-key hit as complete
        stranded exactly those cross-source enrichments (for example GOV.UK text for a
        BAILII UKET stub).

        The second flag keeps the existing crash-recovery behaviour: a full-text document
        stored just before extraction is deduped but carried into the extraction backlog.
        One batched query still replaces hundreds of point lookups on resume walks."""
        out: dict[str, tuple[bool, bool]] = {}
        ids = [i for i in dict.fromkeys(ids) if i]
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            qs = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                    f"SELECT stable_id, has_text, last_extracted_at FROM documents "
                    f"WHERE stable_id IN ({qs})", chunk).fetchall():
                out[r["stable_id"]] = (bool(r["has_text"]), bool(r["last_extracted_at"]))
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

    def rebuild_pending_reference_stats(self) -> int:
        """Refresh the hanging-reference worklist roll-up — the ~96s live aggregate, run
        ONCE on a cadence instead of on every worklist/drain read. Includes the ``echr``
        join (the roll-up pays it once so readers never do)."""
        with self._maintenance_timeout(), self._atomic():
            self.conn.execute("DELETE FROM pending_reference_stats")
            agg = ("string_agg(DISTINCT r.extracted_via, ',')" if self.backend == "postgres"
                   else "group_concat(DISTINCT r.extracted_via)")
            self.conn.execute(
                f"""
                INSERT INTO pending_reference_stats
                    (ref, candidate, raw, anchor, methods, occurrences, citing_count, echr_citing, rebuilt_at)
                SELECT COALESCE(r.candidate_id, r.raw_citation_string) AS ref,
                       MAX(r.candidate_id), MIN(r.raw_citation_string), MIN(r.dst_anchor),
                       {agg}, COUNT(*), COUNT(DISTINCT r.src_id),
                       -- ECHR-cited flag via a membership test against the (small) set of
                       -- echr documents, NOT a JOIN over all ~5M documents: the join made
                       -- this build ~6 min, the IN-subquery ~20s.
                       MAX(CASE WHEN r.src_id IN
                           (SELECT stable_id FROM documents WHERE source = 'echr')
                           THEN 1 ELSE 0 END), ?
                FROM relations r
                WHERE r.resolution_status = 'pending'
                  AND r.extracted_via <> 'inferred'
                  AND COALESCE(r.candidate_id, r.raw_citation_string) IS NOT NULL
                GROUP BY COALESCE(r.candidate_id, r.raw_citation_string)
                """, (_now(),))
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM pending_reference_stats").fetchone()["n"]

    def pending_reference_groups_rollup(self, *, min_citing: int = 1,
                                        limit: int | None = None,
                                        offset: int = 0) -> list[sqlite3.Row]:
        """The worklist from the roll-up (milliseconds), same row shape as
        :meth:`pending_reference_groups`. Empty until first rebuilt — the caller falls
        back to the live aggregate then (fresh DB / test). ``offset`` pages deeper into
        the citation-count-ranked list (the export scans page-by-page so a
        jurisdiction-filtered run isn't capped by the global top window)."""
        params: list = []
        where = ""
        if min_citing > 1:
            where = "WHERE citing_count >= ?"
            params.append(int(min_citing))
        tail = ""
        if limit is not None:
            # a stable secondary sort key (ref) so paging with OFFSET doesn't skip/repeat
            # rows that tie on citing_count across pages
            tail = "LIMIT ?"
            params.append(int(limit))
            if offset:
                tail += " OFFSET ?"
                params.append(int(offset))
        return self.conn.execute(
            f"SELECT ref, candidate, raw, anchor, methods, occurrences, citing_count, "
            f"echr_citing FROM pending_reference_stats {where} "
            f"ORDER BY citing_count DESC, ref {tail}", params).fetchall()

    def pending_reference_stats_age(self) -> str | None:
        """When the worklist roll-up was last rebuilt (ISO), for the 'last refreshed' line."""
        row = self.conn.execute(
            "SELECT MAX(rebuilt_at) AS t FROM pending_reference_stats").fetchone()
        return row["t"] if row else None

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

    def text_document_ids_citing(
        self, target_ids: list[str], *, sources: list[str] | None = None,
        source_prefix: str | None = None, exclude_extraction_run_id: str | None = None,
    ) -> list[str]:
        """Text documents already observed citing one of ``target_ids``.

        This is the bounded grammar-upgrade worklist: a French EU-article grammar
        improvement should revisit the 8k French documents known to discuss the digital
        acquis, not all 2.9m DILA records.  Citation observations are occurrence-level and
        survive resolution, so they are a more precise selector than a full-text LIKE.
        ``last_extraction_run_id`` makes the selection restartable under the normal job
        checkpoint contract.
        """
        ids = list(dict.fromkeys(str(i) for i in target_ids if i))
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        sql = (
            "SELECT DISTINCT d.stable_id FROM documents d "
            "JOIN citations c ON c.src_id = d.stable_id "
            f"WHERE d.has_text = 1 AND c.candidate_id IN ({qs})"
        )
        params: list = list(ids)
        if sources:
            clean_sources = list(dict.fromkeys(str(s) for s in sources if s))
            if not clean_sources:
                return []
            sql += f" AND d.source IN ({','.join('?' * len(clean_sources))})"
            params.extend(clean_sources)
        if source_prefix:
            sql += " AND d.source LIKE ?"
            params.append(f"{source_prefix}%")
        if exclude_extraction_run_id:
            sql += " AND (d.last_extraction_run_id IS NULL OR d.last_extraction_run_id <> ?)"
            params.append(exclude_extraction_run_id)
        sql += " ORDER BY d.stable_id"
        return [r["stable_id"] for r in self.conn.execute(sql, params).fetchall()]

    def held_text_document_ids(self, document_ids: list[str]) -> list[str]:
        """The requested ids that are held with extractable text, preserving input order."""
        requested = list(dict.fromkeys(str(i) for i in document_ids if i))
        if not requested:
            return []
        held: set[str] = set()
        for start in range(0, len(requested), 800):
            chunk = requested[start:start + 800]
            qs = ",".join("?" * len(chunk))
            held.update(
                r["stable_id"] for r in self.conn.execute(
                    f"SELECT stable_id FROM documents WHERE has_text = 1 "
                    f"AND stable_id IN ({qs})", chunk
                ).fetchall()
            )
        return [stable_id for stable_id in requested if stable_id in held]

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

    # -- feedback (Bugs / Feature requests from the app's feedback box) --------
    def add_feedback(self, *, kind: str, message: str, page: str | None = None,
                     url: str | None = None, metadata: str | None = None) -> int:
        """Record one item and return ITS id — on both backends.

        ``lastrowid`` is SQLite-only, so on Postgres this returned 0 and the caller could
        not address the row it had just written: an agent filing a report and then
        resolving it (or referring to it in a reply) had nothing to name. RETURNING costs
        nothing and closes that."""
        sql = ("INSERT INTO feedback (kind, message, page, url, metadata, status, "
               "created_at) VALUES (?,?,?,?,?,'open',?)")
        params = (kind, message, page, url, metadata, _now())
        if self.backend == "postgres":
            row = self.conn.execute(sql + " RETURNING feedback_id", params).fetchone()
            self.conn.commit()
            return int(row["feedback_id"]) if row else 0
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.lastrowid) if getattr(cur, "lastrowid", None) is not None else 0

    def record_issue(self, *, fingerprint: str, message: str, page: str | None = None,
                     metadata: str | None = None, kind: str = "error") -> int:
        """Record a SYSTEM-reported problem in the same queue as user feedback, counting
        repeats instead of re-inserting them.

        A systemic failure does not happen once: one harvest run produced 13,862 identical
        "no targeted adapter" failures, and an extraction protocol mismatch killed a worker
        on every document for a day. Inserting a row per occurrence would bury the review
        queue in copies of one problem, so an issue is keyed on its ``fingerprint`` (the
        shape of the error, with ids and numbers masked out): the first occurrence opens a
        row, the rest bump ``seen_count`` and ``last_seen_at``. Resolving the row lets the
        NEXT occurrence open a fresh one — which is what makes "did that fix hold?"
        answerable."""
        now = _now()
        row = self.conn.execute(
            "SELECT feedback_id FROM feedback WHERE fingerprint = ? AND status = 'open' "
            "ORDER BY created_at LIMIT 1", (fingerprint,)).fetchone()
        if row is not None:
            self.conn.execute(
                "UPDATE feedback SET seen_count = seen_count + 1, last_seen_at = ? "
                "WHERE feedback_id = ?", (now, row["feedback_id"]))
            self.conn.commit()
            return int(row["feedback_id"])
        cur = self.conn.execute(
            "INSERT INTO feedback (kind, message, page, url, metadata, status, created_at, "
            "fingerprint, seen_count, last_seen_at) VALUES (?,?,?,NULL,?,'open',?,?,1,?)",
            (kind, message, page, metadata, now, fingerprint, now))
        self.conn.commit()
        return int(cur.lastrowid) if getattr(cur, "lastrowid", None) is not None else 0

    def feedback(self, *, status: str | None = "open", limit: int = 500,
                 kind: str | None = None) -> list[sqlite3.Row]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        sql = "SELECT * FROM feedback"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # most-recently-SEEN first, so a repeating system error stays at the top of the
        # queue rather than sinking under newer one-off reports
        sql += " ORDER BY COALESCE(last_seen_at, created_at) DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def set_feedback_status(self, feedback_id: int, status: str) -> int:
        cur = self.conn.execute(
            "UPDATE feedback SET status = ? WHERE feedback_id = ?", (status, feedback_id))
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

    def put_alias(self, alias: str, dst_id: str, source: str | None = None, *,
                  commit: bool = True, overwrite: bool = True) -> None:
        """Point a citation key at a document. ``overwrite=False`` keeps an existing
        mapping — first writer wins.

        That matters where two registers publish the same judgment: NeuRIS re-pointed
        2,960 German docket keys away from the ECLI-keyed copies of the same decisions,
        so a citation resolved to a rendition with no ECLI and none of the edges the
        original had."""
        # Store on the de-dotted key so "K.B." and "KB" citations converge on one row
        # rather than each minting its own (and only one of them resolving).
        from ..core.text import fold_citation

        alias = fold_citation(alias) or alias
        conflict = ("DO UPDATE SET dst_id = excluded.dst_id, source = excluded.source"
                    if overwrite else "DO NOTHING")
        self.conn.execute(
            f"""
            INSERT INTO citation_aliases (alias, dst_id, source) VALUES (?,?,?)
            ON CONFLICT(alias) {conflict}
            """,
            (alias, dst_id, source),
        )
        if commit:
            self.conn.commit()

    # -- learned shorthands (corpus-wide, but gated at application time) -------
    def add_learned_shorthands(self, rows: list[dict], *, doc_id: str | None = None,
                               commit: bool = True) -> dict:
        """Record shorthand definitions a document established. Each row: shorthand,
        candidate_id, entity_kind, is_abbrev.

        The definition itself is insert-only (``ON CONFLICT DO NOTHING``): re-extracting
        a document must not rewrite rows it already wrote, because this runs inside the
        whole-corpus rescan where ~700k documents share one table. What re-extraction
        *may* do is add a DOCUMENT to the pair's evidence — recorded as its own row, so
        the count is a set size and rescanning the same document a second time changes
        nothing.

        Returns ``{"written": n, "settled": [(shorthand, candidate_id), …]}``; a settled
        pair has reached ``SHORTHAND_MIN_DOCS`` and needs no further writes ever, which
        is what keeps the hot pairs ("GDPR", "HMRC") off the write path entirely."""
        from ..citations.extractor import SHORTHAND_MIN_DOCS

        rows = [r for r in rows if r.get("shorthand") and r.get("candidate_id")]
        if not rows:
            return {"written": 0, "settled": []}
        now = _now()
        written = 0
        settled: list[tuple[str, str]] = []
        for r in rows:
            key = (r["shorthand"], r["candidate_id"])
            cur = self.conn.execute(
                """
                INSERT INTO learned_shorthands
                    (shorthand, candidate_id, entity_kind, is_abbrev, first_doc,
                     doc_count, created_at)
                VALUES (?,?,?,?,?,0,?)
                ON CONFLICT(shorthand, candidate_id) DO NOTHING
                """,
                (r["shorthand"], r["candidate_id"], r.get("entity_kind"),
                 1 if r.get("is_abbrev") else 0, doc_id, now),
            )
            written += max(cur.rowcount, 0)
            if not doc_id:
                continue
            fresh = self.conn.execute(
                "INSERT INTO learned_shorthand_docs (shorthand, candidate_id, doc_id) "
                "VALUES (?,?,?) ON CONFLICT DO NOTHING", (*key, doc_id))
            if max(fresh.rowcount, 0) == 0:
                continue
            # Derived, never incremented — see the DDL. Two extra statements per pair
            # per document, but only while the pair is still below the threshold.
            seen = self.conn.execute(
                "SELECT COUNT(*) AS n FROM learned_shorthand_docs "
                "WHERE shorthand = ? AND candidate_id = ?", key).fetchone()["n"]
            self.conn.execute(
                "UPDATE learned_shorthands SET doc_count = ? "
                "WHERE shorthand = ? AND candidate_id = ?", (seen, *key))
            if seen >= SHORTHAND_MIN_DOCS:
                settled.append(key)
        if commit:
            self.conn.commit()
        return {"written": written, "settled": settled}

    def learned_shorthand_map(self, *, limit: int = 400000) -> dict[str, list[tuple]]:
        """``{candidate_id: [(shorthand, entity_kind, is_abbrev), …]}`` — the APPLICABLE
        store, loaded once and cached by the stage. Keyed by candidate because
        application is gated on the citing document already citing that candidate, so the
        caller only ever looks up ids it has in hand.

        Only pairs at or over ``SHORTHAND_MIN_DOCS`` are returned: below that a shorthand
        is document-local and never travels. That is also what makes the load cheap —
        the gate takes it from ~1.1M rows to ~15k."""
        from ..citations.extractor import SHORTHAND_MIN_DOCS

        out: dict[str, list[tuple]] = {}
        for r in self.conn.execute(
                "SELECT shorthand, candidate_id, entity_kind, is_abbrev "
                "FROM learned_shorthands WHERE COALESCE(blocked, 0) = 0 "
                "AND COALESCE(doc_count, 0) >= ? LIMIT ?",
                (SHORTHAND_MIN_DOCS, limit)):
            out.setdefault(r["candidate_id"], []).append(
                (r["shorthand"], r["entity_kind"], bool(r["is_abbrev"])))
        return out

    # -- free-text index (§6c) -------------------------------------------------
    # A tsvector is capped at 1 MB. This corpus reaches it: uk-cma averages 1.13M
    # characters a document and uk-lawcom-reports 468k, so a single-row-per-document
    # design would silently fail on exactly the documents most worth searching.
    FTS_PART_CHARS = 120_000
    FTS_PART_WORDS = 10_000

    def put_doc_fts(
        self, doc_id: str, text: str, *, headings: list[tuple[str, int]] | None = None,
        commit: bool = True,
    ) -> int:
        """(Re)index one document. Returns the number of parts written.

        Splitting is on a paragraph boundary where one is available, so a phrase is
        only ever broken across parts if a single paragraph exceeds the cap — and the
        stored ``char_start`` keeps every hit mappable back onto the source text."""
        # PostgreSQL text/tsvector rejects U+0000.  PDF and office converters do emit
        # it occasionally (the Irish Court of Appeal record captured in feedback is a
        # real example). Replace one code point with one code point so stored offsets
        # remain aligned with the source text while the rest of the document indexes.
        text = text.replace("\x00", "\ufffd")
        parts = _fts_parts(text, self.FTS_PART_CHARS, word_cap=self.FTS_PART_WORDS)
        now = _now()
        self.conn.execute("DELETE FROM doc_fts WHERE doc_id = ?", (doc_id,))
        self.conn.execute("DELETE FROM doc_headings WHERE doc_id = ?", (doc_id,))
        for i, (start, end) in enumerate(parts):
            body = text[start:end]
            if self.backend == "postgres":
                self.conn.execute(
                    "INSERT INTO doc_fts (doc_id, part, char_start, char_end, words,"
                    " tsv, indexed_at) VALUES (?,?,?,?,?,to_tsvector('english', ?),?)",
                    (doc_id, i, start, end, len(body.split()), body, now))
            else:
                self.conn.execute(
                    "INSERT INTO doc_fts (doc_id, part, char_start, char_end, words,"
                    " tsv, indexed_at) VALUES (?,?,?,?,?,?,?)",
                    (doc_id, i, start, end, len(body.split()), "", now))
        for heading_no, (label, char_start) in enumerate(headings or []):
            label = str(label or "").strip()
            if not label:
                continue
            if self.backend == "postgres":
                self.conn.execute(
                    "INSERT INTO doc_headings (doc_id,heading_no,label,char_start,tsv) "
                    "VALUES (?,?,?,?,to_tsvector('english', ?))",
                    (doc_id, heading_no, label, int(char_start or 0), label))
            else:
                self.conn.execute(
                    "INSERT INTO doc_headings (doc_id,heading_no,label,char_start,tsv) "
                    "VALUES (?,?,?,?,?)",
                    (doc_id, heading_no, label, int(char_start or 0), ""))
        if commit:
            self.conn.commit()
        return len(parts)

    def drop_doc_fts(self, doc_id: str, *, commit: bool = True) -> None:
        self.conn.execute("DELETE FROM doc_fts WHERE doc_id = ?", (doc_id,))
        self.conn.execute("DELETE FROM doc_headings WHERE doc_id = ?", (doc_id,))
        if commit:
            self.conn.commit()

    def fts_indexed_ids(self, source: str | None = None) -> set[str]:
        # Requiring the companion heading row makes this an automatic one-time upgrade:
        # indexes built before structural labels existed are not considered complete and
        # the next resumable build fills them without a special migration command.
        sql = ("SELECT DISTINCT f.doc_id FROM doc_fts f "
               "JOIN doc_headings h ON h.doc_id = f.doc_id "
               "JOIN documents d ON d.stable_id = f.doc_id")
        params: list[object] = []
        if source:
            sql += " WHERE d.source = ?"
            params.append(source)
        return {r["doc_id"] for r in self.conn.execute(sql, params)}

    def fts_positional_risk_count(self) -> int:
        """Documents still carrying a legacy part beyond the safe position budget."""
        return int(self.conn.execute(
            "SELECT count(DISTINCT doc_id) AS n FROM doc_fts WHERE words > ?",
            (self.FTS_PART_WORDS,),
        ).fetchone()["n"])

    def fts_coverage(self) -> list[dict]:
        """Per-source: how many documents have text, and how many are indexed. This is
        what the settings screen shows so the gate's scope is legible."""
        rows = self.conn.execute(
            "SELECT d.source AS source, d.court AS court, d.doc_type AS doc_type,"
            "       count(*) AS with_text,"
            "       count(f.doc_id) AS indexed "
            "FROM documents d "
            "LEFT JOIN (SELECT DISTINCT doc_id FROM doc_fts) f ON f.doc_id = d.stable_id "
            "WHERE d.has_text = 1 AND d.search_excluded = 0 "
            "GROUP BY d.source, d.court, d.doc_type").fetchall()
        return [dict(r) for r in rows]

    def backfill_effective_dates(self, *, dry_run: bool = True, batch: int = 20000,
                                 on_progress=None) -> dict:
        """Fill ``effective_date`` for rows written before the column existed.

        Every sort, filter and facet reads it, so until this runs the corpus behaves as
        though 68,158 common-law judgments have no date at all — they sink to the bottom
        of a newest-first browse and vanish from any year range. Keyset-paged over the
        primary key; re-runnable, and it never overwrites a row that already agrees."""
        seen = filled = 0
        by_provenance: dict[str, int] = {}
        after = ""
        while True:
            page = self.conn.execute(
                "SELECT stable_id, ecli, decision_date, effective_date "
                "FROM documents WHERE stable_id > ? ORDER BY stable_id LIMIT ?",
                (after, batch)).fetchall()
            if not page:
                break
            after = page[-1]["stable_id"]
            writes = []
            for r in page:
                seen += 1
                date, why = effective_date(r["decision_date"], r["ecli"], r["stable_id"])
                by_provenance[why] = by_provenance.get(why, 0) + 1
                if date != r["effective_date"]:
                    writes.append((date, why, r["stable_id"]))
            filled += len(writes)
            if writes and not dry_run:
                self.conn.executemany(
                    "UPDATE documents SET effective_date = ?, date_provenance = ? "
                    "WHERE stable_id = ?", writes)
                self.conn.commit()
            if on_progress:
                on_progress(seen, filled)
        return {"scanned": seen, "updated": 0 if dry_run else filled,
                "would_update": filled if dry_run else 0,
                "by_provenance": by_provenance, "dry_run": dry_run}

    def documents_meta(self, ids: list[str]) -> list[dict]:
        """Facet-bearing metadata for a set of documents, in one round trip.

        The facet counts have to describe the WHOLE result set, not the page being
        shown — a reader told "912 documents" and then given a breakdown of 40 of
        them has been misled — so the search path fetches every match's metadata and
        counts client-side, which also lets a facet click narrow instantly without
        re-running the query."""
        if not ids:
            return []
        out: list[dict] = []
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            rows = self.conn.execute(
                "SELECT d.stable_id, d.source, d.court, d.doc_type, d.decision_date,"
                "       d.effective_date, d.date_provenance,"
                "       d.title, COALESCE(a.pagerank, 0) AS pagerank,"
                # cited_by on the resolved graph, falling back to the string roll-up
                # for documents outside the authority table
                "       COALESCE(a.in_degree, (SELECT MAX(cc.occurrences)"
                "           FROM citation_counts cc"
                "           WHERE cc.candidate_id IN (d.stable_id, d.ecli)), 0) AS cited_by "
                "FROM documents d LEFT JOIN doc_authority a ON a.doc_id = d.stable_id "
                f"WHERE d.stable_id IN ({','.join('?' * len(chunk))})",
                chunk).fetchall()
            out.extend(dict(r) for r in rows)
        return out

    def documents_citing(self, ids: list[str], dst_id: str) -> set[str]:
        """Which of ``ids`` cite ``dst_id``. Used by the "cites …" facet for an
        authority outside the pre-computed top list."""
        if not ids or not dst_id:
            return set()
        found: set[str] = set()
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            rows = self.conn.execute(
                "SELECT DISTINCT src_id FROM relations "
                f"WHERE dst_id = ? AND src_id IN ({','.join('?' * len(chunk))})",
                [dst_id] + chunk).fetchall()
            found.update(r["src_id"] for r in rows)
        return found

    def cited_by_documents(self, ids: list[str], *, limit: int = 25,
                           with_sources: bool = False) -> list[dict]:
        """What a SET of documents most often cites.

        The doctrinal anchors of a result set: search "duty of care", and this says
        the 300 matching judgments between them cite Donoghue v Stevenson 89 times
        and Caparo 54 — which is how the area is actually navigated, and something no
        per-document view can show."""
        if not ids:
            return []
        counts: dict[str, int] = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            rows = self.conn.execute(
                "SELECT r.dst_id AS dst, count(DISTINCT r.src_id) AS n "
                "FROM relations r "
                f"WHERE r.src_id IN ({','.join('?' * len(chunk))}) "
                "  AND r.dst_id IS NOT NULL AND r.resolution_status = 'resolved' "
                "GROUP BY r.dst_id", chunk).fetchall()
            for r in rows:
                counts[r["dst"]] = counts.get(r["dst"], 0) + r["n"]
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
        if not top:
            return []
        meta = {m["stable_id"]: m for m in self.documents_meta([t[0] for t in top])}
        out = []
        for dst, n in top:
            m = meta.get(dst) or {}
            row = {"stable_id": dst, "citing": n, "title": m.get("title"),
                   "court": m.get("court"), "doc_type": m.get("doc_type"),
                   "source": m.get("source"),
                   "decision_date": m.get("decision_date")}
            if with_sources:
                # the citing ids, so a "cites …" facet click narrows the result set
                # in the browser instead of asking the server again
                row["src_ids"] = sorted(self.documents_citing(ids, dst))
            out.append(row)
        return out

    def fts_indexed_by_source(self) -> list[dict]:
        """How many documents each source has IN the index.

        Deliberately not ``fts_coverage``: that reports what could be indexed as well
        as what is, which means a GROUP BY over all 4.97M documents and takes two
        seconds. This walks the index itself — a twentieth the size — because the
        front page only needs to say what is searchable."""
        rows = self.conn.execute(
            "SELECT d.source AS source, d.court AS court, count(DISTINCT f.doc_id) AS n "
            "FROM doc_fts f JOIN documents d ON d.stable_id = f.doc_id "
            "GROUP BY d.source, d.court").fetchall()
        return [dict(r) for r in rows]

    def embedding_coverage(self) -> list[dict]:
        """Per-source: documents with text, and how many carry a vector. The mirror of
        ``fts_coverage`` so one screen can show what each retrieval path actually
        covers — the two are independent, and until now nothing said so."""
        rows = self.conn.execute(
            "SELECT d.source AS source, count(*) AS with_text,"
            "       count(e.doc_id) AS embedded "
            "FROM documents d "
            "LEFT JOIN (SELECT DISTINCT doc_id FROM embeddings) e "
            "       ON e.doc_id = d.stable_id "
            "WHERE d.has_text = 1 AND d.search_excluded = 0 "
            "GROUP BY d.source").fetchall()
        return [dict(r) for r in rows]

    def fts_search(self, tsquery: str, *, filters: dict | None = None,
                   limit: int = 200) -> list[dict]:
        """Candidate documents for a compiled tsquery, best-ranked first.

        This is *narrowing*, not the answer: for an exact search the caller then
        verifies the literal string against each candidate's text. Postgres stems, so
        a document reading "duties of care" is a candidate for ``"duty of care"`` and
        the literal check is what decides. ``limit`` is therefore a candidate budget,
        not a result count."""
        if self.backend != "postgres":
            return []
        sql = ("SELECT f.doc_id, f.part, f.char_start, f.heading,"
               "       ts_rank_cd(f.tsv, query) AS rank "
               "FROM (SELECT doc_id,part,char_start,tsv,NULL::text AS heading FROM doc_fts "
               "      UNION ALL "
               "      SELECT doc_id,-1,char_start,tsv,label AS heading FROM doc_headings) f "
               "JOIN documents d ON d.stable_id = f.doc_id,"
               "     to_tsquery('english', ?) AS query "
               "WHERE f.tsv @@ query")
        params: list[object] = [tsquery]
        sql, params = _apply_filters(sql, params, filters)
        sql += " ORDER BY rank DESC LIMIT ?"
        params.append(limit)
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except Exception as exc:
            # A malformed tsquery is user input, not a bug — but "no hits" is the wrong
            # way to say so. Zero results is an ANSWER ABOUT THE CORPUS, and a reader
            # given one for a query the database refused concludes the authority does
            # not exist. Raise; the search layer turns it into a stated error.
            raise FtsQueryError(str(exc)) from exc
        return [dict(r) for r in rows]

    def fts_total(self, tsquery: str, *, filters: dict | None = None) -> int:
        """How many documents match, ignoring the candidate budget. Legal readers ask
        "how many judgments use this phrase" and expect a real number, so the count is
        computed rather than inferred from a truncated page."""
        if self.backend != "postgres":
            return 0
        sql = ("SELECT count(DISTINCT f.doc_id) AS n "
               "FROM (SELECT doc_id,tsv FROM doc_fts UNION ALL "
               "      SELECT doc_id,tsv FROM doc_headings) f "
               "JOIN documents d ON d.stable_id = f.doc_id,"
               "     to_tsquery('english', ?) AS query "
               "WHERE f.tsv @@ query")
        params: list[object] = [tsquery]
        sql, params = _apply_filters(sql, params, filters)
        try:
            return self.conn.execute(sql, params).fetchone()["n"]
        except Exception as exc:
            raise FtsQueryError(str(exc)) from exc

    def count_learned_shorthands(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM learned_shorthands").fetchone()["n"]

    def backfill_learned_shorthand_doc_counts(
        self, *, dry_run: bool = True, batch: int = 5000,
        on_progress=None,
    ) -> dict:
        """Populate ``doc_count`` for rows written before the store counted anything.

        The million rows already in the store record only ``first_doc``, so on the day
        the popularity gate ships every one of them reads as document-local and NOTHING
        travels — including the abbreviations that should ("the CPIA"). The evidence is
        in the ``citations`` table, which has always recorded one row per USE with its
        source document; grouping those by (shorthand, target) recovers exactly the
        distinct-document count the store failed to keep.

        Not a plain ``GROUP BY raw``: ``raw`` holds the whole matched span, pincite and
        all ("Suncor, at para 30"), so grouping on it would split one shorthand across
        dozens of keys and undercount every popular name. ``shorthand_name_from_use``
        undoes what the use-pattern added; the fold is case-insensitive because that is
        how a case short-name matches.

        Counting stops at the threshold per pair — this is a gate, not a statistic —
        which also bounds the memory this holds while streaming."""
        from ..citations.extractor import SHORTHAND_MIN_DOCS, shorthand_name_from_use

        keys: dict[tuple[str, str], tuple[str, str]] = {}
        for r in self.conn.execute(
                "SELECT shorthand, candidate_id FROM learned_shorthands"):
            keys[(r["shorthand"].casefold(), r["candidate_id"])] = (
                r["shorthand"], r["candidate_id"])
        docs: dict[tuple[str, str], set[str]] = {}
        scanned = 0
        # Keyset-paged over the primary key, never one streaming SELECT: the Postgres
        # shim buffers a result set client-side, and `citations` is tens of millions of
        # rows. Paging keeps the resident set to one page and lets the PK index serve
        # `citation_id > ?` incrementally even though `method` is unindexed.
        after = 0
        while True:
            page = self.conn.execute(
                "SELECT citation_id, src_id, raw, candidate_id FROM citations "
                "WHERE citation_id > ? AND method = 'shorthand' "
                "AND candidate_id IS NOT NULL "
                "ORDER BY citation_id LIMIT ?", (after, batch)).fetchall()
            if not page:
                break
            after = page[-1]["citation_id"]
            scanned += len(page)
            for row in page:
                name = shorthand_name_from_use(row["raw"])
                pair = keys.get((name.casefold(), row["candidate_id"]))
                if not pair:
                    continue
                seen = docs.setdefault(pair, set())
                if len(seen) < SHORTHAND_MIN_DOCS:
                    seen.add(row["src_id"])
            if on_progress:
                on_progress(scanned, len(docs))
        counted = [(len(v), k[0], k[1]) for k, v in docs.items()]
        popular = sum(1 for n, _s, _c in counted if n >= SHORTHAND_MIN_DOCS)
        if not dry_run:
            for i in range(0, len(counted), batch):
                self.conn.executemany(
                    "UPDATE learned_shorthands SET doc_count = ? "
                    "WHERE shorthand = ? AND candidate_id = ? "
                    "AND COALESCE(doc_count, 0) < ?",
                    [(n, s, c, n) for n, s, c in counted[i:i + batch]])
                self.conn.commit()
        return {"citations_scanned": scanned, "pairs_counted": len(counted),
                "pairs_at_threshold": popular, "threshold": SHORTHAND_MIN_DOCS,
                "updated": 0 if dry_run else len(counted), "dry_run": dry_run}

    def documents_by_shorthand(self, query: str, *, limit: int = 20) -> list[str]:
        """The authorities a corpus-wide shorthand stands for — "CPIA" → the Criminal
        Procedure and Investigations Act 1996.

        Search matches titles, and no statute's title is its abbreviation, so the names
        practitioners actually use were the one way of naming an authority that search
        could not follow. The store already knows them; the popularity gate is what makes
        it safe to search on, since only names several documents independently agreed on
        get this far. Exact (folded) match only — this is a lookup, not a substring
        search."""
        from ..citations.extractor import SHORTHAND_MIN_DOCS

        q = " ".join(str(query or "").split())
        if len(q) < 2:
            return []
        rows = self.conn.execute(
            "SELECT candidate_id, doc_count FROM learned_shorthands "
            "WHERE lower(shorthand) = ? AND COALESCE(blocked, 0) = 0 "
            "AND COALESCE(doc_count, 0) >= ? ORDER BY doc_count DESC LIMIT ?",
            (q.casefold(), SHORTHAND_MIN_DOCS, limit)).fetchall()
        return [r["candidate_id"] for r in rows if r["candidate_id"]]

    def browse_learned_shorthands(
        self, *, query: str | None = None, candidate_id: str | None = None,
        state: str = "all", limit: int = 100, offset: int = 0,
    ) -> tuple[list[sqlite3.Row], int]:
        """A page of the store for the admin panel, with the total matching the filter.

        ``state`` is ``all`` | ``active`` | ``blocked`` | ``invalid`` | ``local`` —
        ``invalid`` being rows that would no longer be learned today (see
        ``valid_shorthand``), which is how the accumulated junk is found without knowing
        what to search for, and ``local`` those below ``SHORTHAND_MIN_DOCS``: stored, but
        established by too few documents to travel."""
        from ..citations.extractor import SHORTHAND_MIN_DOCS

        where, params = ["1=1"], []
        if query:
            where.append("(lower(shorthand) LIKE ? OR lower(candidate_id) LIKE ?)")
            like = f"%{query.lower()}%"
            params += [like, like]
        if candidate_id:
            where.append("candidate_id = ?")
            params.append(candidate_id)
        if state == "active":
            where.append("COALESCE(blocked, 0) = 0")
            where.append("COALESCE(doc_count, 0) >= ?")
            params.append(SHORTHAND_MIN_DOCS)
        elif state == "blocked":
            where.append("COALESCE(blocked, 0) = 1")
        elif state == "local":
            where.append("COALESCE(doc_count, 0) < ?")
            params.append(SHORTHAND_MIN_DOCS)
        sql_where = " AND ".join(where)
        if state == "invalid":
            # No SQL predicate can express it, so filter in Python over the matching
            # rows. Bounded by the same query/candidate filters the caller supplied.
            from ..citations.extractor import valid_shorthand

            rows = self.conn.execute(
                f"SELECT * FROM learned_shorthands WHERE {sql_where} "
                "ORDER BY shorthand LIMIT 20000", params).fetchall()
            bad = [r for r in rows if not valid_shorthand(r["shorthand"])]
            return bad[offset:offset + limit], len(bad)
        total = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM learned_shorthands WHERE {sql_where}",
            params).fetchone()["n"]
        page = self.conn.execute(
            f"SELECT * FROM learned_shorthands WHERE {sql_where} "
            "ORDER BY shorthand, candidate_id LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        return page, total

    def set_learned_shorthand(self, shorthand: str, candidate_id: str,
                              **fields) -> int:
        """Edit one stored shorthand: ``blocked``, ``is_abbrev``, ``entity_kind``."""
        allowed = {"blocked", "is_abbrev", "entity_kind"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return 0
        cols = ", ".join(f"{k} = ?" for k in sets)
        cur = self.conn.execute(
            f"UPDATE learned_shorthands SET {cols} "
            "WHERE shorthand = ? AND candidate_id = ?",
            list(sets.values()) + [shorthand, candidate_id])
        self.conn.commit()
        return max(cur.rowcount, 0)

    def delete_learned_shorthand(self, shorthand: str, candidate_id: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM learned_shorthands WHERE shorthand = ? AND candidate_id = ?",
            (shorthand, candidate_id))
        self.conn.commit()
        return max(cur.rowcount, 0)

    def purge_invalid_learned_shorthands(self, *, dry_run: bool = True,
                                         batch: int = 5000,
                                         include_local: bool = False) -> dict:
        """Delete every stored shorthand that would not be learned today.

        The read path already skips these, so this is housekeeping rather than a fix —
        but the store had grown to 1,114,991 rows, most of them unusable, and a smaller
        store is a faster one (it is loaded whole and cached per rescan).

        ``include_local`` widens it to rows below ``SHORTHAND_MIN_DOCS`` — ~91% of the
        store, and the only way to shift the report boilerplate the read-side rules
        cannot see ("Medium Neutral", "Library Sheet": no colon, and they look like
        names). It is OFF by default and reported separately in the dry run, because it
        deletes the evidence as well as the row: a pair below the threshold is still
        ACCUMULATING documents, and deleting it resets that to whatever
        ``learned_shorthand_docs`` still holds."""
        from ..citations.extractor import SHORTHAND_MIN_DOCS, valid_shorthand

        seen = deleted = 0
        doomed: list[tuple[str, str]] = []
        local = 0
        for r in self.conn.execute(
                "SELECT shorthand, candidate_id, COALESCE(doc_count, 0) AS doc_count "
                "FROM learned_shorthands"):
            seen += 1
            unlearnable = not valid_shorthand(r["shorthand"])
            below = r["doc_count"] < SHORTHAND_MIN_DOCS
            local += bool(below and not unlearnable)
            if unlearnable or (below and include_local):
                doomed.append((r["shorthand"], r["candidate_id"]))
        if not dry_run:
            for i in range(0, len(doomed), batch):
                chunk = doomed[i:i + batch]
                self.conn.executemany(
                    "DELETE FROM learned_shorthands "
                    "WHERE shorthand = ? AND candidate_id = ?", chunk)
                self.conn.executemany(
                    "DELETE FROM learned_shorthand_docs "
                    "WHERE shorthand = ? AND candidate_id = ?", chunk)
                self.conn.commit()
            deleted = len(doomed)
        return {"scanned": seen, "invalid": len(doomed), "deleted": deleted,
                "document_local": local, "threshold": SHORTHAND_MIN_DOCS,
                "include_local": include_local, "dry_run": dry_run}

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
        """Most-cited unresolved citations first — what to harvest next (§8). Served from
        the ``pending_reference_stats`` roll-up (ms); the live GROUP BY over the pending
        ``relations`` slice is ~96s, so it's the fallback only for a not-yet-rolled-up DB."""
        rows = self.conn.execute(
            "SELECT raw AS raw_citation_string, citing_count AS cite_count "
            "FROM pending_reference_stats WHERE raw IS NOT NULL "
            "ORDER BY citing_count DESC LIMIT ?", (limit,)).fetchall()
        if rows:
            return rows
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

    # -- per-run harvest history (§keep-current) ----------------------------
    _SOURCE_RUNS_KEEP = 40  # newest runs retained per source

    def record_source_run(
        self, source_key: str, *, started_at: str, finished_at: str,
        discovered: int, stored: int, deduped: int, refreshed: int,
        errors: int, not_found: int, rate_limited: bool, backfill: bool,
        watermark: str | None, trigger: str = "manual", watch_id: int | None = None,
    ) -> None:
        """Log one harvest run's outcome for the Maintain diagnosis view, then trim to the
        newest ``_SOURCE_RUNS_KEEP`` per source so the log stays bounded."""
        self.conn.execute(
            """INSERT INTO source_runs
               (source_key, watch_id, trigger, backfill, started_at, finished_at,
                discovered, stored, deduped, refreshed, errors, not_found, rate_limited, watermark)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source_key, watch_id, trigger, 1 if backfill else 0, started_at, finished_at,
             discovered, stored, deduped, refreshed, errors, not_found,
             1 if rate_limited else 0, watermark),
        )
        self.conn.execute(
            """DELETE FROM source_runs WHERE source_key = ? AND run_id NOT IN
               (SELECT run_id FROM source_runs WHERE source_key = ?
                ORDER BY run_id DESC LIMIT ?)""",
            (source_key, source_key, self._SOURCE_RUNS_KEEP),
        )
        self.conn.commit()

    def recent_source_runs(self, source_key: str, *, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM source_runs WHERE source_key = ? ORDER BY run_id DESC LIMIT ?",
            (source_key, limit),
        ).fetchall()

    def source_run_summaries(self, *, per_source: int = 10) -> dict[str, list[dict]]:
        """The newest ``per_source`` runs for EVERY source, keyed by source — one query
        for the whole Maintain overview rather than N round-trips."""
        rows = self.conn.execute(
            """SELECT * FROM (
                 SELECT *, ROW_NUMBER() OVER (PARTITION BY source_key ORDER BY run_id DESC) AS rn
                 FROM source_runs
               ) t WHERE rn <= ? ORDER BY source_key, run_id DESC""",
            (per_source,),
        ).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["source_key"], []).append(dict(r))
        return out

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
    def pending_embedding(self, provider: str, model: str, model_version: str,
                          *, sources: list[str] | None = None,
                          limit: int | None = None) -> list[sqlite3.Row]:
        """Documents with text but no vectors in this embedding family (§6). A
        model swap is a new family, so it naturally re-queues the whole corpus.

        ``sources`` scopes the queue to those source keys (the resolution of the
        RAGLEX_EMBED_JURISDICTIONS setting) so an operator can index only the jurisdictions
        that matter instead of the whole multi-million-doc corpus. ``None`` = no scope;
        an empty list = scope to nothing (embed no documents).

        ``limit`` caps the returned queue IN SQL. This is load-bearing, not cosmetic: the
        embedding queue over a 5M-document corpus is millions of rows, and an unbounded
        ``fetchall`` here (with the caller slicing afterwards) grew the worker to ~9GB and
        the kernel OOM-killed it in a restart loop. A bounded batch keeps memory flat and
        the pass resumable — the next run picks up where this one left off."""
        src_clause, params = "", [provider, model, model_version]
        if sources is not None:
            if not sources:
                src_clause = " AND 1 = 0"  # explicit empty scope → nothing to embed
            else:
                src_clause = f" AND d.source IN ({','.join('?' * len(sources))})"
                params.extend(sources)
        limit_clause = ""
        if limit is not None and limit > 0:
            limit_clause = " LIMIT ?"
            params.append(int(limit))
        return self.conn.execute(
            f"""
            SELECT * FROM documents d
            WHERE d.has_text = 1 AND d.text_path IS NOT NULL
              AND d.search_excluded = 0
              AND NOT EXISTS (
                SELECT 1 FROM embeddings e
                WHERE e.doc_id = d.stable_id AND e.provider = ?
                  AND e.model = ? AND e.model_version = ?
              ){src_clause}{limit_clause}
            """,
            tuple(params),
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
    def has_vector_index(self, dimensions: int | None = None) -> bool:
        """Whether a usable pgvector ANN index exists for the family dimension — the gate
        for semantic search (§7). Without it, the ``<=>`` scan would read the WHOLE
        embeddings table (14M+ rows → tens of seconds), so hybrid search must fall back to
        the lexical (FTS) half alone. SQLite scores in Python over the (small dev) family,
        so it always reports available there."""
        if self.backend != "postgres":
            return True
        name = f"embeddings_hnsw_{int(dimensions)}" if dimensions else "embeddings_hnsw_"
        try:
            return self.conn.execute(
                "SELECT 1 FROM pg_class WHERE relname = ? AND relkind = 'i'", (name,)
            ).fetchone() is not None
        except Exception:  # noqa: BLE001
            return False

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

    def corpus_counts_fast(self) -> dict:
        """Same breakdowns as :meth:`corpus_counts`, but summed from the hourly
        ``corpus_shape_stats`` roll-up instead of four full scans over the (now ~5M-row)
        documents table — those scans blew the statement timeout after the fr-dila import
        and left the §8 stats endpoint permanently cold. Falls back to the live counts when
        the roll-up hasn't been built yet (fresh install / tests). A pre-``upstream_status``
        roll-up (old rows, column still NULL) is detected and the upstream breakdown filled
        from a single low-cardinality live scan until the next roll-up rebuild."""
        rows = self.corpus_shape_stats()
        if not rows:
            return self.corpus_counts()
        total = 0
        by_doc_type: dict[str, int] = {}
        by_source: dict[str, int] = {}
        by_upstream: dict[str, int] = {}
        have_upstream = False
        for r in rows:
            n = r["n"] or 0
            total += n
            dt = r["doc_type"] or "?"
            by_doc_type[dt] = by_doc_type.get(dt, 0) + n
            src = r["source"] or "?"
            by_source[src] = by_source.get(src, 0) + n
            us = r["upstream_status"]
            if us:
                have_upstream = True
                by_upstream[us] = by_upstream.get(us, 0) + n
        if not have_upstream:   # roll-up predates the column → one small live GROUP BY
            try:
                by_upstream = self._count_by("upstream_status")
            except Exception:   # noqa: BLE001 — never let a heavy scan fail the whole page
                by_upstream = {}
        _sort = lambda d: dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True))
        return {"total": total, "by_doc_type": _sort(by_doc_type),
                "by_source": _sort(by_source), "by_upstream_status": _sort(by_upstream)}

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
                            id_prefix=None, id_in=None, id_or=None, year_from=None, year_to=None,
                            cites=None, cited_by=None, cites_pinpoint=None):
        """Shared WHERE-clause builder for list/count/search/facets (so every surface filters
        with identical semantics). ``court`` matches the stored court token; ``id_prefix``
        matches one or more slug heads (comma-separated). ``query`` is tokenised — each
        whitespace-separated word must appear (as a substring) in the title or id, so
        *non-consecutive* words match ("erasure data" finds "…data … erasure"). ``year_from``/
        ``year_to`` bound the decision-date year. ``cites`` keeps documents that cite the given
        target (by id/ECLI/candidate); ``cited_by`` keeps documents cited BY the given source."""
        clauses: list[str] = []
        params: list[object] = []
        clauses.append("d.search_excluded = 0")
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
        if id_in:
            # Exact id set — the citation-query path resolves "[2011] IESC 26" to a document
            # id via the grammar/aliases (see Facade._citation_query_ids) and matches it here
            # by PK, instead of substring-scanning. Kept small (a handful of resolved ids).
            ids = list(dict.fromkeys(i for i in id_in if i))
            if ids:
                qs = ",".join("?" for _ in ids)
                clauses.append(f"(d.stable_id IN ({qs}) OR d.ecli IN ({qs}))")
                params.extend(ids)
                params.extend(ids)
        if query:
            # Case-insensitive (Postgres LIKE is case-sensitive; SQLite's is not) AND tokenised
            # — every word must hit the title, id, or ECLI, in any order/position ("erasure
            # data" finds "…data … erasure"). All three branches are pg_trgm-GIN-indexed
            # (§7), so the OR is a BitmapOr, not a seq scan. Citation-FORMAT queries ("[2011]
            # IESC 26") don't come through here — the facade resolves them to `id_in` above,
            # because folding a report/neutral cite into this substring OR would either miss
            # (the id slug omits the brackets) or, if unioned in, defeat the bitmap.
            tok_clauses: list[str] = []
            tok_params: list[object] = []
            for tok in str(query).split():
                tok_clauses.append(
                    "(lower(d.title) LIKE ? OR lower(d.stable_id) LIKE ? OR lower(d.ecli) LIKE ?)")
                like = f"%{tok.lower()}%"
                tok_params.extend([like, like, like])
            # A case is often known by a name that is NOT its title — "Dun & Bradstreet
            # Austria" for CK v Magistrat der Stadt Wien, the name the corpus stores under
            # "also cited as". Those live in citation_aliases, which is 5M rows and must not
            # be dragged into this bitmap, so the caller resolves the query against it
            # SEPARATELY (one trigram-indexed lookup) and hands the hits down as ``id_or``:
            # a document matches if its title/id matches OR it is one of those documents.
            ids_or = list(dict.fromkeys(i for i in (id_or or []) if i))
            if tok_clauses and ids_or:
                qs = ",".join("?" for _ in ids_or)
                clauses.append("((" + " AND ".join(tok_clauses) + ")"
                               f" OR d.stable_id IN ({qs}) OR d.ecli IN ({qs}))")
                params.extend(tok_params)
                params.extend(ids_or)
                params.extend(ids_or)
            else:
                clauses.extend(tok_clauses)
                params.extend(tok_params)
        # Year filters read the EFFECTIVE date: a 1975 Court of Appeal judgment whose
        # metadata carries no decision_date is still a 1975 judgment, and excluding it
        # from "cases before 1980" is the kind of silent omission a reader cannot see.
        if year_from:
            clauses.append("substr(d.effective_date, 1, 4) >= ?"); params.append(str(year_from))
        if year_to:
            clauses.append("substr(d.effective_date, 1, 4) <= ?"); params.append(str(year_to))
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
        # effective_date, not decision_date: the 68,158 undated common-law judgments
        # otherwise sank to the bottom of every date sort as though undated, which for a
        # newest-first browse means they simply never appear. Indexed to match
        # (documents_effective_date_idx), so the browse still serves LIMIT from the index.
        "date": "d.effective_date DESC NULLS LAST, d.stable_id",
        "date_asc": "d.effective_date ASC NULLS LAST, d.stable_id",
        "title": "lower(d.title), d.stable_id",
        "cited": "cited_by DESC, d.effective_date DESC",
        # network authority (PageRank roll-up); raw = landmark, decayed = currently live
        "authority": "authority DESC, cited_by DESC, d.effective_date DESC",
        "authority_recent": "authority_decayed DESC, cited_by DESC, d.effective_date DESC",
    }

    def _sort_clause(self, sort: str | None) -> str:
        key = self._SORT_SQL.get(sort or "date", self._SORT_SQL["date"])
        if self.backend == "sqlite":  # SQLite has no NULLS LAST
            key = key.replace(" DESC NULLS LAST", " DESC").replace(" ASC NULLS LAST", " ASC")
        return key

    # How WELL the title matches, ranked before anything else. A title query is tokenised
    # — every word must appear somewhere in the title or id, in any order — which is a
    # good FILTER and no ranking at all: ordered by date alone, "Consolidated Industry
    # Codes of Practice for the Online Industry" outranked "Code of Practice for
    # General-Purpose AI Models" for the query "code of practice", because it happened to
    # be newer. Exact title, then a title starting with the query, then one containing it
    # as a whole phrase, then the scattered-token match.
    #
    # Inside a band the corpus's own judgement breaks the tie: how many documents cite
    # this one, and only then how recent it is. Two instruments whose titles match a query
    # equally well are not equally what you meant — the one the corpus leans on is.
    _RELEVANCE_BANDS = ("CASE WHEN lower(d.title) = ? THEN 0 "
                        "WHEN lower(d.title) LIKE ? THEN 1 "
                        "WHEN lower(d.title) LIKE ? THEN 2 ELSE 3 END, "
                        "cited_by DESC, ")

    def _order_by(self, sort: str | None, query: str | None = None,
                  id_boost: list | None = None) -> tuple[str, list]:
        """The ORDER BY clause and the parameters it binds (LIKE patterns are bound, never
        interpolated — a literal % in the SQL is the pg placeholder trap).

        Like the "cited"/"authority" clauses, the relevance order reads ``cited_by``, so it
        is only valid for :meth:`search_documents`, which selects it.
        """
        q = (query or "").strip().lower()
        # An exact-name hit (an abbreviation resolved through the shorthand store) leads
        # whatever the sort — including a date browse, where it would otherwise be buried
        # by whatever is newest.
        boost = list(dict.fromkeys(i for i in (id_boost or []) if i))
        lead, lparams = "", []
        if boost:
            qs = ",".join("?" for _ in boost)
            lead = f"CASE WHEN d.stable_id IN ({qs}) THEN 0 ELSE 1 END, "
            lparams = list(boost)
        if (sort or "") != "relevance":
            return lead + self._sort_clause(sort), lparams
        if not q:  # nothing to be relevant TO — browsing, not searching
            return lead + self._sort_clause("date"), lparams
        return (lead + self._RELEVANCE_BANDS + self._sort_clause("date"),
                [*lparams, q, f"{q}%", f"%{q}%"])

    #: A held point-in-time expression is keyed ``<base>@<YYYY-MM-DD>`` (or, for CELLAR
    #: consolidations, ``…-YYYYMMDD``). Both forms are recognised so one rule covers the
    #: EU, Dutch and UK version series alike.
    _VERSION_SUFFIX = re.compile(r"(?:@(\d{4}-\d{2}-\d{2})|-(\d{4})(\d{2})(\d{2}))$")

    @classmethod
    def version_base_and_date(cls, stable_id: str) -> tuple[str, str | None]:
        """``("BWBR0006622", "2013-08-31")`` for a dated expression; ``(id, None)`` for a
        base act. Pure, so the collapse below is testable without a database."""
        sid = str(stable_id or "")
        m = cls._VERSION_SUFFIX.search(sid)
        if not m:
            return sid, None
        if m.group(1):
            return sid[: m.start()], m.group(1)
        return sid[: m.start()], f"{m.group(2)}-{m.group(3)}-{m.group(4)}"

    @classmethod
    def collapse_version_rows(cls, rows, *, on_date: str | None = None) -> list:
        """One row per instrument: its latest READABLE expression in force today.

        A search for "Wegenverkeerswet 1994" returned eight rows with identical titles —
        eight of that law's 182 held snapshots — and never the law itself. The corpus
        holds 68,797 dated expressions, so on any versioned instrument the snapshots
        crowd out everything else and the thing you were looking for is not on the page.

        This is the same rule the reader applies when it opens an act
        (:meth:`applicable_consolidation`), stated over a result set instead of one base
        id: prefer the newest version that HAS TEXT and is not future-dated; a textless
        snapshot is skipped rather than returned, because a version can be held as a
        metadata record with no text at all. An instrument with no usable dated version
        falls back to its base row, and a base act with no versions is simply itself —
        nothing is hidden that has anywhere else to be seen.

        Order is preserved: the surviving row takes the position of the best-ranked row
        of its family, so relevance ordering upstream still decides the page.
        """
        cutoff = str(on_date or date.today().isoformat())[:10]
        best: dict[str, object] = {}
        rank: dict[str, int] = {}
        for position, row in enumerate(rows):
            base, version = cls.version_base_and_date(str(row["stable_id"] or ""))
            rank.setdefault(base, position)
            incumbent = best.get(base)
            if incumbent is None or cls._better_version(row, incumbent, cutoff):
                best[base] = row
        return [best[b] for b in sorted(best, key=lambda b: rank[b])]

    @staticmethod
    def _declared_as_at(row) -> str | None:
        """The date a row says its text is current to (``currency.as_at``).

        Only meaningful for a base row: a dated expression carries the same date in its
        id, and :meth:`version_base_and_date` already has it from there."""
        try:
            meta = row["meta_json"]
        except (IndexError, KeyError, TypeError):
            return None
        if not meta:
            return None
        try:
            currency = (json.loads(meta) or {}).get("currency") or {}
        except (TypeError, ValueError):
            return None
        as_at = currency.get("as_at")
        return str(as_at)[:10] if as_at else None

    @classmethod
    def _better_version(cls, row, incumbent, cutoff: str) -> bool:
        """Is ``row`` the one to show, against the family's current pick?

        The version families are not all shaped alike, and treating them alike published
        the wrong text. For an EU act the base is the ORIGINAL and each dated expression
        is a later amended state, so newest wins. For UK legislation the base row IS the
        revised text legislation.gov.uk serves today — RIPA 2000 is current to
        2026-04-07 — and a dated sibling is a point-in-time snapshot fetched on purpose
        so an old judgment can be read against the law as it then stood. Ranking a base
        row as if it had no date at all made every such snapshot beat it: adding RIPA to
        a static edition published the text as at 1 June 2010, sixteen years stale, and
        the search box showed the snapshot in place of the Act.

        So a base row is dated by what it CLAIMS — ``currency.as_at`` — and only falls
        back to "undated, therefore oldest" when it claims nothing, which is exactly the
        EU sector-3 case the newest-wins rule was written for.
        """
        def key(r):
            _base, version = cls.version_base_and_date(str(r["stable_id"] or ""))
            has_text = bool(r["has_text"]) if "has_text" in r.keys() else True
            if version is None:
                version = cls._declared_as_at(r)
            # A future-dated snapshot is held deliberately but is not the law today, so
            # it ranks below every applicable one — and below the base act.
            applicable = version is None or version <= cutoff
            return (has_text, applicable, version or "")
        return key(row) > key(incumbent)

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
        collapse_versions: bool = False,
    ) -> list[sqlite3.Row]:
        """Browse/filter documents — lets an agent iterate, e.g., a law's sections
        to augment each with secondary material.

        ``collapse_versions`` folds each instrument's held point-in-time expressions down
        to the one a reader wants (:meth:`collapse_version_rows`).
        """
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
        # Collapsing discards rows AFTER the database has ranked them, so ask for enough
        # that a heavily-versioned instrument cannot fill the page on its own: the Dutch
        # road-traffic act alone holds 182 snapshots. Bounded, because this is a search
        # box — a family that still overflows the fetch simply contributes its best row.
        want = limit if not collapse_versions else min(max(limit * 25, 200), 1000)
        order, oparams = self._doc_order_by(query)
        sql += f" {order} LIMIT ? OFFSET ?"
        params.extend([*oparams, want, offset])
        rows = self.conn.execute(sql, params).fetchall()
        if collapse_versions:
            rows = self.collapse_version_rows(rows)[:limit]
        return rows

    def _doc_order_by(self, query: str | None) -> tuple[str, list]:
        """The ORDER BY for a document listing, and its bound parameters.

        With NO query this is the pure date order the Corpus browser relies on, served
        straight off the ``(effective_date DESC, stable_id)`` index — do not add anything
        to this path, it is the one that has to stay index-only at 5M rows.

        With a query it becomes a RANKING, because date order alone made the search
        unusable for its main job: "Digital Economy Act 2017" put ten commencement orders
        above the Act itself, so at the eight rows an autocomplete shows, the Act — an
        exact title match — did not appear at all. Three tiers, cheapest first:

        0. the title IS the query;
        1. the title starts with it (so "Data Protection Act" reaches the Act before the
           regulations made under it);
        2. everything else the filter matched.

        Ties break on HOW MUCH THE CORPUS CITES IT, read from the ``citation_counts``
        roll-up — the cached aggregate, never a live count over the citations table. It
        is a correlated lookup on an indexed column, evaluated only for rows the filter
        already kept, and MAX() rather than SUM() because the roll-up is grouped by
        (candidate_id, entity_kind) and the same instrument can appear under several.
        """
        if not query:
            return "ORDER BY d.effective_date DESC, d.stable_id", []
        folded = " ".join(str(query).split()).lower()
        rank = ("CASE WHEN lower(d.title) = ? THEN 0 "
                "     WHEN lower(d.title) LIKE ? THEN 1 ELSE 2 END")
        cites = ("(SELECT COALESCE(MAX(cc.documents), 0) FROM citation_counts cc "
                 " WHERE cc.candidate_id = d.stable_id)")
        return (f"ORDER BY {rank}, {cites} DESC, d.effective_date DESC, d.stable_id",
                # the LIKE pattern is a bound parameter — a literal % in the SQL breaks
                # the Postgres driver's paramstyle translation
                [folded, f"{folded}%"])

    def documents_by_alias_text(self, query: str, *, limit: int = 200) -> list[str]:
        """Document ids whose "also cited as" forms contain every word of ``query``.

        A case is regularly known by a name that is not its title — "Dun & Bradstreet
        Austria" is how everyone refers to CK v Magistrat der Stadt Wien, and the corpus
        already records it, as an alias. Title search alone therefore could not find the
        case by the only name most people know it by.

        Deliberately a SEPARATE lookup rather than another branch of the title search's OR:
        citation_aliases is millions of rows, and folding it into that bitmap is exactly the
        shape that once starved the connection pool. This is one trigram-indexed scan
        (``citation_aliases_alias_trgm``), bounded, whose ids the caller ORs in.
        """
        toks = [t.lower() for t in str(query or "").split() if len(t) > 2]
        if not toks:
            return []
        where = " AND ".join("lower(alias) LIKE ?" for _ in toks)
        rows = self.conn.execute(
            f"SELECT DISTINCT dst_id FROM citation_aliases WHERE {where} LIMIT ?",
            [*(f"%{t}%" for t in toks), limit]).fetchall()
        return [r["dst_id"] for r in rows if r["dst_id"]]

    def search_documents(self, *, sort: str | None = None, limit: int = 50, offset: int = 0,
                         id_boost: list | None = None, **filters) -> list[sqlite3.Row]:
        """Like :meth:`list_documents` but sortable (incl. by citation frequency) and each row
        carries a ``cited_by`` count (occurrences from the roll-up) for display + ranking.

        ``id_boost`` are documents the query named EXACTLY by some route the title match
        cannot see — today, an abbreviation resolved through the learned-shorthand store
        ("CPIA"). They sort first: a document the query names outranks one that merely
        contains the letters, however well-cited the latter is."""
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
        order, oparams = self._order_by(sort, filters.get("query"), id_boost=id_boost)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        params.extend(oparams)      # positional: after the WHERE's, before LIMIT/OFFSET
        params.extend([limit, offset])
        return self.conn.execute(sql, params).fetchall()

    def count_documents(self, *, source: str | None = None, doc_type: str | None = None,
                        tag: str | None = None, query: str | None = None,
                        court: str | None = None, id_prefix: str | None = None,
                        id_in: list | None = None, id_or: list | None = None,
                        year_from: str | None = None, year_to: str | None = None,
                        cites: str | None = None, cited_by: str | None = None,
                        cites_pinpoint: str | None = None, cap: int | None = None) -> int:
        """Total documents matching the same filters as :meth:`list_documents` — for
        the Corpus page's true count + pagination.

        ``cap`` bounds the count: a keyword search on a common term ("data") matches a huge
        slice of the 5M-row corpus, and counting ALL of them — which no LIMIT can shortcut
        for an exact COUNT — is what made a common-word search take seconds even though the
        page only shows 8 rows. With ``cap`` the scan stops after cap+1 matching rows, so the
        caller can show "N+" for anything past the cap; the exact count is kept for the
        unbounded Corpus browse."""
        # COUNT(*) + EXISTS, not JOIN + COUNT(DISTINCT): same no-fan-out reasoning as
        # list_documents, and a distinct-aggregation over millions of ids is what made
        # the Corpus page's total/pagination time out after the bulk imports.
        params: list[object] = []
        clauses, fparams = self._doc_filter_clauses(
            source=source, doc_type=doc_type, tag=None, query=query, court=court, id_prefix=id_prefix,
            id_in=id_in, id_or=id_or, year_from=year_from, year_to=year_to, cites=cites,
            cited_by=cited_by, cites_pinpoint=cites_pinpoint)
        if tag:
            clauses.insert(0, "EXISTS (SELECT 1 FROM document_tags t "
                              "WHERE t.doc_id = d.stable_id AND t.tag = ?)")
            params.append(tag)
        params.extend(fparams)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        if cap and cap > 0:
            # count only up to cap+1 rows: bounds the work to a small slice regardless of
            # how many the term matches overall.
            sql = f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM documents d{where} LIMIT {int(cap) + 1}) x"
        else:
            sql = f"SELECT COUNT(*) AS n FROM documents d{where}"
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
               "year": "substr(d.effective_date, 1, 4)"}
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
                   attempt: int = 1, checkpoint: dict | None = None,
                   status: str = "running") -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO jobs (job_id, kind, label, params_json, status, origin, "
            "started_at, heartbeat_at, lease_heartbeat_at, root_job_id, resumed_from, resume_policy, attempt, checkpoint_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, kind, label, json.dumps(params or {}), status, origin, now, now, now,
             root_job_id or job_id, resumed_from, resume_policy, attempt,
             json.dumps(checkpoint or {})),
        )
        self.conn.commit()

    def queued_jobs(self) -> list[sqlite3.Row]:
        """Jobs waiting for a concurrency slot, oldest first (FIFO promotion)."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY started_at").fetchall()

    def claim_queued_job(self, job_id: str) -> bool:
        """Atomically move a queued job to running — the guard against two processes (the
        API and the scheduler both promote) starting the same queued job. Returns True to
        exactly one caller."""
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, heartbeat_at = ?, "
            "lease_heartbeat_at = ? WHERE job_id = ? AND status = 'queued'",
            (_now(), _now(), _now(), job_id))
        self.conn.commit()
        return cur.rowcount > 0

    def cancel_queued_job(self, job_id: str) -> bool:
        """Drop a job that is still waiting in the queue (never started)."""
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = ? "
            "WHERE job_id = ? AND status = 'queued'", (_now(), job_id))
        self.conn.commit()
        return cur.rowcount > 0

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

    def reap_stalled_jobs(self, cutoff_iso: str) -> list[sqlite3.Row]:
        """Mark 'running' jobs whose owning process has stopped pulsing (lease heartbeat older
        than ``cutoff_iso``, or never leased) as interrupted, and return the affected rows.

        Cross-process safe: the lease heartbeat is refreshed every ~30s by a live worker's
        pulse thread, so a lease this stale means the process behind the job is gone/frozen —
        regardless of which container owns it. This is what stops a slept-then-woke host from
        leaving a ghost 'running' forever without waiting for that container to restart."""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = 'running' "
            "AND (lease_heartbeat_at IS NULL OR lease_heartbeat_at < ?)",
            (cutoff_iso,),
        ).fetchall()
        if rows:
            self.conn.execute(
                "UPDATE jobs SET status = 'interrupted', finished_at = ? "
                "WHERE status = 'running' AND (lease_heartbeat_at IS NULL OR lease_heartbeat_at < ?)",
                (_now(), cutoff_iso),
            )
            self.conn.commit()
        return rows

    def reap_wedged_jobs(self, cutoff_iso: str) -> list[sqlite3.Row]:
        """Mark jobs whose WORK has stopped, in a process that is still alive.

        :meth:`reap_stalled_jobs` catches a dead process by its lease going cold. It
        cannot catch this: the lease is refreshed by a pulse thread that keeps pulsing
        perfectly while the worker thread is stuck in a call that never returns. A
        committee harvest sat on one document for fifteen minutes that way — twice — and
        every signal said "worker alive", because by the lease's measure it was.

        The distinguishing state is a FRESH lease with a STALE progress heartbeat:
        somebody is home and nothing is being done. That job is interrupted so it stops
        holding a concurrency slot and can be resumed past whatever it choked on. The
        cutoff is deliberately generous — a single legitimately slow item (a scanned PDF
        going through OCR) must not be mistaken for a wedge.
        """
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = 'running' "
            "AND heartbeat_at IS NOT NULL AND heartbeat_at < ? "
            "AND lease_heartbeat_at IS NOT NULL AND lease_heartbeat_at >= ?",
            (cutoff_iso, cutoff_iso),
        ).fetchall()
        if rows:
            self.conn.execute(
                "UPDATE jobs SET status = 'interrupted', finished_at = ? "
                "WHERE job_id IN (%s)" % ",".join("?" * len(rows)),
                (_now(), *[r["job_id"] for r in rows]),
            )
            self.conn.commit()
        return rows

    def recent_jobs(self, kind: str, *, limit: int = 20) -> list[sqlite3.Row]:
        """The last N jobs of one kind whatever their status — the durable answer to
        "has this already run today?", which an in-process variable cannot give across a
        restart."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE kind = ? ORDER BY started_at DESC LIMIT ?",
            (kind, limit),
        ).fetchall()

    def recent_job_results(self, kind: str, *, limit: int = 20) -> list[sqlite3.Row]:
        """The last N outcomes for one job kind — the substrate for "the nightly harvest
        has stored nothing for three days", the alert that would have caught the poisoned
        skip-list."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE kind = ? AND status = 'done' "
            "ORDER BY started_at DESC LIMIT ?",
            (kind, limit),
        ).fetchall()

    def last_run_per_kind(self) -> list[sqlite3.Row]:
        """The most recent terminal outcome for EVERY job kind, in one query.

        The maintenance surface needs "when did this last run, and did it find anything?"
        for ~30 actions at once. Asking per kind was 30 round trips to render one panel;
        DISTINCT ON walks the same index once."""
        if self.backend == "postgres":
            sql = ("SELECT DISTINCT ON (kind) kind, job_id, status, started_at, finished_at, "
                   "result_json FROM jobs WHERE status IN "
                   "('done','error','cancelled','interrupted') "
                   "ORDER BY kind, started_at DESC")
        else:
            sql = ("SELECT kind, job_id, status, started_at, finished_at, result_json "
                   "FROM jobs j WHERE status IN ('done','error','cancelled','interrupted') "
                   "AND started_at = (SELECT MAX(started_at) FROM jobs x "
                   "WHERE x.kind = j.kind AND x.status IN "
                   "('done','error','cancelled','interrupted')) GROUP BY kind")
        return self.conn.execute(sql).fetchall()

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
