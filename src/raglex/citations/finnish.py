"""Finnish citation grammar — the säädösnumero, the pykälä, and an agglutinative name.

Finland identifies a statute by the year and running number of its publication —
``1050/2018`` is the Tietosuojalaki — and Finlex's Akoma Ntoso URIs are built on exactly
that pair (``/akn/fi/act/statute/2018/1050``). So the number is the id:

    tietosuojalain 5 §:n 1 momentti   →  fi/act/2018/1050, anchor "5 § 1 mom."
    laki 1050/2018                    →  fi/act/2018/1050

## Why a name table is unavoidable, and why matching it is not a lookup

Finnish judgments cite by name far more than by number, and the name is **inflected and
compounded**: takaisinsaantilaki appears as *takaisinsaantilain*, *takaisinsaantilaissa*,
*takaisinsaantilakia*; the section marker itself declines — ``5 §:n``, ``5 §:ssä``,
``5 §:ää``. There is no dictionary form to match. The table below therefore maps the
**stem** (everything up to the case ending) onto the säädösnumero, and the matcher
compares stems rather than words — the same problem Slovak declension poses, in a
language with fifteen cases instead of six.

The names listed are the ones the corpus is for: data protection, electronic
communications, publicity of official documents, consumer protection, copyright, plus the
codes and procedural acts every judgment cites.

## Provision levels

``§`` (pykälä) → ``momentti`` (mom.) → ``kohta`` → ``alakohta``, and ``luku`` (chapter)
above the section for the codes. EU instruments use ``artikla`` in place of the pykälä,
with ``kohta``/``alakohta`` beneath it — so "6 artiklan 1 kohdan f alakohta" is
``Article 6(1)(f)``.

## Decisions carry their court in the citation

``KKO:2024:1`` (Supreme Court), ``KHO:2023:45`` (Supreme Administrative Court),
``HelHO:2024:12`` (Helsinki Court of Appeal), ``MAO:123/24`` (Market Court), ``TT
2024:61`` (Labour Court), ``VakO 890/2023`` (Insurance Court). The prefix is the court, so
a citation resolves without context — which is what lets the year/number pair be the id.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Citation


def _fold(value: str) -> str:
    v = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in v if not unicodedata.combining(c)).casefold()


def act_id(year: str | int, number: str | int) -> str:
    """"1050/2018" → ``fi/act/2018/1050`` — the Finlex work, flattened from its AKN URI."""
    return f"fi/act/{int(year)}/{int(number)}"


def case_id(court: str, year: str | int, number: str) -> str:
    """"KKO:2024:1" → ``fi/kko/2024/1``."""
    return f"fi/{(court or '').casefold()}/{int(year)}/{str(number).strip().lower()}"


#: Act name (nominative, folded) → its säädösnumero. Matched by STEM — see ``_stem``.
ACTS: dict[str, tuple[int, int]] = {
    "tietosuojalaki": (2018, 1050),
    "henkilotietolaki": (1999, 523),
    "laki sahkoisen viestinnan palveluista": (2014, 917),
    # Finnish legislative drafting names an act by what it was enacted about, in a
    # participial construction: "takaisinsaannista konkurssipesään annettu laki". That
    # is the form judgments use in running text — far more often than the nominative
    # short title — and its word order is the reverse of the dictionary entry, so it
    # cannot be reached by inflecting the name above.
    "sahkoisen viestinnan palveluista annettu laki": (2014, 917),
    "takaisinsaannista konkurssipesaan annettu laki": (1991, 758),
    "yrityksen saneerauksesta annettu laki": (1993, 47),
    "viranomaisten toiminnan julkisuudesta annettu laki": (1999, 621),
    "digitaalisten palvelujen tarjoamisesta annettu laki": (2019, 306),
    "oikeudenkaynnista hallintoasioissa annettu laki": (2019, 808),
    "julkisista hankinnoista ja kayttooikeussopimuksista annettu laki": (2016, 1397),
    "sahkoisen viestinnan palvelulaki": (2014, 917),
    "tietoyhteiskuntakaari": (2014, 917),
    "julkisuuslaki": (1999, 621),
    "laki viranomaisten toiminnan julkisuudesta": (1999, 621),
    "tiedonhallintalaki": (2019, 906),
    "laki digitaalisten palvelujen tarjoamisesta": (2019, 306),
    "hallintolaki": (2003, 434),
    "laki oikeudenkaynnista hallintoasioissa": (2019, 808),
    "hallintolainkayttolaki": (1996, 586),
    "perustuslaki": (1999, 731),
    "rikoslaki": (1889, 39),
    "oikeudenkaymiskaari": (1734, 4),
    "esitutkintalaki": (2011, 805),
    "pakkokeinolaki": (2011, 806),
    "poliisilaki": (2011, 872),
    "tyosopimuslaki": (2001, 55),
    "yhdenvertaisuuslaki": (2014, 1325),
    "tasa-arvolaki": (1986, 609),
    "kuluttajansuojalaki": (1978, 38),
    "tekijanoikeuslaki": (1961, 404),
    "tavaramerkkilaki": (2019, 544),
    "patenttilaki": (1967, 550),
    "kilpailulaki": (2011, 948),
    "vahingonkorvauslaki": (1974, 412),
    "kauppakaari": (1734, 3),
    "konkurssilaki": (2004, 120),
    "takaisinsaantilaki": (1991, 758),
    "laki takaisinsaannista konkurssipesaan": (1991, 758),
    "yrityssaneerauslaki": (1993, 47),
    "laki yrityksen saneerauksesta": (1993, 47),
    "ulosottokaari": (2007, 705),
    "osakeyhtiolaki": (2006, 624),
    "maankayttojarakennuslaki": (1999, 132),
    "ymparistonsuojelulaki": (2014, 527),
    "ulkomaalaislaki": (2004, 301),
    "sosiaalihuoltolaki": (2014, 1301),
    "hankintalaki": (2016, 1397),
}
#: The initialisms, matched case-SENSITIVELY: "PL" and "OK" are also ordinary strings.
ACT_ABBREVS: dict[str, tuple[int, int]] = {
    "PL": (1999, 731), "RL": (1889, 39), "OK": (1734, 4), "HL": (2003, 434),
    "TSL": (2001, 55), "KSL": (1978, 38), "TekijäL": (1961, 404),
    "JulkL": (1999, 621), "KonkL": (2004, 120), "TakSL": (1991, 758),
    "YSL": (2014, 527), "MRL": (1999, 132), "OYL": (2006, 624), "UK": (2007, 705),
    "HOL": (2019, 808), "ETL": (2011, 805), "PKL": (2011, 806),
}

#: EU instruments as Finnish names them. Finnish translates rather than borrows —
#: the DSA is the *digipalvelusäädös* — so the Finnish name and the English acronym are
#: both listed for each instrument.
EU_INSTRUMENTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("32016R0679", "regulation",
     ("yleinen tietosuoja-asetus", "tietosuoja-asetus", "GDPR",
      "Euroopan parlamentin ja neuvoston asetus (EU) 2016/679", "asetus (EU) 2016/679")),
    ("32022R2065", "regulation", ("digipalvelusäädös", "digipalveluasetus", "DSA")),
    ("32022R1925", "regulation", ("digimarkkinasäädös", "digimarkkina-asetus", "DMA")),
    ("32024R1689", "regulation", ("tekoälysäädös", "tekoälyasetus")),
    ("32022L2555", "directive", ("verkko- ja tietoturvadirektiivi", "NIS2", "NIS 2")),
    ("32002L0058", "directive", ("sähköisen viestinnän tietosuojadirektiivi",)),
    ("32000L0031", "directive", ("sähkökauppadirektiivi", "verkkokauppadirektiivi")),
    ("32019L0790", "directive", ("DSM-direktiivi", "tekijänoikeusdirektiivi")),
    ("32005L0029", "directive", ("sopimattomia kaupallisia menettelyjä koskeva direktiivi",)),
    ("32011L0083", "directive", ("kuluttajanoikeusdirektiivi",)),
    ("32014R0910", "regulation", ("eIDAS-asetus", "eIDAS")),
    ("12016E", "treaty", ("Euroopan unionin toiminnasta tehty sopimus", "SEUT")),
    ("12016M", "treaty", ("sopimus Euroopan unionista", "SEU")),
    ("12012P", "treaty", ("Euroopan unionin perusoikeuskirja", "perusoikeuskirja")),
    ("echr/convention", "treaty",
     ("Euroopan ihmisoikeussopimus", "ihmisoikeussopimus", "EIS")),
)
_EU_BY_NAME = {_fold(n): (celex, kind)
               for celex, kind, names in EU_INSTRUMENTS for n in names}
_EU_STEMS = tuple(sorted(
    ((_fold(n), celex, kind) for celex, kind, names in EU_INSTRUMENTS for n in names),
    key=lambda row: len(row[0]), reverse=True))

# --- patterns -----------------------------------------------------------------
_WORD = r"[A-Za-zÅÄÖåäö][A-Za-zÅÄÖåäö0-9-]*"
#: "5 §:n 1 momentin 2 kohta" — the section marker itself takes a case ending, which is
#: why the ``§`` is followed by an optional ``:``-suffix rather than by whitespace alone.
_LEVEL = (r"(?:\d+[a-z]?\s*(?:mom(?:entti|entin|entissa|entissä|\.)?|kohd(?:an|assa|at|ta)?|"
          r"alakohd(?:an|assa|at|ta)?|luv(?:un|ussa)|luku))")
_PYKALA = r"\d{1,4}\s*[a-z]?\s*§(?::\w{1,4})?"
PROVISION_RE = re.compile(
    rf"(?P<provision>(?:\d{{1,3}}\s*luvun?\s+)?{_PYKALA}(?:\s+{_LEVEL})*)")
#: "(1050/2018)" or "laki 1050/2018" — the säädösnumero, with or without its parentheses.
NUMBER_RE = re.compile(
    r"(?<![\w/])\(?\s*(?P<number>\d{1,4})\s*/\s*(?P<year>(?:1[6-9]|20)\d{2})\s*\)?(?![\w/])")
#: The act named before its provision: "takaisinsaantilain 2 §:n 3 momentissa".
NAMED_PROVISION_RE = re.compile(
    rf"(?P<name>(?:{_WORD}\s+){{0,5}}{_WORD})\s+"
    rf"(?P<provision>(?:\d{{1,3}}\s*luv(?:un|ussa)\s+)?{_PYKALA}(?:\s+{_LEVEL})*)")
ABBREV_PROVISION_RE = re.compile(
    r"(?<![\w])(?P<abbrev>" + "|".join(re.escape(a) for a in sorted(
        ACT_ABBREVS, key=len, reverse=True)) +
    rf")\s+(?P<provision>(?:\d{{1,3}}\s*luvun?\s+)?{_PYKALA}(?:\s+{_LEVEL})*)")
#: "6 artiklan 1 kohdan f alakohta" — the EU article, which Finnish declines like a noun.
#: Finnish consonant gradation changes the stem itself: the nominative is "kohta" and
#: "alakohta", the genitive "kohdan" and "alakohdan". A pattern written on the genitive
#: stem alone silently dropped the lettered point from every citation that used the
#: nominative — "6 artiklan 1 kohdan f alakohta" came out as Article 6(1), losing the (f)
#: that is the whole point of an Article 6(1) citation.
_KOHTA = r"koh(?:d|t)\w*"
_ALAKOHTA = r"alakoh(?:d|t)\w*"
EU_ARTICLE_RE = re.compile(
    rf"(?P<provision>\d{{1,3}}\s*[a-z]?\s*artikl\w*"
    rf"(?:\s+\d{{1,3}}\s*{_KOHTA})?(?:\s+[a-z]\s*{_ALAKOHTA})?)")
#: "KKO:2024:1", "KHO 2023:45", "HelHO:2024:12", "MAO:123/24", "TT 2024:61".
_COURTS = (r"KKO|KHO|HelHO|THO|VHO|IHO|RHO|ItäHO|MAO|TT|VakO|"
           r"HelHAO|TurHAO|VaaHAO|ItäHAO|PSHAO|HAO")
CASE_RE = re.compile(
    rf"(?<![\w])(?P<court>{_COURTS})\s*[:\s]\s*"
    r"(?:(?P<year>(?:19|20)\d{2})\s*[:\s]\s*(?P<number>[IVX]*-?\d{1,5})"
    r"|(?P<number2>\d{1,5})\s*/\s*(?P<year2>\d{2,4}))(?![\w])")
#: The tail may be a SINGLE character — "ECLI:FI:KKO:2024:1" — so the last atom has to
#: be optional. Requiring two dropped every Supreme Court ECLI numbered under ten.
ECLI_RE = re.compile(r"(?<![\w])ECLI:FI:[A-Z]{2,8}:\d{4}:\w(?:[\w.:-]*\w)?(?![\w])")

#: Finnish agglutination adds to the END, so the dictionary form is a PREFIX of every
#: inflected form. A hyphenated compound ("tasa-arvolaki") and a genitive
#: ("takaisinsaantilain") therefore both start with the stem — the comparison is a prefix
#: test with the last two characters of the dictionary form allowed to change
#: ("laki" → "lain" → "laissa" all share "lak").
_STEM_TRIM = 2
#: …except for the four-letter HEAD NOUNS, which are too short for that rule and change
#: their stem under consonant gradation: "laki" becomes "lain", not "lakin". They are the
#: last word of every participial act name ("… annettu laki"), so getting them wrong
#: rejects the whole name — which is the form Finnish judgments actually use.
_HEAD_FORMS: dict[str, frozenset[str]] = {
    "laki": frozenset({"laki", "lain", "laissa", "laista", "lakia", "lakiin", "laiksi",
                       "lailla", "lakiin"}),
    "kaari": frozenset({"kaari", "kaaren", "kaaressa", "kaarta", "kaareen"}),
    "asetus": frozenset({"asetus", "asetuksen", "asetuksessa", "asetusta",
                         "asetukseen"}),
}


def _stem(word: str) -> str:
    folded = _fold(word)
    return folded[:-_STEM_TRIM] if len(folded) > _STEM_TRIM + 3 else folded


def _same_word(inflected: str, dictionary: str) -> bool:
    forms = _HEAD_FORMS.get(_fold(dictionary))
    if forms is not None:
        return _fold(inflected) in forms
    a, b = _stem(inflected), _stem(dictionary)
    return a.startswith(b) or b.startswith(a)


def _match_act(phrase: str) -> tuple[tuple[int, int], int] | None:
    """The act this (inflected) phrase names, and how many of its words were used.

    Suffix-anchored: the name sits immediately BEFORE the provision, so it is the LAST
    words of the captured phrase that matter — the opposite of Slovak, where the name
    follows. Longest first, so "laki takaisinsaannista konkurssipesään" beats a
    single-word near-match.
    """
    words = [w for w in (phrase or "").split() if w]
    for size in range(min(len(words), 5), 0, -1):
        tail = words[-size:]
        for name, number in ACTS.items():
            name_words = name.split()
            if len(name_words) != size:
                continue
            if all(_same_word(w, n) for w, n in zip(tail, name_words)):
                return number, size
    return None


def _anchor(provision: str) -> str:
    """One spelling per provision: "5 §:n 1 momentin" and "5 § 1 mom." are one anchor."""
    text = " ".join((provision or "").split())
    text = re.sub(r"§\s*:\s*\w{1,4}", "§", text)
    text = re.sub(r"(?i)\bmom(?:entti|entin|entissa|entissä)?\.?", "mom.", text)
    text = re.sub(r"(?i)\bkohd(?:an|assa|at|ta)\b", "kohta", text)
    text = re.sub(r"(?i)\balakohd(?:an|assa|at|ta)\b", "alakohta", text)
    text = re.sub(r"(?i)\bluv(?:un|ussa)\b", "luku", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _eu_anchor(provision: str) -> str | None:
    """"6 artiklan 1 kohdan f alakohta" → ``Article 6(1)(f)``."""
    article = re.match(r"\s*(\d{1,3}\s*[a-z]?)\s*artikl", provision or "", re.IGNORECASE)
    if not article:
        return None
    out = "Article " + re.sub(r"\s+", "", article.group(1))
    if sub := re.search(r"(?i)(\d{1,3})\s*koh[dt]", provision):
        out += f"({sub.group(1)})"
    if letter := re.search(r"(?i)\b([a-z])\s*alakoh[dt]", provision):
        out += f"({letter.group(1).casefold()})"
    return out


def _eu_near(text: str, start: int, end: int, window: int = 60) -> tuple[str, str] | None:
    """The EU instrument named around an article reference, if any.

    Finnish puts the instrument on **either** side and inflects it. The genitive
    construction leads — "yleisen tietosuoja-asetuksen 6 artiklan 1 kohdan f alakohta" —
    while a list or a parenthesis trails — "6 artiklan 1 kohta, yleinen tietosuoja-
    asetus". Looking only forward missed the ordinary word order, which is the one every
    Finnish judgment uses. The preceding window is checked first for that reason.
    """
    before = _sentence_tail(_fold(text[max(0, start - window):start]))
    after = _sentence_head(_fold(text[end:end + window]))
    for haystack, from_end in ((before, True), (after, False)):
        best: tuple[int, str, str] | None = None
        for stem, celex, kind in _EU_STEMS:
            head = stem[:-2] if len(stem) > 5 else stem
            at = haystack.rfind(head) if from_end else haystack.find(head)
            if not head or at < 0:
                continue
            # NEAREST wins, not longest. Two instruments named in adjacent sentences put
            # both stems in a fixed-width window, and preferring the longer one attributed
            # "SEUT 267 artikla" to the tietosuoja-asetus mentioned in the sentence before.
            distance = len(haystack) - at if from_end else at
            if best is None or distance < best[0]:
                best = (distance, celex, kind)
        if best is not None:
            return best[1], best[2]
    return None


#: An instrument named in the PREVIOUS sentence is not this article's instrument. Finnish
#: writes each reference in one clause, so the search window is cut at the nearest
#: sentence boundary rather than run to a fixed character count.
_SENTENCE_END = re.compile(r"[.!?;]\s")


def _sentence_tail(text: str) -> str:
    matches = list(_SENTENCE_END.finditer(text))
    return text[matches[-1].end():] if matches else text


def _sentence_head(text: str) -> str:
    m = _SENTENCE_END.search(text)
    return text[:m.start()] if m else text


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
        hit = _eu_near(body, m.start(), m.end())
        if not hit or not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group(0), entity_kind=hit[1], candidate_id=hit[0],
                     pinpoint=_eu_anchor(m.group("provision")),
                     char_start=m.start(), char_end=m.end(),
                     method="fi_eu_article", confidence=0.95))
    for m in ABBREV_PROVISION_RE.finditer(body):
        if not free(m.start(), m.end()):
            continue
        year, number = ACT_ABBREVS[m.group("abbrev")]
        add(Citation(raw=m.group(0), entity_kind="act", candidate_id=act_id(year, number),
                     pinpoint=_anchor(m.group("provision")),
                     char_start=m.start(), char_end=m.end(),
                     method="fi_act_abbrev", confidence=0.9))
    at = 0
    while (m := NAMED_PROVISION_RE.search(body, at)) is not None:
        at = m.end()
        hit = _match_act(m.group("name"))
        if hit is None:
            continue
        (year, number), used = hit
        start = _word_start(body, m.end("name"), used)
        if not free(start, m.end()):
            continue
        add(Citation(raw=body[start:m.end()], entity_kind="act",
                     candidate_id=act_id(year, number),
                     pinpoint=_anchor(m.group("provision")),
                     char_start=start, char_end=m.end(),
                     method="fi_act_reference", confidence=0.95))
    # A säädösnumero standing alone is only a citation when an act name introduced it:
    # "1050/2018" also looks like a date range, a docket and a page span, and this pass
    # runs over every document in the corpus, not only Finnish ones.
    for m in NUMBER_RE.finditer(body):
        head = _fold(body[max(0, m.start() - 60):m.start()])
        if not re.search(r"(laki|lain|laissa|laista|asetu|sääd|saad|kaari|kaaren)\s*$", head):
            continue
        if not free(m.start(), m.end()):
            continue
        add(Citation(raw=m.group(0).strip(), entity_kind="act",
                     candidate_id=act_id(m.group("year"), m.group("number")),
                     pinpoint=None, char_start=m.start(), char_end=m.end(),
                     method="fi_act_number", confidence=0.9))
    return found


def _word_start(text: str, end: int, words: int) -> int:
    """The offset of the ``words``-th whitespace-separated word counting BACK from ``end``."""
    at = end
    for _ in range(words):
        while at > 0 and text[at - 1].isspace():
            at -= 1
        while at > 0 and not text[at - 1].isspace():
            at -= 1
    return at


def case_citations(text: str) -> list[Citation]:
    body = text or ""
    found = [Citation(raw=m.group(0), entity_kind="case", candidate_id=m.group(0).upper(),
                      pinpoint=None, char_start=m.start(), char_end=m.end(),
                      method="fi_ecli", confidence=1.0)
             for m in ECLI_RE.finditer(body)]
    claimed = [(c.char_start, c.char_end) for c in found]
    for m in CASE_RE.finditer(body):
        if any(s <= m.start() and m.end() <= e for s, e in claimed):
            continue
        year = m.group("year") or m.group("year2")
        number = m.group("number") or m.group("number2")
        if not year or not number:
            continue
        # The Market Court's "MAO:123/24" writes a two-digit year; everything else writes
        # four. Reading "24" as the year 24 files a 2024 decision two millennia early.
        if len(year) == 2:
            year = f"20{year}" if int(year) < 70 else f"19{year}"
        found.append(Citation(
            raw=m.group(0), entity_kind="case",
            candidate_id=case_id(m.group("court"), year, number), pinpoint=None,
            char_start=m.start(), char_end=m.end(), method="fi_case", confidence=0.95))
    return found


FINNISH_METHODS: frozenset[str] = frozenset({
    "fi_act_reference", "fi_act_abbrev", "fi_act_number", "fi_eu_article",
    "fi_case", "fi_ecli",
})


def finnish_citations(text: str) -> list[Citation]:
    """Every Finnish citation in ``text``: statutes by name or säädösnumero, EU articles,
    and decisions by court prefix or ECLI."""
    return law_citations(text) + case_citations(text)
