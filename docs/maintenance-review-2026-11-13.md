# Live maintenance review — 13 November 2026

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
- HUDOC's tested routes still return empty/aborted responses or invalid JSON even with
  the restored scraper. Retry results must retain this distinction from infrastructure failure.
- AI Act Article 10(5): the official stored Formex itself contains it. Do not delete it
  based on a feedback assertion; reconcile the publisher consolidation and amending act.
- Wider national full-text coverage, Austrian headnote-parent relationships, complete
  French per-article amendment provenance/citation formatting, and historical UK acquisition
  remain separate coverage work.
- RDI stale detail 404, BIPT detail 500, Belgian recent-month discovery, publisher
  anti-bot failures, and no-new-document warnings require source-specific investigation;
  an empty watch is not automatically evidence of a parser failure.
- PostgreSQL watch deadlocks need reproduction and complete-transaction retry/consistent
  update ordering. A failed transaction must not be labelled as successful coverage.
- Large shorthand cleanup was reviewed in dry-run only; invalid and document-local rules
  must not be bulk-deleted indiscriminately.

## Validation and rollback

Full suite: **3,082 passed, 112 skipped**, no failures. Frontend build and all five
frontend tests pass. Resume-contract and targeted adapter/retrieval/admin tests pass.
`git diff --check` is clean. Live API/UI verified through Asahi Scrapling.

Current image: `raglex:feedback-20261113-recovery-ui`; both services use `init: true`.
Rollback image retained as `raglex:before-feedback-20261113`; deployment environment and
compose backups are retained beside the live compose file. The PostgreSQL service and
unrelated services were not restarted during RagLex deployment.
