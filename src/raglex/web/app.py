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

def _cors_origins() -> list[str]:
    raw = os.environ.get("RAGLEX_CORS_ORIGINS")
    if raw:
        return [o.strip() for o in raw.replace(";", ",").split(",") if o.strip()]
    # With auth enabled, refuse cross-origin by default: a permissive CORS policy would let a
    # malicious page preflight-and-forge state-changing requests from an IP-allow-listed
    # browser. Same-origin (the bundled SPA) needs no CORS at all. Open deployments keep the
    # historical wildcard.
    from .auth import auth_enabled
    return [] if auth_enabled() else ["*"]


def create_app(config: Config | None = None) -> FastAPI:
    facade = Facade(config or Config.from_env())
    facade.warm_caches()  # pre-compute heavy dashboard aggregates so first load is instant
    facade.start_daily_refresh()  # nightly 01:00 UK full recompute + re-warm of those caches
    # Everything RagLex logs about itself at WARNING+ also lands in the review queue, beside
    # user feedback and refinement flags, deduplicated by fingerprint (§8, ops/errorlog).
    from ..ops.errorlog import install as _install_errorlog
    _install_errorlog(facade)
    jobs = JobManager(facade, origin="api")
    # A deploy kills in-process workers. Durable/checkpointed API jobs resume under a
    # new attempt automatically; conservative job kinds remain visibly interrupted.
    jobs.reap_orphans(auto_resume=True)
    app = FastAPI(title="RagLex", version="0.1.0", summary="Legal corpus ops + research API")
    # In production the SPA is served from this same origin, so no CORS is exercised;
    # set RAGLEX_CORS_ORIGINS (comma-separated) for a cross-origin front-end, which then
    # gets credentialed CORS (cookies). A wildcard cannot carry credentials, so we only
    # allow credentials when explicit origins are named.
    origins = _cors_origins()
    app.add_middleware(
        CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"],
        allow_credentials=origins != ["*"],
    )
    # Role-based auth (reader/admin passwords, dual IP allow-lists, passkeys) + read-only
    # enforcement. Opt-in: with nothing configured the API stays open (dev/test). The old
    # RAGLEX_API_TOKEN keeps working as an admin bearer token.
    from .auth import install_web_auth
    install_web_auth(app, facade)

    def _start_job(kind: str, label: str, params: dict | None = None, *,
                   queue: bool = False) -> dict:
        return jobs.start(kind, label, params or {}, queue=queue)

    @app.get("/jobs/queue-status")
    def jobs_queue_status_ep() -> dict:
        """The queue's live state for the Jobs/Maintain UI: how many are running vs the
        cap, how many are waiting, and whether the scheduler is paused."""
        import os as _os
        from ..jobs import scheduler_paused
        with facade._open() as (cat, _rs, _ts):
            running = len(cat.running_jobs())
            queued = len(cat.queued_jobs())
        return {"running": running, "queued": queued,
                "max_concurrent": jobs._max_concurrent(),
                "scheduler_paused": scheduler_paused()}

    @app.post("/jobs/scheduler-pause")
    def jobs_scheduler_pause_ep(payload: dict = Body(default={})) -> dict:
        """Toggle "pause all scheduled jobs" (the scheduler's recurring work + due watches).
        Manual and already-queued jobs keep running. Persists RAGLEX_SCHEDULER_PAUSED."""
        paused = bool((payload or {}).get("paused", True))
        facade.update_settings({"RAGLEX_SCHEDULER_PAUSED": "1" if paused else ""})
        return {"scheduler_paused": paused}

    @app.post("/jobs/max-concurrent")
    def jobs_max_concurrent_ep(payload: dict = Body(...)) -> dict:
        """Set how many jobs run at once (extras queue). Persists RAGLEX_MAX_CONCURRENT_JOBS
        and immediately promotes queued jobs if the cap was raised."""
        try:
            n = max(1, int((payload or {}).get("max_concurrent")))
        except (TypeError, ValueError):
            return {"error": "max_concurrent must be a positive integer"}
        facade.update_settings({"RAGLEX_MAX_CONCURRENT_JOBS": str(n)})
        jobs.promote_queued()
        return {"max_concurrent": n}

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

    @app.get("/unresolved")
    def unresolved(limit: int = 100) -> dict:
        """Hanging references the corpus can't satisfy — the manual-resolution queue.
        Cached stale-while-revalidate: returns {rows, _warming?} so the panel never blocks."""
        return facade.unresolved_references_cached(limit=limit)

    @app.get("/unresolved/unfetchable")
    def unfetchable(limit: int = 200, min_citing: int | None = None) -> dict:
        """Most-cited references with NO fetch route — classic law reports, cases by name,
        courts with no adapter — each with a BAILII link + upload-to-resolve.

        ``min_citing`` is the floor on how many documents must cite a reference for it to
        appear (default 2). It is the main cost control: 70% of hanging references are
        cited exactly once, and classifying them to rank them below the fold is the bulk
        of the work. Drop it to 1 to see the whole tail, at the cost of a slower build."""
        return facade.unfetchable_references(limit=limit, min_citing=min_citing)

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

    @app.get("/export/static-law")
    def export_static_law_status_ep(id: str, max_snippets: int = 4) -> dict:
        """Whether a completed static artifact is ready for immediate download."""
        from ..static_export import static_export_status

        status = static_export_status(
            facade.config, id, max_snippets=max(1, min(int(max_snippets), 12)))
        status.pop("_path", None)
        return status

    @app.post("/export/static-law")
    def export_static_law_build_ep(payload: dict = Body(...)) -> dict:
        """Build or refresh an edition as a durable background job.

        GDPR-scale editions read thousands of source texts for their excerpts, so building
        inside the download request would exceed ordinary proxy timeouts.  The completed
        file is cached atomically and subsequent downloads are immediate.
        """
        from ..static_export import static_export_status

        stable_id = str((payload or {}).get("id") or "").strip()
        if not stable_id:
            return JSONResponse({"error": "id is required"}, status_code=422)
        max_snippets = max(1, min(int((payload or {}).get("max_snippets") or 4), 12))
        with facade._open() as (cat, _rs, _ts):
            if cat.get_document(stable_id) is None:
                return JSONResponse({"error": "document not found"}, status_code=404)
        cached = static_export_status(
            facade.config, stable_id, max_snippets=max_snippets)
        if cached.get("ready") and not (payload or {}).get("refresh"):
            cached.pop("_path", None)
            return cached
        return _start_job(
            "static-export",
            f"build static edition of {stable_id}",
            {"stable_id": stable_id, "max_snippets": max_snippets},
        )

    @app.get("/export/static-law.html")
    def export_static_law_download_ep(id: str, max_snippets: int = 4):
        """Download a completed, self-contained law-and-citations HTML file.

        The page is rendered here from the cached payload, so the attribution (and, for a
        bundle item, its own editorial line) is always the CURRENT one — editing either
        never means rebuilding thousands of excerpts."""
        from fastapi.responses import Response

        from ..static_export import render_cached_export, static_export_status

        status = static_export_status(
            facade.config, id, max_snippets=max(1, min(int(max_snippets), 12)))
        if not status.get("ready") or not status.get("_path"):
            return JSONResponse(
                {
                    "error": "static edition is not built",
                    "hint": "POST /export/static-law, then poll its job id",
                },
                status_code=409,
            )
        return Response(
            content=render_cached_export(status),
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{status["filename"]}"',
                "X-Raglex-Export-Documents": str(status["documents"]),
                "X-Raglex-Export-Mentions": str(status["mentions"]),
            },
        )

    # -- static bundle: a whole set of editions + an index, as a folder and a zip ----
    @app.get("/export/bundle")
    def export_bundle_config_ep() -> dict:
        """The configured set, where it publishes, and what the last run did."""
        from ..schedule import list_tasks
        from ..static_bundle import last_run, load_config

        task = next((t for t in list_tasks() if t["name"] == "static-bundle"), None)
        return {**load_config(facade.config), "last_run": last_run(facade.config),
                "schedule": task}

    @app.post("/export/bundle")
    def export_bundle_save_ep(payload: dict = Body(...)) -> dict:
        """Save the set: documents, their filenames, their own lines, the index text."""
        from ..settings import SettingsStore
        from ..static_bundle import save_config

        return save_config(
            SettingsStore(facade.config.settings_path), payload or {}, facade.config)

    @app.post("/export/bundle/build")
    def export_bundle_build_ep(payload: dict = Body(default={})) -> dict:
        """Build every configured edition as a job. Always writes the export folder;
        ``zip`` additionally packs one for download. Static exports skip the job queue,
        so a full harvest queue can't hold the download up."""
        from ..static_bundle import load_config

        if not load_config(facade.config).get("items"):
            return JSONResponse(
                {"error": "no documents are configured for the static bundle"},
                status_code=422)
        params = {
            "zip": bool((payload or {}).get("zip", True)),
            "refresh": bool((payload or {}).get("refresh", True)),
        }
        return _start_job(
            "static-bundle",
            "static export bundle" + ("" if params["refresh"] else " (re-render)"),
            params,
        )

    @app.get("/export/bundle.zip")
    def export_bundle_zip_ep():
        """Download the zip written by the most recent build."""
        from fastapi.responses import FileResponse

        from ..static_bundle import last_run, zip_path

        path = zip_path(facade.config)
        if not path.is_file():
            return JSONResponse(
                {"error": "no bundle has been built",
                 "hint": "POST /export/bundle/build, then poll its job id"},
                status_code=409)
        run = last_run()
        return FileResponse(
            path,
            media_type="application/zip",
            filename=run.get("zip_filename") or "raglex-static-export.zip",
            headers={"Cache-Control": "no-store"},
        )

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
    @app.get("/legislation/status")
    def legislation_status_ep(id: str) -> dict:
        """Currency of an act from its change-edges: in force / amended / repealed / recast /
        corrected + a consolidation snapshot, with the acts that did it (and per-article
        markers where pinpointed). Source-agnostic (UK + EU).

        A held EU base act with no dated expression is self-healing: opening it starts
        one deduplicated Cellar consolidation sync. A completion stamp suppresses repeat
        lookups for seven days when an act genuinely has no consolidation.
        """
        from datetime import datetime as _dt, timezone as _tz

        status = facade.legislative_status(id)
        if (status.get("source") == "eu-legislation"
                and status.get("version_state") == "base_without_consolidation"
                and re.fullmatch(r"3\d{4}[RLD]\d{4}", id or "", re.I)):
            checked = status.get("consolidations_checked_at")
            try:
                checked_at = _dt.fromisoformat(str(checked).replace("Z", "+00:00"))
                if checked_at.tzinfo is None:
                    checked_at = checked_at.replace(tzinfo=_tz.utc)
                recently_checked = (_dt.now(_tz.utc) - checked_at).total_seconds() < 7 * 86400
            except (TypeError, ValueError):
                recently_checked = False
            if not recently_checked:
                status["consolidation_sync"] = jobs.start(
                    "sync-eu-consolidations",
                    f"auto: import consolidations for {id}",
                    {"stable_id": id},
                )
        return status

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
        """(Re)import the official ECHR Convention PDF with article/paragraph anchors."""
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
        scopes it (e.g. just uk-caselaw), far faster since reports are cited by case law.
        ``scope={fr,de,nl}-eu-digital`` is a deliberately bounded digital-acquis refresh:
        only national documents already observed citing one of the reviewed EU digital-law
        CELEX ids, never the complete (and sometimes multi-million-record) corpus.
        ``scope=eu-digital`` is the same worklist without the national filter — every
        document in the corpus, in any jurisdiction, already observed citing one of those
        instruments (~23k). That is the right scope after a change to citation grammars or
        the shorthand rules, which apply everywhere rather than to one country's reports."""
        p = payload or {}
        if p.get("document_ids") is not None:
            document_ids = list(dict.fromkeys(
                str(i) for i in (p.get("document_ids") or [])
                if isinstance(i, str) and i
            ))[:1000]
            return _start_job(
                "rescan-citations",
                f"repair citations in {len(document_ids)} flagged documents",
                {"document_ids": document_ids},
                queue=bool(p.get("queue")),
            )
        digital_scopes = {
            # no source filter: the acquis citer set across every jurisdiction held
            "eu-digital": {
                "label": "re-scan EU digital-acquis citations (all jurisdictions)",
            },
            "fr-eu-digital": {
                "label": "re-scan French EU digital-acquis citations",
                "source_prefix": "fr-",
            },
            "de-eu-digital": {
                "label": "re-scan German EU digital-acquis citations",
                "sources": ["de-rii", "de-neuris"],
            },
            "nl-eu-digital": {
                "label": "re-scan Dutch EU digital-acquis citations",
                "sources": ["nl-rechtspraak"],
            },
        }
        if p.get("scope") in digital_scopes:
            from ..facade import EU_DIGITAL_ACQUIS_IDS

            scope = digital_scopes[p["scope"]]
            return _start_job(
                "rescan-citations",
                scope["label"],
                {
                    **{k: v for k, v in scope.items() if k != "label"},
                    "target_ids": list(EU_DIGITAL_ACQUIS_IDS),
                },
                queue=bool(p.get("queue")),
            )
        label = f"re-scan {p['source']} for new citations" if p.get("source") \
            else "re-scan corpus for new citations"
        return _start_job(
            "rescan-citations", label,
            {"source": p["source"]} if p.get("source") else {},
            queue=bool(p.get("queue")),
        )

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
        # A full relink defaults to skipping anything already re-extracted in the last
        # week: that's the weekly-maintenance cadence, and without it a re-launched or
        # repeated run redoes the whole corpus instead of advancing into what's stale.
        # The resume set (only_unextracted) is by definition never-scanned, so it isn't
        # date-filtered. Pass stale_days=0 to force a genuine redo of everything.
        if "stale_days" not in params and not params.get("only_unextracted"):
            params["stale_days"] = 7
        if params.get("stale_days") in (0, "0"):
            params.pop("stale_days")          # explicit override → redo everything
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

    @app.post("/jobs/backfill-eu-stubs")
    def job_backfill_eu_stubs_ep(payload: dict = Body(default={})) -> dict:
        """Re-fetch EU instruments held only as metadata stubs. A transient failure at
        harvest time left ~7,400 acts with no text and nothing ever retried them —
        including heavily-cited ones (31987D0373, cited 45×) whose HTML was there all
        along. Re-runnable; instruments still absent upstream are left as they are."""
        return _start_job("backfill-eu-stubs", "re-fetch EU metadata-only stubs",
                          {"limit": int((payload or {}).get("limit") or 500)})

    @app.post("/jobs/rebuild-citation-counts")
    def job_rebuild_counts_ep() -> dict:
        """Refresh the citation-frequency roll-up the worklist ranking reads."""
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

    # -- free-text search -----------------------------------------------------
    @app.get("/freetext")
    def freetext_ep(q: str, exact: bool = True, limit: int = 25, offset: int = 0,
                    source: str | None = None, doc_type: str | None = None,
                    court: str | None = None, jurisdiction: str | None = None,
                    year_from: int | None = None) -> dict:
        """Free-text search over the gated scope.

        ``exact`` (the default) makes a quoted phrase mean the literal characters:
        the tsvector narrows, then the string is checked against the document's own
        text. Postgres stems, so without that check "duty of care" also returns
        "duties of care"."""
        split = lambda v: [x for x in (v or "").split(",") if x]  # noqa: E731
        return facade.freetext_search(
            q, exact=exact, limit=min(limit, 100), offset=max(offset, 0),
            sources=split(source) or None, doc_type=split(doc_type) or None,
            court=split(court) or None, jurisdictions=split(jurisdiction) or None,
            year_from=year_from)

    @app.get("/system/text-storage")
    def text_storage_ep() -> dict:
        """Where each source's text physically lives when the store is split across a
        fast local root and a remote one."""
        return facade.text_storage()

    @app.post("/jobs/localise-text")
    def job_localise_text_ep(payload: dict = Body(default={})) -> dict:
        """Copy the text of the given ``sources`` onto the local store, so free-text
        verification and snippets read at memory speed rather than over the mount."""
        params = {k: v for k, v in (payload or {}).items() if k in ("sources", "limit")}
        return _start_job("localise-text", "copy text to local storage", params)

    @app.get("/search/status")
    def search_status_ep() -> dict:
        """Both retrieval paths in one picture: what the free-text index covers, what
        the embedding pass covers, and the scope settings behind each."""
        return facade.search_status()

    @app.post("/freetext/hydrate")
    def freetext_hydrate_ep(payload: dict = Body(...)) -> dict:
        """Snippets and anchors for one page of an already-narrowed result set."""
        return facade.freetext_hydrate(
            ids=(payload or {}).get("ids") or [],
            query=(payload or {}).get("q") or "",
            exact=bool((payload or {}).get("exact", True)))

    @app.post("/freetext/cites-filter")
    def freetext_cites_filter_ep(payload: dict = Body(...)) -> dict:
        """Which of a result set cite a given authority — on demand, because
        pre-computing it for every candidate target dominated the search."""
        return facade.freetext_cites_filter(ids=(payload or {}).get("ids") or [],
                                            target=(payload or {}).get("target") or "")

    @app.get("/freetext/coverage")
    def freetext_coverage_ep() -> dict:
        """What the free-text index currently covers, by jurisdiction — for the note
        under the search box. Cheap: it walks the index, not the corpus."""
        return facade.freetext_index_summary()

    @app.get("/freetext/scope")
    def freetext_scope_ep() -> dict:
        """What the free-text index covers, what it could cover, and the note shown
        under the search box."""
        return facade.freetext_scope()

    @app.post("/freetext/scope")
    def set_freetext_scope_ep(payload: dict = Body(default={})) -> dict:
        return facade.set_freetext_scope(
            sources=(payload or {}).get("sources"), note=(payload or {}).get("note"))

    @app.post("/jobs/build-fts")
    def job_build_fts_ep(payload: dict = Body(default={})) -> dict:
        """Build (or extend) the free-text index. Resumable — an already-indexed
        document is skipped unless ``reindex``."""
        params = {k: v for k, v in (payload or {}).items()
                  if k in ("sources", "reindex", "limit")}
        return _start_job("build-fts", "build free-text index", params)

    # -- learned shorthands ---------------------------------------------------
    @app.get("/shorthands")
    def list_shorthands_ep(q: str | None = None, candidate_id: str | None = None,
                           state: str = "all", limit: int = 100,
                           offset: int = 0) -> dict:
        """The corpus-wide learned-shorthand store, for review. ``state=invalid``
        lists the rows that would not be learned today — the accumulated junk."""
        return facade.browse_shorthands(query=q, candidate_id=candidate_id,
                                        state=state, limit=min(limit, 500),
                                        offset=max(offset, 0))

    @app.post("/shorthands/set")
    def set_shorthand_ep(payload: dict = Body(...)) -> dict:
        """Block/unblock a shorthand, or change whether it links on a bare mention.
        Blocking (rather than deleting) is what sticks: the store is insert-only, so a
        deleted name is simply re-learned the next time its document is rescanned."""
        fields = {k: v for k, v in (payload or {}).items()
                  if k in ("blocked", "is_abbrev", "entity_kind")}
        for flag in ("blocked", "is_abbrev"):
            if flag in fields:
                fields[flag] = 1 if fields[flag] else 0
        return facade.set_shorthand(shorthand=payload["shorthand"],
                                    candidate_id=payload["candidate_id"], **fields)

    @app.post("/shorthands/delete")
    def delete_shorthand_ep(payload: dict = Body(...)) -> dict:
        return facade.delete_shorthand(shorthand=payload["shorthand"],
                                       candidate_id=payload["candidate_id"])

    @app.post("/shorthands/purge-invalid")
    def purge_shorthands_ep(payload: dict = Body(default={})) -> dict:
        """Delete every stored shorthand that would not be learned today. Defaults to a
        dry run — pass ``{"dry_run": false}`` to actually delete."""
        return facade.purge_shorthands(dry_run=bool((payload or {}).get("dry_run", True)))

    @app.post("/feedback")
    def submit_feedback_ep(payload: dict = Body(...)) -> dict:
        """Record a Bug / Feature request from the app's feedback box, with the page context
        (route, doc id, query, role, user-agent) the client captured as ``metadata``."""
        return facade.submit_feedback(
            kind=payload.get("kind", "bug"), message=payload.get("message", ""),
            page=payload.get("page"), url=payload.get("url"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None)

    @app.get("/feedback")
    def list_feedback_ep(status: str | None = "open", limit: int = 500,
                         kind: str | None = None) -> list[dict]:
        """The review queue: user Bugs / Feature requests and the system's own errors
        (``kind=error``), newest-seen first. A repeating system error is ONE row with a
        ``seen_count``, not one row per occurrence."""
        return facade.list_feedback(status=status or None, limit=limit, kind=kind or None)

    @app.post("/feedback/{feedback_id}/status")
    def set_feedback_ep(feedback_id: int, payload: dict = Body(default={})) -> dict:
        return facade.resolve_feedback(
            feedback_id=feedback_id, status=(payload or {}).get("status", "resolved"))

    @app.post("/unresolved/retry-failed")
    def retry_failed_ep() -> dict:
        """Clear the harvest cool-down lists so the next drain re-attempts every reference."""
        return facade.retry_failed_references()

    @app.post("/unresolved/harvest-all")
    def harvest_all_ep(payload: dict = Body(default={})) -> dict:
        """Drain every routable, high-confidence hanging reference, then resolve once.

        Arguments are whitelisted rather than splatted: an unrecognised key used to reach
        the facade as a keyword and blow the whole drain up before it fetched anything."""
        p = payload or {}
        kw = {k: p[k] for k in ("limit", "min_citing", "adapter", "leg_kind",
                                "retry_cooled") if k in p}
        return facade.harvest_all_references(**kw)

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

    @app.post("/jobs/reparse-source")
    def job_reparse_source_ep(payload: dict = Body(...)) -> dict:
        """Reparse a whole source's held documents from their stored raw (a parser
        upgrade reaching the corpus) — a tracked, cancellable, resumable background job
        with per-document progress, unlike the fire-and-forget CLI path."""
        source = (payload or {}).get("source")
        if not source:
            return {"error": "source is required"}
        params: dict = {"source": str(source)}
        if payload.get("workers") is not None:
            params["workers"] = int(payload["workers"])
        return _start_job("reparse-source", f"reparse {source} from raw", params, queue=bool(payload.get("queue")))

    @app.post("/jobs/reanchor-citations")
    def job_reanchor_citations_ep(payload: dict = Body(...)) -> dict:
        """Re-anchor a source's stored citation offsets to its current text — the light
        repair after a reparse regenerated text without re-extraction (offsets drift, so
        the reader highlights the wrong span). No grammar, no re-resolution: only
        char_start/char_end move. Tracked, cancellable, resumable from the stable_id
        cursor. New reparses do this inline, so this is for corpora reparsed earlier."""
        source = (payload or {}).get("source")
        if not source:
            return {"error": "source is required"}
        court = (payload or {}).get("court")
        label = f"re-anchor {source} citations"
        if court:
            label += f" ({court})"
        return _start_job("reanchor-citations", label,
                          {"source": str(source), **({"court": str(court)} if court else {})},
                          queue=bool(payload.get("queue")))

    @app.post("/jobs/match-reports")
    def job_match_reports_ep() -> dict:
        """Match reporter-only citations ("[1998] AC 1") to harvested cases by name+year."""
        return _start_job("match-reports", "match reporter citations to harvested cases")

    @app.post("/jobs/pull-ag-opinions")
    def job_pull_ag_ep() -> dict:
        """Pull the AG Opinion for every held CJEU judgment that lacks one. Background job."""
        return _start_job("pull-ag-opinions", "pull AG opinions for held CJEU cases")

    @app.post("/jobs/backfill-eu-case-names")
    def job_backfill_eu_case_names_ep(payload: dict = Body(default={})) -> dict:
        """Pull CJEU case names + subject tags from the EUR-Lex webservice, as a job.

        It walks every held eu-cellar case and makes an external, credentialed call per 50
        of them, so it belongs in the queue with the other long upkeep — run synchronously
        it blocked the request until the whole batch finished. The scheduler runs the same
        job daily when the ``eu-case-names`` task is enabled."""
        params: dict = {}
        for key in ("limit", "reset_misses"):
            if (payload or {}).get(key) is not None:
                params[key] = payload[key]
        return _start_job("backfill-eu-case-names", "EU case names + subjects (EUR-Lex)",
                          params)

    @app.post("/jobs/backfill-ag-names")
    def job_backfill_ag_names_ep(payload: dict = Body(default={})) -> dict:
        """Fill in who delivered each held AG Opinion (CELLAR's structured relation, with
        the Opinion's printed heading as the fallback). Local + one batched SPARQL per 200."""
        limit = (payload or {}).get("limit")
        return _start_job("backfill-ag-names", "AG names for held Opinions",
                          {"limit": int(limit)} if isinstance(limit, int) else {})

    @app.post("/jobs/repair-mojibake")
    def job_repair_mojibake_ep(payload: dict = Body(default={})) -> dict:
        """Repair Windows-1252 punctuation mis-decoded into the C1 control block (the
        empty-rectangle glyphs). 1:1, so no citation offset moves; scope with ``source``."""
        params = {k: v for k, v in (payload or {}).items() if k in ("source", "limit")}
        return _start_job("repair-mojibake", "repair mis-decoded text", params)

    @app.post("/jobs/repair-de-citations")
    def job_repair_de_citations_ep(payload: dict = Body(default={})) -> dict:
        """Re-validate every German citation against the current German grammar and drop
        the phantoms it would no longer mint (a law abbreviation read off an ordinary
        German word, a docket read off a report series or the next court in a header).
        Pending edges only — a resolved German link is never cut. ``dry_run`` counts."""
        params = {"dry_run": True} if (payload or {}).get("dry_run") else {}
        return _start_job("repair-de-citations", "re-validate German citations", params)

    @app.post("/jobs/repair-de-renditions")
    def job_repair_de_renditions_ep(payload: dict = Body(default={})) -> dict:
        """Fold a second German register's copies of judgments the corpus already holds
        back into the originals: re-point the docket alias, move resolved edges, record
        the copy as a rendition, delete the duplicate. ``dry_run`` counts."""
        params = {k: (payload or {})[k] for k in ("source", "dry_run") if k in (payload or {})}
        return _start_job("repair-de-renditions", "fold duplicate German renditions", params)

    @app.post("/jobs/repair-eu-repeals")
    def job_repair_eu_repeals_ep(payload: dict = Body(default={})) -> dict:
        """Re-ask CELLAR which of an EU act's stored "repeals" edges were only
        ``implicitly_repeals`` — the predicate that marks an act superseding a REFERENCE
        to another, and that currently has 14,294 held acts reading as repealed."""
        params = {k: (payload or {})[k] for k in ("limit", "workers", "dry_run")
                  if k in (payload or {})}
        return _start_job("repair-eu-repeals", "re-type EU repeal edges", params)

    @app.post("/jobs/repair-eu-annexes")
    def job_repair_eu_annexes_ep(payload: dict = Body(default={})) -> dict:
        """Inspect every held EU Formex ZIP and reparse packages whose annexes were
        split into secondary XML members. Local-only, citation-safe, checkpointed."""
        params = {k: (payload or {})[k] for k in ("limit", "after_stable_id")
                  if k in (payload or {})}
        return _start_job(
            "repair-eu-annexes",
            "repair split EU Formex annexes",
            params,
            queue=bool((payload or {}).get("queue")),
        )

    @app.post("/jobs/backfill-eu-consolidations")
    def job_backfill_eu_consolidations_ep(payload: dict = Body(default={})) -> dict:
        """Walk Cellar's complete sector-0 catalogue and import every dated EU
        consolidation, including future-effective snapshots. The underlying harvest
        checkpoint is its Cellar OFFSET, so interrupted walks resume without replay."""
        payload = payload or {}
        params = {
            "source": "eu-legislation",
            "backfill": True,
            "max_pages": (int(payload["max_pages"])
                          if payload.get("max_pages") is not None else None),
            "options": {"consolidations_only": "true"},
            "force_full": True,
            "resume_unfinished": True,
        }
        return _start_job(
            "harvest-source",
            "backfill all EU dated consolidations (CELLAR)",
            params,
            queue=bool(payload.get("queue")),
        )

    @app.post("/jobs/backfill-intituling")
    def job_backfill_intituling_ep(payload: dict = Body(default={})) -> dict:
        """Record who decided each held judgment (and who argued it), read off its own
        first page. Scope with ``source`` (default uk-caselaw)."""
        params = {k: v for k, v in (payload or {}).items() if k in ("source", "limit")}
        return _start_job("backfill-intituling", "bench + counsel from judgment headers",
                          params)

    @app.post("/jobs/resegment-judgments")
    def job_resegment_judgments_ep(payload: dict = Body(default={})) -> dict:
        """Recompute paragraph structure from already-stored text, so segmentation
        improvements reach documents imported before them. Rewrites only the segment
        index, never the text, so no citation offset moves; scope with ``source``."""
        params = {k: v for k, v in (payload or {}).items() if k in ("source", "limit")}
        return _start_job("resegment-judgments", "recompute paragraph structure", params)

    @app.post("/backfill-titles")
    def backfill_titles_ep(payload: dict = Body(default={})) -> dict:
        """Fill missing CJEU case names from CELLAR (synchronous; the job endpoint above is
        the one the UI and scheduler use)."""
        return facade.backfill_titles(**(payload or {}))

    @app.post("/jobs/backfill-metadata")
    def job_backfill_metadata_ep() -> dict:
        """Repair stored docs from raw: UK court from slug, re-parse ruling-only CJEU
        judgments, derive CJEU titles from the Formex parties. Runs as a job."""
        return _start_job("backfill-metadata", "repair court/title/ruling-only metadata")

    # -- background jobs (so long ops report progress instead of blocking) --
    @app.post("/jobs/harvest-all")
    def job_harvest_all_ep(payload: dict = Body(default={})) -> dict:
        """Drain the routable hanging-reference worklist as a background job.

        Only the drain's OWN arguments become job params: the whole payload used to be
        passed through, so a caller that also said ``queue: true`` (the ordinary way to
        ask for a queued job) had "queue" handed to harvest_all_references as a keyword
        and the job died on `unexpected keyword argument 'queue'` before fetching
        anything."""
        p = payload or {}
        params = {k: p[k] for k in ("limit", "min_citing", "adapter", "leg_kind",
                                    "retry_cooled") if k in p}
        return _start_job("harvest-all", "harvest all routable references", params,
                          queue=bool(p.get("queue")))

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
        """Resume a finished/interrupted job under a linked attempt. Citation scans use
        committed per-document run markers; imports restart discovery and deduplicate
        durable outputs. A live worker is cancelled cooperatively before its replacement
        starts, so two attempts never write the same scope concurrently."""
        return jobs.restart(job_id)

    # -- watches (saved harvest plans + scheduler, §5a) --------------------
    @app.get("/sources/catalog")
    def source_catalog_ep() -> list[dict]:
        """Per-source capabilities — drives the morphing harvest/watch UI."""
        return facade.source_catalog()

    @app.get("/sources/keep-current")
    def keep_current_ep() -> dict:
        """Keep-current diagnosis: per-source incremental mode, watch wiring + cadence,
        held counts, failure state, and recent-run stats — grouped by jurisdiction."""
        return facade.keep_current_overview()

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
        per-stage progress (harvest → discover → fetch cited authorities → tag)."""
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
        if payload.get("refetch_held"):
            params["refetch_held"] = True
        if payload.get("use_llm") is not None:
            params["use_llm"] = bool(payload["use_llm"])
        scope = "everything" if params["max_pages"] is None else f"{params['max_pages']} page(s)"
        verb = "backfill" if params["backfill"] else "harvest"
        return _start_job("harvest-source", f"{verb} {source} — {scope}", params, queue=bool(payload.get("queue")))

    @app.post("/jobs/finish-bulk-postprocess")
    def job_finish_bulk_postprocess_ep(payload: dict = Body(default={})) -> dict:
        """Finish an interrupted bulk import's resolve/tag phases WITHOUT re-running
        discovery or citation extraction — batched relation ranges with a persisted
        cursor, then one idempotent tagging pass over the source. The recovery job for
        a large harvest (DILA, RII/GII, Rechtspraak) whose post-processing was cancelled
        or died: extraction work already stored durably is never repeated."""
        payload = payload or {}
        params: dict = {}
        if payload.get("source"):
            params["source"] = str(payload["source"])
        for key in ("resolve", "tag"):
            if payload.get(key) is not None:
                params[key] = bool(payload[key])
        # No source scope → resolve-only by default: an unscoped tag pass would walk
        # EVERY text document in the corpus, which is never what "finish this import"
        # means. An explicit ``tag: true`` still allows it.
        if "source" not in params and "tag" not in params:
            params["tag"] = False
        if payload.get("batch_size"):
            params["batch_size"] = int(payload["batch_size"])
        label = f"finish bulk post-processing — {params.get('source') or 'whole graph'}"
        return _start_job("finish-bulk-postprocess", label, params)

    @app.get("/health/embedding")
    def embedding_health() -> dict:
        return facade.provider_health()

    @app.get("/system/storage")
    def system_storage_ep() -> dict:
        """Database disk footprint (total + largest tables) for the Maintain page."""
        return facade.system_storage()

    @app.get("/system/db-health")
    def system_db_health_ep() -> dict:
        """DB diagnostics for a sluggish box: planner-stat freshness, bloat, seq-scan-heavy
        tables, unused indexes, cache hit ratio, connections, long queries + hints."""
        return facade.db_health()

    @app.post("/system/db-maintenance")
    def system_db_maintenance_ep(payload: dict = Body(default={})) -> dict:
        """Run ANALYZE (and optionally VACUUM ANALYZE) to refresh planner stats / reclaim
        bloat. Admin-only (write)."""
        p = payload or {}
        return facade.db_maintenance(analyze=bool(p.get("analyze", True)),
                                     vacuum=bool(p.get("vacuum", False)))

    @app.get("/scheduled-tasks")
    def scheduled_tasks_ep() -> dict:
        """Recurring scheduler tasks with per-task enabled/cadence (for the on/off UI)."""
        return facade.list_scheduled_tasks()

    @app.post("/scheduled-tasks")
    def set_scheduled_task_ep(payload: dict = Body(...)) -> dict:
        """Toggle/adjust one scheduler task: {name, enabled?, every_minutes?, remove?}."""
        p = payload or {}
        return facade.set_scheduled_task(
            p["name"], enabled=p.get("enabled"), every_minutes=p.get("every_minutes"),
            remove=bool(p.get("remove")))

    @app.post("/jobs/maintenance")
    def job_maintenance_ep(payload: dict = Body(default={})) -> dict:
        """Run the serial DB-maintenance + repair pass as one background job (one task at a
        time). Body may scope it: {no_repairs, no_rescans, no_rollups, sources, steps}."""
        return _start_job("maintenance-run", "DB maintenance + repair", dict(payload or {}))

    @app.post("/jobs/enrich-eu-legislation")
    def job_enrich_eu_leg_ep(payload: dict = Body(default={})) -> dict:
        """Harvest EU acts' act-to-act CDM relationships (repeals/amends/corrects/legal-basis)
        so old directives learn they were repealed/recast. Body: {limit, workers, queue}.
        ``limit`` defaults high enough to drain the whole backlog; the SPARQL lookups run
        across ``workers`` threads (network-bound)."""
        p = payload or {}
        params = {"limit": int(p.get("limit", 100000)), "workers": int(p.get("workers", 8))}
        return _start_job("enrich-eu-legislation", "enrich EU legislation (CELLAR relations)",
                          params, queue=bool(p.get("queue")))

    @app.get("/maintenance/plan")
    def maintenance_plan_ep() -> dict:
        """Preview what a maintenance pass would do (the ordered step queue)."""
        return facade.maintenance_plan()

    @app.post("/jobs/hpc-embed")
    def job_hpc_embed_ep(payload: dict = Body(default={})) -> dict:
        """Drive the Myriad bulk-embed relay as a background job. Dry-run unless
        ``{"go": true}`` — a paid GPU submission is always explicit."""
        p = payload or {}
        params = {k: p[k] for k in ("go", "pilot", "model", "dimensions", "ntasks", "out")
                  if k in p}
        return _start_job("hpc-embed",
                          "HPC embed relay" + ("" if p.get("go") else " (dry-run)"), params)

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

    @app.post("/corpus-map/refresh")
    def corpus_map_refresh_ep() -> dict:
        """Force a background recompute of the corpus map (the '↻ refresh table' button)."""
        return facade.refresh_corpus_map()

    @app.get("/corpus-map/cites")
    def corpus_map_cites_ep(category: str) -> dict:
        """Lazy: what this category's held docs cite, by target category (unique + total)."""
        return facade.corpus_map_cites(category=category)

    @app.get("/mentions")
    def mentions(id: str, anchor: str | None = None, sort: str = "pagerank",
                 exact: bool = False, offset: int = 0, limit: int = 40,
                 jurisdiction: str | None = None, kind: str | None = None) -> dict:
        """Who mentions this document (optionally one paragraph), grouped by citing document
        and ranked by the citer's own authority — for the "Mentioned by …" line + tray.
        Paginated (``offset``/``limit``) so the tray lazy-loads previews for every citer.
        ``exact`` restricts an anchor to that precise pinpoint (a sub-paragraph badge)
        rather than the whole provision family.

        ``jurisdiction`` / ``kind`` narrow the citing set at the SERVER, so selecting a
        facet chip pages through that whole slice rather than sieving the loaded page —
        ``facets`` in the reply always describes the unfiltered set."""
        return facade.document_mentions(id, anchor=anchor, exact=exact, sort=sort,
                                        jurisdiction=jurisdiction, kind=kind,
                                        offset=max(0, offset), limit=max(1, min(limit, 200)))

    @app.get("/cited-by-breakdown")
    def cited_by_breakdown_ep(id: str) -> dict:
        """Facet counts (jurisdiction × kind) over EVERY resolved citer of a document —
        honest numbers for the cited-by chips, which the loaded top slice can't give."""
        return facade.cited_by_breakdown(id)

    @app.get("/cited-by-slice")
    def cited_by_slice_ep(id: str, jurisdiction: str, kind: str | None = None,
                          limit: int = 60) -> dict:
        """Top citers of a document from one jurisdiction (× kind), PageRank-ordered —
        the server-side fetch behind clicking a facet chip whose documents fall outside
        the globally-loaded slice."""
        return facade.cited_by_slice(id, jurisdiction=jurisdiction, kind=kind,
                                     limit=min(int(limit), 200))

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

    @app.post("/import/legislation-akn")
    async def import_legislation_akn_ep(
        file: UploadFile = File(...),
        stable_id: str | None = Form(None),
    ) -> dict:
        """Import a hand-supplied Akoma Ntoso legislation file (e.g. an act
        legislation.gov.uk won't serve). Keys under the AKN's own FRBRWork id, or a
        supplied one (ukpga/2006/46). Full structural parse — schedules and all."""
        data = await file.read()
        return facade.import_legislation_akn(
            data=data, stable_id=stable_id, filename=file.filename)

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

    @app.get("/reference-lii-links")
    def reference_lii_links_ep(ref: str, raw: str | None = None) -> dict:
        """Outbound LII links for a reference that is NOT held — what the peek sidebar shows
        when an unfetched/unfetchable case is clicked, so the user can read it on the
        institute that publishes it (or find it there and upload it). ``ref`` is the
        reference id / neutral-citation slug; ``raw`` the citation as written."""
        return facade.reference_links(ref=ref, raw=raw)

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
    # .zip is accepted here too so a batch of Westlaw/BAILII zips can be staged into ONE
    # folder and imported as a SINGLE job (import_caselaw_dir unpacks each staged zip),
    # instead of one job — and one corpus-wide roll-up — per zip.
    _CASELAW_EXTS = (".html", ".htm", ".rtf", ".doc", ".zip")

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

    @app.get("/provision-mappings")
    def provision_mappings_ep(id: str) -> dict:
        return facade.provision_mappings(stable_id=id)

    @app.post("/provision-mappings")
    def upsert_provision_mappings_ep(payload: dict = Body(...)) -> dict:
        return facade.upsert_provision_mappings(**payload)

    @app.delete("/provision-mappings/{mapping_id}")
    def delete_provision_mapping_ep(mapping_id: int) -> dict:
        return facade.delete_provision_mapping(mapping_id=mapping_id)

    @app.get("/provision-mappings/inherited")
    def inherited_provision_mentions_ep(
        id: str, current_anchor: str | None = None, limit: int = 600,
    ) -> dict:
        return facade.inherited_provision_mentions(
            stable_id=id, current_anchor=current_anchor, limit=limit)

    @app.post("/link-at-selection")
    def link_at_selection_ep(payload: dict = Body(...)) -> dict:
        """Highlight-to-link: anchor a manual citation at a selected span so it renders
        inline in the reader and survives re-extraction."""
        return facade.link_at_selection(**payload)

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

    @app.get("/sources/us-caselaw/budget")
    def us_caselaw_budget() -> dict:
        """CourtListener's remaining quota + the US backlog waiting on it.

        Its own endpoint rather than a field on ``/sources``: US case law is the only
        source with a hard daily ceiling, and the dashboard polls ``/sources`` often —
        counting pending references on every poll would make a cheap call expensive for
        one source's benefit.
        """
        return facade.us_caselaw_budget()

    @app.get("/sources/ca-canlii/budget")
    def canlii_budget_ep() -> dict:
        """CanLII's remaining metered quota + the Canadian backlogs waiting on it
        (pending citations to resolve into stubs, held docs to enrich)."""
        return facade.canlii_budget()

    @app.post("/jobs/canlii-enrich")
    def canlii_enrich_ep(payload: dict = Body(default={})) -> dict:
        """Enrich held Canadian decisions from the CanLII API (permalinks, docket,
        keywords, citator edges) as a background job — budget-metered and resumable."""
        params = {k: v for k, v in {"limit": payload.get("limit"),
                                    "include_citing": payload.get("include_citing")}.items()
                  if v is not None}
        return jobs.start("canlii-enrich", "CanLII enrich (Canadian metadata + citator)",
                          params)

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
    # the SDK hands back a mountable ASGI app. Its streamable-HTTP endpoint defaults to
    # "/mcp", so point it at "/" and mount the app at "/mcp" → the endpoint lands exactly
    # at /mcp. The sub-app's lifespan (the MCP session manager) doesn't run on its own when
    # mounted, so we thread it into the parent app's lifespan.
    mcp = build_server(config)
    # The MCP SDK's DNS-rebinding guard trusts only localhost by default, so a request
    # arriving with the public Host header (behind the reverse proxy) is rejected 421 —
    # even after OAuth succeeds. Trust the RAGLEX_PUBLIC_URL host (+ localhost) so remote
    # MCP clients (e.g. the Claude app via the public domain) can actually connect.
    from mcp.server.transport_security import TransportSecuritySettings

    from .mcp_oauth import public_base_url as _pub_base
    _hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]:*"]
    _origins = ["http://localhost:*", "http://127.0.0.1:*"]
    _pub = _pub_base()
    if _pub:
        from urllib.parse import urlparse
        _host = urlparse(_pub).netloc
        if _host:
            _hosts += [_host, f"{_host}:*"]
            _origins += [_pub, f"{_pub}:*"]
    # SDK v2 configures the transport on the call rather than through a settings
    # object (pydantic-settings is gone). stateless_http is the 2026-07-28 protocol's
    # own shape and it suits this server exactly: every tool is a request/response
    # call into the facade, there is no Context, no progress reporting, no
    # elicitation or sampling, and citing_documents was already documented as
    # stateless and re-callable. Nothing here needs a session to survive between
    # requests, so nothing has to be kept alive between them.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=_hosts, allowed_origins=_origins),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(title="RagLex", version="0.1.0", lifespan=lifespan)
    app.mount("/api", api)
    app.mount("/mcp", mcp_app)

    # Interactive-priority: stamp genuine user-facing reads (the UI's data calls + MCP
    # requests) so background job workers yield the box's scarce IO to them while a user is
    # active (see raglex.interactive — the fix for document opens hanging behind a graph
    # rebuild). The high-frequency pollers are EXCLUDED: the Jobs panel's /api/jobs and
    # /api/health tick ~1/s, and stamping them would hold the quiet window open forever and
    # starve every job. Static assets and the SPA shell aren't DB reads, so they don't stamp.
    from .. import interactive as _interactive

    _POLL_PREFIXES = ("/api/jobs", "/api/health")

    @app.middleware("http")
    async def _mark_interactive(request, call_next):
        path = request.url.path
        if ((path.startswith("/mcp") or path.startswith("/api/"))
                and not path.startswith(_POLL_PREFIXES)):
            _interactive.note_interactive()
        return await call_next(request)

    # MCP OAuth (opt-in): the consent page + the root-level well-known metadata that the
    # mounted sub-app can't serve at the origin root. No-op when OAuth is disabled.
    _provider = getattr(mcp, "_raglex_oauth_provider", None)
    if _provider is not None:
        from .mcp_oauth import install_mcp_oauth_routes
        install_mcp_oauth_routes(app, _provider, mcp_app)

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
    # Vite copies public/ (the bundled circle-flags SVGs) to the dist root; mount it so
    # /flags/gb.svg serves the file instead of falling through to the SPA catch-all.
    if (dist / "flags").is_dir():
        app.mount("/flags", StaticFiles(directory=str(dist / "flags")), name="flags")

    @app.get("/")
    @app.get("/{_path:path}")  # SPA fallback (tabs are client state, not routes)
    def index(_path: str = "") -> FileResponse:
        return FileResponse(str(dist / "index.html"))

    return app
