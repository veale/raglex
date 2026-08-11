"""Swedish citation grammar — the SFS number, the balk, and a case name in quotation marks.

Sweden identifies a statute by its **SFS number** (Svensk författningssamling), which a
citation prints in brackets inside the name: "lagen (1960:729) om upphovsrätt till
litterära och konstnärliga verk". Domstolsverket's case-law API publishes the same number
as a field of its own (``lagrumLista[].sfsNummer``), so one key serves both:

    56 a § lagen (1960:729) om upphovsrätt   →  se/sfs/1960/729, anchor "56 a §"
    2 kap. 3 § brottsbalken                  →  se/sfs/1962/700, anchor "2 kap. 3 §"

## The balkar are named, never numbered

The six codes — brottsbalken, rättegångsbalken, föräldrabalken, ärvdabalken, jordabalken,
miljöbalken — and the four constitutional laws are cited by name alone, in the definite
form Swedish uses for them. So are the everyday acts (avtalslagen, köplagen,
marknadsföringslagen, konkurrenslagen, dataskyddslagen). Each is one SFS number, and the
table below maps the name onto it. Swedish is only lightly inflected here — the definite
suffix is part of the name — so an exact fold is enough, unlike Finnish or Slovak.

## Provision levels

``kap.`` (chapter) → ``§`` → ``stycket`` (paragraph, written as an ordinal:
"första stycket") → ``punkten``. The chapter comes FIRST, which is the opposite of the
German and Austrian order, and it is part of the anchor: "2 kap. 3 §" and "3 §" are
different provisions of the same act.

## Case citations are report references, and the report is the identifier

Sweden has **no ECLI**. The Supreme Court's decisions are cited as "NJA 2020 s. 123"
(page) or "NJA 1991:47" (running number); the Supreme Administrative Court as "HFD 2021
ref. 12" and, before 2011, "RÅ 2009 ref. 45"; the appellate courts as "RH 2018:23", the
Labour Court as "AD 2019 nr 5", the Land and Environment Court of Appeal as "MÖD 2020:12".
Those strings are the only stable identifiers Swedish practice has, so they are the ids —
``se/nja/2020/123`` — and the adapter registers them as aliases of the decisions it holds.

Domstolsverket also publishes the Supreme Court's own **quoted case names** ("Sökordslistan",
"Innovationen", "Pärmen"), which is what practitioners actually say. Those are recorded as
document titles by the adapter rather than matched here: a quoted noun is not a citation
shape, and treating one as such would link every quotation in the corpus.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Citation


def _fold(value: str) -> str:
    v = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in v if not unicodedata.combining(c)).casefold()


def act_id(year: str | int, number: str | int) -> str:
    """"1960:729" → ``se/sfs/1960/729``."""
    return f"se/sfs/{int(year)}/{int(number)}"


def case_id(series: str, year: str | int, number: str) -> str:
    """"NJA 2020 s. 123" → ``se/nja/2020/123``."""
    return f"se/{_fold(series).replace(' ', '')}/{int(year)}/{str(number).strip().lower()}"


#: Statute name (definite form, folded) → its SFS number.
ACTS: dict[str, tuple[int, int]] = {
    "brottsbalken": (1962, 700),
    "rattegangsbalken": (1942, 740),
    "foraldrabalken": (1949, 381),
    "arvdabalken": (1958, 637),
    "jordabalken": (1970, 994),
    "miljobalken": (1998, 808),
    "socialforsakringsbalken": (2010, 110),
    "regeringsformen": (1974, 152),
    "tryckfrihetsforordningen": (1949, 105),
    "yttrandefrihetsgrundlagen": (1991, 1469),
    "riksdagsordningen": (2014, 801),
    "avtalslagen": (1915, 218),
    "koplagen": (1990, 931),
    "konsumentkoplagen": (2022, 260),
    "konsumenttjanstlagen": (1985, 716),
    "marknadsforingslagen": (2008, 486),
    "konkurrenslagen": (2008, 579),
    "aktiebolagslagen": (2005, 551),
    "upphovsrattslagen": (1960, 729),
    "varumarkeslagen": (2010, 1877),
    "patentlagen": (1967, 837),
    "skadestandslagen": (1972, 207),
    "preskriptionslagen": (1981, 130),
    "forvaltningslagen": (2017, 900),
    "forvaltningsprocesslagen": (1971, 291),
    "offentlighets- och sekretesslagen": (2009, 400),
    "dataskyddslagen": (2018, 218),
    "kamerabevakningslagen": (2018, 1200),
    "lagen om elektronisk kommunikation": (2022, 482),
    "personuppgiftslagen": (1998, 204),
    "utlanningslagen": (2005, 716),
    "plan- och bygglagen": (2010, 900),
    "konkurslagen": (1987, 672),
    "utsokningsbalken": (1981, 774),
    "lagen om offentlig upphandling": (2016, 1145),
    "diskrimineringslagen": (2008, 567),
    "arbetsmiljolagen": (1977, 1160),
    "lagen om anstallningsskydd": (1982, 80),
    "medbestammandelagen": (1976, 580),
    "inkomstskattelagen": (1999, 1229),
    "mervardesskattelagen": (2023, 200),
    "skatteforfarandelagen": (2011, 1244),
}
#: The initialisms, case-SENSITIVE: "RF", "RB" and "MB" are ordinary strings otherwise.
ACT_ABBREVS: dict[str, tuple[int, int]] = {
    "BrB": (1962, 700), "RB": (1942, 740), "FB": (1949, 381), "ÄB": (1958, 637),
    "JB": (1970, 994), "MB": (1998, 808), "RF": (1974, 152), "TF": (1949, 105),
    "YGL": (1991, 1469), "AvtL": (1915, 218), "KöpL": (1990, 931),
    "MFL": (2008, 486), "KL": (2008, 579), "ABL": (2005, 551), "URL": (1960, 729),
    "SkL": (1972, 207), "FL": (2017, 900), "FPL": (1971, 291),
    "OSL": (2009, 400), "PuL": (1998, 204), "UB": (1981, 774), "LAS": (1982, 80),
    "MBL": (1976, 580), "IL": (1999, 1229), "LOU": (2016, 1145), "PBL": (2010, 900),
}

#: EU instruments as Swedish names them. Sweden compounds the name into one definite noun
#: — the GDPR is *dataskyddsförordningen* — and uses the English acronym alongside.
EU_INSTRUMENTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("32016R0679", "regulation",
     ("dataskyddsförordningen", "allmänna dataskyddsförordningen", "GDPR",
      "förordning (EU) 2016/679", "förordningen (EU) 2016/679")),
    ("32022R2065", "regulation",
     ("förordningen om digitala tjänster", "rättsakten om digitala tjänster", "DSA")),
    ("32022R1925", "regulation",
     ("förordningen om digitala marknader", "rättsakten om digitala marknader", "DMA")),
    ("32024R1689", "regulation", ("AI-förordningen", "förordningen om artificiell intelligens")),
    ("32022L2555", "directive", ("NIS2-direktivet", "NIS 2-direktivet")),
    ("32002L0058", "directive", ("e-dataskyddsdirektivet", "direktivet om integritet och "
                                 "elektronisk kommunikation")),
    ("32000L0031", "directive", ("e-handelsdirektivet",)),
    ("32019L0790", "directive", ("DSM-direktivet", "upphovsrättsdirektivet")),
    ("32005L0029", "directive", ("direktivet om otillbörliga affärsmetoder",)),
    ("32011L0083", "directive", ("konsumenträttighetsdirektivet",)),
    ("32014R0910", "regulation", ("eIDAS-förordningen", "eIDAS")),
    ("12016E", "treaty", ("fördraget om Europeiska unionens funktionssätt", "FEUF")),
    ("12016M", "treaty", ("fördraget om Europeiska unionen", "FEU")),
    ("12012P", "treaty", ("Europeiska unionens stadga om de grundläggande rättigheterna",
                          "rättighetsstadgan")),
    ("echr/convention", "treaty", ("Europakonventionen", "EKMR")),
)
_EU_BY_NAME = {_fold(n): (celex, kind)
               for celex, kind, names in EU_INSTRUMENTS for n in names}
_EU_ALT = "|".join(re.escape(n) for n in sorted(
    {n for _c, _k, names in EU_INSTRUMENTS for n in names}, key=len, reverse=True))

#: An acronym this short is also an ordinary string in some other language, and this pass
#: runs over the whole corpus rather than only over Swedish documents. "DSA" is a duty
#: solicitor advice scheme in an English judgment and "DMA" is a French marketing
#: syndicate; both were minting confident references to EU regulations. So anything at or
#: below this length has to be accompanied by a word of its own language — the same guard
#: ``de_laws`` applies with the German article and ``grammars`` with the English
#: determiner. Longer names ("dataskyddsförordningen") are distinctive on their own.
_NEEDS_CONTEXT_LEN = 5
#: The vocabulary that says "this sentence is about an instrument", in Swedish.
_CONTEXT_RE = re.compile(r"\b(?:förordning\w*|forordning\w*|direktiv\w*|fördrag\w*|fordrag\w*|artikel|artiklar\w*|stadgan|konventionen|enligt|unionen|europeisk\w*|EU-domstolen)\b", re.IGNORECASE)
#: How far either side to look. One clause, not one page: an instrument named in the
#: previous sentence does not license every acronym in this one.
_CONTEXT_WINDOW = 60


def _needs_context(name: str) -> bool:
    return len(re.sub(r"[^0-9A-Za-z\u00c0-\u024f]+", "", name or "")) <= _NEEDS_CONTEXT_LEN


def _has_context(text: str, start: int, end: int) -> bool:
    window = text[max(0, start - _CONTEXT_WINDOW):start] + " " + text[end:end + _CONTEXT_WINDOW]
    return bool(_CONTEXT_RE.search(window))


# --- patterns -----------------------------------------------------------------
#: "2 kap. 3 § andra stycket 1" — the chapter leads, the stycke is an ordinal word.
_ORDINAL = (r"första|andra|tredje|fjärde|femte|sjätte|sjunde|åttonde|nionde|tionde")
_LEVEL = (rf"(?:(?:{_ORDINAL})\s+stycket|\d+\s*(?:st\.?|stycket)|"
          r"(?:punkten?|p\.)\s*\d+|\d+\s*(?:punkten?|p\.))")
_PROVISION = (r"(?:\d{1,3}\s*(?:kap\.?|kapitlet)\s+)?"
              r"\d{1,4}\s*[a-z]?\s*§{1,2}"
              rf"(?:\s+{_LEVEL})*")
PROVISION_RE = re.compile(_PROVISION)
#: "(1960:729)" — the SFS number as a citation prints it.
SFS_RE = re.compile(r"\(\s*(?P<year>(?:1[6-9]|20)\d{2})\s*:\s*(?P<number>\d{1,5})\s*\)")
#: A provision of an act carrying its SFS number: "56 a § lagen (1960:729) om …".
SFS_PROVISION_RE = re.compile(
    rf"(?P<raw>(?P<provision>{_PROVISION})\s+[^()\n]{{0,60}}?"
    r"\(\s*(?P<year>(?:1[6-9]|20)\d{2})\s*:\s*(?P<number>\d{1,5})\s*\))")
#: A provision of a NAMED act: "2 kap. 3 § brottsbalken".
_ACT_ALT = "|".join(re.escape(n) for n in sorted(ACTS, key=len, reverse=True))
NAMED_PROVISION_RE = re.compile(
    rf"(?P<raw>(?P<provision>{_PROVISION})\s+(?:i\s+)?(?P<name>{_ACT_ALT}))",
    re.IGNORECASE)
ACT_NAME_RE = re.compile(rf"(?<![\w])(?P<name>{_ACT_ALT})(?![\w])", re.IGNORECASE)
ABBREV_PROVISION_RE = re.compile(
    rf"(?P<raw>(?P<provision>{_PROVISION})\s+(?P<abbrev>" +
    "|".join(re.escape(a) for a in sorted(ACT_ABBREVS, key=len, reverse=True)) +
    r")(?![\w]))")
#: "artikel 6.1 f i dataskyddsförordningen" — Sweden subdivides an EU article with DOTS,
#: which no other language in the corpus does, and drops the brackets entirely.
EU_ARTICLE_RE = re.compile(
    rf"(?P<raw>artikel\s+(?P<article>\d{{1,3}}[a-z]?)"
    rf"(?:\.(?P<para>\d{{1,3}}))?(?:\s*(?P<point>[a-z])\b)?"
    rf"(?:\s+(?:i|av)\s+)?\s*(?P<name>{_EU_ALT}))", re.IGNORECASE)
EU_NAME_RE = re.compile(rf"(?<![\w])(?P<name>{_EU_ALT})(?![\w-])")
#: The report series a Swedish decision is cited by. ``s.`` is a page, ``ref.``/``not.``
#: the Supreme Administrative Court's referat/notis split, ``nr`` the Labour Court's.
CASE_RE = re.compile(
    r"(?<![\w])(?P<series>NJA|HFD|RÅ|RH|AD|MÖD|MD|MIG|PMÖD|PMD|HD|AD)\s+"
    r"(?P<year>(?:19|20)\d{2})\s*"
    r"(?:s\.\s*(?P<page>\d{1,4})|ref\.?\s*(?P<ref>\d{1,4})|not\.?\s*(?P<not>\d{1,4})"
    r"|nr\s*(?P<nr>\d{1,4})|:\s*(?P<num>\d{1,4}))(?![\w])")
#: "T 8371-24", "Ö 4337-25", "B 9488-25", "PMÄ 5160-25" — the målnummer, which is what the
#: courts' own registers key on and what Domstolsverket publishes.
MALNUMMER_RE = re.compile(
    r"(?<![\w-])(?P<register>PMÄ|PMT|ÖÄ|ÖM|Ö|T|B|M|P|F|Ä|UM|UB)\s"
    r"(?P<number>\d{1,6})-(?P<year>\d{2})(?![\w-])")


def _anchor(provision: str) -> str:
    """One spelling per provision: "2 kap 3 §", "2 kap. 3§" → ``2 kap. 3 §``."""
    text = " ".join((provision or "").split())
    text = re.sub(r"(?i)\bkap(?:itlet|\.)?", "kap.", text)
    # "§§" is one marker (a range of sections), not two. Spacing them individually turned
    # "3 §§" into the anchor "3 § §", which matches no provision anyone cites.
    text = re.sub(r"\s*(§{1,2})", r" \1", text)
    text = re.sub(r"(?i)\bst\.(?=\s|$)", "stycket", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _eu_anchor(m: re.Match[str]) -> str:
    out = f"Article {m.group('article')}"
    if m.group("para"):
        out += f"({m.group('para')})"
    if m.group("point"):
        out += f"({m.group('point').casefold()})"
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
        add(Citation(raw=m.group("raw"), entity_kind=hit[1], candidate_id=hit[0],
                     pinpoint=_eu_anchor(m), char_start=m.start(), char_end=m.end(),
                     method="se_eu_article", confidence=1.0))
    for m in SFS_PROVISION_RE.finditer(body):
        if not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group("raw"), entity_kind="act",
                     candidate_id=act_id(m.group("year"), m.group("number")),
                     pinpoint=_anchor(m.group("provision")),
                     char_start=m.start(), char_end=m.end(),
                     method="se_sfs_provision", confidence=1.0))
    for m in NAMED_PROVISION_RE.finditer(body):
        hit = ACTS.get(_fold(m.group("name")))
        if not hit or not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group("raw"), entity_kind="act", candidate_id=act_id(*hit),
                     pinpoint=_anchor(m.group("provision")),
                     char_start=m.start(), char_end=m.end(),
                     method="se_act_provision", confidence=0.95))
    for m in ABBREV_PROVISION_RE.finditer(body):
        if not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group("raw"), entity_kind="act",
                     candidate_id=act_id(*ACT_ABBREVS[m.group("abbrev")]),
                     pinpoint=_anchor(m.group("provision")),
                     char_start=m.start(), char_end=m.end(),
                     method="se_act_abbrev", confidence=0.9))
    for m in ACT_NAME_RE.finditer(body):
        hit = ACTS.get(_fold(m.group("name")))
        if not hit or not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group(0), entity_kind="act", candidate_id=act_id(*hit),
                     pinpoint=None, char_start=m.start(), char_end=m.end(),
                     method="se_act_name", confidence=0.9))
    for m in EU_NAME_RE.finditer(body):
        name = m.group("name")
        hit = _EU_BY_NAME.get(_fold(name))
        if not hit or not free(m.start(), m.end()):
            continue
        if _needs_context(name) and not _has_context(body, m.start(), m.end()):
            continue
        add(Citation(raw=m.group(0), entity_kind=hit[1], candidate_id=hit[0], pinpoint=None,
                     char_start=m.start(), char_end=m.end(),
                     method="se_eu_instrument", confidence=0.85))
    # A bare "(1960:729)" that no provision reached — the act introduced in parentheses
    # after its name. Only counted when the words before it name a statute, because a
    # year:number pair in brackets is also a docket, a page range and a Bible verse.
    for m in SFS_RE.finditer(body):
        head = _fold(body[max(0, m.start() - 40):m.start()])
        if not re.search(r"(lag|lagen|förordning|forordning|förordningen|forordningen|"
                         r"balk|balken|sfs)\s*$", head):
            continue
        if not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group(0), entity_kind="act",
                     candidate_id=act_id(m.group("year"), m.group("number")),
                     pinpoint=None, char_start=m.start(), char_end=m.end(),
                     method="se_sfs_number", confidence=0.9))
    return found


def case_citations(text: str) -> list[Citation]:
    body = text or ""
    found: list[Citation] = []
    for m in CASE_RE.finditer(body):
        number = (m.group("page") or m.group("ref") or m.group("not")
                  or m.group("nr") or m.group("num"))
        if not number:
            continue
        found.append(Citation(
            raw=m.group(0), entity_kind="case",
            candidate_id=case_id(m.group("series"), m.group("year"), number),
            pinpoint=None, char_start=m.start(), char_end=m.end(),
            method="se_report_citation", confidence=0.95))
    claimed = [(c.char_start, c.char_end) for c in found]
    for m in MALNUMMER_RE.finditer(body):
        if any(s <= m.start() and m.end() <= e for s, e in claimed):
            continue
        mark = f"{m.group('register')} {m.group('number')}-{m.group('year')}"
        found.append(Citation(
            raw=m.group(0), entity_kind="case", candidate_id=f"se:mal:{mark}",
            pinpoint=None, char_start=m.start(), char_end=m.end(),
            method="se_malnummer", confidence=0.8))
    return found


SWEDISH_METHODS: frozenset[str] = frozenset({
    "se_sfs_provision", "se_act_provision", "se_act_abbrev", "se_act_name",
    "se_sfs_number", "se_eu_article", "se_eu_instrument",
    "se_report_citation", "se_malnummer",
})


def swedish_citations(text: str) -> list[Citation]:
    """Every Swedish citation in ``text``: statutes by SFS number or name, EU articles,
    and decisions by report reference or målnummer."""
    return law_citations(text) + case_citations(text)
