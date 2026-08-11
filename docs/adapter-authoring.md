# Adapter authoring contract

Every adapter must have two entries in `raglex.adapters.registry`:

1. one lazy, side-effect-free factory in `ADAPTERS`; and
2. one `SourceInfo` row in `SOURCE_INFO`.

`SourceInfo` is the sole public naming and capability schema used by the REST API,
MCP, Backfill, and Keep Current screens. Do not create a screen-local label or
jurisdiction grouping. Use:

- a stable lowercase source key, normally `{jurisdiction}-{publisher-or-series}`;
- a human label led by the name a user will search for (`ACM guidance …`, not
  `Netherlands ACM …`);
- one canonical `kind`: `legislation`, `caselaw`, `administrative`, `guidance`,
  `preparatory`, or `scrape`;
- an ISO-like jurisdiction code already declared in `JURISDICTION_LABELS`;
- explicit `SourceOption` fields for every adapter keyword argument a user may set;
- identifier examples for targeted fetching; and
- a truthful incremental mode in `INCREMENTAL_MODE`.

`source_catalog()` adds `group_key`, `group_label`, `kind_label`, `sort_key`, and
capability flags. All API clients must group by those returned fields and must not
maintain their own country-name table.

An adapter should also test that its key is present in `source_catalog()`, that its
incremental mode is correct, and that each option name is accepted by its constructor.

Regulator determinations, sanctions, enforcement notices, and DPA deliberations are
`administrative`, not case law and not guidance. A mixed archive such as DILA may keep
one storage source for courts, legislation, and CNIL; in that case the document-level
body/court discriminator must also be added to `Facade._ADMIN_COURTS` and mirrored by
`_kind_clause`. Test both the Python display kind and the SQL-filtered slice so a facet
cannot label CNIL correctly while still returning it under “cases”.

Guidance that is explicitly about one governing instrument should store:

```json
{"citation_default_instrument": {"id": "32024R1689", "kind": "regulation"}}
```

This lets orphaned provisions later in the document (“Article 50(2)”) return to the
guidance's declared subject after a sentence discussing another law. Only set it when the
title, register, or source metadata identifies exactly one instrument. Mixed-regime
registers must omit it. A title that itself deterministically cites exactly one law is also
recognised for records imported before this field existed, but new adapters should write
the field explicitly and test both the declaration and a later orphaned provision.

Any adapter used by a background harvest must also follow
[`job-authoring.md`](job-authoring.md). In particular, discovery must stop at a
newest-first cursor, expose a durable page/offset cursor where possible, and avoid
silently paging for minutes without yielding an item. The shared pipeline reports
fetch/store progress for each yielded `Stub`; accurate `Stub.hints.feed_total` and
`resume_offset` make that progress determinate and resumable.

## Getting the page, and getting all of it

Reach for the browser tier only when a plain request actually fails, and check what the
browser gives back. Two sources here fail in opposite directions:

- The ISC's `/reports/` page **loses 211 of its 215 PDFs when rendered**, because its
  per-Parliament accordions are collapsed and dropped from the rendered body. As markup
  it is complete. It refuses a bare client on headers alone, so a browser `User-Agent`
  *with* `Accept`/`Accept-Language` is the whole fix — the browser tier would be a
  downgrade.
- The Library RSS feeds are behind Cloudflare and need the browser, but a browser handed
  an RSS URL **parses it as HTML**, where `<link>` is a void element: every item's link
  swallows the rest of the item. Take the navigation response's *bytes*
  (`BrowserBytesFetcher.fetch_bytes`) and parse real XML.

Blocks of repeated, identically-nested markup must be found by **splitting on their
opening marker**, never by matching a closing one. Every terminator that works for the
blocks in the middle has nothing to stop on for the last, which silently costs the page
its final record — 106 of 107 on the ISC page, and the same bug once cost every Think
Tank document its geographical areas.

Classify a heading by **which element it was**, not by what its text looks like. The
ISC's Transcripts accordions are single years ("2015"); read as section headings rather
than as periods, the previous Parliament stayed in force beneath them and sixty
transcripts were filed under a Parliament that ended in 1997.

A `SourceOption` arrives as whatever the form sent, including `None` for anything the
user never touched. `bool(None)` is `False`, which turns a default-on option off for
everybody who left it alone; use `core.adapter.option_flag` / `option_int`, which treat
blank as "the default".

A search whose date filter is a fixed set of presets may bind the range **server-side**
to an opaque key. The Scottish Parliament's does: the same key carrying different dates
returns zero, `dtDateFrom`/`dtDateTo` alone are accepted and ignored (returning an
unfiltered set that looks filtered), and the archive-wide preset's value changes daily
because its label ends at today. Read the option list off the form on every run.

## Two jurisdictions, one citation

Some legal systems share a language and a notation with a neighbour and mean different
statutes by the same words. Austria and Germany are the worst case in the corpus: `KSchG`
is consumer protection in Vienna and dismissal protection in Berlin, `MSchG` is trade
marks in one and maternity leave in the other, and the `ABGB` has no German counterpart
at all. No pattern can separate them, because the text is identical.

Where that happens, the grammar must **not** choose. Produce both readings, name the
domestic methods in a per-jurisdiction set (`AUSTRIAN_DOMESTIC_METHODS`, `SLOVAK_METHODS`,
…), and let `stage._gate_national_grammars` keep the one belonging to the citing
document's own system, read from its source key, its identifier or its ECLI.

Two rules make that work and both have already been got wrong once:

- The dedupe must let parallel readings of the same span survive
  (`extractor.NATIONAL_PARALLEL_METHODS`). Without it the overlap dedupe picks by list
  order and the gate then deletes the survivor — the citation is *lost*, not merely
  mis-attributed.
- The **EU** reading is right in every document and must be excluded from the domestic
  set. An Austrian court citing Article 6 GDPR means the same instrument a Finnish one
  does; only the domestic candidate is jurisdiction-bound.

A bare acronym for an EU instrument needs a determiner or a context word **of its own
language** before it counts (`de_laws._NEEDS_DETERMINER_LEN`, and `_needs_context` in the
four newer modules). This pass runs over the whole corpus: "DSA" is a duty solicitor
advice scheme in an English judgment and "DMA" a French marketing syndicate.

## Scanned PDFs

`raglex.extraction.ocr` is the OCR tier: `text_or_ocr` returns
`(text, needs_ocr, page_spans, engine)` and is what a PDF adapter should call instead of
`extract_bytes` directly. It escalates only when the born-digital parse came back empty,
and it **drops the page spans when it does** — they described the parse that was just
replaced, so keeping them anchors citations at offsets that no longer exist.

`needs_ocr` must mean "this could not be read", never "this was a scan". A successful OCR
pass is not a review item; a missing OCR stack is. The ISC's 1995 annual report extracts
zero characters born-digital and OCRs to real prose, and must enter the corpus as text.

## Provision lineage

Do not use citation aliases to say that two statutory provisions perform a similar
function. Citation aliases rewrite textual references; provision lineage is an
editorial assertion linking two distinct provisions while preserving both laws.

Create lineage through `upsert_provision_mappings` (MCP) or
`POST /provision-mappings` (REST). The direction is always:

`current law/current anchor -> previous law/previous anchor`.

Each mapping records provenance (`manual`, `llm`, or `structured`), optional confidence,
and an explanation. Functional lineage causes citations to the previous provision to
surface separately beside the current provision. It never changes the literal citation
edge and never claims that the citing author mentioned the newer law.

## Structured archives and legislation versions

Never assume that the largest XML member of a structured archive is the whole
document. Publication packages commonly split schedules/annexes, tables, or later
parts into sibling members. A format parser must:

- exclude bibliographic/manifest members explicitly;
- parse every ordered content member;
- preserve annexes/schedules as named, citable structural segments; and
- include a regression fixture in which the annex is not in the largest member.

Do not model a consolidated text as an overwrite of the base act. Store each dated
expression under its own stable identifier and add a typed link to the base instrument.
Keep all dates, including future-effective expressions; the currency layer decides which
is latest *applicable today* and separately reports a newer future snapshot. An adapter
with an enumerable version series should expose a resumable full-series mode rather than
forcing one lookup per base act. For EU law this is the complete Cellar sector-0 sweep
(`consolidations_only=true`); targeted sector-3 imports should use
`include_consolidations=true`. A reverse sector-0 discovery must also yield each distinct
sector-3 base once: the dated expression omits its preamble, so version discovery is also
the efficient enumeration key for harvesting the recital source.

The read model is also part of this contract. When a base act has a dated consolidation
applicable today, ordinary web and MCP reads default to that consolidation and expose an
explicit route back to the original text. A consolidation inherits literal mention edges
to its base act with the same provision anchor, while preserving direct citations to the
dated expression. Never rewrite the literal edge: project it at read/export time and mark
it as version-inherited. This lets unchanged Articles share their citers and lets a newly
inserted Article 1a surface on the first version that actually contains it.

EU consolidated expressions commonly omit the original preamble. Do not copy recitals
into every stored expression and do not manufacture duplicate citation rows. Preserve
base-act recitals as `recital` segments; the shared read model projects them as a
provenance-marked virtual section on every consolidation that lacks recital segments.
Reader, MCP provision lookup and static export must all use that projection. Incoming
`Recital N` mentions follow the same version-inheritance rule as Article mentions, while
outgoing links printed inside a recital are read live from the base act.
If a legacy base rendition lacks structured recital segments, the reader may temporarily
project them from the earliest held expression with a structured preamble (recitals are
unchanged), but it must label that provenance and the sweep must refresh the sector-3
Formex rather than treating the fallback as permanent.

Apply lineage in both directions. When a consolidated act cites a third instrument,
do not show the base act and every dated snapshot as separate citers of that third
instrument. Keep every per-version relation as evidence, but collapse the citing family
at read/export time, preferring the latest applicable member that actually carries the
citation. Version discovery must not inflate user-facing mention counts merely by
reproducing unchanged text.

## One source, one structure — not one source, one parser

A publisher's markup family is not a document type. CELLAR serves EU acts, Court
judgments *and* Parliament resolutions as Formex, and the three have nothing structural
in common: a resolution has no `ENACTING.TERMS` and no `ARTICLE`, so the act parser kept
only its `ANNEX` and dropped the preamble, thirty-five recitals and sixty-eight operative
paragraphs. Register a new format when the *structure* differs, and have the adapter pick
it; do not widen an existing parser until it recognises two kinds of document.

Where the same text is published in two markups by two services, classify on the feature
that is stable across both. Parliament resolutions arrive as SDOCTA from the Parliament
and as Formex `<GENERAL>` from the Official Journal, and neither distinguishes a recital
from an operative paragraph by element — the 2017 drafting put lettered recitals in
`<ACTION>` under `<DISPOSITIF>`, the 2025 drafting puts them in `<CONS>` under `<GRCONS>`.
The **marker** (`–` / `A.` / `1.`) has meant the same thing in every year sampled, so both
parsers read that and produce identical segments from unrelated XML. Test the two against
one another: agreeing on 68 paragraphs and 35 recitals from independent sources is a
stronger check than either fixture alone.

Formex amendment quotations require the same structural care as annexes. Replacement
text for another instrument may contain genuine nested `<ARTICLE>` elements under
`<QUOT.S>`; retain that prose inside the outer amending Article, but never promote those
nested headings into the current act's provision index. Add a regression fixture whenever
a structured source embeds one law inside another.
