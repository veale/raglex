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
from .courts import DIVISIONS

# Resolvable candidate, pinpoint anchor, optional entity-kind override.
Normalised = tuple[str | None, str | None, str | None]


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
_DESCRIPTOR = {"regulation": "R", "directive": "L", "decision": "D"}


def _eu_celex(kind: str, a: str, b: str) -> str | None:
    """Build a CELEX from an EU instrument number. A 4-digit group is the year
    ('2016/679', '45/2001'); for the old 2-digit forms ('Directive 95/46',
    'Regulation 1612/68') the convention differs by instrument — **directives put
    the year first**, regulations put it second — and a 2-digit year ≥31 is 19xx,
    else 20xx."""
    desc = _DESCRIPTOR.get(kind.lower())
    if not desc:
        return None
    if re.fullmatch(r"(19|20)\d{2}", a):
        year, num = a, b
    elif re.fullmatch(r"(19|20)\d{2}", b):
        year, num = b, a
    else:  # both 2-digit: directives are year/number, regulations number/year
        yy, num = (a, b) if kind.lower() == "directive" else (b, a)
        if not re.fullmatch(r"\d{1,2}", yy):
            return None
        year = ("19" if int(yy) >= 31 else "20") + f"{int(yy):02d}"
    if not re.fullmatch(r"(19|20)\d{2}", year):
        return None
    return f"3{year}{desc}{int(num):04d}"


# GDPR and its multilingual short names → its CELEX.
_NAME_TO_CELEX = {
    "gdpr": "32016R0679", "avg": "32016R0679", "dsgvo": "32016R0679", "rgpd": "32016R0679",
}

# UK statute short names → legislation.gov.uk id (for "section N of the X Act").
_UK_ACT_TO_ID = {
    "freedom of information act 2000": "ukpga/2000/36",
    "foia": "ukpga/2000/36",
    "data protection act 2018": "ukpga/2018/12",
    "dpa 2018": "ukpga/2018/12",
    "human rights act 1998": "ukpga/1998/42",
}


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
# Full ECLI (any country) OR a bare EU ECLI without the "ECLI:" prefix — the latter
# turns up when a PDF drops the prefix or a citation style writes "EU:C:2020:559". The
# bare form is restricted to EU (EU:C/T/F) so it can't swallow arbitrary "XX:YY:…" text.
register(Grammar(
    "ecli", "case",
    re.compile(
        r"(?:ECLI:[A-Z]{2}:[A-Z0-9]+|(?<![A-Za-z])EU:[CTF])"
        r":\d{4}:[A-Z0-9]+(?:[._-][A-Z0-9]+)*",
        re.IGNORECASE,
    ),
    _ecli,
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
    "EHRR", "EHRLR", "EHRC", "CHRLD",  # European Human Rights Reports / Law Review etc.
}

# Statute short-title abbreviations that the *bracketless* grammar ("2009 CTA 2010")
# wrongly grabs as a court token — they're tax/other Acts referenced by a year, not
# neutral citations. Listing them keeps "2009 CTA 2010" from minting a fake cta/2009/2010
# candidate. (Mostly the Tax Law Rewrite abbreviations.)
STATUTE_ABBREVS = {
    "CTA", "ITEPA", "ITTOIA", "TCGA", "TMA", "ITA", "VATA", "VERA", "TPDA", "FA",
    "ICTA", "CAA", "IHTA", "TIOPA", "FA2", "CRCA", "TMA", "CEMA", "OTA",
}


def _neutral(m: "re.Match[str]") -> Normalised:
    court = m.group("court")
    if court.upper() in REPORT_SERIES or court.upper() in STATUTE_ABBREVS:
        return None, None, "case"  # a report series / statute abbrev, not a court
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
    r"(?P<name>[A-Z][A-Za-z.'’-]+(?:\s+(?:and\s+Others|and\s+[A-Z][A-Za-z.'’-]+|"
    r"[A-Z][A-Za-z.'’-]+))*?\s+v\.?\s+(?:the\s+)?[A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){0,3})"
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
register(Grammar(
    "echr_appno", "case",
    re.compile(
        r"(?<!egulation )(?<!irective )(?<!ecision )(?<!Order )"
        r"(?:App(?:lication)?s?\.?\s+)?nos?\.?\s*(?P<appno>\d{1,5}/\d{2})(?!\d)",
        re.IGNORECASE,
    ),
    lambda m: (m.group("appno"), None, "case"),
))

# Bracketless form (Canada / India): "2024 SCC 1", "2023 INSC 456". Tighter to
# curb false positives — a 4-digit year, an all-caps 2–6 letter court token, a
# number. Resolution still gates whether it points at a real node.
register(Grammar(
    "neutral_citation_bracketless", "case",
    re.compile(r"\b(?P<year>(?:19|20)\d{2})\s+(?P<court>[A-Z]{2,6})\s+(?P<num>\d{1,5})\b"),
    _neutral,
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
    year = ("20" if int(yy) < 60 else "19") + yy if len(yy) == 2 else yy
    return f"6{year}CJ{int(m.group('num')):04d}", None, "case"  # pre-1989: all Court of Justice


# Pre-1989 EU cases had NO court letter — "Case 240/83", "Joined Cases 56/64 and 58/64".
# They were all Court of Justice (CJ). Require the "Case"/"Cases" cue so a bare "240/83"
# (a fraction, a ratio) isn't mistaken for a case number.
register(Grammar(
    "cjeu_case_number_old", "case",
    re.compile(
        rf"\b(?:Joined\s+Cases?|Cases?|Case)\s+(?P<num>\d+)/(?P<year>\d{{2,4}})\b"
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
REPORT_SERIES |= {re.sub(r"[.\s'’&]", "", s).upper() for s in _ALL_REPORT_SERIES}


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
        r"(?:Art(?:icle|\.)?\s*(?P<art>\d+[a-z]?)\s+(?:of\s+)?(?:the\s+)?)?"
        r"(?P<kind>Regulation|Directive|Decision)\s*(?:\((?:EU|EC|EEC)\)\s*)?"
        r"(?:No\.?\s*)?(?P<a>\d{1,4})/(?P<b>\d{1,4})",
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

# "Article 17 GDPR" / "Art. 22 of the GDPR" / "AVG".
register(Grammar(
    "eu_named", "regulation",
    re.compile(r"(?:Art(?:icle|\.)?\s*(?P<art>\d+[a-z]?)\s+(?:of\s+(?:the\s+)?)?)?(?P<name>GDPR|AVG|DSGVO|RGPD)\b"),
    lambda m: (
        _NAME_TO_CELEX.get(m.group("name").lower()),
        f"Article {m.group('art')}" if m.group("art") else None,
        None,
    ),
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
        rf"(?:s(?:ection|\.)?\s*(?P<sec>\d+[a-z]?(?:\(\d+[a-z]?\))*)\s+of\s+(?:the\s+)?)?(?P<name>{_ACT_NAMES})"
        rf"(?:\s+s(?:ection|\.)?\s*(?P<sec2>\d+[a-z]?(?:\(\d+[a-z]?\))*))?",
        re.IGNORECASE,
    ),
    lambda m: (
        _UK_ACT_TO_ID.get(m.group("name").lower()),
        (lambda s: f"s. {s}" if s else None)(m.group("sec") or m.group("sec2")),
        None,
    ),
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
    sec = m.group("sec")
    return cid, (f"s. {sec}" if sec else None), "act"


register(Grammar(
    "uk_statute_named", "act",
    re.compile(
        r"(?:s(?:ection|\.)?\s*(?P<sec>\d+[A-Za-z]?(?:\(\d+[A-Za-z]?\))*)\s+of\s+)?"
        r"(?:the\s+)?"
        r"(?P<title>[A-Z][A-Za-z0-9'’.\-]*"
        r"(?:\s+(?:and|of|for|to|in|on|the|No\.?|[A-Z][A-Za-z0-9'’.\-]*|\([^()]{1,60}\)))*?"
        r"\s+(?:Act|Measure))\s+(?P<year>(?:1[6-9]|20)\d{2})\b"
    ),
    _resolve_named_statute,
))
