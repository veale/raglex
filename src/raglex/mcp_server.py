"""RagLex MCP server — every operation the web API has, as MCP tools.

The use case the design imagines: an agent is told to *augment each section/article
of a law with secondary material it finds using other tools*. With this server it
can: ``list_documents`` to iterate the law's sections, ``search`` the corpus,
then post what it finds in several ways — ``import_pdf_url`` (a link it found),
``import_pdf_base64`` (bytes it holds), ``add_note`` (a summary it wrote) — and
wire it into the graph with ``link_documents`` and ``tag_document``. Read tools
(``get_document``, ``graph_neighbours``, ``corpus_stats``, ``dashboard``) let it
inspect what exists first.

Backed by the same ``Facade`` as the web API, so the two never drift. Run with
``raglex mcp`` (stdio transport) or ``raglex mcp --http``.
"""

from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .config import Config
from .facade import Facade


def build_server(config: Config | None = None) -> FastMCP:
    facade = Facade(config or Config.from_env())
    mcp = FastMCP("raglex")

    # -- read / research --------------------------------------------------
    @mcp.tool()
    def search(query: str, k: int = 5, source: Optional[str] = None,
               doc_type: Optional[str] = None, tag: Optional[str] = None,
               year_from: Optional[str] = None) -> list[dict]:
        """Hybrid (keyword+semantic) search with GraphRAG neighbours. Optional
        partition filters by source/doc_type/tag/year."""
        filters: dict = {}
        if source:
            filters["source"] = [source]
        if doc_type:
            filters["doc_type"] = [doc_type]
        if tag:
            filters["tag"] = tag
        if year_from:
            filters["year_from"] = year_from
        return facade.search(query, k=k, filters=filters or None)

    @mcp.tool()
    def list_documents(source: Optional[str] = None, doc_type: Optional[str] = None,
                       tag: Optional[str] = None, query: Optional[str] = None,
                       limit: int = 100) -> list[dict]:
        """Browse/filter documents — e.g. iterate the sections of a law to augment."""
        return facade.list_documents(source=source, doc_type=doc_type, tag=tag,
                                     query=query, limit=limit)

    @mcp.tool()
    def get_document(stable_id: str) -> dict:
        """Full document: metadata, tags, relations (citations), and attachments."""
        return facade.get_document(stable_id)

    @mcp.tool()
    def get_document_body(stable_id: str) -> dict:
        """The document's full text + structural segments (legislation articles /
        sections, judgment paragraphs) with their citable labels and levels."""
        return facade.document_body(stable_id)

    @mcp.tool()
    def graph_neighbours(stable_id: str, relationship_types: Optional[list[str]] = None) -> dict:
        """1-hop typed citation/commentary neighbourhood of a document."""
        return facade.graph(stable_id, rel=relationship_types)

    @mcp.tool()
    def corpus_stats() -> dict:
        """Corpus breakdown by doc_type/source/tag + citation-resolution coverage."""
        return facade.stats()

    @mcp.tool()
    def dashboard() -> dict:
        """Ops health: source dashboard, pipeline queues, and active alerts (§8)."""
        return {"sources": facade.sources(), "queues": facade.queues(), "alerts": facade.alerts()}

    @mcp.tool()
    def harvest_worklist(limit: int = 50) -> list[dict]:
        """Most-cited citations not yet in the corpus — a ranked harvest worklist."""
        return facade.worklist(limit=limit)

    @mcp.tool()
    def list_sources() -> list[str]:
        """The registered source adapters that can be harvested."""
        return facade.list_sources()

    @mcp.tool()
    def harvest(source: str, backfill: bool = False, since: Optional[str] = None,
                max_pages: int = 1) -> dict:
        """Harvest a source (then resolve + tag). Bounded by max_pages; large
        backfills are better run via the CLI."""
        return facade.harvest(source, backfill=backfill, since=since, max_pages=max_pages)

    # -- write / augment (post secondary material in several ways) --------
    @mcp.tool()
    def import_pdf_url(url: str, doc_type: str = "commentary", title: Optional[str] = None,
                       link_to: Optional[str] = None, relationship: Optional[str] = None) -> dict:
        """Import a PDF/HTML from a URL as a secondary document, optionally linking
        it (e.g. relationship='analyses') to a case/law-section stable_id."""
        return facade.import_url(url=url, doc_type=doc_type, title=title,
                                 link_to=link_to, relationship=relationship)

    @mcp.tool()
    def import_pdf_base64(content_base64: str, filename: str, doc_type: str = "commentary",
                          title: Optional[str] = None, link_to: Optional[str] = None,
                          relationship: Optional[str] = None) -> dict:
        """Import a PDF/HTML the agent already holds as base64 bytes."""
        return facade.import_base64(content_base64=content_base64, filename=filename,
                                    doc_type=doc_type, title=title, link_to=link_to,
                                    relationship=relationship)

    @mcp.tool()
    def add_note(text: str, title: Optional[str] = None, link_to: Optional[str] = None,
                 relationship: str = "summarises") -> dict:
        """Write a note/summary as a first-class secondary document, optionally
        linked to the case/law section it concerns."""
        return facade.add_note(text=text, title=title, link_to=link_to, relationship=relationship)

    @mcp.tool()
    def attach_file_base64(doc_id: str, content_base64: str, filename: str,
                           kind: str = "exhibit") -> dict:
        """Attach a file (annotated copy, exhibit) to an existing document."""
        return facade.attach_base64(doc_id=doc_id, content_base64=content_base64,
                                    filename=filename, kind=kind)

    @mcp.tool()
    def link_documents(src_id: str, dst_id: str, relationship: str,
                       src_anchor: Optional[str] = None, dst_anchor: Optional[str] = None) -> dict:
        """Add a typed edge between two documents (e.g. an article 'analyses' a law
        article). Optional pinpoint anchors link a *fragment* of the source to a
        *fragment* of the target — e.g. a handbook's src_anchor='pp. 45-47'
        analyses a law's dst_anchor='Article 17' (use the article/section label
        from get_document_body's segments)."""
        return facade.link(src_id=src_id, dst_id=dst_id, relationship=relationship,
                           src_anchor=src_anchor, dst_anchor=dst_anchor)

    @mcp.tool()
    def tag_document(doc_id: str, tag: str) -> dict:
        """Add a manual tag (never overwritten by rules)."""
        return facade.tag(doc_id=doc_id, tag=tag)

    @mcp.tool()
    def untag_document(doc_id: str, tag: str) -> dict:
        """Remove a manual tag added by mistake."""
        return facade.untag(doc_id=doc_id, tag=tag)

    @mcp.tool()
    def tag_documents(doc_ids: list[str], tag: str) -> dict:
        """Bulk-tag a selection into a collection (a collection = a shared tag)."""
        return facade.tag_many(doc_ids=doc_ids, tag=tag)

    # -- corrections (fix misclassification; human curation wins) ----------
    @mcp.tool()
    def update_document(stable_id: str, doc_type: Optional[str] = None,
                        title: Optional[str] = None, court: Optional[str] = None,
                        source_language: Optional[str] = None) -> dict:
        """Correct a misclassified document's metadata — its type (judgment /
        legislation / guidance / opinion / commentary / …), title, court, or
        language. The edit is recorded as human curation."""
        return facade.update_document(stable_id=stable_id, doc_type=doc_type, title=title,
                                      court=court, source_language=source_language)

    @mcp.tool()
    def correct_citation(relation_id: int, treatment: Optional[str] = None,
                         dst_id: Optional[str] = None, suppress: bool = False) -> dict:
        """Fix one citation edge (its relation_id is on each relation from
        get_document): ``suppress=True`` rejects a false-positive citation (it won't
        reappear on re-extraction); ``dst_id`` re-points a wrong resolution to the
        correct existing document; ``treatment`` corrects how the case is treated
        (e.g. follows → distinguishes). All recorded as manual, so the automatic
        passes never overwrite them."""
        return facade.correct_citation(relation_id=relation_id, treatment=treatment,
                                       dst_id=dst_id, suppress=suppress)

    @mcp.tool()
    def reparse_documents(stable_id: Optional[str] = None, doc_type: Optional[str] = "legislation") -> dict:
        """Re-derive text + structural segments from immutable raw using the current
        parser (e.g. to pick up improved legislation formatting / EU recitals) without
        re-fetching. Pass a stable_id for one document, or omit to reparse all of a
        doc_type (default: legislation)."""
        if stable_id:
            return facade.reparse_document(stable_id=stable_id)
        return facade.reparse_all(doc_type=doc_type)

    @mcp.tool()
    def backfill_eu_case_metadata(limit: int = 500) -> dict:
        """Augment harvested CJEU cases from the EUR-Lex webservice with the official
        case name + subject-matter tags (the free CELLAR data omits these). Batched +
        quota-friendly; needs EURLEX_USERNAME/PASSWORD in settings."""
        return facade.backfill_titles(limit=limit)

    @mcp.tool()
    def coverage() -> dict:
        """Completeness/uncertainty dashboard: per-source counts + date spans,
        citation-resolution rate, how many references are still hanging (known gaps),
        and the top frontiers the corpus cites but doesn't hold. Use it to judge
        whether an area's dataset is complete and what's uncertain about what exists."""
        return facade.coverage()

    @mcp.tool()
    def import_zotero(library_id: str, api_key: str, library_type: str = "users",
                      limit: int = 50, fetch_pdfs: bool = False) -> dict:
        """Import items from a Zotero library as secondary documents."""
        return facade.import_zotero(library_id=library_id, api_key=api_key,
                                    library_type=library_type, limit=limit, fetch_pdfs=fetch_pdfs)

    @mcp.tool()
    def embed_pending(limit: Optional[int] = None) -> dict:
        """Embed documents that have text but no vectors yet (makes them searchable)."""
        return facade.embed(limit=limit)

    @mcp.tool()
    def resolve_citations() -> dict:
        """Re-run entity resolution so new citation strings become live graph edges."""
        return facade.resolve()

    @mcp.tool()
    def extract_citations(stable_id: Optional[str] = None, use_llm: Optional[bool] = None) -> dict:
        """Mine citations from document text into hanging typed edges (entity-level:
        cases, regulations, acts — with article/section pinpoints), classify case
        treatments (mentions → follows/distinguishes/overrules), then resolve.
        Pass a stable_id for one document or omit for the whole corpus. ``use_llm``:
        None=auto (use the configured LLM if reachable), True/False to force the
        batched LLM extraction+treatment pass on/off."""
        return facade.extract_citations(stable_id=stable_id, use_llm=use_llm)

    @mcp.tool()
    def list_unresolved_references(limit: int = 100) -> list[dict]:
        """Hanging references the corpus cites but can't satisfy — the manual-
        resolution queue. Each row gives the reference, what it looks like
        (form/jurisdiction/suggested adapter), its confidence, whether it still
        needs an identifier (recognised by name only), and which documents cite it.
        Pair with ``resolve_reference`` to satisfy one."""
        return facade.unresolved_references(limit=limit)

    @mcp.tool()
    def resolve_reference(ref: str, identifier: Optional[str] = None,
                          jurisdiction: Optional[str] = None, existing_id: Optional[str] = None,
                          url: Optional[str] = None, content_base64: Optional[str] = None,
                          filename: Optional[str] = None, title: Optional[str] = None,
                          doc_type: str = "commentary") -> dict:
        """Satisfy a hanging reference (``ref`` from list_unresolved_references) any
        of four interchangeable ways: supply the missing ``identifier`` (a neutral
        citation / ECLI / CELEX — for a reference known by name only, optionally with
        ``jurisdiction``); point it at an ``existing_id`` already in the corpus;
        give a ``url`` to fetch via the scraping engine; or upload the source as
        ``content_base64`` (+ ``filename``). Re-keys the hanging edges and resolves
        them. An agent can clear the whole queue with these two tools."""
        return facade.resolve_reference(
            ref=ref, identifier=identifier, jurisdiction=jurisdiction, existing_id=existing_id,
            url=url, content_base64=content_base64, filename=filename, title=title, doc_type=doc_type)

    @mcp.tool()
    def harvest_reference(ref: str, candidate: Optional[str] = None) -> dict:
        """One-click resolution for a *routable* hanging reference (a ``ref`` from
        list_unresolved_references whose suggested_adapter is set): fetch exactly that
        item from the adapter that holds it (uk-legislation by id, eu-legislation by
        CELEX, uk-caselaw by document URI) and resolve. Prefer this over upload/scrape
        when the system already knows where the item lives."""
        return facade.harvest_reference(ref=ref, candidate=candidate)

    @mcp.tool()
    def radiate(seeds: Optional[list[str]] = None, seed_rule: Optional[dict] = None,
                degrees: int = 2, max_per_degree: int = 40, dry_run: bool = False) -> dict:
        """Snowball-sample the citation network ``degrees`` hops from a seed set,
        targeted-harvesting routable citations at each hop. Seeds can be explicit ids
        (``seeds=["32016R0679", "[2011] EWCA Civ 31"]``) or defined *by rule*
        (``seed_rule``): ``{"cites": "32016R0679"}`` = every corpus doc citing the
        GDPR (add ``"hops": 2`` for cases citing cases that cite it); ``{"tag": "..."}``
        a category; ``{"query": "..."}`` keyword hits. ``dry_run`` previews the seed
        set. This is the build-a-corpus-around-X engine."""
        return facade.radiate(seeds=seeds, seed_rule=seed_rule, degrees=degrees,
                              max_per_degree=max_per_degree, dry_run=dry_run)

    @mcp.tool()
    def discover_citing(target: str, via: str = "auto", query: Optional[str] = None,
                        max_pages: int = 1) -> dict:
        """Forward-citation discovery — find NEW cases that cite ``target`` from the
        live source: an EU CELEX → CELLAR's "cases interpreting this legislation";
        a UK act/case → Find Case Law full-text search for its citation/title. This
        is the watch seed that genuinely grows over time. Returns the newly-harvested
        citing document ids. ``via`` auto-picks the source; override with
        'eu-cellar'/'uk-caselaw'; ``query`` overrides the search string."""
        return facade.discover_citing(target=target, via=via, query=query, max_pages=max_pages)

    @mcp.tool()
    def detect_citations(text: str) -> dict:
        """Recognise every citation in a block of text (ECLI, CELEX, neutral citation,
        legislation, CJEU case number) and report the routable candidates — the preview
        before seeding. No fetching."""
        return facade.detect_citations(text=text)

    @mcp.tool()
    def seed_from_text(text: str, degrees: int = 1, include_citing: bool = True,
                       max_per_degree: int = 40) -> dict:
        """Paste a block of text → detect every citation in it, harvest those items, then
        radiate ``degrees`` hops over what they cite/link to AND (``include_citing``) pull
        what cites them from the live source. The one-shot 'seed cases and go forwards and
        backwards from them'."""
        return facade.seed_from_text(text=text, degrees=degrees, include_citing=include_citing,
                                     max_per_degree=max_per_degree)

    @mcp.tool()
    def source_catalog() -> list[dict]:
        """Per-source capabilities: what each harvestable source pulls, whether
        keywords are searched at the API vs post-filtered, and its options."""
        return facade.source_catalog()

    @mcp.tool()
    def create_watch(name: str, spec: dict, cadence_minutes: int = 1440, enabled: bool = True) -> dict:
        """Save a harvest plan that keyword-limits a harvest and autosnowballs N
        degrees, run on a cadence. ``spec`` keys: ``source`` (+ ``source_options``),
        ``keywords`` (list — searched at the API where supported, else post-filtered),
        ``seed_rule`` (e.g. {"cites": "32016R0679", "hops": 2}), ``degrees``,
        ``max_pages``, ``max_per_degree``, ``tag``."""
        return facade.create_watch(name=name, spec=spec, cadence_minutes=cadence_minutes, enabled=enabled)

    @mcp.tool()
    def list_watches() -> list[dict]:
        """List saved watches with their spec, cadence, and last run/result."""
        return facade.list_watches()

    @mcp.tool()
    def run_watch(watch_id: int) -> dict:
        """Run one watch now: keyword-limited harvest + autosnowball + tag."""
        return facade.run_watch(watch_id=watch_id)

    @mcp.tool()
    def delete_watch(watch_id: int) -> dict:
        """Delete a saved watch."""
        return facade.delete_watch(watch_id=watch_id)

    @mcp.tool()
    def harvest_legislation_at(stable_id: str, date: str) -> dict:
        """Fetch UK legislation as it stood on ``date`` (YYYY-MM-DD) — the point-in-time
        version, so an old case reads against the live provisions, not today's repealed
        text. Stored as id@date and linked to the base instrument."""
        return facade.harvest_legislation_at(stable_id=stable_id, date=date)

    @mcp.tool()
    def legislation_versions(stable_id: str) -> dict:
        """List the point-in-time versions of a piece of legislation already held."""
        return facade.legislation_versions(stable_id=stable_id)

    @mcp.tool()
    def outstanding_effects(limit: int = 200) -> list[dict]:
        """Legislation in the corpus with *unapplied amendments* — changes the
        legislation.gov.uk editors know about but haven't yet written into the text
        (the editorial lag). Each row: outstanding count, amending instruments, which
        of those we already hold, and the next scheduled re-check."""
        return facade.outstanding_effects(limit=limit)

    @mcp.tool()
    def refresh_effects(limit: int = 10) -> dict:
        """Re-pull the legislation whose outstanding-effects re-check is due, to see if
        the amendments have been incorporated yet. Bounded; reschedules (backing off) or
        clears items whose effects are now applied."""
        return facade.refresh_effects(limit=limit)

    @mcp.tool()
    def import_echr_convention() -> dict:
        """Import the European Convention on Human Rights (ETS No. 5) full text from
        Wikisource as the corpus node ``echr/convention``, segmented by Article — so
        "Article 10 of the Convention" resolves and pinpoints to the real Article 10."""
        return facade.import_echr_convention()

    @mcp.tool()
    def legislation_changes(stable_id: str) -> list[dict]:
        """What an *amending* instrument changes — the affected instruments, the
        provisions it touches, and how (from both its amends and amended_by edges)."""
        return facade.effects_caused_by(stable_id=stable_id)

    @mcp.tool()
    def propagate_changes(stable_id: str = "", limit: int = 5) -> dict:
        """Push an amending act's changes OUT to the instruments it affects: mint amends
        edges and flag affected acts we hold for re-pull, so a new act's amendments reach
        old legislation that might never be fetched again. Pass a stable_id for one act,
        or none to scan a bounded batch of held legislation."""
        if stable_id:
            return facade.propagate_changes_from(stable_id=stable_id)
        return facade.propagate_changes(limit=limit)

    @mcp.tool()
    def create_alias(phrase: str, target_id: str, apply: bool = False) -> dict:
        """Create a shorthand RULE: every occurrence of ``phrase`` (e.g. "UK GDPR")
        links to ``target_id``, propagating across the corpus on extraction. Set
        apply=True to re-extract now."""
        return facade.create_named_alias(phrase=phrase, target_id=target_id, apply=apply)

    @mcp.tool()
    def list_aliases() -> list[dict]:
        """List the shorthand rules (phrase → document)."""
        return facade.list_named_aliases()

    @mcp.tool()
    def delete_alias(phrase: str) -> dict:
        """Remove a shorthand rule."""
        return facade.delete_named_alias(phrase=phrase)

    @mcp.tool()
    def harvest_all_references(limit: int = 25, min_citing: int = 1) -> dict:
        """Drain the routable part of the hanging-reference queue in one pass: fetch
        every high-confidence, adapter-backed reference's exact item and resolve.
        ``limit`` caps how many (most-cited first); ``min_citing`` skips one-offs.
        Leaves un-routable / low-confidence references for manual handling."""
        return facade.harvest_all_references(limit=limit, min_citing=min_citing)

    @mcp.tool()
    def snowball(limit: int = 50, only_unharvestable: bool = False) -> list[dict]:
        """The citation frontier (§5a): forms the corpus cites but doesn't yet hold,
        grouped by (form, jurisdiction, adapter) and ranked by how often they're
        cited. Each row says whether an adapter can fetch it now, or whether it's a
        frequently-cited body with no adapter yet (a build-an-adapter signal — set
        only_unharvestable=True to see just those). Feeds the harvest snowball."""
        return facade.snowball(limit=limit, only_unharvestable=only_unharvestable)

    @mcp.tool()
    def get_settings() -> dict:
        """View configured settings/credentials (secrets masked; shows env vs file)."""
        return facade.get_settings()

    @mcp.tool()
    def set_settings(values: dict) -> dict:
        """Set settings/credentials in the file store (env vars still override)."""
        return facade.update_settings(values)

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
