# Live maintenance review — 13–14 November 2026

The changes are deployed to the local RagLex API and scheduler. Scraper checks and UI
verification use the connected Scrapling service on Asahi. Credentials and source access
letters remain outside the repository and Docker build context.

## Completed repairs

- Added authenticated Danish Domsdatabasen KAPI v2 harvesting: distinct judgments and
  summaries, ECLI aliases, HTML/PDF extraction, inclusive revision/withdrawal feed,
  safe case-based resume cursors, and explicit case-level withdrawal tombstones.
  Added a separate reconciliation job against the public portal inventory. The initial
  public inventory reports 8,841 cases; the 25-case pilot stored 37 documents without errors.
- Added **Admin → Maintain → Remove or refresh a case** with citation preview,
  force-refetch and confirmed purge. Purge is atomic and creates no suppression list.
  Verified live force-refetch of `ewhc/kb/2026/2776`; purged
  `ukftt/tc/2026/1407` and verified it is no longer held. Independent PostgreSQL tests
  verify dependent-row deletion, preservation of incoming citation evidence, and reimport.
- Reparsed all 234 CPR documents and reanchored 536 offsets. Verified rule 27.3 and
  PD27A paragraph 7.3 retain their operative text and list continuations.
- Refetched the DPA 2018 and verified section 164A. Rebuilt the two reported French
  parent instruments and verified their reported articles. Added targeted JORF fetching
  and retained available publisher publication/PDF metadata separately from consolidations.
- Fixed bare/bracketed paragraph labels, paragraph ranges, literal in-document boolean
  words, bounded text windows, false whole-body provision focus, and optional compact
  citer output. Added text-availability and original-EU-act currency information.
- Reject EU publisher error/challenge pages and reports for the hearing as judgment
  bodies. URL imports now recognise PDFs by Content-Type/magic and reject empty imports.
- Preserved CanLII 404 status instead of logging every missing case as a generic failure.
  Fixed the database index-health SQL and an expensive duplicate-producing probe join.
- Added persisted in-app feedback responses. Replied to reported bugs with verification
  and explicit remaining gaps; resolved only verified complete reports.
- Added scraper failure cooldowns and fail-fast MCP handling, process init for both
  containers, truthful free-text coverage loading, and guards against overlapping UI polls.
- A completed bounded FTS build scanned 3,640 documents, indexed 3,602, and wrote
  10,661 parts across CPR, French legislation, EU preparatory material and Danish pilot data.

## Scraper outage and feed recovery

The Asahi scraper was stopped because its requested Docker address was already occupied
by RSSBox. Reconnected only the scraper with an available address, preserved its other
network attachments, and started it successfully. Replaced RagLex's stale backend key
with the current one in persistent settings and deployment configuration. Verified both
service health and a real rendered CPR fetch of 65,175 HTML characters.

RSSBox itself has zero configured feeds and zero stored feed failures. FreshRSS had a
separate local-address allowlist failure. Added only the configured host/port pairs for
RSS Bridge, Morss, ScholRSS, Autofeed Sidecar and ChangeDetection. Saved a config backup.
A checkpointed Asahi systemd task retried all 152 failed feeds: 138 CLI refreshes succeeded;
14 remained publisher/content/rate-limit failures. No feeds were deleted or SSL checks disabled.
Remaining examples include expired TLS, publisher 404/403, malformed XML, and Retry-After.
The private per-feed manifest and results are retained on Asahi under
`/home/mv/raglex-outage-recovery-20261113/`.

RagLex outage recovery uses a new explicit `references` list on
`POST /api/jobs/harvest-all`. It bypasses unrelated global ranking, deduplicates the
recorded worklist and supports `retry_cooled`. Queued 60 distinct recorded transient
failures and bounded source retries for CELLAR, Bundestag WD, HUDOC, Irish DPC, BIPT,
Belgian Council of State and RDI. Genuine publisher 404s were not blanket-cleared.

## Durable jobs and remaining verification

- Danish full backfill: original `f5831217` stopped visibly on a case-level withdrawal;
  fixed the handling and resumed as `f2d7bc28`. The resume backs off a page and deduplicates.
- Reader refinement repair: exact 38-document worklist, `8764b454`, resumed as
  `96b0e968` after a statement timeout. Bounded rescans now resolve only reviewed
  documents rather than launching whole-corpus graph resolution.
- Recorded transient reference retries: `fbcc19a4`.
- Source retry jobs: CELLAR `75f6c403`, Bundestag `4749f908`, HUDOC `b8f74c02`,
  Irish DPC `6de63b2e`, BIPT `588ba59d`, Belgian Council of State `d7480011`,
  RDI `87058835`.

These are progress-tracked jobs, not a claim that every retry recovered a document.
Danish completeness must be checked by the independent inventory audit after full import.

## Open findings

- REACH, Charter and Casa Fleisch text recovery: tested publisher routes still return
  missing/error/challenge content. Invalid stored text is blocked; readable authoritative
  replacements remain needed. Casa Fleisch's report for the hearing is not the judgment.
- Some old Commission decisions still have no usable conversion, and 13 references
  from the recorded outage worklist remain uncertain/empty after recovery. Do not
  turn these publisher-level gaps into long-lived infrastructure cooldowns.
- AI Act Article 10(5): the official stored Formex itself contains it. Do not delete it
  based on a feedback assertion; reconcile the publisher consolidation and amending act.
- Wider national full-text coverage, Austrian headnote-parent relationships, complete
  French per-article amendment provenance/citation formatting, and historical UK acquisition
  remain separate coverage work.
- RDI stale detail 404, an unavailable BIPT dossier, publisher anti-bot failures,
  and no-new-document warnings require source-specific investigation;
  an empty watch is not automatically evidence of a parser failure.
- PostgreSQL watch deadlocks need reproduction and complete-transaction retry/consistent
  update ordering. A failed transaction must not be labelled as successful coverage.
- Large shorthand cleanup was reviewed in dry-run only; invalid and document-local rules
  must not be bulk-deleted indiscriminately.

## Validation and rollback

Full suite and the final targeted recovery checks pass; see final verification below. Frontend build and all five
frontend tests pass. Resume-contract and targeted adapter/retrieval/admin tests pass.
`git diff --check` is clean. Live API/UI verified through Asahi Scrapling.

Current image: `raglex:maintenance-20261114-delivery`; both services use `init: true`.
Rollback image retained as `raglex:before-feedback-20261113`; deployment environment and
compose backups are retained beside the live compose file. The PostgreSQL service and
unrelated services were not restarted during RagLex deployment.


## Follow-through and additional repairs

The original work continued after scraper connectivity was restored:

- Fixed HUDOC JSON entity corruption in the browser transport and excluded English
  placeholder records. Verified live force-refetch of Benghezal under its ECLI:
  39,444 characters of French judgment text. Of the 60 recorded transient reference
  failures, 44 now succeed/are held, three are absent, and 13 remain uncertain/empty.
- Recovered five CELLAR documents and 13 Bundestag WD documents in bounded source runs.
  Irish DPC and RDI bounded runs completed. Repaired the Belgian Council of State's
  migrated date-sorted register, tested a real 113,506-character judgment, and queued
  the rolling-window recovery. Its new register is ordered by decision date; a wider
  inventory reconciliation is still needed for late publication of old decisions.
- Recognised BIPT's new `/sectors/topic/` dossier path, preserving its existing
  unavailable-dossier handling. The bounded retry completed without aborting the
  remaining register; one broken dossier remains explicitly logged.
- Completed the exact 38-document reader-refinement rescan: 19,934 citations extracted,
  5,322 edges resolved. Individual refinement flags remain open until their actual
  reader behaviour has been checked; re-extraction alone does not prove every flag fixed.
- Re-ingested the two broken PDF guidance records under their original IDs:
  `user:guidance:7e131a0fd24ab2cf` (105,957 characters) and
  `user:guidance:298bf8677884b333` (422,516 characters). Imported the English GPAI
  guidance (99,610 characters) and AI-system-definition annex (36,093 characters),
  verified their PDF headers, and removed the SHA-256-of-empty legacy import.
  Official download inventories: [GPAI guidance](https://digital-strategy.ec.europa.eu/en/library/guidelines-scope-obligations-providers-general-purpose-ai-models-under-ai-act)
  and [AI-system-definition guidance](https://digital-strategy.ec.europa.eu/en/library/commission-publishes-guidelines-ai-system-definition-facilitate-first-ai-acts-rules-application).
- Fixed URL encoding in CELLAR's OJ-item PDF fallback. The official Blue Guide 2022
  PDF at `52022XC0629(04)` yields 635,793 characters; unencoded item parentheses
  previously left this route as metadata only.
- Added the digital-strategy annex sweep to ordinary harvests. Previously held covers
  no longer prevent their advertised English PDF annexes being fetched as separate
  linked records. Explicit non-PDF assets remain in parent metadata. Added this source
  to text-search scope and indexed its 263 existing records (447 parts); new harvests
  extend the index automatically.
- Returned error payloads and nonzero harvest-error counts now produce job status
  `error`, preserving the result, instead of looking like successfully finished backfills.
- Confirmed older Danish failures are scanned PDFs; Danish OCR recovered 10,247
  characters from a real example. The full recovery now includes OCR and automatically
  runs the independent inventory audit afterwards. Watch 150 remains disabled until
  that verification succeeds. The bootstrap root is `0e4cfeae`, resumed as
  `9ef76f5e` after deployment; Jobs records the complete lineage and checkpoints.
- The independent public inventory itself needs reconciliation: publication-order
  the latest completed audit enumerated 8,841 cases and 10,844 public documents against
  8,842 advertised results, with 10,832 readable held documents, 12 missing and no
  held-only documents. The earlier 40-result discrepancy was not stable. The remaining
  one-result discrepancy and missing documents keep verification incomplete.
- Deep corpus-integrity probes timed out; no matching query was left running. Database
  health diagnostics succeeded: approximately 71 GB, 83.4% cache-hit ratio, no reported
  table bloat. Review memory/cache sizing and repeated whole-graph work before dropping
  rarely used indexes; low scan counts alone are not proof an index is unnecessary.

Final verification jobs: annex sweep `de255cb2`, encoded Blue Guide refresh `f0cd824d`.
Full Danish OCR/import is a long-running durable job; its automatic audit and watch gate
make unfinished recovery explicit rather than leaving a manual follow-up unrecorded.


## Overnight results and final delivery — 14 November

- Danish bootstrap `9ef76f5e` correctly finished in **error**, not success: six extraction
  failures included three image-only HTML documents and complete scans of 211, 408 and
  408 pages. Twelve public document identities were still missing overall. Added the
  authoritative public-PDF fallback, selected-case recovery and an explicit OCR ceiling
  up to 500 pages. Targeted recovery root `5c96b211` stored nine records before reaching
  the large scans; deployment retries retain the lineage and back off safely. The final
  deployed attempt is `dde4acd6`. It runs
  the full independent audit after recovery. Selected recovery never advances the main
  feed watermark or enables watch 150. Long OCR remains durable work in Jobs, not a
  claim that the corpus is complete.
- Wider outage recovery `7a256d8d` examined 344 recorded references: 214 succeeded/were
  already held, 117 remained retryable and 13 absent. Its historical status was `done`;
  the deployed job wrapper now treats a nonzero `retry_later` count as **error**, while
  preserving the result and individual outcomes. Publisher failures were not erased.
- Annex sweep `de255cb2` stored 649 attachments with 80 explicit failures. Bounded
  follow-up identified 49 non-PDF assets (DOCX/XLSX/ZIP/CSV/PNG), two PDFs, four HTML
  responses and one 404; 24 requests were rate-limited before classification. These are
  not 80 missing PDFs, and are not all recovered. Non-PDF extraction/asset handling and
  paced retries remain required; do not retry the whole list aggressively.
- Corrected nine attachment language labels that bypassed the English filter because
  the publisher used Gaelic/Slovene, misspellings or extra punctuation. Added a regression
  test for these labels. Retained those valid documents with corrected language metadata.
- Blue Guide refresh `330834fc` completed with zero errors: 635,793 characters and
  **152 page anchors**. PDF fallback now replaces stale header segments, and same-byte
  forced refreshes update stored text/segments as well as metadata. Reapplied its curated
  guidance classification/title. Linked the three substantive English AI guidelines to
  their cover records, including imports deduplicated by landing URL.
- Updated feedback #591, #597 and #605 as resolved after their concrete repairs and
  verification. Updated aggregate HUDOC reports #543/#544/#620 with the recovered
  Benghezal example and remaining failures; kept them open. Updated #271 with the
  Bundestag recovery and remaining parliamentary identity work.
- Verified the deployed UI returns a rendered sign-in page over Asahi Scrapling and the
  API health endpoint responds successfully. No Scrapling sessions were left open.

Remaining work is explicit: the Danish recovery/audit, publisher-specific reference
failures, the 14 remaining FreshRSS failures, non-PDF annex handling, and the feature/
coverage issues listed above. A completed repair or a functioning browser is not proof
that every upstream document is obtainable.

## Next fixes for remaining failures

| Finding | Concrete next action |
| --- | --- |
| Danish one-result discrepancy | Repeat the independent inventory with a fixed publication cutoff; compare identities rather than totals, and inspect withdrawn/grouped results before enabling the watch. |
| Large Danish scans | Let the queued complete-scan OCR finish, inspect its automatic audit and extraction errors, then run a full verified KAPI pass before enabling daily changes. |
| Non-PDF annexes | Add format-aware DOCX extraction, preserve spreadsheet tables with explicit structure, and treat ZIP/image assets as assets requiring a separate extraction path. Never store their bytes as PDF text. |
| Annex rate limits | Retry only unclassified/unavailable PDF candidates with publisher pacing and Retry-After handling, after the current limit clears. |
| HUDOC failures | Group remaining results by application/item ID, distinguish metadata-only translations and absent conversions, and repair malformed identifiers before further network retries. |
| FreshRSS 404/410 or changed destinations | Verify each publisher's current canonical feed and update only confirmed replacements; a home-page redirect is not an RSS feed. |
| FreshRSS expired TLS / malformed XML | Publisher certificate renewal or a narrowly scoped feed conversion is needed; retain TLS verification and surface invalid XML. |
| PostgreSQL watch deadlocks | Reproduce the conflicting transactions, impose consistent row-lock ordering, then add bounded jittered retry around the complete rolled-back transaction. |
| Slow whole-graph work | Continue resolving scoped worklists; inspect query plans and memory/cache pressure before any index removal. |

Final verification: the full suite passed **3,099 tests, 112 skipped**. The final
Danish recovery/audit, job-status and attachment checks passed **59 tests** after
adding the automatic selected-case audit. Frontend build and all **5 frontend tests**
passed. Deployed API/scheduler use the delivery image with process init; source hashes
were checked against the checkout. `git diff --check` passed. No credentials are staged.

## Danish grammar and nighttime acquisition follow-up

Added and tested general Danish EU instrument references, Danish case paragraph cues,
TEU/TEUF articles, explicit shorthand definitions and Danish Article subdivisions in
carry-forward references. A local source-scoped citation rescan (`efb12de0`) applies the
new grammar to held Danish text; its resolution is bounded to that source's documents.

Enforced 01:00–06:00 `Europe/Copenhagen` at both queue and HTTP levels, with DST-aware
boundary, retry and redirect tests. At 06:00 a walk is deferred with its checkpoint,
without consuming a retry attempt or keeping a worker slot. Added/enabled the recurring
Danish bootstrap task and queued full verified acquisition `af13d1b8`; the old backfill
frontier cannot silently turn a verified full walk into a change-feed-only run.

The earlier targeted recovery completed with zero extraction errors and its independent
comparison now finds all **10,844 enumerated public documents readable**, with no missing
or held-only documents. The one-result advertised-total discrepancy remains unresolved;
the daily change watch stays gated pending successful verification. See the Danish source
documentation for the operational schedule and grammar coverage limitations.

The final grammar was also checked through live MCP against references in held Danish
judgments: Article 15(2) of Regulation (EEC) No 3821/85 and Regulation (EC) No 882 of
29 April 2004 now resolve to `31985R3821` and `32004R0882`. The initial local rescan
`c5642e72` was cancelled while still queued and replaced with `efb12de0` so it uses
this final grammar. The full acquisition `af13d1b8` remains queued outside the window;
the local rescan is running. The deployed image is
`raglex:danish-grammar-night-20261114`; deployed grammar/window/job source hashes match
the checkout.

Final Danish release validation: **3,144 passed, 112 skipped**, with no failures;
**79 focused checks passed** for extraction, window enforcement, jobs and source-scoped
re-extraction. The live MCP checks recognised both additional instrument forms from held
judgments. `git diff --check` passed. The full acquisition remains durably queued outside
the window; local re-extraction had examined 1,000 of 10,844 documents at the final check.
