"""Adapter registry — a new jurisdiction is one new entry (§1.5).

Adapters self-register here so the CLI/orchestrator can look them up by source key
without importing each module. Keep factories lazy and side-effect-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..core.adapter import Adapter
from ..scraping.recipes import RECIPES
from ..scraping.scrape_adapter import RecipeScrapeAdapter
from .echr import ECHRAdapter
from .eu_cellar import EUCellarAdapter
from .eu_legislation import EULegislationAdapter
from .hol import HouseOfLordsAdapter
from .nl_legislation import NLLegislationAdapter
from .nl_rechtspraak import NLRechtspraakAdapter
from .uk_caselaw import UKCaseLawAdapter
from .uk_legislation import UKLegislationAdapter


def _scrape_factory(recipe):
    return lambda **kw: RecipeScrapeAdapter(recipe, **kw)


# Factory per source key. Build steps 5+ (FR/DE/CH) add rows here.
ADAPTERS: dict[str, Callable[..., Adapter]] = {
    "uk-caselaw": UKCaseLawAdapter,
    # UK FTT(GRC) — the info-rights / data-protection tribunal (§2, §4).
    "uk-grc": lambda **kw: UKCaseLawAdapter(court="ukftt/grc", **kw),
    # Netherlands — Rechtspraak Open Data, ECLI-native, citation graph included.
    "nl-rechtspraak": NLRechtspraakAdapter,
    # EU — CELLAR SPARQL + Formex; CJEU case law interpreting the GDPR (§2, §6).
    "eu-cellar": EUCellarAdapter,
    # ECHR — HUDOC; resolves by ECLI (ECLI:CE:ECHR:…) OR application number (58170/13).
    "echr": ECHRAdapter,
    # House of Lords (1996–2009) — scraped from publications.parliament.uk. Resolves
    # "[YYYY] UKHL N" and gives pre-2001 report-only cases a home (§5a).
    "uk-hol": HouseOfLordsAdapter,
    # Legislation (§0) — statute, not just cases. stable_ids are the resolution
    # targets so harvesting these closes the §5b loop (FOIA, DPA, GDPR, …).
    "uk-legislation": UKLegislationAdapter,
    "eu-legislation": EULegislationAdapter,
    "nl-legislation": NLLegislationAdapter,
    # Scrape recipes (§5a) — regulator portals with no API.
    **{key: _scrape_factory(recipe) for key, recipe in RECIPES.items()},
}


# Sources that are in-scope by construction (§4) — tagged, not topic-gated:
# the GRC tribunal, GDPR-linked CJEU cases, and in-scope regulator scrape recipes.
IN_SCOPE_SOURCES: set[str] = {"uk-grc", "eu-cellar", "echr"} | {
    key for key, recipe in RECIPES.items() if recipe.in_scope
}


# -- source capabilities (so the UI can morph per source) -------------------
@dataclass(frozen=True)
class SourceOption:
    name: str          # the adapter kwarg (-o name=value)
    label: str
    placeholder: str = ""


@dataclass(frozen=True)
class SourceInfo:
    key: str
    label: str
    kind: str           # caselaw | legislation | scrape
    jurisdiction: str   # GB | EU | NL
    keyword_search: bool  # True: keywords are searched in the source API (precise);
    #                       False: keywords post-filter what's harvested (any-term match)
    description: str
    options: tuple[SourceOption, ...] = field(default_factory=tuple)
    # The identifier forms this source can fetch a *single item* by (targeted harvest)
    # — what a new adapter declares so the resolver/UI know how to route a citation.
    identifiers: tuple[str, ...] = field(default_factory=tuple)


SOURCE_INFO: dict[str, SourceInfo] = {
    "uk-caselaw": SourceInfo(
        "uk-caselaw", "UK Find Case Law", "caselaw", "GB", True,
        "All courts/tribunals on the National Archives’ Find Case Law. Keywords are "
        "full-text searched at the source; newest first.",
        (SourceOption("court", "Court filter", "e.g. ewca/civ, uksc, ukftt/grc"),
         SourceOption("query", "Keyword query", "free text, searched in the API")),
        ("neutral citation (e.g. [2024] EWCA Civ 1)", "Find Case Law document URI"),
    ),
    "uk-grc": SourceInfo(
        "uk-grc", "UK FTT — General Regulatory Chamber (info rights / DP)", "caselaw", "GB", True,
        "The information-rights / data-protection tribunal. In-scope by construction "
        "(not topic-gated). Keywords are full-text searched at the source.",
        (SourceOption("query", "Keyword query", "free text, searched in the API"),),
        ("neutral citation",),
    ),
    "nl-rechtspraak": SourceInfo(
        "nl-rechtspraak", "NL Rechtspraak (Open Data)", "caselaw", "NL", False,
        "Dutch case law, ECLI-native, with a built-in citation graph. The API indexes "
        "by date/court, so keywords filter the harvested results (Dutch terms work).",
        (SourceOption("court", "Court filter", "e.g. Hoge Raad"),),
        ("ECLI:NL:…",),
    ),
    "eu-cellar": SourceInfo(
        "eu-cellar", "EU CJEU case law (CELLAR / SPARQL)", "caselaw", "EU", False,
        "CJEU judgments + AG opinions discovered by what legislation they interpret. "
        "Set the instrument to follow; keywords post-filter the results.",
        (SourceOption("legislation_celex", "Legislation CELEX to follow", "e.g. 32016R0679 (GDPR)"),),
        ("CJEU case CELEX (62018CJ0511)", "ECLI:EU:C:…"),
    ),
    "echr": SourceInfo(
        "echr", "ECHR case law (HUDOC)", "caselaw", "CoE", False,
        "ECtHR judgments fetched by ECLI (ECLI:CE:ECHR:…) or application number (58170/13) "
        "— give either as ids.",
        (SourceOption("ids", "ECLIs or application numbers", "58170/13, ECLI:CE:ECHR:2021:0525JUD005817013"),),
        ("ECLI:CE:ECHR:…", "application no. 58170/13"),
    ),
    "uk-legislation": SourceInfo(
        "uk-legislation", "UK legislation (legislation.gov.uk)", "legislation", "GB", False,
        "Fetches specific Acts/SIs by id (Akoma Ntoso). Defaults to the core FOI/DP "
        "instruments; override with ids. Keywords don’t apply (you name the acts).",
        (SourceOption("ids", "Legislation ids", "ukpga/2000/36,ukpga/2018/12"),),
        ("legislation id (ukpga/2000/36)", "legislation.gov.uk URI"),
    ),
    "eu-legislation": SourceInfo(
        "eu-legislation", "EU legislation (CELLAR / Formex)", "legislation", "EU", False,
        "Fetches specific instruments by CELEX (Formex; articles + recitals). Defaults "
        "to the GDPR; override with celex. Keywords don’t apply.",
        (SourceOption("celex", "CELEX ids", "32016R0679,32002L0058"),),
        ("CELEX (32016R0679)", "Directive/Regulation number"),
    ),
    "nl-legislation": SourceInfo(
        "nl-legislation", "NL legislation (KOOP / BWB)", "legislation", "NL", False,
        "Dutch consolidated legislation via the KOOP SRU service; supports topic "
        "discovery by rechtsgebied. Keywords post-filter the results.",
        (SourceOption("rechtsgebied", "Legal area", "e.g. staats- en bestuursrecht"),),
    ),
}


# Sources that support forward-citation discovery (find NEW documents that cite a target,
# via the live source) — the renewing kind of watch. uk-caselaw uses Find Case Law's
# full-text search; eu-cellar walks CELLAR's citation graph.
DISCOVER_CITING_SOURCES = frozenset({"uk-caselaw", "uk-grc", "eu-cellar"})
# Sources whose ids are sequential neutral citations, so a court/year can be gap-scanned.
GAP_SCAN_SOURCES = frozenset({"uk-caselaw"})


def source_catalog() -> list[dict]:
    """Capabilities per harvestable source — what it pulls, whether keywords are
    searched at the API vs post-filtered, whether it supports incremental "new since last
    run" harvest, forward-citation discovery, and neutral-citation gap-scanning. Drives the
    Maintain page's per-source capability chips + explanations."""
    from dataclasses import asdict

    out = []
    for key in sorted(ADAPTERS):
        info = SOURCE_INFO.get(key)
        if info is None:  # scrape recipes + anything without a descriptor
            row = {"key": key, "label": key, "kind": "scrape", "jurisdiction": "",
                   "keyword_search": False, "options": [], "identifiers": [],
                   "description": "Scraped source (regulator portal). Keywords post-filter."}
        else:
            row = asdict(info)
        # capability flags the UI turns into plain-language chips
        row["can_keyword_search"] = bool(row.get("keyword_search"))
        row["can_discover_citing"] = key in DISCOVER_CITING_SOURCES
        row["can_gap_scan"] = key in GAP_SCAN_SOURCES
        # incremental "check for new" makes sense for feed-like caselaw sources; the
        # legislation/by-id sources are fetched by naming the item, not by a moving feed.
        row["can_incremental"] = row.get("kind") == "caselaw"
        out.append(row)
    return out


def get_adapter(source_key: str, **kwargs) -> Adapter:
    try:
        factory = ADAPTERS[source_key]
    except KeyError:
        known = ", ".join(sorted(ADAPTERS))
        raise KeyError(f"unknown source {source_key!r}; known: {known}") from None
    return factory(**kwargs)
