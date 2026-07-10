"""RagLex command-line entry point.

    raglex run <source> [--backfill] [--since YYYY-MM-DD] [--max-pages N]
    raglex sources                     # list registered adapters
    raglex status <source>             # watermark + run-state for a source

The ops UI (§8) is the eventual front end; this CLI is the day-one operator
surface and what cron drives (§5).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .adapters.registry import ADAPTERS, get_adapter
from .config import Config
from .pipeline import Pipeline
from .resolve import Resolver
from .storage import Catalogue, RawStore, TextStore
from .tagging import RuleEngine, seed

from .adapters.registry import IN_SCOPE_SOURCES as _SKIP_GATE  # noqa: E402


def _open(config: Config) -> tuple[Catalogue, RawStore, TextStore]:
    return (
        Catalogue(config.catalogue_path),
        RawStore(config.raw_dir),
        TextStore(config.text_dir),
    )


def _parse_opts(pairs: list[str] | None) -> dict[str, object]:
    """Generic KEY=VALUE adapter options. Domain/jurisdiction focus is config, not
    code: e.g. `--opt legislation_celex=32004R0139` points eu-cellar at the Merger
    Regulation instead of its GDPR default. Digit strings coerce to int."""
    opts: dict[str, object] = {}
    for pair in pairs or []:
        key, _, value = pair.partition("=")
        opts[key.strip()] = int(value) if value.strip().isdigit() else value.strip()
    return opts


def cmd_run(args: argparse.Namespace) -> int:
    config = Config.from_env()
    catalogue, rawstore, textstore = _open(config)
    try:
        adapter = get_adapter(args.source, **_parse_opts(args.opt))
        pipeline = Pipeline(
            catalogue,
            rawstore,
            textstore=textstore,
            topic_threshold=config.topic_threshold,
            skip_topic_gate=args.source in _SKIP_GATE,
        )
        stats = pipeline.run(
            adapter,
            backfill=args.backfill,
            since=args.since,
            max_pages=args.max_pages,
        )
        print(stats.summary())
        for note in stats.notes:
            print(f"  note: {note}")
        # Re-run resolution after the ingest cycle (§5b): citations to freshly
        # harvested targets become live edges now.
        if not args.no_resolve:
            print(Resolver(catalogue).run().summary())
        # Re-run enabled tag rules over the corpus (§4a on-ingest tagging).
        if not args.no_tag:
            results = RuleEngine(catalogue).run_all(enabled_only=True)
            print(f"[tag] ran {len(results)} rules")
        return 0
    finally:
        catalogue.close()


def cmd_resolve(_: argparse.Namespace) -> int:
    config = Config.from_env()
    catalogue, *_ = _open(config)
    try:
        print(Resolver(catalogue).run().summary())
        return 0
    finally:
        catalogue.close()


def cmd_worklist(args: argparse.Namespace) -> int:
    """The §8 harvest worklist: most-cited citations not yet in the corpus."""
    config = Config.from_env()
    catalogue, *_ = _open(config)
    try:
        rows = catalogue.resolution_worklist(limit=args.limit)
        if not rows:
            print("no pending citations")
            return 0
        for row in rows:
            print(f"{row['cite_count']:>4}×  {row['raw_citation_string']}")
        return 0
    finally:
        catalogue.close()


def cmd_sources(_: argparse.Namespace) -> int:
    for key in sorted(ADAPTERS):
        print(key)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = Config.from_env()
    catalogue, *_ = _open(config)
    try:
        state = catalogue.source_state(args.source)
        if state is None:
            print(f"{args.source}: no runs recorded")
            return 0
        print(f"{args.source}:")
        for key in state.keys():
            print(f"  {key}: {state[key]}")
        return 0
    finally:
        catalogue.close()


def cmd_dashboard(_: argparse.Namespace) -> int:
    """The §8 ops dashboard: source health + pipeline queues + alerts."""
    from .ops import check_alerts, pipeline_queues, source_dashboard

    config = Config.from_env()
    catalogue, *_ = _open(config)
    try:
        print("=== sources ===")
        for s in source_dashboard(catalogue):
            flags = "".join(f for f, on in [("J", s.requires_js), ("P", s.requires_proxy)] if on)
            print(
                f"  {s.key:<16} docs={s.documents:<6} fails={s.consecutive_failures} "
                f"watermark={s.watermark or '-'} last_yield={s.last_yield_at or '-'} {flags}"
            )
        print("\n=== pipeline queues ===")
        for name, depth in pipeline_queues(catalogue).items():
            print(f"  {name:<22} {depth}")
        alerts = check_alerts(catalogue)
        print(f"\n=== alerts ({len(alerts)}) ===")
        for a in alerts:
            print(f"  [{a.severity.upper()}] {a.code} ({a.subject}): {a.message}")
        if not alerts:
            print("  none — all healthy")
        return 0
    finally:
        catalogue.close()


def cmd_stats(_: argparse.Namespace) -> int:
    from .ops import corpus_stats

    config = Config.from_env()
    catalogue, *_ = _open(config)
    try:
        st = corpus_stats(catalogue)
        print(f"total documents: {st.total}")
        cov = st.resolution.get("coverage", 0.0)
        print(f"citation resolution: {st.resolution.get('resolved', 0)}/{st.resolution.get('total', 0)} ({cov:.0%})")
        for label, d in [
            ("by doc_type", st.by_doc_type), ("by source", st.by_source),
            ("by tag", st.by_tag), ("by upstream_status", st.by_upstream_status),
        ]:
            print(f"\n{label}:")
            for k, n in d.items():
                print(f"  {k:<22} {n}")
        return 0
    finally:
        catalogue.close()


def cmd_alerts(_: argparse.Namespace) -> int:
    from .ops import LogNotifier, push_alerts

    config = Config.from_env()
    catalogue, *_ = _open(config)
    try:
        alerts = push_alerts(catalogue, LogNotifier(sink=print))
        if not alerts:
            print("no alerts — all healthy")
        return 0
    finally:
        catalogue.close()


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("install the web extra:  uv sync --extra web")
        return 1
    from .web import serve_app

    uvicorn.run(serve_app(), host=args.host, port=args.port)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Run the MCP server — exposes every API operation as MCP tools."""
    try:
        from .mcp_server import build_server
    except ImportError:
        print("install the web extra:  uv sync --extra web")
        return 1
    server = build_server()
    server.run(transport="streamable-http" if args.http else "stdio")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    """Extract citations from document text → hanging edges, then resolve (§5)."""
    from .facade import Facade

    use_llm = True if args.llm else (False if args.no_llm else None)
    res = Facade(Config.from_env()).extract_citations(
        stable_id=args.doc, limit=args.limit, use_llm=use_llm)
    print(f"[cite-extract] documents={res['documents']} citations={res['citations']} "
          f"reclassified={res['reclassified']} resolved={res['resolved_edges']} "
          f"llm={'on' if res.get('llm') else 'off'}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Saved harvest plans (§5a): keyword-limited harvest + autosnowball, scheduled."""
    import time

    from .facade import Facade

    f = Facade(Config.from_env())
    sub = args.watch_command
    if sub == "list":
        rows = f.list_watches()
        if not rows:
            print("no watches — add one with: raglex watch add --name … --source …")
            return 0
        for w in rows:
            sp = w["spec"]
            print(f"#{w['watch_id']} {'·' if w['enabled'] else '✗'} {w['name']}  "
                  f"source={sp.get('source') or sp.get('seed_rule')} kw={sp.get('keywords') or []} "
                  f"degrees={sp.get('degrees', 1)} every {w['cadence_minutes']}m  last={w['last_run_at'] or 'never'}")
        return 0
    if sub == "add":
        spec: dict = {"degrees": args.degrees, "max_pages": args.max_pages}
        if args.source:
            spec["source"] = args.source
        if args.keywords:
            spec["keywords"] = [k.strip() for k in args.keywords.split(",") if k.strip()]
        if args.cites:
            spec["seed_rule"] = {"cites": args.cites, "hops": args.hops}
        if args.citing:
            spec["discover"] = {"citing": args.citing, "via": args.via}
        if args.tag:
            spec["tag"] = args.tag
        w = f.create_watch(name=args.name, spec=spec, cadence_minutes=args.cadence)
        print(f"created watch #{w['watch_id']}: {w['name']}")
        return 0
    if sub == "run":
        print(json.dumps(f.run_watch(watch_id=args.id), indent=1, default=str))
        return 0
    if sub == "tick":
        print(json.dumps(f.tick_watches(), indent=1, default=str))
        return 0
    if sub == "delete":
        print(json.dumps(f.delete_watch(watch_id=args.id), indent=1, default=str))
        return 0
    if sub == "serve":
        import os

        from .jobs import JobManager

        print(f"[watch] scheduler up; ticking every {args.interval}s")
        # The scheduler's own work is recorded as jobs, in the same table the API reads —
        # so the auto-drain finally appears in the jobs panel, with its per-tick outcome.
        # An auto-drain quietly storing zero documents for a fortnight was invisible before.
        jobs = JobManager(f, origin="scheduler")
        jobs.reap_orphans()
        last_backfill = 0.0
        last_effects = 0.0
        last_counts = 0.0
        eurlex_broken_until = 0.0
        pushed_alerts: set = set()  # (code, subject) already notified — don't nag
        while True:
            try:
                res = f.tick_watches()
                if res["ran"]:
                    print(f"[watch] ran {res['ran']} due watch(es)")
                # Slow worklist drain: fetch a bounded batch of routable references
                # each tick (survives restarts; the scheduler service is persistent).
                Config.from_env()  # refresh settings → env (RAGLEX_AUTOHARVEST)
                batch = int(os.environ.get("RAGLEX_AUTOHARVEST") or 0)
                if batch > 0:
                    started = jobs.start("auto-drain", f"auto-drain worklist ({batch}/tick)",
                                         {"limit": batch})
                    if started.get("already_running"):
                        print("[watch] auto-drain: previous tick still running; skipping")
                    elif started.get("error"):
                        print(f"[watch] auto-drain: {started['error']}")
                # Once a day: pull EU case names/subjects from the EUR-Lex webservice
                # (batched, skipping known-empty CELEXes). No-op without creds. The service
                # 500s for days at a time; when it does, stop asking until tomorrow rather
                # than grinding the whole batch against it every tick.
                if time.time() - last_backfill >= 86400 and time.time() >= eurlex_broken_until:
                    last_backfill = time.time()
                    bf = f.backfill_titles()
                    if bf.get("provider_down"):
                        eurlex_broken_until = time.time() + 86400
                        print("[watch] eurlex backfill: service erroring; backing off 24h")
                    elif bf.get("titled") or bf.get("subject_tags_added"):
                        print(f"[watch] eurlex backfill: {bf}")
                # Hourly: re-pull legislation whose outstanding-effects re-check is due
                # (§0). Bounded; usually a no-op (backoff is weeks). Only touches items
                # already flagged stale — never the whole corpus.
                if time.time() - last_effects >= 3600:
                    last_effects = time.time()
                    ef = f.refresh_effects(limit=int(os.environ.get("RAGLEX_EFFECTS_BATCH") or 10))
                    if ef.get("checked"):
                        print(f"[watch] effects refresh: checked {ef['checked']}, "
                              f"cleared {ef['cleared']}, still outstanding {ef['still_outstanding']}")
                    # Affecting-side: scan a few held acts for the changes they make and
                    # push those out to the instruments they affect (flag held affected
                    # acts for re-pull). Bounded; scanned once per ~90d per act.
                    pc = f.propagate_changes(limit=int(os.environ.get("RAGLEX_CHANGES_BATCH") or 3))
                    if pc.get("flagged") or pc.get("edges"):
                        print(f"[watch] changes propagate: scanned {pc['scanned']}, "
                              f"flagged {pc['flagged']} for re-pull, {pc['edges']} amends edge(s)")
                # Hourly: refresh the citation-frequency roll-up the snowball reads. The
                # live aggregate is a ~13s scan of a 10M-row table; the frontier doesn't
                # move between ticks, so a page load must never pay for it.
                if time.time() - last_counts >= 3600:
                    last_counts = time.time()
                    cc = f.rebuild_citation_counts()
                    print(f"[watch] citation counts: {cc['candidates']} distinct candidates")
                # Push the alerts a solo operator can't get by watching a dashboard —
                # to RAGLEX_ALERT_WEBHOOK if set, otherwise the log. Deduped by (code,
                # subject) so a standing condition isn't re-pushed every 15 minutes.
                for alert in f.push_alerts(seen=pushed_alerts):
                    print(f"[watch] ALERT {alert['code']} {alert['subject']}: {alert['message']}")
            except Exception as exc:  # noqa: BLE001 — a scheduler must not die on one error
                print(f"[watch] tick error: {exc}")
            time.sleep(args.interval)
    return 0


def cmd_snowball(args: argparse.Namespace) -> int:
    """The citation frontier (§5a): forms the corpus cites but doesn't yet hold,
    grouped by (form, jurisdiction, adapter) and ranked by frequency."""
    from .facade import Facade

    rows = Facade(Config.from_env()).snowball(limit=args.limit, only_unharvestable=args.needs_adapter)
    if not rows:
        print("no unresolved citation frontier — corpus holds everything it cites")
        return 0
    print(f"{'occ':>5} {'cands':>5} {'docs':>4}  form / jurisdiction / adapter")
    for r in rows:
        flag = r["adapter"] or "⚠ no adapter — build one"
        print(f"{r['occurrences']:>5} {r['candidates']:>5} {r['documents']:>4}  "
              f"{r['form']} [{r['jurisdiction'] or '?'}] → {flag}   e.g. {r['sample']}")
    return 0


def cmd_index(_: argparse.Namespace) -> int:
    """Build the pgvector HNSW index (Postgres only; §7)."""
    from .facade import Facade

    result = Facade(Config.from_env()).create_index()
    if result["created"]:
        print(f"created HNSW index for {result['dimensions']}-dim vectors")
    else:
        print(f"no index created (backend={result['backend']}; pgvector only)")
    return 0


def cmd_migrate(_: argparse.Namespace) -> int:
    """One-off data migration after an upgrade: backfill candidate_id/raw_fold on edges
    written before those columns existed (§5b), then rebuild the citation-frequency
    roll-up. Idempotent — safe to re-run."""
    from .facade import Facade

    f = Facade(Config.from_env())
    print("backfilling edge candidate ids (may take a few minutes over a large graph)…")
    bf = f.backfill_edge_keys(on_progress=lambda **p: None)
    print(f"  backfilled {bf['strings_backfilled']} distinct citation strings")
    print("minting aliases implied by held documents (ECHR appno → ECLI)…")
    with f._open() as (cat, _rs, _ts):
        am = cat.backfill_alias_from_meta()
    print(f"  minted {am.get('echr_appno', 0)} ECHR application-number aliases")
    print("resolving now-linkable edges…")
    res = f.resolve()
    print(f"  resolved {res.get('resolved', 0)} edge(s)")
    print("rebuilding the citation-frequency roll-up…")
    cc = f.rebuild_citation_counts()
    print(f"  {cc['candidates']} distinct candidates")
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    from .embeddings import EmbedStage

    config = Config.from_env()
    catalogue, _rawstore, textstore = _open(config)
    try:
        provider = config.make_provider()
        if not provider.health():
            print(f"embedding provider {provider.name!r} is not healthy (missing key?)")
            return 1
        stats = EmbedStage(catalogue, provider, textstore=textstore).run(limit=args.limit)
        print(stats.summary())
        return 0
    finally:
        catalogue.close()


def cmd_search(args: argparse.Namespace) -> int:
    from .retrieval import SearchEngine

    config = Config.from_env()
    catalogue, *_ = _open(config)
    try:
        filters: dict = {}
        if args.source:
            filters["source"] = args.source
        if args.doc_type:
            filters["doc_type"] = args.doc_type
        if args.year_from:
            filters["year_from"] = args.year_from
        if args.tag:
            filters["tag"] = args.tag

        engine = SearchEngine(catalogue, config.make_provider())
        hits = engine.search(args.query, k=args.k, filters=filters or None)
        if not hits:
            print("no results (have you run `raglex embed`?)")
            return 0
        for i, h in enumerate(hits, 1):
            label = h.ecli or h.title or h.doc_id
            print(f"\n[{i}] {label}  ({h.source}/{h.court})  score={h.score:.4f}")
            snippet = h.chunk_text[:240].replace("\n", " ")
            print(f"    · {h.structural_unit} chars {h.char_start}-{h.char_end}")
            print(f"    {snippet}")
            if h.neighbours and h.neighbours.neighbours:
                for nb in h.neighbours.neighbours[:3]:
                    arrow = "->" if nb.direction == "out" else "<-"
                    print(f"      {arrow} {nb.relationship_type}: {nb.dst_id}  {nb.title or ''}")
        return 0
    finally:
        catalogue.close()


def cmd_tag(args: argparse.Namespace) -> int:
    config = Config.from_env()
    catalogue, *_ = _open(config)
    engine = RuleEngine(catalogue)
    try:
        if args.tag_command == "seed":
            ids = seed(engine)
            print(f"seeded {len(ids)} rules: {ids}")
        elif args.tag_command == "list":
            for r in catalogue.list_rules():
                flag = "on " if r["enabled"] else "off"
                print(f"  #{r['rule_id']} [{flag}] {r['tag']:<18} v{r['version']}  {r['note'] or ''}")
        elif args.tag_command == "run":
            results = [engine.run_rule(args.rule)] if args.rule else engine.run_all()
            for res in results:
                print(res.summary())
        elif args.tag_command == "preview":
            rule = catalogue.get_rule(args.rule)
            if rule is None:
                print(f"no rule {args.rule}")
                return 1
            res = engine.preview(rule["tag"], json.loads(rule["condition_tree_json"]),
                                 scope=json.loads(rule["scope_json"]))
            print(res.summary())
            for sid, title in res.sample:
                print(f"    {sid}  {title or ''}")
        elif args.tag_command == "show":
            for t in catalogue.tags_for(args.stable_id):
                print(f"  {t['tag']:<18} method={t['method']} rule={t['assigned_by_rule_id']}")
        elif args.tag_command == "group":
            rows = catalogue.documents_with_tag(args.tag)
            print(f"{len(rows)} documents tagged {args.tag!r}")
            for row in rows[: args.limit]:
                print(f"    {row['stable_id']}  {row['title'] or ''}")
        return 0
    finally:
        catalogue.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="raglex", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="harvest one source")
    run.add_argument("source", help="source key (see `raglex sources`)")
    run.add_argument("--backfill", action="store_true", help="ignore watermark, page deep")
    run.add_argument("--since", help="backfill start date YYYY-MM-DD")
    run.add_argument("--max-pages", type=int, default=None, help="bound the backfill")
    run.add_argument(
        "--opt", "-o", action="append", metavar="KEY=VALUE",
        help="adapter option, repeatable (e.g. -o legislation_celex=32004R0139)",
    )
    run.add_argument("--no-resolve", action="store_true", help="skip the post-harvest resolution pass")
    run.add_argument("--no-tag", action="store_true", help="skip the post-harvest tag-rule pass")
    run.set_defaults(func=cmd_run)

    res = sub.add_parser("resolve", help="resolve pending citation edges (§5b)")
    res.set_defaults(func=cmd_resolve)

    wl = sub.add_parser("worklist", help="most-cited citations not yet in the corpus")
    wl.add_argument("--limit", type=int, default=50)
    wl.set_defaults(func=cmd_worklist)

    src = sub.add_parser("sources", help="list registered adapters")
    src.set_defaults(func=cmd_sources)

    st = sub.add_parser("status", help="show a source's watermark and run-state")
    st.add_argument("source")
    st.set_defaults(func=cmd_status)

    dash = sub.add_parser("dashboard", help="ops dashboard: source health + queues + alerts (§8)")
    dash.set_defaults(func=cmd_dashboard)

    stat = sub.add_parser("stats", help="corpus stats: counts by doc_type/source/tag (§8)")
    stat.set_defaults(func=cmd_stats)

    al = sub.add_parser("alerts", help="compute + push alerts (§8)")
    al.set_defaults(func=cmd_alerts)

    srv = sub.add_parser("serve", help="run the ops/research web API (needs the web extra)")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    srv.set_defaults(func=cmd_serve)

    mc = sub.add_parser("mcp", help="run the MCP server (all API ops as tools)")
    mc.add_argument("--http", action="store_true", help="HTTP transport instead of stdio")
    mc.set_defaults(func=cmd_mcp)

    emb = sub.add_parser("embed", help="embed documents with text (§6)")
    emb.add_argument("--limit", type=int, default=None, help="max documents this run")
    emb.set_defaults(func=cmd_embed)

    idx = sub.add_parser("index", help="build the pgvector HNSW index (Postgres, §7)")
    idx.set_defaults(func=cmd_index)

    mig = sub.add_parser("migrate", help="one-off post-upgrade backfill: edge keys + counts (§5b)")
    mig.set_defaults(func=cmd_migrate)

    ex = sub.add_parser("extract", help="extract citations from text into edges (§5)")
    ex.add_argument("--doc", default=None, help="a single stable_id (default: whole corpus)")
    ex.add_argument("--limit", type=int, default=None)
    ex.add_argument("--llm", action="store_true", help="force the LLM extraction/treatment pass on")
    ex.add_argument("--no-llm", action="store_true", help="force grammars/heuristics only")
    ex.set_defaults(func=cmd_extract)

    sn = sub.add_parser("snowball", help="citation frontier: what's cited but not yet harvested (§5a)")
    sn.add_argument("--limit", type=int, default=50)
    sn.add_argument("--needs-adapter", action="store_true", dest="needs_adapter",
                    help="only forms with no adapter yet (the build-an-adapter list)")
    sn.set_defaults(func=cmd_snowball)

    wt = sub.add_parser("watch", help="saved harvest plans: keyword harvest + autosnowball, scheduled (§5a)")
    wt_sub = wt.add_subparsers(dest="watch_command", required=True)
    wt_sub.add_parser("list", help="list saved watches")
    w_add = wt_sub.add_parser("add", help="create a watch")
    w_add.add_argument("--name", required=True)
    w_add.add_argument("--source", help="source key (e.g. uk-grc); omit for a pure seed-rule watch")
    w_add.add_argument("--keywords", help="comma-separated keyword limiters")
    w_add.add_argument("--cites", help="seed rule: corpus docs that cite this id (e.g. 32016R0679)")
    w_add.add_argument("--hops", type=int, default=1, help="hops for --cites (2 = cases citing cases that cite it)")
    w_add.add_argument("--citing", help="discover NEW cases citing this target via the live source (FCL/CELLAR)")
    w_add.add_argument("--via", default="auto", choices=["auto", "uk-caselaw", "eu-cellar"],
                       help="discovery source for --citing")
    w_add.add_argument("--degrees", type=int, default=2, help="autosnowball degrees")
    w_add.add_argument("--max-pages", type=int, default=1, dest="max_pages")
    w_add.add_argument("--tag", help="tag everything this watch brings in")
    w_add.add_argument("--cadence", type=int, default=1440, help="minutes between runs")
    w_run = wt_sub.add_parser("run", help="run one watch now")
    w_run.add_argument("--id", type=int, required=True)
    w_del = wt_sub.add_parser("delete", help="delete a watch")
    w_del.add_argument("--id", type=int, required=True)
    wt_sub.add_parser("tick", help="run every due watch once (the scheduler unit)")
    w_serve = wt_sub.add_parser("serve", help="run the scheduler loop (ticks on an interval)")
    w_serve.add_argument("--interval", type=int, default=900, help="seconds between ticks")
    wt.set_defaults(func=cmd_watch)

    se = sub.add_parser("search", help="hybrid search: FTS+vector → RRF → graph (§6c)")
    se.add_argument("query")
    se.add_argument("-k", type=int, default=5, help="results to return")
    se.add_argument("--source", action="append", help="filter by source (repeatable)")
    se.add_argument("--doc-type", action="append", dest="doc_type", help="filter by doc_type")
    se.add_argument("--year-from", dest="year_from", help="only docs from this year onward")
    se.add_argument("--tag", help="filter by a rule/manual tag")
    se.set_defaults(func=cmd_search)

    tag = sub.add_parser("tag", help="rule-based tagging engine (§4a)")
    tag_sub = tag.add_subparsers(dest="tag_command", required=True)
    tag_sub.add_parser("seed", help="bootstrap the §4 topic vocab as rules")
    tag_sub.add_parser("list", help="list tag rules")
    t_run = tag_sub.add_parser("run", help="run one rule (--rule N) or all enabled")
    t_run.add_argument("--rule", type=int, default=None)
    t_prev = tag_sub.add_parser("preview", help="dry-run a stored rule (no writes)")
    t_prev.add_argument("rule", type=int)
    t_show = tag_sub.add_parser("show", help="show a document's tags + provenance")
    t_show.add_argument("stable_id")
    t_grp = tag_sub.add_parser("group", help="list documents carrying a tag (§8)")
    t_grp.add_argument("tag")
    t_grp.add_argument("--limit", type=int, default=50)
    tag.set_defaults(func=cmd_tag)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
