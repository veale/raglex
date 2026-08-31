"""Estonian citation grammar — the abbreviation IS the citation.

Estonia has the tidiest statutory citation practice in the corpus. Every act has an
official abbreviation formed from its title (karistusseadustik → **KarS**,
tsiviilkohtumenetluse seadustik → **TsMS**, võlaõigusseadus → **VÕS**), and a citation is
that abbreviation followed by the provision:

    KarS § 199 lg 2 p 1     →  ee/seadus/kars, anchor "§ 199 lg 2 p 1"
    IKÜM art 6 lg 1 p f     →  32016R0679, anchor "Article 6(1)(f)"

No number, no year, no inflection to undo — Estonian declines heavily but the
abbreviation never does. That makes the abbreviation the Work id, which is also the key
lahend.ee uses in its API (``get_law_section(abbrev='KarS', paragrahv='199')``), so the
grammar and the adapter agree without a translation table.

## Provision levels

``§`` → ``lg`` (lõige) → ``p`` (punkt) → ``lause``. A superscript section — ``§ 43¹`` —
is a **different provision** from ``§ 43``, not a subdivision of it, so it is normalised
to ``§ 43-1`` rather than being folded away. lahend.ee makes the same point in its own
tool description, having been bitten by it.

## Case numbers encode the proceeding, the year and the document

``3-25-3458/5`` is administrative case 3458 of 2025, document 5 in the file; ``1-25-7144/6``
is a criminal one. The leading digit is the type (1 criminal, 2 civil, 3 administrative,
4 misdemeanour), and the trailing ``/n`` is *which document of the case* — a first-instance
judgment and the appellate ruling in the same case differ only in that suffix. The id
therefore keeps the case number without the document suffix as an alias, so a citation of
the case reaches whichever document of it the corpus holds, while each document keeps its
own full number.

The pre-2010 Supreme Court used a longer form, ``3-2-1-45-12`` (chamber, division, kind,
number, year), which is still cited constantly and is matched separately.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Citation


def _fold(value: str) -> str:
    v = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in v if not unicodedata.combining(c)).casefold()


def law_id(abbrev: str) -> str:
    """"KarS" → ``ee/seadus/kars``."""
    return "ee/seadus/" + re.sub(r"[^a-z0-9]+", "", _fold(abbrev))


def act_key(name_or_abbrev: str) -> str:
    """An act named EITHER way → the one ``ee/seadus/<abbrev>`` id.

    lahend.ee returns the act of a cited provision sometimes as its abbreviation ("RÕS")
    and sometimes as its full title ("Halduskohtumenetluse seadustik"), in the same list.
    Minting an id from whichever arrived would file HKMS under two ids and split its
    citers between them — one from the structured index, the other from every judgment
    that wrote the abbreviation. The abbreviation wins because that is what a citation
    uses and what the grammar produces.
    """
    token = _clean_token(name_or_abbrev)
    if not token:
        return ""
    for abbrev in ACTS:
        if _fold(abbrev) == _fold(token):
            return law_id(abbrev)
    folded = _fold(token)
    for abbrev, title in ACTS.items():
        if _fold(title) == folded:
            return law_id(abbrev)
    return law_id(token)


def _clean_token(value: str) -> str:
    return " ".join(str(value or "").split())


def case_id(number: str) -> str:
    """"3-25-3458/5" → ``ee/lahend/3-25-3458/5``."""
    return "ee/lahend/" + re.sub(r"\s+", "", (number or "").strip())


def case_family_id(number: str) -> str:
    """The case WITHOUT its document suffix — the alias every document of it shares."""
    return case_id((number or "").split("/", 1)[0])


#: The acts an Estonian judgment cites, by their official abbreviations. Estonian
#: abbreviations are mixed-case and meaningful (``TsMS`` = tsiviilkohtumenetluse
#: seadustik), and they are matched case-INSENSITIVELY only where they are long enough
#: not to collide: ``PS`` and ``TS`` are two letters and are matched exactly as written.
ACTS: dict[str, str] = {
    "PS": "põhiseadus",
    "KarS": "karistusseadustik",
    "KrMS": "kriminaalmenetluse seadustik",
    "TsMS": "tsiviilkohtumenetluse seadustik",
    "TsÜS": "tsiviilseadustiku üldosa seadus",
    "VÕS": "võlaõigusseadus",
    "AÕS": "asjaõigusseadus",
    "PärS": "pärimisseadus",
    "PKS": "perekonnaseadus",
    "ÄS": "äriseadustik",
    "TLS": "töölepingu seadus",
    "HKMS": "halduskohtumenetluse seadustik",
    "HMS": "haldusmenetluse seadus",
    "HÕNTE": "hea õigusloome ja normitehnika eeskiri",
    "VTMS": "väärteomenetluse seadustik",
    "TMS": "täitemenetluse seadustik",
    "PankrS": "pankrotiseadus",
    "IKS": "isikuandmete kaitse seadus",
    "AvTS": "avaliku teabe seadus",
    "ESS": "elektroonilise side seadus",
    "InfoTS": "infoühiskonna teenuse seadus",
    "AutÕS": "autoriõiguse seadus",
    "KaMS": "kaubamärgiseadus",
    "KonkS": "konkurentsiseadus",
    "TKS": "tarbijakaitseseadus",
    "RÕS": "riigi õigusabi seadus",
    "RLS": "riigilõivuseadus",
    "MKS": "maksukorralduse seadus",
    "TuMS": "tulumaksuseadus",
    "KMS": "käibemaksuseadus",
    "PlanS": "planeerimisseadus",
    "EhS": "ehitusseadustik",
    "KeÜS": "keskkonnaseadustiku üldosa seadus",
    "VangS": "vangistusseadus",
    "KorS": "korrakaitseseadus",
    "VSS": "väljasõidukohustuse ja sissesõidukeelu seadus",
    "VMS": "välismaalaste seadus",
    "RHS": "riigihangete seadus",
    "KOKS": "kohaliku omavalitsuse korralduse seadus",
    "ATS": "avaliku teenistuse seadus",
    "LiS": "liiklusseadus",
    "KindlTS": "kindlustustegevuse seadus",
}
#: Two-letter abbreviations are matched case-SENSITIVELY: "PS" is the Constitution and
#: also an ordinary pair of letters, and this pass runs over every language in the corpus.
_SHORT = {a for a in ACTS if len(a) <= 2}

#: EU instruments, as Estonian names them. Estonian translates the acquis and abbreviates
#: the result — the GDPR is *isikuandmete kaitse üldmäärus*, cited as **IKÜM**.
EU_INSTRUMENTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("32016R0679", "regulation",
     ("isikuandmete kaitse üldmäärus", "IKÜM", "GDPR", "määrus (EL) 2016/679")),
    ("32022R2065", "regulation", ("digiteenuste määrus", "DSA")),
    ("32022R1925", "regulation", ("digiturgude määrus", "DMA")),
    ("32024R1689", "regulation", ("tehisintellekti määrus", "AI Act")),
    ("32022L2555", "directive", ("NIS2 direktiiv", "NIS2", "küberturvalisuse direktiiv")),
    ("32002L0058", "directive", ("eraelu puutumatuse ja elektroonilise side direktiiv",)),
    ("32000L0031", "directive", ("e-kaubanduse direktiiv",)),
    ("32019L0790", "directive", ("autoriõiguse direktiiv", "DSM direktiiv")),
    ("32005L0029", "directive", ("ebaausate kaubandustavade direktiiv",)),
    ("32011L0083", "directive", ("tarbija õiguste direktiiv",)),
    ("32014R0910", "regulation", ("eIDAS määrus", "eIDAS")),
    ("12016E", "treaty", ("Euroopa Liidu toimimise leping", "ELTL")),
    ("12016M", "treaty", ("Euroopa Liidu leping", "ELL")),
    ("12012P", "treaty", ("Euroopa Liidu põhiõiguste harta", "põhiõiguste harta", "harta")),
    ("echr/convention", "treaty",
     ("Euroopa inimõiguste ja põhivabaduste kaitse konventsioon", "EIÕK")),
)
_EU_BY_NAME = {_fold(n): (celex, kind)
               for celex, kind, names in EU_INSTRUMENTS for n in names}
_EU_ALT = "|".join(re.escape(n) for n in sorted(
    {n for _c, _k, names in EU_INSTRUMENTS for n in names}, key=len, reverse=True))

#: An acronym this short is also an ordinary string in some other language, and this pass
#: runs over the whole corpus rather than only over Estonian documents. "DSA" is a duty
#: solicitor advice scheme in an English judgment and "DMA" is a French marketing
#: syndicate; both were minting confident references to EU regulations. So anything at or
#: below this length has to be accompanied by a word of its own language — the same guard
#: ``de_laws`` applies with the German article and ``grammars`` with the English
#: determiner. Longer names ("isikuandmete kaitse üldmäärus") are distinctive on their own.
_NEEDS_CONTEXT_LEN = 5
#: The vocabulary that says "this sentence is about an instrument", in Estonian.
_CONTEXT_RE = re.compile(r"\b(?:määrus\w*|maarus\w*|direktiiv\w*|leping\w*|artikkel|artikli\w*|art\.|harta|konventsioon\w*|kohaselt|alusel|tähenduses|liidu|euroopa|kohus)\b", re.IGNORECASE)
#: How far either side to look. One clause, not one page: an instrument named in the
#: previous sentence does not license every acronym in this one.
_CONTEXT_WINDOW = 60


def _needs_context(name: str) -> bool:
    return len(re.sub(r"[^0-9A-Za-z\u00c0-\u024f]+", "", name or "")) <= _NEEDS_CONTEXT_LEN


def _has_context(text: str, start: int, end: int) -> bool:
    window = text[max(0, start - _CONTEXT_WINDOW):start] + " " + text[end:end + _CONTEXT_WINDOW]
    return bool(_CONTEXT_RE.search(window))


# --- patterns -----------------------------------------------------------------
#: "§ 199 lg 2 p 1" — and the superscript form, which Estonian writes either as a real
#: superscript (§ 43¹) or as a hyphenated ASCII fallback (§ 43-1).
_SECTION = r"\d{1,4}(?:[¹²³⁴⁵⁶⁷⁸⁹]|-\d)?"
#: The level names, **longest first**. Python's alternation is first-match, not
#: longest-match, so a short name listed before the longer one it prefixes wins and the
#: trailing ``[a-z]?`` swallows the next letter of the word it just truncated:
#:
#:   ``punkti f``  → ``p`` + ``u``  → "pu",     and the EU anchor became Article 6(1)(**u**)
#:   ``lõiget 1``  → ``lõige`` + ``t``          and the "1" was never consumed, so the
#:                                              pinpoint decayed to the bare section
#:
#: 103,298 held Estonian edges carried an anchor mangled this way — 96,380 lõiked whose
#: number was dropped, 6,757 "pu", and 161 citations of a GDPR point (u) that does not
#: exist. All three are one ordering mistake, and each produced a confident wrong anchor
#: rather than no anchor, which is why none of them showed up as a failure.
_LEVEL = (r"(?:lõikes|lõiget|lõike|lõige|lg|punktis|punkti|punkt|p|lauses|lause|"
          r"ls|jj)\s*\.?\s*\d{0,3}[a-z]?")
_PROVISION = rf"§{{1,2}}\s*{_SECTION}(?:\s+{_LEVEL})*"
#: "KarS § 199 lg 2" — the abbreviation leads, which is the ordinary Estonian order.
LAW_REFERENCE_RE = re.compile(
    r"(?<![\wÕÄÖÜõäöü])(?P<abbrev>" +
    "|".join(re.escape(a) for a in sorted(ACTS, key=len, reverse=True)) +
    rf")\s+(?P<provision>{_PROVISION})")
#: …and the trailing order, which appears when the provision is quoted first:
#: "§ 121 lg 1 HKMS".
LAW_TRAILING_RE = re.compile(
    rf"(?<![\w])(?P<provision>{_PROVISION})\s+(?P<abbrev>" +
    "|".join(re.escape(a) for a in sorted(ACTS, key=len, reverse=True)) +
    r")(?![\wÕÄÖÜõäöü])")
#: The act named in full, in any of its cases — Estonian declines the title but not the
#: abbreviation, so the stem is matched and the ending left free.
_ACT_NAMES = "|".join(re.escape(n) for n in sorted(
    {n for n in ACTS.values()}, key=len, reverse=True))
ACT_NAME_RE = re.compile(rf"(?<![\wÕÄÖÜõäöü])(?P<name>{_ACT_NAMES})[a-zõäöü]{{0,4}}"
                         rf"(?![\wÕÄÖÜõäöü])", re.IGNORECASE)
_ACT_BY_NAME = {_fold(name): abbrev for abbrev, name in ACTS.items()}
#: "IKÜM art 6 lg 1 p f" / "ELTL artikkel 267".
EU_ARTICLE_RE = re.compile(
    rf"(?P<name>{_EU_ALT})\s+(?P<provision>art(?:ikkel|ikli|iklis|ikkel|\.)?\s*"
    rf"\d{{1,3}}[a-z]?(?:\s+{_LEVEL})*)", re.IGNORECASE)
EU_NAME_RE = re.compile(rf"(?<![\wÕÄÖÜõäöü])(?P<name>{_EU_ALT})(?![\wÕÄÖÜõäöü])")
#: "3-25-3458/5" — the modern case number, with its document suffix.
CASE_RE = re.compile(r"(?<![\w-])(?P<case>[1-4]-\d{2}-\d{1,6})(?:/(?P<doc>\d{1,3}))?(?![\w-])")
#: "3-2-1-45-12" — the pre-2010 Supreme Court form, still cited constantly.
OLD_CASE_RE = re.compile(r"(?<![\w-])(?P<case>3-\d-1-\d{1,4}-\d{2})(?![\w-])")


def _anchor(provision: str) -> str:
    """One spelling per provision. The superscript is preserved as ``-1``: ``§ 43¹`` is a
    different section from ``§ 43``, and folding the marker away merges two provisions."""
    text = " ".join((provision or "").split())
    for sup, digit in zip("¹²³⁴⁵⁶⁷⁸⁹", "123456789"):
        text = text.replace(sup, f"-{digit}")
    text = re.sub(r"(?i)\bl(?:g|õi(?:ge|get|kes|ke))\b\.?", "lg", text)
    text = re.sub(r"(?i)\bp(?:unkt(?:i|is)?)\b\.?", "p", text)
    text = re.sub(r"(?i)\bl(?:ause|auses)\b|\bls\b\.?", "lause", text)
    text = re.sub(r"§\s*", "§ ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _eu_anchor(provision: str) -> str | None:
    article = re.search(r"(?i)art(?:ikkel|ikli|iklis|\.)?\s*(\d{1,3}[a-z]?)", provision or "")
    if not article:
        return None
    out = f"Article {article.group(1)}"
    if sub := re.search(r"(?i)\bl(?:g|õi(?:ge|get|kes|ke))\.?\s*(\d{1,3})", provision):
        out += f"({sub.group(1)})"
    if point := re.search(r"(?i)\bp(?:unkt(?:i|is)?)?\.?\s*([a-z]|\d{1,3})\b", provision):
        out += f"({point.group(1).casefold()})"
    return out


def law_citations(text: str) -> list[Citation]:
    body = text or ""
    found: list[Citation] = []
    spans: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return not any(s < end and start < e for s, e in spans)

    def add(cite: Citation) -> None:
        spans.append((cite.char_start, cite.char_end))
        found.append(cite)

    for m in EU_ARTICLE_RE.finditer(body):
        hit = _EU_BY_NAME.get(_fold(m.group("name")))
        if not hit or not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group(0), entity_kind=hit[1], candidate_id=hit[0],
                     pinpoint=_eu_anchor(m.group("provision")),
                     char_start=m.start(), char_end=m.end(),
                     method="ee_eu_article", confidence=1.0))
    for pattern, method in ((LAW_REFERENCE_RE, "ee_law_reference"),
                            (LAW_TRAILING_RE, "ee_law_reference")):
        for m in pattern.finditer(body):
            abbrev = m.group("abbrev")
            # A two-letter abbreviation is matched exactly as Estonia writes it; folded,
            # "PS" would fire on the English "ps" and on half the acronyms in the corpus.
            if abbrev in _SHORT and m.group("abbrev") != abbrev:
                continue
            if not free(m.start(), m.end()):
                continue
            add(Citation(raw=m.group(0), entity_kind="act", candidate_id=law_id(abbrev),
                         pinpoint=_anchor(m.group("provision")),
                         char_start=m.start(), char_end=m.end(),
                         method=method, confidence=1.0))
    for m in ACT_NAME_RE.finditer(body):
        abbrev = _ACT_BY_NAME.get(_fold(m.group("name")))
        if not abbrev or not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group(0), entity_kind="act", candidate_id=law_id(abbrev),
                     pinpoint=None, char_start=m.start(), char_end=m.end(),
                     method="ee_act_name", confidence=0.9))
    for m in EU_NAME_RE.finditer(body):
        name = m.group("name")
        hit = _EU_BY_NAME.get(_fold(name))
        if not hit or not free(m.start(), m.end()):
            continue
        if _needs_context(name) and not _has_context(body, m.start(), m.end()):
            continue
        add(Citation(raw=m.group(0), entity_kind=hit[1], candidate_id=hit[0], pinpoint=None,
                     char_start=m.start(), char_end=m.end(),
                     method="ee_eu_instrument", confidence=0.85))
    return found


def case_citations(text: str) -> list[Citation]:
    body = text or ""
    found: list[Citation] = []
    spans: list[tuple[int, int]] = []
    for m in OLD_CASE_RE.finditer(body):
        spans.append((m.start(), m.end()))
        found.append(Citation(
            raw=m.group(0), entity_kind="case", candidate_id=case_id(m.group("case")),
            pinpoint=None, char_start=m.start(), char_end=m.end(),
            method="ee_case_old", confidence=0.95))
    for m in CASE_RE.finditer(body):
        if any(s <= m.start() and m.end() <= e for s, e in spans):
            continue
        number = m.group("case") + (f"/{m.group('doc')}" if m.group("doc") else "")
        found.append(Citation(
            raw=m.group(0), entity_kind="case", candidate_id=case_id(number),
            pinpoint=None, char_start=m.start(), char_end=m.end(),
            method="ee_case", confidence=0.9))
    return found


ESTONIAN_METHODS: frozenset[str] = frozenset({
    "ee_law_reference", "ee_act_name", "ee_eu_article", "ee_eu_instrument",
    "ee_case", "ee_case_old",
})


def estonian_citations(text: str) -> list[Citation]:
    """Every Estonian citation in ``text``: acts by abbreviation or title, EU instruments,
    and decisions by case number."""
    return law_citations(text) + case_citations(text)
