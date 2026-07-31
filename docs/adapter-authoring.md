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
- one canonical `kind`: `legislation`, `caselaw`, `guidance`, `preparatory`, or
  `scrape`;
- an ISO-like jurisdiction code already declared in `JURISDICTION_LABELS`;
- explicit `SourceOption` fields for every adapter keyword argument a user may set;
- identifier examples for targeted fetching; and
- a truthful incremental mode in `INCREMENTAL_MODE`.

`source_catalog()` adds `group_key`, `group_label`, `kind_label`, `sort_key`, and
capability flags. All API clients must group by those returned fields and must not
maintain their own country-name table.

An adapter should also test that its key is present in `source_catalog()`, that its
incremental mode is correct, and that each option name is accepted by its constructor.

Any adapter used by a background harvest must also follow
[`job-authoring.md`](job-authoring.md). In particular, discovery must stop at a
newest-first cursor, expose a durable page/offset cursor where possible, and avoid
silently paging for minutes without yielding an item. The shared pipeline reports
fetch/store progress for each yielded `Stub`; accurate `Stub.hints.feed_total` and
`resume_offset` make that progress determinate and resumable.

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
`include_consolidations=true`.

The read model is also part of this contract. When a base act has a dated consolidation
applicable today, ordinary web and MCP reads default to that consolidation and expose an
explicit route back to the original text. A consolidation inherits literal mention edges
to its base act with the same provision anchor, while preserving direct citations to the
dated expression. Never rewrite the literal edge: project it at read/export time and mark
it as version-inherited. This lets unchanged Articles share their citers and lets a newly
inserted Article 1a surface on the first version that actually contains it.

Formex amendment quotations require the same structural care as annexes. Replacement
text for another instrument may contain genuine nested `<ARTICLE>` elements under
`<QUOT.S>`; retain that prose inside the outer amending Article, but never promote those
nested headings into the current act's provision index. Add a regression fixture whenever
a structured source embeds one law inside another.
