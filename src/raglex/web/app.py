"""Thin FastAPI app over the shared Facade (§8).

Ops-first (§8): source health, queues, alerts come before the research surface
(stats, search, the citation-graph neighbourhood). The write surface lets a human
(via the React UI) or an agent augment the corpus: import PDFs/HTML in several
modes (file upload, URL, base64), write notes, attach files, link, and tag. The
exact same operations are exposed over MCP (``raglex.mcp_server``) from the same
Facade, so the two never drift.
"""

from __future__ import annotations

import os
import re
import uuid as _uuid

from fastapi import Body, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import Config
from ..facade import Facade
from ..jobs import JobManager

# Endpoints reachable without the API token: the liveness probe (so a healthcheck needn't
# hold a secret) and the CORS preflight the browser sends before it can add a header.
_PUBLIC_PATHS = frozenset({"/health"})


def _install_auth(app: FastAPI) -> None:
    """Require a bearer token when ``RAGLEX_API_TOKEN`` is set.

    Unauthenticated, the write surface lets anyone on the network rewrite settings —
    including the stored API keys — and run corrections against the corpus. The token is
    opt-in so existing local/dev setups keep working untouched; set it and the whole API
    (and the MCP endpoint mounted beside it) is closed.
    """
    token = os.environ.get("RAGLEX_API_TOKEN")
    if not token:
        return

    @app.middleware("http")
    async def _require_token(request: Request, call_next):  # noqa: ANN001
        if request.method == "OPTIONS" or request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else \
            request.headers.get("x-api-key", "")
        # constant-time compare: a token check that leaks timing is a token check that
        # can be guessed byte by byte.
        import hmac

        if not hmac.compare_digest(supplied, token):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        return await call_next(request)


def create_app(config: Config | None = None) -> FastAPI:
    facade = Facade(config or Config.from_env())
    facade.warm_caches()  # pre-compute heavy dashboard aggregates so first load is instant
    jobs = JobManager(facade, origin="api")
    jobs.reap_orphans()  # rows the previous process left 'running' have no thread behind them
    app = FastAPI(title="RagLex", version="0.1.0", summary="Legal corpus ops + research API")
    # The React dev server lives on another origin; allow it (tighten in prod).
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    _install_auth(app)

    def _start_job(kind: str, label: str, params: dict | None = None) -> dict:
        return jobs.start(kind, label, params or {})

    # -- ops (build/observe first, §8) ------------------------------------
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/sources")
    def sources() -> list[dict]:
        return facade.sources()

    @app.get("/queues")
    def queues() -> dict:
        return facade.queues()

    @app.get("/alerts")
    def alerts() -> list[dict]:
        return facade.alerts()

    @app.get("/worklist")
    def worklist(limit: int = 50) -> list[dict]:
        return facade.worklist(limit=limit)

    @app.get("/snowball")
    def snowball(limit: int = 50, only_unharvestable: bool = False) -> list[dict]:
        """Citation frontier (§5a): cited-but-not-held forms, ranked by frequency."""
        return facade.snowball(limit=limit, only_unharvestable=only_unharvestable)

    @app.get("/unresolved")
    def unresolved(limit: int = 100) -> list[dict]:
        """Hanging references the corpus can't satisfy — the manual-resolution queue."""
        return facade.unresolved_references(limit=limit, with_citing=True)

    @app.get("/unresolved/unfetchable")
    def unfetchable(limit: int = 200) -> dict:
        """Most-cited references with NO fetch route — classic law reports, cases by name,
        courts with no adapter — each with a BAILII link + upload-to-resolve."""
        return facade.unfetchable_references(limit=limit)

    @app.get("/export/retrieval-citations")
    def export_retrieval_citations_ep(
        min_citing: int = 2, batch_size: int = 100, include_names: bool = False,
        separator: str = "newline", series: str | None = None,
        jurisdictions: str | None = None,
    ) -> dict:
        """Mention-ranked, ≤100-per-batch citation lists to paste into Westlaw Find & Print
        / Lexis+ Get & Print (the report-only authorities BAILII + FCL lack).
        ``jurisdictions`` is a csv of uk/ie/eu/commonwealth — a UK subscription can't
        retrieve the Irish/Commonwealth series, so filter them out of the batch."""
        inc = tuple(s.strip() for s in series.split(",") if s.strip()) if series else None
        jur = tuple(j.strip() for j in jurisdictions.split(",") if j.strip()) if jurisdictions else None
        return facade.export_retrieval_citations(
            min_citing=min_citing, batch_size=batch_size, include_names=include_names,
            separator=separator, include_series=inc, jurisdictions=jur)

    @app.get("/export/retrieval-citations.txt")
    def export_retrieval_citations_txt_ep(
        min_citing: int = 2, batch_size: int = 100, include_names: bool = False,
        separator: str = "newline", series: str | None = None,
        jurisdictions: str | None = None,
    ):
        """The same export as a downloadable .txt (all batches, delimited by headers)."""
        from fastapi.responses import PlainTextResponse

        inc = tuple(s.strip() for s in series.split(",") if s.strip()) if series else None
        jur = tuple(j.strip() for j in jurisdictions.split(",") if j.strip()) if jurisdictions else None
        res = facade.export_retrieval_citations(
            min_citing=min_citing, batch_size=batch_size, include_names=include_names,
            separator=separator, include_series=inc, jurisdictions=jur)
        return PlainTextResponse(res["combined_text"], headers={
            "Content-Disposition": 'attachment; filename="raglex-citations-for-retrieval.txt"'})

    @app.get("/coverage")
    def coverage() -> dict:
        """Completeness/uncertainty dashboard: counts, date spans, resolution rate,
        hanging references, and the citation frontier (§8)."""
        return facade.coverage()

    # -- corrections (fix misclassification) -------------------------------
    @app.post("/documents/{stable_id:path}/update")
    def update_document_ep(stable_id: str, payload: dict = Body(...)) -> dict:
        return facade.update_document(stable_id=stable_id, **payload)

    @app.post("/citations/correct")
    def correct_citation_ep(payload: dict = Body(...)) -> dict:
        return facade.correct_citation(**payload)

    @app.post("/documents/{stable_id:path}/reparse")
    def reparse_ep(stable_id: str) -> dict:
        return facade.reparse_document(stable_id=stable_id)

    @app.post("/reparse-all")
    def reparse_all_ep(payload: dict = Body(default={})) -> dict:
        return facade.reparse_all(**(payload or {}))

    @app.post("/untag")
    def untag_ep(payload: dict = Body(...)) -> dict:
        return facade.untag(**payload)

    @app.post("/tag-many")
    def tag_many_ep(payload: dict = Body(...)) -> dict:
        return facade.tag_many(**payload)

    @app.post("/unresolved/resolve")
    def resolve_reference_ep(payload: dict = Body(...)) -> dict:
        """Satisfy a hanging reference by identifier / existing item / scrape URL."""
        return facade.resolve_reference(**payload)

    @app.post("/unresolved/harvest")
    def harvest_reference_ep(payload: dict = Body(...)) -> dict:
        """One-click: fetch a routable reference's exact item from its adapter, resolve."""
        return facade.harvest_reference(**payload)

    # -- legislation point-in-time versions --------------------------------
    @app.get("/legislation/versions")
    def legislation_versions_ep(id: str) -> dict:
        return facade.legislation_versions(stable_id=id)

    @app.post("/legislation/version")
    def legislation_version_ep(payload: dict = Body(...)) -> dict:
        return facade.harvest_legislation_at(stable_id=payload["id"], date=payload["date"])

    # -- outstanding amendments (the editorial lag) ------------------------
    @app.get("/legislation/effects")
    def outstanding_effects_ep(limit: int = 500) -> list[dict]:
        return facade.outstanding_effects(limit=limit)

    @app.post("/legislation/effects/refresh")
    def refresh_effects_ep(payload: dict = Body(default={})) -> dict:
        return facade.refresh_effects(limit=int((payload or {}).get("limit", 10)))

    @app.post("/legislation/echr-convention")
    def import_echr_ep() -> dict:
        """(Re)import the European Convention on Human Rights full text from Wikisource."""
        return facade.import_echr_convention()

    @app.get("/legislation/changes")
    def effects_caused_by_ep(id: str) -> list[dict]:
        """What an amending instrument changes (its incoming amended_by edges)."""
        return facade.effects_caused_by(stable_id=id)

    @app.post("/legislation/changes/propagate")
    def propagate_changes_ep(payload: dict = Body(default={})) -> dict:
        """Push one act's changes out to the instruments it affects, OR (no id) scan a
        bounded batch of held acts. Flags affected acts we hold for re-pull."""
        p = payload or {}
        if p.get("id"):
            return facade.propagate_changes_from(stable_id=p["id"])
        return facade.propagate_changes(limit=int(p.get("limit", 5)))

    # -- named aliases / shorthand rules -----------------------------------
    @app.get("/aliases")
    def list_aliases_ep() -> list[dict]:
        return facade.list_named_aliases()

    @app.post("/aliases")
    def create_alias_ep(payload: dict = Body(...)) -> dict:
        return facade.create_named_alias(**payload)

    @app.delete("/aliases")
    def delete_alias_ep(phrase: str) -> dict:
        return facade.delete_named_alias(phrase=phrase)

    @app.post("/aliases/apply")
    def apply_rules_ep() -> dict:
        return facade.apply_rules()

    @app.post("/jobs/rescan-citations")
    def job_rescan_ep(payload: dict = Body(default={})) -> dict:
        """Re-extract every document with the current grammars/rules (picks up new grammars
        like the law reports) — as a progress-tracked background job. Optional ``source``
        scopes it (e.g. just uk-caselaw), far faster since reports are cited by case law."""
        p = payload or {}
        label = f"re-scan {p['source']} for new citations" if p.get("source") else "re-scan corpus for new citations"
        return _start_job("rescan-citations", label, {"source": p["source"]} if p.get("source") else {})

    @app.post("/jobs/rescan")
    def job_rescan_full_ep(payload: dict = Body(default={})) -> dict:
        """Full fresh relink: re-extract EVERY text document with the current grammars, then
        run the whole resolution chain (legislation-name, report, EHRR and parallel/ECR
        matchers). One progress-tracked job; ``no_parallel`` skips the heavy mining pass."""
        p = payload or {}
        params = {k: v for k, v in p.items()
                  if k in ("limit", "parallel", "coref", "doc_types", "source",
                           # resume rather than redo: only documents with no edges yet
                           "only_unextracted",
                           # skip documents extracted within the last N days (restart-cheap)
                           "stale_days")}
        scope = params.get("source") or (
            "judgments" if params.get("doc_types") == ["judgment"] else "all docs")
        if params.get("stale_days"):
            scope += f", stale >{params['stale_days']}d"
        return _start_job("rescan", f"full fresh relink ({scope}) — re-extract + match everything", params)

    @app.post("/jobs/harvest-echr")
    def job_harvest_echr_ep(payload: dict = Body(default={})) -> dict:
        """Queue the ECtHR cases the corpus cites by name/EHRR but doesn't hold, and fetch
        them from HUDOC by docname search; ``limit`` bounds how many (most-cited first)."""
        p = payload or {}
        params = {k: v for k, v in p.items() if k in ("limit", "match_after")}
        return _start_job("harvest-echr", "queue + harvest missing ECtHR cases from HUDOC", params)

    @app.post("/jobs/match-legislation")
    def job_match_legislation_ep() -> dict:
        """Resolve name-only statute references against the titles of held legislation."""
        return _start_job("match-legislation", "match named legislation to held titles")

    @app.post("/jobs/match-echr")
    def job_match_echr_ep() -> dict:
        """Link EHRR citations to held ECtHR cases by applicant name + year."""
        return _start_job("match-echr", "match EHRR citations to ECtHR cases")

    @app.post("/jobs/mine-parallel")
    def job_mine_parallel_ep() -> dict:
        """Mine parallel citations (neutral↔report, ECR↔case number) from judgment text."""
        return _start_job("mine-parallel", "mine parallel citations")

    @app.post("/jobs/backfill-edge-keys")
    def job_backfill_edge_keys_ep() -> dict:
        """One-off after upgrade: populate candidate_id/raw_fold on pre-existing edges so
        the set-based resolver and the SQL worklist see the whole graph."""
        return _start_job("backfill-edge-keys", "backfill edge candidate ids")

    @app.post("/jobs/rebuild-citation-counts")
    def job_rebuild_counts_ep() -> dict:
        """Refresh the snowball's citation-frequency roll-up."""
        return _start_job("rebuild-citation-counts", "rebuild citation frequency roll-up")

    @app.get("/probes")
    def probes_ep(only: str | None = None) -> list[dict]:
        """Corpus-integrity probes: invariant violations with counts + samples."""
        return facade.run_probes(only=only.split(",") if only else None)

    @app.post("/backfill-eu-titles")
    def backfill_eu_titles_ep(payload: dict = Body(default={})) -> dict:
        """Fill missing EU-instrument titles from their own scraped text."""
        return facade.backfill_eu_titles(limit=int((payload or {}).get("limit", 2000)))

    @app.post("/probes/repair")
    def probes_repair_ep(payload: dict = Body(...)) -> dict:
        """Run the targeted repair for one repairable probe (read samples first)."""
        return facade.repair_probe(payload["name"])

    @app.post("/jobs/rebuild-authority")
    def job_rebuild_authority_ep() -> dict:
        """Recompute the PageRank authority roll-up over the citation graph (design §3a) —
        feeds search fusion, ranked neighbours, the citator, and 'sort by authority'."""
        return _start_job("rebuild-authority", "rebuild citation-network authority (PageRank)")

    @app.post("/suggestions/decide-bulk")
    def decide_suggestions_bulk_ep(payload: dict = Body(...)) -> dict:
        """Decide MANY suggestions in one call — items: [{ref, suggested_id, accept}].
        Resolves once at the end rather than per row."""
        return facade.decide_suggestions(items=payload.get("items") or [])

    @app.post("/jobs/suggest-matches")
    def job_suggest_matches_ep(payload: dict = Body(default={})) -> dict:
        """Populate the human-confirmable "Possibly: …?" match suggestions (nested/year-slip
        legislation titles, party-name report matches, sub-threshold EHRR names)."""
        return _start_job("suggest-matches", "suggest matches for hanging references",
                          payload or {})

    @app.post("/suggestions/decide")
    def decide_suggestion_ep(payload: dict = Body(...)) -> dict:
        """Tick (accept) or cross (reject) a suggestion. Accept aliases + resolves, and
        harvests the target if it isn't held yet. ``resolve: false`` defers the resolver
        pass (the bulk sweep resolves once at the end via POST /resolve)."""
        return facade.decide_suggestion(
            ref=payload["ref"], suggested_id=payload["suggested_id"],
            accept=bool(payload.get("accept", True)),
            resolve=bool(payload.get("resolve", True)))

    @app.get("/suggestions/pending")
    def pending_suggestions_ep(limit: int = 500) -> dict:
        """All pending naming-candidate suggestions, best score first — the bulk list."""
        return facade.list_pending_suggestions(limit=limit)

    @app.get("/reference-context")
    def reference_context_ep(ref: str, limit: int = 5) -> dict:
        """The passages where the corpus cites a hanging reference — the judgement
        evidence behind a near-miss suggestion."""
        return facade.reference_context(ref, limit=limit)

    @app.post("/refinement-flags")
    def add_refinement_flag_ep(payload: dict = Body(...)) -> dict:
        """Record a reader passage flagged 'for improved refinement' — the selection, its
        location, what it currently links to, and the user's note."""
        return facade.flag_refinement(
            doc_id=payload["doc_id"], selected_text=payload["selected_text"],
            anchor=payload.get("anchor"), context=payload.get("context"),
            current_links=payload.get("current_links"), note=payload.get("note"))

    @app.get("/refinement-flags")
    def list_refinement_flags_ep(status: str | None = "open", limit: int = 500) -> list[dict]:
        return facade.list_refinement_flags(status=status or None, limit=limit)

    @app.post("/refinement-flags/{flag_id}/status")
    def set_refinement_flag_ep(flag_id: int, payload: dict = Body(default={})) -> dict:
        return facade.resolve_refinement_flag(
            flag_id=flag_id, status=(payload or {}).get("status", "resolved"))

    @app.post("/unresolved/retry-failed")
    def retry_failed_ep() -> dict:
        """Clear the harvest cool-down lists so the next drain re-attempts every reference."""
        return facade.retry_failed_references()

    @app.post("/unresolved/harvest-all")
    def harvest_all_ep(payload: dict = Body(default={})) -> dict:
        """Drain every routable, high-confidence hanging reference, then resolve once."""
        return facade.harvest_all_references(**(payload or {}))

    @app.post("/radiate")
    def radiate_ep(payload: dict = Body(...)) -> dict:
        """Snowball-sample the citation network from a seed (or seed rule) N degrees."""
        return facade.radiate(**payload)

    @app.post("/discover-citing")
    def discover_citing_ep(payload: dict = Body(...)) -> dict:
        """Find NEW cases citing a target via the live source (FCL search / CELLAR)."""
        return facade.discover_citing(**payload)

    @app.post("/detect-citations")
    def detect_citations_ep(payload: dict = Body(...)) -> dict:
        """Preview: recognise every citation in a block of pasted text (no fetching)."""
        return facade.detect_citations(text=payload.get("text", ""))

    @app.post("/jobs/expand-citing")
    def job_expand_citing_ep(payload: dict = Body(default={})) -> dict:
        """Find + pull every case that cites a case already in the corpus (default: the EU
        case-law, via CELLAR's citation graph). Runs as a background job."""
        p = payload or {}
        return _start_job("expand-citing", "pull cases citing held cases",
                          {"source": p.get("source", "eu-cellar"), "limit": int(p.get("limit", 1000))})

    @app.post("/jobs/refresh-category")
    def job_refresh_category_ep(payload: dict = Body(...)) -> dict:
        """"Total refresh" for one Corpus Map category: harvest its pending references, then
        (EU case-law) pull citing cases. Runs as a background job."""
        cat = (payload or {}).get("category", "")
        return _start_job("refresh-category", f"total refresh — {cat}", {"category": cat})

    @app.post("/jobs/harvest-hol")
    def job_harvest_hol_ep(payload: dict = Body(default={})) -> dict:
        """Scrape the House of Lords archive (1996–2009) and link reporter-only citations
        to what's harvested. Background job (the scrape is slow + bot-gated)."""
        p = payload or {}
        params = {k: v for k, v in p.items() if k in ("ids", "limit", "match_reports")}
        return _start_job("harvest-hol", "scrape House of Lords + match reports", params)

    @app.post("/jobs/match-reports")
    def job_match_reports_ep() -> dict:
        """Match reporter-only citations ("[1998] AC 1") to harvested cases by name+year."""
        return _start_job("match-reports", "match reporter citations to harvested cases")

    @app.post("/jobs/pull-ag-opinions")
    def job_pull_ag_ep() -> dict:
        """Pull the AG Opinion for every held CJEU judgment that lacks one. Background job."""
        return _start_job("pull-ag-opinions", "pull AG opinions for held CJEU cases")

    @app.post("/jobs/seed-text")
    def job_seed_text_ep(payload: dict = Body(...)) -> dict:
        """Paste text → detect citations → harvest + radiate (forwards) and pull citing
        cases (backwards), as a background job."""
        return _start_job("seed-text", "seed from pasted text", dict(payload or {}))

    @app.post("/backfill-titles")
    def backfill_titles_ep(payload: dict = Body(default={})) -> dict:
        """Fill missing CJEU case names from CELLAR."""
        return facade.backfill_titles(**(payload or {}))

    @app.post("/jobs/backfill-metadata")
    def job_backfill_metadata_ep() -> dict:
        """Repair stored docs from raw: UK court from slug, re-parse ruling-only CJEU
        judgments, derive CJEU titles from the Formex parties. Runs as a job."""
        return _start_job("backfill-metadata", "repair court/title/ruling-only metadata")

    # -- background jobs (so long ops report progress instead of blocking) --
    @app.post("/jobs/radiate")
    def job_radiate_ep(payload: dict = Body(...)) -> dict:
        label = "snowball " + ", ".join(payload.get("seeds") or [str(payload.get("seed_rule"))])
        return _start_job("radiate", label[:80], dict(payload or {}))

    @app.post("/jobs/harvest-all")
    def job_harvest_all_ep(payload: dict = Body(default={})) -> dict:
        return _start_job("harvest-all", "harvest all routable references", dict(payload or {}))

    @app.get("/jobs")
    def jobs_list_ep(limit: int = 60) -> list[dict]:
        """All recent jobs (running first) for the global jobs panel — each with its
        latest log line so the panel shows live activity without fetching every job.
        Includes jobs started by the *scheduler* container, which the old in-process
        registry could never see."""
        return jobs.list(limit=limit)

    @app.get("/jobs/{job_id}")
    def job_status_ep(job_id: str, tail: int = 40) -> dict:
        """Full status of one job incl. the rolling log (last ``tail`` lines) — polled by
        the jobs panel for the live, verbose, item-by-item view."""
        return jobs.get(job_id, tail=tail)

    @app.post("/jobs/{job_id}/cancel")
    def job_cancel_ep(job_id: str) -> dict:
        return jobs.cancel(job_id)

    @app.post("/jobs/{job_id}/restart")
    def job_restart_ep(job_id: str) -> dict:
        """Re-launch a job from where its persisted data left off — for a frozen job (host
        slept and its network socket died) or any finished/cancelled one. The work is
        idempotent: dedup skips held docs, recorded misses are skipped, so a restart only
        does what's left. The old (maybe still-parked) thread is signalled to cancel."""
        return jobs.restart(job_id)

    # -- watches (saved harvest plans + scheduler, §5a) --------------------
    @app.get("/sources/catalog")
    def source_catalog_ep() -> list[dict]:
        """Per-source capabilities — drives the morphing harvest/watch UI."""
        return facade.source_catalog()

    @app.get("/watches")
    def list_watches_ep() -> list[dict]:
        return facade.list_watches()

    @app.post("/watches")
    def create_watch_ep(payload: dict = Body(...)) -> dict:
        return facade.create_watch(**payload)

    @app.post("/watches/{watch_id}")
    def update_watch_ep(watch_id: int, payload: dict = Body(...)) -> dict:
        return facade.update_watch(watch_id=watch_id, **payload)

    @app.post("/watches/{watch_id}/run")
    def run_watch_ep(watch_id: int) -> dict:
        """Run a watch as a background job so it shows up in the Jobs panel with
        per-stage progress (harvest → discover → snowball → tag)."""
        w = facade.get_watch(watch_id)
        label = f"watch: {w.get('name', watch_id)}" if w else f"watch {watch_id}"
        return _start_job("run-watch", label, {"watch_id": watch_id})

    @app.post("/jobs/gap-scan")
    def gap_scan_ep(payload: dict = Body(...)) -> dict:
        """Fill gaps in a court's neutral-citation numbering: probe ``[year] COURT n`` for
        n = 1…, harvest the ones that exist, record the gaps (historic = permanent). Runs
        as a background job (each probe is one fetch)."""
        p = payload or {}
        court = (p.get("court") or "").strip()
        year = p.get("year")
        if not court or not year:
            return {"error": "court (e.g. ewca/civ) and year required"}
        params = {k: p[k] for k in ("court", "year", "start", "max_probes", "stop_after_misses") if p.get(k) is not None}
        return _start_job("gap-scan", f"gap-scan {court} {year}", params)

    @app.get("/gap-status")
    def gap_status_ep(court: str, year: int) -> dict:
        """Completeness of one court+year: held numbers, permanent gaps, pending re-probes."""
        return facade.gap_status(court=court, year=year)

    @app.post("/gap-clear")
    def gap_clear_ep(payload: dict = Body(default={})) -> dict:
        """Forget recorded gaps (so they're re-probed) for a court/year, or all."""
        p = payload or {}
        return facade.clear_gap_markers(court=p.get("court"), year=p.get("year"))

    @app.delete("/watches/{watch_id}")
    def delete_watch_ep(watch_id: int) -> dict:
        return facade.delete_watch(watch_id=watch_id)

    @app.post("/watches/tick")
    def tick_watches_ep() -> dict:
        return facade.tick_watches()

    @app.post("/unresolved/resolve-file")
    async def resolve_reference_file_ep(
        file: UploadFile = File(...),
        ref: str = Form(...),
        identifier: str | None = Form(None),
        jurisdiction: str | None = Form(None),
        title: str | None = Form(None),
        doc_type: str = Form("commentary"),
    ) -> dict:
        import base64 as _b64

        data = await file.read()
        return facade.resolve_reference(
            ref=ref, identifier=identifier, jurisdiction=jurisdiction, title=title,
            doc_type=doc_type, content_base64=_b64.b64encode(data).decode(),
            filename=file.filename or "reference.bin",
        )

    @app.get("/sources/list")
    def sources_list() -> list[str]:
        return facade.list_sources()

    @app.post("/harvest")
    def harvest(payload: dict = Body(...)) -> dict:
        return facade.harvest(
            payload["source"], backfill=payload.get("backfill", False),
            since=payload.get("since"), max_pages=payload.get("max_pages", 1),
            options=payload.get("options"),
        )

    @app.post("/jobs/harvest-source")
    def job_harvest_source_ep(payload: dict = Body(...)) -> dict:
        """Harvest/backfill one source as a background job.

        A full-catalogue backfill (``max_pages: null``) walks a whole register and can
        run for hours, so it goes in the job table where it survives the request, shows
        progress in the Jobs panel, and can be cancelled — unlike ``POST /harvest``,
        which is the small, bounded, synchronous version."""
        source = (payload or {}).get("source")
        if not source:
            return {"error": "source is required"}
        params: dict = {"source": source, "backfill": bool(payload.get("backfill", True))}
        # max_pages absent/None → no page cap (the true "everything" walk).
        if payload.get("max_pages") is not None:
            params["max_pages"] = int(payload["max_pages"])
        else:
            params["max_pages"] = None
        if payload.get("since"):
            params["since"] = payload["since"]
        if payload.get("options"):
            params["options"] = payload["options"]
        scope = "everything" if params["max_pages"] is None else f"{params['max_pages']} page(s)"
        verb = "backfill" if params["backfill"] else "harvest"
        return _start_job("harvest-source", f"{verb} {source} — {scope}", params)

    @app.get("/health/embedding")
    def embedding_health() -> dict:
        return facade.provider_health()

    # -- research ----------------------------------------------------------
    @app.get("/stats")
    def stats() -> dict:
        return facade.stats()

    @app.get("/documents")
    def documents(
        source: str | None = None, doc_type: str | None = None, tag: str | None = None,
        query: str | None = None, court: str | None = None, id_prefix: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        return facade.list_documents(
            source=source, doc_type=doc_type, tag=tag, query=query, court=court,
            id_prefix=id_prefix, limit=limit, offset=offset,
        )

    @app.get("/documents/count")
    def documents_count(source: str | None = None, doc_type: str | None = None,
                        tag: str | None = None, query: str | None = None,
                        court: str | None = None, id_prefix: str | None = None) -> dict:
        return facade.count_documents(source=source, doc_type=doc_type, tag=tag, query=query,
                                      court=court, id_prefix=id_prefix)

    @app.get("/search-corpus")
    def search_corpus_ep(
        query: str | None = None, source: str | None = None, doc_type: str | None = None,
        court: str | None = None, tag: str | None = None, year_from: str | None = None,
        year_to: str | None = None, cites: str | None = None, cited_by: str | None = None,
        cites_pinpoint: str | None = None, id_prefix: str | None = None,
        sort: str | None = None, limit: int = 50, offset: int = 0, facets: bool = True,
    ) -> dict:
        """Unified metadata search: filtered + sorted results plus the facet distribution of
        the whole match set (for the refine sidebar + histograms)."""
        return facade.search_corpus(
            query=query, source=source, doc_type=doc_type, court=court, tag=tag,
            year_from=year_from, year_to=year_to, cites=cites, cited_by=cited_by,
            cites_pinpoint=cites_pinpoint, id_prefix=id_prefix,
            sort=sort, limit=limit, offset=offset, facets=facets)

    @app.get("/facet-values")
    def facet_values_ep() -> dict:
        """Values (+counts) for each advanced-search facet — sources, doc types, courts, tags."""
        return facade.corpus_facet_values()

    @app.get("/corpus-shape")
    def corpus_shape_ep() -> dict:
        """The Explore homepage: the corpus's whole shape by jurisdiction — counts by
        kind, year distributions, courts, density, top-authority documents."""
        return facade.corpus_shape()

    @app.get("/drill")
    def drill_ep(jurisdiction: str = "", court: str | None = None, kind: str | None = None,
                 year_from: str | None = None, year_to: str | None = None,
                 cites: str | None = None, sort: str = "authority",
                 leg: str | None = None, limit: int = 25) -> dict:
        """One Explore drill-down step: top documents of a slice, sortable
        (authority/cited/newest/oldest), with hanging groupings for legislation.
        ``cites`` flips to the documents citing that target; ``leg`` is a JSON
        list of taxonomy filter dicts scoping a legislation type."""
        return facade.jurisdiction_drill(jurisdiction, court=court, kind=kind,
                                         year_from=year_from, year_to=year_to,
                                         cites=cites, sort=sort, leg=leg, limit=limit)

    @app.get("/corpus-map")
    def corpus_map_ep() -> dict:
        """Held-vs-pending by legal category & sub-type — the dashboard coverage table."""
        return facade.corpus_map()

    @app.get("/corpus-map/cites")
    def corpus_map_cites_ep(category: str) -> dict:
        """Lazy: what this category's held docs cite, by target category (unique + total)."""
        return facade.corpus_map_cites(category=category)

    @app.get("/mentions")
    def mentions(id: str, anchor: str | None = None) -> dict:
        """Who mentions this document (optionally one paragraph), grouped by citing document
        and ranked by the citer's own authority — for the "Mentioned by …" line + tray."""
        return facade.document_mentions(id, anchor=anchor)

    @app.get("/citations-out")
    def citations_out(id: str, family: str = "cases") -> dict:
        """Distinct authorities this document cites (``family`` = cases | statute), OSCOLA-
        formatted with collapsed pinpoints — for the summary-line trays."""
        return facade.document_citations_out(id, family=family)

    @app.get("/document-body")
    def document_body(id: str) -> dict:
        # query-param route: stable_ids contain slashes (ukpga/2000/36), so a
        # /documents/{id}/body suffix would be ambiguous.
        return facade.document_body(id)

    # NB: registered BEFORE the /documents/{stable_id:path} catch-all so the
    # trailing /raw wins the route match (slugs themselves contain slashes).
    @app.get("/documents/{stable_id:path}/raw")
    def document_raw_ep(stable_id: str):
        """Stream the document's ORIGINAL stored file (guidance PDF, styled BAILII
        page, Formex XML) for the reader's original-document pane. HTML is served
        under a sandboxing CSP: a stored page's scripts must never run against the
        app's origin (they could read the API token)."""
        from fastapi.responses import FileResponse, JSONResponse as _JR

        info = facade.document_raw(stable_id)
        if info is None:
            return _JR({"error": "no stored original for this document"}, status_code=404)
        media = {
            "pdf": "application/pdf", "html": "text/html; charset=utf-8",
            "htm": "text/html; charset=utf-8", "xml": "application/xml",
            "txt": "text/plain; charset=utf-8", "rtf": "application/rtf",
            "json": "application/json",
        }.get(info["ext"], "application/octet-stream")
        headers = {"Content-Disposition": f'inline; filename="{info["stable_id"].replace("/", "_")}.{info["ext"]}"'}
        if info["ext"] in ("html", "htm"):
            headers["Content-Security-Policy"] = "sandbox; script-src 'none'"
        return FileResponse(info["path"], media_type=media, headers=headers)

    @app.post("/citations/scan")
    def scan_citations_ep(payload: dict = Body(...)) -> dict:
        """Recognise + resolve citations in arbitrary text — the PDF viewer sends each
        rendered page's text layer through this to linkify it like the text reader."""
        return {"citations": facade.scan_citations(text=payload.get("text") or "")}

    @app.get("/related")
    def related_ep(id: str, limit: int = 12) -> dict:
        """Related documents via the citation network: co-citation ("often cited together")
        + bibliographic coupling ("relies on the same authorities")."""
        return facade.related_documents(id, limit=limit)

    @app.get("/citator")
    def citator_ep(id: str) -> dict:
        """How this authority stands: citation volume + recency, authority percentile,
        most significant citors. (No treatment counts — not reliable yet.)"""
        return facade.citator(id)

    @app.get("/provision")
    def provision_ep(id: str, label: str | None = None, start: int | None = None,
                     end: int | None = None, n: int = 1) -> dict:
        """One provision/paragraph by citable label OR char span, with ±n context
        segments and the heading breadcrumb — the search 'show context' expander
        and the MCP get_provision tool."""
        return facade.get_provision(id, label=label, char_start=start, char_end=end, context=n)

    @app.get("/documents/{stable_id:path}")
    def document(stable_id: str) -> dict:
        return facade.get_document(stable_id)

    @app.get("/graph/{stable_id:path}")
    def graph(stable_id: str, rel: list[str] | None = Query(default=None)) -> dict:
        return facade.graph(stable_id, rel=rel)

    @app.get("/search")
    def search(
        q: str, k: int = 5, source: list[str] | None = Query(default=None),
        doc_type: list[str] | None = Query(default=None),
        year_from: str | None = None, tag: str | None = None,
    ) -> list[dict]:
        filters: dict = {}
        if source:
            filters["source"] = source
        if doc_type:
            filters["doc_type"] = doc_type
        if year_from:
            filters["year_from"] = year_from
        if tag:
            filters["tag"] = tag
        return facade.search(q, k=k, filters=filters or None)

    # -- write / augment ---------------------------------------------------
    @app.post("/import/file")
    async def import_file_ep(
        file: UploadFile = File(...),
        doc_type: str = Form("commentary"),
        title: str | None = Form(None),
        link_to: str | None = Form(None),
        relationship: str | None = Form(None),
    ) -> dict:
        data = await file.read()
        return facade.import_bytes(
            data=data, filename=file.filename or "upload.bin", doc_type=doc_type,
            title=title, link_to=link_to, relationship=relationship,
        )

    @app.post("/import/url")
    def import_url_ep(payload: dict = Body(...)) -> dict:
        return facade.import_url(**payload)

    @app.post("/import/base64")
    def import_base64_ep(payload: dict = Body(...)) -> dict:
        return facade.import_base64(**payload)

    @app.post("/import/note")
    def import_note_ep(payload: dict = Body(...)) -> dict:
        return facade.add_note(**payload)

    @app.post("/import/zotero")
    def import_zotero_ep(payload: dict = Body(...)) -> dict:
        return facade.import_zotero(**payload)

    @app.get("/zotero/status")
    def zotero_status_ep() -> dict:
        """Connection state + collections — one API key is all setup takes (the
        library id is derived from the key and persisted)."""
        return facade.zotero_status()

    # -- guidance classification: inspectable rules + evidence-carrying fields --
    @app.get("/guidance/rules")
    def guidance_rules_ep() -> dict:
        return facade.guidance_rules()

    @app.post("/guidance/rules")
    def update_guidance_rules_ep(payload: dict = Body(...)) -> dict:
        return facade.update_guidance_rules(payload)

    @app.post("/guidance/classify")
    def classify_guidance_ep(payload: dict = Body(...)) -> dict:
        """Dry-run the classifier (a held doc via stable_id, or pasted title/url/text
        as the rules test-bench) — returns each field with the rule that fired and
        the text it matched. Never writes."""
        return facade.classify_guidance_preview(
            stable_id=payload.get("stable_id"), title=payload.get("title"),
            url=payload.get("url"), text=payload.get("text"))

    @app.post("/guidance/field")
    def set_guidance_field_ep(payload: dict = Body(...)) -> dict:
        """A human's correction of one field (method 'manual' — re-classify never
        overwrites it). Empty value clears the field."""
        return facade.set_guidance_field(
            stable_id=payload["stable_id"], field=payload["field"],
            value=payload.get("value"))

    @app.post("/jobs/classify-guidance")
    def job_classify_guidance_ep() -> dict:
        """Re-classify all guidance with the current rules (the improvement loop)."""
        return _start_job("classify-guidance", "re-classify guidance documents")

    @app.post("/import/case")
    async def import_case_ep(
        file: UploadFile = File(...),
        ref: str | None = Form(None),
        neutral_citation: str | None = Form(None),
        also_cited_as: str | None = Form(None),
        title: str | None = Form(None),
    ) -> dict:
        """Import a judgment file as a first-class case: extract clean text (RTF de-RTF'd),
        detect its own neutral citation from the header, key it by that, and alias every
        other form it's cited by (report citations, chamber-less variant) so they resolve."""
        data = await file.read()
        extra = [a.strip() for a in (also_cited_as or "").split(";") if a.strip()]
        return facade.import_case(data=data, filename=file.filename or "case.pdf", ref=ref,
                                  neutral_citation=neutral_citation, also_cited_as=extra, title=title)

    @app.post("/import/bailii")
    async def import_bailii_ep(
        file: UploadFile = File(...),
        stable_id: str = Form(...),
        title: str | None = Form(None),
    ) -> dict:
        """Accept a manually-downloaded BAILII RTF and store it as a UK judgment.

        The file must be the RTF served by BAILII (e.g. the one linked from the
        ``bailii_url`` field on an unresolved reference). ``stable_id`` must match
        the Find Case Law key already cited in the corpus (e.g. ``ewca/civ/2006/717``)
        — this is what connects the upload to all outstanding citations.
        """
        data = await file.read()
        return facade.import_bailii_file(stable_id=stable_id, data=data, title=title or None)

    @app.post("/import/bailii-zip")
    async def import_bailii_zip_ep(file: UploadFile = File(...)) -> dict:
        """Accept a zip of saved BAILII judgment HTML pages and process it as a
        background job: each page is parsed (slug, case name, date, "Cite as:" list,
        numbered paragraphs) and synthesised with the corpus — new cases imported,
        lower-fidelity copies superseded, authoritative ones enriched with aliases.
        The zip is spooled to disk so the job survives a restart."""
        data = await file.read()
        spool = facade.config.data_dir / "uploads"
        spool.mkdir(parents=True, exist_ok=True)
        path = spool / f"bailii-{_uuid.uuid4().hex[:12]}.zip"
        path.write_bytes(data)
        return jobs.start("import-bailii-zip",
                          f"Import BAILII zip ({file.filename or 'upload.zip'})",
                          {"zip_path": str(path)})

    # No-zip path for a big Finder folder: the browser picks the whole folder and
    # streams the .html files up in batches into a server-side spool directory, then
    # starts ONE background job over that directory. Batching keeps any single request
    # small (thousands of files never fit in one POST) and survives a restart.
    _BAILII_SPOOL_ID = re.compile(r"^[A-Za-z0-9]{6,40}$")

    def _bailii_batch_dir(upload_id: str):
        if not _BAILII_SPOOL_ID.match(upload_id or ""):
            return None  # reject anything that could escape the spool root
        d = facade.config.data_dir / "uploads" / f"bailii-files-{upload_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @app.post("/import/bailii-files")
    async def import_bailii_files_batch_ep(
        upload_id: str = Form(...), files: list[UploadFile] = File(...),
    ) -> dict:
        """Receive one batch of BAILII ``.html`` files into the spool directory keyed by
        ``upload_id``. Call repeatedly to stage a whole folder, then POST
        ``/import/bailii-files/start`` to launch the import. Returns the running count."""
        d = _bailii_batch_dir(upload_id)
        if d is None:
            return JSONResponse({"error": "bad upload_id"}, status_code=400)
        written = 0
        for f in files:
            name = (f.filename or "").rsplit("/", 1)[-1]
            if not name.lower().endswith((".html", ".htm")) or name.startswith("."):
                continue
            # de-dup within a batch selection by content-addressing the name collision
            dest = d / name
            if dest.exists():
                dest = d / f"{_uuid.uuid4().hex[:8]}_{name}"
            dest.write_bytes(await f.read())
            written += 1
        staged = sum(1 for _ in d.glob("*.htm*"))
        return {"upload_id": upload_id, "received": written, "staged": staged}

    @app.post("/import/bailii-files/start")
    def import_bailii_files_start_ep(payload: dict = Body(...)) -> dict:
        """Launch the import over everything staged under ``upload_id``."""
        d = _bailii_batch_dir(payload.get("upload_id", ""))
        if d is None:
            return JSONResponse({"error": "bad upload_id"}, status_code=400)
        staged = sum(1 for _ in d.glob("*.htm*"))
        if not staged:
            return {"error": "no files staged for this upload"}
        return jobs.start("import-bailii-dir",
                          f"Import BAILII folder ({staged} files)",
                          {"dir_path": str(d)})

    @app.post("/jobs/repair-au-cth")
    def job_repair_au_cth_ep(payload: dict = Body(default={})) -> dict:
        """Heal au-cth records an older adapter left incomplete: re-fetch missing bodies
        via the API content endpoint, and mint canonical year/number citation aliases.
        Idempotent and bounded — safe to run any time, does nothing when nothing is wrong."""
        limit = (payload or {}).get("limit")
        params = {"limit": int(limit)} if isinstance(limit, int) else {}
        return _start_job("repair-au-cth", "repair au-cth (bodies + citation aliases)", params)

    @app.post("/import/sg-seed")
    def import_sg_seed_ep(payload: dict = Body(...)) -> dict:
        """Seed Singapore legislation from a server-side SSO parquet snapshot (``dir_path``
        points at the folder holding documents.parquet + sections.parquet). Reconciles the
        truncated seed names to SSO act codes via the live browse listing unless
        ``reconcile: false``."""
        dir_path = (payload.get("dir_path") or "").strip()
        if not dir_path or not os.path.isdir(dir_path):
            return JSONResponse({"error": f"not a directory: {dir_path!r}"}, status_code=400)
        params: dict = {"dir_path": dir_path}
        if isinstance(payload.get("reconcile"), bool):
            params["reconcile"] = payload["reconcile"]
        if isinstance(payload.get("limit"), int):
            params["limit"] = payload["limit"]
        return jobs.start("import-sg-seed", "Seed Singapore legislation (SSO)", params)

    @app.post("/import/indian-sci")
    def import_indian_sci_ep(payload: dict = Body(...)) -> dict:
        """Import the Supreme Court of India slice of a server-side KanoonGPT
        ``indian-case-laws`` parquet dump (``dir_path`` points at ``structured/v1``)."""
        dir_path = (payload.get("dir_path") or "").strip()
        if not dir_path or not os.path.isdir(dir_path):
            return JSONResponse({"error": f"not a directory: {dir_path!r}"}, status_code=400)
        params: dict = {"dir_path": dir_path}
        if isinstance(payload.get("limit"), int):
            params["limit"] = payload["limit"]
        if isinstance(payload.get("extract"), bool):
            params["extract"] = payload["extract"]
        return jobs.start("import-indian-sci", "Import Supreme Court of India", params)

    @app.get("/document-lii-links")
    def document_lii_links_ep(id: str) -> dict:
        """Outbound LII links for one document — what the reader shows when a case is a
        name-only record with no judgment text, so the text can be fetched from the
        institute that publishes it.

        Keyed by query param, not a path segment: stable_ids contain slashes, so a
        ``/documents/{id:path}/lii-links`` route is swallowed whole by the generic
        document route (the same reason ``/document-body?id=`` is shaped this way)."""
        return {"stable_id": id, "links": facade.lii_links_for(id)}

    @app.get("/lii-links")
    def lii_links_ep(scope: str = "unheld", limit: int = 2000,
                     sites: str | None = None) -> dict:
        """The LII fetch worklist: constructed links to cases the corpus cites but cannot
        show. ``scope`` is ``unheld`` | ``textless`` | ``both``."""
        site_list = [s for s in (sites or "").split(",") if s] or None
        rows = facade.lii_link_targets(scope=scope, limit=limit, sites=site_list)
        return {"scope": scope, "count": len(rows), "links": rows}

    @app.get("/lii-links.csv")
    def lii_links_csv_ep(scope: str = "unheld", limit: int = 20000,
                         sites: str | None = None):
        """The same worklist as a CSV download — the aggregate list someone works through
        by hand, saving each page under the ``filename`` column so the companion importer
        can recover each document's identity from the filename alone."""
        import csv as _csv
        import io as _io

        from fastapi.responses import Response

        site_list = [s for s in (sites or "").split(",") if s] or None
        rows = facade.lii_link_targets(scope=scope, limit=limit, sites=site_list)
        cols = ["stable_id", "citation", "title", "status", "citing_count",
                "site", "site_name", "url", "certainty", "filename"]
        buf = _io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return Response(
            content=buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="lii-links-{scope}.csv"'})

    @app.post("/import/bailii-parquet")
    def import_bailii_parquet_ep(payload: dict = Body(...)) -> dict:
        """Launch an import of a **server-side BAILII parquet dump** (a bulk Scrapy crawl
        exported as Parquet shards, mounted on the host — e.g. under ``/corpora/…``). Unlike
        the upload paths this reads a directory already on the box, so it takes a
        ``dir_path`` plus optional ``databases`` / ``exclude_databases`` filters (on the
        dump's ``database_name`` column, e.g. exclude ``UKAITUR`` to skip the asylum bulk).
        Runs as one cancellable background job."""
        dir_path = (payload.get("dir_path") or "").strip()
        if not dir_path or not os.path.isdir(dir_path):
            return JSONResponse({"error": f"not a directory: {dir_path!r}"}, status_code=400)
        params: dict = {"dir_path": dir_path}
        for key in ("databases", "exclude_databases"):
            val = payload.get(key)
            if isinstance(val, list) and val:
                params[key] = [str(v) for v in val]
        # start_row resumes an interrupted run at the row offset the last one reported;
        # extract=False imports only, leaving the (resumable) extraction pass for later.
        for key in ("limit", "start_row", "batch_size"):
            if isinstance(payload.get(key), int):
                params[key] = payload[key]
        if isinstance(payload.get("extract"), bool):
            params["extract"] = payload["extract"]
        return jobs.start("import-bailii-parquet", "Import BAILII parquet dump", params)

    # Westlaw RTF import — the sibling of the BAILII-page path, for the other big source
    # of older UK (and UK-reported EU) judgments. Same zip + batched-folder shape.
    @app.post("/import/westlaw-zip")
    async def import_westlaw_zip_ep(file: UploadFile = File(...)) -> dict:
        """Accept a zip of Westlaw ``.rtf`` case exports and process it as a background
        job: each file is parsed (parties, court, every parallel report citation, judges,
        counsel, digest, numbered paragraphs / star-pages) and synthesised with the
        corpus — keyed by its strongest identity (neutral slug → ECLI → Westlaw id)."""
        data = await file.read()
        spool = facade.config.data_dir / "uploads"
        spool.mkdir(parents=True, exist_ok=True)
        path = spool / f"westlaw-{_uuid.uuid4().hex[:12]}.zip"
        path.write_bytes(data)
        return jobs.start("import-westlaw-zip",
                          f"Import Westlaw zip ({file.filename or 'upload.zip'})",
                          {"zip_path": str(path)})

    def _westlaw_batch_dir(upload_id: str):
        if not _BAILII_SPOOL_ID.match(upload_id or ""):
            return None  # reject anything that could escape the spool root
        d = facade.config.data_dir / "uploads" / f"westlaw-files-{upload_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @app.post("/import/westlaw-files")
    async def import_westlaw_files_batch_ep(
        upload_id: str = Form(...), files: list[UploadFile] = File(...),
    ) -> dict:
        """Receive one batch of Westlaw ``.rtf`` files into the spool directory keyed by
        ``upload_id``. Call repeatedly to stage a whole folder, then POST
        ``/import/westlaw-files/start`` to launch the import."""
        d = _westlaw_batch_dir(upload_id)
        if d is None:
            return JSONResponse({"error": "bad upload_id"}, status_code=400)
        written = 0
        for f in files:
            name = (f.filename or "").rsplit("/", 1)[-1]
            if not name.lower().endswith((".rtf", ".doc")) or name.startswith("."):
                continue
            dest = d / name
            if dest.exists():
                dest = d / f"{_uuid.uuid4().hex[:8]}_{name}"
            dest.write_bytes(await f.read())
            written += 1
        staged = sum(1 for p in d.iterdir() if p.suffix.lower() in (".rtf", ".doc"))
        return {"upload_id": upload_id, "received": written, "staged": staged}

    @app.post("/import/westlaw-files/start")
    def import_westlaw_files_start_ep(payload: dict = Body(...)) -> dict:
        """Launch the import over everything staged under ``upload_id``."""
        d = _westlaw_batch_dir(payload.get("upload_id", ""))
        if d is None:
            return JSONResponse({"error": "bad upload_id"}, status_code=400)
        staged = sum(1 for p in d.iterdir() if p.suffix.lower() in (".rtf", ".doc"))
        if not staged:
            return {"error": "no files staged for this upload"}
        return jobs.start("import-westlaw-dir",
                          f"Import Westlaw folder ({staged} files)",
                          {"dir_path": str(d)})

    # Unified case-law import — one uploader that accepts a mixed folder/zip of saved
    # BAILII .html pages and Westlaw .rtf exports, routing each file to its own parser by
    # extension. This is what the Import UI drives; the source-specific endpoints above
    # stay for CLI/API parity.
    _CASELAW_EXTS = (".html", ".htm", ".rtf", ".doc")

    @app.post("/import/caselaw-zip")
    async def import_caselaw_zip_ep(file: UploadFile = File(...)) -> dict:
        """Accept a zip mixing BAILII ``.html`` pages and Westlaw ``.rtf`` exports; each
        entry is routed to its parser by extension in one background job."""
        data = await file.read()
        spool = facade.config.data_dir / "uploads"
        spool.mkdir(parents=True, exist_ok=True)
        path = spool / f"caselaw-{_uuid.uuid4().hex[:12]}.zip"
        path.write_bytes(data)
        return jobs.start("import-caselaw-zip",
                          f"Import case law zip ({file.filename or 'upload.zip'})",
                          {"zip_path": str(path)})

    def _caselaw_batch_dir(upload_id: str):
        if not _BAILII_SPOOL_ID.match(upload_id or ""):
            return None
        d = facade.config.data_dir / "uploads" / f"caselaw-files-{upload_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @app.post("/import/caselaw-files")
    async def import_caselaw_files_batch_ep(
        upload_id: str = Form(...), files: list[UploadFile] = File(...),
    ) -> dict:
        """Stage one batch of ``.html``/``.htm``/``.rtf`` files under ``upload_id``. Call
        repeatedly to stage a whole folder, then POST ``/import/caselaw-files/start``."""
        d = _caselaw_batch_dir(upload_id)
        if d is None:
            return JSONResponse({"error": "bad upload_id"}, status_code=400)
        written = 0
        for f in files:
            name = (f.filename or "").rsplit("/", 1)[-1]
            if not name.lower().endswith(_CASELAW_EXTS) or name.startswith("."):
                continue
            dest = d / name
            if dest.exists():
                dest = d / f"{_uuid.uuid4().hex[:8]}_{name}"
            dest.write_bytes(await f.read())
            written += 1
        staged = sum(1 for p in d.iterdir() if p.suffix.lower() in _CASELAW_EXTS)
        return {"upload_id": upload_id, "received": written, "staged": staged}

    @app.post("/import/caselaw-files/start")
    def import_caselaw_files_start_ep(payload: dict = Body(...)) -> dict:
        """Launch the mixed import over everything staged under ``upload_id``."""
        d = _caselaw_batch_dir(payload.get("upload_id", ""))
        if d is None:
            return JSONResponse({"error": "bad upload_id"}, status_code=400)
        staged = sum(1 for p in d.iterdir() if p.suffix.lower() in _CASELAW_EXTS)
        if not staged:
            return {"error": "no files staged for this upload"}
        return jobs.start("import-caselaw-dir",
                          f"Import case law folder ({staged} files)",
                          {"dir_path": str(d)})

    @app.post("/documents/{doc_id:path}/attach")
    async def attach_ep(doc_id: str, file: UploadFile = File(...), kind: str = Form("exhibit")) -> dict:
        data = await file.read()
        return facade.attach(doc_id=doc_id, data=data, filename=file.filename or "asset.bin", kind=kind)

    @app.post("/link")
    def link_ep(payload: dict = Body(...)) -> dict:
        return facade.link(**payload)

    @app.post("/tag")
    def tag_ep(payload: dict = Body(...)) -> dict:
        return facade.tag(**payload)

    @app.get("/embed/backlog")
    def embed_backlog_ep() -> dict:
        """How many docs still need indexing in the current embedding family."""
        return facade.embedding_backlog()

    @app.post("/embed")
    def embed_ep(payload: dict = Body(default={})) -> dict:
        """Index/embed documents as a background job (resumable) — returns a job_id so it
        shows progress in the Jobs panel. Pass ``{"sync": true}`` to run inline (small
        batches / scripts), optionally with ``limit``."""
        params = {k: v for k, v in {"limit": payload.get("limit")}.items() if v is not None}
        if payload.get("sync"):
            return facade.embed(**params)
        backlog = facade.embedding_backlog()
        return jobs.start("embed", f"Embed / index ({backlog['pending']} pending)", params)

    @app.post("/resolve")
    def resolve_ep() -> dict:
        return facade.resolve()

    # -- settings (UI-editable secrets; env overrides file) ---------------
    @app.get("/settings")
    def get_settings() -> dict:
        return facade.get_settings()

    @app.post("/settings")
    def update_settings(payload: dict = Body(...)) -> dict:
        return facade.update_settings(payload)

    return app


def _frontend_dist() -> "Path | None":
    """Locate the built React UI (``frontend/dist``) so the API can serve it at the
    same origin — one ``docker compose up`` then gives the whole app on :8000.
    ``RAGLEX_FRONTEND_DIST`` overrides; otherwise probe the usual spots."""
    import os
    from pathlib import Path

    candidates = [os.environ.get("RAGLEX_FRONTEND_DIST"),
                  "/app/frontend/dist",
                  str(Path(__file__).resolve().parents[3] / "frontend" / "dist")]
    for c in candidates:
        if c and (Path(c) / "index.html").exists():
            return Path(c)
    return None


def serve_app(config: Config | None = None) -> FastAPI:
    """The app the ``serve`` command runs: the API, plus the built React UI served
    at the same origin when present (so one ``docker compose up`` is the whole app).
    Unit tests use the bare ``create_app`` instead, so route paths stay stable."""
    from contextlib import asynccontextmanager

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    from ..mcp_server import build_server

    api = create_app(config)
    dist = _frontend_dist()

    # Serve the MCP server at /mcp on this same origin (instead of a second process/port):
    # FastMCP hands back a mountable ASGI app. Its streamable-HTTP endpoint defaults to
    # "/mcp", so point it at "/" and mount the app at "/mcp" → the endpoint lands exactly
    # at /mcp. The sub-app's lifespan (the MCP session manager) doesn't run on its own when
    # mounted, so we thread it into the parent app's lifespan.
    mcp = build_server(config)
    mcp.settings.streamable_http_path = "/"
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(title="RagLex", version="0.1.0", lifespan=lifespan)
    app.mount("/api", api)
    app.mount("/mcp", mcp_app)

    # The mounted app's endpoint is /mcp/ (mount prefix + its "/" route). A client hitting
    # /mcp (no trailing slash) would otherwise fall through to the SPA catch-all below and
    # 405. Redirect /mcp → /mcp/ with 307 (preserves the POST method + body) so the bare
    # URL works for MCP clients. Defined before the catch-all so it wins.
    from starlette.responses import RedirectResponse

    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    async def _mcp_no_slash() -> RedirectResponse:
        return RedirectResponse(url="/mcp/", status_code=307)

    if dist is None:
        return app
    app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

    @app.get("/")
    @app.get("/{_path:path}")  # SPA fallback (tabs are client state, not routes)
    def index(_path: str = "") -> FileResponse:
        return FileResponse(str(dist / "index.html"))

    return app
