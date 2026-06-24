# RagLex

A robust, incremental, multi-jurisdiction harvester and analysis system for
case-law, legislation, and regulatory guidance — with an initial focus on data
protection and freedom of information. See [`raglex design docs/`](raglex%20design%20docs/)
for the full design rationale and source map.

## Status — build steps 1, 3, 4 (§9)

Implemented and tested end-to-end against the live UK source:

- **Step 1 — spine + UK & NL adapters.** Adapter pattern, content-addressed raw +
  text stores, catalogue with typed-relations edges, two-stage topic gate, shared
  ingest pipeline with watermarks. **UK Find Case Law** (Atom/LegalDocML) and
  **NL Rechtspraak** (REST/XML, ECLI-native, FormeleRelaties typed edges for free)
  feed the same pipeline — the cross-jurisdiction premise, demonstrated.
- **Step 6 — EU CELLAR adapter (SPARQL).** CJEU case law via the CELLAR SPARQL
  endpoint (CDM ontology) + Formex content; discovers case law linked to any
  configured legislation and brings the EU citation graph (typed
  `interprets`/`applies` + cited-case edges with ECLI destinations). CELLAR is
  also the resolution target for the EU citations every other source makes.
- **Step 3 — entity resolution (§5b).** Deterministic matchers (ECLI, CELEX, UK
  neutral citation, legislation.gov.uk + Find Case Law URIs), the resolution
  ladder (preferring adapter-supplied `dst_id`s), the retry queue, and the
  `cite_count`-ranked harvest worklist.
- **Step 4 — rule-based tagging engine (§4a).** `literal`/`regex`/`grep_like`/
  `field` predicates composed in an AND/OR/NOT condition tree, dry-run preview,
  provenance-tagged results, manual-tag protection, and the §4 topic vocabularies
  re-expressed as editable seed rules.
- **Steps 10–11 — embeddings + hybrid retrieval (§6).** Pluggable embedding
  providers (§6d — zero-dep offline default + OpenRouter), **structure-aware
  chunking** (§6b — adapters emit native units: CJEU `NP.ECR` paragraphs, UK
  Akoma Ntoso paragraphs, NL paragraphs; oversized units sliced within their own
  span; contextual headers; char offsets back to source), a re-runnable embed
  stage, and hybrid retrieval (§6c): FTS + vector → **RRF** → reranker →
  **GraphRAG** 1-hop typed-neighbour expansion, with partition pre-filtering.
- **Legislation, not just cases (§0).** Pluggable **format parsers**
  (`formats/`: Akoma Ntoso, Formex-legislation, BWB, EUR-Lex HTML) decoupled from
  fetch adapters, plus **UK** (legislation.gov.uk Akoma Ntoso), **EU** (CELLAR
  Formex — the GDPR's 99 articles), and **NL** (wetten.overheid.nl BWB toestand
  XML — the UAVG) legislation adapters. Their
  stable_ids are the resolution targets, so harvesting FOIA/DPA/GDPR **closes the
  §5b loop** (dangling "cites FOIA s.14" / "interprets 32016R0679" edges resolve).
  A structured, hierarchy-aware **legislation reader** renders the AKN/Formex with
  CSS while the raw markup stays the machine-readable base. *(AKN4EU isn't yet a
  CELLAR-published manifestation even for the 2024 AI Act — only `fmx4`/`xhtml`/
  `pdf` — so Formex is used today; an `akn4eu` parser is a drop-in when it lands.)*
- **Full document versioning (§1.4).** A document is a series of versions; on an
  upstream content change the prior version is archived to `document_versions`
  (raw + text stay content-addressed), surfaced as history in the UI.
- **Citation extraction + treatment (§5, §1.3a).** A pluggable **grammar registry**
  ([core/registry.py](src/raglex/core/registry.py) is the shared extensibility
  primitive) mines references from document *text* — entity-level (cases /
  regulations / directives / acts) with **pinpoints** — recorded in a `citations`
  audit table (with char spans + confidence) that feeds **deduped hanging edges**.
  Grammars cover **ECLIs (any jurisdiction)**, **CELEX**, EU
  regulations/directives by number *and* name, UK acts/sections, **CJEU case
  numbers including procedure suffixes** (`C-11/26 P`, `C-619/18 PPU`, `T-1/24 R`,
  joined cases), and **generic neutral citations** — the *shape* `[2024] COURT N`
  / `2024 SCC N` for **known and unknown** courts (divisions like `EWCA Civ`
  folded in). A **treatment classifier** then reclassifies bare `mentions` into
  `follows` / `distinguishes` / `overrules` / `applies` / `considers` from the
  prose around each citation, scoped to the citation's own sentence. Extraction
  (recognise) and resolution (identify) are separate; "Case C-311/18" → its ECLI
  judgment via a systematically-minted CELEX→ECLI alias. AG **Opinions** classified
  + linked to their judgment (`opinion_in`, CELEX `CC`→`CJ`).
- **Optional LLM passes (§5) — resilient & batched.** One shared, OpenAI-chat-
  shaped client ([llm/](src/raglex/llm/)) backs two *additive* enrichment passes
  behind the existing interfaces: a **narrative citation extractor** (the
  references prose carries that no grammar can catch — "the Court's earlier
  data-retention ruling", normalised to the same resolvable candidate forms) and
  an **LLM treatment classifier** (implicit treatments the cue phrases miss). Both
  are **batched** (one request per N citations) and **degrade safely** — no
  key / unreachable host / malformed JSON → the deterministic grammars +
  heuristics stand, so the LLM only ever *raises recall*, never breaks the
  pipeline. Everything that drifts between providers/versions is config (base URL,
  model, key env, json-mode, retries) so a new provider — OpenRouter, a local
  Ollama/vLLM endpoint, … — is a settings row. Auto-on when configured;
  `extract --llm` / `--no-llm` force it.
- **Citation snowball (§5a).** The references the corpus *makes* become the
  worklist of what to *harvest*: [citations/snowball.py](src/raglex/citations/snowball.py)
  reads pending candidates back through their detected form and infers
  `(form, jurisdiction, adapter)` from shape alone — a CELEX → `eu-legislation`,
  an `ECLI:NL:…` → `nl-rechtspraak`, a neutral citation's court token looked up in
  the [court registry](src/raglex/citations/courts.py). So a frequently-cited body
  with **no adapter yet** (an unknown neutral-citation court) surfaces as a
  ranked *build-an-adapter* signal — `raglex snowball --needs-adapter`.
- **Pinpoint / fragment linking (§1.9).** Typed edges carry `src_anchor`/
  `dst_anchor`, so a practitioner handbook's *pages* link to a law's *article*
  (`pp. 45-47` ─analyses→ `Article 17`) — JuriConnect-style. Imported PDFs are
  segmented by page; the legislation reader shows commentary pinned per article
  and a ＋link affordance on each part. Exposed in the API + MCP.
- **Citation-graph explorer (§8).** Interactive **Cytoscape** view — server-
  computed neighbourhood, expand-on-click, typed/coloured edges — the signature
  research view.
- **PostgreSQL spine (§7).** The catalogue runs on **Postgres + pgvector**
  (real ANN cosine) + **tsvector** FTS when `RAGLEX_DB_URL` is set, or portable
  **SQLite** (brute-force cosine + FTS5) otherwise — one method surface, two
  backends, both tested.
- **Step 14 — ops + research UI (§8).** Source-health dashboard, pipeline-queue
  view, citation-resolution coverage, corpus stats, and **push** alerting behind a
  pluggable notifier; a FastAPI API; a **React UI** (search, document reader,
  citation neighbours, dashboard, import, settings); and a shared **Facade** so a
  matching **MCP server** exposes every operation as tools.
- **Manual + Zotero import (§1.9, §5c).** PDF/HTML/text/Zotero items become
  secondary `documents` (`added_by=user`) sharing the graph — imported by file
  upload, URL, or base64, linked to a case/law section by a typed edge, with
  pluggable text extraction. An agent can drive the whole augment-a-law workflow
  over MCP. A UI-editable **settings store** (one bind-mountable file; env still
  overrides) holds API keys / Zotero login instead of env-var soup.

Later steps (extraction/OCR escalation, more adapters, embeddings, hybrid
retrieval, the ops/research UI) slot onto these abstractions without re-harvesting.

## Scope & generality

RagLex is a **general-purpose** legal-corpus system — domain- and
jurisdiction-agnostic by design. Data protection / freedom of information is the
*configured initial focus* (design §0), not the system's purpose, and it lives
entirely in **editable data and arguments**, never in the engine:

- topic vocabularies — [topics/vocab.py](src/raglex/topics/vocab.py) (data);
- seed tag rules — [tagging/seed.py](src/raglex/tagging/seed.py) (a bootstrap; the
  rule engine itself tags on *any* condition → *any* tag);
- the CELLAR adapter's focus is an argument — `raglex run eu-cellar -o
  legislation_celex=32004R0139` harvests EU Merger Regulation (competition-law)
  cases through the identical pipeline.

The core — document model, adapter protocol, pipeline, catalogue, entity
resolution, the rule engine — contains no domain-specific logic. Swap the
vocab/rules/arguments and the same machinery serves any legal domain or
jurisdiction. (GDPR makes a convenient, well-structured *test* area.)

## Architecture

```
raw immutable store (content-addressed, §1.2)
        │
        └── catalogue (PostgreSQL in production, §7; SQLite for local/dev)
                documents [polymorphic, versioned] · relations [typed edges]
                sources [watermarks] · tag tables (§4a, ready)

adapter.discover(since) → cheap topic gate → dedup (hash) → adapter.fetch()
        → store raw → confirm topic → catalogue + typed edges
```

Core principles, load-bearing: **ECLI as the primary key** (surrogate IDs where
none exists); **raw bytes immutable, everything else a re-derivable projection**;
**one polymorphic `documents` table**; **typed edges from day one**; **append-only
catalogue** (disappearance is a flag, never a row deletion).

## Quickstart

```bash
uv sync
uv run raglex sources                       # list registered adapters
uv run raglex tag seed                       # port §4 topic vocab into rules
uv run raglex run uk-grc --backfill --max-pages 1   # harvest → resolve → tag
uv run raglex status uk-grc                  # watermark + run-state
uv run raglex worklist --limit 10            # most-cited citations not yet harvested
uv run raglex extract --llm                   # citations + treatment (+ optional LLM pass, §5)
uv run raglex snowball --needs-adapter        # cited bodies with no adapter yet (§5a)
uv run raglex tag group data_protection      # group-by-tag view (§8)
uv run raglex embed                          # chunk + embed documents with text (§6)
uv run raglex search "right to erasure of personal data"   # hybrid + GraphRAG (§6c)
uv run raglex dashboard                      # source health + queues + alerts (§8)
uv run raglex stats                          # corpus breakdown + resolution coverage
uv run raglex serve                          # ops/research web API (needs: uv sync --extra web)
uv run raglex mcp                            # MCP server: every API op as a tool
uv run pytest
```

**Web UI** (search, document reader, dashboard, import, settings):
```bash
uv sync --extra web && uv run raglex serve      # API on :8000
cd frontend && npm install && npm run dev        # UI on :5173 (proxies /api → :8000)
```
Set API keys / Zotero login in the **Settings** tab — they persist to
`data/settings.json` (bind-mount `data/`); an env var overrides the file if set.
`docker compose up` runs the API + MCP server with `./data` bind-mounted.

Embeddings use the zero-dependency offline provider by default; set
`RAGLEX_EMBED_PROVIDER=openrouter` (+ `OPENROUTER_API_KEY`, `RAGLEX_EMBED_MODEL`)
for a real model. `raglex search` accepts `--source`, `--doc-type`, `--year-from`,
`--tag` partition filters.

`raglex run` harvests a source, then re-runs entity resolution (so citations to
freshly-harvested targets become live edges) and the enabled tag rules
(`--no-resolve` / `--no-tag` to skip).

Configuration is env-driven (`RAGLEX_DATA_DIR`, `RAGLEX_RAW_DIR`,
`RAGLEX_TOPIC_THRESHOLD`); defaults put everything under `./data`. Set
`RAGLEX_DB_URL=postgresql://…` to use the Postgres + pgvector spine (§7) instead
of the bundled SQLite file — the catalogue detects the backend from the string.
Run a local one with `docker compose up db` (pgvector on :55432); the test suite
covers it via `RAGLEX_TEST_PG_URL`.

## Layout

| Path | Role |
|---|---|
| `src/raglex/core/` | jurisdiction-agnostic models, adapter protocol, errors, rate-limited HTTP |
| `src/raglex/storage/` | content-addressed raw store + catalogue repository |
| `src/raglex/topics/` | two-stage topic gate + multilingual vocabularies (§4) |
| `src/raglex/pipeline/` | the shared ingest runner (§5) |
| `src/raglex/adapters/` | source adapters (UK/NL/EU case law + UK/EU legislation) + registry |
| `src/raglex/formats/` | pluggable format parsers — Akoma Ntoso, Formex, BWB, EUR-Lex HTML |
| `src/raglex/citations/` | citation extraction — grammar registry, entity-level, pinpoints, court registry + snowball (§5/§5a) |
| `src/raglex/llm/` | one resilient, batched, OpenAI-shaped LLM client behind the optional extraction/treatment passes (§5) |
| `src/raglex/scraping/` | anti-bot fetchers (Scrapling/Camoufox, Playwright) + recipe adapters (§5a) |
| `src/raglex/resolve/` | entity resolution — matchers + ladder + queue (§5b) |
| `src/raglex/tagging/` | rule engine — predicates, condition tree, seed rules (§4a) |
| `src/raglex/embeddings/` | providers (§6d), structural chunking (§6b), embed stage (§6) |
| `src/raglex/retrieval/` | hybrid FTS+vector RRF, reranker, GraphRAG, search (§6c) |
| `src/raglex/ops/` | observability views + push alerting (§8) |
| `src/raglex/extraction/` | pluggable PDF/HTML/text extraction (§5c) |
| `src/raglex/imports/` | manual + Zotero import of secondary material (§1.9) |
| `src/raglex/facade.py` | one service layer shared by the API and the MCP server |
| `src/raglex/web/` | FastAPI ops/research + import/settings API (§8) |
| `src/raglex/mcp_server.py` | MCP server — every API operation as a tool |
| `src/raglex/settings.py` | UI-editable, file-persisted settings/secrets store |
| `frontend/` | React (Vite + TS) ops/research UI |
| `schema/postgres.sql` | canonical production catalogue schema (§7, Appendix B) |
