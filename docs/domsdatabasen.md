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

Start `harvest-source` for `dk-domsdatabasen` with `backfill=true` and no page cap.
Follow the durable job in the Jobs panel. After that initial walk completes,
create a daily source watch. A bounded pilot does **not** establish completeness;
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
append-only archive remains available with its status. A withdrawn case with no
document identities raises for operator review rather than silently pretending
its earlier documents were reconciled.

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
portal initially reported 8,841 cases on 13 November 2026. The initial bounded
pilot imported 37 documents from 25 cases without errors; this pilot is not a
completeness claim.
