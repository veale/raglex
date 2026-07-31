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
