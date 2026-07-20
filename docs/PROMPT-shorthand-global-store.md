# Task prompt: promote in-document shorthands/abbreviations to a corpus-wide store

Paste this into a fresh session working on the RagLex repo.

---

## Context you need

**RagLex** is a self-hosted multi-jurisdiction legal corpus system (~700k documents:
UK/EU/Commonwealth case law, legislation, regulator guidance). Python backend
(`src/raglex/`), React/TS frontend (`frontend/src/`), SQLite for dev/tests and
PostgreSQL in production. Tests are `pytest` at the repo root (`python3 -m pytest
tests/`), currently ~862 passing — keep them green and add new ones.

House style, which the codebase holds to consistently and you should match:
- Comments explain **why**, not what — especially the incident or defect that
  motivated the code. Look at `src/raglex/citations/extractor.py` for the tone.
- Every behavioural fix gets a regression test that names the real-world case.
- SQL must run on **both** backends (no `ILIKE`, no backend-only casts). LIKE
  patterns are bound as parameters, never inlined (a literal `%` in SQL text trips
  psycopg's placeholder scan).

### The citation pipeline (what you're extending)

`extract_citations(text, aliases=…)` in `src/raglex/citations/extractor.py` runs
registered grammars, then a chain of post-passes. Each returns `Citation` objects
(`raw`, `entity_kind`, `candidate_id`, `pinpoint`, `char_start`, `char_end`,
`method`, `confidence`). `candidate_id` is the resolvable id (ECLI/CELEX/legislation
URI/neutral-citation slug); resolution later links it to a held document.

`extract_document(catalogue, textstore, stable_id, …)` in
`src/raglex/citations/stage.py` is the per-document driver: it loads text, calls the
extractor, writes `citations` rows (the audit layer, with char spans) and collapses
them into deduped `relations` edges. **This is the layer with catalogue access** —
the pure extractor has none.

### What already exists (do not rebuild it)

`_attach_shorthands` in `extractor.py` already does the **in-document** job:

1. **Definitions** are collected beside a citation the grammars resolved:
   - any bracket (`[]`, `()`, `{}`) holding a name in single/double/curly quotes or
     behind a cue (`hereinafter`, `hereafter`, `henceforth`, `collectively`, `or`) —
     `("Digital Rights")`, `('FMIOA')`, `(hereinafter "the Charter")`;
   - a bare name in **square** brackets only (the OSCOLA convention) — `[Suncor]`;
   - the CJEU idiom `judgment in <Name>` beside the citation;
   - **party-name derivation** with no marker at all: `Dunsmuir v. New Brunswick,
     2008 SCC 9` registers `Dunsmuir`. A `_GENERIC_PARTY` stoplist blocks
     `Canada`/`R`/`the Minister` etc. so a bare "Canada, at para 5" can't mislink.
2. **Uses** are linked later in the same document:
   - case short-names require a pincite — `Dunsmuir, at para. 61`,
     `Judgment in Digital Rights, paragraph 57`;
   - **abbreviations** (initialisms, per `_is_abbrev`) hosted by a statute link on a
     **bare** mention too — `the FCA`, `under FCA`.

Everything is scoped to one document: a shorthand learned in document A is
invisible in document B.

## What to build

Promote these learned shorthands into a **corpus-wide store**, applied in other
documents but tightly gated so it cannot manufacture false links.

The owner's specification, verbatim in substance:

> It could build a list of such abbreviations stored against the cases, and in
> cases where these cases are detected, that list could also be checked as well as
> any term derived from the case itself. But the abbreviations would not otherwise
> be checked if the case was not cited, as that would create false positives for
> simple abbreviations. Of course, another gate would be it wouldn't trigger unless
> there was a pincite. Also stored against statutes not just cases.

### The gates (these are the point of the feature)

1. **Parent-cited gate.** A stored shorthand for candidate `X` is only ever applied
   in a document that **already cites `X`** (by any other means — full citation,
   ECLI, neutral citation). Never apply a stored abbreviation corpus-wide: a bare
   "CA" or "FCA" in an unrelated judgment must not link.
2. **Pincite gate for cases.** A stored *case* short-name links only when the use
   carries a pincite (`Dunsmuir, at para 61`), exactly as the in-document rule does.
   Statute abbreviations may link on a bare mention, since an initialism hosted by a
   statute is far less ambiguous — but see (3).
3. **Ambiguity guard.** If one abbreviation maps to more than one candidate, do not
   guess: either skip it, or apply it only when a single one of those candidates is
   cited in this document. Short/ambiguous tokens (≤2 characters, and common legal
   initialisms like `CA`, `SC`, `HC`, `CJ`, `DPP`) should be excluded or require the
   parent-cited gate plus a pincite.
4. **Never override** an in-document definition. If the document defines the
   shorthand itself, that wins.

### Suggested shape (adjust if you find something better)

- **Schema.** A new table, created in `src/raglex/storage/catalogue.py` alongside
  `citation_aliases` (note: `citation_aliases` is the *unconditional* user-alias map
  — deliberately NOT the right home, because it is applied everywhere). Something
  like `learned_shorthands(shorthand, candidate_id, entity_kind, documents, PRIMARY
  KEY (shorthand, candidate_id))`. Follow the existing DDL conventions
  (`CREATE TABLE IF NOT EXISTS`; new columns go through `Catalogue._migrate`).
- **Population.** In `extract_document`, after extraction, harvest the definitions
  the document established and upsert them. You will need the extractor to expose
  them — add a small function (e.g. `shorthand_defs(text, cites)`) rather than
  changing `extract_citations`'s return type, which has many callers.
- **Application.** Also in `extract_document`: collect the candidate ids the document
  cites, load stored shorthands **for those candidates only**, and link their uses
  (respecting the gates above), appending `Citation` rows with a distinct `method`
  (e.g. `shorthand_global`) and a confidence below the in-document one.

### Hard constraint: the rescan write path

`extract_document` runs across the whole corpus during a re-extraction
(`facade.rescan`, ~700k documents, parallel workers). A naive per-document
`INSERT` into a shared table will contend badly on Postgres. Mitigate — e.g. only
write genuinely new pairs, batch them, use `ON CONFLICT DO NOTHING`, and consider
making population a **separate pass** over the already-written `citations` rows
rather than inline in the hot loop. Measure before shipping: a rescan is a
multi-hour job and this must not slow it materially or deadlock it.

### Tests to write

- A shorthand defined in document A links in document B **only** when B also cites
  the parent (both directions asserted).
- A case short-name in B without a pincite does **not** link; with one, it does.
- An ambiguous abbreviation mapping to two candidates does not guess.
- An in-document definition beats a conflicting stored one.
- A common initialism (`CA`) in a document that does not cite the parent links
  nothing.
- Population is idempotent — running extraction twice doesn't duplicate rows.

### Where to look first

| Path | Why |
| --- | --- |
| `src/raglex/citations/extractor.py` | `_attach_shorthands`, `_SHORTHAND_DEF`, `_is_abbrev`, `_party_short_form`, `_GENERIC_PARTY` |
| `src/raglex/citations/stage.py` | `extract_document` — the catalogue-aware driver |
| `src/raglex/storage/catalogue.py` | DDL, `_migrate`, `citation_aliases`, `named_alias_map` |
| `tests/test_citations.py`, `tests/test_grammar_adversarial.py` | existing shorthand tests to extend |
| `src/raglex/facade.py` | `rescan` — the pass this must not slow down |

### Definition of done

Tests green (`python3 -m pytest tests/`), the frontend still builds
(`cd frontend && npx tsc --noEmit && npx vite build`), a measured statement that the
rescan hot path is not materially slower, and a note in the commit message of how
many stored shorthands the live corpus yields and how many extra links they produce.
