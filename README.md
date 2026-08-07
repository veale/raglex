# RagLex

RagLex builds and searches a corpus of law. It collects case law, legislation, and
regulatory guidance from official and free sources across many jurisdictions, keeps each
original document beside the text extracted from it, works out how the documents cite one
another, and lets a person or an agent search and navigate the result.

## How it works

An adapter for a source discovers what is available, fetches each document, and hands it to a
shared pipeline. The pipeline stores the raw bytes exactly as received, extracts clean text,
records the document in a catalogue, and reads the text for citations to other legal
materials. Those citations become edges in a graph; when a cited document is harvested later,
the edge resolves and the two documents join up. Because the raw bytes are kept and addressed
by their content hash, everything derived from them — the text, the structural segments, the
embeddings, the citation edges — can be rebuilt without fetching anything again.

Documents are versioned. When a source changes a document the earlier version is archived
rather than overwritten, and the change is visible in the interface. Legislation goes further:
you can hold a provision as it stood on a given date, so an old judgment reads against the law
in force when it was decided rather than today's amended text.

## Sources and jurisdictions

The United Kingdom is the deepest jurisdiction. Case law comes from the National Archives Find
Case Law service and the First-tier Tribunal's General Regulatory Chamber; legislation from
legislation.gov.uk, with amendments and point-in-time versions; regulatory material from Ofcom
(online-safety documents and enforcement) and the Home Office codes of practice under the
Investigatory Powers Act. The House of Lords archive and case reports that predate neutral
citations are brought in from pre-downloaded databases rather than by scraping, and where the
licence allows, cases and statutes exported from Westlaw or Lexis can be imported and keyed by
their neutral citation, their ECLI, or the report series they were printed in.

For the European Union, RagLex takes Court of Justice case law and legislation through the
CELLAR and EUR-Lex services, along with Commission preparatory and policy documents. The
European Parliament's adopted texts are held back to the first directly elected Parliament
in 1979 — chiefly the non-legislative resolutions, which read an instrument already in force
and say at length how it is working. Recent ones come from the Parliament's own open-data
service within days of the vote rather than waiting for the Official Journal, which can
publish them a year late; each is held under its CELEX, its `P8_TA(2017)0051` reference and
its `T8-0051/2017` form alike, with recitals and numbered paragraphs as citable units. The
Commission's formal reply to each resolution is held beside it. Data
protection and platform regulation are covered in some depth: the European Data Protection
Board's guidelines and opinions, the one-stop-shop register of final decisions taken under
Article 60, the Article 29 Working Party archive, the Digital Markets Act case register, and
the decisions and analysis collected on GDPRhub. The European Court of Human Rights is read
through HUDOC.

France is covered through Légifrance for codes and consolidated legislation, the Cour de
cassation via Judilibre, the Conseil d'État, the Conseil constitutionnel, CNIL deliberations,
and the DILA open-data bulk release. Germany is covered through the federal statute and
case-law collections at gesetze-im-internet and rechtsprechung-im-internet, with the newer
NeuRIS service in beta. Ireland has legislation both as enacted (eISB) and as revised by the
Law Reform Commission, plus the superior courts. The Netherlands is read through Rechtspraak
and the KOOP legislation register.

Outside Europe, United States case law comes from CourtListener, using both its live API and
its bulk exports. Canada contributes federal legislation from Justice Laws, a bulk case-law
corpus from A2AJ, and CanLII metadata and citator links. Australia is covered at the
Commonwealth level through the Federal Register and at the state level through the LawMaker
services, with case law from the Open Australian Legal Corpus and live feeds for the High
Court, the Federal Court, and New South Wales. New Zealand, Singapore, Hong Kong, and India
are held for legislation or through bulk case-law imports. Adding a jurisdiction or a source
means writing an adapter; nothing else in the system changes.

Regulatory decisions from data-protection authorities are treated as their own kind of
material, distinct from both case law and guidance, and are attributed to the country whose
authority made them rather than to the register that happens to publish them.

## Citations and resolution

Recognising a citation and identifying the document it points to are kept separate. A set of
grammars reads the text and picks out ECLIs, CELEX numbers, European regulations and
directives by number and by name, UK acts and their sections, Court of Justice case numbers,
United States reporter citations, and neutral citations in general — the shape "[year] COURT
number" even for a court the system has never seen. Pinpoints are captured too, so a citation
of Article 15 of a regulation or section 45 of an act is tied to that provision and not merely
to the instrument. An optional language-model pass can be turned on to catch references
written in prose that no grammar matches, and to read how one case treats another, whether it
follows, distinguishes, overrules, applies, or only mentions it. When the model is not
configured or cannot be reached the grammars carry on unaffected, so the model adds recall and
never breaks the pipeline.

Resolution matches a recognised citation to a held document by its identifier — an ECLI, a
CELEX number, a neutral citation, a legislation.gov.uk or Find Case Law address — or by an
alias where the same authority is cited in another form. Aliases carry the rules rather than a
cache: a CELEX number mapped to its ECLI, a chamber-less form of a neutral citation, a case
cited under the printed reporter it appeared in rather than its neutral citation. Report
citations are matched whether or not the abbreviations carry full stops, so "[1996] 3 S.C.R.
458" and "[1996] 3 SCR 458" reach the same case. The matching is set-based over identifiers
persisted on each edge, so re-resolving after an import is a few indexed passes rather than a
walk over the whole graph.

Citations the corpus makes but cannot yet satisfy form a worklist, ranked by how often they
are cited. From a citation's shape alone the system infers which jurisdiction it belongs to
and which source could fetch it, so a frequently cited court with no adapter shows up as a
signal that one is worth writing.

## Search and navigation

Search combines full-text matching with vector similarity, fuses the two rankings, can rerank
the top results with a cross-encoder, and then expands along the citation graph so the answer
carries the authorities most closely connected to the matches. A separate measure of standing,
a PageRank over the whole citation graph, feeds the ranking and a citator view that shows how
heavily a document is relied on and which of the documents citing it matter most. Search by
concept depends on documents having been embedded; where that pass has not run, finding a case
or an act by its name or its citation still works, and the interface says so rather than
returning nothing.

The web application lets you search, read a document with its citations linked inline, pincite
a paragraph or a section, follow the citation graph outward from any document, and see at a
glance what the corpus holds and what it is missing, broken down by jurisdiction and by
category. A reader can flag a passage where the linking is wrong, and anyone can file a bug or
a feature request from the page they are on; both land in a review queue with the context
attached. Access can be left open for local use or locked down with reader and administrator
roles, address allow-lists, and passkeys.

## Getting authorities the corpus does not hold

A document that the corpus cites but does not hold can often be fetched on demand from the
source that publishes it, and the agent's lookup does this quietly. Where that is not possible
the system points you at the free legal-information institute that carries it — BAILII,
AustLII, CanLII, NZLII, and the rest — so you can read it there. The institutes largely do not
permit scraping, and RagLex respects that: it directs a person to read rather than fetching on
their behalf.

For the pre-neutral-citation authorities that only the subscription databases hold, RagLex
produces paste-ready batches of report citations for Westlaw's Find & Print or Lexis's Get &
Print, ranked by how often the corpus reaches for each and filtered to the jurisdiction whose
reports a given subscription can actually retrieve. Anything the corpus can already resolve is
left out, so the list is the genuine backlog rather than work already done.

## Keeping the corpus current

A background scheduler runs saved searches on a cadence, drains a little of the harvest
worklist on each tick, re-checks legislation for amendments that have since come into force,
tops up the authority ranking as new citations land, and, overnight when nothing else is
running, works through the routable references the corpus is still missing. Longer jobs record
their progress and resume where they left off if the process restarts, and the heavy roll-ups
that a bulk import invalidates are run once at the end of a batch rather than after every
file.

## The agent interface

A companion server exposes the same corpus to a language-model agent over the Model Context
Protocol. Its front door takes a citation — or a statute named in prose, or a specific
provision — resolves it, and returns the document or a pinpointed passage with a chosen amount
of surrounding context. When the citation names a provision it also returns the documents that
cite that provision, not the whole instrument, with counts by jurisdiction and kind and a
handle to page, filter, and sort that list; asking which cases cite Article 15 of the GDPR is
one call. If the corpus does not hold the citation yet but can fetch it, it does so silently
and returns the text; if it cannot, it returns a link to read it elsewhere. Retrieval and
navigation are the tools an agent sees first, and the operations that change the corpus sit
behind a single maintenance tool so their schemas do not crowd the context for the tools used
most. Remote agents can authenticate over OAuth. The web API and the agent server share one
service layer, so the two never drift apart.

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
uv run raglex extract                       # find citations and classify treatment
uv run raglex worklist --limit 10           # most-cited references not yet held
uv run raglex embed                         # chunk and embed documents that have text
uv run raglex search "right to erasure of personal data"
uv run raglex stats                         # corpus breakdown and resolution coverage
uv run raglex export-static 32016R0679      # one-file law + incoming-citations edition
uv run raglex serve                         # the web API (needs: uv sync --extra web)
uv run raglex mcp                           # the agent server
uv run pytest
```

`export-static` writes a self-contained HTML file under `data/exports` unless `--output`
names another path. It embeds the held text, provision index, incoming references, excerpts,
filters and public-source links; it makes no API requests when opened, so the file can be
used offline or copied directly to GitHub Pages. Where the public page supports it, excerpt
links include a browser text fragment which attempts to scroll to and highlight the passage.
PDF links use a page fragment when RagLex has a page anchor. Missing public copies remain in
the results and are labelled as such.

Administrators can download the same edition from the document page's `…` menu. For a large
instrument, `POST /export/static-law` starts a durable background build; poll its `job_id` at
`GET /jobs/{job_id}`, then download from
`GET /export/static-law.html?id=32016R0679`. Pass `{"id":"32016R0679","refresh":true}` to
rebuild the saved edition from the current corpus. Static-export jobs skip the job queue, so
a running import never delays one.

The Settings page's “Static exports” panel builds a **set** of instruments as one small
website: each row names a statute and the filename it saves as (`gdpr.html`), and every
build also writes an `index.html` linking them all, with its own title and text. The panel
holds the shared line shown beneath every exported title, and each row may add a second line
of its own, beneath it, in that file only; every edition links back to the index. Both accept
simple HTML (`<a>`, `<b>`, `<i>`, `<u>`, `<br>`) and the placeholders `<dateexported>`,
`<datetimeexported>`, `<yearexported>` and `<count>`. Each index entry states the date its
edition was exported.

Every build writes the flat folder given as the export folder (by default
`<data dir>/exports/site`, beside the catalogue and the stores), replacing same-named files
in place; “Build and download ZIP” additionally hands the browser a zip of the whole set.
Because the expensive half of a build (the citing documents and their excerpts) is cached as
data, editing the shared line, a row's line or the index text needs only the quick re-render,
not another pass over the corpus. `POST /export/bundle/build` does the same over the API, and
enabling the `static-bundle` scheduler task republishes the folder on a cadence.

For the web interface, run the API with `uv sync --extra web && uv run raglex serve`, which
serves on port 8000, and then start the frontend with `cd frontend && npm install && npm run
dev`, which serves on port 5173 and proxies its API calls back to the server. API keys and a
Zotero login go in the Settings tab and persist to `data/settings.json`; an environment
variable overrides the file when both are set. `docker compose up` runs the API and the agent
server with `./data` bind-mounted.

Embeddings use a zero-dependency offline provider by default. Set
`RAGLEX_EMBED_PROVIDER=openrouter`, along with `OPENROUTER_API_KEY` and `RAGLEX_EMBED_MODEL`,
for a hosted model; for a large corpus there is also an orchestrator that runs the embedding
pass on a UCL HPC cluster and imports the vectors back. Indexing the whole corpus is
expensive, so the scope can be limited to chosen jurisdictions with
`RAGLEX_EMBED_JURISDICTIONS`.

Configuration is driven by environment variables, defaulting under `./data`. Set
`RAGLEX_DB_URL=postgresql://...` to use the Postgres and pgvector catalogue rather than the
bundled SQLite file; the catalogue detects the backend from the connection string. A local
Postgres is available with `docker compose up db`.

## Project layout

The `core` package holds the jurisdiction-agnostic pieces: the document model, the adapter
protocol, and the segmentation and rate-limited HTTP helpers. `adapters` contains the source
adapters and the registry that lists them, and `formats` holds the parsers for structured
legal formats such as Akoma Ntoso and Formex that the adapters share. `citations` covers
citation extraction, the court registry, and the reasoning that turns pending references into
a harvest worklist; `resolve` matches citations to documents. `storage` is the content-
addressed raw store and the catalogue, `embeddings` and `retrieval` are the indexing and
search stages, and `tagging` is the rule engine for user-defined tags. `llm` is the single
resilient client behind the optional extraction and treatment passes; `scraping` and `imports`
handle anti-bot fetching and manual, PDF, or Zotero imports; `jobs` runs and resumes the
background work. `facade.py` is the one service layer shared by the web API in `web` and the
agent server in `mcp_server.py`, and `frontend` is the React interface. The canonical
production schema lives in `schema/postgres.sql`.
