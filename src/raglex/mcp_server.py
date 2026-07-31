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

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import Config
from .core.models import RelationshipType as _RelationshipType
from .facade import Facade

# Tool behaviour hints (MCP spec) — clients read these to decide auto-approval + safety UX.
# Most of the first-class surface is pure corpus reads. lookup() is special: it can silently
# FETCH a missing authority from an external source, so it is neither read-only nor
# closed-world. search() reads the local corpus only.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_FETCHING = ToolAnnotations(readOnlyHint=False, openWorldHint=True, idempotentHint=True)


_INSTRUCTIONS = (
    "RagLex is a legal-research corpus — case law, legislation and regulatory guidance "
    "across many jurisdictions. Orient with overview() (the main jurisdictions held + their "
    "density of case-law / legislation / guidance).\n\n"
    "The workhorse is lookup(citation). Give it a citation, a statute-by-name, or a "
    "stable_id and it resolves the authority and returns its text (a token-cheap preview by "
    "default). Two things make it the front door:\n"
    "• PINPOINT + CITERS IN ONE STEP. lookup('Article 15 GDPR') resolves the GDPR, quotes "
    "Article 15, AND returns the documents that cite THAT article (not the whole "
    "regulation), with jurisdiction/kind facets. You can also pass pincite='Article 15' "
    "explicitly. To browse/filter/sort that citer list, call citing_documents(target, "
    "anchor='Article 15', sort=…, jurisdiction=…) — it is stateless and re-callable, so it "
    "IS your results list to return to.\n"
    "• SILENT FETCH. An authority merely new to the corpus is fetched from its source "
    "(CourtListener, Find Case Law, legislation.gov.uk, CELLAR, HUDOC…); if unfetchable you "
    "get an external URL to read yourself.\n\n"
    "FINDING things — two different questions:\n"
    "• WHICH DOCUMENT is this → search(query) / lookup(citation). They match CITATIONS "
    "and TITLES (and party/act names), so give them a case name, an act, or a citation.\n"
    "• WHERE IS THIS SAID → search_text(query). Full text of the indexed jurisdictions "
    "(call search_coverage() for which, and how many documents). A quoted phrase is "
    "LITERAL — \"duty of care\" will not return \"duties of care\" — and it supports OR, "
    "-exclusion, (grouping), wildcard*, and NEAR/3 proximity. It reports how many "
    "PASSAGES each document matches, which distinguishes a judgment that turns on a "
    "phrase from one that mentions it, and what the matching documents cite in common.\n"
    "There is still NO concept/semantic search: the embedding index is empty, so a "
    "natural-language question ('cases about fairness') matches nothing. Search for words "
    "that would actually appear in the text, or for a name or citation.\n\n"
    "Everything that CHANGES the corpus — harvesting, imports, watches, aliases, settings, "
    "repairs — is behind the single maintenance(op, args) tool. Call maintenance('help') "
    "only when you actually need to modify the corpus."
)


class NotPermitted(Exception):
    """A reader token reached an admin-only tool."""


def _require_admin() -> None:
    """Admin scope, or refuse.

    Mirrors the web exactly: reads are open to a reader, anything that CHANGES the
    corpus is admin-only. maintenance() is the single door to ~60 mutation ops, so
    this is the single place the door is locked — a new op inherits the gate rather
    than having to remember it.

    With OAuth unconfigured there is no token and no gate, which is the same
    trade the HTTP API makes: an unauthenticated deployment is trusted, and a local
    stdio client has the operator's own shell anyway."""
    from .web.mcp_oauth import SCOPE_ADMIN, mcp_oauth_enabled

    if not mcp_oauth_enabled():
        return
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:  # pragma: no cover — SDK without the auth middleware
        return
    token = get_access_token()
    if token is None:
        return  # stdio / in-process: no HTTP request, so no bearer token to check
    if SCOPE_ADMIN not in (token.scopes or []):
        raise NotPermitted(
            "This token is read-only. maintenance() changes the corpus and needs the "
            "admin password at the consent screen; reconnect and authorise as admin.")


def build_server(config: Config | None = None) -> MCPServer:
    facade = Facade(config or Config.from_env())
    # OAuth 2.1 for the HTTP transport (opt-in via RAGLEX_MCP_PASSWORD + RAGLEX_PUBLIC_URL).
    # The SDK wires the whole AS/RS surface from these; we only supply the provider (storage
    # + shared-password consent). Unconfigured → no auth, so stdio/local stays untouched.
    from .web.mcp_oauth import auth_settings, build_provider

    _auth = auth_settings()
    if _auth is not None:
        _provider = build_provider(facade)
        mcp = MCPServer("raglex", instructions=_INSTRUCTIONS,
                      auth_server_provider=_provider, auth=_auth)
        mcp._raglex_oauth_provider = _provider  # retrieved by serve_app for the consent page
    else:
        mcp = MCPServer("raglex", instructions=_INSTRUCTIONS)
        mcp._raglex_oauth_provider = None

    # Maintenance/mutation operations are NOT registered as individual tools (their schemas
    # would swamp an agent's context for tools it rarely uses). Each is collected into
    # ``_MAINT`` by the ``admin`` decorator and reached through the one ``maintenance``
    # dispatcher tool. The retrieval/navigation tools below stay first-class.
    _MAINT: dict = {}

    def admin(fn):
        _MAINT[fn.__name__] = fn
        return fn

    # -- read / research --------------------------------------------------
    @mcp.tool(annotations=_READ_ONLY)
    def search(query: str, k: int = 10, jurisdiction: Optional[str] = None,
               kind: Optional[str] = None, source: Optional[str] = None,
               doc_type: Optional[str] = None, tag: Optional[str] = None,
               year_from: Optional[str] = None) -> dict:
        """Find documents by CITATION or by TITLE / party / act name — the reliable way in.
        It reads the query as a citation first (and hands you to lookup() if it resolves),
        then matches your words against document TITLES and ids.

        NOT a text search: it matches titles and citations, not what documents SAY. For
        words in the body — a phrase, a term of art, a statutory formula — use
        search_text(), which searches the full text of the indexed jurisdictions and
        supports literal quoted phrases. Neither is a concept/semantic search: the
        embedding index is empty, so a natural-language question matches nothing. For a specific provision and who cites it, use lookup(citation,
        pincite=…) → citing_documents(). Scope with ``jurisdiction`` (ISO code like "fr"
        or a name), ``kind`` ("cases" | "administrative" | "legislation" | "guidance"),
        or source/doc_type/tag/year_from."""
        return facade.find(query, k=k, jurisdiction=jurisdiction, kind=kind, source=source,
                           doc_type=doc_type, tag=tag, year_from=year_from)

    @mcp.tool(annotations=_READ_ONLY)
    def search_text(query: str, limit: int = 10, exact: bool = True,
                    jurisdiction: Optional[str] = None, source: Optional[str] = None,
                    doc_type: Optional[str] = None, court: Optional[str] = None,
                    year_from: Optional[int] = None, passages: int = 3) -> dict:
        """Search the FULL TEXT of the corpus — where something is said, rather than
        which document it is.

        A quoted phrase means those characters: ``"duty of care"`` will not return
        "duties of care" (set exact=False for the looser, stemmed reading). Operators:
        ``a OR b``, ``-excluded``, ``-"excluded phrase"``, ``(a OR b) c``, ``neglig*``,
        ``a NEAR/3 b`` (also ``/3`` and ``~3``), and ``"phrase"~4`` for slop.

        Returns per document how many PASSAGES matched and previews of the first few —
        a judgment using a phrase eight times is a different answer from one mentioning
        it once, and a single snippet hides that. Also returns facet counts over the
        WHOLE result set (not the page) and the authorities the matching documents cite
        in common, which is often the fastest route to the leading case.

        Scope it with jurisdiction / source / doc_type / court / year_from (comma-
        separated where several). Call search_coverage() for what is indexed: a
        jurisdiction that is not indexed returns nothing, which is not the same as the
        corpus not holding it."""
        split = lambda v: [x for x in (v or "").split(",") if x]  # noqa: E731
        return facade.freetext_for_agent(
            query, limit=min(limit, 50), exact=exact,
            jurisdictions=split(jurisdiction) or None, sources=split(source) or None,
            doc_type=split(doc_type) or None, court=split(court) or None,
            year_from=year_from, passages=max(0, min(passages, 10)))

    @mcp.tool(annotations=_READ_ONLY)
    def search_coverage() -> dict:
        """Which jurisdictions search_text() actually covers, and how many documents.

        Worth checking before concluding the corpus lacks something: a jurisdiction
        that is held but not indexed is searchable by citation and title, and invisible
        to full-text search."""
        return facade.freetext_index_summary()

    @mcp.tool(annotations=_READ_ONLY)
    def overview() -> dict:
        """The dense, parsimonious balance of holdings — per jurisdiction, how much
        case-law / legislation / guidance is HELD and what can be FETCHED on demand. Read
        this first to know what the corpus can be relied on for."""
        return facade.holdings_overview()

    @mcp.tool(annotations=_READ_ONLY)
    def jurisdictions() -> list[dict]:
        """The selectable jurisdictions (natural-language names) with their held-document
        counts — the vocabulary the ``jurisdiction`` filter on search() accepts."""
        return facade.jurisdictions()

    @mcp.tool(annotations=_FETCHING)
    def lookup(citation: str, pincite: Optional[str] = None, context: int = 1,
               full: bool = False, cited_by: bool = True, similar: bool = True,
               autofetch: bool = True, original: bool = False,
               outline_kind: Optional[str] = None) -> dict:
        """Resolve a CITATION, a statute-by-name, or a stable_id and return one
        self-contained answer. This is the front door — start here.

        PINPOINT A PROVISION + WHO CITES IT. If the citation names a provision
        ("Article 15 GDPR", "Article 17 of Regulation 2016/679", "s. 45 of the DPA 2018"),
        lookup quotes exactly that provision AND returns the documents that cite THAT
        provision — under ``citing`` (total + jurisdiction/kind facets + top rows), with a
        ready-made ``citing.browse`` handle. You can also pass ``pincite`` explicitly
        ("Article 15", "[42]", "at 644"); ``context`` sets how many neighbouring segments
        come with the quote (0 = the pinpoint alone / 1 = some / 2 = lots). To browse /
        filter / sort every citer of that provision, call citing_documents(target, anchor)
        — see ``citing.browse``.

        Legislation with a held consolidation opens at the latest version applicable today.
        The reply identifies the base act; pass ``original=true`` to inspect its original
        text instead. Explicit historical/future dated ids are never redirected.

        NO PINPOINT → metadata + a short text PREVIEW + the structural outline (so you can
        pick a provision to pincite), plus document-level ``cited_by``. ``full=true`` returns
        the whole (capped) text — prefer a pincite, it is exact and cheap. Also returned: the
        ways it is cited (``also_cited_as``) and cocitation neighbours (``similar``).

        The outline is sampled ACROSS segment kinds, with the true totals in
        ``outline_counts``, so a long EU act does not spend the whole outline on recitals
        and never reach an article. ``outline_kind='article'`` (or 'recital', 'section',
        …) returns that one kind in full.

        SILENT FETCH: an authority merely NEW to the corpus but routable (US case via
        CourtListener, a UK case/act, an EU/ECHR item) is fetched and returned. Only when it
        can't be fetched do you get an external LII/BAILII URL to read yourself. You rarely
        need to harvest by hand."""
        return facade.lookup(citation=citation, pincite=pincite, context=context, full=full,
                             cited_by=cited_by, similar=similar, autofetch=autofetch,
                             original=original, outline_kind=outline_kind)

    @mcp.tool(annotations=_READ_ONLY)
    def citing_documents(target: str, anchor: Optional[str] = None, sort: str = "pagerank",
                         jurisdiction: Optional[str] = None, kind: Optional[str] = None,
                         offset: int = 0, limit: int = 20) -> dict:
        """The browsable list of documents that CITE ``target`` — the results list you page,
        filter and sort, and return to. ``target`` is a citation or a stable_id.

        Pin it to ONE provision with ``anchor`` ("Article 15", "s. 45", "[42]") to get
        exactly the documents that cite THAT article, not the whole instrument — this is the
        answer to "which cases cite Article 15 of the GDPR". Then:
        • ``sort``: pagerank (most authoritative, default) | cited | newest | oldest | passages
        • ``jurisdiction``: an ISO code ("fr", "gb", "eu") or a name — narrow to one place
        • ``kind``: "cases" | "administrative" (DPA/regulator decisions) | "legislation" | "guidance"
        • ``offset`` / ``limit``: page through (the reply's ``how_to_browse`` gives the next offset)

        Each row carries an inline snippet of where it cites, its OSCOLA cite, court,
        jurisdiction, kind and date; ``facets`` shows the whole citer set so you know what you
        can narrow to. Stateless — call again with the same args to come back to these
        results, or open any row with lookup(its stable_id)."""
        return facade.citing_documents(target, anchor=anchor, sort=sort,
                                       jurisdiction=jurisdiction, kind=kind,
                                       offset=offset, limit=limit)

    @mcp.tool(annotations=_READ_ONLY)
    def list_documents(source: Optional[str] = None, doc_type: Optional[str] = None,
                       tag: Optional[str] = None, query: Optional[str] = None,
                       limit: int = 100) -> list[dict]:
        """Browse/filter documents — e.g. iterate the sections of a law to augment."""
        return facade.list_documents(source=source, doc_type=doc_type, tag=tag,
                                     query=query, limit=limit)

    @mcp.tool(annotations=_READ_ONLY)
    def get_document(stable_id: str, original: bool = False,
                     relations_limit: int = 50, incoming_limit: int = 50,
                     offset: int = 0, brief: bool = True) -> dict:
        """Full document: metadata, tags, relations, attachments, and a
        ``preparatory_documents`` availability/count flag when legislative history exists.
        Base legislation opens at today's applicable consolidation; pass
        ``original=true`` for the original/base text.

        The two edge lists are PAGED — ``relations`` (what this document cites) and
        ``incoming`` (what cites it) each return ``*_limit`` rows from ``offset``, with
        the true totals in ``relations_total`` / ``incoming_total``. A heavily-cited
        instrument otherwise returned hundreds of fully-nested rows in one response.
        ``brief=False`` restores the full nested OSCOLA object per incoming row; by
        default each carries its citation as a string instead."""
        target = facade.canonical_read_target(stable_id, original=original)
        result = dict(facade.get_document(target["stable_id"]))
        offset = max(0, int(offset))
        for key, limit in (("relations", relations_limit), ("incoming", incoming_limit)):
            rows = result.get(key) or []
            result[f"{key}_total"] = len(rows)
            page = rows[offset:offset + max(1, min(int(limit), 500))]
            if brief and key == "incoming":
                # The nested src_oscola object per row is most of the response weight and
                # its rendered text is the only part a reader uses.
                page = [
                    {**row, "src_oscola": (row.get("src_oscola") or {}).get("text")
                     if isinstance(row.get("src_oscola"), dict) else row.get("src_oscola")}
                    for row in page
                ]
            result[key] = page
            result[f"{key}_offset"] = offset
        result["read_target"] = target
        return result

    @mcp.tool(annotations=_READ_ONLY)
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

    @mcp.tool(annotations=_READ_ONLY)
    def get_document_body(stable_id: str, original: bool = False,
                          segments_only: bool = False, offset: int = 0,
                          limit: Optional[int] = None) -> dict:
        """The document's full text + structural segments (legislation articles /
        sections, judgment paragraphs) with their citable labels and levels. Base
        legislation defaults to today's applicable consolidation; pass
        ``original=true`` to read the original. A consolidation's unchanged recitals
        are returned separately as ``inherited_recitals`` with original-act provenance;
        they are live projections, not copied consolidation text.

        A long act CANNOT be returned whole — the DPA 2018 is 1,222 segments and exceeds
        the 1 MB tool ceiling outright. Two ways through:

        * ``segments_only=True`` — every label, kind, level and char offset, no text. For
          structural work (picking a provision to pincite, building a correlation table)
          this is the whole answer, in one cheap call.
        * ``offset``/``limit`` — a character window, carrying the segments and citations
          that overlap it; ``window.next_offset`` walks the rest.
        """
        target = facade.canonical_read_target(stable_id, original=original)
        result = facade.document_body(
            target["stable_id"], offset=offset, limit=limit,
            segments_only=segments_only)
        result["read_target"] = target
        return result

    @mcp.tool(annotations=_READ_ONLY)
    def get_provision(stable_id: str, label: Optional[str] = None,
                      char_start: Optional[int] = None, context: int = 1,
                      original: bool = False) -> dict:
        """ONE provision/paragraph of a document by its citable label ("Article 17",
        "Recital 47", "s. 45", "[42]") — or by char offset — with N context segments
        either side and the heading breadcrumb. A recital requested from a consolidation
        transparently reads the unchanged recital from its original act and reports that
        provenance. Prefer this over get_document_body when you need to quote a single
        provision exactly: it's pinpoint-accurate and token-cheap."""
        target = facade.canonical_read_target(stable_id, original=original)
        result = facade.get_provision(
            target["stable_id"], label=label, char_start=char_start, context=context)
        result["read_target"] = target
        return result

    @mcp.tool(annotations=_READ_ONLY)
    def graph_neighbours(stable_id: str, relationship_types: Optional[list[str]] = None) -> dict:
        """1-hop typed citation/commentary neighbourhood of a document, most
        authoritative neighbours first (PageRank-ranked, design §3c).

        One row per (neighbour, relationship, direction). ``passages`` is how many edges
        that row stands for and ``anchor_pairs`` lists their pinpoints — so four
        per-provision edges to the same act read as one row of four, not as one edge.
        ``relationship_types`` filters in the query, so a rare edge type is found from
        either end however heavily cited the document is."""
        return facade.graph(stable_id, rel=relationship_types)

    @mcp.tool(annotations=_READ_ONLY)
    def related_documents(stable_id: str, limit: int = 12) -> dict:
        """Related documents via the citation network (not vector similarity):
        ``co_cited`` = most often cited together with this one in the same citing
        document; ``coupled`` = relies on the same authorities (bibliographic
        coupling). The practical "cases like this one" for legal research."""
        return facade.related_documents(stable_id, limit=limit)

    @mcp.tool(annotations=_READ_ONLY)
    def legislative_status(stable_id: str) -> dict:
        """Is this legislation still good law? Unified currency across every jurisdiction RagLex
        holds — UK, EU, France, Germany, Netherlands, Australia, New Zealand, Ireland — mapped
        onto one vocabulary: ``status`` (in_force / amended / corrected / repealed / recast /
        consolidated / prospective / partially_in_force / expired) with a ``status_label``, the
        source's own ``native_status`` token + ``scheme``, in-force/repeal dates, the acts that
        amended/repealed/corrected it and its legal basis, ``point_in_time_capable`` +
        ``as_at``, an ``unapplied_count`` / ``up_to_date`` editorial-lag flag (native for UK &
        AU, inferred elsewhere), and a ``provisions`` list giving per-article/section status +
        validity windows + what changed each, where the source pinpoints it. ``degraded`` = the
        "in force" is inferred from absence of recorded changes, not confirmed. Check this
        before relying on an act — an old directive may have been repealed and recast (e.g.
        Directive 95/46 → GDPR), or its text may predate unapplied amendments."""
        return facade.legislative_status(stable_id)

    @mcp.tool(annotations=_READ_ONLY)
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
    def merge_westlaw_duplicates(apply: bool = False, limit: Optional[int] = None) -> dict:
        """Collapse Westlaw imports held twice: a case keyed by a ``westlaw:`` surrogate
        (report-citation slug, WL number or content hash) that the corpus ALSO holds under
        its real citation, because the RTF was imported before its BAILII/FCL copy. Folds
        the surrogate into the citation-keyed document, carrying text, edges, aliases and
        tags. Matches only on a precise identifier (a parallel report citation or a
        WL/ECLI/CJEU id), never a party name. DRY RUN unless ``apply=True`` — the dry run
        lists every planned merge."""
        return facade.refix_westlaw_imports(apply=apply, limit=limit)

    @admin
    def backfill_ag_names(limit: int = 20000) -> dict:
        """Fill in WHO wrote each held AG Opinion, read from the Opinion's own first page
        ("OPINION OF ADVOCATE GENERAL EMILIOU delivered on 15 May 2025"). CELLAR omits the
        Advocate General and these documents arrive titleless, so their OSCOLA citation
        renders as "…, Opinion of AG" with the name missing. Local text only, no network;
        idempotent."""
        from .jobs import JobManager
        return JobManager(facade, origin="mcp").start(
            "backfill-ag-names", "AG names for held Opinions", {"limit": limit})

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
    def feedback(status: str = "open", limit: int = 200, kind: str | None = None) -> list[dict]:
        """The review queue, newest-seen first — user-submitted Bugs / Feature requests
        from the app's feedback box AND the system's own errors (``kind='error'``: a failed
        job, a warning RagLex logged about itself), each with the message, the page/route
        or logger it came from, and its captured metadata.

        A systemic error is ONE row carrying ``seen_count`` / ``last_seen_at``, not one row
        per occurrence — so "13,862 failures" reads as a single item to fix. Filter with
        ``kind`` (bug | feature | error); close with resolve_feedback once the underlying
        cause is fixed, and the next occurrence opens a fresh row."""
        return facade.list_feedback(status=status or None, limit=limit, kind=kind or None)

    @admin
    def resolve_feedback(feedback_id: int, status: str = "resolved") -> dict:
        """Mark a feedback item handled (status: resolved | open)."""
        return facade.resolve_feedback(feedback_id=feedback_id, status=status)

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
                       src_anchor: Optional[str] = None, dst_anchor: Optional[str] = None,
                       note: Optional[str] = None, dry_run: bool = False) -> dict:
        """Add a typed edge between two documents (e.g. an article 'analyses' a law
        article). Optional pinpoint anchors link a *fragment* of the source to a
        *fragment* of the target — e.g. a handbook's src_anchor='pp. 45-47'
        analyses a law's dst_anchor='Article 17' (use the article/section label
        from get_document_body's segments).

        ``relationship`` is a CLOSED vocabulary — call list_relationship_types() rather
        than guessing; an unknown term is refused, never coerced. Re-running an identical
        edge updates it in place instead of minting a second one. ``note`` records why
        the edge was asserted. ``dry_run=True`` returns what would be written, including
        whether each anchor matches a real segment, and writes nothing."""
        return facade.link(src_id=src_id, dst_id=dst_id, relationship=relationship,
                           src_anchor=src_anchor, dst_anchor=dst_anchor,
                           note=note, dry_run=dry_run)

    @admin
    def list_relationship_types() -> dict:
        """The relationship vocabulary link_documents accepts, grouped by family.
        Read this before writing an edge: an unrecognised term is rejected. Also
        reproduced in maintenance('help'), which needs no admin token."""
        return facade.relationship_types()

    @admin
    def list_manual_links(stable_id: str, limit: int = 500) -> dict:
        """Every HAND-WRITTEN edge into or out of a document, each with its own
        relation_id — which is what delete_manual_link needs. A manual edge on a
        document pair that also has an extracted citation is otherwise invisible as a
        separately addressable thing."""
        return facade.manual_links(stable_id=stable_id, limit=limit)

    @admin
    def delete_manual_link(relation_id: int) -> dict:
        """Retract ONE hand-written edge by its relation_id. Unlike
        correct_citation(suppress=True) — which suppresses the whole relation and would
        take a genuine extracted citation between the same documents down with it — this
        removes only the manual assertion, and refuses to touch anything else."""
        return facade.delete_manual_link(relation_id=relation_id)

    @admin
    def upsert_provision_mappings(
        current_id: str, previous_id: str, mappings: list[dict],
        replace: bool = False, created_by: str = "llm",
        mapping_type: str = "functional_predecessor", dry_run: bool = False,
        return_all: bool = False,
    ) -> dict:
        """Bulk-map corresponding statutory provisions between two laws.

        Direction is current → the other law. Each mapping is
        ``{"current_anchor":"Article 6","previous_anchor":"Article 15",
        "note":"optional explanation","confidence":0.9}``. These mappings preserve
        literal old-law citations and surface them separately beside the current article;
        do not use citation aliases for this purpose. ``replace`` replaces all mappings
        between this pair of laws, enabling an LLM to submit a reviewed correlation table.

        ``mapping_type`` — set per call or per mapping — says what the row CLAIMS, and an
        unknown value is refused rather than silently downgraded:

        * ``functional_predecessor`` (default) — the other provision is an earlier
          iteration this one succeeds (ECD Art 14 → DSA Art 6; DPD → GDPR). Citations to
          it are read as this provision's history.
        * ``equivalent`` — a parallel provision in a companion instrument, both in force
          (GDPR / EUDPR / LED, drafted as one package). Use this rather than asserting
          descent between instruments that never replaced one another.

        Anchors are resolved against each law's own segments as they are written; any
        that matches no provision comes back in ``unresolved_anchors`` (the mapping is
        still stored — a stub document has no segments to match — but you can SEE it).
        ``dry_run=True`` runs those checks and returns the plan without writing.

        BATCHING a large correlation table: send **at most ~50 mappings per call**.
        Beyond that the call has been seen to hit the four-minute tool ceiling — and the
        write is ATOMIC under that timeout, so a timed-out batch stored nothing and the
        correct recovery is to re-send it, smaller. The reply echoes only the rows this
        call wrote (plus ``total_for_pair``); pass ``return_all=True``, or call
        list_provision_mappings, for the whole set.
        """
        return facade.upsert_provision_mappings(
            current_id=current_id, previous_id=previous_id, mappings=mappings,
            replace=replace, created_by=created_by, mapping_type=mapping_type,
            dry_run=dry_run, return_all=return_all)

    @admin
    def list_provision_mappings(stable_id: str) -> dict:
        """List every current→previous provision mapping across one law, with inherited
        mention counts. Use this before editing or replacing an existing map."""
        return facade.provision_mappings(stable_id=stable_id)

    @admin
    def inherited_provision_mentions(
        stable_id: str, current_anchor: Optional[str] = None, limit: int = 600,
    ) -> dict:
        """Documents that literally cited a mapped previous provision, projected as
        functional history for the current provision. Results remain marked inherited."""
        return facade.inherited_provision_mentions(
            stable_id=stable_id, current_anchor=current_anchor, limit=limit)

    @admin
    def delete_provision_mapping(mapping_id: int) -> dict:
        """Remove one editorial provision mapping by its mapping_id."""
        return facade.delete_provision_mapping(mapping_id=mapping_id)

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
    def repair_eu_annexes(limit: int = 100000, after_stable_id: str = "") -> dict:
        """Start a resumable local repair of held EU Formex packages whose annexes
        were split into secondary XML members. Reparse and citation re-extraction are
        checkpointed together, so MCP and the web maintenance action behave identically."""
        from .jobs import JobManager
        return JobManager(facade, origin="mcp").start(
            "repair-eu-annexes",
            "repair split EU Formex annexes",
            {"limit": limit, "after_stable_id": after_stable_id},
        )

    @admin
    def backfill_eu_consolidations(max_pages: Optional[int] = None) -> dict:
        """Start the reverse Cellar sweep over every sector-0 dated expression,
        including future-effective snapshots. Each is linked to its sector-3 base act;
        the walk is offset-checkpointed and held versions deduplicate before download."""
        from .jobs import JobManager
        return JobManager(facade, origin="mcp").start(
            "harvest-source",
            "backfill all EU dated consolidations (CELLAR)",
            {
                "source": "eu-legislation",
                "backfill": True,
                "max_pages": max_pages,
                "options": {"consolidations_only": "true"},
                "force_full": True,
                "resume_unfinished": True,
            },
        )

    @admin
    def sync_eu_act_consolidations(stable_id: str) -> dict:
        """Start an immediate deduplicated Cellar lookup for every dated expression
        of one sector-3 CELEX. This is the same self-healing operation the reader starts
        automatically when an EU base act has no held consolidation lineage."""
        from .jobs import JobManager
        return JobManager(facade, origin="mcp").start(
            "sync-eu-consolidations",
            f"import consolidations for {stable_id}",
            {"stable_id": stable_id},
        )

    @admin
    def backfill_eu_case_metadata(limit: int = 500) -> dict:
        """Augment harvested CJEU cases from the EUR-Lex webservice with the official
        case name + subject-matter tags (the free CELLAR data omits these). Batched +
        quota-friendly; needs EURLEX_USERNAME/PASSWORD in settings. Runs as a background
        job (one external call per 50 cases) — poll it for progress. The scheduler runs
        the same job daily when the 'eu-case-names' task is enabled."""
        from .jobs import JobManager
        return JobManager(facade, origin="mcp").start(
            "backfill-eu-case-names", "EU case names + subjects (EUR-Lex)",
            {"limit": limit})

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
        legislation, CJEU case number) and report the routable candidates. No fetching."""
        return facade.detect_citations(text=text)

    @admin
    def source_catalog() -> list[dict]:
        """Per-source capabilities: what each harvestable source pulls, whether
        keywords are searched at the API vs post-filtered, and its options."""
        return facade.source_catalog()

    @admin
    def create_watch(name: str, spec: dict, cadence_minutes: int = 1440, enabled: bool = True) -> dict:
        """Save a harvest plan run on a cadence. ``spec`` keys: ``source``
        (+ ``source_options`` and ``keywords`` — searched at the API where supported, else
        post-filtered), ``discover`` ({"citing": id} — find NEW cases citing a target),
        ``enrich`` (default true: fetch what each new case cites, one hop), ``max_pages``,
        ``tag``, ``backfill``."""
        return facade.create_watch(name=name, spec=spec, cadence_minutes=cadence_minutes, enabled=enabled)

    @admin
    def list_watches() -> list[dict]:
        """List saved watches with their spec, cadence, and last run/result."""
        return facade.list_watches()

    @admin
    def run_watch(watch_id: int) -> dict:
        """Run one watch now: harvest the source delta / discover citing cases, fetch what
        each new case cites (one hop), tag."""
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
        """Import the official current European Convention on Human Rights (ETS No. 5)
        as ``echr/convention``, segmented through ``Article 5(1)(a)``-style anchors."""
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
    def citation_frontier(limit: int = 50, only_unharvestable: bool = False) -> list[dict]:
        """The citation frontier (§5a): forms the corpus cites but doesn't yet hold,
        grouped by (form, jurisdiction, adapter) and ranked by how often they're
        cited. Each row says whether an adapter can fetch it now, or whether it's a
        frequently-cited body with no adapter yet (a build-an-adapter signal — set
        only_unharvestable=True to see just those). The completeness worklist."""
        return facade._citation_frontier(limit=limit, only_unharvestable=only_unharvestable)

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
        """Refresh the citation-frequency roll-up the worklist reads (the live aggregate
        over the citations table is slow at scale, so it's cached; this recomputes it)."""
        return facade.rebuild_citation_counts()

    @admin
    def backfill_edge_keys() -> dict:
        """One-off after upgrade: populate candidate_id/raw_fold on edges written before
        those columns existed, so set-based resolution and the SQL worklist see them."""
        return facade.backfill_edge_keys()

    @admin
    def db_health() -> dict:
        """Diagnose a sluggish database: planner-stat freshness, table bloat (dead tuples),
        sequential-scan-heavy tables (missing-index hints), unused indexes, buffer-cache hit
        ratio, connection pressure, and the longest-running queries — with actionable hints.
        Read-only (system catalogs/stat views only)."""
        return facade.db_health()

    @admin
    def db_maintenance(analyze: bool = True, vacuum: bool = False) -> dict:
        """Refresh planner statistics (ANALYZE — the cheap big lever after the corpus grows)
        and optionally reclaim bloat (VACUUM ANALYZE, online). Run db_health() first."""
        return facade.db_maintenance(analyze=analyze, vacuum=vacuum)

    @admin
    def enrich_eu_legislation(limit: int = 200) -> dict:
        """Harvest EU acts' act-to-act CDM relationships from CELLAR (repeals / amends /
        corrects / legal-basis, both directions) and store them, so an old directive learns
        it was repealed/recast (e.g. Directive 95/46 → repealed_by GDPR) and legislative_
        status() lights up. Bounded + resumable; needs network to CELLAR."""
        from .jobs import JobManager
        return JobManager(facade, origin="mcp").start(
            "enrich-eu-legislation", "enrich EU legislation (CELLAR)", {"limit": limit})

    @admin
    def maintenance_run(no_repairs: bool = False, no_rescans: bool = False,
                        no_rollups: bool = False) -> dict:
        """Run the serial DB-maintenance + repair pass as one background job: safe repairs →
        re-extract never-extracted sources → ANALYZE → citation-count + PageRank roll-ups,
        ONE task at a time (never over-parallelises). No LLM needed. Poll the job for
        progress. Preview with maintenance_plan()."""
        from .jobs import JobManager
        return JobManager(facade, origin="mcp").start(
            "maintenance-run", "DB maintenance + repair",
            {"no_repairs": no_repairs, "no_rescans": no_rescans, "no_rollups": no_rollups})

    @admin
    def maintenance_plan() -> dict:
        """Preview the serial maintenance queue (ordered steps) without running it."""
        return facade.maintenance_plan()

    @admin
    def scheduled_tasks() -> dict:
        """List recurring scheduler tasks with their per-task enabled/cadence + the global
        pause state."""
        return facade.list_scheduled_tasks()

    @admin
    def set_scheduled_task(name: str, enabled: Optional[bool] = None,
                           every_minutes: Optional[int] = None, remove: bool = False,
                           at_hour: Optional[int] = None) -> dict:
        """Enable/disable a scheduler task, set its cadence, or pin it to one UTC hour
        (``at_hour=4`` for 04:00; the heavy roll-ups default there). ``remove`` reverts to
        default. Names: scheduled_tasks() lists them."""
        return facade.set_scheduled_task(name, enabled=enabled,
                                         every_minutes=every_minutes, remove=remove,
                                         at_hour=at_hour)

    @admin
    def hpc_embed(go: bool = False, pilot: Optional[int] = None,
                  model: Optional[str] = None, dimensions: Optional[int] = None) -> dict:
        """Drive the UCL-Myriad bulk-embed relay (export→ship→qsub→poll→fetch→import) as one
        resumable, queue-aware, deadline-guarded job. DRY-RUN unless go=True — it prints the
        plan + every remote command without touching the cluster; a paid GPU submission is
        always explicit. Scoped by RAGLEX_EMBED_JURISDICTIONS. Needs SSH access to the
        RAGLEX_HPC_HOST alias. Returns the started job (poll it for progress)."""
        from .jobs import JobManager
        params = {"go": go, "pilot": pilot, "model": model, "dimensions": dimensions}
        return JobManager(facade, origin="mcp").start(
            "hpc-embed", "HPC embed relay" + ("" if go else " (dry-run)"),
            {k: v for k, v in params.items() if v is not None})

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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                          idempotentHint=False, openWorldHint=True))
    def maintenance(op: str = "help", args: Optional[dict] = None) -> dict:
        """The gated admin surface: harvesting, imports, watches, aliases, resolution,
        settings, probes, backfills — every operation that CHANGES the corpus, behind one
        tool so its ~60 schemas don't crowd out the retrieval tools you use most.

        ``maintenance("help")`` lists every op with its one-line purpose and argument names;
        then ``maintenance("<op>", {..args..})`` runs it (e.g.
        ``maintenance("harvest", {"source": "uk-caselaw"})``). For everyday research you
        won't need this — lookup() already fetches silently.

        Needs an ADMIN token: the consent screen takes either the admin or the reader
        password, and only the admin one yields a token that may change anything.
        help/list is readable by anyone, so a reader can still see what exists."""
        if op in ("help", "", "list", "ops"):
            return {"count": len(_MAINT),
                    "note": "call maintenance('<op>', {..args..}); most research needs none of these",
                    # The closed vocabulary link_documents accepts, in the one place a
                    # caller is guaranteed to look. It used to be discoverable only by
                    # writing an edge and reading back what landed.
                    "relationship_types": sorted(
                        r.value for r in _RelationshipType),
                    "ops": {name: _op_summary(fn) for name, fn in sorted(_MAINT.items())}}
        # everything past the help listing changes the corpus
        _require_admin()
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
