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

    @app.post("/jobs/backfill-edge-keys")
    def job_backfill_edge_keys_ep() -> dict:
        """One-off after upgrade: populate candidate_id/raw_fold on pre-existing edges so
        the set-based resolver and the SQL worklist see the whole graph."""
        return _start_job("backfill-edge-keys", "backfill edge candidate ids")

    @app.post("/jobs/rebuild-citation-counts")
    def job_rebuild_counts_ep() -> dict:
        """Refresh the snowball's citation-frequency roll-up."""
        return _start_job("rebuild-citation-counts", "rebuild citation frequency roll-up")

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
        return facade.run_watch(watch_id=watch_id)

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
        )

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

    @app.get("/corpus-map")
    def corpus_map_ep() -> dict:
        """Held-vs-pending by legal category & sub-type — the dashboard coverage table."""
        return facade.corpus_map()

    @app.get("/corpus-map/cites")
    def corpus_map_cites_ep(category: str) -> dict:
        """Lazy: what this category's held docs cite, by target category (unique + total)."""
        return facade.corpus_map_cites(category=category)

    @app.get("/document-body")
    def document_body(id: str) -> dict:
        # query-param route: stable_ids contain slashes (ukpga/2000/36), so a
        # /documents/{id}/body suffix would be ambiguous.
        return facade.document_body(id)

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

    @app.post("/embed")
    def embed_ep(payload: dict = Body(default={})) -> dict:
        return facade.embed(limit=payload.get("limit"))

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
