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
from .a29wp import A29WPAdapter
from .au_legislation import CommonwealthAdapter, LawMakerAdapter
from .au_caselaw import AustralianCaseLawAdapter
from .au_nsw_caselaw import NSWCaselawAdapter
from .au_fca_caselaw import FCACaselawAdapter
from .au_hca_caselaw import HCACaselawAdapter
from .ca_caselaw import CanadianCaseLawAdapter
from .ca_legislation import CanadaFederalAdapter
from .canlii import CanLIIAdapter
from .courtlistener import CourtListenerAdapter
from .courtlistener_bulk import CourtListenerBulkAdapter
from .dma import DMACasesAdapter
from .hk_legislation import HKLegislationAdapter
from .nz_legislation import NZLegislationAdapter
from .sg_legislation import SGLegislationAdapter
from .echr import ECHRAdapter
from .edpb import EDPBAdapter
from .eu_cellar import EUCellarAdapter
from .fr_conseil_etat import FrConseilEtatAdapter
from .fr_dila import FrDilaAdapter
from .fr_judilibre import FrJudilibreAdapter
from .fr_legislation import FrLegislationAdapter
from .gdprhub import GDPRhubAdapter
from .uk_ipa_codes import UKIPACodesAdapter
from .de_gii import DeGiiAdapter
from .de_neuris import DeNeurisAdapter
from .de_rii import DeRiiAdapter
from .ofcom import OfcomOSAAdapter
from .ofcom_enforcement import OfcomEnforcementAdapter
from .eu_legislation import EULegislationAdapter
from .eu_preparatory import EUPreparatoryAdapter
from .hol import HouseOfLordsAdapter
from .ie_legislation import IrishRevisedActsAdapter, IrishStatuteBookAdapter
from .nl_legislation import NLLegislationAdapter
from .nl_rechtspraak import NLRechtspraakAdapter
from .nz_caselaw import NZSupremeCourtAdapter
from .uk_caselaw import UKCaseLawAdapter
from .uk_legislation import UKLegislationAdapter


def _scrape_factory(recipe):
    return lambda **kw: RecipeScrapeAdapter(recipe, **kw)


# Factory per source key. Build steps 5+ (FR/DE/CH) add rows here.
ADAPTERS: dict[str, Callable[..., Adapter]] = {
    "uk-caselaw": UKCaseLawAdapter,
    # UK FTT — General Regulatory Chamber (information rights, environment, charity…).
    "uk-grc": lambda **kw: UKCaseLawAdapter(court="ukftt/grc", **kw),
    # Netherlands — Rechtspraak Open Data, ECLI-native, citation graph included.
    "nl-rechtspraak": NLRechtspraakAdapter,
    # EU — CELLAR SPARQL + Formex; CJEU case law relative to a named instrument/case.
    "eu-cellar": EUCellarAdapter,
    # ECHR — HUDOC; resolves by ECLI (ECLI:CE:ECHR:…) OR application number (58170/13).
    "echr": ECHRAdapter,
    # House of Lords (1996–2009) — scraped from publications.parliament.uk. Resolves
    # "[YYYY] UKHL N" and gives pre-2001 report-only cases a home (§5a).
    "uk-hol": HouseOfLordsAdapter,
    # EDPB (§1.9/§4a) — the Board's whole document register (guidelines, opinions,
    # binding decisions, statements, reports…), classified by the guidance machinery.
    "edpb": EDPBAdapter,
    # EDPB one-stop-shop register — ~2,600 Art 60 final DPA decisions, EDPBI-keyed,
    # split by lead SA, with interprets edges to the GDPR articles they apply.
    "edpb-oss": lambda **kw: EDPBAdapter(register=True, **kw),
    # Article 29 Working Party (1997–2018, closed archive) — the justice-site
    # opinion/recommendation index + the newsroom items, WP-number identity.
    "a29wp": A29WPAdapter,
    # UK Investigatory Powers Act 2016 codes of practice (Home Office guidance) — a
    # fixed one-time import of the nine gov.uk codes; bare section/schedule references
    # are linked to the IPA 2016 (ukpga/2016/25).
    "uk-ipa-codes": UKIPACodesAdapter,
    # GDPRhub (noyb's DP case-report wiki) — DPA decisions + court judgments as
    # structured infobox reports, harvested from the NewPages Atom feed (the site is
    # Anubis-walled, the feed is open). Stored under jurisdiction (court = dpa-xx), the
    # machine translation as body, GDPRhub's analysis as attached commentary, GDPR
    # articles + mined DP instruments as interprets edges.
    "gdprhub": GDPRhubAdapter,
    # Digital Markets Act enforcement cases — the Commission's DMA register via its
    # ODSE search API; every case/decision linked to the DMA (32022R1925).
    "dma-cases": DMACasesAdapter,
    # Ofcom online-safety regulatory documents — Codes of Practice, risk guidance…
    # implementing the Online Safety Act 2023, with supersession version chains.
    "ofcom-osa": OfcomOSAAdapter,
    # Ofcom enforcement actions — one record per investigation/decision (HTML + its
    # case PDFs combined), linked to the OSA sections it turns on.
    "ofcom-enforcement": OfcomEnforcementAdapter,
    # Legislation — statute, not just cases. stable_ids are the resolution targets, so
    # harvesting these closes the §5b loop for every statutory citation in the corpus.
    "uk-legislation": UKLegislationAdapter,
    "eu-legislation": EULegislationAdapter,
    "eu-preparatory": EUPreparatoryAdapter,
    # Ireland — the eISB (Acts + SIs as enacted/made, the OFFICIAL text) and the LRC
    # Revised Acts (administrative consolidations, point-in-time). Both speak ELI, so
    # Ireland is another ELI source beside legislation.gov.uk and EUR-Lex.
    "ie-legislation": IrishStatuteBookAdapter,
    "ie-revised": IrishRevisedActsAdapter,
    # Australia — nine registers, one model. The Commonwealth OData API (au-cth) plus
    # the three LawMaker states that share one adapter (Qld/NSW/Tas). Jurisdiction is a
    # first-class key: stable_ids are au/{juris}/{type}/{year}/{number}.
    "au-cth": CommonwealthAdapter,
    "au-qld": lambda **kw: LawMakerAdapter(jurisdiction="qld", **kw),
    "au-nsw": lambda **kw: LawMakerAdapter(jurisdiction="nsw", **kw),
    "au-tas": lambda **kw: LawMakerAdapter(jurisdiction="tas", **kw),
    # Singapore — Singapore Statutes Online (SSO). Keyless server-rendered HTML, no ELI;
    # keyed by SSO's own act code (sg/act/coa1967). `subsidiary=true` browses the SL listing.
    "sg-legislation": SGLegislationAdapter,
    "sg-sl": lambda **kw: SGLegislationAdapter(subsidiary=True, **kw),
    # Canada federal — the Justice Laws open XML corpus, read from a local clone of
    # justicecanada/laws-lois-xml. Version-controlled primary law: the repo IS the
    # distribution channel, so enumeration and change detection are both offline.
    "ca-federal": CanadaFederalAdapter,
    # Canadian case law — the A2AJ bulk parquet corpus (~223k decisions, 26 courts).
    # Neutral-citation slugs match the extractor's, so importing resolves the Canadian
    # citations the corpus already holds pending; law-report citations become aliases.
    "ca-caselaw": CanadianCaseLawAdapter,
    # CanLII API — Canadian case METADATA + the citator, never full text (their API is
    # metadata-only by design). Resolves pending Canadian citations into metadata-stub
    # documents with a verified "view on CanLII" link, and enriches held decisions
    # with permalinks, keywords and citator edges. Needs an individually-granted key.
    "ca-canlii": CanLIIAdapter,
    # Australian case law — the Open Australian Legal Corpus JSONL. Decisions only by
    # default: the statutes are better served by the live registers (au-cth et al).
    "au-caselaw": AustralianCaseLawAdapter,
    # NSW Caselaw — the LIVE incremental layer over the OALC bulk snapshot. Newest-first
    # crawl of caselaw.nsw.gov.au's browse JSON, stopping at the watermark; neutral-cite
    # identity (nswsc/2024/1) unifies with au-caselaw. Run as a weekly (staggered) watch.
    "au-nsw-caselaw": NSWCaselawAdapter,
    # Federal Court of Australia (+ FCAFC + federal tribunals + Norfolk Island SC) — the
    # live federal layer. Newest-first Funnelback crawl of the judgments database (stealth,
    # sort=date), watermark stop; identity from the URL segment (fca/2026/981) unifies
    # with au-caselaw. Weekly (staggered) watch.
    "au-fca": FCACaselawAdapter,
    # High Court of Australia — the judgments index (hcourt.gov.au), one page per year
    # (no pagination). Imports saved listing HTML (path=) now, or fetches live once a
    # real-Chrome fetch is available (the site WAFs everything else). Metadata-stub
    # judgments keyed hca/2026/22, resolving citations + linking to the HCA site.
    "au-hca": HCACaselawAdapter,
    # US case law — CourtListener v4. Keyed by reporter citation (us/us/576/644), the
    # same slug the US matcher mints, so harvesting a case resolves the citations the
    # corpus already holds pending. Free tier is 125 requests/day, enforced by a
    # persisted budget ledger: this is an on-demand + drip source, never a bulk one.
    "us-caselaw": CourtListenerAdapter,
    # The bulk path for the same corpus — quarterly CSV exports read off local disk,
    # no API and no rate limit. How whole courts (SCOTUS, the circuits) get seeded.
    "us-caselaw-bulk": CourtListenerBulkAdapter,
    # New Zealand Supreme Court — the Courts of NZ RSS feed → case page → judgment PDF,
    # keyed by the neutral citation printed in the PDF ("[2026] NZSC 88" → nzsc/2026/88).
    "nz-caselaw": NZSupremeCourtAdapter,
    # Hong Kong — the e-Legislation bulk XML drop (HKLM schema). Content is local-only
    # by necessity: elegislation.gov.hk robots.txt disallows everything but /sitemap.
    "hk-legislation": HKLegislationAdapter,
    # New Zealand — the PCO Developer API (key required). The website is bot-walled
    # (HTTP 405 human-verification), so there is deliberately no HTML fallback.
    "nz-legislation": NZLegislationAdapter,
    "nl-legislation": NLLegislationAdapter,
    # France — one PISTE-authed family plus the administrative order. Légifrance
    # (fr-legislation) is the ELI resolution target the case-law edges point at; the
    # CNIL and CONSTIT funds ride the same client. Judilibre (fr-judilibre) is the
    # ECLI-native Cour de cassation base with court-authored edges. fr-conseil-etat is
    # the administrative order (opendata.justice-administrative.fr).
    "fr-legislation": FrLegislationAdapter,
    "fr-cnil": lambda **kw: FrLegislationAdapter(fond="CNIL", **kw),
    "fr-constit": lambda **kw: FrLegislationAdapter(fond="CONSTIT", **kw),
    "fr-judilibre": FrJudilibreAdapter,
    "fr-conseil-etat": FrConseilEtatAdapter,
    # Germany — NeuRIS / rechtsinformationen.bund.de (beta), ELI + ECLI native. One
    # adapter, two modes: federal case law (default) and federal legislation (LDML.de).
    "de-neuris": DeNeurisAdapter,
    "de-neuris-legislation": lambda **kw: DeNeurisAdapter(mode="legislation", **kw),
    # Germany bulk seeds (no key): the legacy juris-DTD portals. de-gii = federal
    # statutes (gesetze-im-internet, local clone or gii-toc.xml); de-rii = federal
    # case law (rechtsprechung-im-internet, rii-toc.xml). NeuRIS is the live increment.
    "de-gii": DeGiiAdapter,
    "de-rii": DeRiiAdapter,
    # France bulk seed (no auth): the DILA OPENDATA archives read from local disk. One
    # adapter across funds; the PISTE/Conseil-d'État live adapters handle increments.
    "fr-dila": FrDilaAdapter,  # CASS (Cour de cassation) by default
    "fr-dila-legi": lambda **kw: FrDilaAdapter(fond="LEGI", **kw),
    "fr-dila-jade": lambda **kw: FrDilaAdapter(fond="JADE", **kw),
    "fr-dila-constit": lambda **kw: FrDilaAdapter(fond="CONSTIT", **kw),
    "fr-dila-cnil": lambda **kw: FrDilaAdapter(fond="CNIL", **kw),
    # Scrape recipes (§5a) — regulator portals with no API.
    **{key: _scrape_factory(recipe) for key, recipe in RECIPES.items()},
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
        "uk-grc", "UK FTT — General Regulatory Chamber", "caselaw", "GB", True,
        "The First-tier Tribunal's General Regulatory Chamber (information rights, "
        "environment, charity, and other regulatory appeals). Keywords are full-text "
        "searched at the source.",
        (SourceOption("query", "Keyword query", "free text, searched in the API"),),
        ("neutral citation",),
    ),
    "nl-rechtspraak": SourceInfo(
        "nl-rechtspraak", "NL Rechtspraak (Open Data)", "caselaw", "NL", False,
        "Dutch case law, ECLI-native, with a built-in citation graph. The API indexes "
        "by date/court, so keywords filter the harvested results (Dutch terms work).",
        (SourceOption("path", "Bulk archive path", "OpenDataUitspraken.zip or extracted folder"),
         SourceOption("lido_links", "Import LiDO graph", "true — structured outgoing links")),
        ("ECLI:NL:…",),
    ),
    "eu-cellar": SourceInfo(
        "eu-cellar", "EU CJEU case law (CELLAR / SPARQL)", "caselaw", "EU", False,
        "CJEU judgments + AG opinions discovered relative to a named instrument or case. "
        "Set the instrument to follow (required); keywords post-filter the results.",
        (SourceOption("legislation_celex", "Legislation CELEX to follow", "e.g. 32004R0139"),
         SourceOption("cited_by_celex", "Find cases citing this case", "e.g. 62018CJ0311")),
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
        "uk-legislation", "UK legislation (legislation.gov.uk)", "legislation", "GB", True,
        "Walks the newest-published search feed by default (Akoma Ntoso): an incremental "
        "run imports new legislation as it is made; a backfill walks the whole "
        "back-catalogue for the chosen types. Name ids to fetch specific Acts/SIs; "
        "keywords run a title search at the source.",
        (SourceOption("ids", "Legislation ids", "ukpga/2000/36,ukpga/2018/12"),
         SourceOption("feed", "Follow new-legislation feed", "new"),
         SourceOption("types", "Feed types", "ukpga,uksi (default)"),
         SourceOption("query", "Title search", "e.g. companies")),
        ("legislation id (ukpga/2000/36)", "legislation.gov.uk URI"),
    ),
    "edpb": SourceInfo(
        "edpb", "EDPB documents (guidelines, opinions, decisions…)", "guidance", "EU", False,
        "The whole EDPB document register via its sitemap: guidelines, recommendations, "
        "Art 70 opinions, Art 65 binding decisions, statements, reports, letters. "
        "Incremental on the sitemap's lastmod; drafts are imported and become the "
        "adopted version in place. Slow-paced (europa.eu WAF).",
        (SourceOption("sections", "Only these sections", "e.g. guideline,recommendation,statement"),),
        ("EDPB document page URL",),
    ),
    "edpb-oss": SourceInfo(
        "edpb-oss", "EDPB one-stop-shop register (Art 60 final decisions)", "guidance", "EU", False,
        "~2,600 final national-DPA decisions from the OSS register, keyed by their "
        "EDPBI identifier, split by lead SA (court = dpa-xx), each linked to the GDPR "
        "articles it applies. Scanned PDFs are OCR'd (tesseract) or flagged needs_ocr. "
        "First run walks the whole register (resumable); then incremental by serial.",
        (),
        ("EDPBI identifier (EDPBI:LU:OSS:D:2026:3920)",),
    ),
    "ofcom-enforcement": SourceInfo(
        "ofcom-enforcement", "Ofcom enforcement actions (Online Safety Act)", "guidance", "GB", False,
        "Ofcom's Online Safety Act enforcement register — one record per investigation / "
        "decision / penalty, combining the action's HTML narrative with its case PDFs, and "
        "linked to the OSA sections it turns on. Re-checks each action for updates (new "
        "documents, status changes) via a content hash.",
        (SourceOption("topic", "Enforcement topic id", "67866 = online safety (default)"),),
        ("Ofcom enforcement action",),
    ),
    "ofcom-osa": SourceInfo(
        "ofcom-osa", "Ofcom online-safety documents (Online Safety Act)", "guidance", "GB", False,
        "Ofcom's regulatory documents implementing the Online Safety Act 2023 — Codes of "
        "Practice, risk-assessment guidance, registers of risks. Version chains are "
        "tracked: an updated document supersedes the old one (kept, marked superseded). "
        "Each links to the OSA sections/parts it implements, both ways.",
        (),
        ("Ofcom regulatory document",),
    ),
    "dma-cases": SourceInfo(
        "dma-cases", "Digital Markets Act cases (Commission register)", "guidance", "EU", False,
        "The Commission's DMA enforcement register via its ODSE search API — one document "
        "per case with its full decision timeline, press releases and OJ references, every "
        "case and decision linked to the DMA (Reg. 2022/1925). Incremental on the last "
        "decision date; a new step on an existing case re-fetches it.",
        (),
        ("DMA case number (DMA.100209)",),
    ),
    "a29wp": SourceInfo(
        "a29wp", "Article 29 Working Party (archive, 1997–2018)", "guidance", "EU", False,
        "The EDPB's predecessor: ~250 opinions/recommendations from the old justice-site "
        "index plus ~120 newsroom items (guidelines, letters, press releases). A CLOSED "
        "archive — harvest once; WP numbers key identity and mint citation aliases. "
        "Scanned early-years PDFs are OCR'd or flagged. Slow-paced (europa.eu WAF).",
        (SourceOption("surface", "Surface", "both | justice | newsroom"),),
        ("WP number (WP248)",),
    ),
    "uk-ipa-codes": SourceInfo(
        "uk-ipa-codes", "UK IPA 2016 codes of practice (Home Office)", "guidance", "GB", False,
        "The nine Investigatory Powers Act 2016 codes of practice published by the Home "
        "Office on gov.uk (interception, equipment interference, communications data, bulk "
        "acquisition, bulk personal datasets, notices…). A fixed set fetched through the "
        "stealth tier and stored as guidance under Home Office. Every bare section/schedule "
        "reference — and any tied to 'the Act' — is linked to the Investigatory Powers Act "
        "2016 (ukpga/2016/25), pinpointed; references to a different named Act are left to "
        "the resolver. A maintenance import: safe to re-run or schedule (unchanged pages "
        "dedup, a revised gov.uk page re-ingests via content hash).",
        (),
        ("gov.uk code-of-practice URL",),
    ),
    "gdprhub": SourceInfo(
        "gdprhub", "GDPRhub (DP decisions & analysis)", "caselaw", "EU", False,
        "noyb's GDPRhub wiki: DPA decisions and court judgments on the GDPR as structured "
        "infobox case reports, harvested from the NewPages Atom feed (the site itself is "
        "Anubis-walled; only the feed is pulled, through the stealth tier). Each report is "
        "stored under its jurisdiction (court = dpa-xx) with the machine translation as the "
        "body, GDPRhub's summary + analysis as attached commentary (shown when no "
        "translation exists), and interprets edges to the GDPR articles applied plus any "
        "LED/EUDPR/ePrivacy/Charter/DSA/DMA/AI-Act references mined from the text. ECLI or "
        "native case number is the identity and a resolution alias. Incremental on the "
        "feed's newest-page timestamp. The NewPages feed is a rolling ~90-day window "
        "(MediaWiki prunes recentchanges at 90 days) — run it as a recurring watch for "
        "currency. For the full historical corpus set api=true, which switches discovery to "
        "the MediaWiki API (list=allpages + batched revisions) and backfills every page; "
        "same identity, so the two modes share nodes. New pages only via the feed; later "
        "edits do not resurface there (a re-run of the api backfill picks up edits).",
        (SourceOption("api", "Full-catalogue backfill via API", "true (whole history) | false (feed)"),
         SourceOption("max_pages", "Page/batch cap per run", "feed: ~50 reports/page; api: 500/batch"),),
        ("ECLI:…", "native DPA/court case number", "GDPRhub page URL"),
    ),
    "eu-legislation": SourceInfo(
        "eu-legislation", "EU legislation (CELLAR / Formex)", "legislation", "EU", False,
        "Walks sector-3 legal acts (Regulations, Directives, Decisions) via a CELLAR "
        "SPARQL enumeration by default, newest-first: an incremental run picks up newly "
        "published acts, a backfill pages through the whole series. Name CELEXes to "
        "fetch specific instruments (Formex; articles + recitals). EU primary-law "
        "documents (Charter, TEU, TFEU) are importable by CELEX and retain their ELI + names.",
        (SourceOption("celex", "CELEX ids", "32016R0679,12012P,12016M,12016E"),
         SourceOption("types", "Descriptors to enumerate", "R,L,D,TREATY (default)"),
         SourceOption("years", "Year range", "1990-2026")),
        ("CELEX (32016R0679)", "Treaty/Charter CELEX (12012P)", "Directive/Regulation number"),
    ),
    "eu-preparatory": SourceInfo(
        "eu-preparatory", "EU preparatory and Commission policy documents", "preparatory", "EU", False,
        "Walks EUR-Lex sector 5 through CELLAR: Commission proposals and communications, "
        "JOIN papers, staff working documents, SEC papers and impact assessments. Imports "
        "the official procedure graph linking preparatory papers to proposals and final acts.",
        (SourceOption("celex", "CELEX ids", "52021PC0554,52021SC0551"),
         SourceOption("types", "Document families", "PC,DC,JC,SC,XC (default)"),
         SourceOption("years", "Year range", "2020-2026")),
        ("CELEX (52021PC0554)", "COM/SWD/SEC/JOIN document number"),
    ),
    "ie-legislation": SourceInfo(
        "ie-legislation", "Irish legislation — as enacted (eISB)", "legislation", "IE", False,
        "Acts and Statutory Instruments from the electronic Irish Statute Book, as "
        "enacted / as made — the OFFICIAL text. Walks the yearly indexes (or fetches "
        "named ids), probing xml → print → html because SIs and pre-1922 Acts have no "
        "XML. Harvests the RDFa metadata block for the amendment graph, EU "
        "transposition links and enabling powers, plus the ISBC tables for what "
        "amended each Act and what was made under it.",
        (SourceOption("ids", "Instrument ids", "ie/2018/act/7, S.I. No. 201 of 2016"),
         SourceOption("years", "Years to walk", "2016 or 2016-2018 (default: from 1922)"),
         SourceOption("types", "Resource types", "act,si (default)"),
         SourceOption("isbc", "Fetch amendment tables", "true (default) | false")),
        ("ELI id (ie/2018/act/7)", "No. 7 of 2018", "S.I. No. 201 of 2016",
         "irishstatutebook.ie URL"),
    ),
    "ie-revised": SourceInfo(
        "ie-revised", "Irish legislation — revised (LRC consolidations)", "legislation", "IE", False,
        "The Law Reform Commission's Revised Acts: ~600 Acts consolidated with "
        "amendments applied and annotated, each stamped with the date it consolidates "
        "to. NON-AUTHORITATIVE (administrative consolidation) and flagged as such. "
        "The list's 'Updated to' column is the whole change signal, so a new "
        "consolidation is detected without fetching a document; each becomes a new "
        "point-in-time record rather than overwriting the last.",
        (SourceOption("ids", "Limit to these Acts", "ie/2003/act/32"),
         SourceOption("language", "Language", "en (default) | ga")),
        ("ELI id (ie/2003/act/32)",),
    ),
    "au-cth": SourceInfo(
        "au-cth", "Australian Commonwealth legislation (Federal Register, OData API)",
        "legislation", "AU", True,
        "The Federal Register of Legislation via its keyless OData v4 API: query Acts / "
        "instruments by filter, page with $skip. Gives the amendment graph as structured "
        "edges (statusHistory), the point-in-time compilation series, the originating "
        "Bill link and name history, all inline. Body text from the register's "
        "unzipped-EPUB HTML. Incremental by asMadeRegisteredAt.",
        (SourceOption("ids", "Title ids", "C1901A00002 or au/cth/act/1901/2"),
         SourceOption("collection", "Collection", "Act (default) | LegislativeInstrument"),
         SourceOption("filter", "Extra OData $filter", "year eq 2024"),
         SourceOption("principal_only", "Principal titles only", "true (default) | false")),
        ("FRL Title id (C1901A00002)", "au/cth/act/1901/2", "legislation.gov.au URL"),
    ),
    "au-qld": SourceInfo(
        "au-qld", "Queensland legislation (LawMaker)", "legislation", "AU", False,
        "Queensland Acts and subordinate legislation via LawMaker's deterministic "
        "/view/whole/html/{status}/{date}/{docid} URLs. Default discovery is the crawler "
        "feed (recently-changed deltas — the incremental path). For a full-catalogue "
        "backfill set enumerate=true (optionally years=1990-2026) to walk every "
        "{type}-{year}-{n}. Point-in-time is a path segment.",
        (SourceOption("ids", "Document ids", "act-2016-001, sl-2023-0107"),
         SourceOption("enumerate", "Full-catalogue backfill", "true"),
         SourceOption("years", "Year range to enumerate", "1990-2026"),
         SourceOption("types", "Types:width to enumerate", "act:3,sl:4 (default)"),
         SourceOption("status", "View status", "inforce (default) | asmade | repealed")),
        ("LawMaker docid (act-2016-001)", "au/qld/act/2016/1"),
    ),
    "au-nsw": SourceInfo(
        "au-nsw", "New South Wales legislation (LawMaker)", "legislation", "AU", False,
        "NSW Acts and regulations via LawMaker's deterministic point-in-time URLs. NSW "
        "has no headless-reachable feed, so discovery is either named ids or a "
        "full-catalogue enumerate=true backfill (years=…) that walks every "
        "{type}-{year}-{n}.",
        (SourceOption("ids", "Document ids", "act-1900-088"),
         SourceOption("enumerate", "Full-catalogue backfill", "true"),
         SourceOption("years", "Year range to enumerate", "1990-2026"),
         SourceOption("types", "Types:width to enumerate", "act:3,sl:4,epi:4 (default)"),
         SourceOption("status", "View status", "inforce (default) | asmade | repealed")),
        ("LawMaker docid", "au/nsw/act/1900/88"),
    ),
    "au-tas": SourceInfo(
        "au-tas", "Tasmania legislation (LawMaker)", "legislation", "AU", False,
        "Tasmanian Acts and statutory rules via LawMaker's deterministic point-in-time "
        "URLs and its crawler feed (deltas — the incremental path). For a full-catalogue "
        "backfill set enumerate=true (optionally years=…).",
        (SourceOption("ids", "Document ids", "act-2000-019, sr-2026-046"),
         SourceOption("enumerate", "Full-catalogue backfill", "true"),
         SourceOption("years", "Year range to enumerate", "1990-2026"),
         SourceOption("types", "Types:width to enumerate", "act:3,sr:3 (default)"),
         SourceOption("status", "View status", "inforce (default) | asmade | repealed")),
        ("LawMaker docid", "au/tas/act/2000/19"),
    ),
    "sg-legislation": SourceInfo(
        "sg-legislation", "Singapore legislation (Singapore Statutes Online)",
        "legislation", "SG", False,
        "Singapore Statutes Online (sso.agc.gov.sg): keyless, server-rendered HTML, no ELI "
        "and no search API (robots.txt disallows /search, crawl-delay 6s). Browses the "
        "current Acts / subsidiary-legislation listings and fetches each document, keyed by "
        "SSO's own act code (sg/act/coa1967). Large Acts lazy-load their provision bodies, "
        "backfilled section-by-section via ?ProvIds. Seed the bulk from the SSO parquet "
        "snapshot first (import_sg_seed); this keeps it current.",
        (SourceOption("subsidiary", "Browse subsidiary legislation", "true | false (default)"),
         SourceOption("ids", "SSO act codes", "CoA1967, SCJA1969-N2"),
         SourceOption("max_backfill", "Max lazy-loaded sections to fetch", "400 (default)")),
        ("SSO act code (CoA1967)", "sg/act/coa1967", "sso.agc.gov.sg URL"),
    ),
    "ca-federal": SourceInfo(
        "ca-federal", "Canada federal legislation (Justice Laws XML)", "legislation",
        "CA", False,
        "All consolidated federal Acts and Regulations, read from a local clone of "
        "justicecanada/laws-lois-xml. Enumeration and change detection come from the "
        "repo's own lookup manifest (each document's consolidation date is the change "
        "signal), so a full run needs no network at all; set pull=true to git-pull "
        "first. Gives provision-level point-in-time (lims:inforce-start-date), the "
        "regulation→enabling-Act edge, and the Act→regulations-made-under-it edge. "
        "English and French are equally authoritative — lang selects which to ingest.",
        (SourceOption("path", "Path to laws-lois-xml clone", "/path/to/laws-lois-xml"),
         SourceOption("lang", "Language", "eng (default) | fra | both"),
         SourceOption("types", "Types", "act,regulation (default)"),
         SourceOption("ids", "Limit to these", "C-46, SOR/2018-69, ca/act/a-1"),
         SourceOption("include_repealed", "Include repealed laws", "true | false (default)"),
         SourceOption("pull", "git pull before run", "true | false (default)")),
        ("chapter code (C-46)", "instrument number (SOR/2018-69)", "ca/act/c-46"),
    ),
    "ca-caselaw": SourceInfo(
        "ca-caselaw", "Canadian case law (A2AJ bulk corpus)", "caselaw", "CA", False,
        "~223k full-text decisions from 26 Canadian courts and tribunals, imported from "
        "the A2AJ parquet dataset on disk (one folder per court). Neutral-citation ids "
        "match the citation extractor's, so importing RESOLVES the Canadian citations "
        "already pending in the corpus; law-report citations ([1999] 2 SCR 817) are "
        "minted as aliases so they resolve too. Ships its own citation network, so "
        "cites edges land at import. A2AJ is a secondary source — flagged as such.",
        (SourceOption("path", "Path to the A2AJ dataset", "/data/corpora/canadian-case-law"),
         SourceOption("courts", "Limit to these courts", "SCC,FCA,ONCA"),
         SourceOption("min_year", "Earliest decision year", "2000"),
         SourceOption("language", "Preferred text language", "en (default) | fr")),
        ("neutral citation (2011 SCC 10)", "scc/2011/10"),
    ),
    "ca-canlii": SourceInfo(
        "ca-canlii", "Canadian case law metadata (CanLII API)", "caselaw", "CA", False,
        "CanLII's REST API: per-case metadata (title, parallel citations, decision "
        "date, docket, subject keywords, the canlii.ca permalink) and the CITATOR — "
        "what a case cites (cases + legislation) and what cites it. NEVER full text: "
        "a fetched case becomes a metadata stub with a verified 'view on CanLII' "
        "link, under the same slug the citation extractor mints, so pending Canadian "
        "citations resolve. Needs an API key (granted individually via CanLII's "
        "feedback form); politeness enforced by a persisted budget ledger.",
        (SourceOption("ids", "Cases to fetch", "2011 SCC 10, scc/2011/10"),
         SourceOption("databases", "Databases to poll", "csc-scc (default), onca, bcca…"),
         SourceOption("citator", "Fetch citator edges", "true for ids (default) | false"),
         SourceOption("citing_cap", "Max citing-cases edges per case", "200 (default)"),
         SourceOption("detail", "Per-case metadata call", "true (default) | false")),
        ("neutral citation (2011 SCC 10)", "scc/2011/10", "CanLII caseId (2011scc10)"),
    ),
    "au-caselaw": SourceInfo(
        "au-caselaw", "Australian case law (Open Australian Legal Corpus)", "caselaw",
        "AU", False,
        "Australian decisions from Isaacus' Open Australian Legal Corpus — a single "
        "large JSONL file on disk, streamed. Imports decisions only by default: the "
        "corpus also carries statutes, but the live registers (au-cth, au-nsw…) give "
        "point-in-time compilations and an amendment graph a flat dump cannot. "
        "Neutral-citation ids match the extractor's, so this resolves the Australian "
        "citations already pending. Secondary source — flagged as such.",
        (SourceOption("path", "Path to corpus.jsonl", "/data/corpora/au-corpus.jsonl"),
         SourceOption("types", "Document types", "decision (default) | primary_legislation"),
         SourceOption("jurisdictions", "Limit to jurisdictions", "new_south_wales,commonwealth"),
         SourceOption("min_year", "Earliest decision year", "2000")),
        ("neutral citation ([2020] NSWSC 1)", "nswsc/2020/1"),
    ),
    "au-nsw-caselaw": SourceInfo(
        "au-nsw-caselaw", "NSW Caselaw (live incremental)", "caselaw", "AU", False,
        "The currency layer for Australian case law: a newest-first incremental crawl of "
        "caselaw.nsw.gov.au's browse index (the same source the Open Australian Legal "
        "Corpus creator scrapes), stopping at the watermark so a weekly run pulls only new "
        "decisions. Judgment HTML is the body; PDF-only decisions fall back to their asset "
        "PDF (OCR-flagged if scanned). Keyed by the medium neutral citation (nswsc/2024/1), "
        "so a live decision is the same node as its OALC-snapshot copy and resolves the "
        "'[2024] NSWSC 1' citations already held pending. Best run as a staggered weekly watch.",
        (),
        ("neutral citation ([2024] NSWSC 1)", "nswsc/2024/1", "caselaw.nsw.gov.au decision id"),
    ),
    "au-fca": SourceInfo(
        "au-fca", "Federal Court of Australia (live incremental)", "caselaw", "AU", False,
        "The federal currency layer over the OALC bulk: a newest-first crawl of the Federal "
        "Court judgments database (search.judgments.fedcourt.gov.au, Funnelback, sort=date), "
        "covering FCA, the Full Court (FCAFC), the federal tribunals (IRCA/ACOMPT/ACOPYT/"
        "ADFDAT/FPDT) and the Supreme Court of Norfolk Island. The search WAFs plain HTTP, so "
        "it runs through the stealth tier. Identity is the neutral-citation slug read from the "
        "judgment URL (fca/2026/981), unifying with au-caselaw and resolving pending "
        "'[2026] FCA 981' citations. Stops at the watermark — run as a staggered weekly watch.",
        (),
        ("neutral citation ([2026] FCA 981)", "fca/2026/981", "judgments.fedcourt.gov.au URL"),
    ),
    "au-hca": SourceInfo(
        "au-hca", "High Court of Australia (judgments index)", "caselaw", "AU", False,
        "The High Court judgments index (hcourt.gov.au) — one server-rendered page per year "
        "(items_per_page=100; the Court delivers well under 100 a year, so no pagination). "
        "The site WAFs everything but a real desktop Chrome, so run it either by importing "
        "saved listing HTML (path= a year page saved from Chrome) or live once a real-Chrome "
        "fetch is available (years=all|2020-2026|current). Each judgment becomes a metadata "
        "stub keyed by its neutral citation (hca/2026/22) — coram, date and a 'view on the "
        "High Court' link, resolving pending '[2026] HCA 22' citations; full text is a later "
        "enrichment once the judgment pages can be fetched.",
        (SourceOption("path", "Saved listing HTML", "a year page (or folder) saved from Chrome"),
         SourceOption("years", "Years to fetch live", "current (default) | all | 2020-2026")),
        ("neutral citation ([2026] HCA 22)", "hca/2026/22", "hcourt.gov.au judgment URL"),
    ),
    "us-caselaw": SourceInfo(
        "us-caselaw", "US case law (CourtListener API)", "caselaw", "US", False,
        "US federal case law from CourtListener (Free Law Project). Cases are stored "
        "under their reporter citation (us/us/576/644) — the same id the citation "
        "matcher mints — so pulling one resolves every pending reference to it, in "
        "every parallel reporter. Needs a free API token "
        "(courtlistener.com/profile/api-token/). "
        "The free tier allows 125 requests/day, enforced by a persisted budget: give "
        "citation ids to fetch specific cases, or leave blank for an incremental poll "
        "of the named courts. Seed whole courts with us-caselaw-bulk instead — this "
        "API cannot afford a backfill.",
        (SourceOption("ids", "Citations to fetch", "576 U.S. 644, us/f3d/347/1200"),
         SourceOption("cluster_ids", "CourtListener cluster ids", "2812209"),
         SourceOption("courts", "Courts to poll", "scotus,ca9 (default: SCOTUS + circuits)"),
         SourceOption("prefer_html", "Also store display HTML", "true | false (default)")),
        ("reporter citation (576 U.S. 644)", "us/us/576/644",
         "CourtListener cluster id / opinion URL"),
    ),
    "us-caselaw-bulk": SourceInfo(
        "us-caselaw-bulk", "US case law (CourtListener bulk CSV)", "caselaw", "US", False,
        "The quarterly CourtListener bulk exports, read from a local directory — no "
        "API and no rate limit, which is the only practical way to seed whole courts. "
        "Point `path` at the downloaded CSVs (courts, dockets, opinion-clusters, "
        "opinions, citation map) and set `courts` to the allowlist you actually want: "
        "the exports are whole-table snapshots of every US jurisdiction, so filtering "
        "on the way in is what keeps a SCOTUS+circuits seed from ingesting millions of "
        "district-court rows. Ids and aliases match the API adapter's exactly, so bulk "
        "and on-demand rows are the same nodes. Re-point at a fresh quarterly drop to "
        "refresh; identical rows dedup on content hash.",
        (SourceOption("path", "Bulk export directory", "/corpora/courtlistener"),
         SourceOption("courts", "Court allowlist", "scotus,ca1,ca2… (required in practice)"),
         SourceOption("min_year", "Earliest decision year", "1900"),
         SourceOption("citation_map", "Import the citation graph", "true (default) | false")),
        ("reporter citation (576 U.S. 644)", "us/us/576/644"),
    ),
    "nz-caselaw": SourceInfo(
        "nz-caselaw", "New Zealand Supreme Court (Courts of NZ RSS)", "caselaw",
        "NZ", False,
        "Every NZ Supreme Court judgment from the Courts of NZ RSS feed (2004–present). "
        "Each case page's judgment PDF is fetched and parsed layout-aware: the neutral "
        "citation printed in the PDF is the identity (\"[2026] NZSC 88\" → nzsc/2026/88), "
        "numbered paragraphs become citable segments, and footnotes are lifted into a "
        "preserved zone so their authorities still resolve. Party names come from the case "
        "page. Incremental by the RSS pubDate; a backfill walks the whole feed. Polite 10s "
        "floor between requests, widening automatically if the court rate-limits.",
        (SourceOption("rss_url", "RSS feed URL", "defaults to the Supreme Court feed"),
         SourceOption("rss_path", "Local RSS fallback", "path to a saved feed XML")),
        ("neutral citation ([2026] NZSC 88)", "nzsc/2026/88"),
    ),
    "hk-legislation": SourceInfo(
        "hk-legislation", "Hong Kong legislation (e-Legislation bulk XML)", "legislation",
        "HK", False,
        "The consolidated Hong Kong statute book from the Department of Justice bulk XML "
        "drop — Ordinances, subsidiary legislation and the Basic Law instruments. "
        "Content is read from the local drop and never fetched over HTTP: "
        "elegislation.gov.hk's robots.txt disallows all paths but /sitemap. Each "
        "chapter's consolidation date is encoded in its filename, so re-pointing at a "
        "refreshed drop imports only what changed. check_sitemap=true additionally "
        "reports chapters that exist upstream but are missing from the drop.",
        (SourceOption("path", "Path to bulk XML drop", "/path/to/hkleg"),
         SourceOption("ids", "Limit to these chapters", "486, 571, cap.1"),
         SourceOption("check_sitemap", "Report chapters missing from the drop", "true"),
         SourceOption("include_repealed", "Include repealed", "true (default) | false")),
        ("chapter number (Cap. 486)", "hk/cap/486"),
    ),
    "nz-legislation": SourceInfo(
        "nz-legislation", "New Zealand legislation (PCO Developer API)", "legislation",
        "NZ", True,
        "Acts, secondary legislation and Bills via the Parliamentary Counsel Office's "
        "Developer API. REQUIRES an API key (set RAGLEX_NZ_API_KEY) — without one the "
        "source yields nothing by design: the legislation website is bot-walled (HTTP "
        "405 human-verification), so there is deliberately no scraping fallback. "
        "Point-in-time is native (each consolidation is its own addressable version). "
        "Title keywords are searched at the API. The PCO's amendment annotations are "
        "kept out of the body text (they are ~35% of a large act) and recorded as "
        "amendment edges instead.",
        (SourceOption("legislation_type", "Type", "act (default) | secondary-legislation | bill"),
         SourceOption("query", "Title search", "e.g. privacy"),
         SourceOption("ids", "Work ids", "act_public_1990_109"),
         SourceOption("status", "Status", "in_force | not_in_force"),
         SourceOption("agency", "Administering agency", "e.g. Ministry of Justice")),
        ("work id (act_public_1990_109)", "nz/act/public/1990/109"),
    ),
    "nl-legislation": SourceInfo(
        "nl-legislation", "NL legislation (KOOP / BWB)", "legislation", "NL", False,
        "Dutch consolidated legislation via the KOOP SRU service; supports topic "
        "discovery by rechtsgebied. Keywords post-filter the results.",
        (SourceOption("rechtsgebied", "Legal area", "e.g. staats- en bestuursrecht"),
         SourceOption("all_records", "Entire BWB", "true — paginate every SRU record"),
         SourceOption("ids", "BWB identifiers", "BWBR0040940,BWBR0045754"),
         SourceOption("version_date", "Exact historical date", "YYYY-MM-DD"),
         SourceOption("path", "KOOP bulk path", "multi-part .7z / zip / extracted XML folder")),
    ),
    "fr-legislation": SourceInfo(
        "fr-legislation", "France — Légifrance (codes, PISTE)", "legislation", "FR", False,
        "Consolidated French statute law via DILA's Légifrance API on the PISTE gateway "
        "(needs free PISTE credentials — one app also serves fr-judilibre). Fund LEGI "
        "enumerates every consolidated code via /list/code; each article carries an ELI "
        "and a full version history (mapped onto document versions for point-in-time). "
        "Name LEGITEXT/LEGIARTI ids or an ELI to fetch specific instruments.",
        (SourceOption("ids", "Instrument ids", "LEGITEXT000006070721, LEGIARTI000006419292"),
         SourceOption("fond", "Fund", "LEGI (default) | CNIL | CONSTIT | JORF")),
        ("ELI id", "LEGITEXT/LEGIARTI id", "legifrance.gouv.fr URL"),
    ),
    "fr-cnil": SourceInfo(
        "fr-cnil", "France — CNIL deliberations (Légifrance)", "guidance", "FR", False,
        "The French DPA's deliberations, harvested through the same Légifrance/PISTE "
        "client (fund CNIL) — a high-relevance addition to the EDPB/ICO guidance layer.",
        (), ("CNILTEXT id",),
    ),
    "fr-constit": SourceInfo(
        "fr-constit", "France — Conseil constitutionnel (Légifrance)", "caselaw", "FR", False,
        "Conseil constitutionnel decisions via Légifrance/PISTE (fund CONSTIT).",
        (), ("CONSTEXT id", "ECLI:FR:CC:…"),
    ),
    "fr-judilibre": SourceInfo(
        "fr-judilibre", "France — Cour de cassation (Judilibre)", "caselaw", "FR", False,
        "The Cour de cassation open-data judgment base via Judilibre on PISTE (shares "
        "credentials with fr-legislation). ECLI-native and incremental: discovery walks "
        "/export by update date, each decision's functional zones (motivations, "
        "dispositif…) become citable segments, and the court-authored textes appliqués "
        "and rapprochements become typed edges to legislation and case law.",
        (SourceOption("ids", "Decision ids/ECLIs", "ECLI:FR:CCASS:2021:C100400"),),
        ("ECLI:FR:CCASS:…", "Judilibre decision id"),
    ),
    "fr-conseil-etat": SourceInfo(
        "fr-conseil-etat", "France — administrative order (Conseil d'État)", "caselaw",
        "FR", False,
        "The administrative court order (Conseil d'État, cours administratives d'appel, "
        "tribunaux administratifs) from opendata.justice-administrative.fr — the "
        "complete set, ECLI-native (ECLI:FR:CE:…). Where most data-protection and "
        "public-law litigation sits. The search endpoint is undocumented, so it is read "
        "defensively; verify live before a backfill.",
        (), ("ECLI:FR:CE:…", "numéro de dossier"),
    ),
    "de-neuris": SourceInfo(
        "de-neuris", "Germany — federal case law (NeuRIS, beta)", "caselaw", "DE", False,
        "Federal court decisions (BVerfG, BGH, BAG, BFH, BSG, BVerwG, BPatG) from the "
        "official rechtsinformationen.bund.de open API — ECLI-native, anonymised, 2010 "
        "onward. BETA: endpoints may change, data still filling. Daily watermark.",
        (SourceOption("ids", "Document numbers/ECLIs", "ECLI:DE:BGH:2021:..."),),
        ("ECLI:DE:…", "NeuRIS document number"),
    ),
    "de-neuris-legislation": SourceInfo(
        "de-neuris-legislation", "Germany — federal legislation (NeuRIS, beta)",
        "legislation", "DE", False,
        "Consolidated federal laws and ordinances (BGB, SGB, GG, BDSG…) from "
        "rechtsinformationen.bund.de — ELI-native, served as LegalDocML.de (the German "
        "AKN profile), so §/Abs./Satz become chunk units. BETA; only current versions "
        "are reachable by ELI today (point-in-time is a known gap).",
        (SourceOption("ids", "ELIs", "eli/bund/bgbl-1/..."),),
        ("ELI id", "Jurabk (BGB, BDSG)"),
    ),
    "de-gii": SourceInfo(
        "de-gii", "Germany — federal statutes bulk (gesetze-im-internet)",
        "legislation", "DE", False,
        "The no-key bulk seed: every federal statute as juris gii-norm XML. Point `path` "
        "at a local clone of the gesetze-im-internet corpus (one folder per law) for "
        "offline enumeration + change detection off each file's builddate; leave it blank "
        "to fetch gii-toc.xml and pull per-law zips. Keyed by the abbreviation "
        "(de/gesetz/bgb). Current versions only — NeuRIS is the live increment.",
        (SourceOption("path", "Local gii clone", "/data/corpora/gesetze-im-internet"),
         SourceOption("ids", "Limit to abbreviations", "BGB,BDSG,SGB V")),
        ("Jurabk (BGB)", "de/gesetz/bgb"),
    ),
    "de-rii": SourceInfo(
        "de-rii", "Germany — federal case law bulk (rechtsprechung-im-internet)",
        "caselaw", "DE", False,
        "The no-key case-law bulk seed: BVerfG, the five supreme federal courts and the "
        "BPatG (2010→), anonymised, ECLI-native, as juris rii XML. Fetches rii-toc.xml "
        "and pulls each decision, or reads a local `path` of rii XML files. Every seeded "
        "decision resolves the ECLI:DE: citations the corpus already holds.",
        (SourceOption("path", "Local rii folder", "/data/corpora/rechtsprechung-im-internet"),),
        ("ECLI:DE:…",),
    ),
    "fr-dila": SourceInfo(
        "fr-dila", "France — DILA OPENDATA bulk seed", "caselaw", "FR", False,
        "The no-auth offline seed from the echanges.dila.gouv.fr/OPENDATA archives (read "
        "from local disk — a directory of extracted XML or a .tar.gz). One adapter across "
        "the funds via `fond`: CASS (default, Cour de cassation), CAPP, JADE "
        "(administrative), CONSTIT, CNIL, and LEGI (legislation). Same ECLI / Légifrance "
        "identifiers as the live PISTE adapters, so seeding resolves pending citations. "
        "Apply the daily deltas after the Freemium global snapshot to stay current.",
        (SourceOption("path", "Path to DILA archives/dir", "/data/corpora/dila/CASS"),
         SourceOption("fond", "Fund", "CASS (default) | CAPP | JADE | CONSTIT | CNIL | LEGI")),
        ("ECLI:FR:…", "Légifrance JURI id", "LEGIARTI id"),
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
        # incremental "check for new" makes sense for feed-like sources: the caselaw
        # feeds, UK legislation's newest-published search feed (feed=new), and the
        # EDPB sitemap/register cursors. The other legislation/by-id sources are
        # fetched by naming the item — no moving feed.
        row["can_incremental"] = (row.get("kind") == "caselaw"
                                  or key in ("uk-legislation", "eu-preparatory", "edpb", "edpb-oss", "dma-cases",
                                             "ofcom-osa", "ofcom-enforcement",
                                             # year cursor / "Updated to" cursor
                                             "ie-legislation", "ie-revised",
                                             # asMadeRegisteredAt cursor / crawler-feed deltas
                                             "au-cth", "au-qld", "au-tas",
                                             # consolidation-date cursors: the Canadian
                                             # manifest's LastConsolidationDate, the HK
                                             # drop's filename timestamp, and the NZ
                                             # API's most_recently_updated sort
                                             "ca-federal", "hk-legislation",
                                             "nz-legislation",
                                             # Légifrance code /list/code lastUpdate cursor,
                                             # CNIL fund search date, NeuRIS published-from
                                             "fr-legislation", "fr-cnil",
                                             "de-neuris-legislation",
                                             # gii builddate change-detection cursor
                                             "de-gii"))
        out.append(row)
    return out


def get_adapter(source_key: str, **kwargs) -> Adapter:
    try:
        factory = ADAPTERS[source_key]
    except KeyError:
        known = ", ".join(sorted(ADAPTERS))
        raise KeyError(f"unknown source {source_key!r}; known: {known}") from None
    return factory(**kwargs)
