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
    fetched_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_source_idx ON documents (source);
CREATE INDEX IF NOT EXISTS documents_ecli_idx ON documents (ecli);
CREATE INDEX IF NOT EXISTS documents_payload_hash_idx ON documents (payload_hash);

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
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, started_at);

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
)


# DSNs whose schema this process has already ensured. Postgres DDL is idempotent but not
# free, and the catalogue is opened per request.
_PG_SCHEMA_READY: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        (the DDL is CREATE-IF-NOT-EXISTS, which doesn't add columns to a live table)."""
        for table, col, decl in (
            ("documents", "meta_json", "TEXT"),
            ("relations", "candidate_id", "TEXT"),
            ("relations", "raw_fold", "TEXT"),
        ):
            try:
                if self.backend == "postgres":
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {decl}")
                else:
                    cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                    if col not in cols:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except Exception:  # noqa: BLE001 — a migration mustn't block startup
                pass
        for stmt in _POST_MIGRATE_INDEXES:
            try:
                self.conn.execute(stmt)
            except Exception:  # noqa: BLE001
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

    # -- writes ------------------------------------------------------------
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
                record.court,
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
            # projection, §1.2): clear this src's prior edges, then re-add.
            self.conn.execute("DELETE FROM relations WHERE src_id = ?", (record.stable_id,))
            for rel in record.relations:
                self._add_relation(record.stable_id, rel)

    @staticmethod
    def _edge_keys(rel: TypedRelation) -> tuple[str | None, str | None]:
        """``(candidate_id, raw_fold)`` for an edge — the normalised target id and the
        folded raw string, computed once here so every later read is an indexed lookup
        instead of re-running the matcher ladder (§5b)."""
        # Imported lazily: resolve/ imports the catalogue, so a module-level import cycles.
        from ..resolve.matchers import normalise_candidate
        from ..topics.gate import fold

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

    def add_relations(self, src_id: str, rels: list[TypedRelation]) -> None:
        """Bulk-add edges (one commit) — used by the citation-extraction stage."""
        for rel in rels:
            self._add_relation(src_id, rel)
        self.conn.commit()

    # -- extracted citations (§5, the audit/observation layer) -------------
    def add_citations(self, src_id: str, rows: list[dict]) -> None:
        """Bulk-record extracted citations (one commit). Each row: raw, entity_kind,
        candidate_id, pinpoint, char_start, char_end, method, confidence."""
        now = _now()
        for r in rows:
            self.conn.execute(
                """
                INSERT INTO citations (
                    src_id, raw, entity_kind, candidate_id, pinpoint,
                    char_start, char_end, method, confidence, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    src_id, r["raw"], r.get("entity_kind"), r.get("candidate_id"),
                    r.get("pinpoint"), r.get("char_start"), r.get("char_end"),
                    r.get("method"), r.get("confidence"), now,
                ),
            )
        self.conn.commit()

    def clear_citations(self, src_id: str) -> None:
        self.conn.execute("DELETE FROM citations WHERE src_id = ?", (src_id,))
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
        with self._atomic():
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

    def clear_relations(self, src_id: str, *, extracted_via: str) -> None:
        """Drop a source's edges from one extraction method, so re-running that
        extractor is idempotent (a re-derivable projection, §1.2). Leaves
        structurally-extracted and manual edges intact."""
        self.conn.execute(
            "DELETE FROM relations WHERE src_id = ? AND extracted_via = ?",
            (src_id, extracted_via),
        )
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
            "SELECT * FROM relations WHERE dst_id = ? AND resolution_status = 'resolved'",
            (dst_id,),
        ).fetchall()

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

    def resolve_pending_for(self, stable_id: str, ecli: str | None = None) -> int:
        """The incremental case: only edges pointing at THIS document (just harvested)
        can newly resolve, so a single indexed lookup replaces a whole-graph pass."""
        keys = [k for k in (stable_id, ecli) if k]
        qs = ",".join("?" * len(keys))
        with self._atomic():
            cur = self.conn.execute(
                f"""
                UPDATE relations SET dst_id = ?, resolution_status = 'resolved'
                WHERE resolution_status = 'pending' AND (
                    candidate_id IN ({qs})
                    OR lower(candidate_id) IN (SELECT alias FROM citation_aliases WHERE dst_id IN ({qs}))
                    OR raw_fold IN (SELECT alias FROM citation_aliases WHERE dst_id IN ({qs}))
                )
                """,
                (stable_id, *keys, *keys, *keys),
            )
            return max(cur.rowcount, 0)

    def backfill_edge_keys(self, *, batch: int = 20000, on_progress=None) -> int:
        """Populate ``candidate_id``/``raw_fold`` on edges written before those columns
        existed. Runs the matcher ladder once per DISTINCT raw string (a few hundred
        thousand) rather than once per edge (millions), then updates by string.

        The per-string UPDATE keys on ``raw_citation_string``, which isn't indexed in
        steady state (candidate_id is the hot column), so over millions of edges that would
        be a full scan each. Build a throwaway index for the duration and drop it after."""
        from ..resolve.matchers import normalise_candidate
        from ..topics.gate import fold

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

    def pending_reference_groups(self) -> list[sqlite3.Row]:
        """One row per distinct hanging reference — the worklist, as a single GROUP BY
        instead of a 450k-row Python pass (§5b, §8).

        ``inferred`` edges are heuristic carry-forwards (a bare "Section 12" pinned to the
        last-named Act); useful as in-document pinpoints, too ambiguous to drive harvesting,
        so they never enter the worklist. ``echr_citing`` says whether any citing document
        is a Strasbourg one — a bare ``115/92`` is an ECtHR application number there and an
        old CJEU case number anywhere else, and nothing but the citing document tells them
        apart."""
        agg = "string_agg(DISTINCT r.extracted_via, ',')" if self.backend == "postgres" \
            else "group_concat(DISTINCT r.extracted_via)"
        return self.conn.execute(
            f"""
            SELECT COALESCE(r.candidate_id, r.raw_citation_string) AS ref,
                   MAX(r.candidate_id)          AS candidate,
                   MIN(r.raw_citation_string)   AS raw,
                   MIN(r.dst_anchor)            AS anchor,
                   {agg}                        AS methods,
                   COUNT(*)                     AS occurrences,
                   COUNT(DISTINCT r.src_id)     AS citing_count,
                   MAX(CASE WHEN d.source = 'echr' THEN 1 ELSE 0 END) AS echr_citing
            FROM relations r
            JOIN documents d ON d.stable_id = r.src_id
            WHERE r.resolution_status = 'pending'
              AND r.extracted_via <> 'inferred'
              AND COALESCE(r.candidate_id, r.raw_citation_string) IS NOT NULL
            GROUP BY COALESCE(r.candidate_id, r.raw_citation_string)
            ORDER BY citing_count DESC
            """
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

    def get_alias(self, alias: str) -> str | None:
        row = self.conn.execute(
            "SELECT dst_id FROM citation_aliases WHERE alias = ?", (alias,)
        ).fetchone()
        return row["dst_id"] if row else None

    def put_alias(self, alias: str, dst_id: str, source: str | None = None, *, commit: bool = True) -> None:
        self.conn.execute(
            """
            INSERT INTO citation_aliases (alias, dst_id, source) VALUES (?,?,?)
            ON CONFLICT(alias) DO UPDATE SET dst_id = excluded.dst_id, source = excluded.source
            """,
            (alias, dst_id, source),
        )
        if commit:
            self.conn.commit()

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
    def _doc_filter_clauses(*, source, doc_type, tag, query, court=None, id_prefix=None):
        """Shared WHERE-clause builder for list/count_documents (so the Corpus browser and
        the Corpus Map deep-link with identical semantics). ``court`` matches the stored
        court token; ``id_prefix`` matches one or more slug heads (comma-separated), e.g.
        ``uksi`` → stable_ids like ``uksi/2016/413``."""
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
            clauses.append("(d.title LIKE ? OR d.stable_id LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        return clauses, params

    def list_documents(
        self,
        *,
        source: str | None = None,
        doc_type: str | None = None,
        tag: str | None = None,
        query: str | None = None,
        court: str | None = None,
        id_prefix: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """Browse/filter documents — lets an agent iterate, e.g., a law's sections
        to augment each with secondary material."""
        sql = "SELECT DISTINCT d.* FROM documents d"
        params: list[object] = []
        if tag:
            sql += " JOIN document_tags t ON t.doc_id = d.stable_id"
        clauses, fparams = self._doc_filter_clauses(
            source=source, doc_type=doc_type, tag=None, query=query, court=court, id_prefix=id_prefix)
        if tag:
            clauses.insert(0, "t.tag = ?"); params.append(tag)
        params.extend(fparams)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY d.decision_date DESC, d.stable_id LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self.conn.execute(sql, params).fetchall()

    def count_documents(self, *, source: str | None = None, doc_type: str | None = None,
                        tag: str | None = None, query: str | None = None,
                        court: str | None = None, id_prefix: str | None = None) -> int:
        """Total documents matching the same filters as :meth:`list_documents` — for
        the Corpus page's true count + pagination."""
        sql = "SELECT COUNT(DISTINCT d.stable_id) AS n FROM documents d"
        params: list[object] = []
        if tag:
            sql += " JOIN document_tags t ON t.doc_id = d.stable_id"
        clauses, fparams = self._doc_filter_clauses(
            source=source, doc_type=doc_type, tag=None, query=query, court=court, id_prefix=id_prefix)
        if tag:
            clauses.insert(0, "t.tag = ?"); params.append(tag)
        params.extend(fparams)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.conn.execute(sql, params).fetchone()["n"]

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

    def document_subtype_counts(self) -> list[sqlite3.Row]:
        """Held-document counts grouped by (source, doc_type, court, slug-prefix) — the raw
        material for the Corpus Map's per-sub-type "Held" column. The slug-prefix is the part
        of stable_id before the first '/' (``uksi/2016/413`` → ``uksi``); for ids without a
        slash (CELEX/ECLI) it's the whole id. Backend-portable (different string fns)."""
        if self.backend == "postgres":
            prefix = "split_part(stable_id, '/', 1)"
        else:
            prefix = ("substr(stable_id, 1, CASE WHEN instr(stable_id, '/') > 0 "
                      "THEN instr(stable_id, '/') - 1 ELSE length(stable_id) END)")
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
                   *, origin: str = "api") -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO jobs (job_id, kind, label, params_json, status, origin, "
            "started_at, heartbeat_at) VALUES (?,?,?,?,'running',?,?,?)",
            (job_id, kind, label, json.dumps(params or {}), origin, now, now),
        )
        self.conn.commit()

    def heartbeat_job(self, job_id: str, progress: dict, log_tail: list[str]) -> None:
        self.conn.execute(
            "UPDATE jobs SET progress_json = ?, log_json = ?, heartbeat_at = ? WHERE job_id = ?",
            (json.dumps(progress or {}), json.dumps(log_tail[-300:]), _now(), job_id),
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
