# Live maintenance review — 13–14 November 2026

This is the current status of the feedback and outage review, replacing the earlier
chronological notes. Completed source repairs are distinguished from queued corpus
work and publisher gaps. Credentials, access letters and private recovery manifests
remain outside Git. Rendering and scraper checks use Scrapling on Asahi.

## Verified repairs

| Finding | Repair and live evidence |
| --- | --- |
| Scraper outage | Restored the Asahi container's network connection and current backend authentication. A real rendered CPR request returned 65,175 HTML characters. Added fail-fast handling, cooldowns and process init. RSSBox had no configured feeds or stored failures. |
| Citation purge / refresh | Admin citation preview, force-refetch and atomic purge are deployed. Verified refetch of `ewhc/kb/2026/2776` and purge of `ukftt/tc/2026/1407`. Purge creates no suppression list: a later republication can be imported normally. |
| Reader issues | Fixed bracketed paragraph labels/ranges, literal phrases containing and/or/not, bounded character windows, unsegmented provision focus and compact citer output. Reparsed 234 CPR documents and reanchored 536 offsets; verified rule 27.3 and PD27A 7.3. Recovered DPA 2018 section 164A. |
| REACH stub | Added official CELLAR PDF negotiation when structured renditions are unusable. The first retry exposed a misleading HTTP-200 OJ index; the final guard rejects it. Live recovery `34e88cdc` stored 965,351 characters, 141 articles and 17 annexes with zero errors. Article 31 was verified readable (5,273 characters). This is the original instrument, not a claim of current consolidated law. |
| Casa Fleisch hearing report | CELLAR's HTTP 300 manifest contains both the judgment and hearing report. Select the unambiguous judgment PDF and reject report/opinion items. Live recovery `f8db82e7` restored `ECLI:EU:C:1989:331`, date 13 July 1989 and party title, with zero errors. PDF reparsing retains judgment structure. Earlier-than-1989 dockets no longer acquire a fabricated C- prefix. |
| AI Act Article 10(5) | Verified Article 1(9)(b) of [Regulation (EU) 2026/1744](https://eur-lex.europa.eu/eli/reg/2026/1744/oj). The held 27 July consolidation contains the stale paragraph. An exact-payload-hash erratum now displays an explicit deletion/editorial correction, retains its authority and leaves the raw publisher bytes unchanged. Reparsed live; original SHA-256 remains `655c4c6c957d1484426e1eb859a4fc65ad58978c1edd4d3a936fee87e4bf153b`. No blanket paragraph deletion is applied to other editions. |
| Austrian missing full judgment | RIS defaults to headnotes and silently ignores the old text-valued Dokumenttyp parameter. Use documented `Dokumenttyp.SucheInRechtssaetzen` and `Dokumenttyp.SucheInEntscheidungstexten` flags. Targeted job `d2e38506` stored all 31 results for Ro 2025/04/0007, including `ECLI:AT:VWGH:2026:RO2025040007.J00`, with zero errors. Headnotes link to the publisher's parent judgment identifier; no guessed ECLI. Exact RIS-ID fetches validate returned identity instead of accepting ignored filters. |
| PostgreSQL watch deadlocks | Reproduced a genuine server deadlock in an isolated schema. The victim's complete transaction is rolled back and replayed, with bounded jitter and three attempts for SQLSTATE 40P01/40001. Verified final values were incremented once by each worker. Nested transactions and non-retryable failures propagate; consistent document/alias ordering reduces contention. This does not claim every slow query or future contention is eliminated. |
| Invalid shorthand store | Durable job `07215bde` scanned 1,246,151 unblocked rules and removed exactly 94,132 invalid entries. Preserved 1,071,258 valid document-local rules and all blocked rules. Cancellation reports committed deletions truthfully. Existing citation edges are not assumed corrected merely because housekeeping completed. |
| French provenance | Rematerialization `58c46000` rebuilt both reported LEGITEXT parents. DILA and API projections preserve per-article versions, validity dates and explicit publisher amendment/creation links. Live Article 31 exposes both versions and the 2014-344 Article 154 amendment. Provision responses expose relevant provenance. French citations use available JO date/issue/text number and genuine print page; a PDF page is never invented as a print citation. This is not a claim of complete Bluebook formatting or complete historical amendment coverage. |
| Non-PDF Commission annexes | Format detection now extracts DOCX, XLSX and CSV/TSV. Spreadsheet extraction retains sheet names, cell coordinates, formula/cache values and empty-column positions. ZIP/image/legacy binary files are retained as metadata assets with raw bytes, not misreported as readable legal text. Invalid HTML and malformed Office files remain failures. Held-parent attachment discovery continues; the new sweep is a durable job. |
| HTTP 200 / enumeration failures | Reject source error/challenge pages and reports for the hearing; preserve CanLII 404 classification. Failed EU SPARQL enumeration now raises instead of silently appearing empty. URL imports recognise PDF signatures and reject empty payloads. |
| Belgian and HUDOC examples | Recovered Benghezal in French (39,444 characters), Blue Guide `52022XC0629(04)` (635,793 characters, 152 page anchors), and a real Belgian Council of State judgment (113,506 characters). Updated the new Council register routes and BIPT dossier routes. An unavailable incidental BIPT dossier warns/skips rather than aborting the register; the dossier itself remains unavailable. |

## Danish acquisition and grammar

The authenticated KAPI v2 adapter supports judgments/summaries, ECLI aliases, inclusive
revision feeds, withdrawals, PDF/HTML extraction and safe case-based resume cursors.
The independent portal comparison now finds **10,844 enumerated public documents
readable**, across 8,841 cases, with no missing or held-only document identities.
The publisher advertises 8,842 cases: the one-result discrepancy is still open and
must not be hidden by enabling the change watch.

Danish grammar covers cross-references, EU instruments and cases, TEU/TEUF articles,
paragraph/subdivision cues, carry-forward references and explicit shorthand definitions.
Regression tests and live held-judgment examples cover `31985R3821` and `32004R0882`.
It is tested grammar coverage, not a guarantee of every possible Danish citation form.

All Danish acquisition HTTP sends, authentication, retries and redirects are restricted
to **01:00–06:00 Europe/Copenhagen**, including DST. At 06:00 jobs defer with their
checkpoint without retaining a worker slot or consuming an attempt. Local parsing and
OCR may run during the day. A nightly verified full acquisition is enabled; `af13d1b8`
is durably queued outside the window. Pacing, token renewal and Retry-After handling are
documented in [domsdatabasen.md](domsdatabasen.md).

The grammar rescan `efb12de0` completed extraction but failed during resolution on a
statement timeout. Fixed resumed scans so extraction checkpoints cannot omit unfinished
resolution. Its continuation `2ee32bba` is running in Jobs; it must not be described as a fully
completed graph refresh until that job finishes.

## Corpus work running or queued

| Work | Durable tracking / completion criterion |
| --- | --- |
| Austrian full decisions | Fresh backfills requesting decision texts: VwGH `c4694ba1`, BVwG `cb4bec34`, LVwG `ce3b1a17`. Start at zero with `force_full`; old headnote/frontier offsets cannot establish decision-text coverage. These are substantial backfills, not completed acquisitions. |
| National free-text expansion | `587fa9ab` indexes registered DE/FR/NL/BE/AT case-law and legislation plus legacy DILA. Search scope retains existing selected sources and explicitly says coverage is partial. Resumed as `3dbb8b62`; the redundant smaller-scope build was cancelled. Use live per-source indexed counts; do not infer absence from an unfinished index. |
| Annex format recovery | `e0484b15` stored 98 further items with format-aware extraction, leaving seven transient failures and one unavailable attachment. The job correctly reports error. Previous sweep recovered 649 attachments and retained 80 failures. ZIP/images are assets, not recovered readable documents. |
| AI Act citation refresh | `b9149946` completed: one document, 1,042 citations, 424 resolved edges after the correction. Raw authenticity and the corrected reader projection were checked separately. |
| Outage references | Preserved 344-reference worklist: 214 recovered/already held, 117 requiring retry and 13 absent/uncertain in the earlier completed run. Do not blanket-clear failure evidence or guess malformed ECLIs. |

## FreshRSS follow-through

The initial allowlist repair and durable worker retried 152 failed feeds; 138 succeeded.
The follow-through retried the remaining 14 and made these verified repairs:

- Statewatch: publisher's working `/feed/` replaces the old 404; imported 10 articles.
- ScholRSS AI and Ethics and AI & Society: restored missing journal configurations;
  verified both 50-item feeds and imported 50 articles each.
- Rest of World: narrowly scoped FreshRSS extension repairs bare XML ampersands outside
  CDATA, validates XML and preserves TLS verification. Imported 12 articles. Installable
  source and operational details are in [ops/freshrss-xml-repair](../ops/freshrss-xml-repair/README.md).
- EDPS: verified the native publisher RSS through the existing AutoFeed sidecar backend,
  persisted its configuration and verified ten Atom entries from FreshRSS's network.
  Imported 10 articles. Direct optional HTML/favicon requests still receive publisher 403.

No feeds were deleted and no certificate validation was disabled. Backups and replayable
manifests are retained privately on Asahi under `raglex-outage-recovery-20261114` and
service data directories. The follow-through worker completed; it is not an unmonitored
promise to retry forever.

The final live error list contains nine feeds, including a newly failing Kingsley Napley
feed: Afripoli Digital Agenda, Berkeley Technology Law Journal, Kingsley Napley data
protection, EDPB, Fairwork, Google TAG, KICTANet, Light Blue Touchpaper and Euractiv.
Observed causes include removed 404/410 routes, expired publisher TLS, 403/503 responses
and Euractiv's redirect to Rapporteur. A homepage redirect is not proof of an equivalent
feed. Verified canonical replacements or source-specific HTML feeds remain necessary;
changing topic scope or silently disabling these feeds would not repair them.

## Remaining findings and limits

| Finding | Evidence and required next step |
| --- | --- |
| Charter authoritative text | Tested official routes returned missing/challenge content; no reliable replacement was imported. Recover the correct edition from an official readable rendition. Do not substitute the 2000 Charter for the 2012 instrument or store a challenge page. |
| Old Commission / HUDOC conversions | Some records genuinely lack usable bodies. Group remaining failures by publisher identifier and distinguish absent conversions, translation placeholders, invalid identifiers and transport failures before retrying. Aggregate feedback stays open. |
| Historical UK report-only authorities | Worklists now offer explicitly inferred context names for acquisition, without creating aliases or identifying cases by party-name guess. Missing licensed bodies still require an authorised ICLR/Westlaw/Lexis export or a verified public source. |
| French completeness | Publisher article links are preserved where supplied; absent historical amendment chains, missing print pages and unverified Conseil d'État examples remain coverage gaps. No fabricated page numbers or inferred amendment direction. |
| RIS publisher collision | The reported docket includes two publisher headnote identifiers with the same ECLI. Preserve publisher evidence and investigate identity before any corpus-wide rekey; the full-judgment recovery does not resolve this separate publisher inconsistency. |
| RDI / BIPT stale links | Recovery imports and route repairs do not restore a publisher-removed detail/dossier. Keep individual failures visible. No-new-document warnings alone do not prove parser failure. |
| Slow graph/index work | Work is scoped and resumable, but statement timeouts and resource contention can still occur. Inspect failed job results and query plans before widening timeouts or deleting indexes. Completed extraction is not proof of completed resolution. |

## Validation and operation

The final release's full Python suite passed **3,165 tests, 112 skipped**, with no
failures, including the scan-resume, blocked-rule and misleading OJ-index regressions.
A real PostgreSQL deadlock test
used an isolated temporary schema and cleaned it up. Production tables were not used
by destructive test fixtures. Existing frontend validation remains five passing tests
and a successful build; this follow-through changes backend and operations code.

API and scheduler run the reviewed local image with process init; the previous images
and private compose/env backups are retained for rollback. Long acquisitions, rescans,
index builds and cleanup run through durable RagLex jobs. Feedback is resolved only
where its whole reported issue has been verified; aggregate/coverage reports retain
responses describing remaining gaps.

Final deployment: `raglex:open-findings-20261114-final` for API and scheduler.
Verified the rendered sign-in page through Asahi Scrapling (HTTP 200); no browser
sessions remain. VwGH/BVwG/LVwG backfill continuations after deployment are
`27718c78`, `8e9c3261`, `065a1ad3`. Another 20 stale-source warnings now have bounded
one-page diagnostic harvests queued, with persisted results and normal source-conflict
guards. These diagnose silent emptiness; queued probes are not successful coverage.
The separate Danish authority stale-feed warning remains for nighttime investigation.
