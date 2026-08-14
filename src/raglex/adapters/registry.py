"""Adapter registry — a new jurisdiction is one new entry (§1.5).

Adapters self-register here so the CLI/orchestrator can look them up by source key
without importing each module. Keep factories lazy and side-effect-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..core.adapter import Adapter
from ..core.models import DocType
from ..scraping.recipes import RECIPES
from ..scraping.scrape_adapter import RecipeScrapeAdapter
from .a29wp import A29WPAdapter
from .au_legislation import CommonwealthAdapter, LawMakerAdapter
from .au_caselaw import AustralianCaseLawAdapter
from .au_nsw_caselaw import NSWCaselawAdapter
from .au_fca_caselaw import FCACaselawAdapter
from .au_hca_caselaw import HCACaselawAdapter
from .au_vic_legislation import VictoriaLegislationAdapter
from .au_sa_legislation import SouthAustraliaLegislationAdapter
from .au_wa_legislation import WesternAustraliaLegislationAdapter
from .au_esafety import ESafetyOnlineSafetyAdapter
from .ca_caselaw import CanadianCaseLawAdapter
from .ca_lexum import CanadianLexumAdapter
from .ca_legislation import CanadaFederalAdapter
from .canlii import CanLIIAdapter
from .courtlistener import CourtListenerAdapter
from .courtlistener_bulk import CourtListenerBulkAdapter
from .berec import BERECAdapter
from .dma import DMACasesAdapter
from .eu_dma_policy import DMAAnnualReportsAdapter, DMAConsultationsAdapter
from .hk_legislation import HKLegislationAdapter
from .nz_legislation import NZLegislationAdapter
from .sg_legislation import SGLegislationAdapter
from .echr import ECHRAdapter
from .edpb import EDPBAdapter
from .eu_cellar import EUCellarAdapter
from .eu_curia_observations import EUCuriaObservationsAdapter
from .eu_euipo import EUIPOPublicationsAdapter
from .fr_conseil_etat import FrConseilEtatAdapter
from .fr_dila import FrDilaAdapter
from .fr_judilibre import FrJudilibreAdapter
from .fr_legislation import FrLegislationAdapter
from .gdprhub import GDPRhubAdapter
from .uk_ipa_codes import UKIPACodesAdapter
from .uk_ipt import UKIPTAdapter
from .de_gii import DeGiiAdapter
from .de_bundestag import BundestagDrucksachenAdapter, BundestagWDAdapter
from .de_openlegaldata import DeOpenLegalDataAdapter
from .de_neuris import DeNeurisAdapter
from .de_rii import DeRiiAdapter
from .at_ris import APPLICATIONS as RIS_APPLICATIONS, AustrianRISAdapter
from .sk_ress import SlovakRESSAdapter
from .fi_finlex import SERIES as FINLEX_SERIES, FinlexAdapter
from .se_domstol import SwedishCaseLawAdapter
from .se_domstol_bulk import SwedishCaseLawBulkAdapter
from .ee_lahend import EstonianLahendAdapter
from .ofcom import OfcomOSAAdapter
from .ofcom_enforcement import OfcomEnforcementAdapter
from .eu_legislation import EULegislationAdapter
from .eu_ep_followups import EPFollowUpsAdapter
from .eu_ep_thinktank import EPThinkTankAdapter
from .eu_ep_resolutions import EPResolutionsAdapter
from .eu_preparatory import EUPreparatoryAdapter
from .eu_ombudsman import EUOmbudsmanAdapter
from .eu_edps import EDPSInvestigationsAdapter, EDPSOpinionsAdapter
from .eu_dgcomp import DGCompAntitrustAdapter
from .eu_consumer_guidance import EUConsumerGuidanceAdapter
from .eu_dpa_guidance import (
    AEPDGuidanceAdapter,
    CNILGuidanceAdapter,
    DSKGuidanceAdapter,
    DatatilsynetGuidanceAdapter,
    GBAGuidanceAdapter,
    GaranteGuidanceAdapter,
)
from .eu_regulator_registers import (
    ESMASanctionsAdapter,
    ESAsBoardOfAppealAdapter,
    SRBAppealPanelAdapter,
)
from .hol import HouseOfLordsAdapter
from .ie_caselaw import IrishCaseLawAdapter
from .ie_dpc import IrishDPCAdapter, IrishDPCGuidanceAdapter
from .ie_tax_appeals import IrishTaxAppealsAdapter
from .ie_revenue_tdm import IrishRevenueTDMAdapter
from .ie_ccpc_mergers import IrishCCPCMergerAdapter
from .ie_legislation import IrishRevisedActsAdapter, IrishStatuteBookAdapter
from .ie_oireachtas import OireachtasLaidAdapter
from .ie_oireachtas_committees import OireachtasCommitteeEvidenceAdapter
from .nl_legislation import NLLegislationAdapter
from .nl_rechtspraak import NLRechtspraakAdapter
from .nl_acm_guidance import ACMGuidanceAdapter
from .be_gba_decisions import GBADecisionsAdapter
from .uk_parl_committees import UKCommitteePublicationsAdapter
from .uk_parl_written_questions import UKWrittenQuestionsAdapter
from .nl_ap import APDocumentsAdapter
from .it_agcm import AGCMBulletinAdapter
from .nz_caselaw import NZSupremeCourtAdapter
from .uk_caselaw import UKCaseLawAdapter
from .uk_cat import CompetitionAppealTribunalAdapter
from .uk_cpr import UKCivilProcedureRulesAdapter
from .uk_cps_guidance import CPSProsecutionGuidanceAdapter
from .uk_et import UKEmploymentTribunalAdapter
from .uk_govuk_regulator import GOVUKRegulatorAdapter
from .uk_fca_notices import FCANoticesAdapter
from .uk_ico import ICOAdapter
from .uk_legislation import UKLegislationAdapter
from .uk_legislation_materials import UKLegislationMaterialsAdapter
from .uk_ftt_ir import InformationRightsAdapter
from .eu_digital_strategy import DigitalStrategyLibraryAdapter
from .uk_judiciary import JudiciaryGuidanceAdapter
from .uk_lawcom import LawCommissionReportsAdapter
from .uk_parl_library import ParliamentLibraryAdapter
from .scot_spice import SPICeBriefingsAdapter
from .uk_ipco import IPCOPublicationsAdapter
from .uk_isc import ISCReportsAdapter
from .uk_ehrc import EHRCAdapter
from .uk_ofgem import OfgemPublicationsAdapter
from .uk_ofs import OfSPublicationsAdapter
from .parliamentary_reports import (
    AssembleeInformationReportsAdapter,
    SenatComparativeLawAdapter,
    SenatInformationReportsAdapter,
    TweedeKamerReportsAdapter,
)


def _scrape_factory(recipe):
    return lambda **kw: RecipeScrapeAdapter(recipe, **kw)


# Factory per source key. Build steps 5+ (FR/DE/CH) add rows here.
ADAPTERS: dict[str, Callable[..., Adapter]] = {
    "uk-caselaw": UKCaseLawAdapter,
    # UK FTT — General Regulatory Chamber (information rights, environment, charity…).
    "uk-grc": lambda **kw: UKCaseLawAdapter(court="ukftt/grc", **kw),
    "uk-ftt-tax": lambda **kw: UKCaseLawAdapter(
        court="ukftt/tc", source_key="uk-ftt-tax", **kw),
    "uk-utaac": lambda **kw: UKCaseLawAdapter(
        court="ukut/aac", source_key="uk-utaac", **kw),
    "uk-iac": lambda **kw: UKCaseLawAdapter(
        court="ukut/iac", source_key="uk-iac", **kw),
    "uk-cat": CompetitionAppealTribunalAdapter,
    # Employment Tribunal — the GOV.UK register's Search + Content Store APIs.  Its
    # decision-year/case-number identity deliberately matches the BAILII metadata seed,
    # replacing existing textless UKET stubs with the official full text.
    "uk-et": UKEmploymentTribunalAdapter,
    # Every GOV.UK feed shares one stable_id namespace (``govuk/<base_path>``), because
    # the feeds genuinely overlap: 268 of the CMA's publications are also in the
    # cross-government policy corpus, and a page harvested by two feeds must be ONE
    # document. The source column still records which feed found it.
    # NB the CMA feed is the WHOLE organisation (~6,400 items), not the ~110-item
    # consumer-enforcement facet it used to be: that facet was a tenth of the register
    # and left mergers, CA98 antitrust cases and market investigations unharvested.
    "uk-cma": lambda **kw: GOVUKRegulatorAdapter(
        source="uk-cma", organisation="competition-and-markets-authority",
        court="CMA", id_prefix="govuk", **kw),
    # Kept as a distinct feed because guidance is what most readers of this corpus want
    # first, and it is worth being able to run it alone — but it writes into the shared
    # namespace, so it dedupes against the full CMA feed rather than storing a twin.
    "uk-cma-guidance": lambda **kw: GOVUKRegulatorAdapter(
        source="uk-cma-guidance",
        organisation="competition-and-markets-authority",
        court="CMA", id_prefix="govuk",
        search_filters={"filter_format": "guidance"},
        record_doc_type=DocType.GUIDANCE,
        require_recognized_legal_citation=False,
        **kw),
    "uk-ofgem": lambda **kw: GOVUKRegulatorAdapter(
        source="uk-ofgem", organisation="ofgem", court="Ofgem",
        id_prefix="govuk", **kw),
    "uk-ofwat": lambda **kw: GOVUKRegulatorAdapter(
        source="uk-ofwat", organisation="the-water-services-regulation-authority",
        court="Ofwat", id_prefix="govuk", **kw),
    # Ofgem's OWN register (ofgem.gov.uk), which is where the regulator actually
    # publishes: 24,000 decisions, consultations, licence and code modifications back
    # to 1998, none of them on GOV.UK. Rides the site's Drupal listing API, whose
    # facet/keyword/sort grammar is documented in the adapter.
    "uk-ofgem-publications": OfgemPublicationsAdapter,
    # The Office for Students — the whole publications listing, with a long report's
    # chapters followed one level deep and its PDFs/DOCX inlined.
    "uk-ofs": OfSPublicationsAdapter,
    # The Equality and Human Rights Commission — discovered from the sitemap rather
    # than the Turnstile-walled search, so no browser tier and a real lastmod cursor.
    "uk-ehrc": EHRCAdapter,
    # The whole-of-government policy corpus (~25,000 items): policy papers, impact
    # assessments, consultations and calls for evidence with their outcomes. No fixed
    # publisher — each record is attributed to the body on its own "From:" line — and
    # categorised by GOV.UK's own content-purpose subgroup and schema rather than by a
    # taxonomy invented here.
    "uk-govuk-policy": lambda **kw: GOVUKRegulatorAdapter(
        source="uk-govuk-policy", supergroup="policy_and_engagement",
        id_prefix="govuk", record_doc_type=DocType.GUIDANCE, **kw),
    "uk-fca-notices": FCANoticesAdapter,
    # The Information Commissioner, in four collections sharing one adapter. The three
    # registers ride the site's own XHR search endpoint (a JSON listing carrying a CMS
    # revision stamp — a real change signal, so an unchanged item is never re-downloaded);
    # the guidance corpus comes from the sitemap's lastmod. Each item's PDFs are the
    # substance (the HTML page is a summary) and are inlined into the record.
    "uk-ico-enforcement": ICOAdapter,
    "uk-ico-audits": lambda **kw: ICOAdapter(collection="audits", **kw),
    "uk-ico-consultations": lambda **kw: ICOAdapter(collection="consultations", **kw),
    "uk-ico-guidance": lambda **kw: ICOAdapter(collection="guidance", **kw),
    # The Ministry of Justice's current consolidated Civil Procedure Rules: one
    # record per Part / Practice Direction, with exact rule-number aliases.
    "uk-cpr": UKCivilProcedureRulesAdapter,
    "uk-cps-guidance": CPSProsecutionGuidanceAdapter,
    "uk-lawcom-reports": LawCommissionReportsAdapter,
    # The Information Rights tribunal's OWN decisions register — the fifteen years of
    # FOIA/EIR/DPA/PECR appeals that predate Find Case Law's coverage of the chamber.
    # Keyed by neutral citation where one exists, so the recent overlap with uk-grc
    # dedups onto one node instead of storing the decision twice.
    "uk-ftt-ir": InformationRightsAdapter,
    # Judicial guidance on judiciary.uk — the Crown Court Compendium, the Equal Treatment
    # Bench Book and the Chief Coroner's guidance/law sheets. Discovery hashes what each
    # landing page OFFERS, so a monthly check on an unchanged page downloads nothing.
    "uk-judiciary": JudiciaryGuidanceAdapter,
    # The Commission's digital-policy publication register (DG CONNECT), pre-filtered to
    # policy/legislation + reports; each item's downloads panel holds the real documents.
    "eu-digital-strategy": DigitalStrategyLibraryAdapter,
    "eu-consumer-guidance": EUConsumerGuidanceAdapter,
    # Netherlands — Rechtspraak Open Data, ECLI-native, citation graph included.
    "nl-rechtspraak": NLRechtspraakAdapter,
    "nl-acm-guidance": ACMGuidanceAdapter,
    "it-agcm": AGCMBulletinAdapter,
    # EU — CELLAR SPARQL + Formex; CJEU case law relative to a named instrument/case.
    "eu-cellar": EUCellarAdapter,
    # Public party/Member State/Commission submissions in CJEU cases. These are
    # published by InfoCuria only (not EUR-Lex), one PDF record per language rendition.
    "eu-curia-observations": EUCuriaObservationsAdapter,
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
    # Investigatory Powers Tribunal judgments — one HTML page each, keyed by the
    # neutral citation printed on an early line. RIPA and IPA are treated as shorthand
    # for the two Acts throughout, because in this Tribunal they always are.
    "uk-ipt": UKIPTAdapter,
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
    # Official explanatory notes and impact assessments stay separate from the
    # enacted text, but carry structured links back to the Act/SI they explain.
    "uk-legislation-materials": UKLegislationMaterialsAdapter,
    "eu-legislation": EULegislationAdapter,
    "eu-preparatory": EUPreparatoryAdapter,
    "eu-ep-resolutions": EPResolutionsAdapter,
    "eu-ep-followups": EPFollowUpsAdapter,
    "eu-ep-thinktank": EPThinkTankAdapter,
    "eu-ombudsman": EUOmbudsmanAdapter,
    "eu-edps-opinions": EDPSOpinionsAdapter,
    "eu-edps-investigations": EDPSInvestigationsAdapter,
    "eu-dgcomp-antitrust": DGCompAntitrustAdapter,
    "eu-esma-sanctions": ESMASanctionsAdapter,
    "eu-esas-boa": ESAsBoardOfAppealAdapter,
    "eu-srb-appeals": SRBAppealPanelAdapter,
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
    "au-vic": VictoriaLegislationAdapter,
    "au-sa": SouthAustraliaLegislationAdapter,
    "au-wa": WesternAustraliaLegislationAdapter,
    # Commonwealth eSafety publications under the Online Safety Act 2021: the
    # registered codes/standards/notices plus the regulator's current guidance.
    "au-esafety-osa": ESafetyOnlineSafetyAdapter,
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
    # Official live overlays on the A2AJ bulk seed. Their RSS feeds include
    # translations/corrections as well as new decisions; neutral-citation identity
    # enriches the same nodes already held from bulk.
    "ca-scc-live": lambda **kw: CanadianLexumAdapter(court="scc", **kw),
    "ca-tcc-live": lambda **kw: CanadianLexumAdapter(court="tcc", **kw),
    "ca-fc-live": lambda **kw: CanadianLexumAdapter(court="fc", **kw),
    "ca-fca-live": lambda **kw: CanadianLexumAdapter(court="fca", **kw),
    "ca-sst-live": lambda **kw: CanadianLexumAdapter(court="sst", **kw),
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
    # Ireland (Supreme/Court of Appeal/High Court) — ww2.courts.ie register. Landing page
    # for keep-current, year-search facet for backfill (to 2001); each row's /view/ detail
    # page carries the authoritative Neutral Citation (never the PDF filename). Keyed by the
    # neutral-cite slug ("[2025] IESC 49" → iesc/2025/49); multi-judgment cases store one
    # opinion per PDF, grouped by case_citation, the lead owning the bare slug.
    "ie-caselaw": IrishCaseLawAdapter,
    "ie-dpc": IrishDPCAdapter,
    "ie-dpc-guidance": IrishDPCGuidanceAdapter,
    "nl-ap": APDocumentsAdapter,
    "eu-berec": BERECAdapter,
    # EUIPO Observatory publications — the site's own public Algolia index faceted
    # to observatory-publications, then the study PDFs linked one level down.
    "eu-euipo": EUIPOPublicationsAdapter,
    "dma-consultations": DMAConsultationsAdapter,
    "dma-annual-reports": DMAAnnualReportsAdapter,
    # national DPA guidance libraries (§ eu_dpa_guidance)
    "fr-cnil-guidance": CNILGuidanceAdapter,
    "es-aepd-guias": AEPDGuidanceAdapter,
    "dk-datatilsynet": DatatilsynetGuidanceAdapter,
    "de-dsk": DSKGuidanceAdapter,
    "be-gba": GBAGuidanceAdapter,
    # the Dispute Chamber's own rulings — enforcement, not guidance
    "be-gba-decisions": GBADecisionsAdapter,
    # UK Parliament — committee output and the written Q&A record
    "uk-parl-committees": UKCommitteePublicationsAdapter,
    "uk-parl-written-questions": UKWrittenQuestionsAdapter,
    # …and the two Libraries' research, whose RSS carries the whole briefing
    "uk-commons-library": lambda **kw: ParliamentLibraryAdapter(house="commons", **kw),
    "uk-lords-library": lambda **kw: ParliamentLibraryAdapter(house="lords", **kw),
    # The Scottish Parliament's research service
    "scot-spice": SPICeBriefingsAdapter,
    # UK investigatory-powers oversight: the Commissioner's office and the ISC
    "uk-ipco": IPCOPublicationsAdapter,
    "uk-isc": ISCReportsAdapter,
    "it-garante": GaranteGuidanceAdapter,
    "ie-tax-appeals": IrishTaxAppealsAdapter,
    "ie-revenue-tdm": IrishRevenueTDMAdapter,
    "ie-ccpc-mergers": IrishCCPCMergerAdapter,
    # The Oireachtas Library's catalogue of everything laid before the Houses —
    # committee reports, statutory annual reports and accounts, post-enactment reviews.
    "ie-oireachtas": OireachtasLaidAdapter,
    # …and the evidence those committees heard, which is never laid and so is
    # in no catalogue: opening statements, submissions, briefings.
    "ie-oireachtas-committees": OireachtasCommitteeEvidenceAdapter,
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
    "fr-judilibre-ca": lambda **kw: FrJudilibreAdapter(jurisdiction="ca", **kw),
    "fr-judilibre-tj": lambda **kw: FrJudilibreAdapter(jurisdiction="tj", **kw),
    "fr-conseil-etat": FrConseilEtatAdapter,
    # Germany — NeuRIS / rechtsinformationen.bund.de (beta), ELI + ECLI native. One
    # adapter, two modes: federal case law (default) and federal legislation (LDML.de).
    "de-neuris": DeNeurisAdapter,
    "de-neuris-legislation": lambda **kw: DeNeurisAdapter(mode="legislation", **kw),
    # Germany bulk seeds (no key): the legacy juris-DTD portals. de-gii = federal
    # statutes (gesetze-im-internet, local clone or gii-toc.xml); de-rii = federal
    # case law (rechtsprechung-im-internet, rii-toc.xml). NeuRIS is the live increment.
    "de-gii": DeGiiAdapter,
    "de-bt-drucksachen": BundestagDrucksachenAdapter,
    "de-bt-wd": BundestagWDAdapter,
    "de-rii": DeRiiAdapter,
    # Germany — Open Legal Data: the LÄNDER case law the federal portals never publish
    # (424k decisions, 918 courts). Bulk-seeded from the parquet dump (`path`), then kept
    # current off the same project's REST API. ECLI-keyed, so it dedups against de-rii.
    "de-openlegaldata": DeOpenLegalDataAdapter,
    # ---- Austria: one RIS Applikation per key ------------------------------
    # The API partitions its 985,000 documents by deciding body, and the bodies are not
    # comparable — OGH civil judgments, the constitutional court, and the data-protection
    # authority's decisions are three different corpora behind one endpoint. Each is its
    # own source so a user can harvest the DPA without pulling 357,000 VwGH documents,
    # and so the catalogue can say which is case law and which is administrative.
    "at-justiz": lambda **kw: AustrianRISAdapter(application="Justiz",
                                                 source_key="at-justiz", **kw),
    "at-vwgh": lambda **kw: AustrianRISAdapter(application="Vwgh",
                                               source_key="at-vwgh", **kw),
    "at-vfgh": lambda **kw: AustrianRISAdapter(application="Vfgh",
                                               source_key="at-vfgh", **kw),
    "at-bvwg": lambda **kw: AustrianRISAdapter(application="Bvwg",
                                               source_key="at-bvwg", **kw),
    "at-lvwg": lambda **kw: AustrianRISAdapter(application="Lvwg",
                                               source_key="at-lvwg", **kw),
    "at-dsb": lambda **kw: AustrianRISAdapter(application="Dsk", source_key="at-dsb", **kw),
    "at-gbk": lambda **kw: AustrianRISAdapter(application="Gbk", source_key="at-gbk", **kw),
    "at-verg": lambda **kw: AustrianRISAdapter(application="Verg",
                                               source_key="at-verg", **kw),
    # …and one generic key for the remaining bodies (Dok, Pvak, Uvs, AsylGH, Ubas, Umse,
    # Bks), which are historical or small enough not to warrant a key each.
    "at-ris": lambda **kw: AustrianRISAdapter(source_key="at-ris", **kw),
    "sk-ress": SlovakRESSAdapter,
    # ---- Finland: one Finlex series per key --------------------------------
    **{key: (lambda series: lambda **kw: FinlexAdapter(series=series, **kw))(key)
       for key in FINLEX_SERIES},
    "se-domstol": SwedishCaseLawAdapter,
    "se-domstol-bulk": SwedishCaseLawBulkAdapter,
    "ee-lahend": EstonianLahendAdapter,
    # France bulk seed (no auth): the DILA OPENDATA archives read from local disk. One
    # adapter across funds; the PISTE/Conseil-d'État live adapters handle increments.
    "fr-dila": FrDilaAdapter,  # CASS (Cour de cassation) by default
    "fr-dila-legi": lambda **kw: FrDilaAdapter(fond="LEGI", **kw),
    "fr-dila-jade": lambda **kw: FrDilaAdapter(fond="JADE", **kw),
    "fr-dila-constit": lambda **kw: FrDilaAdapter(fond="CONSTIT", **kw),
    "fr-dila-cnil": lambda **kw: FrDilaAdapter(fond="CNIL", **kw),
    "fr-senat-reports": SenatInformationReportsAdapter,
    "fr-senat-lc": SenatComparativeLawAdapter,
    "fr-an-reports": AssembleeInformationReportsAdapter,
    "nl-tk-reports": TweedeKamerReportsAdapter,
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
    kind: str           # caselaw | administrative | legislation | guidance | scrape
    jurisdiction: str   # GB | EU | NL
    keyword_search: bool  # True: keywords are searched in the source API (precise);
    #                       False: keywords post-filter what's harvested (any-term match)
    description: str
    options: tuple[SourceOption, ...] = field(default_factory=tuple)
    # The identifier forms this source can fetch a *single item* by (targeted harvest)
    # — what a new adapter declares so the resolver/UI know how to route a citation.
    identifiers: tuple[str, ...] = field(default_factory=tuple)


# Public source-catalogue schema. Keep these labels here rather than re-inventing them
# in the REST API, MCP, or individual screens. Adapter authors add exactly one
# ``SourceInfo`` row; ``source_catalog()`` supplies grouping/sort/capability fields.
JURISDICTION_LABELS: dict[str, str] = {
    "GB": "United Kingdom", "EU": "European Union", "CoE": "Council of Europe",
    "IE": "Ireland", "FR": "France", "DE": "Germany", "NL": "Netherlands",
    "IT": "Italy", "AU": "Australia", "CA": "Canada", "NZ": "New Zealand",
    "ES": "Spain", "DK": "Denmark", "BE": "Belgium",
    "SG": "Singapore", "HK": "Hong Kong", "IN": "India", "US": "United States",
    "AT": "Austria", "SK": "Slovakia", "FI": "Finland", "SE": "Sweden",
    "EE": "Estonia",
    "": "Other",
}
KIND_LABELS: dict[str, str] = {
    "legislation": "Legislation",
    "caselaw": "Case law",
    "administrative": "Administrative decisions",
    "guidance": "Guidance and regulatory material",
    "preparatory": "Preparatory and policy material",
    "scrape": "Other harvested material",
}


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
    "uk-ftt-tax": SourceInfo(
        "uk-ftt-tax", "UK FTT — Tax Chamber", "caselaw", "GB", True,
        "Official Find Case Law Tax Chamber feed and full Akoma Ntoso judgments.",
        (SourceOption("query", "Keyword query", "free text"),), ("UKFTT (TC) citation",),
    ),
    "uk-utaac": SourceInfo(
        "uk-utaac", "UK Upper Tribunal — Administrative Appeals", "caselaw", "GB", True,
        "Official Find Case Law Administrative Appeals Chamber feed and full judgments.",
        (SourceOption("query", "Keyword query", "free text"),), ("UKUT (AAC) citation",),
    ),
    "uk-iac": SourceInfo(
        "uk-iac", "UK Upper Tribunal — Immigration and Asylum", "caselaw", "GB", True,
        "Official Find Case Law Immigration and Asylum Chamber feed and full judgments.",
        (SourceOption("query", "Keyword query", "free text"),), ("UKUT (IAC) citation",),
    ),
    "uk-cat": SourceInfo(
        "uk-cat", "Competition Appeal Tribunal judgments", "caselaw", "GB", False,
        "The Tribunal's official sitemap, judgment pages and full decision PDFs.",
        (), ("CAT neutral citation",),
    ),
    "uk-et": SourceInfo(
        "uk-et", "UK Employment Tribunal (GOV.UK)", "caselaw", "GB", True,
        "Employment Tribunal decisions from the official GOV.UK register, including "
        "full PDF-derived text. Existing textless UKET metadata records are enriched "
        "in place by decision year and case number.",
        (SourceOption("query", "Keyword query", "free text, searched in GOV.UK"),),
        ("UKET citation or tribunal case number",),
    ),
    "uk-cpr": SourceInfo(
        "uk-cpr", "UK Civil Procedure Rules (current consolidation)", "legislation", "GB",
        False,
        "The Ministry of Justice's current consolidated Civil Procedure Rules: every "
        "active Part and Practice Direction as structured HTML. The official index is "
        "walked on each maintenance run and substantive content hashes detect amendments "
        "or replacements. Rule and PD citations resolve to the exact current Part or "
        "Direction; each rule Part remains linked to SI 1998/3132 on legislation.gov.uk.",
        (SourceOption("ids", "Parts, rules, or directions",
                      "uk/cpr/part/3, uk/cpr/rule/3.9, uk/cpr/pd/3d"),),
        ("CPR rule (CPR 3.9)", "CPR Part", "Practice Direction (PD 3D)"),
    ),
    "uk-cps-guidance": SourceInfo(
        "uk-cps-guidance", "CPS prosecution guidance library",
        "scrape", "GB", False,
        "The Crown Prosecution Service's official A–Z prosecution-guidance library. "
        "Current HTML guidance and library PDFs are stored with canonical titles, "
        "revision dates and heading anchors; citation-free outliers are retained as "
        "processed records but excluded from search.",
        (), ("CPS prosecution-guidance URL", "guidance title"),
    ),
    "eu-digital-strategy": SourceInfo(
        "eu-digital-strategy", "EU digital-strategy library (DG CONNECT)", "guidance", "EU", False,
        "The Commission's digital-policy publication register, filtered to Policy and "
        "legislation + Report/Study: AI Act guidelines and Commission opinions, DSA/DMA "
        "material, codes of practice, connectivity and cybersecurity reports. Each item's "
        "downloads panel is followed to the document itself (the newsroom redirection "
        "endpoint serves the PDF directly); where a document is published in several "
        "languages only the English version is taken, and annexes published alongside are "
        "recorded on the record. Newest-first, so an incremental run stops at the cursor.",
        (), ("library item URL", "document title"),
    ),
    "eu-consumer-guidance": SourceInfo(
        "eu-consumer-guidance", "European Commission consumer guidance and CPC positions",
        "guidance", "EU", False,
        "The English Commission consumer-policy subtree from its official sitemap: CPC "
        "common positions and understandings, coordinated actions, sweeps, commitments "
        "and DG JUST consumer guidance. First-party document UUIDs are stored separately "
        "from their context pages; a title naming exactly one directive supplies a safe "
        "default for otherwise orphaned Article references.",
        (), ("Commission document UUID", "consumer-topic page URL"),
    ),
    "uk-judiciary": SourceInfo(
        "uk-judiciary", "UK judicial guidance (judiciary.uk)", "guidance", "GB", False,
        "The Judicial College's bench books and the Chief Coroner's guidance: the Crown "
        "Court Compendium (Parts I and II), the Equal Treatment Bench Book, and the ~30 "
        "numbered Chief Coroner guidance notes, five law sheets and the Treasure guide. "
        "Each is cited by its own reference (\"Guidance No 16A\", \"Law Sheet No 1\"), "
        "minted as an alias. Revised a couple of times a year, so discovery fingerprints "
        "the documents each landing page offers and a monthly check on an unchanged page "
        "fetches nothing at all.",
        (SourceOption("collection", "One collection only",
                      "crown-court-compendium | equal-treatment-bench-book | chief-coroner"),),
        ("Chief Coroner guidance number", "document title"),
    ),
    "uk-ftt-ir": SourceInfo(
        "uk-ftt-ir", "UK FTT — Information Rights decisions register", "caselaw", "GB", False,
        "The Information Rights tribunal's own database (informationrights.decisions."
        "tribunals.gov.uk): every FOIA, EIR, DPA, PECR and national-security appeal "
        "decided by the Information Tribunal and the First-tier Tribunal's General "
        "Regulatory Chamber, as published PDFs with their appeal number, parties, "
        "outcome and panel. A CLOSED archive: it took its last decision in August 2023, "
        "when the chamber moved to Find Case Law — so it is a one-off backfill, not a "
        "watch. Covers the years before Find Case Law carried the chamber; "
        "a decision printing a neutral citation is keyed by it, so it dedups against the "
        "same case held from uk-grc. The Commissioner's decision-notice reference is kept "
        "as the join to the ICO side.",
        (), ("appeal number (e.g. EA/2022/0273)", "neutral citation"),
    ),
    "uk-lawcom-reports": SourceInfo(
        "uk-lawcom-reports", "Law Commission completed-project documents",
        "scrape", "GB", False,
        "All PDFs nested in the Documents sections of the Law Commission's completed "
        "project pages, including National Archives-preserved projects. Paragraph-initial "
        "colon definitions with labels up to 15 characters create document-local Act/SI "
        "shorthands and provision pinpoints. Repeated undefined 'the YYYY Act' references "
        "use a conservative unique-year fallback; the Freedom of Information Act is "
        "excluded as a 2000 fallback candidate.",
        (), ("Law Commission number", "project/document title"),
    ),
    "uk-cma": SourceInfo(
        "uk-cma", "CMA publications (all)", "guidance", "GB", False,
        "Everything the Competition and Markets Authority publishes on GOV.UK — the "
        "whole organisation feed (~6,400 items), through the official Search and "
        "Content Store APIs rather than the results page: merger and CA98 antitrust "
        "cases, market studies and investigations, guidance, decisions, consultations "
        "and reports. A publication is a container, so each item's child HTML "
        "publications and attached PDFs are followed and inlined; where GOV.UK offers "
        "the same document as accessible HTML and as a PDF, the HTML is taken and its "
        "PDF twins skipped. Documents share the GOV.UK-wide id namespace, so an item "
        "also carried by another GOV.UK feed is one document, not two.",
        (), ("GOV.UK path", "CMA case reference (e.g. CMA207)"),
    ),
    "uk-govuk-policy": SourceInfo(
        "uk-govuk-policy", "GOV.UK policy and engagement (all departments)",
        "guidance", "GB", False,
        "The whole-of-government policy corpus from GOV.UK's content-purpose "
        "supergroup ``policy_and_engagement`` (~25,000 items): policy papers, impact "
        "assessments, open and closed consultations with their outcomes, and calls for "
        "evidence. Discovery is the official Search API newest-first, so an "
        "incremental run stops at the cursor and a backfill pages the whole set. "
        "Each record is attributed to the body on its own \"From:\" line rather than "
        "to a single fixed publisher, and is tagged with GOV.UK's own categorisation — "
        "the content-purpose subgroup (policy / consultations / calls for evidence), "
        "the document schema (policy_paper, impact_assessment, consultation_outcome…) "
        "and the publishing organisation's slug. The legal-relevance gate applies: "
        "everything is held and deduped, but citation-free operational material stays "
        "out of search.",
        (SourceOption("organisation", "One department only",
                      "e.g. home-office, hm-treasury"),),
        ("GOV.UK path", "publication title"),
    ),
    "uk-cma-guidance": SourceInfo(
        "uk-cma-guidance", "CMA guidance and regulation", "guidance", "GB", False,
        "All official CMA guidance publications from GOV.UK, including the CMA200-series "
        "DMCCA guidance, unfair-contract-terms guidance and sector compliance guides. "
        "Accessible HTML children are preferred and supplementary PDFs are retained.",
    ),
    "uk-ofgem": SourceInfo(
        "uk-ofgem", "Ofgem regulatory publications", "guidance", "GB", False,
        "Official Ofgem GOV.UK publications and attached PDFs, relevance-gated by "
        "recognised case or legislation citations.",
    ),
    "uk-ofwat": SourceInfo(
        "uk-ofwat", "Ofwat regulatory publications", "guidance", "GB", False,
        "Official Water Services Regulation Authority GOV.UK publications and attached "
        "PDFs, relevance-gated by recognised case or legislation citations.",
    ),
    "uk-ofgem-publications": SourceInfo(
        "uk-ofgem-publications", "Ofgem publications (ofgem.gov.uk)", "guidance", "GB",
        True,
        "Ofgem's own register — ~24,000 decisions, consultations, guidance, licence and "
        "code modifications and enforcement cases back to 1998, none of which are on "
        "GOV.UK. Keywords are searched at the source; a facet may be pinned by term id "
        "(the vocabulary is in the listing response's own facets). Each publication's "
        "PDFs and Word documents are inlined; spreadsheets are recorded unread. "
        "Relevance-gated, because the register also carries blogs and press notices.",
        (SourceOption("query", "Keyword query", "free text, searched in the API"),
         SourceOption("facet", "Facet to filter on",
                      "facet_case_publication_type | topic | facet_scheme_name | "
                      "facet_industry_sector | facet_publication_date"),
         SourceOption("facet_value", "Facet term id", "e.g. 1602 for Decision"),
         SourceOption("include_documents", "Download attached files", "true/false"),
         SourceOption("max_documents", "Attachments per publication", "default 20")),
        ("ofgem.gov.uk publication path",),
    ),
    "uk-ofs": SourceInfo(
        "uk-ofs", "Office for Students publications", "guidance", "GB", False,
        "The English higher education regulator's whole publications listing (~670 "
        "items): registration conditions, quality and access guidance, consultations "
        "and their outcomes, and independent research. A multi-chapter report's "
        "sub-pages are followed one level deep and its PDFs/DOCX inlined; the OfS's own "
        "'OfS 2026.38' reference is kept as an alias, and every record links to the "
        "Higher Education and Research Act 2017.",
        (SourceOption("include_child_pages", "Follow report chapters", "true/false"),
         SourceOption("include_documents", "Download attached files", "true/false"),
         SourceOption("max_documents", "Attachments per publication", "default 20")),
        ("officeforstudents.org.uk publication path", "OfS reference (OfS 2026.38)"),
    ),
    "uk-ehrc": SourceInfo(
        "uk-ehrc", "Equality and Human Rights Commission", "guidance", "GB", False,
        "The EHRC's published site (~1,970 pages): statutory codes of practice under "
        "the Equality Act 2010, technical guidance on the public sector equality duty, "
        "research and its advice to Parliament. Discovered from the sitemap — the "
        "on-site search is behind a Cloudflare challenge and its pager is unreliable — "
        "so an incremental run uses each page's real lastmod. Attached PDFs and Word "
        "documents are inlined and bare provision references resolve to the Equality "
        "Act 2010. Relevance-gated: the sitemap also holds careers and corporate pages.",
        (SourceOption("section", "One part of the site only",
                      "guidance | our-work | human-rights | news | about-us"),
         SourceOption("include_documents", "Download attached files", "true/false"),
         SourceOption("max_documents", "Attachments per page", "default 20")),
        ("equalityhumanrights.com page path",),
    ),
    "nl-rechtspraak": SourceInfo(
        "nl-rechtspraak", "NL Rechtspraak (Open Data)", "caselaw", "NL", False,
        "Dutch case law, ECLI-native, with a built-in citation graph. The API indexes "
        "by date/court, so keywords filter the harvested results (Dutch terms work).",
        (SourceOption("path", "Bulk archive path", "OpenDataUitspraken.zip or extracted folder"),
         SourceOption("lido_links", "Import LiDO graph", "true — structured outgoing links")),
        ("ECLI:NL:…",),
    ),
    "nl-acm-guidance": SourceInfo(
        "nl-acm-guidance", "ACM guidance (Netherlands — Leidraden)", "guidance", "NL", False,
        "The complete official ACM guidance series for businesses, including online "
        "consumer protection, price display and sustainability claims. Detail-page HTML "
        "and official PDF attachments are combined; the small catalogue is fully "
        "rechecked so revisions retaining their original publication date are caught.",
    ),
    "nl-ap": SourceInfo(
        "nl-ap", "Dutch DPA (Autoriteit Persoonsgegevens) documents",
        "guidance", "NL", False,
        "The AP's whole publication register in one feed: fines and other sanctions, "
        "blacklist licence decisions, Woo decisions, its legislative-advice opinions "
        "(wetgevingstoetsen), policy rules, normative interpretations, guidance and "
        "annual reports. The card names no document type, so the register's own "
        "document_type facet is swept in parallel and each item is recorded with the "
        "NARROWEST type that claims it plus the full path (Besluit \u2192 Sanctie \u2192 "
        "Boete). Most items are two-step \u2014 a summary page over the operative PDF \u2014 "
        "and both are kept. Backfill pages the view; keep-current polls the daily "
        "publication RSS feed.",
        (), ("autoriteitpersoonsgegevens.nl document URL",),
    ),
    "fr-cnil-guidance": SourceInfo(
        "fr-cnil-guidance", "CNIL guidance (France — médiathèque)",
        "guidance", "FR", False,
        "The CNIL's whole published médiathèque: guides, lignes directrices, "
        "recommandations, fiches pratiques and référentiels, keyed on the CNIL's own "
        "collection type. Each row is rendered twice (grid + list) and is deduplicated "
        "to one document; the upload folder supplies the publication month the "
        "catalogue itself does not print.",
        (), ("cnil.fr publication URL",),
    ),
    "es-aepd-guias": SourceInfo(
        "es-aepd-guias", "AEPD guías (Spain)", "guidance", "ES", False,
        "The Agencia Española de Protección de Datos' guías y herramientas: the "
        "official PDF guides with their publication dates, covering the RGPD and the "
        "Spanish LOPDGDD.",
        (), ("aepd.es guía URL",),
    ),
    "dk-datatilsynet": SourceInfo(
        "dk-datatilsynet", "Datatilsynet guidance (Denmark)", "guidance", "DK", False,
        "Datatilsynet's vejledninger. The library is one flat hub page rather than a "
        "listing view, so it is read whole and each PDF keeps the topic heading it sits "
        "under. Includes the Danish preparatory materials the authority itself points "
        "to for databeskyttelsesloven.",
        (), ("datatilsynet.dk PDF URL",),
    ),
    "de-dsk": SourceInfo(
        "de-dsk", "Datenschutzkonferenz (Germany — joint DPA positions)",
        "guidance", "DE", False,
        "The German supervisory authorities' joint output: Orientierungshilfen, "
        "Kurzpapiere and Beschlüsse. This is where German data-protection practice is "
        "actually settled — the federal BfDI publishes little standalone interpretation "
        "and the Länder authorities mostly link here. An Orientierungshilfe and its "
        "Anhang are kept as one document.",
        (), ("datenschutzkonferenz-online.de PDF URL",),
    ),
    "be-gba": SourceInfo(
        "be-gba", "Belgian DPA (APD/GBA) publications", "guidance", "BE", False,
        "The Autorité de protection des données' recommendations, advice "
        "(avis/adviezen) and documentation from its publication register. The register "
        "prints a year rather than a date, which is recorded as such rather than "
        "presented as a precise one.",
        (), ("autoriteprotectiondonnees.be publication URL",),
    ),
    "be-gba-decisions": SourceInfo(
        "be-gba-decisions",
        "Belgian DPA Dispute Chamber decisions (Geschillenkamer)",
        "administrative", "BE", False,
        "The Geschillenkamer's own rulings: substantive decisions (Beslissing ten "
        "gronde) and settlement decisions, with the reprimands, orders and "
        "administrative fines they impose. Kept apart from be-gba, which is the "
        "authority's guidance register — a Dispute Chamber ruling is a regulator "
        "determination, and burying the enforcement record inside explanatory material "
        "makes it unfindable. Keyed on the decision number as Belgian practitioners "
        "cite it (102/2026), not on the PDF filename, which the register has spelt more "
        "than one way for the same ruling.",
        (), ("decision number", "102/2026"),
    ),
    "uk-parl-committees": SourceInfo(
        "uk-parl-committees", "UK parliamentary committee publications",
        "preparatory", "GB", False,
        "Select-committee reports, government responses, special reports, correspondence "
        "and scrutiny evidence from the committees API. Keyed on the paper number a "
        "report is actually cited by (HC 69 (2026-27), HL Paper 45 (2026-27)) rather "
        "than the API's internal id, with the session as part of the identity because "
        "paper numbers repeat every session. Attendance and gender-balance statistics "
        "are excluded by default: they are tables of numbers that cite nothing.",
        (SourceOption("publication_types",
                      "Publication type ids to sweep (comma-separated; default is the "
                      "argumentative types)", "1,2,3,8,12,16"),),
        ("paper number", "HC 69"),
    ),
    "uk-parl-written-questions": SourceInfo(
        "uk-parl-written-questions", "UK parliamentary written questions and answers",
        "preparatory", "GB", False,
        "A written question and the Government's answer stored as one document, keyed on "
        "the UIN. Incremental runs ask what has been ANSWERED since the cursor, so an "
        "answer to a question tabled months earlier arrives without anything being "
        "polled in between. A holding answer is not treated as an answer.",
        (SourceOption("include_unanswered",
                      "Also hold questions with no answer yet (provisional)", "false"),
         SourceOption("since_floor",
                      "Earliest answer date a backfill walks from — the API returns "
                      "nothing at all for a query with no date bound", "2014-01-01")),
        ("UIN", "HL2522"),
    ),
    "uk-commons-library": SourceInfo(
        "uk-commons-library", "House of Commons Library research briefings",
        "preparatory", "GB", False,
        "Every briefing the Commons Library has published, back to a 1993 research "
        "paper. The Library's RSS carries the COMPLETE briefing in content:encoded — "
        "same headings, same text as the web page — so one request yields ten finished "
        "documents, and ?paged=N walks the feed to the beginning of the archive "
        "(~1,200 pages). Everything on parliament.uk sits behind a Cloudflare "
        "challenge, so the feed is read through the browser tier as bytes rather than "
        "as rendered HTML, which would parse RSS as HTML and mangle every item. A "
        "briefing published before the Library typeset in HTML arrives as an abstract "
        "only; those fall back to the researchbriefings PDF, and the oldest of those "
        "are scans and are OCR'd. Published under the Open Parliament Licence.",
        (SourceOption("start_page", "First feed page to walk", "1"),
         SourceOption("max_feed_pages", "How many feed pages a backfill may walk",
                      "2000 (the Commons feed ends at 1,200)"),
         SourceOption("slugs", "Fetch exactly these briefings",
                      "cbp-10974,sn02811,rp94-22"),
         SourceOption("include_pdf",
                      "Fall back to the PDF when the feed carries only an abstract",
                      "true"),
         SourceOption("ocr", "OCR a PDF with no text layer", "true")),
        ("briefing number", "CBP-10974", "SN02811", "RP94-22"),
    ),
    "uk-lords-library": SourceInfo(
        "uk-lords-library", "House of Lords Library research briefings",
        "preparatory", "GB", False,
        "The Lords Library's in-focus briefings and debate packs, read the same way as "
        "the Commons Library's: the whole briefing is in the RSS, and ?paged=N walks "
        "back to 1998 over ~281 pages. The Lords feed also carries the author. Older "
        "LLN and LIF notes are PDF-only and fall back to the researchbriefings file "
        "host. Published under the Open Parliament Licence.",
        (SourceOption("start_page", "First feed page to walk", "1"),
         SourceOption("max_feed_pages", "How many feed pages a backfill may walk",
                      "2000 (the Lords feed ends at 281)"),
         SourceOption("slugs", "Fetch exactly these briefings", "lln-2019-0042"),
         SourceOption("include_pdf",
                      "Fall back to the PDF when the feed carries only an abstract",
                      "true"),
         SourceOption("ocr", "OCR a PDF with no text layer", "true")),
        ("briefing number", "LLN-2019-0042", "LIF-2024-0001"),
    ),
    "scot-spice": SourceInfo(
        "scot-spice", "SPICe briefings (Scottish Parliament research)",
        "preparatory", "GB", False,
        "The Scottish Parliament Information Centre's research briefings — 750 of them, "
        "back to April 2017, which is where the Parliament's own index starts. The "
        "search DEFAULTS to a few-week window and silently ignores dtDateFrom/dtDateTo, "
        "so the adapter reads the date presets off the form on every run and picks the "
        "widest; the preset's value changes daily because its label ends at today, and "
        "a hard-coded one stops working overnight. Text comes from each briefing's own "
        "PDF ({page}/pdf), which is the complete document — the HTML view splits it "
        "across several paginated pages. Documents are held with jurisdiction gb-sct.",
        (SourceOption("date_select",
                      "Override the dateSelect preset (blank = widest on the form)",
                      "{guid}|Wednesday, May 12, 1999|Friday, August 7, 2026"),
         SourceOption("subject", "Subject facet filter", "Justice, Health, Transport…"),
         SourceOption("slugs", "Fetch exactly these briefings", "sb-2650,sb-2649"),
         SourceOption("page_size", "Results per listing page (max 50)", "50"),
         SourceOption("ocr", "OCR a PDF with no text layer", "true")),
        ("briefing slug", "sb-2650", "SB 26-50"),
    ),
    "uk-ipco": SourceInfo(
        "uk-ipco", "IPCO publications (Investigatory Powers Commissioner)",
        "guidance", "GB", False,
        "Annual reports, inspection reports, consultations and correspondence from the "
        "Investigatory Powers Commissioner's Office, including the IOCCO and OSC "
        "material IPCO inherited. Enumerated from post-sitemap.xml, whose lastmod is "
        "the only place on the site that records a REVISION — a reissued annual report "
        "keeps its URL and its published date, so a listing crawl cannot see it. The "
        "sitemap is one request for the whole archive (222 URLs), so an incremental "
        "run is that request filtered on lastmod. The text is in the attached PDFs; "
        "scanned legacy reports are OCR'd. No default instrument is declared: the "
        "register spans the IPA 2016, RIPA 2000 and Part III of the Police Act 1997.",
        (SourceOption("include_news", "Also hold /news/ posts", "true"),
         SourceOption("sections",
                      "Only these sitemap sections",
                      "annual-report,iocco-publication,osc-publication,correspondence"),
         SourceOption("ocr", "OCR a PDF with no text layer", "true")),
        ("publication slug", "annual-report-2024"),
    ),
    "uk-isc": SourceInfo(
        "uk-isc", "ISC reports (Intelligence and Security Committee)",
        "guidance", "GB", False,
        "Everything the ISC has published — 215 PDFs from the 1995 Annual Report "
        "onwards — all of which live on one /reports/ page. That page must be read as "
        "MARKUP, not through a browser: rendered, the collapsed per-Parliament "
        "accordions are dropped and only four PDFs survive. The publications post type "
        "is not addressable (every sitemap entry 302s to a 404), so the PDF is the "
        "document; each is titled by its own link text, which is what separates a "
        "report from its press notice. Pre-2000 Command Papers are scans with no text "
        "layer and are OCR'd — slow, and the right trade at this size.",
        (SourceOption("include_press", "Also hold press notices as documents", "true"),
         SourceOption("ocr", "OCR a PDF with no text layer", "true"),
         SourceOption("max_ocr_pages", "Page ceiling for one OCR pass", "200")),
        ("report PDF slug", "1995-isc-ar"),
    ),
    "it-garante": SourceInfo(
        "it-garante", "Garante per la protezione dei dati personali (Italy)",
        "guidance", "IT", False,
        "The Garante's linee guida and provvedimenti, keyed on the doc web number — "
        "the authority's own permanent identifier, the one Italian practitioners cite "
        "('doc. web n. 10241943'). The adoption date is parsed from the measure's "
        "title, which is where the Garante puts it.",
        (), ("doc web number", "10241943"),
    ),
    "eu-berec": SourceInfo(
        "eu-berec", "BEREC document register (EU electronic communications)",
        "guidance", "EU", False,
        "BEREC's whole /all-documents tree, category by category: opinions, guidelines, "
        "common positions, recommendations, reports and decisions, plus the BEREC "
        "Office's administrative papers. Each document keeps its BoR number (BoR (26) "
        "88_1) as an alias so the number as cited resolves. The site's rss.xml is a NEWS "
        "feed carrying no documents, so keep-current re-reads the newest page of each "
        "category and stops at the cursor.",
        (), ("BoR document number", "BoR (26) 88_1"),
    ),
    "eu-euipo": SourceInfo(
        "eu-euipo", "EUIPO Observatory publications (EU intellectual property)",
        "guidance", "EU", False,
        "The European Observatory on Infringements of Intellectual Property Rights: the "
        "IP Perception surveys, the IPR Infringement and Online Advertising series, the "
        "sector-level economic-cost studies and the legal/case-law comparisons — the "
        "evidence base the EU institutions cite when they legislate on counterfeiting "
        "and enforcement. Discovery is the site's own public Algolia index faceted to "
        "observatory-publications (nine pages, no crawl and no browser); the study "
        "itself is the PDF linked from each landing page, and every linked PDF is "
        "followed, extracted and inlined — so a report's executive summary, its press "
        "release and its per-Member-State country notes are searchable as one "
        "publication rather than lost behind a filename.",
        (SourceOption("max_pdfs", "Linked PDFs to follow per publication", "60"),),
        ("EUIPO publication slug", "euipn-trends-report-2025"),
    ),
    "dma-consultations": SourceInfo(
        "dma-consultations", "DMA public consultations (European Commission)",
        "guidance", "EU", False,
        "The Digital Markets Act consultation surface: draft guidelines, compliance and "
        "reporting templates, and the published submissions. Backfill walks the index "
        "(which keeps the closed consultations); keep-current reads the consultations "
        "RSS feed. Documents are keyed on the Commission document UUID, so anything "
        "also published on another Commission site is one document, not two.",
        (), ("Commission document UUID",),
    ),
    "dma-annual-reports": SourceInfo(
        "dma-annual-reports", "DMA annual reports (Article 35 DMA)",
        "guidance", "EU", False,
        "The Commission's Article 35 DMA annual reports to the Parliament and Council on "
        "the implementation of the Regulation. One page, one report a year.",
        (), ("Commission document UUID",),
    ),
    "it-agcm": SourceInfo(
        "it-agcm", "Italy AGCM weekly decision bulletins", "guidance", "IT", False,
        "The official sequential Bollettino settimanale: competition and consumer "
        "protection measures as published PDFs. Italian Codice del consumo article "
        "references are extracted explicitly; orphan article carry-forward is disabled "
        "across the mixed-decision bulletin to prevent cross-case false links.",
        (), ("bulletin number and year", "AGCM PS decision number"),
    ),
    "eu-cellar": SourceInfo(
        "eu-cellar", "EU CJEU case law (CELLAR / SPARQL)", "caselaw", "EU", False,
        "CJEU judgments + AG opinions discovered relative to a named instrument or case. "
        "Set the instrument to follow (required); keywords post-filter the results.",
        (SourceOption("legislation_celex", "Legislation CELEX to follow", "e.g. 32004R0139"),
         SourceOption("cited_by_celex", "Find cases citing this case", "e.g. 62018CJ0311")),
        ("CJEU case CELEX (62018CJ0511)", "ECLI:EU:C:…"),
    ),
    "eu-curia-observations": SourceInfo(
        "eu-curia-observations", "CJEU published written observations (InfoCuria)",
        "preparatory", "EU", False,
        "Statements of case and written observations which the Court has made public on "
        "InfoCuria but which are not carried by EUR-Lex. The public CURIA search service "
        "is filtered to OBSRP_PUB documents and each stable logical document is fetched "
        "as an official PDF in every language CURIA offers. Discovery is a weekly full "
        "walk so a filing published late is not missed; the default backfill is limited "
        "to five years. Each filing is linked to the held CJEU judgment through its "
        "decision CELEX alias, and its PDF text runs through the normal multilingual "
        "citation grammars.",
        (SourceOption("years", "Maximum filing age in years", "5"),),
        ("InfoCuria logical document id", "CJEU case number"),
    ),
    "echr": SourceInfo(
        # keyword_search stays False: HUDOC's query language is Lucene-ish and a bare
        # multi-word keyword injected into it returns an empty result set rather than an
        # error — a silent nothing. Keywords post-filter; ``query`` is the expert escape.
        "echr", "ECHR case law (HUDOC)", "caselaw", "CoE", False,
        "Walks HUDOC's Chamber/Grand Chamber judgment feed newest-first by default, so a "
        "watch picks up judgments as the Court publishes them; a backfill walks the whole "
        "series. Name ids to fetch specific judgments by ECLI (ECLI:CE:ECHR:…) or "
        "application number (58170/13).",
        (SourceOption("ids", "ECLIs or application numbers", "58170/13, ECLI:CE:ECHR:2021:0525JUD005817013"),
         SourceOption("collections", "HUDOC collections to follow",
                      "GRANDCHAMBER,CHAMBER (default); add COMMITTEE or DECISIONS"),
         SourceOption("query", "Extra HUDOC query clause", 'e.g. article="8"')),
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
    "uk-legislation-materials": SourceInfo(
        "uk-legislation-materials", "UK explanatory notes & impact assessments",
        "guidance", "GB", False,
        "Imports official explanatory notes and impact assessments from "
        "legislation.gov.uk. Name Acts/SIs to pull their accompanying material; "
        "with no ids, follows the newest-first UK impact-assessment feed. Older "
        "structured notes and newer paged HTML notes are both supported.",
        (SourceOption("ids", "Parent legislation or impact-assessment ids",
                      "ukpga/2000/36,ukpga/2018/12,ukia/2016/251"),
         SourceOption("notes", "Include explanatory notes", "true (default)"),
         SourceOption("impacts", "Include impact assessments", "true (default)")),
        ("parent legislation id", "impact-assessment id (ukia/2016/251)"),
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
    "uk-fca-notices": SourceInfo(
        "uk-fca-notices", "FCA decision and final notices", "guidance", "GB", False,
        "Official FCA enforcement PDFs discovered through sitemap last-modified "
        "timestamps. Notices remain deduplicated but are excluded from retrieval when "
        "the legal grammars find no case or legislation citation; no single statute is "
        "assumed because FCA notices span several regulatory regimes.",
        (), ("FCA decision/final notice PDF",),
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
    "uk-ipt": SourceInfo(
        "uk-ipt", "Investigatory Powers Tribunal", "caselaw", "GB", True,
        "Judgments of the Investigatory Powers Tribunal, published one HTML page each at "
        "investigatorypowerstribunal.org.uk. Identity is the neutral citation printed on an "
        "early line ([2025] UKIPTrib 10 -> ukiptrib/2025/10); the body is segmented by the "
        "numbered paragraph a later judgment pinpoints. A backfill reads the listing page "
        "(the whole set in one request); a watch posts the site's own date-range filter, so "
        "an incremental check normally returns nothing. The Strasbourg judgment in Kennedy, "
        "republished on the site, is skipped -- its infobox carries an application number "
        "rather than a case number, and the corpus holds ECtHR judgments under their HUDOC "
        "identity. Within this source RIPA and IPA resolve to the Regulation of "
        "Investigatory Powers Act 2000 and the Investigatory Powers Act 2016.",
        (),
        ("neutral citation ([2025] UKIPTrib 10)", "IPT case number"),
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
         SourceOption("include_consolidations", "Fetch dated consolidations",
                      "true — with explicitly named sector-3 CELEX ids"),
         SourceOption("consolidations_only", "Walk every dated consolidation",
                      "true — all Cellar sector-0 acts, including future snapshots"),
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
    "eu-ep-resolutions": SourceInfo(
        "eu-ep-resolutions", "European Parliament resolutions and adopted texts",
        "preparatory", "EU", False,
        "Adopted texts of the European Parliament, discovered over CELLAR sector 5 and "
        "read from the Parliament\u2019s own Open Data API where it holds them \u2014 which "
        "is the only way to get a resolution before the Official Journal publishes it, "
        "often a year after the vote. The default is the non-legislative (IP) family: "
        "own-initiative and implementation resolutions, which comment on law already in "
        "force. Coverage runs to 1979; text degrades with age (Formex from ~2007, HTML "
        "1995\u20132006, PDF for parts of 1997\u20132004, metadata-only before ~1994). "
        "Recitals and numbered operative paragraphs are citable segments, and each text "
        "is aliased by CELEX, P8_TA(2017)0051 and T8-0051/2017 alike.",
        (SourceOption("celex", "CELEX ids", "52017IP0051,52024AP0138"),
         SourceOption("types", "Document families", "IP (default), AP, DP, BP, XP"),
         SourceOption("years", "Year range", "1979-2026"),
         SourceOption("use_ep_portal", "Prefer the Parliament\u2019s own XML",
                      "true (default) \u2014 false to read CELLAR only")),
        ("CELEX (52017IP0051)", "P8_TA(2017)0051", "T8-0051/2017"),
    ),
    "eu-ep-followups": SourceInfo(
        "eu-ep-followups", "Commission follow-up to Parliament resolutions",
        "preparatory", "EU", False,
        "The Parliament\u2019s external-documents register \u2014 which holds one work type, "
        "the Commission\u2019s formal reply to an adopted text, answering its numbered "
        "paragraphs. Each is linked back to the resolution it answers. This register does "
        "NOT carry EPRS studies or briefings; no endpoint of the Open Data API does. "
        "Items are retained for the pairing but enter retrieval only when the grammars "
        "recognise a legal citation in them.",
        (),
        ("EP external-document id (SP-2026-04-14-TA-10-2025-0343)",),
    ),
    "eu-ep-thinktank": SourceInfo(
        "eu-ep-thinktank", "European Parliament research (EPRS, policy departments)",
        "guidance", "EU", False,
        "EPRS briefings, policy-department studies, in-depth analyses and the Fact Sheets "
        "on the European Union \u2014 the Parliament\u2019s own research, which no API "
        "serves. Read from the Think Tank\u2019s advanced search, windowed by date back to "
        "1989 and paged to exhaustion within each window. Text comes from the published "
        "JATS or Fact-Sheet XML where there is one (sections and endnotes preserved) and "
        "from the PDF otherwise. Each document carries its publication type, authors "
        "(internal and commissioned), policy areas, EuroVoc keywords and geographical "
        "areas, each with the facet code the Think Tank itself filters by. Published "
        "under CC-BY 4.0.",
        (SourceOption("years", "Year range", "1989-2026"),
         SourceOption("window_days", "Days per search window", "blank = calendar months"),
         SourceOption("document_ids", "Fetch exactly these",
                      "EPRS_BRI(2026)789356,IPOL_STU(2015)510012"),
         SourceOption("language", "Language of the rendition", "en (default)")),
        ("Think Tank document id (EPRS_BRI(2026)789356)", "Fact Sheet id (04A_FT(2017)N51055)"),
    ),
    "eu-ombudsman": SourceInfo(
        "eu-ombudsman", "European Ombudsman decisions", "guidance", "EU", False,
        "Official English decisions from the Ombudsman's public REST API. Items are "
        "retained for dedup but enter retrieval only when the legal grammars recognise "
        "a case or legislation citation.",
        (), ("Ombudsman case reference",),
    ),
    "eu-edps-opinions": SourceInfo(
        "eu-edps-opinions", "EDPS legislative opinions", "guidance", "EU", False,
        "Official EDPS opinions and operative PDFs, newest first. The listing uses "
        "RagLex's linked Scrapling service where the EDPS WAF blocks direct requests. "
        "Each opinion is linked to Article 42 of Regulation 2018/1725, its formal "
        "legislative-consultation mandate; citations in the PDF add specific laws.",
        (), ("EDPS Opinion number",),
    ),
    "eu-edps-investigations": SourceInfo(
        "eu-edps-investigations", "EDPS investigations and audits",
        "guidance", "EU", False,
        "Official EDPS investigation and audit publications and operative PDFs. "
        "Because the register spans multiple legal regimes, bare articles are not "
        "assigned to a default law; citation-free items are retained as processed "
        "but excluded from retrieval.",
        (), ("EDPS investigation title or reference",),
    ),
    "eu-dgcomp-antitrust": SourceInfo(
        "eu-dgcomp-antitrust", "European Commission antitrust decisions",
        "guidance", "EU", False,
        "English operative decision attachments from DG COMP's official AT open-data "
        "export. Press releases and unattached case publicity are excluded. The "
        "structured case legal basis links Articles 101/102 to the TFEU.",
        (), ("AT.40861", "DG COMP decision attachment"),
    ),
    "eu-esma-sanctions": SourceInfo(
        "eu-esma-sanctions", "ESMA sanctions register",
        "guidance", "EU", False,
        "The official ESMA Solr register of sanctions imposed by ESMA and national "
        "competent authorities. Uses server-side modification-date filtering. Mixed "
        "national and EU legal frameworks are never assigned a guessed default law; "
        "citation-free entries remain processed but are excluded from retrieval.",
        (), ("ESMA sanction id", "sanctioned entity", "national authority"),
    ),
    "eu-esas-boa": SourceInfo(
        "eu-esas-boa", "ESAs Joint Board of Appeal decisions",
        "guidance", "EU", False,
        "Full-text decisions of the independent Joint Board of Appeal for EBA, EIOPA "
        "and ESMA. The official EIOPA register is polled newest-first. Because appeals "
        "span different sectoral laws, only recognised legal citations admit a decision "
        "to retrieval.",
        (), ("Board of Appeal decision title", "document UUID"),
    ),
    "eu-srb-appeals": SourceInfo(
        "eu-srb-appeals", "Single Resolution Board Appeal Panel decisions",
        "guidance", "EU", False,
        "The SRB Appeal Panel's official thematic decisions register, including case "
        "number, publication and decision dates, description and full PDF. Its banking, "
        "access-to-documents and procedure regimes are mixed, so citation-free records "
        "are held but excluded from retrieval.",
        (), ("SRB Appeal Panel case number", "decision PDF"),
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
    "ie-dpc": SourceInfo(
        "ie-dpc", "Irish Data Protection Commission decisions", "guidance", "IE", False,
        "The DPC's official decision register and operative PDFs. The register's "
        "Articles facet becomes structured links to GDPR articles or, where explicitly "
        "prefixed S, sections of the Irish Data Protection Act 2018.",
        (), ("DPC inquiry reference",),
    ),
    "ie-dpc-guidance": SourceInfo(
        "ie-dpc-guidance", "Irish Data Protection Commission guidance",
        "guidance", "IE", False,
        "The DPC's guidance library as the Commission itself indexes it, keeping its "
        "topical sections (General Guidance, Technological issues, GDPR requirements, "
        "Direct marketing/Electoral, COVID-19) as tags. Most items are published "
        "two-step — a short landing page over a 'Full Guidance Note' PDF — so both are "
        "pulled and concatenated. The hub's EDPB accordion is skipped: those documents "
        "are held under the edpb source.",
        (), ("dataprotection.ie guidance URL",),
    ),
    "ie-tax-appeals": SourceInfo(
        "ie-tax-appeals", "Irish Tax Appeals Commission determinations",
        "caselaw", "IE", False,
        "Official TAC determination register and PDFs, newest first. Chrome TLS is "
        "used for the WAF and blocked HTML can escalate to RagLex's linked Scrapling "
        "service. Compact citations such as 79TACD2026 resolve to the harvested case.",
        (), ("79TACD2026", "tacd/2026/79"),
    ),
    "ie-revenue-tdm": SourceInfo(
        "ie-revenue-tdm", "Irish Revenue Tax and Duty Manuals",
        "guidance", "IE", False,
        "The official current Revenue manual register and PDFs. Timestamped prior "
        "versions provide a cheap per-manual refresh signal. The manuals span many "
        "tax statutes, so only recognised citations are linked and citation-free "
        "items remain processed but outside retrieval.",
        (), ("Revenue manual code", "Part 01-00-02"),
    ),
    "ie-ccpc-mergers": SourceInfo(
        "ie-ccpc-mergers", "Irish CCPC merger determinations",
        "guidance", "IE", False,
        "Operative determination PDFs from the official live merger register. This "
        "is a single-regime feed under the Competition Act 2002, so genuinely bare "
        "section references are anchored to that Act; explicitly named other laws "
        "remain with the normal grammar resolver.",
        (), ("M/26/044", "M.26.044"),
    ),
    "ie-oireachtas": SourceInfo(
        "ie-oireachtas", "Oireachtas documents laid — committee reports and statutory "
        "reports", "preparatory", "IE", True,
        "Everything laid before the Dáil and the Seanad, from the Oireachtas Library's "
        "catalogue: committee reports, the annual reports and accounts a statute obliges "
        "a body to lay, post-enactment reviews, EU scrutiny notes and treaty texts. Each "
        "record names the provision that obliged it to be laid, which is recorded as a "
        "citation edge — most annual reports name their enabling section nowhere in the "
        "PDF. The Oireachtas's own publications search is behind a captcha; this "
        "catalogue is not, and reaches back to 1922. Statutory instruments are excluded "
        "by default (their text is already held from the Statute Book) and the sweep "
        "starts at 1996; both are options. PDFs are OCR'd when the scan has no text.",
        (SourceOption("since_year", "Earliest year to sweep (Date Laid)", "1996"),
         SourceOption("subcollections",
                      "Subcollections to sweep — \"*\" for all, or a comma-separated "
                      "list", "Committee Report,Ombudsman Report"),
         SourceOption("include_statutory_instruments",
                      "Also hold the 40,643 laid statutory instruments", "false"),
         SourceOption("collections",
                      "Catalogue collections to sweep", "Documents Laid,L&RS Publications"),
         SourceOption("query", "Catalogue keyword search (searched at the source)",
                      "data protection"),
         SourceOption("max_kb", "Skip files larger than this many KB", "120000")),
        ("DL211160", "ie/oireachtas/opac/215643"),
    ),
    "ie-oireachtas-committees": SourceInfo(
        "ie-oireachtas-committees", "Oireachtas committee evidence — opening statements "
        "and submissions", "preparatory", "IE", False,
        "The evidence Oireachtas committees heard, which is never laid before the Houses "
        "and so appears in no catalogue: opening statements, witness submissions and "
        "briefings. This is where the legal argument is — sampled submissions cited "
        "statutes where the minutes beside them cited nothing, so retrieval is gated on "
        "the grammars finding a statute or an authority. Which committees exist comes "
        "from the open-data API (232 across the 31st to 34th Dáil, against the 89 the "
        "website's index shows); the documents come from each committee's own page. "
        "Coverage is the recent tail per committee and accumulates as it is re-run: the "
        "complete index is behind the site's captcha AND disallowed by its robots.txt, "
        "so no backfill can reach further. Reports are excluded by default because "
        "ie-oireachtas already holds them with their date laid and enabling provision.",
        (SourceOption("include_reports",
                      "Also take committee reports (duplicates ie-oireachtas)", "false"),
         SourceOption("families", "Document families — \"*\" for all",
                      "submissions,reports"),
         SourceOption("houses", "Dáil numbers to sweep (comma-separated)", "33,34"),
         SourceOption("first_house", "Oldest Dáil to try (the site starts at the 32nd)",
                      "32")),
        ("ie/oireachtas/committee/dail/33/joint_committee_on_justice/submissions/"
         "2024-10-08_opening-statement-dr-sharon-lambert",),
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
    "au-vic": SourceInfo(
        "au-vic", "Victoria current consolidated legislation", "legislation", "AU", False,
        "Official legislation.vic.gov.au JSON:API for in-force Acts and statutory "
        "rules, with the current authorised PDF/DOCX body and effective date.",
        (), ("au/vic/act/year/number", "au/vic/regulation/year/number"),
    ),
    "au-sa": SourceInfo(
        "au-sa", "South Australia current consolidated legislation",
        "legislation", "AU", False,
        "Official fortnightly SA Parliamentary Counsel XML update packages. A full "
        "walk seeds every consolidation; routine runs fetch only CKAN releases newer "
        "than the cursor, turning the former bulk-only source into a live overlay.",
        (), ("au/sa/act/year/number", "au/sa/regulation/year/number"),
    ),
    "au-wa": SourceInfo(
        "au-wa", "Western Australia current consolidated legislation",
        "legislation", "AU", False,
        "Official Parliamentary Counsel in-force Acts and subsidiary legislation. "
        "The alphabetical register is a live manifest: a changed mrdoc rendition "
        "causes only that consolidation to be refetched.",
        (), ("au/wa/act/year/number", "au/wa/regulation/year/number"),
    ),
    "au-esafety-osa": SourceInfo(
        "au-esafety-osa", "Australian eSafety Online Safety Act publications",
        "scrape", "AU", False,
        "The eSafety Commissioner's official live Register of Online Safety Codes and "
        "Standards plus its Regulatory Guidance page. Canonically titled current PDFs "
        "are linked to the Online Safety Act 2021 (Cth); only references explicitly "
        "qualified as sections of 'the Act' are provision-anchored, avoiding internal "
        "code sections. Chrome-TLS is used first with linked Scrapling fallback.",
        (), ("eSafety publication title", "eSafety PDF URL"),
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
    "ca-scc-live": SourceInfo(
        "ca-scc-live", "Supreme Court of Canada — official live decisions",
        "caselaw", "CA", False,
        "The Court's official RSS and complete HTML judgments. The feed reports "
        "translations, amendments and corrections as well as new decisions; neutral "
        "citation identity merges them with the Canadian bulk seed.",
        (), ("SCC neutral citation",),
    ),
    "ca-tcc-live": SourceInfo(
        "ca-tcc-live", "Tax Court of Canada — official live decisions",
        "caselaw", "CA", False,
        "The Tax Court's official RSS and complete HTML judgments, preserving native "
        "paragraph anchors and merging with the bulk seed by neutral citation.",
        (), ("TCC neutral citation",),
    ),
    "ca-fc-live": SourceInfo(
        "ca-fc-live", "Federal Court of Canada — official live decisions",
        "caselaw", "CA", False,
        "The Federal Court's official RSS and complete HTML judgments. New and "
        "corrected decisions merge with the Canadian bulk seed by neutral citation.",
        (), ("FC neutral citation",),
    ),
    "ca-fca-live": SourceInfo(
        "ca-fca-live", "Federal Court of Appeal — official live decisions",
        "caselaw", "CA", False,
        "The Court's official Recent Additions channel and complete Norma judgments, "
        "including English and French decisions and native paragraph anchors.",
        (), ("FCA or CAF neutral citation",),
    ),
    "ca-sst-live": SourceInfo(
        "ca-sst-live", "Social Security Tribunal of Canada — official live decisions",
        "caselaw", "CA", False,
        "The Tribunal's official Recent Additions channel and complete Decisia "
        "decisions. Neutral-citation identity enriches the SST decisions already "
        "present in the Canadian bulk seed, while supplying new and corrected cases.",
        (), ("SST or TSS neutral citation",),
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
        "au-hca", "High Court of Australia (full text)", "caselaw", "AU", False,
        "Full-text High Court judgments from hcourt.gov.au. The site WAFs everything but a "
        "real Chrome, so it is fetched with curl_cffi's Chrome-TLS impersonation (no browser): "
        "the listing (items_per_page=100&page=N, ~14 pages for 1998→present, newest-first, "
        "watermark stop) → each judgment's detail page → its DOCX (extracted to text). Keyed "
        "by neutral citation (hca/2026/22), unifying with au-caselaw and resolving pending "
        "'[2026] HCA 22' citations; a judgment whose DOCX is unreachable falls back to a "
        "metadata stub. path= imports a listing page saved from a browser instead of live.",
        (SourceOption("path", "Saved listing HTML", "a listing page (or folder) saved from a browser"),
         SourceOption("max_pages", "Listing page cap", "40 (default); 100 judgments/page")),
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
    "ie-caselaw": SourceInfo(
        "ie-caselaw", "Ireland — Supreme Court / Court of Appeal / High Court", "caselaw",
        "IE", False,
        "Judgments from the Courts Service register (ww2.courts.ie). A keep-current run "
        "walks the /judgments landing page (newest first) and stops at the first case "
        "already held; a backfill walks the year-search facet by (court, year) back to "
        "2001. Each row's /view/ detail page carries the authoritative Neutral Citation, "
        "Record Number, court, judge and delivery date — identity is taken from there, "
        "never the unreliable PDF filename. The judgment PDF is fetched and its numbered "
        "paragraphs become citable segments. Keyed by the neutral-cite slug "
        "(\"[2025] IESC 49\" → iesc/2025/49), so held cases resolve pending citations. A "
        "multi-judgment case (one PDF per judge) stores each opinion separately, grouped "
        "by case_citation, with the lead opinion owning the bare slug.",
        (SourceOption("path", "Saved listing HTML", "a saved /judgments or year-search page/folder"),),
        ("neutral citation ([2025] IESC 49)", "iesc/2025/49"),
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
        "fr-cnil", "France — CNIL deliberations (Légifrance)", "administrative", "FR", False,
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
        (SourceOption("ids", "Decision ids/ECLIs", "ECLI:FR:CCASS:2021:C100400"),
         SourceOption("since_date", "Stop a seed here (where the DILA bulk ends)",
                      "2025-07-01")),
        ("ECLI:FR:CCASS:…", "Judilibre decision id"),
    ),
    "fr-judilibre-ca": SourceInfo(
        "fr-judilibre-ca", "France — cours d'appel (Judilibre)", "caselaw", "FR", False,
        "The appellate half of Judilibre — 626,374 decisions, the same endpoint under "
        "jurisdiction=ca. NOT ECLI-native: these carry no ECLI at all, so they are keyed "
        "by their Judilibre id, and their number is an RG number unique only within the "
        "issuing court (24/00002 is live at Nîmes and at Amiens on the same day), so the "
        "citation key is scoped by court. Complements the DILA CAPP bulk (73,046 held) "
        "rather than replacing it.",
        (SourceOption("ids", "Decision ids", "5fca…"),
         SourceOption("since_date", "Stop a seed here", "2020-01-01")),
        ("Judilibre decision id",),
    ),
    "fr-judilibre-tj": SourceInfo(
        "fr-judilibre-tj", "France — tribunaux judiciaires (Judilibre)", "caselaw", "FR",
        False,
        "First-instance civil France — 697,807 decisions, jurisdiction=tj on the same "
        "endpoint, and the largest single register PISTE exposes. No ECLI and a "
        "court-scoped RG number, as with the cours d'appel. Nothing else in the corpus "
        "reaches this material.",
        (SourceOption("ids", "Decision ids", "5fca…"),
         SourceOption("since_date", "Stop a seed here", "2020-01-01")),
        ("Judilibre decision id",),
    ),
    "fr-senat-reports": SourceInfo(
        "fr-senat-reports", "Sénat — rapports d'information", "preparatory", "FR", False,
        "French Senate information and control reports. Incremental runs use the Senate's "
        "Atom feed; backfills walk every parliamentary session since 1958. Each notice is "
        "followed to the complete one-page HTML report, with whole-report PDF fallback.",
        (SourceOption("start_offset", "Resume listing offset", "0"),),
        ("Sénat report number", "r25-883"),
    ),
    "fr-senat-lc": SourceInfo(
        "fr-senat-lc", "Sénat — études de législation comparée", "preparatory", "FR", False,
        "All comparative-law studies (LC) from the Senate's complete year accordions. The "
        "collapsed sections are present in static markup; each notice is followed to the "
        "complete HTML study or, where HTML is unavailable, the complete PDF.",
        (SourceOption("start_offset", "Resume listing offset", "0"),),
        ("LC study number", "LC 362"),
    ),
    "fr-an-reports": SourceInfo(
        "fr-an-reports", "Assemblée nationale — rapports d'information", "preparatory",
        "FR", False,
        "Information reports from every separately queried legislature. Backfills do not "
        "rely on the current-legislature default: they page legislatures 17 through 1 and "
        "prefer the complete dyn/opendata HTML rendition, falling back to the report PDF.",
        (SourceOption("legislatures", "Legislatures (comma-separated)", "17,16,15"),
         SourceOption("start_offset", "Resume listing offset", "0"),
         SourceOption("page_size", "Rows per listing page", "150")),
        ("Assemblée report number", "RINFANR5L17B3074"),
    ),
    "nl-tk-reports": SourceInfo(
        "nl-tk-reports", "Tweede Kamer — committee and research reports", "preparatory",
        "NL", False,
        "Committee/debate reports and standalone parliamentary, scientific and audit "
        "reports from the public OData v4 service. Bills, votes and bill-stage reports are "
        "excluded. OData's modification timestamp makes watches incremental; the full "
        "authoritative DOCX resource is ingested because the public HTML page is metadata.",
        (SourceOption("types", "Document types (comma-separated)", "Rapport,Jaarverslag"),
         SourceOption("start_offset", "Resume OData offset", "0"),
         SourceOption("page_size", "OData page size", "250")),
        ("Tweede Kamer document number", "2026D38058"),
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
    "de-bt-drucksachen": SourceInfo(
        "de-bt-drucksachen", "Bundestagsdrucksachen — legislative history",
        "preparatory", "DE", False,
        "Official Bundestag bills, explanatory memoranda, committee reports and "
        "government answers from the DIP API. Discovery uses the last-modified cursor; "
        "the flat text's Besonderer Teil is recovered into nested Artikel/Nummer/"
        "Buchstabe segments, and headings that explicitly name an amended statute emit "
        "provision-level interprets edges. Drucksachen are tagged as official works "
        "under § 5(1) UrhG. Requires a Bundestag DIP API key.",
        (SourceOption("api_key", "DIP API key", "or BUNDESTAG_DIP_API_KEY"),
         SourceOption("document_numbers", "Drucksache numbers", "20/5548,19/28444"),
         SourceOption("types", "Drucksache types",
                      "Gesetzentwurf,Beschlussempfehlung und Bericht,Antwort"),
         SourceOption("prefer_pdf_tables", "Use PDF for transposition bills",
                      "true (default) — preserves correlation-table layout"),
         SourceOption("start_offset", "Resume listing offset", "0")),
        ("BT-Drs 20/5548", "Drucksache 20/5548", "DIP document id"),
    ),
    "de-bt-wd": SourceInfo(
        "de-bt-wd", "Wissenschaftliche Dienste and Fachbereich Europa papers",
        "guidance", "DE", False,
        "Bundestag research-service memoranda from the public Analysen listing. The "
        "adapter preserves each PDF, extracts its numbered outline, registers WD/PE/EU "
        "document-number aliases, and marks the Bundestag's reserved publication and "
        "distribution rights per document (attributed excerpts only). The listing is "
        "newest-first and is safe for a recurring watch.",
        (SourceOption("ids", "Exact Bundestag PDF URLs", "https://www.bundestag.de/resource/blob/…/paper.pdf"),
         SourceOption("start_offset", "Resume listing offset", "0"),
         SourceOption("limit", "Rows per fragment request", "50")),
        ("WD 3 - 3000 - 045/21", "WD3/045/21", "Bundestag resource PDF URL"),
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
    "at-justiz": SourceInfo(
        "at-justiz", "Austria — OGH, OLG, LG, BG (ordentliche Gerichtsbarkeit)", "caselaw", "AT", True,
        "Austria's RIS OGD API — no key, native ECLI (including on Rechtssätze), "
        "and RIS's own structured Normen index, which writes EU instruments under "
        "their German abbreviations (DSGVO Art15) and so joins straight onto the "
        "CELEX the corpus already holds. The RIS house XML is parsed into the "
        "decision's own zones (Kopf, Spruch, Text, Begründung) with its bracketed "
        "paragraph numbers kept as citable labels. Statutory references resolve to "
        "at/gesetz/…, never to the German act of the same abbreviation: KSchG is "
        "consumer protection in Vienna and dismissal protection in Berlin. This is "
        "the civil and criminal side (~138,000 documents, 1954 onward) and the home "
        "of the Rechtssatz corpus: each proposition carries its own permanent "
        "number, its own ECLI, and the full list of decisions that have applied it, "
        "typed by the documentation office's own shorthand — applied, cf., "
        "contrary, and 'Ablehnung von <docket>', an express rejection naming the "
        "decision rejected. Those become typed citator edges rather than something "
        "a classifier has to guess from prose. ",
        (SourceOption("document_type", "Only one document type",
                      "Rechtssatz | Entscheidungstext"),
         SourceOption("query", "Keyword query", "free text, searched in the RIS API"),
         SourceOption("norm", "Only decisions on one norm", "e.g. DSGVO"),
         SourceOption("earliest_year", "Backfill from", "default 1945"),
         SourceOption("start_date", "Backfill from a date", "YYYY-MM-DD"),
         SourceOption("lookback_days", "Keep-current window", "default 120"),
         SourceOption("ids", "RIS document numbers / ECLIs / Geschaeftszahlen",
                      "JJT_20260130_OGH0002_… , ECLI:AT:OGH0002:2026:RS0142730")),
        ("RIS document number", "ECLI:AT:…", "Geschäftszahl (6 Ob 127/20z)"),
    ),
    "at-vwgh": SourceInfo(
        "at-vwgh", "Austria — Verwaltungsgerichtshof", "caselaw", "AT", True,
        "Austria's RIS OGD API — no key, native ECLI (including on Rechtssätze), "
        "and RIS's own structured Normen index, which writes EU instruments under "
        "their German abbreviations (DSGVO Art15) and so joins straight onto the "
        "CELEX the corpus already holds. The RIS house XML is parsed into the "
        "decision's own zones (Kopf, Spruch, Text, Begründung) with its bracketed "
        "paragraph numbers kept as citable labels. Statutory references resolve to "
        "at/gesetz/…, never to the German act of the same abbreviation: KSchG is "
        "consumer protection in Vienna and dismissal protection in Berlin. The "
        "supreme administrative court, ~357,000 documents. Its Rechtssätze carry a "
        "Stammrechtssatznummer — the parent proposition this one restates — which "
        "is recorded as a derivation edge between the two. ",
        (SourceOption("document_type", "Only one document type",
                      "Rechtssatz | Entscheidungstext"),
         SourceOption("query", "Keyword query", "free text, searched in the RIS API"),
         SourceOption("norm", "Only decisions on one norm", "e.g. DSGVO"),
         SourceOption("earliest_year", "Backfill from", "default 1945"),
         SourceOption("start_date", "Backfill from a date", "YYYY-MM-DD"),
         SourceOption("lookback_days", "Keep-current window", "default 120"),
         SourceOption("ids", "RIS document numbers / ECLIs / Geschaeftszahlen",
                      "JJT_20260130_OGH0002_… , ECLI:AT:OGH0002:2026:RS0142730")),
        ("RIS document number", "ECLI:AT:…", "Geschäftszahl (6 Ob 127/20z)"),
    ),
    "at-vfgh": SourceInfo(
        "at-vfgh", "Austria — Verfassungsgerichtshof", "caselaw", "AT", True,
        "Austria's RIS OGD API — no key, native ECLI (including on Rechtssätze), "
        "and RIS's own structured Normen index, which writes EU instruments under "
        "their German abbreviations (DSGVO Art15) and so joins straight onto the "
        "CELEX the corpus already holds. The RIS house XML is parsed into the "
        "decision's own zones (Kopf, Spruch, Text, Begründung) with its bracketed "
        "paragraph numbers kept as citable labels. Statutory references resolve to "
        "at/gesetz/…, never to the German act of the same abbreviation: KSchG is "
        "consumer protection in Vienna and dismissal protection in Berlin. The "
        "constitutional court, ~24,000 documents, with the court's own Leitsatz and "
        "its official collection number (VfSlg), registered as an alias so a 'VfSlg "
        "19.632/2012' citation resolves. ",
        (SourceOption("document_type", "Only one document type",
                      "Rechtssatz | Entscheidungstext"),
         SourceOption("query", "Keyword query", "free text, searched in the RIS API"),
         SourceOption("norm", "Only decisions on one norm", "e.g. DSGVO"),
         SourceOption("earliest_year", "Backfill from", "default 1945"),
         SourceOption("start_date", "Backfill from a date", "YYYY-MM-DD"),
         SourceOption("lookback_days", "Keep-current window", "default 120"),
         SourceOption("ids", "RIS document numbers / ECLIs / Geschaeftszahlen",
                      "JJT_20260130_OGH0002_… , ECLI:AT:OGH0002:2026:RS0142730")),
        ("RIS document number", "ECLI:AT:…", "Geschäftszahl (6 Ob 127/20z)"),
    ),
    "at-bvwg": SourceInfo(
        "at-bvwg", "Austria — Bundesverwaltungsgericht", "caselaw", "AT", True,
        "Austria's RIS OGD API — no key, native ECLI (including on Rechtssätze), "
        "and RIS's own structured Normen index, which writes EU instruments under "
        "their German abbreviations (DSGVO Art15) and so joins straight onto the "
        "CELEX the corpus already holds. The RIS house XML is parsed into the "
        "decision's own zones (Kopf, Spruch, Text, Begründung) with its bracketed "
        "paragraph numbers kept as citable labels. Statutory references resolve to "
        "at/gesetz/…, never to the German act of the same abbreviation: KSchG is "
        "consumer protection in Vienna and dismissal protection in Berlin. The "
        "federal administrative court, ~288,000 documents, 2014 onward. ",
        (SourceOption("document_type", "Only one document type",
                      "Rechtssatz | Entscheidungstext"),
         SourceOption("query", "Keyword query", "free text, searched in the RIS API"),
         SourceOption("norm", "Only decisions on one norm", "e.g. DSGVO"),
         SourceOption("earliest_year", "Backfill from", "default 1945"),
         SourceOption("start_date", "Backfill from a date", "YYYY-MM-DD"),
         SourceOption("lookback_days", "Keep-current window", "default 120"),
         SourceOption("ids", "RIS document numbers / ECLIs / Geschaeftszahlen",
                      "JJT_20260130_OGH0002_… , ECLI:AT:OGH0002:2026:RS0142730")),
        ("RIS document number", "ECLI:AT:…", "Geschäftszahl (6 Ob 127/20z)"),
    ),
    "at-lvwg": SourceInfo(
        "at-lvwg", "Austria — Landesverwaltungsgerichte", "caselaw", "AT", True,
        "Austria's RIS OGD API — no key, native ECLI (including on Rechtssätze), "
        "and RIS's own structured Normen index, which writes EU instruments under "
        "their German abbreviations (DSGVO Art15) and so joins straight onto the "
        "CELEX the corpus already holds. The RIS house XML is parsed into the "
        "decision's own zones (Kopf, Spruch, Text, Begründung) with its bracketed "
        "paragraph numbers kept as citable labels. Statutory references resolve to "
        "at/gesetz/…, never to the German act of the same abbreviation: KSchG is "
        "consumer protection in Vienna and dismissal protection in Berlin. The nine "
        "state administrative courts, ~77,000 documents, each record carrying its "
        "Bundesland — which is what distinguishes the nine different Bauordnungen "
        "and Raumplanungsgesetze that share one abbreviation. ",
        (SourceOption("document_type", "Only one document type",
                      "Rechtssatz | Entscheidungstext"),
         SourceOption("query", "Keyword query", "free text, searched in the RIS API"),
         SourceOption("norm", "Only decisions on one norm", "e.g. DSGVO"),
         SourceOption("earliest_year", "Backfill from", "default 1945"),
         SourceOption("start_date", "Backfill from a date", "YYYY-MM-DD"),
         SourceOption("lookback_days", "Keep-current window", "default 120"),
         SourceOption("ids", "RIS document numbers / ECLIs / Geschaeftszahlen",
                      "JJT_20260130_OGH0002_… , ECLI:AT:OGH0002:2026:RS0142730")),
        ("RIS document number", "ECLI:AT:…", "Geschäftszahl (6 Ob 127/20z)"),
    ),
    "at-dsb": SourceInfo(
        "at-dsb", "Datenschutzbehörde decisions (Austria — DPA)", "administrative", "AT", True,
        "Austria's RIS OGD API — no key, native ECLI (including on Rechtssätze), "
        "and RIS's own structured Normen index, which writes EU instruments under "
        "their German abbreviations (DSGVO Art15) and so joins straight onto the "
        "CELEX the corpus already holds. The RIS house XML is parsed into the "
        "decision's own zones (Kopf, Spruch, Text, Begründung) with its bracketed "
        "paragraph numbers kept as citable labels. Statutory references resolve to "
        "at/gesetz/…, never to the German act of the same abbreviation: KSchG is "
        "consumer protection in Vienna and dismissal protection in Berlin. The "
        "Austrian data-protection authority's decisions (~1,900), modelled in RIS "
        "alongside the courts. Each carries its Anfechtung note — whether it is "
        "final or still open to appeal to the Bundesverwaltungsgericht — which is "
        "the only statement RIS makes about a decision's standing. ",
        (SourceOption("document_type", "Only one document type",
                      "Rechtssatz | Entscheidungstext"),
         SourceOption("query", "Keyword query", "free text, searched in the RIS API"),
         SourceOption("norm", "Only decisions on one norm", "e.g. DSGVO"),
         SourceOption("earliest_year", "Backfill from", "default 1945"),
         SourceOption("start_date", "Backfill from a date", "YYYY-MM-DD"),
         SourceOption("lookback_days", "Keep-current window", "default 120"),
         SourceOption("ids", "RIS document numbers / ECLIs / Geschaeftszahlen",
                      "JJT_20260130_OGH0002_… , ECLI:AT:OGH0002:2026:RS0142730")),
        ("RIS document number", "ECLI:AT:…", "Geschäftszahl (6 Ob 127/20z)"),
    ),
    "at-gbk": SourceInfo(
        "at-gbk", "Gleichbehandlungskommission (Austria — equal treatment)", "administrative", "AT", True,
        "Austria's RIS OGD API — no key, native ECLI (including on Rechtssätze), "
        "and RIS's own structured Normen index, which writes EU instruments under "
        "their German abbreviations (DSGVO Art15) and so joins straight onto the "
        "CELEX the corpus already holds. The RIS house XML is parsed into the "
        "decision's own zones (Kopf, Spruch, Text, Begründung) with its bracketed "
        "paragraph numbers kept as citable labels. Statutory references resolve to "
        "at/gesetz/…, never to the German act of the same abbreviation: KSchG is "
        "consumer protection in Vienna and dismissal protection in Berlin. The "
        "equal-treatment commission's opinions, recorded with the senate, the "
        "discrimination ground and the form of discrimination alleged. ",
        (SourceOption("document_type", "Only one document type",
                      "Rechtssatz | Entscheidungstext"),
         SourceOption("query", "Keyword query", "free text, searched in the RIS API"),
         SourceOption("norm", "Only decisions on one norm", "e.g. DSGVO"),
         SourceOption("earliest_year", "Backfill from", "default 1945"),
         SourceOption("start_date", "Backfill from a date", "YYYY-MM-DD"),
         SourceOption("lookback_days", "Keep-current window", "default 120"),
         SourceOption("ids", "RIS document numbers / ECLIs / Geschaeftszahlen",
                      "JJT_20260130_OGH0002_… , ECLI:AT:OGH0002:2026:RS0142730")),
        ("RIS document number", "ECLI:AT:…", "Geschäftszahl (6 Ob 127/20z)"),
    ),
    "at-verg": SourceInfo(
        "at-verg", "Austria — procurement review (Vergabekontrolle)", "administrative", "AT", True,
        "Austria's RIS OGD API — no key, native ECLI (including on Rechtssätze), "
        "and RIS's own structured Normen index, which writes EU instruments under "
        "their German abbreviations (DSGVO Art15) and so joins straight onto the "
        "CELEX the corpus already holds. The RIS house XML is parsed into the "
        "decision's own zones (Kopf, Spruch, Text, Begründung) with its bracketed "
        "paragraph numbers kept as citable labels. Statutory references resolve to "
        "at/gesetz/…, never to the German act of the same abbreviation: KSchG is "
        "consumer protection in Vienna and dismissal protection in Berlin. "
        "Public-procurement review decisions from the federal and state review "
        "bodies. ",
        (SourceOption("document_type", "Only one document type",
                      "Rechtssatz | Entscheidungstext"),
         SourceOption("query", "Keyword query", "free text, searched in the RIS API"),
         SourceOption("norm", "Only decisions on one norm", "e.g. DSGVO"),
         SourceOption("earliest_year", "Backfill from", "default 1945"),
         SourceOption("start_date", "Backfill from a date", "YYYY-MM-DD"),
         SourceOption("lookback_days", "Keep-current window", "default 120"),
         SourceOption("ids", "RIS document numbers / ECLIs / Geschaeftszahlen",
                      "JJT_20260130_OGH0002_… , ECLI:AT:OGH0002:2026:RS0142730")),
        ("RIS document number", "ECLI:AT:…", "Geschäftszahl (6 Ob 127/20z)"),
    ),
    "at-ris": SourceInfo(
        "at-ris", "Austria — RIS Judikatur (any application)", "caselaw", "AT", True,
        "Austria's RIS OGD API — no key, native ECLI (including on Rechtssätze), "
        "and RIS's own structured Normen index, which writes EU instruments under "
        "their German abbreviations (DSGVO Art15) and so joins straight onto the "
        "CELEX the corpus already holds. The RIS house XML is parsed into the "
        "decision's own zones (Kopf, Spruch, Text, Begründung) with its bracketed "
        "paragraph numbers kept as citable labels. Statutory references resolve to "
        "at/gesetz/…, never to the German act of the same abbreviation: KSchG is "
        "consumer protection in Vienna and dismissal protection in Berlin. The "
        "generic key: set application to any of the fifteen RIS bodies, including "
        "the historical ones with no key of their own — Dok (disciplinary "
        "commissions), Pvak, Uvs (independent administrative senates, to 2013), "
        "AsylGH (2008–2013), Ubas, Umse and Bks. ",
        (SourceOption("document_type", "Only one document type",
                      "Rechtssatz | Entscheidungstext"),
         SourceOption("query", "Keyword query", "free text, searched in the RIS API"),
         SourceOption("norm", "Only decisions on one norm", "e.g. DSGVO"),
         SourceOption("earliest_year", "Backfill from", "default 1945"),
         SourceOption("start_date", "Backfill from a date", "YYYY-MM-DD"),
         SourceOption("lookback_days", "Keep-current window", "default 120"),
         SourceOption("ids", "RIS document numbers / ECLIs / Geschaeftszahlen",
                      "JJT_20260130_OGH0002_… , ECLI:AT:OGH0002:2026:RS0142730"),
         SourceOption("application", "RIS Applikation",
                      "Justiz | Vwgh | Vfgh | Bvwg | Lvwg | Dsk | Dok | Pvak | Gbk | "
                      "Uvs | AsylGH | Ubas | Umse | Bks | Verg")),
        ("RIS document number", "ECLI:AT:…", "Geschäftszahl (6 Ob 127/20z)"),
    ),
    "sk-ress": SourceInfo(
        "sk-ress", "Slovakia — all courts (Ministry of Justice RESS)", "caselaw", "SK", True,
        "The only genuinely all-instance case-law source in the corpus: 4.68 "
        "million decisions from the district courts up, published by the Ministry "
        "of Justice with an OpenAPI description and no key. Each detail record "
        "carries the ECLI, the legal area, the decision's PDF, and two things that "
        "are otherwise inferred — odkazovanePredpisy, Slov-Lex ELI references with "
        "the section, odsek and písmeno in the fragment, which become structured "
        "statutory edges; and povodnySud plus povodnaSpisovaZnacka, the court below "
        "and its file mark, which is a stated appellate edge typed by the povaha "
        "outcome vocabulary (affirming / annulling / varying). Note the publisher's "
        "asymmetry: judges are named in the clear, parties are pseudonymised. The "
        "date filters obey ISO dates only — given the DD.MM.YYYY form the API "
        "itself prints, they are silently ignored and the whole register is "
        "returned. ",
        (SourceOption("query", "Full-text query", "searched in the ministry API"),
         SourceOption("court_type", "Court type",
                      "Okresný súd | Krajský súd | Mestský súd | Správny súd | "
                      "Najvyšší súd SR"),
         SourceOption("court", "One court", "court GUID from /v1/sud"),
         SourceOption("area", "Legal area", "e.g. Trestné právo"),
         SourceOption("outcome", "Nature of decision",
                      "Potvrdzujúce | Zrušujúce | Zmeňujúce | …"),
         SourceOption("legislation", "Only decisions citing one act",
                      "Slov-Lex ELI, e.g. /SK/ZZ/2018/18"),
         SourceOption("start_date", "Issued from", "YYYY-MM-DD (default 2015-01-01)"),
         SourceOption("end_date", "Issued to", "YYYY-MM-DD"),
         SourceOption("include_text", "Download the decision PDF", "true/false"),
         SourceOption("start_page", "Resume at page", "1-based"),
         SourceOption("ids", "Composite guids / ECLIs / spisové značky",
                      "ECLI:SK:NSSR:2025:6322010282.1")),
        ("ECLI:SK:…", "spisová značka (6S/74/2018)", "composite guid"),
    ),
    "fi-kko": SourceInfo(
        "fi-kko", "Finland — Supreme Court precedents (KKO)", "caselaw", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. KKO's published "
        "precedents, 1979 onward, cited as KKO:2024:1 — which is registered as an "
        "alias so that citation resolves. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("ECLI:FI:KKO:…", "KKO:2024:1", "diary number (S2022/290)"),
    ),
    "fi-kho": SourceInfo(
        "fi-kho", "Finland — Supreme Administrative Court precedents (KHO)", "caselaw", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. KHO's published "
        "precedents, in Finnish and Swedish. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("ECLI:FI:KHO:…", "KHO:2023:45", "diary number"),
    ),
    "fi-hovioikeus": SourceInfo(
        "fi-hovioikeus", "Finland — Courts of Appeal (hovioikeudet)", "caselaw", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. The five appellate "
        "courts' published decisions. The court is read from the document's own "
        "TLCOrganization registry rather than from a table, so a court Finlex adds "
        "arrives correctly named. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("HelHO:2024:12", "diary number (S 24/65)", "year/number"),
    ),
    "fi-hao": SourceInfo(
        "fi-hao", "Finland — Administrative Courts (hallinto-oikeudet)", "caselaw", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. The regional "
        "administrative courts' published decisions. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("diary number", "year/number"),
    ),
    "fi-mao": SourceInfo(
        "fi-mao", "Finland — Market Court (markkinaoikeus)", "caselaw", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. Competition, procurement, "
        "IP and marketing decisions. Its numbering carries a two-digit year "
        "(MAO:123/24) which is expanded before it is stored. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("MAO:123/24", "diary number"),
    ),
    "fi-tt": SourceInfo(
        "fi-tt", "Finland — Labour Court (työtuomioistuin)", "caselaw", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. Collective-agreement "
        "decisions, cited as TT 2024:61. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("TT 2024:61", "diary number"),
    ),
    "fi-vako": SourceInfo(
        "fi-vako", "Finland — Insurance Court (vakuutusoikeus)", "caselaw", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. Social-insurance appeals. "
        "Its numbers are not integers (890-2023), so the field is never typed as "
        "one. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("890-2023", "diary number"),
    ),
    "fi-tsv": SourceInfo(
        "fi-tsv", "Data Protection Ombudsman decisions (Finland — DPA)", "administrative", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. The Finnish "
        "data-protection authority's decisions, modelled as judgments by Finlex. "
        "Each states its legal basis as an ontology concept, which is what "
        "distinguishes a GDPR decision from a national-law one — machine-stated "
        "rather than inferred from the text. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("diary number (TSV/5875/2024)", "year/number"),
    ),
    "fi-oka": SourceInfo(
        "fi-oka", "Chancellor of Justice decisions (Finland)", "administrative", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. The Oikeuskansleri's "
        "decisions on the legality of official action. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("diary number", "year/number"),
    ),
    "fi-saadokset": SourceInfo(
        "fi-saadokset", "Finland — statutes as enacted (säädökset)", "legislation", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. The statute book as "
        "published in the Säädöskokoelma, keyed on the säädösnumero (1050/2018 → "
        "fi/act/2018/1050) that Finnish citation practice uses, with the act's ELI "
        "recorded alongside. Sections are labelled the way Finland numbers them ('5 "
        "§'), not with an OSCOLA prefix, so a citation's anchor matches. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("1050/2018", "year/number"),
    ),
    "fi-saadokset-ajantasa": SourceInfo(
        "fi-saadokset-ajantasa", "Finland — consolidated statutes (ajantasainen)", "legislation", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. The consolidated statute "
        "book. Each dated expression is its own document rather than an overwrite "
        "of the base act. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("1050/2018", "year/number"),
    ),
    "fi-he": SourceInfo(
        "fi-he", "Finland — government proposals (hallituksen esitykset)", "preparatory", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. The travaux préparatoires "
        "Finnish courts cite constantly. The AKN is a metadata wrapper for most of "
        "these, so the proposal itself is taken from main.pdf and the wrapper "
        "supplies its identifiers. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("HE 153/2024", "year/number"),
    ),
    "fi-viranomaismaaraykset": SourceInfo(
        "fi-viranomaismaaraykset", "Finland — authority regulations", "guidance", "FI", True,
        "Finland's Finlex open data — Akoma Ntoso end to end, with ECLI and ELI "
        "aliases, the court's own keyword classification, and publishedSince "
        "returning a per-document NEW/MODIFIED status, which is a cleaner "
        "incremental key than a bare timestamp. The AKN body is parsed into the "
        "judgment's own outline (Johdanto / tausta / Perustelut / Ratkaisu and the "
        "nested tblock headings beneath them). Two routing quirks are handled here: "
        "the whole /judgment/ tree 404s, so the akn_uri the listing returns has to "
        "be rewritten to /doc/, and the court segment must be dropped from it — the "
        "retrieved document's own FRBRWork URI is then checked against the one "
        "requested. The list endpoint caps at ten results a page, so a historical "
        "backfill is a long, polite crawl sliced by year. Binding regulations "
        "issued by Finnish authorities (Traficom, Finanssivalvonta and others) "
        "under statutory powers. ",
        (SourceOption("language", "Expression language", "fin (default) | swe"),
         SourceOption("keyword", "Finlex keyword", "searched in the API"),
         SourceOption("title_contains", "Title contains", "free text"),
         SourceOption("start_year", "Backfill from year", "e.g. 1979"),
         SourceOption("end_year", "Backfill to year", ""),
         SourceOption("include_swedish", "Also keep swe@ expressions", "true/false"),
         SourceOption("ids", "ECLIs, diary numbers or year/number",
                      "ECLI:FI:KKO:2024:1, S2022/290, 2024/1")),
        ("authority/year/number",),
    ),
    "se-domstol-bulk": SourceInfo(
        "se-domstol-bulk", "Sweden — withdrawn case law (archived Sök rättspraxis)",
        "caselaw", "SE", False,
        "The publications Domstolsverket has taken down. Read from a local parquet "
        "snapshot of its own REST service (nexoneAB/swedish-legal-decisions-raw-v1, "
        "17,228 publications; the README's 55,096 counts the same records rendered three "
        "ways and written twice each). This is not a faster route to the corpus — "
        "`se-domstol` reads the publisher's HTML and PDF and its text was comparable or "
        "longer in every record of a 400-record comparison, never shorter — so `mode` "
        "defaults to `withdrawn`: walk the live service and import only what it can no "
        "longer supply. That is 41 publications, 40 of them prövningstillstånd notices "
        "from Högsta domstolen and Högsta förvaltningsdomstolen granted between October "
        "2024 and March 2026. A leave-to-appeal notice states the question the court "
        "agreed to hear and is taken down once the court has answered it, so it is "
        "evidence that stops existing exactly when it becomes citable. (The other 391 "
        "unlisted ids are judgments the paged list merely hides; `se-domstol` reaches "
        "those by expanding the publication group, and they are not imported here.) "
        "Deciding what is withdrawn costs about 600 requests, not 17,228: the paged list "
        "settles the rest in 174. If the service is unreachable, `withdrawn` mode raises "
        "rather than importing everything — a network failure and a withdrawal must not "
        "produce the same import. `mode=all` is for a cold start with no network and "
        "should not be run against a corpus already harvested live.",
        (SourceOption("path", "Local corpus directory", "/corpora/se"),
         SourceOption("mode", "What to import",
                      "withdrawn (default) — only what the live service no longer "
                      "serves | all"),
         SourceOption("verify_withdrawn", "Probe each unlisted id before importing",
                      "true (default) — false trusts the paged list alone"),
         SourceOption("ids", "Publication UUIDs", "544e30bb-0378-4499-b442-f8268ade966f")),
        ("record UUID", "målnummer (T 2067-25)"),
    ),
    "se-domstol": SourceInfo(
        "se-domstol", "Sweden — superior courts (Sök rättspraxis)", "caselaw", "SE", True,
        "Domstolsverket's own case-law service: 17,321 publications from the "
        "superior courts, selected for what guides other courts. Case reports run "
        "from 1981; full judgments only from 3 March 2025, so this is a precedent "
        "layer rather than a corpus. Three fields do work that would otherwise be "
        "inferred: typ, a native precedential-weight taxonomy (prejudikat / guiding "
        "but not precedent-setting / not guiding); lagrumLista, statutory citations "
        "already parsed with the SFS number split out; and "
        "hanvisadePubliceringarLista, the authorities cited — including CJEU "
        "judgments whose ECLIs are printed in the free text, which is a clean join "
        "into the EU corpus. It also publishes the Supreme Court's own quoted case "
        "names ('Sökordslistan', 'Pärmen'), which is what Swedish practitioners "
        "actually cite by and which no other source carries. Sweden mints no ECLI, "
        "so identity is the service's record id with the report citation (NJA 2020 "
        "s. 123) and the målnummer registered as aliases. Page is zero-based; the "
        "sort parameters are accepted but do not order the paged result set, so "
        "they are never sent and the watch is a full walk of 174 pages. ",
        (SourceOption("court", "One court", "domstolKod, e.g. HDO, HFD, MMOD, ADO"),
         SourceOption("weight", "Precedential weight",
                      "PREJUDIKAT | VAGLEDANDE_MEN_EJ_PREJUDICERANDE | EJ_VAGLEDANDE | "
                      "PROVNINGSTILLSTAND"),
         SourceOption("publication_form", "Publication form",
                      "DOM_ELLER_BESLUT | REFERAT | NOTIS"),
         SourceOption("case_number", "Målnummer", "e.g. Ö 4337-25"),
         SourceOption("include_documents", "Download the PDF", "true/false"),
         SourceOption("expand_groups", "Also fetch the group members the list hides",
                      "true (default) — reaches 391 judgments no page returns"),
         SourceOption("start_page", "Resume at page", "0-based"),
         SourceOption("ids", "Record UUIDs or målnummer", "Ö 4337-25")),
        ("NJA 2020 s. 123", "målnummer (Ö 4337-25)", "record UUID"),
    ),
    "ee-lahend": SourceInfo(
        "ee-lahend", "Estonia — court decisions (lahend.ee)", "caselaw", "EE", True,
        "Estonia publishes its court decisions in Riigi Teataja under a statutory "
        "duty but offers no API or bulk download for them. lahend.ee, a non-profit "
        "open-data service, has extracted 3.05 million citations from 346,000 "
        "decisions and joined them to the Riigi Teataja statute book and to "
        "EUR-Lex, and exposes the result free through a Model Context Protocol "
        "endpoint — which is the only public interface there is. Per decision it "
        "returns the full text as Markdown, the provisions relied on resolved to an "
        "act abbreviation and a § with its lõiked, and the EU instruments cited as "
        "CELEX numbers with articles: an Estonian judgment therefore joins the EU "
        "corpus without a grammar pass. Coverage is a statutory SELECTION, not a "
        "census — the judgment must have entered into force and must carry no "
        "sensitive personal data — and natural persons are anonymised by the court "
        "while company names are not. ",
        (SourceOption("query", "Full-text query", "searched at lahend.ee"),
         SourceOption("court", "Exact court name",
                      "e.g. Riigikohus, Harju Maakohus Tallinna kohtumaja"),
         SourceOption("case_type", "Case type", "civil | administrative | criminal"),
         SourceOption("category", "Area of law", "e.g. Võlaõigus"),
         SourceOption("start_date", "Decided from", "YYYY-MM-DD"),
         SourceOption("end_date", "Decided to", "YYYY-MM-DD"),
         SourceOption("include_citations", "Fetch the citation graph per decision",
                      "true (default) — statutory and EU-law edges"),
         SourceOption("lookback_days", "Keep-current window", "default 90"),
         SourceOption("ids", "Case numbers or lahend ids", "3-25-3458/5, ruling:1589986")),
        ("case number (3-25-3458/5)", "lahend ruling id"),
    ),
    "de-openlegaldata": SourceInfo(
        "de-openlegaldata", "Germany — Länder + federal case law (Open Legal Data)",
        "caselaw", "DE", False,
        "The Länder case law the federal portals do not publish: 424k decisions from 918 "
        "courts — Oberverwaltungs-, Landes-, Verwaltungs-, Landesarbeits- and "
        "Landessozialgerichte down to the Amtsgerichte — from openlegaldata.io. Point "
        "`path` at the parquet bulk dump (HuggingFace openlegaldata/court-decisions-"
        "germany) to seed offline; leave it blank to run the REST API newest-first as a "
        "watch. Keyed by ECLI where one exists, so decisions already held from de-rii / "
        "de-neuris dedup instead of duplicating; the rest key on their slug and mint the "
        "court+Aktenzeichen alias a German citation resolves through. The upstream law "
        "markers (§ + book) arrive as structured edges. Luxembourg decisions the register "
        "mirrors are skipped — the corpus holds those from CELLAR (`include_eu` opts in).",
        (SourceOption("path", "Local parquet dump", "/data/corpora/de-openlegaldata/dump-20260520"),
         SourceOption("ids", "Case ids / slugs / ECLIs", "521203,ECLI:DE:VGK:2025:0617.1L1930.22.00"),
         SourceOption("courts", "Limit to court slugs", "ovgnrw,vg-koln,lg-bonn"),
         SourceOption("min_year", "Earliest decision year", "2000"),
         SourceOption("include_eu", "Include mirrored EU decisions", "false (default)"),
         SourceOption("start_offset", "Resume listing offset", "0")),
        ("ECLI:DE:…", "de/openlegaldata/<slug>", "openlegaldata case id"),
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
    "fr-dila-legi": SourceInfo(
        "fr-dila-legi", "DILA LEGI legislation bulk (France)", "legislation", "FR", False,
        "Offline DILA LEGI archive using the same identifiers as the live Légifrance adapter.",
        (SourceOption("path", "Path to LEGI archive", "/data/corpora/dila/LEGI"),),
        ("LEGIARTI id", "LEGITEXT id"),
    ),
    "fr-dila-jade": SourceInfo(
        "fr-dila-jade", "DILA JADE administrative case-law bulk (France)", "caselaw", "FR", False,
        "Offline DILA JADE archive of French administrative case law.",
        (SourceOption("path", "Path to JADE archive", "/data/corpora/dila/JADE"),),
        ("ECLI:FR:…", "Légifrance JADE id"),
    ),
    "fr-dila-constit": SourceInfo(
        "fr-dila-constit", "DILA constitutional decisions bulk (France)", "caselaw", "FR", False,
        "Offline DILA CONSTIT archive of Conseil constitutionnel decisions.",
        (SourceOption("path", "Path to CONSTIT archive", "/data/corpora/dila/CONSTIT"),),
        ("ECLI:FR:CC:…",),
    ),
    "fr-dila-cnil": SourceInfo(
        "fr-dila-cnil", "DILA CNIL decisions bulk (France)", "administrative", "FR", False,
        "Offline DILA CNIL archive of data-protection authority decisions.",
        (SourceOption("path", "Path to CNIL archive", "/data/corpora/dila/CNIL"),),
        ("CNIL decision id",),
    ),
    "sg-sl": SourceInfo(
        "sg-sl", "Singapore subsidiary legislation", "legislation", "SG", False,
        "Subsidiary legislation from Singapore Statutes Online, using the same structured "
        "provision model as the primary-legislation adapter.",
        (), ("Singapore subsidiary-legislation id",),
    ),
    "uk-hol": SourceInfo(
        "uk-hol", "House of Lords judgments archive", "caselaw", "GB", False,
        "Closed official judgments archive from publications.parliament.uk (1996–2009).",
        (), ("neutral citation ([2008] UKHL 1)",),
    ),
    "uk-ico": SourceInfo(
        "uk-ico", "Example scrape recipe (unverified template)", "scrape", "GB", False,
        "A built-in illustration of the recipe abstraction, pointed at an ICO listing "
        "page whose selectors were never verified against the live DOM. Superseded for "
        "real work by the uk-ico-* sources below; kept only as a worked example of a "
        "user-supplied recipe.",
        (), ("listing page URL",),
    ),
    "uk-ico-enforcement": SourceInfo(
        "uk-ico-enforcement", "ICO enforcement register", "administrative", "GB", False,
        "The Information Commissioner's enforcement action register: enforcement "
        "notices, monetary penalty notices, reprimands, prosecutions, undertakings and "
        "assessment notices. The HTML page is a summary — the notice itself is a PDF, "
        "which is downloaded, text-extracted (OCR'd if it is a scan) and inlined so the "
        "whole action is one searchable record. Each item carries the ICO's own type "
        "and sector facets, and the instruments it turns on: an interprets edge and a "
        "topic tag per regime (PECR, the UK GDPR, the DPA 2018, FOIA, the EIR…), plus a "
        "declared governing instrument where one dominates, so later bare pinpoints "
        "(\"regulation 21(1)(b)\", \"Article 5(1)(f)\") return to the right law. "
        "Discovery walks the register's JSON listing every run and re-fetches only the "
        "items whose CMS revision stamp moved.",
        (SourceOption("min_interval", "Seconds between requests", "2.0 (robots says 6)"),),
        ("ICO enforcement item URL", "organisation name"),
    ),
    "uk-ico-audits": SourceInfo(
        "uk-ico-audits", "ICO audits and overview reports", "guidance", "GB", False,
        "Consensual audits, follow-up audits and sector overview reports published "
        "under \"Action we've taken\". Each item's executive-summary PDF is inlined; "
        "the audit type and any named regime are recorded as facets.",
        (SourceOption("min_interval", "Seconds between requests", "2.0"),),
        ("ICO audit page URL", "audited organisation"),
    ),
    "uk-ico-consultations": SourceInfo(
        "uk-ico-consultations", "ICO consultations and consultation responses",
        "guidance", "GB", False,
        "Both consultation registers in one source: the Commissioner's responses to "
        "other bodies' consultations and calls for evidence (DSIT, Ofcom, the Home "
        "Office, Senedd Cymru…), and the ICO's own and stakeholder consultations on "
        "draft guidance. Opening/closing dates and the open/closed status are kept as "
        "fields; the response or draft-guidance PDF is inlined. Welsh-language twins of "
        "an English document are skipped.",
        (SourceOption("min_interval", "Seconds between requests", "2.0"),),
        ("ICO consultation page URL", "consultation title"),
    ),
    "uk-ico-guidance": SourceInfo(
        "uk-ico-guidance", "ICO guidance and research library", "guidance", "GB", False,
        "The ICO's guidance corpus from its own sitemap: /for-organisations/ (the UK "
        "GDPR guide, FOI and EIR guidance, direct marketing and PECR, law enforcement "
        "processing, NIS, eIDAS), /for-the-public/, and the research, impact and "
        "evaluation library — whose reports' PDFs are followed and inlined. Guides are "
        "trees of short section pages, so each page is titled by the guide it belongs "
        "to (\"A guide to lawful basis — Consent\"). The sitemap's lastmod is both the "
        "cursor and the change signal, so an unrevised page is never re-downloaded.",
        (SourceOption("sections", "Subtrees to harvest",
                      "for-organisations,for-the-public,"
                      "about-the-ico/research-reports-impact-and-evaluation"),
         SourceOption("min_interval", "Seconds between requests", "2.0")),
        ("ICO guidance page URL", "guidance title"),
    ),
}


# Sources that support forward-citation discovery (find NEW documents that cite a target,
# via the live source) — the renewing kind of watch. uk-caselaw uses Find Case Law's
# full-text search; eu-cellar walks CELLAR's citation graph.
DISCOVER_CITING_SOURCES = frozenset({"uk-caselaw", "uk-grc", "eu-cellar"})
# Sources whose ids are sequential neutral citations, so a court/year can be gap-scanned.
GAP_SCAN_SOURCES = frozenset({"uk-caselaw"})

# How each source keeps current (see raglex design docs/backfill-keepcurrent-audit.md):
#   server     — the API filters by ``since`` (modified>=, date_start=, publishedAfter=);
#                only new rows cross the wire. Ideal.
#   early-stop — a newest-first feed; the crawl BREAKS at the first item <= cursor, so a
#                routine run reads ~1 page. Efficient.
#   full-walk  — must read the whole index/listing each run (no server date filter, no
#                early break), then filter past the cursor. Correct but re-walks everything.
#   targeted   — fetches by id only; NO discovery crawl (can't find "new" on its own).
#   bulk       — a local-file seed; no live path (usually has a live sibling).
#   closed     — a closed archive; no new items ever exist.
# Unlisted caselaw sources default to early-stop (a newest-first feed crawl).
INCREMENTAL_MODE: dict[str, str] = {
    # ---- the five European sources added in 2026-08 ------------------------
    # Austria: the RIS date filter is applied server-side and the run keeps whatever
    # Allgemein.Geaendert says has changed since the cursor, over a trailing window —
    # RIS publishes late and revises old documents, so a point cursor strands both.
    "at-justiz": "server", "at-vwgh": "server", "at-vfgh": "server",
    "at-bvwg": "server", "at-lvwg": "server", "at-dsb": "server",
    "at-gbk": "server", "at-verg": "server", "at-ris": "server",
    # Slovakia: indexDatumOd is the register's own indexing date — the only monotonic key
    # it has, since a 2018 judgment can be published in 2026.
    "sk-ress": "server",
    # Finland: publishedSince, which also reports NEW vs MODIFIED per document.
    "fi-kko": "server", "fi-kho": "server", "fi-hovioikeus": "server",
    "fi-hao": "server", "fi-mao": "server", "fi-tt": "server", "fi-vako": "server",
    "fi-tsv": "server", "fi-oka": "server", "fi-saadokset": "server",
    "fi-saadokset-ajantasa": "server", "fi-he": "server",
    "fi-viranomaismaaraykset": "server",
    # Sweden: neither the publication-date filter nor the sort parameters agree with the
    # paged result set, and publication lags the decision by up to twelve years — so the
    # watch walks all 174 pages and filters on publiceringstid itself.
    "se-domstol": "full-walk",
    "se-domstol-bulk": "bulk",
    # Estonia: search_rulings filters on the DECISION date and publication lags it, so a
    # watch re-walks a trailing window rather than cutting at the cursor.
    "ee-lahend": "server",
    # server-side incremental
    "us-caselaw": "server", "nl-rechtspraak": "server", "nl-legislation": "server",
    "de-neuris": "server", "de-neuris-legislation": "server", "fr-judilibre": "server", "fr-judilibre-ca": "server",
    "de-bt-drucksachen": "server",
    "nl-tk-reports": "server",
    "fr-judilibre-tj": "server",
    "fr-conseil-etat": "server", "fr-legislation": "server", "fr-cnil": "server",
    "fr-constit": "server", "ca-canlii": "server", "au-cth": "server",
    "eu-cellar": "server", "eu-legislation": "server",
    # CURIA can make an old filing public later, so a date cursor is unsafe. Six API
    # pages currently cover the complete public observations register.
    "eu-curia-observations": "full-walk",
    # client-side early-stop on a newest-first feed
    "uk-caselaw": "early-stop", "uk-grc": "early-stop", "uk-ftt-tax": "early-stop",
    "uk-utaac": "early-stop", "uk-iac": "early-stop", "uk-legislation": "early-stop",
    "uk-legislation-materials": "early-stop",
    "uk-cma": "early-stop", "uk-cma-guidance": "early-stop",
    "uk-ofgem": "early-stop", "uk-ofwat": "early-stop",
    # Ofgem's own listing sorts newest-first, but only APPROXIMATELY — a 28 July item
    # lands after a 21 July one — so the stop is three consecutive exhausted pages
    # rather than the first old item. The OfS listing is properly ordered.
    "uk-ofgem-publications": "early-stop", "uk-ofs": "early-stop",
    # One sitemap for the whole site, filtered on each entry's lastmod.
    "uk-ehrc": "full-walk",
    # newest-first Search API, same as the other GOV.UK feeds
    "uk-govuk-policy": "early-stop",
    "uk-fca-notices": "early-stop",
    "au-nsw-caselaw": "early-stop", "au-fca": "early-stop", "au-hca": "early-stop",
    "ca-scc-live": "early-stop", "ca-tcc-live": "early-stop",
    "ca-fc-live": "early-stop", "ca-fca-live": "early-stop",
    "ca-sst-live": "early-stop",
    "ie-caselaw": "early-stop", "ie-revised": "early-stop",
    "ie-tax-appeals": "early-stop", "nz-caselaw": "early-stop",
    "ie-revenue-tdm": "full-walk",
    "ie-ccpc-mergers": "full-walk",
    # Each month of the laid register is a server-side date filter, so an incremental
    # run asks for the current month and stops there.
    "ie-oireachtas": "server",
    # Committee pages carry no date filter and no paging — each run re-reads the
    # same recent rows and the pipeline skips what is already held.
    "ie-oireachtas-committees": "full-walk",
    # full-walk-then-filter (correct but re-reads the whole source each run)
    "edpb": "full-walk", "edpb-oss": "full-walk", "de-rii": "full-walk",
    "eu-consumer-guidance": "full-walk", "nl-acm-guidance": "full-walk",
    "nl-ap": "early-stop",
    "eu-berec": "early-stop", "dma-consultations": "early-stop",
    # Nine pages of JSON, not ordered by date, and a study's page is re-published
    # when a language version or a country note is added. Walk it all.
    "eu-euipo": "full-walk",
    "dma-annual-reports": "full-walk",
    "fr-cnil-guidance": "full-walk", "es-aepd-guias": "full-walk",
    "dk-datatilsynet": "full-walk", "de-dsk": "full-walk",
    "be-gba": "full-walk", "it-garante": "full-walk",
    # newest-first search view with its own result total, so an incremental
    # run stops at the cursor within a page or two
    "be-gba-decisions": "early-stop",
    # both filter server-side on a date and sort newest-first
    "uk-parl-committees": "server", "uk-parl-written-questions": "server",
    "fr-senat-reports": "early-stop", "fr-senat-lc": "full-walk",
    "fr-an-reports": "early-stop",
    # Both Library feeds are ordered newest-first by post date, so an incremental run
    # stops within a page or two. It is early-stop rather than server-side: the feed
    # takes no date parameter, only ?paged=N.
    "uk-commons-library": "early-stop", "uk-lords-library": "early-stop",
    "de-bt-wd": "early-stop",
    # The SPICe listing sorts newest-first and reports an authoritative result count.
    "scot-spice": "early-stop",
    # One sitemap / one page for the whole archive, then filtered — cheap, and there is
    # no newest-first order to stop at.
    "uk-ipco": "full-walk", "uk-isc": "full-walk",
    "it-agcm": "early-stop",
    "dma-cases": "full-walk", "ofcom-osa": "full-walk", "ofcom-enforcement": "full-walk",
    "eu-ombudsman": "full-walk",
    "eu-edps-opinions": "early-stop",
    "eu-edps-investigations": "early-stop",
    "eu-dgcomp-antitrust": "early-stop",
    "eu-esma-sanctions": "server",
    "eu-esas-boa": "early-stop",
    "eu-srb-appeals": "early-stop",
    "sg-legislation": "full-walk", "sg-sl": "full-walk", "ca-federal": "full-walk",
    "hk-legislation": "full-walk", "nz-legislation": "full-walk", "gdprhub": "full-walk",
    "de-gii": "full-walk", "eu-preparatory": "full-walk", "au-qld": "full-walk",
    # /api/cases/?ordering=-created_date is newest-first on the register's own ingest
    # date, so a watch stops within a page or two; the parquet bulk path is a local walk.
    "de-openlegaldata": "early-stop",
    # CELLAR enumeration is newest-first on work_date_document, so an incremental
    # run stops at its cursor; the Parliament’s external-documents register offers
    # neither a date filter nor a sort, so that one is honestly a full walk.
    "eu-ep-resolutions": "early-stop", "eu-ep-followups": "full-walk",
    # date-windowed newest-first, so an incremental run stops in the first
    # window or two; a backfill walks every month back to 1989.
    "eu-ep-thinktank": "early-stop",
    "au-tas": "full-walk", "au-vic": "full-walk", "au-sa": "server",
    "au-wa": "full-walk", "au-esafety-osa": "full-walk",
    "ie-legislation": "full-walk",
    "uk-ico": "full-walk",  # scrape recipe (ICO portal page)
    # The registers' JSON listing is ordered by the item's own DATE, not by its publish
    # stamp, so there is nothing to early-stop on — but it is cheap JSON, and only items
    # whose stamp moved are fetched. The guidance sitemap is likewise walked whole and
    # filtered on lastmod.
    "uk-ico-enforcement": "full-walk", "uk-ico-audits": "full-walk",
    "uk-ico-consultations": "full-walk", "uk-ico-guidance": "full-walk",
    "uk-cpr": "full-walk", "uk-cps-guidance": "full-walk",
    "uk-lawcom-reports": "full-walk",
    "uk-judiciary": "full-walk",   # a fingerprint check, not a feed
    "eu-digital-strategy": "early-stop",   # newest-first listing → stop at the cursor
    "uk-cat": "full-walk",
    "ie-dpc": "full-walk",
    "ie-dpc-guidance": "full-walk",
    # HUDOC's own feed is newest-first by kpdate, so the crawl breaks at the cursor.
    "echr": "early-stop",
    # targeted-only — no keep-current crawl (the audit's live-update GAPS)
    "au-nsw": "targeted",
    # bulk / local-file seeds (no live path)
    "au-caselaw": "bulk", "ca-caselaw": "bulk", "us-caselaw-bulk": "bulk",
    "in-caselaw": "bulk", "fr-dila": "bulk", "fr-dila-legi": "bulk",
    "fr-dila-jade": "bulk", "fr-dila-constit": "bulk", "fr-dila-cnil": "bulk",
    # closed archives (no new items ever)
    "a29wp": "closed", "uk-hol": "closed", "uk-ipa-codes": "closed",
    # the Information Rights register stopped taking new decisions in August 2023, when
    # the chamber moved to Find Case Law (uk-grc harvests it from there now)
    "uk-ftt-ir": "closed",
}


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
        jurisdiction = str(row.get("jurisdiction") or "")
        kind = str(row.get("kind") or "scrape")
        row["group_key"] = jurisdiction.casefold() or "other"
        row["group_label"] = JURISDICTION_LABELS.get(
            jurisdiction, jurisdiction or JURISDICTION_LABELS[""])
        row["kind_label"] = KIND_LABELS.get(kind, kind.replace("_", " ").title())
        row["sort_key"] = (
            row["group_label"].casefold(),
            row["kind_label"].casefold(),
            str(row.get("label") or key).casefold(),
            key,
        )
        # capability flags the UI turns into plain-language chips
        row["can_keyword_search"] = bool(row.get("keyword_search"))
        row["can_discover_citing"] = key in DISCOVER_CITING_SOURCES
        row["can_gap_scan"] = key in GAP_SCAN_SOURCES
        # incremental "check for new" makes sense for feed-like sources: the caselaw
        # feeds, UK legislation's newest-published search feed (feed=new), and the
        # EDPB sitemap/register cursors. The other legislation/by-id sources are
        # fetched by naming the item — no moving feed.
        mode = INCREMENTAL_MODE.get(key, "caselaw-default" if row.get("kind") == "caselaw" else "none")
        if mode == "caselaw-default":
            mode = "early-stop"  # an unlisted caselaw feed defaults to a newest-first crawl
        row["incremental_mode"] = mode
        # a source can be *polled for new* when its cursor actually narrows the crawl:
        # server-side, client early-stop, or a full-walk-then-filter. targeted/bulk/closed
        # cannot (no moving feed / by-id only / no new items ever exist).
        row["can_incremental"] = mode in ("server", "early-stop", "full-walk")
        out.append(row)
    return sorted(out, key=lambda row: tuple(row["sort_key"]))


def get_adapter(source_key: str, **kwargs) -> Adapter:
    try:
        factory = ADAPTERS[source_key]
    except KeyError:
        known = ", ".join(sorted(ADAPTERS))
        raise KeyError(f"unknown source {source_key!r}; known: {known}") from None
    return factory(**kwargs)
