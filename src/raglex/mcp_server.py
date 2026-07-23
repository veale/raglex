"""RagLex MCP server — a legal-research corpus as MCP tools.

Retrieval and navigation are the first-class surface, because that is what an agent doing
legal research reaches for constantly: ``overview`` (the balance of holdings),
``jurisdictions``, ``search`` (scoped by jurisdiction/kind), ``lookup`` (resolve a citation
→ its text or a pinpoint passage, the ways it is cited, who cites it, similar cases —
fetching it silently if it is merely new to the corpus), plus ``get_document`` /
``get_provision`` / ``related_documents`` / ``citator`` / ``graph_neighbours``.

Everything that CHANGES the corpus — harvesting, imports, watches, aliases, resolution,
settings, probes, backfills (~60 operations) — is gated behind the single ``maintenance``
tool, so its schemas don't crowd the context for tools rarely used. ``maintenance('help')``
lists the ops; ``maintenance('<op>', {..})`` runs one.

Backed by the same ``Facade`` as the web API, so the two never drift. Run with
``raglex mcp`` (stdio transport) or ``raglex mcp --http``.
"""

from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .config import Config
from .facade import Facade


_INSTRUCTIONS = (
    "RagLex is a legal-research corpus — case law, legislation and regulatory guidance "
    "across many jurisdictions. Orient yourself first with overview() (the dense balance of "
    "holdings, and what can be fetched on demand) and jurisdictions() (the selectable "
    "jurisdiction filter).\n\n"
    "The workhorse is lookup(citation): give it a citation (or a stable_id) and it resolves "
    "the authority and returns its text — or, with a pincite, just that passage plus a scale "
    "of surrounding context — together with the ways it is cited (parallel citations and "
    "shorthands), who cites it, and cocitation 'similar cases'. If the authority is new to "
    "the corpus it is fetched SILENTLY from its source (CourtListener, Find Case Law, "
    "legislation.gov.uk, CELLAR, HUDOC…); if it cannot be fetched, an external legal-"
    "information-institute URL is returned so you can read it yourself.\n\n"
    "Prefer search / lookup / related_documents / citator for research. Everything that "
    "CHANGES the corpus — harvesting, imports, watches, aliases, settings, repairs — is "
    "behind the single maintenance(op, args) tool, to keep this surface small. Call "
    "maintenance('help') only when you actually need to modify the corpus."
)


def build_server(config: Config | None = None) -> FastMCP:
    facade = Facade(config or Config.from_env())
    mcp = FastMCP("raglex", instructions=_INSTRUCTIONS)

    # Maintenance/mutation operations are NOT registered as individual tools (their schemas
    # would swamp an agent's context for tools it rarely uses). Each is collected into
    # ``_MAINT`` by the ``admin`` decorator and reached through the one ``maintenance``
    # dispatcher tool. The retrieval/navigation tools below stay first-class.
    _MAINT: dict = {}

    def admin(fn):
        _MAINT[fn.__name__] = fn
        return fn

    # -- read / research --------------------------------------------------
    @mcp.tool()
    def search(query: str, k: int = 8, jurisdiction: Optional[str] = None,
               kind: Optional[str] = None, source: Optional[str] = None,
               doc_type: Optional[str] = None, tag: Optional[str] = None,
               year_from: Optional[str] = None) -> list[dict]:
        """Hybrid (keyword+semantic) search with GraphRAG neighbours. Scope it by
        ``jurisdiction`` (a natural-language name from jurisdictions(), e.g. "United States",
        expanded to that jurisdiction's sources), ``kind`` ("cases" | "legislation" |
        "guidance"), or the finer source/doc_type/tag/year filters."""
        filters: dict = {}
        if jurisdiction:
            srcs = facade.sources_for_jurisdiction(jurisdiction)
            if srcs:
                filters["source"] = srcs
        if kind:
            _KIND = {"cases": ["judgment", "decision", "opinion"],
                     "legislation": ["legislation"], "guidance": ["guidance"]}
            filters["doc_type"] = _KIND.get(kind.lower(), [kind])
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
    def overview() -> dict:
        """The dense, parsimonious balance of holdings — per jurisdiction, how much
        case-law / legislation / guidance is HELD and what can be FETCHED on demand. Read
        this first to know what the corpus can be relied on for."""
        return facade.holdings_overview()

    @mcp.tool()
    def jurisdictions() -> list[dict]:
        """The selectable jurisdictions (natural-language names) with their held-document
        counts — the vocabulary the ``jurisdiction`` filter on search() accepts."""
        return facade.jurisdictions()

    @mcp.tool()
    def lookup(citation: str, pincite: Optional[str] = None, context: int = 1,
               full: bool = False, cited_by: bool = True, similar: bool = True,
               autofetch: bool = True) -> dict:
        """Resolve a CITATION (or a stable_id) and return one self-contained answer.

        By default you get metadata + a short text PREVIEW + the document's structural
        outline (token-cheap) — then either ``pincite`` ("Article 17", "s. 45", "[42]",
        "at 644") for just that passage plus ``context`` neighbouring segments (0 = the
        pinpoint alone / 1 = some / 2 = lots), or ``full=true`` for the whole (capped) text.
        Prefer a pincite: it is exact and cheap. Also returned: the ways it is cited
        (``also_cited_as``), who cites it (``cited_by`` — each queryable in turn), and
        cocitation neighbours (``similar``).

        Fetching is a silent fallback: an authority that is merely NEW to the corpus but
        routable (a US case via CourtListener, a UK case/act, an EU/ECHR item) is fetched
        for you and returned. Only when it cannot be fetched at all do you get an external
        LII/BAILII URL to read or scrape yourself. This is the front door — you rarely need
        to harvest anything by hand."""
        return facade.lookup(citation=citation, pincite=pincite, context=context, full=full,
                             cited_by=cited_by, similar=similar, autofetch=autofetch)

    @mcp.tool()
    def list_documents(source: Optional[str] = None, doc_type: Optional[str] = None,
                       tag: Optional[str] = None, query: Optional[str] = None,
                       limit: int = 100) -> list[dict]:
        """Browse/filter documents — e.g. iterate the sections of a law to augment."""
        return facade.list_documents(source=source, doc_type=doc_type, tag=tag,
                                     query=query, limit=limit)

    @mcp.tool()
    def get_document(stable_id: str) -> dict:
        """Full document: metadata, tags, relations, attachments, and a
        ``preparatory_documents`` availability/count flag when legislative history exists."""
        return facade.get_document(stable_id)

    @mcp.tool()
    def preparatory_documents(stable_id: str, limit: int = 50) -> dict:
        """Preparatory/legislative-history documents linked to an item: impact
        assessments, Commission proposals and communications, explanatory material,
        and (as those sources are added) Hansard. Returns exact citing passages and
        structured procedure links; empty when none exist."""
        result = facade.document_mentions(stable_id, snippet_docs=limit, max_groups=limit)
        return {
            "target": stable_id,
            "count": result.get("preparatory_count", 0),
            "message": result.get("preparatory_note"),
            "documents": result.get("preparatory_groups", []),
        }

    @mcp.tool()
    def get_document_body(stable_id: str) -> dict:
        """The document's full text + structural segments (legislation articles /
        sections, judgment paragraphs) with their citable labels and levels."""
        return facade.document_body(stable_id)

    @mcp.tool()
    def get_provision(stable_id: str, label: Optional[str] = None,
                      char_start: Optional[int] = None, context: int = 1) -> dict:
        """ONE provision/paragraph of a document by its citable label ("Article 17",
        "s. 45", "[42]") — or by char offset — with N context segments either side and
        the heading breadcrumb. Prefer this over get_document_body when you need to
        quote a single provision exactly: it's pinpoint-accurate and token-cheap."""
        return facade.get_provision(stable_id, label=label, char_start=char_start,
                                    context=context)

    @mcp.tool()
    def graph_neighbours(stable_id: str, relationship_types: Optional[list[str]] = None) -> dict:
        """1-hop typed citation/commentary neighbourhood of a document, most
        authoritative neighbours first (PageRank-ranked, design §3c)."""
        return facade.graph(stable_id, rel=relationship_types)

    @mcp.tool()
    def related_documents(stable_id: str, limit: int = 12) -> dict:
        """Related documents via the citation network (not vector similarity):
        ``co_cited`` = most often cited together with this one in the same citing
        document; ``coupled`` = relies on the same authorities (bibliographic
        coupling). The practical "cases like this one" for legal research."""
        return facade.related_documents(stable_id, limit=limit)

    @mcp.tool()
    def citator(stable_id: str) -> dict:
        """How this authority currently stands: citation volume, how many citing
        documents are recent, its network-authority percentile (PageRank), and the
        most significant documents citing it. NOTE: treatment classifications
        (followed/overruled) are deliberately absent — not yet reliable — so do not
        infer 'still good law' from this alone; read the significant citors."""
        return facade.citator(stable_id)

    @admin
    def run_probes(only: Optional[str] = None) -> list[dict]:
        """Corpus-integrity probes: invariant checks over the citation network
        (mis-carried pinpoints, self-edges, kind mismatches, broken resolution
        invariants), each with a count + violating samples. ``only``: comma-
        separated probe names to run a subset."""
        return facade.run_probes(only=only.split(",") if only else None)

    @admin
    def repair_probe(name: str) -> dict:
        """Run the bounded repair matched to a repairable probe (e.g.
        'case_paragraph_carry_forward'). Inspect the probe's samples FIRST —
        repairs delete the probe's matching rows. Re-runnable. After a repair
        that touches citations, run rebuild_citation_counts."""
        return facade.repair_probe(name)

    @admin
    def rebuild_authority() -> dict:
        """Recompute the citation-network PageRank roll-up (batch; run after large
        imports or resolution sweeps so ranking/citator/related stay current)."""
        return facade.rebuild_authority()

    @admin
    def corpus_stats() -> dict:
        """Corpus breakdown by doc_type/source/tag + citation-resolution coverage."""
        return facade.stats()

    @admin
    def dashboard() -> dict:
        """Ops health: source dashboard, pipeline queues, and active alerts (§8)."""
        return {"sources": facade.sources(), "queues": facade.queues(), "alerts": facade.alerts()}

    @admin
    def harvest_worklist(limit: int = 50) -> list[dict]:
        """Most-cited citations not yet in the corpus — a ranked harvest worklist."""
        return facade.worklist(limit=limit)

    @admin
    def refinement_flags(status: str = "open", limit: int = 200) -> list[dict]:
        """Reader passages the user flagged "for improved refinement" — each with the
        document, anchor, selected text, what it currently links to, and the user's note.
        The review queue for improving the linking/refinement logic."""
        return facade.list_refinement_flags(status=status or None, limit=limit)

    @admin
    def resolve_refinement_flag(flag_id: int, status: str = "resolved") -> dict:
        """Mark a refinement flag handled after the underlying logic has been improved."""
        return facade.resolve_refinement_flag(flag_id=flag_id, status=status)

    @admin
    def decide_match_suggestion(ref: str, suggested_id: str, accept: bool = True) -> dict:
        """Accept (alias + resolve, fetching the target if not held) or reject a
        'Possibly: …?' match suggestion attached to a hanging reference."""
        return facade.decide_suggestion(ref=ref, suggested_id=suggested_id, accept=accept)

    @admin
    def list_sources() -> list[str]:
        """The registered source adapters that can be harvested."""
        return facade.list_sources()

    @admin
    def harvest(source: str, backfill: bool = False, since: Optional[str] = None,
                max_pages: int = 1) -> dict:
        """Harvest a source (then resolve + tag). Bounded by max_pages; large
        backfills are better run via the CLI."""
        return facade.harvest(source, backfill=backfill, since=since, max_pages=max_pages)

    # -- write / augment (post secondary material in several ways) --------
    @admin
    def import_pdf_url(url: str, doc_type: str = "commentary", title: Optional[str] = None,
                       link_to: Optional[str] = None, relationship: Optional[str] = None) -> dict:
        """Import a PDF/HTML from a URL as a secondary document, optionally linking
        it (e.g. relationship='analyses') to a case/law-section stable_id."""
        return facade.import_url(url=url, doc_type=doc_type, title=title,
                                 link_to=link_to, relationship=relationship)

    @admin
    def import_pdf_base64(content_base64: str, filename: str, doc_type: str = "commentary",
                          title: Optional[str] = None, link_to: Optional[str] = None,
                          relationship: Optional[str] = None) -> dict:
        """Import a PDF/HTML the agent already holds as base64 bytes."""
        return facade.import_base64(content_base64=content_base64, filename=filename,
                                    doc_type=doc_type, title=title, link_to=link_to,
                                    relationship=relationship)

    @admin
    def add_note(text: str, title: Optional[str] = None, link_to: Optional[str] = None,
                 relationship: str = "summarises") -> dict:
        """Write a note/summary as a first-class secondary document, optionally
        linked to the case/law section it concerns."""
        return facade.add_note(text=text, title=title, link_to=link_to, relationship=relationship)

    @admin
    def attach_file_base64(doc_id: str, content_base64: str, filename: str,
                           kind: str = "exhibit") -> dict:
        """Attach a file (annotated copy, exhibit) to an existing document."""
        return facade.attach_base64(doc_id=doc_id, content_base64=content_base64,
                                    filename=filename, kind=kind)

    @admin
    def link_documents(src_id: str, dst_id: str, relationship: str,
                       src_anchor: Optional[str] = None, dst_anchor: Optional[str] = None) -> dict:
        """Add a typed edge between two documents (e.g. an article 'analyses' a law
        article). Optional pinpoint anchors link a *fragment* of the source to a
        *fragment* of the target — e.g. a handbook's src_anchor='pp. 45-47'
        analyses a law's dst_anchor='Article 17' (use the article/section label
        from get_document_body's segments)."""
        return facade.link(src_id=src_id, dst_id=dst_id, relationship=relationship,
                           src_anchor=src_anchor, dst_anchor=dst_anchor)

    @admin
    def tag_document(doc_id: str, tag: str) -> dict:
        """Add a manual tag (never overwritten by rules)."""
        return facade.tag(doc_id=doc_id, tag=tag)

    @admin
    def untag_document(doc_id: str, tag: str) -> dict:
        """Remove a manual tag added by mistake."""
        return facade.untag(doc_id=doc_id, tag=tag)

    @admin
    def tag_documents(doc_ids: list[str], tag: str) -> dict:
        """Bulk-tag a selection into a collection (a collection = a shared tag)."""
        return facade.tag_many(doc_ids=doc_ids, tag=tag)

    # -- corrections (fix misclassification; human curation wins) ----------
    @admin
    def update_document(stable_id: str, doc_type: Optional[str] = None,
                        title: Optional[str] = None, court: Optional[str] = None,
                        source_language: Optional[str] = None) -> dict:
        """Correct a misclassified document's metadata — its type (judgment /
        legislation / guidance / opinion / commentary / …), title, court, or
        language. The edit is recorded as human curation."""
        return facade.update_document(stable_id=stable_id, doc_type=doc_type, title=title,
                                      court=court, source_language=source_language)

    @admin
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

    @admin
    def reparse_documents(stable_id: Optional[str] = None, doc_type: Optional[str] = "legislation") -> dict:
        """Re-derive text + structural segments from immutable raw using the current
        parser (e.g. to pick up improved legislation formatting / EU recitals) without
        re-fetching. Pass a stable_id for one document, or omit to reparse all of a
        doc_type (default: legislation)."""
        if stable_id:
            return facade.reparse_document(stable_id=stable_id)
        return facade.reparse_all(doc_type=doc_type)

    @admin
    def backfill_eu_case_metadata(limit: int = 500) -> dict:
        """Augment harvested CJEU cases from the EUR-Lex webservice with the official
        case name + subject-matter tags (the free CELLAR data omits these). Batched +
        quota-friendly; needs EURLEX_USERNAME/PASSWORD in settings."""
        return facade.backfill_titles(limit=limit)

    @admin
    def coverage() -> dict:
        """Completeness/uncertainty dashboard: per-source counts + date spans,
        citation-resolution rate, how many references are still hanging (known gaps),
        and the top frontiers the corpus cites but doesn't hold. Use it to judge
        whether an area's dataset is complete and what's uncertain about what exists."""
        return facade.coverage()

    @admin
    def import_zotero(library_id: str, api_key: str, library_type: str = "users",
                      limit: int = 50, fetch_pdfs: bool = False) -> dict:
        """Import items from a Zotero library as secondary documents."""
        return facade.import_zotero(library_id=library_id, api_key=api_key,
                                    library_type=library_type, limit=limit, fetch_pdfs=fetch_pdfs)

    @admin
    def embed_pending(limit: Optional[int] = None) -> dict:
        """Embed documents that have text but no vectors yet (makes them searchable)."""
        return facade.embed(limit=limit)

    @admin
    def resolve_citations() -> dict:
        """Re-run entity resolution so new citation strings become live graph edges."""
        return facade.resolve()

    @admin
    def extract_citations(stable_id: Optional[str] = None, use_llm: Optional[bool] = None) -> dict:
        """Mine citations from document text into hanging typed edges (entity-level:
        cases, regulations, acts — with article/section pinpoints), classify case
        treatments (mentions → follows/distinguishes/overrules), then resolve.
        Pass a stable_id for one document or omit for the whole corpus. ``use_llm``:
        None=auto (use the configured LLM if reachable), True/False to force the
        batched LLM extraction+treatment pass on/off."""
        return facade.extract_citations(stable_id=stable_id, use_llm=use_llm)

    @admin
    def list_unresolved_references(limit: int = 100) -> list[dict]:
        """Hanging references the corpus cites but can't satisfy — the manual-
        resolution queue. Each row gives the reference, what it looks like
        (form/jurisdiction/suggested adapter), its confidence, whether it still
        needs an identifier (recognised by name only), and which documents cite it.
        Pair with ``resolve_reference`` to satisfy one."""
        return facade.unresolved_references(limit=limit)

    @admin
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

    @admin
    def harvest_reference(ref: str, candidate: Optional[str] = None) -> dict:
        """One-click resolution for a *routable* hanging reference (a ``ref`` from
        list_unresolved_references whose suggested_adapter is set): fetch exactly that
        item from the adapter that holds it (uk-legislation by id, eu-legislation by
        CELEX, uk-caselaw by document URI) and resolve. Prefer this over upload/scrape
        when the system already knows where the item lives."""
        return facade.harvest_reference(ref=ref, candidate=candidate)

    @admin
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

    @admin
    def discover_citing(target: str, via: str = "auto", query: Optional[str] = None,
                        max_pages: int = 1) -> dict:
        """Forward-citation discovery — find NEW cases that cite ``target`` from the
        live source: an EU CELEX → CELLAR's "cases interpreting this legislation";
        a UK act/case → Find Case Law full-text search for its citation/title. This
        is the watch seed that genuinely grows over time. Returns the newly-harvested
        citing document ids. ``via`` auto-picks the source; override with
        'eu-cellar'/'uk-caselaw'; ``query`` overrides the search string."""
        return facade.discover_citing(target=target, via=via, query=query, max_pages=max_pages)

    @admin
    def detect_citations(text: str) -> dict:
        """Recognise every citation in a block of text (ECLI, CELEX, neutral citation,
        legislation, CJEU case number) and report the routable candidates — the preview
        before seeding. No fetching."""
        return facade.detect_citations(text=text)

    @admin
    def seed_from_text(text: str, degrees: int = 1, include_citing: bool = True,
                       max_per_degree: int = 40) -> dict:
        """Paste a block of text → detect every citation in it, harvest those items, then
        radiate ``degrees`` hops over what they cite/link to AND (``include_citing``) pull
        what cites them from the live source. The one-shot 'seed cases and go forwards and
        backwards from them'."""
        return facade.seed_from_text(text=text, degrees=degrees, include_citing=include_citing,
                                     max_per_degree=max_per_degree)

    @admin
    def source_catalog() -> list[dict]:
        """Per-source capabilities: what each harvestable source pulls, whether
        keywords are searched at the API vs post-filtered, and its options."""
        return facade.source_catalog()

    @admin
    def create_watch(name: str, spec: dict, cadence_minutes: int = 1440, enabled: bool = True) -> dict:
        """Save a harvest plan that keyword-limits a harvest and autosnowballs N
        degrees, run on a cadence. ``spec`` keys: ``source`` (+ ``source_options``),
        ``keywords`` (list — searched at the API where supported, else post-filtered),
        ``seed_rule`` (e.g. {"cites": "32016R0679", "hops": 2}), ``degrees``,
        ``max_pages``, ``max_per_degree``, ``tag``."""
        return facade.create_watch(name=name, spec=spec, cadence_minutes=cadence_minutes, enabled=enabled)

    @admin
    def list_watches() -> list[dict]:
        """List saved watches with their spec, cadence, and last run/result."""
        return facade.list_watches()

    @admin
    def run_watch(watch_id: int) -> dict:
        """Run one watch now: keyword-limited harvest + autosnowball + tag."""
        return facade.run_watch(watch_id=watch_id)

    @admin
    def delete_watch(watch_id: int) -> dict:
        """Delete a saved watch."""
        return facade.delete_watch(watch_id=watch_id)

    @admin
    def harvest_legislation_at(stable_id: str, date: str) -> dict:
        """Fetch UK legislation as it stood on ``date`` (YYYY-MM-DD) — the point-in-time
        version, so an old case reads against the live provisions, not today's repealed
        text. Stored as id@date and linked to the base instrument."""
        return facade.harvest_legislation_at(stable_id=stable_id, date=date)

    @admin
    def legislation_versions(stable_id: str) -> dict:
        """List the point-in-time versions of a piece of legislation already held."""
        return facade.legislation_versions(stable_id=stable_id)

    @admin
    def outstanding_effects(limit: int = 200) -> list[dict]:
        """Legislation in the corpus with *unapplied amendments* — changes the
        legislation.gov.uk editors know about but haven't yet written into the text
        (the editorial lag). Each row: outstanding count, amending instruments, which
        of those we already hold, and the next scheduled re-check."""
        return facade.outstanding_effects(limit=limit)

    @admin
    def refresh_effects(limit: int = 10) -> dict:
        """Re-pull the legislation whose outstanding-effects re-check is due, to see if
        the amendments have been incorporated yet. Bounded; reschedules (backing off) or
        clears items whose effects are now applied."""
        return facade.refresh_effects(limit=limit)

    @admin
    def import_echr_convention() -> dict:
        """Import the European Convention on Human Rights (ETS No. 5) full text from
        Wikisource as the corpus node ``echr/convention``, segmented by Article — so
        "Article 10 of the Convention" resolves and pinpoints to the real Article 10."""
        return facade.import_echr_convention()

    @admin
    def legislation_changes(stable_id: str) -> list[dict]:
        """What an *amending* instrument changes — the affected instruments, the
        provisions it touches, and how (from both its amends and amended_by edges)."""
        return facade.effects_caused_by(stable_id=stable_id)

    @admin
    def propagate_changes(stable_id: str = "", limit: int = 5) -> dict:
        """Push an amending act's changes OUT to the instruments it affects: mint amends
        edges and flag affected acts we hold for re-pull, so a new act's amendments reach
        old legislation that might never be fetched again. Pass a stable_id for one act,
        or none to scan a bounded batch of held legislation."""
        if stable_id:
            return facade.propagate_changes_from(stable_id=stable_id)
        return facade.propagate_changes(limit=limit)

    @admin
    def create_alias(phrase: str, target_id: str, apply: bool = False) -> dict:
        """Create a shorthand RULE: every occurrence of ``phrase`` (e.g. "UK GDPR")
        links to ``target_id``, propagating across the corpus on extraction. Set
        apply=True to re-extract now."""
        return facade.create_named_alias(phrase=phrase, target_id=target_id, apply=apply)

    @admin
    def list_aliases() -> list[dict]:
        """List the shorthand rules (phrase → document)."""
        return facade.list_named_aliases()

    @admin
    def delete_alias(phrase: str) -> dict:
        """Remove a shorthand rule."""
        return facade.delete_named_alias(phrase=phrase)

    @admin
    def harvest_all_references(limit: int = 25, min_citing: int = 1) -> dict:
        """Drain the routable part of the hanging-reference queue in one pass: fetch
        every high-confidence, adapter-backed reference's exact item and resolve.
        ``limit`` caps how many (most-cited first); ``min_citing`` skips one-offs.
        Leaves un-routable / low-confidence references for manual handling."""
        return facade.harvest_all_references(limit=limit, min_citing=min_citing)

    @admin
    def snowball(limit: int = 50, only_unharvestable: bool = False) -> list[dict]:
        """The citation frontier (§5a): forms the corpus cites but doesn't yet hold,
        grouped by (form, jurisdiction, adapter) and ranked by how often they're
        cited. Each row says whether an adapter can fetch it now, or whether it's a
        frequently-cited body with no adapter yet (a build-an-adapter signal — set
        only_unharvestable=True to see just those). Feeds the harvest snowball."""
        return facade.snowball(limit=limit, only_unharvestable=only_unharvestable)

    @admin
    def import_case_base64(content_base64: str, filename: str, ref: Optional[str] = None,
                           neutral_citation: Optional[str] = None,
                           also_cited_as: Optional[list[str]] = None,
                           title: Optional[str] = None) -> dict:
        """Import a judgment file (PDF/RTF/HTML/text, base64) as a first-class case: extract
        clean text, detect its own neutral citation from the header, key it by that, and
        alias every other form it's cited by (report citations like "[2022] 1 WLR 2241", the
        chamber-less variant) so all of them resolve to this one document. The robust way to
        add a case TNA/BAILII only offers as a PDF."""
        import base64 as _b64
        return facade.import_case(data=_b64.b64decode(content_base64), filename=filename,
                                  ref=ref, neutral_citation=neutral_citation,
                                  also_cited_as=also_cited_as, title=title)

    @admin
    def harvest_house_of_lords(ids: Optional[str] = None, limit: Optional[int] = None,
                               match_reports: bool = True) -> dict:
        """Scrape the House of Lords archive (publications.parliament.uk, 1996–2009) and
        link classic-reporter citations ("[1998] AC 1") to the harvested cases by matching
        the case name in the citing text against a judgment of the right year. Resolves
        "[YYYY] UKHL N" citations and gives pre-2001 report-only cases a home. Slow (bot-
        gated scrape) — prefer running it as a background job via the API."""
        return facade.harvest_house_of_lords(ids=ids, limit=limit, match_reports=match_reports)

    @admin
    def match_report_citations() -> dict:
        """Match reporter-only citations to already-harvested cases by name + year + a
        plausible reporter, minting an alias per confident match so they resolve (§5b)."""
        return facade.match_report_citations()

    @admin
    def unfetchable_references(limit: int = 200) -> dict:
        """The most-cited references the system CANNOT fetch — classic law reports
        ("[1982] AC 1"), cases cited by name, courts with no adapter — ranked by how often
        the corpus cites them, each with a BAILII link (direct RTF where a neutral citation
        exists, else a citation search) and whether an uploaded file can resolve it. The
        pre-neutral-citation frontier a completeness-minded corpus must source by hand."""
        return facade.unfetchable_references(limit=limit)

    @admin
    def retry_failed_references() -> dict:
        """Clear the harvest cool-down lists so the next drain re-attempts every routable
        reference. Use when a source was merely unavailable and its references were
        wrongly parked — a drain that reports attempting nothing is the tell."""
        return facade.retry_failed_references()

    @admin
    def canlii_budget() -> dict:
        """CanLII API quota state + the Canadian backlogs queued against it (pending
        citations to resolve into metadata stubs, held decisions awaiting enrichment)."""
        return facade.canlii_budget()

    @admin
    def canlii_enrich(limit: int = 200, include_citing: bool = True) -> dict:
        """Decorate held Canadian decisions with CanLII metadata (permalink, docket,
        keywords) + citator edges (cited cases/legislation, capped citing cases), and
        mint parallel-citation aliases so report/CanLII-number citations resolve.
        Budget-metered and resumable; needs RAGLEX_CANLII_API_KEY."""
        return facade.canlii_enrich(limit=limit, include_citing=include_citing)

    @admin
    def rebuild_citation_counts() -> dict:
        """Refresh the citation-frequency roll-up the snowball reads (the live aggregate
        over the citations table is slow at scale, so it's cached; this recomputes it)."""
        return facade.rebuild_citation_counts()

    @admin
    def backfill_edge_keys() -> dict:
        """One-off after upgrade: populate candidate_id/raw_fold on edges written before
        those columns existed, so set-based resolution and the SQL worklist see them."""
        return facade.backfill_edge_keys()

    @admin
    def get_settings() -> dict:
        """View configured settings/credentials (secrets masked; shows env vs file)."""
        return facade.get_settings()

    @admin
    def set_settings(values: dict) -> dict:
        """Set settings/credentials in the file store (env vars still override)."""
        return facade.update_settings(values)

    # -- the single gated entry point for everything that changes the corpus --------
    import inspect as _inspect

    def _op_summary(fn) -> dict:
        doc = (fn.__doc__ or "").strip().split("\n")
        first = " ".join(l.strip() for l in doc).strip()
        sig = _inspect.signature(fn)
        params = [f"{n}{'' if p.default is _inspect._empty else '?'}"
                  for n, p in sig.parameters.items()]
        return {"summary": (first[:200] + ("…" if len(first) > 200 else "")),
                "args": params}

    @mcp.tool()
    def maintenance(op: str = "help", args: Optional[dict] = None) -> dict:
        """The gated admin surface: harvesting, imports, watches, aliases, resolution,
        settings, probes, backfills — every operation that CHANGES the corpus, behind one
        tool so its ~60 schemas don't crowd out the retrieval tools you use most.

        ``maintenance("help")`` lists every op with its one-line purpose and argument names;
        then ``maintenance("<op>", {..args..})`` runs it (e.g.
        ``maintenance("harvest", {"source": "uk-caselaw"})``). For everyday research you
        won't need this — lookup() already fetches silently."""
        if op in ("help", "", "list", "ops"):
            return {"count": len(_MAINT),
                    "note": "call maintenance('<op>', {..args..}); most research needs none of these",
                    "ops": {name: _op_summary(fn) for name, fn in sorted(_MAINT.items())}}
        fn = _MAINT.get(op)
        if fn is None:
            from difflib import get_close_matches
            near = get_close_matches(op, list(_MAINT), n=5)
            return {"error": f"unknown op {op!r}",
                    "did_you_mean": near, "hint": "maintenance('help') lists every op"}
        try:
            return fn(**(args or {}))
        except TypeError as exc:
            return {"error": f"bad arguments for {op!r}: {exc}",
                    "args": _op_summary(fn)["args"],
                    "hint": "maintenance('help') shows each op's arguments"}
        except Exception as exc:  # noqa: BLE001 — surface the failure, don't crash the server
            return {"error": f"{op} failed: {exc}"}

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
