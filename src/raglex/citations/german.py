"""German extract → normalise helpers.

German statutory references are one-to-many.  ``bundesrecht.normalise`` expands
ranges, i.V.m. joins and compact sub-provision lists before we create graph edges.
The destination is the federal law Work (the same ``de/gesetz/<jurabk>`` id minted by
GII); the exact canonical provision is retained as ``dst_anchor``.
"""

from __future__ import annotations

import re
import unicodedata

from bundesrecht import normalise

from . import de_courts, de_laws
from .models import Citation


# German judgments normally cite EU instruments by their German abbreviations.  They
# have exactly the same surface form as a domestic statute reference, but must resolve
# to the CELEX Work rather than a fictitious ``de/gesetz/dsgvo`` node.  Keep this list
# deliberately small and unambiguous: these are official or universally established
# abbreviations, not learned aliases.
#
# Primary law belongs here as much as the regulations do, and its absence was the
# single biggest source of phantom German "laws" in the corpus: 1,491 references to
# ``de/gesetz/aeuv``, 753 to ``euv``, 421 to ``emrk``, 279 to ``grc``/``grch`` and
# 7,888 to ``eg`` — every one of them a real, held instrument the citation could not
# reach.  The candidate is the same consolidated-CELEX Work the English and French
# treaty grammars mint (``12016E`` TFEU, ``12016M`` TEU, ``12012P`` Charter,
# ``echr/convention``), so a German "Art. 267 AEUV" and an English "Article 267 TFEU"
# land on ONE node.  ``EG``/``EGV`` is the pre-Lisbon EC Treaty, whose articles were
# renumbered in 2009 — it therefore maps to its OWN Work (12002E), never to the TFEU,
# so "Art. 81 EG" cannot be silently read as today's Article 81.
_EU_LAW_IDS = {abbrev: inst.candidate_id
               for abbrev, inst in de_laws._BY_ABBREV.items()
               if not inst.candidate_id.startswith("de/")}
# Which of those are treaties rather than regulations/directives — the entity_kind the
# rest of the pipeline keys treatment and the relevance gate off.
_EU_TREATY_IDS = frozenset(inst.candidate_id for inst in de_laws.INSTRUMENTS
                           if inst.kind == "treaty")


def _eu_law_id(law: str) -> str | None:
    """The CELEX an abbreviation names, or None for a German (or unknown) statute."""
    inst = de_laws.resolve(law)
    if inst is None or inst.candidate_id.startswith("de/"):
        return None
    return inst.candidate_id


def law_id(abbreviation: str) -> str:
    return "de/gesetz/" + re.sub(r"[^a-z0-9]+", "", _fold_law(abbreviation))


def normalise_docket(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").upper()
    return re.sub(r"\s+", " ", value.replace("–", "-").replace("—", "-")).strip(" ,.;")


def case_alias(court: str, docket: str) -> str:
    """The key a German decision is cited by when it has no ECLI: court + Aktenzeichen.

    The court half must survive every spelling a citation uses, or the alias a judgment
    mints and the one the harvested decision registers never meet. ``de_courts.court_key``
    folds the full name, the abbreviation + seat and the ECLI court token onto one code —
    "OVG Münster", "OVG Nordrhein-Westfalen" and "Oberverwaltungsgericht
    Nordrhein-Westfalen" all key on ``OVGNRW``. A court the table does not know (a
    foreign court, a body named in prose) falls back to its stripped upper-case form,
    which is what this did for every court before the table existed.
    """
    from .de_courts import court_key as _court_key

    court_raw = re.sub(r"[^A-ZÄÖÜ0-9]+", "", (court or "").upper())
    court_key = _court_key(court) or court_raw
    docket_key = re.sub(r"[^A-Z0-9/.-]+", "", normalise_docket(docket))
    return f"de:case:{court_key}:{docket_key}"


# Deliberately bounded.  It starts on §/Art., accepts only provision vocabulary, and
# ends on a law abbreviation.  This avoids the unbounded legal-regex failure mode that
# previously wedged whole-corpus rescans.
_PARA = r"\d{1,5}[a-z]?"
# ``lit.`` (littera) is how German data-protection material points at a lettered point —
# "Art. 6 Abs. 1 lit. f DSGVO" is the single most common citation in the field — and its
# absence here did not merely lose the pinpoint: the pattern must run from § / Art. to a
# law abbreviation, so an unrecognised sub-clause in between made the whole reference
# unmatchable. Every "lit." citation in the corpus was invisible.
# ``UAbs.`` (Unterabsatz) is the subparagraph level, and it sits BETWEEN the two rungs
# this vocabulary already knew: the canonical long form of the most-cited provision in
# European data-protection law is "Art. 6 Abs. 1 UAbs. 1 Buchst. f DSGVO". Because the
# pattern must run unbroken from § / Art. to a law abbreviation, an unrecognised rung in
# the middle did not merely lose the pinpoint — it ended the match at "UAbs", which then
# read as the law, minting de/gesetz/uabs (and de/gesetz/unterabs) and losing the GDPR
# reference entirely. Same failure mode as "lit." before it.
_SUB = (r"(?:U(?:nter)?[Aa]bs(?:atz)?\.?|Abs(?:atz)?\.?|S(?:atz)?\.?|Nrn?\.?|Nummer|"
        r"Buchst(?:abe)?\.?|lit(?:t(?:era)?)?\.?|Alt(?:ernative)?\.?|"
        r"Halbs(?:atz)?\.?|HS\.?)"
        r"\s*(?:\d+[a-z]?|[a-z]|[IVX]+)")
_TAIL = rf"(?:\s*(?:{_SUB}|[,;]|und\b|oder\b|bis\s+{_PARA}|[-–—]\s*{_PARA}|f{{1,2}}\.))*"
_ONE = rf"(?:§§?|Art(?:ikel|\.)?)\s*{_PARA}{_TAIL}"
_COMPACT_ONE = rf"(?:§|Art(?:ikel|\.)?)\s*{_PARA}\s+(?:[IVX]{{1,4}}|\(\d+\))\s+\d+"
_IVM = rf"(?:\s+i\.?\s*V\.?\s*m\.?\s+{_ONE})?"
# The law abbreviation is matched CASE-SENSITIVELY (``(?-i:…)``) inside an otherwise
# case-insensitive pattern, because the rest of the reference is written both ways
# ("Art." / "art.", "Abs." / "abs.") but an abbreviation never is. Case-insensitivity
# here was minting a law out of the next word: "§ 8 Abs. 2 Nr. 1 MarkenG i.V.m. …" read
# the "i" as a book numeral ("MarkenG I" → de/gesetz/markeng1) and an English "v" in
# "BGB v Smith" the same way ("BGB V" → de/gesetz/bgb5) — thousands of phantom siblings
# of laws the corpus already holds. The book numeral itself is real (SGB V → sgb5, the
# key gesetze-im-internet uses), so it stays — but not before an ordinal point, which
# is an edition or a Halbsatz ("BGB 5. Aufl.", "BGB 1. Alt."), never a book.
# The internal hyphen is required, not cosmetic: German official usage writes the GDPR
# as DS-GVO at least as often as DSGVO, and the TTDSG, DS-GVO-Anpassungsgesetze and the
# Länder acts (LDSG-BW) all carry one. Without it the pattern stopped at the hyphen and
# minted de/gesetz/ds — a phantom law that collected every GDPR article in every German
# document. Only ONE hyphenated part is allowed, and it must look like an abbreviation
# continuation, so an ordinary compound noun cannot be swallowed.
_LAW = (r"(?-i:[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9]*(?:-[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9]*)?"
        r"(?:\s+(?:[IVX]{1,4}|\d{1,2}(?!\.)))?)")
LAW_REFERENCE_RE = re.compile(
    rf"(?P<raw>(?:{_COMPACT_ONE}|{_ONE}{_IVM})\s+(?P<law>{_LAW}))", re.IGNORECASE)

# German runs on capitalised nouns, and the pattern must END on a law — so when a
# reference carries no abbreviation at all, backtracking hands back whatever word sits
# where a law would ("§ 100 Absatz 1 Satz 1" → "Satz 1"; "§ 100a Rn" → "Rn"; a statute
# section heading "§ 4 Geltungsbereich" → "Geltungsbereich"). Two guards separate a real
# abbreviation from an ordinary word:
#
#   1. an abbreviation carries at least TWO capitals (BGB, ZPO, MarkenG, TzBfG, SGB V,
#      AEUV, EMRK); a German noun and the structural vocabulary carry exactly one;
#   2. an explicit stop-list for the apparatus that passes (1) — the Halbsatz "HS",
#      "aaO", and the law-report series a citation trails off into (NJW, BGHZ, SozR…).
# The German (and Austrian) law-report series. Shared, because the same tokens cause two
# different problems at two ends of the pipeline: a §-reference can trail INTO one and
# read it as the law ("§ 5 … NJW" → de/gesetz/njw), and a series citation has the same
# shape as a bracketless neutral citation and reads as a court ("BGHZ 174, 101" →
# bghz/2007/174 — see ``grammars._neutral_bracketless``). A report series is not a law
# and not a court.
LAW_REPORT_SERIES = frozenset({
    "njw", "njw-rr", "nza", "nza-rr", "nvwz", "nvwz-rr", "njoz", "nzs", "nzm", "nzi",
    "nstz", "nstz-rr", "grur", "grur-rr", "mdr", "wm", "zip", "dstr", "dstre",
    "bghz", "bghst", "bghr", "bverfge", "bage", "bfhe", "bsge", "bverwge", "sozr",
    "euzw", "eugrz", "versr", "dvbl", "bb", "db", "jz", "jr", "mmr", "cr", "k&r",
    "zd", "zum", "afp", "wrp", "gewarch", "npa", "efg", "dstrk", "rk", "beckrs",
})

_LAW_STOPWORDS = {
    # structural / pinpoint vocabulary
    "rn", "rn.", "rdn", "rdnr", "rdnrn", "rz", "ziff", "ziffer", "satz", "saetze",
    "abs", "absatz", "uabs", "uabsatz", "unterabs", "unterabsatz",
    "nr", "nrn", "nummer", "buchst", "buchstabe", "halbs", "hs",
    "alt", "alternative", "var", "variante", "unterabs", "unterabsatz", "spiegelstrich",
    "abschn", "abschnitt", "kap", "kapitel", "teil", "anl", "anlage", "anh", "fn",
    "art", "artikel", "s", "seite", "ff", "f",
    # citation apparatus
    "aao", "mwn", "vgl", "rspr", "juris", "beckrs", "az",
    # law-report series a reference can trail into
    *LAW_REPORT_SERIES,
}


def _is_law_abbreviation(law: str) -> bool:
    """Does this token read as a German law abbreviation rather than an ordinary word?

    See ``_LAW_STOPWORDS``: two capitals or more, and not a piece of citation apparatus."""
    stem = (law or "").split()[0] if (law or "").strip() else ""
    folded = re.sub(r"[^a-z]+", "", _fold_law(stem))
    if not stem or folded in _LAW_STOPWORDS:
        return False
    return sum(1 for ch in stem if ch.isupper()) >= 2


def _fold_law(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in folded if not unicodedata.combining(c)).casefold()


def _expand_compact(raw: str) -> str:
    """§ 19 IV 1 / § 19 (4) 1 → the explicit form bundesrecht canonicalises."""
    def _arabic(value: str) -> str:
        if value.isdigit():
            return value
        total, previous = 0, 0
        for ch in reversed(value.upper()):
            current = {"I": 1, "V": 5, "X": 10}[ch]
            total += current if current >= previous else -current
            previous = current
        return str(total)

    return re.sub(
        r"^((?:§|Art\.)\s*\d+[a-z]?)\s+(?:\((\d+)\)|([IVX]+))\s+(\d+)\s+",
        lambda m: f"{m.group(1)} Abs. {_arabic(m.group(2) or m.group(3))} Satz {m.group(4)} ",
        raw, flags=re.IGNORECASE,
    )


def _canonical_parts(canonical: str) -> tuple[str, str] | None:
    m = re.match(r"^(?P<prefix>§|Art\.)\s+(?P<body>.+?)\s+"
                 r"(?P<law>[A-ZÄÖÜ][\wÄÖÜäöüß]*(?:-[A-ZÄÖÜ][\wÄÖÜäöüß]*)?(?:\s+\d+)?)$",
                 canonical)
    if not m:
        return None
    return m.group("law"), f"{m.group('prefix')} {m.group('body')}"


def _eu_pinpoint(pinpoint: str) -> str:
    """Translate a German EU-law pinpoint to the Formex anchor vocabulary.

    Paragraphs and numbered definition points are useful article sub-anchors.  Sentence
    and alternative markers are retained in the raw citation but omitted here because
    EUR-Lex Formex does not expose stable sentence-level anchors consistently.
    """
    article = re.search(r"(?i)^Art(?:ikel|\.)?\s+(\d{1,3}[a-z]?)", pinpoint)
    if not article:
        return pinpoint
    out = f"Article {article.group(1)}"
    sub = re.search(r"(?i)\b(?:Abs(?:atz)?\.?|Nrn?\.?|Nummer)\s*(\d+[a-z]?)", pinpoint)
    if sub:
        out += f"({sub.group(1)})"
    # The lettered point — "lit. f" / "Buchst. a" — is a real Formex anchor level, and
    # in data-protection law it is usually the whole point of the citation: Article 6(1)
    # lists six lawful bases and only (f) is legitimate interests.
    letter = re.search(r"(?i)\b(?:lit(?:t(?:era)?)?\.?|Buchst(?:abe)?\.?)\s*([a-z])\b",
                       pinpoint)
    if letter:
        out += f"({letter.group(1).casefold()})"
    return out


def law_citations(text: str) -> list[Citation]:
    found: list[Citation] = []
    for match in LAW_REFERENCE_RE.finditer(text):
        raw = match.group("raw")
        # CEDH is the French abbreviation for the European Convention/Court, not a
        # German statute abbreviation. In French judgments ``§ 95, CEDH 19`` is a
        # Strasbourg paragraph/report marker; treating it as de/gesetz/cedh19 creates
        # a cross-jurisdiction phantom node. German texts use EMRK for the Convention.
        if re.match(r"CEDH\b", match.group("law"), re.IGNORECASE):
            continue
        # …and the same for the ordinary German word backtracking leaves behind when a
        # reference carries no abbreviation at all (see _is_law_abbreviation).
        if not _is_law_abbreviation(match.group("law")):
            continue
        try:
            canonical_refs = normalise(_expand_compact(raw))
        except (ValueError, TypeError):
            continue
        for canonical in dict.fromkeys(canonical_refs):
            parts = _canonical_parts(canonical)
            if not parts:
                continue
            law, pinpoint = parts
            # DS-GVO / DSGVO / DS-GVO are one instrument; fold the separators away
            # before the lookup so the spelling doesn't decide whether it resolves.
            eu_id = _eu_law_id(law)
            kind = "act"
            if eu_id:
                kind = "treaty" if eu_id in _EU_TREATY_IDS else "regulation"
            found.append(Citation(
                raw=raw, entity_kind=kind,
                candidate_id=eu_id or law_id(law),
                pinpoint=_eu_pinpoint(pinpoint) if eu_id else pinpoint,
                char_start=match.start(), char_end=match.end(),
                method="de_eu_article" if eu_id else "de_law_reference",
                confidence=1.0,
            ))
    return found


# Every German court, from the registry — the seven federal courts plus the Länder
# courts a citation names by abbreviation and seat ("OVG Münster", "VG Köln", "LAG
# Hamm", "AG Hünfeld"). The hand-kept list this replaced knew nine forms, so a
# Land court's decisions were invisible to the case grammar: the corpus's German case
# law was federal, and every citation of the courts BELOW them dangled.
_COURT = de_courts.COURT_RE_SOURCE
# Registers longest-first, so "StB" isn't cut short to "B" and "AnwZ" to "AR".
#
# The administrative, social and labour registers are as load-bearing as the civil ones
# now that the corpus holds those courts: a Verwaltungsgericht's main proceedings are
# "K", its interim relief "L", an appeal to the OVG "A" ("13 A 1234/20") and its interim
# relief "B"; the Sozialgerichte use "KR"/"AS"/"SO"/"AL"/"R"; the Arbeitsgerichte "Ca"
# (first instance), "Sa" (appeal), "BV"/"TaBV" (works-council proceedings); the criminal
# registers are "Ls", "KLs", "Ds", "Ns", "Qs", "Ss", "Ws", "OWi". Without them the
# docket half of a Länder citation matched nothing at all.
_REGISTER = (r"AnwZ|NotZ|EnZR|EnVR|TaBV|BvR|BvL|BvF|BvQ|BVR|AZR|ABR|KZR|KVR|StR|StB|"
             r"KLs|OWi|Sa|Ca|BV|Ns|Ds|Ls|Qs|Ss|Ws|KR|AS|SO|AL|VG|"
             r"ZR|ZB|ZA|AR|CN|R|C|B|W|U|L|K|O|A|F|S|T")
# The senate prefix is a number or a Roman numeral, and the Roman one may carry a
# lower-case letter — the BGH's VIa, IVa and XIa senates. Without it "BGH VIa ZR 335/21"
# lost its senate and became de:case:BGH:ZR335/21, merging VIa and IVa ZR 335/21 into one
# node. The lookbehind keeps a register letter from being read off the tail of a word:
# "SozR 4-1500" (the social-law report series) was minting de:case:BSG:ZR4-1500, the
# most-cited German "case" in the corpus.
_DOCKET = (rf"(?<![A-Za-zÄÖÜäöüß])(?:(?:\d+|[IVX]+[a-z]?)\s+)?(?:{_REGISTER})"
           r"\s+\d{1,6}(?:[./-]\d{1,4})+")
# What may NOT appear between the court and the docket. A German judgment's header lists
# the courts below it ("BGH … Beschluss vorgehend KG Berlin, … Az: 10 U 54/19"), so a
# window that steps over another court's name attributes ITS docket to the first court.
_OTHER_COURT = (r"\b(?:OLG|LG|AG|KG|OVG|VG|VGH|LSG|LAG|SG|FG|ArbG|ArbGG|BayObLG|BayVGH|"
                r"VerfGH|StGH|AnwGH|BVerfG|BGH|BAG|BFH|"
                r"BSG|BVerwG|BPatG|EuGH|EuG|EGMR)\b|vorgehend|nachgehend|Vorinstanz")
CASE_REFERENCE_RE = re.compile(
    rf"\b(?P<court>{_COURT})\b(?:(?!{_OTHER_COURT})[^;\n]){{0,80}}?(?P<docket>{_DOCKET})\b"
    rf"(?:\s*,?\s*[Rr]n\.?\s*(?P<rn>\d+(?:\s*(?:ff?\.|[-–,])\s*\d*)?))?")


def case_citations(text: str) -> list[Citation]:
    return [Citation(
        raw=m.group(0).strip(), entity_kind="case",
        candidate_id=case_alias(m.group("court"), m.group("docket")),
        pinpoint=f"Rn. {m.group('rn')}" if m.group("rn") else None,
        char_start=m.start(), char_end=m.end(), method="de_case_reference", confidence=0.95,
    ) for m in CASE_REFERENCE_RE.finditer(text)]


def german_citations(text: str) -> list[Citation]:
    """Every German citation in ``text``: §-anchored statutory references, decisions, and
    the instruments the judgment merely NAMES.

    The named-instrument pass runs last and is handed the spans the first two already
    claimed, so an instrument that was cited with a provision ("Art. 6 DSGVO") is not
    also reported as a bare mention of itself."""
    laws = law_citations(text)
    cases = case_citations(text)
    occupied = [(c.char_start, c.char_end) for c in laws + cases]
    return laws + cases + de_laws.instrument_citations(text, occupied=occupied)
