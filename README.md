# RagLex

RagLex builds and searches a corpus of law. It collects case law, legislation, and
regulatory guidance from official and free sources across many jurisdictions, keeps the
original documents next to the text extracted from them, works out how the documents cite
one another, and lets you search and navigate the result.

## How it works

The core loop is simple. An adapter for a source discovers what is available, fetches each
document, and passes it to a shared pipeline. The pipeline stores the raw bytes exactly as
received, extracts clean text, records the document in a catalogue, and reads the text for
citations to other legal materials. Those citations become edges in a graph. When a cited
document is harvested later, the edge resolves and the two documents are joined. Because the
raw bytes are kept and addressed by their content hash, everything derived from them (the
text, the structural segments, the embeddings, the citation edges) can be rebuilt without
fetching anything again.

Documents are versioned. When a source changes a document, the earlier version is archived
rather than overwritten, and the change is visible in the interface.

## Sources and jurisdictions

RagLex harvests from the United Kingdom (the National Archives Find Case Law service,
legislation.gov.uk, BAILII (only indirectly from pre-downloaded databases, not scraping), the House of Lords archive, and can accept cases and legislation imported
from Westlaw and Lexis exports if the licensing terms allow), the European Union (Court of Justice case law and
legislation through the CELLAR and EUR-Lex services), the European Court of Human Rights
through HUDOC, Ireland (legislation via API, cases via manual import), the United States through CourtListener (both its live API and its
bulk exports), Canada, Australia, New Zealand, Singapore, Hong Kong, India, and the
Netherlands. It also collects regulatory guidance and decisions from bodies such as the
European Data Protection Board, the Article 29 Working Party, Ofcom, and the Information
Commissioner's Office. Adding a jurisdiction or a source means writing an adapter; the rest
of the system does not change.

A document that the corpus cites but does not hold can often be fetched on demand from the
source that publishes it. Where that is not possible, the system can point you at the free
legal information institute that carries it (BAILII, AustLII, CanLII, NZLII, and the others)
so you can read it there. This is important as broadly the LIIs do not allow scraping and so the system respects that and directs humans to read there.

## Citations and resolution

Recognising a citation and identifying the document it points to are kept separate. A set of
grammars reads the document text and recognises ECLIs, CELEX numbers, European regulations
and directives by number and by name, UK acts and sections, Court of Justice case numbers,
United States reporter citations, and neutral citations in general, meaning the shape "[year]
COURT number" even for courts the system has never seen before. An optional language model
pass can be turned on to catch references written in prose that no grammar matches, and to
read how one case treats another, whether it follows, distinguishes, overrules, applies, or
merely mentions it. When the model is not configured or cannot be reached, the deterministic
grammars carry on unaffected, so the model only ever adds recall and never breaks the
pipeline.

Resolution then matches a recognised citation to a held document by its identifier, such as
an ECLI, a CELEX number, a neutral citation, or a legislation.gov.uk or Find Case Law
address, or by a report-series alias where a case is cited under a printed reporter rather
than its neutral citation. Citations the corpus makes but cannot yet satisfy form a worklist
of things to harvest, ranked by how often they are cited. From a citation's shape alone the
system infers which jurisdiction it belongs to and which source could fetch it, so a
frequently cited court with no adapter yet shows up as a signal that one is worth writing.

## Search and navigation

Search combines full-text matching with vector similarity, fuses the two rankings, can rerank
the top results with a cross-encoder, and then expands along the citation graph so that the
answer includes the authorities most closely connected to the matches. A separate measure of
authority, computed as a PageRank over the whole citation graph, feeds the ranking and a
citator view that shows how heavily a document is relied on and which of the citing documents
matter most.

The web application lets you search, read a document with its citations linked inline,
pincite a particular paragraph or section, follow the citation graph outward from any
document, and see at a glance what the corpus holds and what it is missing, broken down by
jurisdiction.

## The agent interface

A companion server exposes the same corpus to a language-model agent over the Model Context
Protocol. Its main tool takes a citation, resolves it, and returns the document, or a
pinpointed passage with a chosen amount of surrounding context, together with the different
ways the authority is cited, who cites it, and similar cases found through shared citations.
If the corpus does not hold the citation yet but can fetch it, it does so quietly and returns
the text; if it cannot, it returns a link to read it elsewhere. Retrieval and navigation are
the tools an agent sees first; the operations that change the corpus, such as harvesting and
imports, sit behind a single maintenance tool so they do not crowd the context for the tools
used most often. The web API and the agent server share one service layer, so the two never
drift apart.

## Keeping the corpus current

A background scheduler runs saved searches on a cadence, drains a little of the harvest
worklist on each tick, re-checks legislation for amendments that have since come into force,
tops up the authority ranking as new citations land, and, overnight when nothing else is
running, works through the routable references the corpus is still missing.

## Storage

The catalogue runs on PostgreSQL with pgvector for vector similarity and tsvector for
full-text search. For local use it runs on SQLite instead, with brute-force cosine similarity
and FTS5, behind the same interface, so both are exercised by the same code and the same
tests. Raw documents and extracted text are stored as files named by their content hash.

## Running it

```bash
uv sync
uv run raglex sources                       # list the registered adapters
uv run raglex run uk-caselaw --backfill --max-pages 1   # harvest, resolve, tag
uv run raglex extract                        # find citations and classify treatment
uv run raglex worklist --limit 10            # most-cited references not yet held
uv run raglex embed                          # chunk and embed documents that have text
uv run raglex search "right to erasure of personal data"
uv run raglex stats                          # corpus breakdown and resolution coverage
uv run raglex serve                          # the web API (needs: uv sync --extra web)
uv run raglex mcp                            # the agent server
uv run pytest
```

For the web interface, run the API with `uv sync --extra web && uv run raglex serve`, which
serves on port 8000, and then start the frontend with `cd frontend && npm install && npm run
dev`, which serves on port 5173 and proxies its API calls back to the server. API keys and a
Zotero login go in the Settings tab and persist to `data/settings.json`; an environment
variable overrides the file when both are set. `docker compose up` runs the API and the agent
server with `./data` bind-mounted.

Embeddings use a zero-dependency offline provider by default. Set
`RAGLEX_EMBED_PROVIDER=openrouter`, along with `OPENROUTER_API_KEY` and `RAGLEX_EMBED_MODEL`,
to use a hosted model instead.

Configuration is driven by environment variables, with everything defaulting under `./data`.
Set `RAGLEX_DB_URL=postgresql://...` to use the Postgres and pgvector catalogue rather than
the bundled SQLite file; the catalogue detects the backend from the connection string. A
local Postgres is available with `docker compose up db`.

## Project layout

The `core` package holds the jurisdiction-agnostic pieces: the document model, the adapter
protocol, and the segmentation and rate-limited HTTP helpers. `adapters` contains the source
adapters and the registry that lists them, and `formats` holds the parsers for structured
legal formats such as Akoma Ntoso and Formex that the adapters share. `citations` covers
citation extraction, the court registry, and the reasoning that turns pending references into
a harvest worklist; `resolve` matches citations to documents. `storage` is the content-
addressed raw store and the catalogue, `embeddings` and `retrieval` are the indexing and
search stages, and `tagging` is the rule engine for user-defined tags. `llm` is the single
resilient client behind the optional extraction and treatment passes, and `scraping` and
`imports` handle anti-bot fetching and manual or Zotero imports. `facade.py` is the one
service layer shared by the web API in `web` and the agent server in `mcp_server.py`, and
`frontend` is the React interface. The canonical production schema lives in
`schema/postgres.sql`.
