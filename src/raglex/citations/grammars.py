"""Citation grammars — the extensibility foundation (§5).

Each grammar is a named (pattern + normaliser) that recognises one citation form
and produces a candidate id + pinpoint + entity kind. Coverage grows by
*registering grammars* — per jurisdiction, per instrument type — not by rewriting
the extractor, mirroring the plug-in discipline of format parsers (§formats), tag
rules (§4a), and embedding providers (§6d).

A normaliser returns ``(candidate_id, pinpoint, kind_override)``: the candidate is
the resolvable form (so the §5b resolver's "prefer supplied dst_id" path links it
once the target is harvested); the pinpoint becomes the edge ``dst_anchor`` (the
article/section the citation targets); kind_override lets one grammar classify by
content (a CELEX is a regulation *or* a case depending on its sector).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..core.registry import Registry
from .courts import COURTS_BY_CODE, DIVISIONS

# Resolvable candidate, pinpoint anchor, optional entity-kind override.
Normalised = tuple[str | None, str | None, str | None]

# Sentinel kind_override: "this match is not a citation at all — drop it entirely".
# A normaliser returns it (via the third tuple slot) so the extractor skips the match
# instead of recording a candidate-less "maybe" (see grammar_citations). Used to suppress
# the bracketless grammar's false positives on currency codes / ISBNs / structure words.
DROP = "\x00drop"


@dataclass(frozen=True, slots=True)
class Grammar:
    name: str
    entity_kind: str
    pattern: re.Pattern[str]
    normalize: Callable[[re.Match[str]], Normalised]


# The grammar registry — the extension surface (§core.registry).
GRAMMARS: Registry[Grammar] = Registry("citation grammar")


def register(grammar: Grammar) -> None:
    GRAMMARS.register(grammar.name, grammar)


# -- helpers ----------------------------------------------------------------
# CELEX sector-3 descriptors. A FRAMEWORK decision is "F", not "D" — the third-pillar
# instruments were numbered separately — so "Council Framework Decision 2008/977/JHA on
# the protection of personal data" is 32008F0977. Matching only the word "Decision" built
# 32008D0977, an id that does not exist: 659 citations resolved to an empty stub the
# bare-CELEX path then created, while the real act sat beside it holding three.
_DESCRIPTOR = {"regulation": "R", "directive": "L", "decision": "D",
               "framework decision": "F"}


def _eu_celex(kind: str, a: str, b: str) -> str | None:
    """Build a CELEX from an EU instrument number. A 4-digit group is the year
    ('2016/679', '45/2001'); for the old 2-digit forms ('Directive 95/46',
    'Regulation 1612/68') the convention differs by instrument — **directives put
    the year first**, regulations put it second — and a 2-digit year ≥31 is 19xx,
    else 20xx."""
    desc = _DESCRIPTOR.get(re.sub(r"\s+", " ", kind).strip().lower())
    if not desc:
        return None
    if re.fullmatch(r"(19|20)\d{2}", a):
        year, num = a, b
    elif re.fullmatch(r"(19|20)\d{2}", b):
        year, num = b, a
    else:
        # Old two-digit-year form. The *year* is the two-digit group, whichever side it's
        # on — "94/800" (Decision, year first) and "1831/81" (number first) both put the
        # 2-digit group in the year slot. Only when BOTH groups are two digits ("95/46") is
        # it ambiguous, and there the per-instrument convention decides: directives and
        # decisions are numbered year/number, regulations number/year.
        if re.fullmatch(r"\d\d", a) and not re.fullmatch(r"\d\d", b):
            yy, num = a, b
        elif re.fullmatch(r"\d\d", b) and not re.fullmatch(r"\d\d", a):
            yy, num = b, a
        elif kind.lower() in ("directive", "decision"):
            yy, num = a, b
        else:  # regulation
            yy, num = b, a
        if not re.fullmatch(r"\d\d", yy):
            return None
        year = ("19" if int(yy) >= 31 else "20") + f"{int(yy):02d}"
    if not re.fullmatch(r"(19|20)\d{2}", year):
        return None
    return f"3{year}{desc}{int(num):04d}"


# GDPR and its multilingual short names → its CELEX.
#
# This map is what lets the Commission's own drafting form resolve. EU guidance cites
# an instrument by short name with the pinpoint in FRONT and no "of the" —
# "Article 50(2) AI Act", "Article 34 DSA" — which the ``eu_named``/``eu_named_full``
# grammars below match directly. A name that is MISSING here doesn't merely fail to
# resolve: the bare "Article 50(2)" falls through to the carry-forward pass and is
# attached to whichever instrument was named most recently, which in a Commission
# opinion is usually a cross-reference to some other regulation. (Before the AI Act
# was added, the Commission's Opinion on the Code of Practice on transparency of
# AI-generated content had 31 of its Articles attributed to the DSA and one to the
# AI Act.) So: when the corpus takes on a body of guidance about an instrument, the
# instrument's short name belongs here.
_NAME_TO_CELEX = {
    "gdpr": "32016R0679", "avg": "32016R0679", "dsgvo": "32016R0679", "rgpd": "32016R0679",
    # the digital-regulation instruments, cited by acronym or full name in guidance/cases
    "dma": "32022R1925", "digital markets act": "32022R1925",
    "dsa": "32022R2065", "digital services act": "32022R2065",
    "e-privacy directive": "32002L0058", "eprivacy directive": "32002L0058",
    # NB: "ePrivacy Regulation" is deliberately NOT mapped — it refers to the
    # (still-withdrawn) proposal, not Directive 2002/58, so mapping it here would
    # mint a confidently wrong edge to the existing Directive.
    "law enforcement directive": "32016L0680", "led": "32016L0680",
    # ── the 2022-2024 digital acquis ────────────────────────────────────────
    # The substance of the Commission's digital-strategy library and of the AI
    # Office's opinions, codes of practice and guidelines.
    "ai act": "32024R1689", "artificial intelligence act": "32024R1689",
    "data act": "32023R2854",
    "data governance act": "32022R0868",
    "nis2": "32022L2555", "nis 2": "32022L2555", "nis2 directive": "32022L2555",
    "nis 2 directive": "32022L2555",
    "emfa": "32024R1083", "european media freedom act": "32024R1083",
    "cyber resilience act": "32024R2847",
    "chips act": "32023R1781",
    "european accessibility act": "32019L0882",
    "open data directive": "32019L1024",
    "dsm directive": "32019L0790", "copyright directive": "32019L0790",
    "eecc": "32018L1972", "european electronic communications code": "32018L1972",
    "avmsd": "32010L0013",
    "interoperable europe act": "32024R0903",
    "e-commerce directive": "32000L0031", "ecommerce directive": "32000L0031",
}
# Acronyms are matched UPPERCASE-only (case-sensitive) so the common word "led" never
# resolves to the Law Enforcement Directive; the spelled-out names match case-
# insensitively (a separate pattern). Both look up through ``_name_to_celex``.
#
# "AI Act" is deliberately NOT in this uppercase-only list — it is written in mixed
# case ("AI Act", never "AI ACT" outside a heading) and is matched by the full-name
# pattern instead, which is case-insensitive and requires the whole two-word phrase.
# A bare uppercase "AIA"/"CRA"/"DGA"/"EAA" is likewise left out: those collide with
# ordinary initialisms in other corpora (a Canadian "CRA" is the Canada Revenue
# Agency), and the spelled-out names below carry them.
_EU_ACRONYMS = r"GDPR|AVG|DSGVO|RGPD|DMA|DSA|LED|NIS2|EMFA|AVMSD|EECC"
_EU_FULL_NAMES = "|".join(
    re.escape(k).replace(r"\ ", r"\s+")
    for k in sorted(_NAME_TO_CELEX, key=len, reverse=True) if " " in k)


def _name_to_celex(name: str) -> str | None:
    return _NAME_TO_CELEX.get(re.sub(r"\s+", " ", name).strip().lower())

# UK statute short names → legislation.gov.uk id (for "section N of the X Act").
_UK_ACT_TO_ID = {
    "freedom of information act 2000": "ukpga/2000/36",
    "foia": "ukpga/2000/36",
    "data protection act 2018": "ukpga/2018/12",
    "dpa 2018": "ukpga/2018/12",
    "human rights act 1998": "ukpga/1998/42",
    # the Online Safety Act 2023 — Ofcom's online-safety guidance implements its
    # sections/parts, so precise "section N of the Online Safety Act 2023" pinpoints
    # link the guidance to the exact provision (both directions, via the graph).
    "online safety act 2023": "ukpga/2023/50",
    "online safety act": "ukpga/2023/50",
    # The surveillance statutes. The full names are unambiguous anywhere; the acronyms
    # (RIPA, IPA) are not, and are expanded only inside the sources where they always
    # mean these — see citations.stage._SOURCE_ALIASES.
    "investigatory powers act 2016": "ukpga/2016/25",
    "investigatory powers act": "ukpga/2016/25",
    "regulation of investigatory powers act 2000": "ukpga/2000/23",
}

# Statutory instruments cited BY NAME. They need their own map, and their own grammar,
# for two reasons that the Act machinery cannot cover:
#
#  * ``uk_statute_named`` matches "<Title> Act|Measure <year>" and the vendored gazetteer
#    holds Acts — so an instrument called "… Regulations 2003" is invisible to both. The
#    only route to PECR was ``uk_si_named``, which requires the SERIES NUMBER to be
#    written out ("…Regulations 2003, SI 2003/2426"). A judgment that says "the Privacy
#    and Electronic Communications (EC Directive) Regulations 2003", or just "PECR",
#    resolved to nothing at all — which is why an instrument at the centre of UK
#    e-privacy law held 91 edges.
#  * an SI is pinpointed by REGULATION, not section, so the pinpoint this grammar emits
#    has to be "reg. 6" to meet the segment labels.
_UK_SI_TO_ID = {
    "pecr": "uksi/2003/2426",
    "privacy and electronic communications (ec directive) regulations 2003": "uksi/2003/2426",
    "privacy and electronic communications regulations 2003": "uksi/2003/2426",
    "privacy and electronic communications regulations": "uksi/2003/2426",
    # The Electronic Commerce (EC Directive) Regulations 2002 — the UK transposition of
    # the e-Commerce Directive, and the hosting/mere-conduit defences litigated with it.
    # NB no acronym: "ECR" is the European Court Reports and would collide catastrophically.
    "electronic commerce (ec directive) regulations 2002": "uksi/2002/2013",
    "electronic commerce (ec directive) regulations": "uksi/2002/2013",
    "electronic commerce regulations 2002": "uksi/2002/2013",
    "e-commerce regulations 2002": "uksi/2002/2013",
}
# The bare acronym is matched case-SENSITIVELY (uppercase only), the spelled-out names
# case-insensitively — the same discipline the EU acronyms use, so a lower-case "pecr"
# inside a word can never mint an edge.
_UK_SI_ACRONYMS = r"PECR"
_UK_SI_FULL_NAMES = "|".join(
    re.escape(k).replace(r"\ ", r"\s+")
    for k in sorted(_UK_SI_TO_ID, key=len, reverse=True) if " " in k)


# The many dash characters a PDF can encode a hyphen as — hyphen-minus, the Unicode
# hyphen/non-breaking hyphen, figure/en/em dash, horizontal bar, minus sign. CJEU case
# numbers ("C-311/18") and ECLIs come through PDFs with any of these.
_DASH = r"[-‐‑‒–—―−]"


def _ecli(m: "re.Match[str]") -> Normalised:
    v = m.group(0).upper()
    if not v.startswith("ECLI:"):  # bare EU:C:2020:559 (PDF-stripped prefix / OSCOLA) → full ECLI
        v = "ECLI:" + v
    return v, None, None


# -- grammars ---------------------------------------------------------------
# Full ECLI (any country) OR a bare ECLI without the "ECLI:" prefix — the latter
# turns up when a PDF drops the prefix or a citation style writes "EU:C:2020:559".
# The bare forms are an allow-list (EU:C/T/F, and CE:ECHR for Strasbourg) so the
# pattern can't swallow arbitrary "XX:YY:…" text. CE:ECHR is how CJEU opinions cite
# the ECtHR — "K.U. v. Finland (CE:ECHR:2008:1202JUD000287202, § 48)" — and the
# corpus already holds those judgments under exactly that ECLI, so recognising the
# bare form links them straight through instead of leaving a dangling reference.
register(Grammar(
    "ecli", "case",
    re.compile(
        r"(?:ECLI:[A-Z]{2}:[A-Z0-9]+|(?<![A-Za-z])(?:EU:[CTF]|CE:ECHR))"
        r":\d{4}:[A-Z0-9]+(?:[._-][A-Z0-9]+)*",
        re.IGNORECASE,
    ),
    _ecli,
))


def _irish_tacd(m: "re.Match[str]") -> Normalised:
    return f"tacd/{m.group('year')}/{int(m.group('num'))}", None, "case"


# The Irish Tax Appeals Commission prints and cites its determinations compactly:
# ``79TACD2026`` (sequence, TACD, year), not in the year-first neutral-cite shape.
register(Grammar(
    "irish_tacd", "case",
    re.compile(
        r"\b(?P<num>\d{1,5})\s*TACD\s*(?P<year>(?:20)\d{2})\b",
        re.IGNORECASE,
    ),
    _irish_tacd,
))

# -- neutral citations (common-law systems) ---------------------------------
# Detect the *shape* generically — for known AND unknown courts — so an unknown
# court token is still captured as a hanging edge and surfaces in the snowball
# (citations.snowball). The candidate is a normalised slug court[/div]/year/num.
_DIV_RE = "|".join(sorted(DIVISIONS, key=len, reverse=True))


# Law-report series abbreviations ([2023] 1 WLR 1327, [2022] AACR 4) — these look
# like neutral citations but the "court" token is a *report series*, not a court, so
# they must NOT mint a (wrong) neutral-citation candidate. They stay candidate-less
# "maybe" citations: recognised as a case reference, resolvable only by a lookup.
REPORT_SERIES = {
    "WLR", "AC", "QB", "KB", "CH", "FAM", "AACR", "ICR", "IRLR", "ECR", "CMLR",
    "BCLC", "FSR", "RPC", "FLR", "HRLR", "UKHRR", "EHRR", "LGR", "STC", "ER",
    "PIQR", "BMLR", "EMLR", "ENTLR", "INLR", "ACD", "COD", "WLUK", "NI",  # NI Law Reports
    "EHRLR", "EHRC", "CHRLD",  # European Human Rights Law Review etc.
    # further report/journal series the bracketless grammar was minting as fake courts —
    # they have no neutral-citation URI, so they stay candidate-less "maybe" citations
    "NJ",       # Nederlandse Jurisprudentie (Dutch law reports)
    "CLC",      # Commercial Law Cases
    "ETMR",     # European Trade Mark Reports
    "ILPR",     # International Litigation Procedure Reports
    "LMCLQ",    # Lloyd's Maritime & Commercial Law Quarterly
    "ECDR",     # European Copyright & Design Reports
    "ECC",      # European Commercial Cases
    "PLCR",     # Planning Law Case Reports
    "COPLR",    # Court of Protection Law Reports
    "EPOR",     # European Patent Office Reports
    "LCR",      # Licensing Case Reports
    "CLJ",      # Cambridge Law Journal
    "EULR",     # European Law Review
    "NE",       # North Eastern Reporter
    "JLR",      # Jersey Law Reports
    "BNB",      # Beslissingen Nederlandse Belastingrechtspraak (Dutch tax reports)
    "SLT",      # Scots Law Times
}

# Statute short-title abbreviations that the *bracketless* grammar ("2009 CTA 2010")
# wrongly grabs as a court token — they're tax/other Acts referenced by a year, not
# neutral citations. Listing them keeps "2009 CTA 2010" from minting a fake cta/2009/2010
# candidate. (Mostly the Tax Law Rewrite abbreviations.)
STATUTE_ABBREVS = {
    "CTA", "ITEPA", "ITTOIA", "TCGA", "TMA", "ITA", "VATA", "VERA", "TPDA", "FA",
    "ICTA", "CAA", "IHTA", "TIOPA", "FA2", "CRCA", "CEMA", "OTA", "TCTA",
}

# Tokens the *bracketless* grammar ("YEAR TOKEN NUMBER") grabs as a "court" that are not
# citations of anything — the year/token/number shape also spells out a monetary amount
# ("2000 NLG 25", "1987 USD 534"), an ISBN, a Westlaw locator, an EU Official Journal
# reference, or a document-structure word ("2005 … PART 3", "2012 … FINAL 12"). Left alone
# they mint a bogus resolvable slug (and "EUR …" is even mis-classified as UK assimilated
# EU law), so the normaliser DROPs them: recognised as noise, recorded as nothing.
NON_CITATION_TOKENS = {
    # ISO-4217 currency codes (amounts of money, not courts)
    "USD", "GBP", "EUR", "CHF", "DM", "DEM", "NLG", "HFL", "BFR", "DGB", "ITL",
    "FRF", "ESP", "NOK", "SEK", "DKK", "JPY", "ATS", "IEP", "LUF", "PTE", "GRD", "FIM",
    "CAD", "AUD",
    # publication / locator references (not case reports)
    "OJ", "OJL", "WL", "ISBN", "ISO", "SI", "SR",
    # document-structure / OCR junk swept up by "YEAR WORD NUMBER"
    "PART", "FINAL", "TOTAL", "FORM", "TABLE", "AND", "NO", "THE", "OF", "EN", "CY", "TO",
    # government-agency initialisms the bracketless grammar grabs as a "court"
    # ("In 2019 HMRC 5 assessments" → hmrc/2019/5) — parties/bodies, not courts.
    # (Kept to UK-only bodies; e.g. "FCA" is left out — it's a live neutral-citation
    # court token for the Federal Court of Australia.)
    "HMRC", "DWP", "ICO", "OFT",
}

# OCR / typo court codes → the canonical Find Case Law code, so the minted slug is
# harvestable (the corpus contains "EWCH" for "EWHC", producing 404-ing ewch/… slugs).
_COURT_ALIASES = {"EWCH": "EWHC"}


# Devolved-legislation series that the year-first shape sweeps up as "courts":
# "2000 ASP 1" is the Public Finance and Accountability (Scotland) Act 2000, not
# a case. The number IS the legislation.gov.uk slug, so mint it as an act.
_LEGISLATION_SERIES = {"ASP": "asp", "ANAW": "anaw", "ASC": "asc", "NIA": "nia"}


def _neutral(m: "re.Match[str]", *, court_override: str | None = None) -> Normalised:
    court = court_override or m.group("court")
    cu = court.upper()
    if cu in NON_CITATION_TOKENS:
        return None, None, DROP  # currency / ISBN / locator / structure word — not a citation
    if cu in _LEGISLATION_SERIES:  # "2000 ASP 1" → asp/2000/1, an act not a case
        return f"{_LEGISLATION_SERIES[cu]}/{m.group('year')}/{int(m.group('num'))}", None, "act"
    if cu in REPORT_SERIES or cu in STATUTE_ABBREVS:
        return None, None, "case"  # a report series / statute abbrev, not a court
    court = _COURT_ALIASES.get(cu, court)  # normalise typo'd court codes before minting the slug
    parts = [court.lower()]
    # The division/chamber becomes a path segment in the Find Case Law URI
    # (ewca/civ/…, ukut/aac/…). It appears EITHER before the number ("EWCA Civ 1")
    # OR after it in parentheses ("UKUT 440 (AAC)", "EWHC 22 (Admin)"); take
    # whichever is present so the candidate matches the canonical id.
    g = m.groupdict()
    seg = g.get("div") or g.get("chamber")
    if seg:
        parts.append(seg.lower())
    parts += [m.group("year"), m.group("num")]
    return "/".join(parts), None, "case"


def _neutral_bracketless(m: "re.Match[str]") -> Normalised:
    """Canada's two official-language codes identify one decision.

    Only apply this mapping to the bare year-first grammar.  Bracket style is the
    jurisdiction discriminator for colliding Commonwealth codes (notably FCA), and a
    bracketed token must retain its ordinary meaning.
    """
    from .courts import CANADIAN_FRENCH_COURT_EQUIVALENTS

    court = m.group("court")
    canonical = CANADIAN_FRENCH_COURT_EQUIVALENTS.get(court.upper(), court)
    return _neutral(m, court_override=canonical)


# Bracketed form: "[2024] UKSC 12", "[2024] EWCA Civ 1", "[2012] UKUT 440 (AAC)",
# "[2024] EWHC 22 (Admin)". The trailing parenthetical chamber/division is folded
# into the slug (so it resolves to ukut/aac/2012/440, not a 404 on ukut/2012/440).
register(Grammar(
    "neutral_citation", "case",
    re.compile(
        rf"\[(?P<year>(?:19|20)\d{{2}})\]\s+(?P<court>[A-Z][A-Za-z]{{1,9}})"
        rf"(?:\s+(?P<div>{_DIV_RE}))?\s+(?P<num>\d+)"
        rf"(?:\s+\((?P<chamber>[A-Za-z]{{2,12}})\))?"
    ),
    _neutral,
))

# Classic law reports live in citations/reporters.py — an exhaustive, punctuation-tolerant
# set of series and three structural shapes (modern "[1982] AC 1", English Reports
# "150 ER 1030", old Law Reports "(1868) LR 3 HL 330"). Registered near the bottom of this
# module, after the neutral-citation grammar so a genuine neutral citation wins any overlap.

# An ECtHR case cited by name + EHRR ("Osman v UK (2000) 29 EHRR 245"). HUDOC has no
# EHRR-number index, but it DOES index the case name (docname), so we capture the
# "X v <Respondent>" name as the candidate and resolve it via a HUDOC name search (an
# inferred, name-based match → routed to the echr adapter). The captured name is also
# what tags an otherwise-bare EHRR citation as ECHR.
_ECHR_CASE_NAME = (
    # applicant-name run bounded {0,7}: unbounded, it has the same failure mode as the
    # statute-title grammar — every capitalised word starts a scan across any long run of
    # capitalised tokens hunting for a " v " that never comes (tables of names, headings).
    r"(?P<name>[A-Z][A-Za-z.'’-]+(?:\s+(?:and\s+Others|and\s+[A-Z][A-Za-z.'’-]+|"
    r"[A-Z][A-Za-z.'’-]+)){0,7}?\s+v\.?\s+(?:the\s+)?[A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){0,3})"
)


def _echr_named(m: "re.Match[str]") -> Normalised:
    name = (m.groupdict().get("name") or "").strip().rstrip(",")
    name = re.sub(r"\s+", " ", name) if name else None
    # prefix marks it as a HUDOC-name candidate, so the worklist routes it to the echr
    # adapter (docname search) even though the dst_id is a free-text name, not an id.
    return (f"echr:{name}" if name else None), None, "echr_case"


register(Grammar(
    "echr_report", "echr_case",
    re.compile(rf"{_ECHR_CASE_NAME}\s*,?\s*\((?:19|20)\d{{2}}(?:-\d{{2}})?\)\s+\d+\s+EHRR\s+\d+"),
    _echr_named,
))

# ECHR application number — the resolvable key for a Strasbourg case. Many surface forms:
# "no. 4451/70", "Application no. 5493/72", "App no 47940/99" (OSCOLA, no full stop),
# "App. No. 60561/14" (Bluebook), "nos. 16064/90 and 2 others", "(dec.) [GC], no. 36022/97",
# "no. 3/02" (short). Resilience: the year is ALWAYS two digits (4451/**70**), so requiring
# ``/\d\d`` (not more) cleanly excludes EU instruments cited "No 1/2003" / "No 17/62"; the
# negative look-behinds drop "Regulation/Directive/Decision No …". Captures the FIRST number
# of a joined set — enough to resolve the case via HUDOC. → echr adapter.
# An EU instrument reference immediately before "No <n>/<yy>" — the lookbehinds above
# can't see past the parenthetical treaty tag ("Regulation (EEC) No 1408/71",
# "Directive (EU) 2016/680 …"), which minted famous regulations as bogus ECHR appnos
# (1408/71 was one of the most-cited "applications" in the corpus).
_EU_INSTRUMENT_BEFORE = re.compile(
    r"(?:regulation|directive|decision|protocol)s?\s*(?:\([A-Za-z]{2,8}(?:,?\s*[A-Za-z]+)?\)\s*)?$",
    re.IGNORECASE,
)


def _echr_appno(m: "re.Match[str]") -> Normalised:
    before = m.string[max(0, m.start() - 40): m.start()]
    if _EU_INSTRUMENT_BEFORE.search(before):
        return None, None, DROP  # "…Regulation (EEC) No 1408/71" — an EU instrument, not an appno
    # "Case No 9/70" is a pre-1989 CJEU case number, not a Strasbourg application
    # number — the cjeu grammar owns it (and a 2-digit "year" like /70 is a CJEU
    # tell; ECHR application numbers run /YY too, but never behind "Case")
    if re.search(r"(?i)\bcases?\s*$", before):
        return None, None, DROP
    return m.group("appno"), None, "case"


register(Grammar(
    "echr_appno", "case",
    re.compile(
        r"(?<!egulation )(?<!irective )(?<!ecision )(?<!Order )"
        r"(?:App(?:lication)?s?\.?\s+)?nos?\.?\s*(?P<appno>\d{1,5}/\d{2})(?!\d)",
        re.IGNORECASE,
    ),
    _echr_appno,
))

# (CanLII citations — "1997 CanLII 358 (SCC)" — are handled by the commonwealth
# grammar set, which mints canlii/YYYY/N candidates; the parallel-adjacency miner
# clusters them with SCR/neutral forms cited in the same breath.)

# Bracketless form (Canada / India): "2024 SCC 1", "2023 INSC 456". Tighter to
# curb false positives — a 4-digit year, an all-caps 2–6 letter court token, a
# number. Resolution still gates whether it points at a real node.
register(Grammar(
    "neutral_citation_bracketless", "case",
    re.compile(r"\b(?P<year>(?:19|20)\d{2})\s+(?P<court>[A-Z]{2,6})\s+(?P<num>\d{1,5})\b"),
    _neutral_bracketless,
))


# "Case C-311/18", "C-617/10", "C-11/26 P" (appeal), "C-619/18 PPU" (urgent),
# "T-1/24 R" (interim), joined cases "C-293/12 and C-594/12". → CJEU CELEX
# (6 + year + CJ/TJ + number). The procedure suffix (P/PPU/R/DEP/…) is recorded
# in the matched text but doesn't change the CELEX descriptor (still a judgment);
# the 2-digit /NN year is 20NN. Candidate resolves to the ECLI-keyed judgment via
# the CELEX→ECLI alias the pipeline registers on harvest.
_CJEU_SUFFIX = rf"P{_DASH}R|P|PPU|R|RENV|REV|REC|DEP|OST|SA|AJ|INT|OP|TO"


def _cjeu_case_celex(m: "re.Match[str]") -> Normalised:
    court = {"C": "CJ", "T": "TJ", "F": "FJ"}.get(m.group("court").upper(), "CJ")
    yy = m.group("year")
    year = ("20" if int(yy) < 60 else "19") + yy if len(yy) == 2 else yy
    return f"6{year}{court}{int(m.group('num')):04d}", None, "case"


register(Grammar(
    "cjeu_case_number", "case",
    re.compile(
        rf"\b(?:Joined\s+Cases?\s+|Cases?\s+|Case\s+)?"
        # PDFs/typesetting often put spaces around the dash: "C - 176/03", "T – 344/99"
        rf"(?P<court>[CTF])\s*{_DASH}\s*(?P<num>\d+)/(?P<year>\d{{2,4}})"
        rf"(?:\s+(?:{_CJEU_SUFFIX}))?\b"
    ),
    _cjeu_case_celex,
))


def _cjeu_old_celex(m: "re.Match[str]") -> Normalised:
    yy = m.group("year")
    # The bracketless "Case N/YY" form only existed 1952–1989, so a 2-digit year is
    # always 19xx — Case 9/56 (Meroni) is 1956, not 2056. (The C-/T- prefixed form,
    # which starts in 1989, keeps the <60 → 20xx rule below.)
    year = "19" + yy if len(yy) == 2 else yy
    return f"6{year}CJ{int(m.group('num')):04d}", None, "case"  # pre-1989: all Court of Justice


# Pre-1989 EU cases had NO court letter — "Case 240/83", "Joined Cases 56/64 and 58/64".
# They were all Court of Justice (CJ). Require the "Case"/"Cases" cue so a bare "240/83"
# (a fraction, a ratio) isn't mistaken for a case number. The older reports write
# "Case No 9/70" / "Case No. 13/68" — the "No" must be allowed, and it must WIN over
# the ECHR application-number grammar (which would otherwise read "No 9/70" as a
# Strasbourg appno); the longer "Case No 9/70" span carries it through the dedupe.
register(Grammar(
    "cjeu_case_number_old", "case",
    re.compile(
        rf"\b(?:Joined\s+Cases?|Cases?|Case)\s+(?:Nos?\.?\s+)?(?P<num>\d+)/(?P<year>\d{{2,4}})\b"
    ),
    _cjeu_old_celex,
))

# European Court Reports — the pre-ECLI report citation for EU cases: "[1985] ECR 531",
# "[2002] ECR II-2905" (II = General Court/CFI), "[2005] ECR I-7879" (I = Court of
# Justice). No CELEX is derivable from the page number, so like a law report it's a
# candidate-less "maybe" the user resolves/disposes of manually. OCR mangles the
# volume ("II-" → "1-"/"11-"/"2-"/"ll-"/"Il-"), so the volume token is read loosely.
register(Grammar(
    "ecr_report", "case",
    re.compile(
        r"\[(?:19|20)\d{2}\]\s+E\.?C\.?R\.?\s+(?:(?:I{1,2}|ll?|Il|1{1,2}|2)\s*[-‐‑‒–—―−]\s*)?\d+",
        re.IGNORECASE,
    ),
    lambda m: (None, None, "case"),  # candidate-less → flagged for manual handling
))

# Classic UK/Irish/Commonwealth law reports — "[1982] AC 1", "(1985) 80 Cr App R 1",
# "(1868) LR 3 HL 330", "150 ER 1030". The pre-neutral-citation way of citing a case;
# there's no fetchable id, so like the ECR these are candidate-less, but recognising them
# surfaces heavily-cited pre-2001 authorities the corpus can't hold (reporters.py owns the
# series list and the three structural shapes). entity_kind ``law_report`` keeps them out
# of the routable worklist and into the "cited but unfetchable" frontier, where the reader
# offers a BAILII link and an upload resolves them.
from .reporters import (  # noqa: E402
    ENGLISH_REPORTS_RE,
    OLD_LAW_REPORTS_RE,
    REPORT_RE,
    REPORT_SERIES as _ALL_REPORT_SERIES,
    SCOTS_BARE_RE,
)

# Fold every report-series token into the set the neutral-citation grammars use to reject
# a court, so "1999 SC 583" (Session Cases) is never minted as a fake sc/1999/583 slug.
#
# Registered court codes are held back from the whole set — both the hand-listed literal
# above and the folded additions. A token in BOTH tables (SGCA the Singapore court and a
# "series"; JLR the Jersey court and the Jersey Law Reports) must resolve as the court, or
# the neutral grammar rejects "[2011] SGCA 9" as a report and the court mints no candidate
# at all. Courts always win — pruning the final set makes that hold no matter which table
# introduced the collision.
_REPORT_TOKENS = {re.sub(r"[.\s'’&]", "", s).upper() for s in _ALL_REPORT_SERIES}
REPORT_SERIES = (REPORT_SERIES | _REPORT_TOKENS) - set(COURTS_BY_CODE)


def _law_report(m: "re.Match[str]") -> Normalised:
    # candidate-less (no fetchable id) — recognising it makes an unfetchable authority
    # visible + rankable; entity_kind 'case' keeps pinpoint + treatment logic working.
    return None, None, "case"


for _name, _pat in (
    ("law_report", REPORT_RE),
    ("law_report_old_lr", OLD_LAW_REPORTS_RE),
    ("law_report_er", ENGLISH_REPORTS_RE),
    ("law_report_scots", SCOTS_BARE_RE),
):
    register(Grammar(_name, "case", _pat, _law_report))


def _celex_kind(celex: str) -> str:
    sector = celex[0]
    if sector == "6":
        return "case"
    desc = celex[5] if len(celex) > 5 else ""
    return {"R": "regulation", "L": "directive", "D": "decision"}.get(desc, "eu_instrument")


register(Grammar(
    "celex", "eu_instrument",
    re.compile(r"\b\d{5}[A-Z]{1,2}\d{4}\b"),
    lambda m: (m.group(0).upper(), None, _celex_kind(m.group(0).upper())),
))

# "Article 17 of Regulation (EU) 2016/679", "Directive 2002/58/EC", with pinpoint.
register(Grammar(
    "eu_instrument_numeric", "regulation",
    re.compile(
        r"(?:Art(?:icle|\.)?\s*(?P<art>\d+[a-z]?(?:\(\d+[a-z]?\))*)\s+(?:of\s+)?(?:the\s+)?)?"
        r"(?:(?:Council|Commission|European\s+Parliament\s+and\s+(?:of\s+the\s+)?Council)\s+)?(?:(?:Implementing|Delegated)\s+)?(?P<kind>(?:Framework\s+)?(?:Regulation|Directive|Decision))\s*(?:\((?:EU|EC|EEC)\)\s*)?"
        r"(?:No\.?\s*)?(?P<a>\d{1,4})/(?P<b>\d{1,4})(?:/(?:EU|EC|EEC|JHA|CFSP|PESC|Euratom))?\b",
        re.IGNORECASE,
    ),
    lambda m: (
        _eu_celex(m.group("kind"), m.group("a"), m.group("b")),
        f"Article {m.group('art')}" if m.group("art") else None,
        m.group("kind").lower(),
    ),
))

# "Article 10 of the Convention" / "Article 8 ECHR" / "Art. 6 of the European Convention on
# Human Rights" → the European Convention on Human Rights (ETS No. 5). Without this, a bare
# "Article 10" carries forward to the last-named EU instrument — wrong when it's the ECHR.
# "of the Geneva Convention" etc. don't match (the word between "the" and "Convention" breaks
# it); plain "the Convention" in this domain means the ECHR.
register(Grammar(
    "echr_convention_article", "treaty",
    re.compile(
        r"\bArt(?:icle)?s?\.?\s+(?P<num>\d{1,2})(?:\s*§+\s*\d+)?\s+"
        r"(?:of\s+the\s+)?"
        r"(?:(?:European\s+)?Convention(?:\s+on\s+Human\s+Rights)?|ECHR)\b",
        re.IGNORECASE,
    ),
    lambda m: ("echr/convention", f"Article {m.group('num')}", "treaty"),
))

# EU primary law + the Charter, cited by name: "Article 4(2) TEU", "Article 267 of
# the Treaty on the Functioning of the European Union", "Article 52(1) of the
# Charter of Fundamental Rights". These weren't recognised at all, so the reference
# vanished (and a bare "Article 6" then carried forward to the last-named directive,
# which is exactly the Charter-vs-Directive mix-up flagged in the corpus). The
# candidate is the consolidated CELEX so every reference clusters to one node and
# the instrument is harvestable. The "(2)" sub-article rides along as the pinpoint.
_EU_TREATIES = (
    (r"(?:the\s+)?Treaty\s+on\s+the\s+Functioning\s+of\s+the\s+European\s+Union|TFEU|TFUE"
     r"|(?:le\s+)?Trait[ée]\s+sur\s+le\s+fonctionnement\s+de\s+l['’]Union\s+europ[ée]enne", "12016E"),
    (r"(?:the\s+)?Treaty\s+on\s+European\s+Union|TEU|TUE"
     r"|(?:le\s+)?Trait[ée]\s+sur\s+l['’]Union\s+europ[ée]enne", "12016M"),
    (r"(?:the\s+)?Charter\s+of\s+Fundamental\s+Rights(?:\s+of\s+the\s+European\s+Union)?"
     r"|(?:the\s+)?EU\s+Charter|CFREU|the\s+Charter"
     r"|(?:la\s+)?Charte\s+des\s+droits\s+fondamentaux(?:\s+de\s+l['’]Union\s+europ[ée]enne)?", "12012P"),
)
for _i, (_names, _celex) in enumerate(_EU_TREATIES):
    register(Grammar(
        f"eu_treaty_{_celex}", "treaty",
        re.compile(
            rf"\bArt(?:icle|\.)?s?\.?\s*(?P<art>\d+[a-z]?(?:\(\d+[a-z]?\))*)\s+"
            rf"(?:(?:of|du|de\s+la|des)\s+)?(?:{_names})\b",
            re.IGNORECASE,
        ),
        (lambda celex: lambda m: (celex, f"Article {m.group('art')}", "treaty"))(_celex),
    ))

# Just the instrument name at the head of a string → its candidate + kind, for the
# multi-article-list pass (which resolves the instrument that closes "Articles 4 and
# 6 of the Charter" itself, since only the list's last article — if any — reaches the
# grammar). Treaties first (most specific), then named acronyms/full names, then the
# numeric "Regulation/Directive X/Y" form.
_TREATY_HEAD = [(re.compile(rf"^(?:{names})\b", re.IGNORECASE), celex)
                for names, celex in _EU_TREATIES]


def instrument_at(text: str) -> tuple[str | None, str | None]:
    """(candidate, kind) for an EU instrument named at the START of ``text``."""
    for rx, celex in _TREATY_HEAD:
        if rx.match(text):
            return celex, "treaty"
    m = re.match(rf"(?:the\s+)?(?P<name>{_EU_FULL_NAMES})\b", text, re.IGNORECASE)
    if m:
        return _name_to_celex(m.group("name")), "regulation"
    m = re.match(rf"(?P<name>{_EU_ACRONYMS})\b", text)
    if m:
        return _name_to_celex(m.group("name")), "regulation"
    m = re.match(r"(?:(?:Council|Commission|European\s+Parliament\s+and\s+(?:of\s+the\s+)?Council)\s+)?(?:(?:Implementing|Delegated)\s+)?(?P<kind>(?:Framework\s+)?(?:Regulation|Directive|Decision))\s*(?:\((?:EU|EC|EEC)\)\s*)?"
                 r"(?:No\.?\s*)?(?P<a>\d{1,4})/(?P<b>\d{1,4})(?:/(?:EU|EC|EEC|JHA|CFSP|PESC|Euratom))?\b", text, re.IGNORECASE)
    if m:
        return _eu_celex(m.group("kind"), m.group("a"), m.group("b")), m.group("kind").lower()
    return None, None

# "Article 17 GDPR" / "Art. 22 of the GDPR" / "Article 6 of the DMA" / "Digital
# Services Act". Acronym form (uppercase) and spelled-out form (any case).
# Acronyms that are also ordinary words or company-name fragments, and so need the
# citation-shaped context the real usage always has: an Article pinpoint, or an
# immediately preceding definite article. "LED" is English prose in ALL-CAPS
# Commonwealth headnotes ("EVIDENCE LED AT TRIAL") — a bug that once made the Law
# Enforcement Directive the corpus's top EU authority, cited by 1902 cases. AVG,
# DSGVO and RGPD are the Dutch, German and French names for the GDPR, and are only
# ever written that way in those languages; a bare "AVG" in an Ontario judgment is a
# corporate name ("ASU AVG"), not the GDPR. The determiners therefore span the
# languages the acronym actually appears in, so "de AVG" and "der DSGVO" still link.
_NEEDS_DETERMINER = {"LED", "AVG", "DSGVO", "RGPD", "DSA"}
_DETERMINER_RE = re.compile(
    r"(?i)\b(?:the|of|de|het|der|die|das|dem|den|des|la|le|les|du|del|el|il)\s+$")


def _eu_acronym(m: "re.Match[str]") -> Normalised:
    name = m.group("name")
    if name in _NEEDS_DETERMINER and not m.group("art"):
        pre = m.string[max(0, m.start("name") - 12):m.start("name")]
        if not _DETERMINER_RE.search(pre):
            return None, None, DROP
    if name == "DSA" and not m.group("art"):
        # UK immigration judgments use DSA for the Duty Solicitor Advice scheme:
        # phrases such as "both the DSA scheme" satisfy the determiner rule above
        # but are still plainly not the Digital Services Act.
        after = m.string[m.end("name"):m.end("name") + 24]
        if re.match(
            r"(?i)^\s+(?:scheme|surgery|session|appointment)\b", after,
        ):
            return None, None, DROP
    return (_name_to_celex(name),
            f"Article {m.group('art')}" if m.group("art") else None, None)


register(Grammar(
    "eu_named", "regulation",
    # Case-sensitive so the acronym (GDPR/DMA/DSA/LED) stays uppercase-only, with
    # the "Article" prefix case-insensitive. The leading \b is load-bearing:
    # without it "APPEALED"/"RULED"/"MISLED" match their final LED.
    re.compile(rf"(?:(?i:art(?:icle|\.)?)\s*(?P<art>\d+[a-z]?(?:\(\d+[a-z]?\))*)\s+(?i:of\s+(?:the\s+)?)?)?\b(?P<name>{_EU_ACRONYMS})\b"),
    _eu_acronym,
))
register(Grammar(
    "eu_named_full", "regulation",
    re.compile(rf"(?:Art(?:icle|\.)?\s*(?P<art>\d+[a-z]?(?:\(\d+[a-z]?\))*)\s+(?:of\s+)?(?:the\s+)?)?(?P<name>{_EU_FULL_NAMES})\b",
               re.IGNORECASE),
    lambda m: (
        _name_to_celex(m.group("name")),
        f"Article {m.group('art')}" if m.group("art") else None,
        None,
    ),
))

# ── UK GDPR (the assimilated / "retained" EU GDPR) ───────────────────────────
# "Article 20 of the UK GDPR" is the DOMESTIC, UK-amendable version — NOT the EU
# original: it lives on legislation.gov.uk at ``eur/2016/679`` (fetched via
# uk-legislation, giving the amended UK text), kept distinct from CELEX 32016R0679.
# Registered so it beats the plain GDPR grammar, which would otherwise map "UK GDPR"
# to the EU instrument and drop the article. "UK Data Protection Regulation" too.
UK_GDPR_ID = "european/regulation/2016/0679"
register(Grammar(
    "uk_gdpr", "regulation",
    re.compile(
        r"(?:Art(?:icle|\.)?\s*(?P<art>\d+[a-z]?(?:\(\d+[a-z]?\))*)\s+(?:of\s+)?(?:the\s+)?)?"
        r"(?:UK|United\s+Kingdom)\s+GDPR\b",
        re.IGNORECASE,
    ),
    lambda m: (UK_GDPR_ID, f"Article {m.group('art')}" if m.group("art") else None, "regulation"),
))

# ── recitals (EU instruments have them; UK Acts don't) ───────────────────────
# Recitals are cited constantly in guidance and cases, in many shapes: "Recital 47",
# "recital (47)", "Recitals 26 and 27", "recital 1 to 5", "recital 65 of Regulation
# (EU) 2016/679", "Recital 47 of the GDPR", "recital (26) of the UK GDPR". They pin
# to the SAME instrument node as an article, only the anchor differs ("Recital 47").
# A bare "recital 47" (no instrument) is handled by the carry-forward pass, like a
# bare article.
_RECITAL = (r"[Rr]ecitals?\s*\(?(?P<rec>\d+\s*"
            r"(?:(?:to|and|,|&|" + _DASH + r")\s*\d+\s*)*)\)?")


def _recital_pin(rec: str) -> str:
    """Normalise a recital number expression into a pinpoint anchor: "Recital 47", or
    "Recitals 26 and 27" / "Recitals 1 to 5" when it's a list/range."""
    rec = re.sub(r"\s+", " ", rec).strip().rstrip(",")
    plural = bool(re.search(r"(?:to|and|,|&|" + _DASH + r")", rec))
    return f"Recital{'s' if plural else ''} {rec}"


# "recital 65 of Regulation (EU) 2016/679" / "Recitals 1 to 5 of Directive 2002/58/EC".
register(Grammar(
    "recital_eu_numeric", "regulation",
    re.compile(
        _RECITAL + r"\s+(?:of\s+)?(?:the\s+)?"
        r"(?:(?:Council|Commission|European\s+Parliament\s+and\s+(?:of\s+the\s+)?Council)\s+)?(?:(?:Implementing|Delegated)\s+)?(?P<kind>(?:Framework\s+)?(?:Regulation|Directive|Decision))\s*(?:\((?:EU|EC|EEC)\)\s*)?"
        r"(?:No\.?\s*)?(?P<a>\d{1,4})/(?P<b>\d{1,4})(?:/(?:EU|EC|EEC|JHA|CFSP|PESC|Euratom))?\b",
        re.IGNORECASE,
    ),
    lambda m: (_eu_celex(m.group("kind"), m.group("a"), m.group("b")),
               _recital_pin(m.group("rec")), m.group("kind").lower()),
))

# "recital (26) of the UK GDPR" → the assimilated UK instrument (before plain GDPR).
register(Grammar(
    "recital_uk_gdpr", "regulation",
    re.compile(_RECITAL + r"\s+(?:of\s+)?(?:the\s+)?(?:UK|United\s+Kingdom)\s+GDPR\b", re.IGNORECASE),
    lambda m: (UK_GDPR_ID, _recital_pin(m.group("rec")), "regulation"),
))

# "Recital 47 of the GDPR" / "recital (26) GDPR" / "Recital 11 of the DMA".
register(Grammar(
    "recital_eu_named", "regulation",
    re.compile(_RECITAL + rf"\s+(?:of\s+(?:the\s+)?)?(?P<name>{_EU_ACRONYMS})\b"),
    lambda m: (_name_to_celex(m.group("name")), _recital_pin(m.group("rec")), None),
))
register(Grammar(
    "recital_eu_named_full", "regulation",
    re.compile(_RECITAL + rf"\s+(?:of\s+)?(?:the\s+)?(?P<name>{_EU_FULL_NAMES})\b", re.IGNORECASE),
    lambda m: (_name_to_celex(m.group("name")), _recital_pin(m.group("rec")), None),
))

# legislation.gov.uk URI, with optional /section/N pinpoint.
# ── Civil Procedure Rules and Practice Directions ────────────────────────────
# The current consolidation is not provision-addressable on legislation.gov.uk.
# The Ministry of Justice adapter holds one record per Part/PD and aliases every
# printed rule number (uk/cpr/rule/3.9 -> uk/cpr/part/3).  Grammar candidates use
# those addresses so resolution lands on the current consolidated text, while the
# pinpoint preserves the full sub-rule.
_CPR_NAME = r"(?:CPR|Civil\s+Procedure\s+Rules(?:\s+1998)?)"
_CPR_RULE = r"\d+[A-Z]?\.\d+[A-Z]?(?:\([A-Za-z0-9]+\))*"
_CPR_PART = r"\d+[A-Z]?"
_CPR_PD = r"\d+[A-Z]+(?:\d+)?"
_CPR_PARA = r"\d+(?:\.\d+)*(?:\([A-Za-z0-9]+\))*"


def _cpr_code(value: str) -> str:
    m = re.fullmatch(r"0*(\d+)([A-Za-z]*)(\d*)", value.strip())
    if not m:
        return value.casefold()
    return f"{int(m.group(1))}{m.group(2).lower()}{m.group(3)}"


def _cpr_rule(m: "re.Match[str]") -> Normalised:
    printed = re.sub(r"\s+", "", m.group("rule"))
    base = printed.split("(", 1)[0].casefold()
    return f"uk/cpr/rule/{base}", f"rule {printed}", "regulation"


def _cpr_part(m: "re.Match[str]") -> Normalised:
    code = _cpr_code(m.group("part"))
    return f"uk/cpr/part/{code}", f"Part {code.upper()}", "regulation"


def _cpr_pd(m: "re.Match[str]") -> Normalised:
    code = _cpr_code(m.group("pd"))
    para = m.groupdict().get("para") or m.groupdict().get("para_br")
    return f"uk/cpr/pd/{code}", f"paragraph {para}" if para else None, "guidance"


# "CPR 3.9(1)(a)", "CPR r. 3.9", "Civil Procedure Rules 1998, rule 3.9".
register(Grammar(
    "uk_cpr_rule_prefix", "regulation",
    re.compile(
        rf"\b{_CPR_NAME}\s*,?\s*(?:(?:r(?:ule)?s?)\.?\s*)?"
        rf"(?P<rule>{_CPR_RULE})(?![A-Za-z0-9(])",
        re.IGNORECASE,
    ),
    _cpr_rule,
))

# "rule 3.9 of the CPR" / "r. 3.9 under the Civil Procedure Rules".
register(Grammar(
    "uk_cpr_rule_suffix", "regulation",
    re.compile(
        rf"\b(?:r(?:ule)?s?)\.?\s*(?P<rule>{_CPR_RULE})"
        rf"\s+(?:of|under)\s+(?:the\s+)?{_CPR_NAME}\b",
        re.IGNORECASE,
    ),
    _cpr_rule,
))

# "CPR Part 36", "Pt 52 of the CPR".
register(Grammar(
    "uk_cpr_part_prefix", "regulation",
    re.compile(rf"\b{_CPR_NAME}\s*,?\s*(?:Part|Pt\.?)\s*(?P<part>{_CPR_PART})\b",
               re.IGNORECASE),
    _cpr_part,
))
register(Grammar(
    "uk_cpr_part_suffix", "regulation",
    re.compile(
        rf"\b(?:Part|Pt\.?)\s*(?P<part>{_CPR_PART})"
        rf"\s+(?:of|under)\s+(?:the\s+)?{_CPR_NAME}\b",
        re.IGNORECASE,
    ),
    _cpr_part,
))

# "Practice Direction 3D para 5.2", "CPR PD51U, paragraph 2.1",
# "paragraph 18.1 of PD 32".  Numeric-only PDs (PD 32) are valid, but a decimal
# cannot be a PD code, which keeps "Practice Direction 5.5" from being mistaken
# for a document identity.
register(Grammar(
    "uk_cpr_practice_direction", "guidance",
    re.compile(
        rf"\b(?:CPR\s+)?(?:Practice\s+Direction|PD)\s*"
        rf"(?P<pd>{_CPR_PD}|\d+)"
        rf"(?:\s*,?\s*(?:para(?:graph)?|§)\.?\s*(?P<para>{_CPR_PARA}))?\b",
        re.IGNORECASE,
    ),
    _cpr_pd,
))
register(Grammar(
    "uk_cpr_practice_direction_suffix", "guidance",
    re.compile(
        rf"(?:\b(?:para(?:graph)?|§)\.?\s*(?P<para>{_CPR_PARA})"
        rf"|\[(?P<para_br>{_CPR_PARA})\])"
        rf"\s*(?:(?:of|under)\s+(?:the\s+)?|,\s*)(?:CPR\s+)?"
        rf"(?:Practice\s+Direction|PD)\s*(?P<pd>{_CPR_PD}|\d+)\b",
        re.IGNORECASE,
    ),
    _cpr_pd,
))
register(Grammar(
    "uk_cpr_practice_direction_welsh", "guidance",
    re.compile(
        rf"\bCyfarwyddyd\s+Ymarfer\s*(?P<pd>{_CPR_PD}|\d+)"
        rf"(?:\s*,?\s*paragraff\s*(?P<para>{_CPR_PARA}))?\b",
        re.IGNORECASE,
    ),
    lambda m: (
        f"uk/cpr/pd/{_cpr_code(m.group('pd'))}/cy",
        f"paragraff {m.group('para')}" if m.group("para") else None,
        "guidance",
    ),
))

# The most frequently cited named, non-numbered direction has no PD code.
register(Grammar(
    "uk_cpr_pre_action_direction", "guidance",
    re.compile(
        r"\bPractice\s+Direction\s*(?:\(|[-–—:]?\s*)"
        r"(?:on\s+)?Pre-Action\s+Conduct(?:\s+and\s+Protocols)?\)?",
        re.IGNORECASE,
    ),
    lambda m: ("uk/cpr/pd/pre-action-conduct-and-protocols", None, "guidance"),
))
register(Grammar(
    "uk_cpr_insolvency_direction", "guidance",
    re.compile(
        r"\b(?:Practice\s+Direction\s*[:–—-]\s*Insolvency\s+Proceedings"
        r"|Insolvency\s+Practice\s+Direction)\b",
        re.IGNORECASE,
    ),
    lambda m: ("uk/cpr/pd/insolvency-proceedings", None, "guidance"),
))

# A bare full title is useful and unambiguous.  Bare "CPR" is deliberately not a
# grammar: it is also a Canadian law-report abbreviation and would pollute non-UK
# material.  Once a judgment defines "(CPR)", Raglex's shorthand pass can still
# link later uses in that document.
register(Grammar(
    "uk_cpr_instrument", "regulation",
    re.compile(r"\bCivil\s+Procedure\s+Rules(?:\s+1998)?\b", re.IGNORECASE),
    lambda m: ("uk/cpr", None, "regulation"),
))


# legislation.gov.uk URI, with optional /section/N pinpoint.
register(Grammar(
    "uk_legislation_uri", "act",
    re.compile(r"legislation\.gov\.uk/(?:id/)?(?P<path>[a-z]{2,6}/\d{4}/\d+)(?:/section/(?P<sec>\d+[a-z]?))?", re.IGNORECASE),
    lambda m: (m.group("path").lower(), f"s. {m.group('sec')}" if m.group("sec") else None, None),
))

# "section 14 of the Freedom of Information Act 2000" / "FOIA s.14".
_ACT_NAMES = "|".join(re.escape(k) for k in sorted(_UK_ACT_TO_ID, key=len, reverse=True))
register(Grammar(
    "uk_act_section", "act",
    # the section number may carry a subsection/paragraph tail — "166(2)", "55A",
    # "33(1)(a)" — all of which belong in the pinpoint.
    re.compile(
        # \b after the name group so an abbreviation like "FOIA" doesn't match inside
        # a longer word ("FOIAs", "FOIABILITY").
        rf"(?:s(?:ection|\.)?\s*(?P<sec>\d+[a-z]?(?:\(\d+[a-z]?\))*)\s+of\s+(?:the\s+)?)?(?P<name>{_ACT_NAMES})\b"
        rf"(?:\s+s(?:ection|\.)?\s*(?P<sec2>\d+[a-z]?(?:\(\d+[a-z]?\))*))?",
        re.IGNORECASE,
    ),
    lambda m: (
        _UK_ACT_TO_ID.get(m.group("name").lower()),
        (lambda s: f"s. {s}" if s else None)(m.group("sec") or m.group("sec2")),
        None,
    ),
))


# "regulation 6 of the Privacy and Electronic Communications (EC Directive) Regulations
# 2003", "reg 6 PECR", "PECR", "PECR reg 6". The pinpoint may lead or trail, as it does
# for Acts, and carries any sub-paragraph tail ("21(1)(b)").
def _resolve_named_si(m: "re.Match[str]") -> Normalised:
    number = m.group("reg") or m.group("reg2")
    return (
        _UK_SI_TO_ID.get(re.sub(r"\s+", " ", m.group("name")).strip().lower()),
        f"reg. {re.sub(r'\s+', '', number)}" if number else None,
        "regulation",
    )


def _si_pattern(names: str) -> str:
    # "of" is optional: "reg 6 PECR" is as ordinary a form as "reg 6 of PECR".
    return (
        r"(?:reg(?:ulation|\.)?\s*(?P<reg>\d+[A-Za-z]?(?:\([0-9A-Za-z]+\))*)"
        r"\s+(?:of\s+)?(?:the\s+)?)?"
        rf"(?P<name>{names})\b"
        # The series number is routinely restated right after the name — "the X
        # Regulations 2003, SI 2003/2426". That is ONE reference, so swallow it here
        # rather than leaving a second grammar to match it as a separate citation.
        r"(?:\s*,?\s*\(?S\.?\s?I\.?\s*(?:No\.?\s*)?(?:(?:19|20)\d{2}\s*/\s*)?\d{1,5}\)?)?"
        r"(?:\s*,?\s*reg(?:ulation|\.)?\s*(?P<reg2>\d+[A-Za-z]?(?:\([0-9A-Za-z]+\))*))?"
    )


# Two registrations, because the case rules differ: the spelled-out name is matched in
# any case, the bare acronym only in upper — so "pecr" in a filename or an identifier
# can never mint an edge, while "the Privacy and Electronic Communications Regulations"
# resolves however a judgment happens to capitalise it.
register(Grammar(
    "uk_si_short_name", "regulation",
    re.compile(_si_pattern(_UK_SI_FULL_NAMES), re.IGNORECASE),
    _resolve_named_si,
))
register(Grammar(
    "uk_si_acronym", "regulation",
    re.compile(_si_pattern(_UK_SI_ACRONYMS)),
    _resolve_named_si,
))


# Generic "<Title> Act <year>" (with optional "section N of …" pinpoint), resolved via
# the vendored legislation.gov.uk title gazetteer (statute_gazetteer) — so we recognise
# the *thousands* of statutes a corpus cites by name, not just the curated handful above.
# Precision comes from confirmation: the shape is loose, but we only mint a candidate when
# the gazetteer (or the curated map) actually has that title+year; otherwise it stays a
# name-only "maybe" the snowball can surface. Year is mandatory (exact-match resolution).
def _resolve_named_statute(m: "re.Match[str]") -> Normalised:
    from .statute_gazetteer import resolve as _gz

    title, year = m.group("title").strip(), m.group("year")
    cid = _UK_ACT_TO_ID.get(f"{title} {year}".lower()) or _gz(title, year)
    sec = re.sub(r"\s+", "", m.groupdict().get("sec") or "") or None
    part = re.sub(r"\s+", "", m.groupdict().get("part") or "") or None
    pinpoint = f"s. {sec}" if sec else f"Part {part}" if part else None
    return cid, pinpoint, "act"


register(Grammar(
    "uk_statute_named", "act",
    # Many Act short titles have internal commas — "Local Government, Economic Development
    # and Construction Act 2009", "Housing Grants, Construction and Regeneration Act 1996",
    # "Police, Crime, Sentencing and Courts Act 2022" — so a comma is allowed between title
    # tokens (``,?\s+``), or the first clause is lost. Over-capture is harmless: a candidate
    # is only minted when the gazetteer confirms the exact title+year.
    # The token run is BOUNDED ({0,11}): an unbounded ``*?`` here meant every capitalised
    # word in the text started a scan that could chew through an arbitrarily long run of
    # capitalised tokens hunting for an "Act <year>" that never comes — a tabular annexure
    # of names froze a rescan for hours (fca/2016/1034, 2026-07). No real short title has
    # more than ~8 tokens, so the bound only cuts the pathological scans.
    re.compile(
        r"(?:(?:s(?:ection|\.)?\s*(?P<sec>\d+[A-Za-z]?(?:\s*\(\s*[A-Za-z0-9]+\s*\))*)"
        r"|Part\s+(?P<part>[IVXLC]+[A-Za-z]?|\d+[A-Za-z]?))\s+of\s+)?"
        r"(?:the\s+)?"
        r"(?P<title>[A-Z][A-Za-z0-9'’.\-]*"
        r"(?:,?\s+(?:and|of|for|to|in|on|the|No\.?|[A-Z][A-Za-z0-9'’.\-]*|\([^()]{1,60}\))){0,11}?"
        r"\s+(?:Act|Measure))\s+(?P<year>(?:1[6-9]|20)\d{2})\b"
    ),
    _resolve_named_statute,
))

# Law Commission glossaries and government guidance often give a statutory
# instrument's full title followed by the printed series form rather than a URL:
# ``General Product Safety Regulations 2005, SI No 1803``.  The series number is
# the authoritative legislation.gov.uk identity, so this resolves directly.
def _uk_si_named(m: "re.Match[str]") -> Normalised:
    year = m.group("series_year") or m.group("title_year")
    num = m.group("num_no") or m.group("num_series") or m.group("num_plain")
    return (
        f"uksi/{year}/{int(num)}",
        None,
        "regulation",
    )


register(Grammar(
    "uk_si_named", "regulation",
    re.compile(
        r"(?P<title>[A-Z][A-Za-z0-9'’().,\-]*"
        r"(?:\s+(?:and|of|for|to|in|on|the|from|by|with|without|against|"
        r"No\.?|[A-Z][A-Za-z0-9'’().,\-]*)){0,14}?"
        r"\s+(?:Regulations?|Rules|Order))"
        r"(?:\s+(?P<title_year>(?:19|20)\d{2}))?"
        r"(?:\s*\((?P<abbr>[A-Za-z][A-Za-z0-9.]{1,14})\))?"
        r"\s*,?\s*\(?S\.?\s*I\.?\s*"
        r"(?:"
        r"No\.?\s*(?P<num_no>\d{1,5})"
        r"|(?P<series_year>(?:19|20)\d{2})\s*"
        r"(?:/|No\.?\s*)(?P<num_series>\d{1,5})"
        r"|(?P<num_plain>\d{1,5})"
        r")\)?\b"
    ),
    _uk_si_named,
))


# The bare series form, with no short title in front of it: ``SI 2003/2426``,
# ``S.I. 2003 No. 2426``. Inside a sentence the named grammar above already caught it,
# so a lookup of the SAME citation on its own answered "not held, not routable" for an
# instrument the corpus had all along — the front door said no about PECR while the
# document sat in the catalogue. The pattern is deliberately anchored on the year/number
# pair, so it cannot fire on a stray "SI" (Système international, a party's initials).
def _uk_si_bare(m: "re.Match[str]") -> Normalised:
    return f"uksi/{m.group('year')}/{int(m.group('num'))}", None, "regulation"


register(Grammar(
    "uk_si_bare", "regulation",
    re.compile(
        r"\bS\.?\s?I\.?\s*"
        r"(?P<year>(?:19|20)\d{2})\s*(?:/|No\.?\s*)\s*(?P<num>\d{1,5})\b"
    ),
    _uk_si_bare,
))


# Commonwealth citation forms that break the shapes above — India's colon-delimited
# neutral citation and AIR, Canada's CanLII slot, the South African SA-report shape,
# Nigeria's NWLR part format, Kenya's eKLR database id, and Hong Kong registry case
# numbers. Imported last so its (longer, more specific) patterns are registered
# alongside the generic ones; the extractor's longest-match dedupe does the rest.
from . import commonwealth as _commonwealth  # noqa: E402,F401  (registers on import)

# Imported last so the French module can reuse the registry primitives above.
from . import french as _french  # noqa: E402,F401  (registers on import)
