"""Slovak citation grammar — the Zbierka zákonov, the spisová značka, and the EU acquis.

Slovakia cites a statute by its **collection number**, not by a short title: "zákon č.
514/2003 Z. z." is the Act published as item 514 of the 2003 Zbierka zákonov, and that
pair is the identifier Slov-Lex uses in its ELI URIs (``/SK/ZZ/2003/514``). The Ministry
of Justice's decision API publishes exactly that string in ``odkazovanePredpisy``, so the
same key serves the structured index and the running text:

    zákon č. 514/2003 Z. z.   →  sk/zz/2003/514
    § 9 ods. 1 písm. a)       →  the anchor on that Work

Practitioners also use short names for the codes — Občiansky zákonník, Trestný zákon,
Zákonník práce, Civilný sporový poriadok — and each of those is one specific Zbierka
number, so the table below maps the name onto the same ``sk/zz/…`` id the numeric form
mints. Without it, half the citations in a Slovak judgment would name a code the corpus
holds and reach nothing.

## Provision levels

``§`` → ``ods.`` (odsek, paragraph) → ``písm.`` (písmeno, lettered point) → ``bod``
(numbered point). Slovak keeps the abbreviating full stop that Austrian usage drops, and
puts the lettered point in brackets: "§ 9 ods. 1 písm. a)". Constitutional and EU
references use ``čl.`` (článok) instead of ``§``.

## Case numbers announce their register, not their court

A spisová značka is ``<senát><register>/<číslo>/<rok>`` — "6S/74/2018", "2Tdo/78/2024",
"16Co/166/2018". The register letters are the proceeding type (``C`` civil, ``Co`` civil
appeal, ``Cdo`` civil cassation, ``T`` criminal, ``To`` criminal appeal, ``Tdo`` criminal
cassation, ``S``/``Sž`` administrative, ``Ob``/``Obdo`` commercial), and — unlike Austria
— they do **not** identify the court: the same mark is used at district, regional and
Supreme Court level. The number that does identify a case uniquely is the
``identifikacneCislo`` (the ten-digit file id), which is also the body of the ECLI. So a
bare spisová značka is minted as a court-qualified key only where the text names the
court; otherwise it stays a file-mark key that the adapter registers for its own
decisions.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Citation


def _fold(value: str) -> str:
    v = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in v if not unicodedata.combining(c)).casefold()


def act_id(year: str | int, number: str | int) -> str:
    """"514/2003 Z. z." → ``sk/zz/2003/514`` — the Slov-Lex ELI, flattened."""
    return f"sk/zz/{int(year)}/{int(number)}"


def eli_to_id(eli: str) -> tuple[str, str | None] | None:
    """A Slov-Lex ELI fragment → ``(work id, anchor)``.

    ``odkazovanePredpisy`` publishes references as ELI paths with the provision in the
    fragment: ``/SK/ZZ/2005/300/#paragraf-221.odsek-3.pismeno-a``. That is a fully
    resolved statutory citation — act, section, paragraph and lettered point — and it is
    the single most valuable field the Slovak API returns.
    """
    m = re.match(r"^/?SK/ZZ/(?P<year>\d{4})/(?P<number>\d+)(?:/#(?P<anchor>.+))?$",
                 (eli or "").strip(), re.IGNORECASE)
    if not m:
        return None
    work = act_id(m.group("year"), m.group("number"))
    anchor = _anchor_from_fragment(m.group("anchor") or "")
    return work, anchor


_FRAGMENT_LEVEL = {"paragraf": "§", "clanok": "čl.", "odsek": "ods.",
                   "pismeno": "písm.", "bod": "bod", "veta": "veta", "cast": "časť"}


def _anchor_from_fragment(fragment: str) -> str | None:
    """``paragraf-221.odsek-3.pismeno-a`` → ``§ 221 ods. 3 písm. a``."""
    parts: list[str] = []
    for chunk in (fragment or "").split("."):
        m = re.match(r"^([a-z]+)-(.+)$", _fold(chunk))
        if not m:
            continue
        level = _FRAGMENT_LEVEL.get(m.group(1))
        if not level:
            continue
        parts.append(f"{level} {m.group(2)}" if level != "§" else f"§ {m.group(2)}")
    return " ".join(parts) or None


# --- the codes, by the names practitioners actually write ---------------------
#: Short name (as Slovak judgments write it) → its Zbierka zákonov number. Only the
#: instruments whose number is unambiguous and settled; a name that has been re-enacted
#: under a new number (the three 2015 procedural codes replacing the OSP) lists both, so
#: an older judgment still resolves to the code it was applying.
CODES: dict[str, tuple[int, int]] = {
    "ustava slovenskej republiky": (1992, 460),
    "ustava sr": (1992, 460),
    "ustava": (1992, 460),
    "obciansky zakonnik": (1964, 40),
    "obchodny zakonnik": (1991, 513),
    "zakonnik prace": (2001, 311),
    "trestny zakon": (2005, 300),
    "trestny poriadok": (2005, 301),
    "obciansky sudny poriadok": (1963, 99),
    "civilny sporovy poriadok": (2015, 160),
    "civilny mimosporovy poriadok": (2015, 161),
    "spravny sudny poriadok": (2015, 162),
    "spravny poriadok": (1967, 71),
    "zakon o ochrane osobnych udajov": (2018, 18),
    "zakon o slobodnom pristupe k informaciam": (2000, 211),
    "zakon o elektronickych komunikaciach": (2022, 452),
    "autorsky zakon": (2015, 185),
    "zakon o ochrane spotrebitela": (2024, 108),
    "zakon o priestupkoch": (1990, 372),
    "zakon o konkurze a restrukturalizacii": (2005, 7),
    "zakon o sudoch": (2004, 757),
    "exekucny poriadok": (1995, 233),
    "zakon o spravnom konani": (1967, 71),
    "zakon o e-governmente": (2013, 305),
}
#: The initialisms the same codes are cited by. Kept apart from ``CODES`` because they
#: are matched case-SENSITIVELY: "TP" is the Trestný poriadok and also an ordinary
#: two-letter string, and "ZP" is the Zákonník práce and also a docket prefix.
CODE_ABBREVS: dict[str, tuple[int, int]] = {
    "OZ": (1964, 40), "ObZ": (1991, 513), "ZP": (2001, 311),
    "TZ": (2005, 300), "TP": (2005, 301), "OSP": (1963, 99),
    "CSP": (2015, 160), "CMP": (2015, 161), "SSP": (2015, 162),
    "SP": (1967, 71), "EP": (1995, 233), "AZ": (2015, 185),
    "ZoOOÚ": (2018, 18), "ZSPI": (2000, 211), "ZEK": (2022, 452),
}

# --- EU instruments, as a Slovak court names them ------------------------------
#: Slovak translates the digital acquis rather than transliterating it, so the acronym a
#: Slovak judgment uses is usually the English one while the *name* is Slovak. Both are
#: listed: "akt o digitálnych službách" and "DSA" are the same regulation.
EU_INSTRUMENTS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("32016R0679", "regulation", "GDPR",
     ("všeobecné nariadenie o ochrane údajov", "všeobecného nariadenia o ochrane údajov",
      "nariadenie (EÚ) 2016/679", "nariadenia (EÚ) 2016/679", "GDPR")),
    ("32022R2065", "regulation", "DSA",
     ("akt o digitálnych službách", "aktu o digitálnych službách",
      "nariadenie o digitálnych službách", "DSA")),
    ("32022R1925", "regulation", "DMA",
     ("akt o digitálnych trhoch", "aktu o digitálnych trhoch",
      "nariadenie o digitálnych trhoch", "DMA")),
    ("32024R1689", "regulation", "AI Act",
     ("akt o umelej inteligencii", "nariadenie o umelej inteligencii", "AI Act")),
    ("32022L2555", "directive", "NIS 2",
     ("smernica NIS 2", "smernice NIS 2", "NIS2", "NIS 2")),
    ("32002L0058", "directive", "ePrivacy",
     ("smernica o súkromí a elektronických komunikáciách", "smernica ePrivacy")),
    ("32000L0031", "directive", "e-Commerce",
     ("smernica o elektronickom obchode", "smernice o elektronickom obchode")),
    ("32019L0790", "directive", "DSM",
     ("smernica o autorskom práve na digitálnom jednotnom trhu",)),
    ("32014R0910", "regulation", "eIDAS", ("nariadenie eIDAS", "eIDAS")),
    ("32005L0029", "directive", "UCPD",
     ("smernica o nekalých obchodných praktikách",)),
    ("32011L0083", "directive", "CRD", ("smernica o právach spotrebiteľov",)),
    ("12016E", "treaty", "ZFEÚ",
     ("Zmluva o fungovaní Európskej únie", "Zmluvy o fungovaní Európskej únie", "ZFEÚ")),
    ("12016M", "treaty", "ZEÚ",
     ("Zmluva o Európskej únii", "Zmluvy o Európskej únii", "ZEÚ")),
    ("12012P", "treaty", "Charta",
     ("Charta základných práv Európskej únie", "Charty základných práv Európskej únie")),
    ("echr/convention", "treaty", "EDĽP",
     ("Dohovor o ochrane ľudských práv a základných slobôd", "EDĽP", "EĽDP")),
)
_EU_BY_NAME: dict[str, tuple[str, str]] = {
    _fold(name): (celex, kind)
    for celex, kind, _label, names in EU_INSTRUMENTS for name in names}
#: Longest-first so "akt o digitálnych trhoch" is not shadowed by a shorter phrase, and
#: the bare acronyms are matched case-sensitively — "DSA" is also an ordinary token.
_EU_NAMES = "|".join(re.escape(n) for n in sorted(
    {n for _c, _k, _l, names in EU_INSTRUMENTS for n in names if not n.isupper()},
    key=len, reverse=True))
_EU_ACRONYMS = "|".join(re.escape(n) for n in sorted(
    {n for _c, _k, _l, names in EU_INSTRUMENTS for n in names if n.isupper()},
    key=len, reverse=True))

#: An acronym this short is also an ordinary string in some other language, and this pass
#: runs over the whole corpus rather than only over Slovak documents. "DSA" is a duty
#: solicitor advice scheme in an English judgment and "DMA" is a French marketing
#: syndicate; both were minting confident references to EU regulations. So anything at or
#: below this length has to be accompanied by a word of its own language — the same guard
#: ``de_laws`` applies with the German article and ``grammars`` with the English
#: determiner. Longer names ("akt o digitálnych službách") are distinctive on their own.
_NEEDS_CONTEXT_LEN = 5
#: The vocabulary that says "this sentence is about an instrument", in Slovak.
_CONTEXT_RE = re.compile(r"\b(?:nariaden\w*|smernic\w*|akt\w*|zmluv\w*|chart\w*|dohovor\w*|článk\w*|čl\.|podľa|zmysle|súlade|ustanoven\w*|právo|práva|únie|európsk\w*|EÚ)\b", re.IGNORECASE)
#: How far either side to look. One clause, not one page: an instrument named in the
#: previous sentence does not license every acronym in this one.
_CONTEXT_WINDOW = 60


def _needs_context(name: str) -> bool:
    return len(re.sub(r"[^0-9A-Za-z\u00c0-\u024f]+", "", name or "")) <= _NEEDS_CONTEXT_LEN


def _has_context(text: str, start: int, end: int) -> bool:
    window = text[max(0, start - _CONTEXT_WINDOW):start] + " " + text[end:end + _CONTEXT_WINDOW]
    return bool(_CONTEXT_RE.search(window))


# --- patterns -----------------------------------------------------------------
#: "§ 9 ods. 1 písm. a) bod 2" / "čl. 46 ods. 2". The lettered point's closing bracket is
#: part of Slovak typography, not of the reference.
_LEVEL = (r"(?:ods\.?|písm\.?|pism\.?|bod|body|vety?|vete|časti?|casti?)\s*"
          r"[\dA-Za-zÁÄČĎÉÍĽĹŇÓÔŔŠŤÚÝŽáäčďéíľĺňóôŕšťúýž]{1,4}\)?")
_PROVISION = rf"(?:§{{1,2}}|čl\.?|clanok|článok)\s*\d{{1,4}}[a-z]?(?:\s*{_LEVEL})*"
#: "zákona č. 514/2003 Z. z." — the collection reference, in any of its declensions.
_ZZ = (r"z[áa]kon(?:a|om|u|e|y|ov)?\s+č\.?\s*(?P<number>\d{1,4})\s*/\s*(?P<year>\d{4})"
       r"(?:\s*Z\.?\s*z\.?)?")
LAW_REFERENCE_RE = re.compile(
    rf"(?P<raw>(?P<provision>{_PROVISION})\s+{_ZZ})", re.IGNORECASE)
#: The same act named without a provision, which is how a judgment introduces it.
ACT_RE = re.compile(rf"(?P<raw>{_ZZ})", re.IGNORECASE)
#: "§ 9 ods. 1 Občianskeho zákonníka" — a provision of a named code.
#:
#: Slovak declines: the code named "Občiansky zákonník" in the nominative is "Občianskeho
#: zákonníka" in the genitive that a citation always uses, and "Trestného poriadku" for
#: "Trestný poriadok". Matching the dictionary form finds nothing at all, and matching a
#: fixed stem is guesswork about which syllables survive. So the words after the provision
#: are captured generically and resolved by comparing STEMS — see ``_match_code``.
CODE_REFERENCE_RE = re.compile(
    rf"(?P<raw>(?P<provision>{_PROVISION})\s+"
    rf"(?P<code>(?:[A-Za-zÁÄČĎÉÍĽĹŇÓÔŔŠŤÚÝŽáäčďéíľĺňóôŕšťúýž]+\s+){{0,3}}"
    rf"[A-Za-zÁÄČĎÉÍĽĹŇÓÔŔŠŤÚÝŽáäčďéíľĺňóôŕšťúýž]+))")

#: Slovak endings run one to three characters ("-y"/"-eho", "-ok"/"-ku", "-ík"/"-íka"), so
#: two words are the same word when they agree on everything but the last three letters of
#: the dictionary form. Trimming a FIXED three from both fails, because the two forms are
#: different lengths: "občianskeho"[:-3] is "občiansk" and "občiansky"[:-3] is "občian".
#: Comparing the common prefix against the dictionary word's length is what makes the
#: declension irrelevant.
_MIN_STEM = 4


def _same_word(declined: str, dictionary: str) -> bool:
    a, b = _fold(declined), _fold(dictionary)
    shared = 0
    for x, y in zip(a, b):
        if x != y:
            break
        shared += 1
    return shared >= max(_MIN_STEM, len(b) - 3)


def _match_code(phrase: str) -> tuple[tuple[int, int], int] | None:
    """The code this (declined) phrase names, and how many of its words were used.

    Only a PREFIX of the captured words is tried, because the code name begins
    immediately after the provision — "§ 420 Občianskeho zákonníka a ďalšie" names the
    civil code in its first two words and says nothing in the rest. Longest first, so
    "Trestného poriadku" is not settled as "Trestný zákon" plus a stray word.
    """
    words = [w for w in (phrase or "").split() if w]
    for size in range(min(len(words), 4), 0, -1):
        head = words[:size]
        for name, number in CODES.items():
            name_words = name.split()
            if len(name_words) != size:
                continue
            if all(_same_word(w, n) for w, n in zip(head, name_words)):
                return number, size
    return None
CODE_ABBREV_RE = re.compile(
    r"(?P<raw>(?P<provision>(?:§{1,2}|čl\.?)\s*\d{1,4}[a-z]?"
    r"(?:\s*(?:ods\.?|písm\.?|bod)\s*[\dA-Za-z]{1,4}\)?)*)\s+"
    r"(?P<abbrev>" + "|".join(re.escape(a) for a in sorted(
        CODE_ABBREVS, key=len, reverse=True)) + r")(?![\w]))")
#: "čl. 6 ods. 1 písm. f) GDPR" / "článku 15 nariadenia (EÚ) 2016/679".
EU_REFERENCE_RE = re.compile(
    rf"(?P<raw>(?P<provision>(?:čl\.?|článk(?:u|om|y|och)?|clank\w*)\s*\d{{1,3}}[a-z]?"
    rf"(?:\s*{_LEVEL})*)\s+(?P<name>{_EU_NAMES}|{_EU_ACRONYMS}))", re.IGNORECASE)
EU_NAME_RE = re.compile(rf"(?<![\w])(?:(?P<name>{_EU_NAMES})|(?P<acronym>{_EU_ACRONYMS}))"
                        r"(?![\w])")
#: "6S/74/2018", "2Tdo/78/2024" — senate, register, number, year.
_REGISTER = (r"Sžfk|Sžrk|Sžhpu|Sžsk|Sžik|Sžik|Obdo|Cdo|Tdo|Sždo|Ndob|Ncp|Csp|Cpr|CoPr|"
             r"Cob|Cbi|Cbs|Cob|Sžk|Sžf|Sžo|Sžr|Sžz|Sža|Sžd|Ntd|Nds|Ndc|Ndt|Cob|"
             r"Co|Cb|Ca|Ce|Cd|Er|Nc|Nt|Ps|Po|Pp|Rob|Sa|So|Sp|To|Tp|Tk|Tost|Up|"
             r"C|S|T|P|E|R|M|K|D|B")
FILE_MARK_RE = re.compile(
    rf"(?<![\w/])(?P<senate>\d{{1,3}})(?P<register>{_REGISTER})/"
    rf"(?P<number>\d{{1,6}})/(?P<year>\d{{4}})(?![\w/])")
#: "ECLI:SK:NSSR:2025:6322010282.1" — Slovakia mints one per decision, and it is the
#: only identifier that is unique across courts.
#: The trailing character class must END on a word character. Slovak ECLIs carry an
#: internal dot ("…:6322010282.1") and so does the sentence they sit in; a greedy
#: ``[\w.]+`` swallowed the full stop and minted an id no decision has.
ECLI_RE = re.compile(r"(?<![\w])ECLI:SK:[A-Z0-9]{2,10}:\d{4}:\w(?:[\w.]*\w)?(?![\w])")


def _pinpoint(provision: str) -> str:
    """Normalise a Slovak provision string to one spelling.

    Slovak writes the same reference as "§ 9 ods. 1 písm. a)", "§9 ods.1 pism. a)" and
    "§ 9 odsek 1 písmeno a)". They are one anchor; storing three splits the provision's
    citers three ways. The trailing bracket goes with them — it is typography.
    """
    text = " ".join((provision or "").split())
    text = re.sub(r"(?i)\bods(?:ek|eku|ekom)?\.?", "ods.", text)
    text = re.sub(r"(?i)\bp[ií]sm(?:eno|ena|enom)?\.?", "písm.", text)
    text = re.sub(r"(?i)\bčl(?:ánok|ánku|ánkom|anok|anku)?\.?", "čl.", text)
    text = re.sub(r"(?i)(§{1,2}|čl\.)\s*", lambda m: m.group(1) + " ", text)
    text = re.sub(r"(?i)\b(ods\.|písm\.|bod)\s*", lambda m: m.group(1) + " ", text)
    return re.sub(r"\s{2,}", " ", text).replace(")", "").strip()


def _eu_anchor(provision: str) -> str | None:
    """"čl. 6 ods. 1 písm. f)" → ``Article 6(1)(f)`` — the Formex anchor the corpus stores."""
    article = re.search(r"(?i)(?:čl\.?|článk\w*|clank\w*)\s*(\d{1,3}[a-z]?)", provision or "")
    if not article:
        return None
    out = f"Article {article.group(1)}"
    if sub := re.search(r"(?i)\bods(?:ek|eku)?\.?\s*(\d+[a-z]?)", provision):
        out += f"({sub.group(1)})"
    if letter := re.search(r"(?i)\bp[ií]sm(?:eno)?\.?\s*([a-z])\)?", provision):
        out += f"({letter.group(1).casefold()})"
    return out


def _word_end(text: str, start: int, words: int) -> int:
    """The offset just past the ``words``-th whitespace-separated word from ``start``."""
    at = start
    for _ in range(words):
        while at < len(text) and text[at].isspace():
            at += 1
        while at < len(text) and not text[at].isspace():
            at += 1
    return at


def case_id(file_mark: str) -> str:
    """"6S/74/2018" → ``sk:case:6S/74/2018``. Not court-qualified — see the docstring."""
    return "sk:case:" + re.sub(r"\s+", "", file_mark or "")


def law_citations(text: str) -> list[Citation]:
    body = text or ""
    found: list[Citation] = []
    spans: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return not any(s < end and start < e for s, e in spans)

    def add(cite: Citation) -> None:
        spans.append((cite.char_start, cite.char_end))
        found.append(cite)

    for m in EU_REFERENCE_RE.finditer(body):
        hit = _EU_BY_NAME.get(_fold(m.group("name")))
        if not hit or not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group("raw"), entity_kind=hit[1], candidate_id=hit[0],
                     pinpoint=_eu_anchor(m.group("provision")),
                     char_start=m.start(), char_end=m.end(),
                     method="sk_eu_article", confidence=1.0))
    for m in LAW_REFERENCE_RE.finditer(body):
        if not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group("raw"), entity_kind="act",
                     candidate_id=act_id(m.group("year"), m.group("number")),
                     pinpoint=_pinpoint(m.group("provision")),
                     char_start=m.start(), char_end=m.end(),
                     method="sk_law_reference", confidence=1.0))
    # Scanned by hand rather than with ``finditer``: the code capture reaches four words
    # ahead, so a greedy match consumes the START of the next citation even though the
    # code name ended two words earlier. "§ 12 Obchodného zákonníka a čl. 152 Ústavy…"
    # swallowed the "čl" and the constitutional reference beside it disappeared. Resuming
    # at the end of the NAME, not the end of the regex match, is the whole fix.
    at = 0
    while (m := CODE_REFERENCE_RE.search(body, at)) is not None:
        hit = _match_code(m.group("code"))
        if hit is None:
            at = m.start("code")
            at = _word_end(body, at, 1)
            continue
        (year, number), used = hit
        # The capture reaches up to four words ahead so a multi-word code name fits; the
        # citation ends where the NAME ends, not where the greedy capture did, or the
        # span claims the next clause and blocks whatever cites in it.
        end = _word_end(body, m.start("code"), used)
        at = end
        if not free(m.start(), end):
            continue
        add(Citation(raw=body[m.start():end], entity_kind="act",
                     candidate_id=act_id(year, number),
                     pinpoint=_pinpoint(m.group("provision")),
                     char_start=m.start(), char_end=end,
                     method="sk_code_reference", confidence=0.95))
    for m in CODE_ABBREV_RE.finditer(body):
        if not free(m.start(), m.end()):
            continue
        year, number = CODE_ABBREVS[m.group("abbrev")]
        add(Citation(raw=m.group("raw"), entity_kind="act", candidate_id=act_id(year, number),
                     pinpoint=_pinpoint(m.group("provision")),
                     char_start=m.start(), char_end=m.end(),
                     method="sk_code_abbrev", confidence=0.9))
    for m in ACT_RE.finditer(body):
        if not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group("raw"), entity_kind="act",
                     candidate_id=act_id(m.group("year"), m.group("number")),
                     pinpoint=None, char_start=m.start(), char_end=m.end(),
                     method="sk_act_name", confidence=0.95))
    for m in EU_NAME_RE.finditer(body):
        name = m.group("name") or (m.groupdict().get("acronym") or "")
        hit = _EU_BY_NAME.get(_fold(name))
        if not hit or not free(m.start(), m.end()):
            continue
        if _needs_context(name) and not _has_context(body, m.start(), m.end()):
            continue
        add(Citation(raw=m.group(0), entity_kind=hit[1], candidate_id=hit[0], pinpoint=None,
                     char_start=m.start(), char_end=m.end(),
                     method="sk_eu_instrument", confidence=0.85))
    return found


def case_citations(text: str) -> list[Citation]:
    body = text or ""
    found = [Citation(raw=m.group(0), entity_kind="case", candidate_id=m.group(0).upper(),
                      pinpoint=None, char_start=m.start(), char_end=m.end(),
                      method="sk_ecli", confidence=1.0)
             for m in ECLI_RE.finditer(body)]
    claimed = [(c.char_start, c.char_end) for c in found]
    for m in FILE_MARK_RE.finditer(body):
        if any(s <= m.start() and m.end() <= e for s, e in claimed):
            continue
        mark = f"{m.group('senate')}{m.group('register')}/{m.group('number')}/{m.group('year')}"
        found.append(Citation(raw=m.group(0), entity_kind="case", candidate_id=case_id(mark),
                              pinpoint=None, char_start=m.start(), char_end=m.end(),
                              method="sk_file_mark", confidence=0.85))
    return found


SLOVAK_METHODS: frozenset[str] = frozenset({
    "sk_law_reference", "sk_code_reference", "sk_code_abbrev", "sk_act_name",
    "sk_eu_article", "sk_eu_instrument", "sk_file_mark", "sk_ecli",
})


def slovak_citations(text: str) -> list[Citation]:
    """Every Slovak citation in ``text``: statutes by collection number or code name,
    EU instruments, and decisions by ECLI or spisová značka."""
    return law_citations(text) + case_citations(text)
