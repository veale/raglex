# Domsdatabasen (Denmark)

`dk-domsdatabasen` harvests the Danish courts' official KAPI v2. This is a
selected publication register, not an inventory of all Danish judgments.
The access grant supplied locally lasts from 22 October 2026 to 22 October 2031.
Credentials are operator settings, never source options or committed files:
`DOMSDATABASEN_EMAIL` and `DOMSDATABASEN_PASSWORD` (Settings → Sources).

The technical integration guide and access letter are in the ignored local
`.scratch/Domsdatabasen/` directory. Do not copy their credentials into a fixture,
log, job parameter, or image build context.

## Initial import and updates

Start `harvest-source` for `dk-domsdatabasen` with `backfill=true`, no page cap,
and `verify_inventory=true`. Follow the durable job in the Jobs panel. It runs the
independent inventory audit after harvesting and enables/creates the daily watch
only when verification succeeds. Harvest errors or unreconciled inventory differences
leave the job in error and the pending watch disabled. A bounded pilot does **not** establish completeness;
do not enable an incremental watch after only a pilot.

The adapter authenticates with `POST /webapi/kapi/v2/autoriser`, then walks
`GET /sager?sideNr=N&perSide=25` until an empty array. Non-array/error bodies,
repeated pages, malformed identities, empty live documents, and invalid PDF
payloads raise errors. A short nonempty page is not treated as the end.
Tokens are renewed before their five-hour expiry and once after a 401.

A checkpoint counts **cases**, not documents. Every sibling shares its case's
position. `start_offset` backs off a case page through `resume_floor`; a restart
re-covers siblings rather than dropping the tail of an interrupted case.

The initial scan's start date is the watermark. An incremental run uses
`/sagsaendringer?fraDato=YYYY-MM-DD`, inclusive, and hashes the returned envelope
to notice revisions. It never stops by decision date: the live register includes
judgments published years after they were decided.

## Identity, provenance, and withdrawals

Each document is `dk/domsdatabasen/{case_id}/{document_id}`, with the source's
ECLI registered as an alias where supplied. Judgments and separate summaries
retain different identities and types; summaries do not claim the judgment's
docket alias. Raw JSON envelopes retain the source metadata and embedded HTML or
base64 PDF. Court, decision date, case subjects, document type, appeal groups,
and publisher bookmarks are preserved. HTML entities are decoded and inline
Word spans remain inside their paragraphs.

Explicit document/case withdrawals mark an already held document `withdrawn`
before held-item deduplication. They never re-ingest a withdrawn body. The
append-only archive remains available with its status. A withdrawn case whose document
identities have been removed emits a case-level tombstone: all held siblings under
that exact source/case prefix are marked withdrawn, and the job records the observation.
This does not invent a document or block the rest of the catalogue.

## Completeness audit

The guide says KAPI and the public portal expose the same published cases. This
is a claim to reconcile, not a manifest. Compare the imported distinct case IDs
with an independent walk of the public portal's case search, classify differences
(including withdrawals), and record the date and both counts before claiming the
initial corpus complete. This audit remains required even when two KAPI walks
agree. A completed harvesting job alone proves only that its API feed was exhausted.

The independent comparison is available as the `audit-domsdatabasen` job,
started through `POST /api/jobs/audit-domsdatabasen`. It returns missing and
held-only document identities as well as case/document counts. The public
portal initially reported 8,841 results on 13 November 2026. The initial bounded
pilot imported 37 documents from 25 cases without errors; this pilot is not a
completeness claim.


Scanned PDFs use Danish/English Tesseract OCR in the background harvest job. The
Docker image includes the Danish language pack. Scans over the configured page ceiling raise an explicit review error rather than
publishing a truncated judgment. The default is 80; an explicit targeted background
repair may set `max_ocr_pages` up to 500. Image-only KAPI HTML falls back to the
publisher's public PDF download for OCR.

For reconciled missing cases, `options.case_ids` accepts comma-separated numeric
publisher case IDs and uses the public case/document/download routes. This sparse
worklist retains the same document identities, cannot advance the main feed watermark,
and automatically reruns the independent audit afterwards. It cannot enable the daily
watch; `verify_inventory` still requires the full KAPI walk. Recovery does not substitute
for the authenticated feed or silently declare coverage.

The public audit uses ascending publication order; judgment-date ordering produced
repeated cases at page boundaries. It respects the advertised page count, records
unique cases and case occurrences, and reports any difference from the publisher's
advertised total. The latest completed audit (13 November, reviewed 14 November 2026) enumerated
8,841 cases and 10,844 public documents against 8,842 advertised results. It found
10,832 held readable documents, 12 missing and no held-only documents. The remaining
one-result count difference requires classification; an earlier walk's difference of
40 was not stable. None of these differences authorises deleting documents or declaring
complete coverage. The subsequent targeted recovery and its automatic audit remain
visible in Jobs; the daily watch stays disabled pending successful full verification.

## Access window and recurring full acquisition

All new requests to `domsdatabasen.dk`, including authentication, public-PDF fallbacks,
redirects and independent inventory requests, are restricted to **01:00 inclusive until
06:00 exclusive in `Europe/Copenhagen`**. This uses the IANA timezone, not a fixed UTC
hour, and follows Danish summer/winter time. Requests already in flight may complete;
no subsequent request or retry is sent after the window closes. Local parsing, OCR and
citation extraction do not contact the publisher and can continue outside this window.

The supplied technical guide specifies a maximum of 25 cases per KAPI page and five-hour
tokens. No numerical requests-per-second limit or clock restriction was found in the
supplied guide/access letter. The adapter and audit use a conservative one-second minimum
interval between requests per client and the shared Retry-After/backoff handling. The
nighttime restriction is the operator's explicit instruction.

`danish-acquisition` is an opt-in scheduler task, enabled on this instance. During the
window it queues one full KAPI acquisition per Danish calendar night while the daily
watch remains disabled. The completed inventory audit gates enabling that watch. A
verified full acquisition bypasses any old backfill frontier so it does not accidentally
walk only `/sagsaendringer`. Once verification succeeds, the enabled watch handles daily
inclusive changes within the same enforced access window.

Queue admission prevents full acquisitions, audits and Danish watches from starting
outside the window. A request-time check also stops a running walk at the boundary: its
job returns to `queued`, retaining the checkpoint and unfinished extraction work. It does
not spend a retry attempt or hold a worker slot until the next night. The queue's promotion
loop rechecks the window; a restart or manual start cannot bypass it.

On 14 November 2026, full verified acquisition **`af13d1b8`** was queued for the next
permitted window. Local citation rescan **`efb12de0`** applies the expanded grammar to
already held Danish documents. The preceding recovery's audit found **10,844 readable
held documents, zero missing and zero held-only documents** against the enumerated public
inventory. Its advertised-total difference of one remains visible; coverage verification
has not been declared complete.

## Danish citation grammar

The production extraction pipeline includes Danish numbered EU regulations, directives
and decisions, preserving `artikel`, `stk.`, `litra` and `nr.` as canonical EU Article
anchors. It uses the shared CELEX year/number checks, including older number/year forms and numbered instruments followed by their Danish adoption date.
CJEU case numbers and ECLIs are recognised by the shared grammar; Danish `præmis`,
paragraph lists joined by `og`, and ranges joined by `til` now attach case pinpoints.
`TEU`/`TEUF` and their article references are recognised too.

Explicit definitions such as `(herefter AI-forordningen)`, `(i det følgende …)` and
`(efterfølgende benævnt …)` bind later Danish references to the named authority. Generic
`forordningen`, `direktivet` and `loven` bindings stay document-local and never become
corpus-wide shorthand rules. Case short names need a paragraph pinpoint. Bare EU articles
carry forward from the preceding instrument and retain Danish subdivisions.

Existing named data-protection-act provisions are retained. Domestic docket forms resolve
through the adapter's docket aliases. UfR references are recognised as report candidates;
recognition alone does not establish a report-to-judgment alias or acquire a report that
is not held. This is not complete Danish statute-name coverage or universal equivalence
to the mature UK grammars.

Regression tests cover the production extractor, exact offsets, year ambiguity, authority
switches, shorthand leakage, DST boundaries, queue deferral, checkpoints, retries and
redirects. A deployed MCP detection check recognised the Danish GDPR reference,
`C-311/18` and the AI Act as routable authorities already held by RagLex.
