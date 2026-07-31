# Consumer-law source coverage

Verified against the live official services on 30 July 2026.

## Implemented collection surfaces

| Source key | Coverage | Enumeration/update mechanism | Citation policy |
|---|---|---|---|
| `uk-legislation` | UK primary/secondary legislation | legislation.gov.uk Atom/XML | Native Act/SI identifiers and provision anchors |
| `uk-cma` | CMA consumer-enforcement cases | GOV.UK Search API, `case_type=consumer-enforcement` | Generic UK/EU grammar; case PDFs included |
| `uk-cma-guidance` | All CMA guidance publications | GOV.UK Search API, `format=guidance`; Content API | HTML children preferred; CMA200/207/208 default to DMCCA 2024, CMA37 to CRA 2015 |
| `eu-legislation` | EU acts, national transposition index and opt-in dated consolidations | CELLAR SPARQL + Formex/HTML | CELEX; sector-0 snapshot → base `consolidates`; citing document → dated `applicable_version` |
| `eu-preparatory` | Sector-5 Commission documents, including OJ C notices | CELLAR SPARQL + Formex/HTML/PDF | A title naming one directive supplies the safe home for orphan Articles |
| `eu-consumer-guidance` | Commission consumer pages, CPC positions/common understandings, sweeps and documents | Commission sitemap + first-party document UUIDs | Same single-directive title rule; mixed-law pages have no default |
| `nl-acm-guidance` | Complete ACM `Leidraden` series | Three-page official HTML catalogue | Dutch BWB/Juriconnect grammar, including host-first `artikel` forms |
| `it-agcm` | AGCM weekly decision bulletins | Official paged register + PDFs | Explicit Codice del consumo article lists; no orphan carry-forward across decisions |
| `nl-rechtspraak` | Dutch case law | Rechtspraak Open Data/LiDO | ECLI plus structured outgoing links |
| `nl-legislation` | Dutch consolidated legislation | KOOP BWB/SRU | BWB/Juriconnect identifiers and dated versions |
| `fr-legislation` / `fr-dila-*` | French consolidated codes and decisions | PISTE and DILA open data | French code/article aliases, including Code de la consommation |
| `de-neuris*` / `de-gii` / `de-rii` | German legislation/case law | NeuRIS and federal bulk portals | German provision grammar |

Official entry points:

- GOV.UK APIs: <https://www.gov.uk/help/reuse-govuk-content>
- CMA cases: <https://www.gov.uk/cma-cases>
- Commission consumer topics: <https://commission.europa.eu/topics/consumers_en>
- EUR-Lex UCPD guidance: <https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52021XC1229(05)>
- ACM guidance: <https://www.acm.nl/nl/publicaties/voorlichting-aan-bedrijven/acm-leidraad>
- AGCM bulletins: <https://www.agcm.it/pubblicazioni/bollettino-settimanale/>

## UCPD identity and versions

The authoritative base act is `32005L0029`. Dated consolidated texts are separate
sector-0 records:

- `02005L0029-20050612` — original snapshot;
- `02005L0029-20220528` — current snapshot on 30 July 2026;
- `02005L0029-20260927` — future snapshot incorporating Directive (EU) 2024/825.

Import all three, with their base edge, using:

```text
uv run raglex run eu-legislation --backfill \
  -o celex=32005L0029 -o include_consolidations=true
```

Consolidated texts are documentation aids with no legal effect. RagLex marks them
non-authoritative and marks a not-yet-effective snapshot as future.

Citation/version semantics are deliberately two-edged:

1. The literal citation remains a `mentions`/treatment edge to the base Work
   (`32005L0029`), because that is the instrument the author cited.
2. RagLex derives an `applicable_version` edge to the latest held consolidation whose
   date is not later than the citing document. It preserves the same Article/section
   anchor, records the source and consolidation dates in `src_anchor`, and uses
   `extracted_via=inferred` so it cannot be mistaken for a printed sector-0 citation.
3. Undated material uses the latest non-future held consolidation. Importing a new
   consolidation retrofits existing citations immediately; it does not require every
   citing document to be re-extracted.

This gives a 2018 decision the 2005 UCPD text, a 2024 decision the 2022 text, and
never sends either to the future 27 September 2026 snapshot.

The Commission interpretation notice is `52021XC1229(05)`. It is collected by
`eu-preparatory`; its title declares the UCPD as the default instrument so its many
otherwise orphaned `Article N` references anchor to `32005L0029`.

## Citation-form audit

Live-document samples on 30 July 2026:

| Material | Observed reference form | Result |
|---|---|---|
| CMA207 | Mostly orphan `Section 225`, `section 226`, etc. after the regime is introduced once | CMA207 is an allow-listed DMCCA guide, so 207 carry-forward occurrences resolve to DMCCA 2024; 221/233 total citations target it |
| Commission UCPD notice | Hundreds of bare `Article N` and `Recital N` references, interspersed with other EU acts | The title names exactly one governing Directive, so 538 carry-forwards reset to UCPD after sentence boundaries; 549/1,323 citations target UCPD |
| General Commission consumer pages | Mixed directives/regulations and occasional orphan Articles | No page-wide default unless the title names exactly one Directive; only local explicit/carry-forward context is used |
| ACM guidance | Dutch `artikel 6:193a BW`, host-first `Burgerlijk Wetboek Boek 6 … artikel 193h`, `artikel 15 AVG`, `artikelen 5, 6 en 7 van de Richtlijn oneerlijke handelspraktijken`, and numbered EU Directives | Native Dutch host grammar; colon/dot articles are not truncated; `AVG` maps to GDPR rather than a German-law abbreviation; Dutch lists expand one edge per Article and named/numbered UCPD references resolve to `32005L0029` |
| AGCM bulletins | `articoli 20, 21 e 22 del Codice del consumo`, Italian UCPD names/numbers, and PDF artefacts such as `direttiva 2005/2915` (footnote 15 glued to 2005/29); later decisions restart numbering under other laws | Explicit article lists resolve to `it/dlgs/2005/206` or the printed EU Directive; the observed glued UCPD footnote resolves deterministically; all generic carry-forward is discarded at bulletin scope to prevent cross-decision leakage |

The general carry-forward grammar therefore remains useful, but a source receives a
document-wide home only when its adapter can prove a single governing instrument.
Multi-decision registers deliberately receive none.

## UCPD database verification (production, 31 July 2026)

Production now holds the base act and all three Cellar sector-0 records. The reader
correctly identifies `02005L0029-20220528` as the latest consolidation applicable today
and `02005L0029-20260927` as a held future snapshot. The base act and both usable dated
packages expose Annex I and Annex II as citable `annex` segments; the earliest 2005
expression remains metadata-only where Cellar supplies no usable English manifestation.

Opening the base UCPD in the web reader or MCP now defaults to the 2022 consolidation and
offers an explicit route to the original act. Literal citations remain attached to
`32005L0029` as evidence, but the consolidation's read model projects those mentions onto
the same Article/Recital anchor and combines them with direct citations to the dated
expression. This also surfaces a citation to a newly inserted provision on the first
consolidation that contains it. The projection is marked as version-inherited and is used
by the API/MCP and static exporter; it is not another fabricated citation row.

Because sector-0 expressions omit the preamble, each consolidation also exposes the
base act's unchanged recital segments as a virtual, clearly labelled section. The recital
text and its outgoing links are read from `32005L0029`; `Recital N` incoming mentions are
already projected by the same version-inheritance mechanism. Nothing is copied into the
dated expression and no mention count is multiplied by the number of consolidations.

A complete resumable sector-0 Cellar walk and a local all-held-EU annex repair are
available as first-class jobs and scheduled maintenance. Opening a base EU act with no
held version also starts a deduplicated targeted Cellar sync automatically. Targeted sync
only processes that act's discovered versions; the source-wide unfinished citation
backlog belongs exclusively to the full sector-0 sweep. The reverse sweep yields each
distinct sector-3 base act before its first dated expression, so the original Formex
preamble is collected as the consolidation catalogue expands.

Repeatable audit queries:

```sql
-- Base + snapshots + persisted payload versions.
SELECT stable_id, source, title, has_text, version, meta_json
FROM documents
WHERE stable_id = '32005L0029'
   OR stable_id LIKE '02005L0029-%'
ORDER BY stable_id;

SELECT stable_id, version, archived_at, payload_hash
FROM document_versions
WHERE stable_id = '32005L0029'
   OR stable_id LIKE '02005L0029-%'
ORDER BY stable_id, version;

-- Every direct inbound edge, grouped by resolution/provenance.
SELECT resolution_status, extracted_via, relationship_type,
       count(*) AS edges, count(DISTINCT src_id) AS documents
FROM relations
WHERE dst_id = '32005L0029' OR candidate_id = '32005L0029'
GROUP BY 1,2,3
ORDER BY documents DESC;

-- Citation observations which name the UCPD but did not become an edge to it.
SELECT count(*) AS occurrences, count(DISTINCT src_id) AS documents,
       method, candidate_id
FROM citations
WHERE candidate_id = '32005L0029'
   OR raw ~* '(2005\\s*/\\s*29|unfair commercial practices directive|\\bUCPD\\b)'
GROUP BY method, candidate_id
ORDER BY documents DESC;

-- Suspect observations: text names UCPD, relation target differs or is absent.
SELECT c.src_id, c.raw, c.pinpoint, c.method, c.candidate_id
FROM citations c
WHERE c.raw ~* '(2005\\s*/\\s*29|unfair commercial practices directive|\\bUCPD\\b)'
  AND c.candidate_id IS DISTINCT FROM '32005L0029'
ORDER BY c.src_id
LIMIT 500;

-- Structured transposition measures already attached to the directive.
SELECT r.raw_citation_string, r.dst_id, r.resolution_status
FROM relations r
WHERE r.src_id = '32005L0029' AND r.relationship_type = 'transposes'
ORDER BY r.raw_citation_string;
```

“Everything that links to the UCPD” is represented in two layers: `citations` retains
each textual observation and pinpoint; `relations` collapses repeated observations to
graph edges. A document therefore need not have one relation per textual occurrence.
Resolved relations use `dst_id=32005L0029`; pending observations use
`candidate_id=32005L0029` until resolution runs.

## Next national priorities

1. France DGCCRF practical sheets, guidance and published enforcement material. The
   official site currently challenges plain HTTP clients; use RagLex's existing
   Scrapling/Camoufox tier rather than an undocumented browser API.
2. Individual AGCM decisions. The weekly bulletin is complete, but splitting its
   `PS`/`IP` measures into first-class records would improve decision-level pincites.
3. Ireland CCPC consumer guidance and enforcement undertakings (separate from the
   existing merger register).
4. Spain and Nordic consumer-authority guidance; expect small, heterogeneous HTML/PDF
   collections rather than stable APIs.
5. Historical e-Justice Consumer Law Database snapshot, clearly stamped with its
   15 July 2021 cutoff.

Do not ingest BAILII or ASA at scale without resolving their reuse/licensing terms.
