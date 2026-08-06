"""Dutch legal references: extract locally, resolve against Rechtspraak/BWB/LiDO ids.

The graph keeps a BWB work as the destination and the exact provision as its anchor.
When a Juriconnect reference supplies a ``g``/``z`` date, that date is retained both
in the candidate (so a point-in-time copy can be harvested) and in the anchor.
"""

from __future__ import annotations

import re
import unicodedata

from .grammars import _eu_celex
from .models import Citation


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in value if not unicodedata.combining(c)).casefold()


def law_name_alias(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", " ", _fold(value)).strip()
    # BWB publishes the civil code as separate works titled "Burgerlijk Wetboek
    # Boek 1" … "Boek 10", while citations use the collective abbreviation BW.
    key = re.sub(r"^burgerlijk wetboek boek \d+$", "burgerlijk wetboek", key)
    return f"nl:law:{key}"


def ljn_alias(value: str) -> str:
    return "nl:ljn:" + re.sub(r"\s+", "", value or "").upper()


_LAW_NAMES = {
    "BW": "Burgerlijk Wetboek", "Awb": "Algemene wet bestuursrecht",
    "Sr": "Wetboek van Strafrecht", "Sv": "Wetboek van Strafvordering",
    "Gw": "Grondwet", "Rv": "Wetboek van Burgerlijke Rechtsvordering",
    "Vw": "Vreemdelingenwet 2000", "Wob": "Wet openbaarheid van bestuur",
    "Woo": "Wet open overheid", "UAVG": "Uitvoeringswet AVG",
    "Whc": "Wet handhaving consumentenbescherming",
    "Prijzenwet": "Prijzenwet",
    "Tw": "Telecommunicatiewet",
    "WIA": "Wet werk en inkomen naar arbeidsvermogen",
    "WAO": "Wet op de arbeidsongeschiktheidsverzekering",
    "WW": "Werkloosheidswet", "ZW": "Ziektewet",
    # Dutch data-protection statutes. The AP's decisions are grounded in these as much
    # as in the AVG itself: the UAVG carries the national derogations (including the
    # Article 33 blacklist licence), the Wpg/Wjsg are the LED-side regimes for police
    # and criminal-justice data, and the Wbp is the pre-2018 Act the older decisions
    # and every appeal against them are still argued under.
    "Wpg": "Wet politiegegevens",
    "Wjsg": "Wet justitiële en strafvorderlijke gegevens",
    "Wbp": "Wet bescherming persoonsgegevens",
    # Belgian Dutch. The GBA's Dispute Chamber grounds every decision in two national
    # acts alongside the AVG, and cites both by abbreviation from first use: the WOG is
    # the act that created the authority and confers the Chamber's powers (the
    # corrective measures in art. 100 and the fining power in art. 101), and the
    # Kaderwet carries Belgium's Article 6(2)/23 derogations. Without these an
    # "art. 100 WOG" in a Belgian decision is an unrecognised reference, not a link.
    "WOG": "Wet van 3 december 2017 tot oprichting van de Gegevensbeschermingsautoriteit",
    "Kaderwet": ("Wet van 30 juli 2018 betreffende de bescherming van natuurlijke "
                 "personen met betrekking tot de verwerking van persoonsgegevens"),
    "WVP": "Wet van 8 december 1992 tot bescherming van de persoonlijke levenssfeer",
    # The rest of the Belgian statute book the Geschillenkamer actually reaches for. Direct
    # marketing and cookie complaints are argued under the WER as much as under the AVG;
    # camera cases under the Camerawet; police-data cases under the WPA. Acronyms only —
    # a Belgian decision introduces each on first use and then cites it short.
    "WER": "Wetboek van economisch recht",
    "WPA": "Wet op het politieambt",
    "Camerawet": "Wet van 21 maart 2007 tot regeling van de plaatsing en het gebruik van bewakingscamera's",
    "WIB92": "Wetboek van de inkomstenbelastingen 1992",
    "WVW": "Wegverkeerswet",
    "Strafwetboek": "Strafwetboek",
    "Gerechtelijk Wetboek": "Gerechtelijk Wetboek",
}
# Belgian note: ``Gw`` and ``BW`` are live in both countries and are deliberately NOT
# re-pointed here — a Belgian decision citing the Grondwet and a Dutch one citing it are
# the same abbreviation for different instruments, and guessing by acronym alone would
# mislink one of them. They resolve through the alias store, which can hold the
# jurisdiction-specific target.
# Surface forms that are not the canonical title and not a plain abbreviation. The AP
# spells the UAVG out in full on first use in nearly every decision.
_LAW_ALIASES = {
    "Uitvoeringswet Algemene verordening gegevensbescherming": "Uitvoeringswet AVG",
    "Uitvoeringswet AVG": "Uitvoeringswet AVG",
}
_LAW_ALT = "|".join(sorted(
    (re.escape(x) for x in (*_LAW_NAMES, *_LAW_NAMES.values(), *_LAW_ALIASES)),
    key=len, reverse=True))

_NL_ORDINALS = {
    "eerste": "1", "tweede": "2", "derde": "3", "vierde": "4", "vijfde": "5",
    "zesde": "6", "zevende": "7", "achtste": "8", "negende": "9", "tiende": "10",
}
_NL_ORDINAL_ALT = "|".join(_NL_ORDINALS)

# Dutch statutory drafting numbers a provision in words *after* the article, and the
# authorities quote it in full: ``artikel 6, eerste lid, aanhef en onder f, van de
# AVG``. Both parts are optional and both are separated by commas, which is exactly
# what the old pattern could not cross — it demanded whitespace before the instrument
# name, so every reference carrying a *lid* fell through to the heuristic carry-forward
# and lost its sub-article pincite. ``aanhef en`` ("opening words and") is apparatus,
# not a pinpoint: it says the opening words are read together with point (f).
_NL_LID = rf"(?:\s*,?\s*(?P<lid>\d+|{_NL_ORDINAL_ALT})\s+lid)?"
_NL_ONDER = r"(?:\s*,?\s*(?:aanhef\s+en\s+)?onder\s+(?P<onder>[a-z])\b)?"
# What sits between the provision and the instrument it belongs to. The comma is the
# load-bearing part: Dutch closes the *lid* clause with one ("artikel 5, tweede lid,
# AVG"), and "van de" is only present about half the time.
_NL_OF = r"\s*,?\s*(?:(?:van\s+)?(?:de|het)\s+)?"

# In Dutch, AVG is the GDPR. ``artikel 15 AVG`` otherwise has exactly the shape
# accepted by the deliberately broad German-law parser and becomes the phantom
# ``de/gesetz/avg``. Capture the Dutch construction first and keep the EU article
# anchor in the same language-neutral form as Formex.
#
# The spelled-out name is accepted too, but never when the *Uitvoeringswet* Algemene
# verordening gegevensbescherming is what is being cited: that is the national
# implementing Act, a different instrument that happens to end in the same six words.
AVG_ARTICLE_RE = re.compile(
    r"\b(?:art(?:ikel)?\.?)\s+(?P<article>\d{1,3}[a-z]?"
    r"(?:\(\d+[a-z]?\))*)"
    rf"{_NL_LID}{_NL_ONDER}{_NL_OF}"
    r"(?:(?-i:AVG)|(?<!Uitvoeringswet\s)"
    r"Algemene\s+verordening\s+gegevensbescherming)\b",
    re.I,
)


def _eu_pin(article: str, lid: str | None = None, onder: str | None = None) -> str:
    """``6`` + ``eerste`` + ``f`` → ``Article 6(1)(f)`` — the Formex anchor vocabulary
    the rest of the corpus uses for EU provisions, so a Dutch decision's pincite lands
    on the same GDPR article node as an English or German one."""
    out = f"Article {article}"
    if lid:
        out += f"({_NL_ORDINALS.get(lid.casefold(), lid)})"
    if onder:
        out += f"({onder.casefold()})"
    return out


# Belgium writes a GDPR provision with dots, not with "lid"/"onder": the Geschillenkamer's
# decisions cite "artikel 6.1.f) AVG" and "artikel 83.5.a) AVG" throughout, which is the
# EU institutional style rather than the Netherlands' spelled-out one. Neither the Dutch
# pattern above (which wants "eerste lid, onder f") nor the parenthesised "6(1)(f)" form
# matches it, so on a Belgian decision every GDPR pincite fell through to the heuristic
# carry-forward and lost its sub-article precision — the very thing these decisions turn on.
BE_AVG_DOTTED_RE = re.compile(
    r"\b(?:art(?:ikel)?\.?)\s+(?P<article>\d{1,3}[a-z]?)"
    r"\.(?P<lid>\d{1,2})"
    r"(?:\.(?P<onder>[a-z])\)?)?"
    rf"{_NL_OF}"
    r"(?:(?-i:AVG)|Algemene\s+verordening\s+gegevensbescherming)\b",
    re.I,
)


def be_avg_dotted_citations(text: str) -> list[Citation]:
    return [Citation(
        raw=m.group(0), entity_kind="regulation", candidate_id="32016R0679",
        pinpoint=_eu_pin(m.group("article"), m.group("lid"), m.group("onder")),
        char_start=m.start(), char_end=m.end(),
        method="be_avg_dotted_article", confidence=1.0,
    ) for m in BE_AVG_DOTTED_RE.finditer(text)]


def avg_citations(text: str) -> list[Citation]:
    return [Citation(
        raw=m.group(0), entity_kind="regulation", candidate_id="32016R0679",
        pinpoint=_eu_pin(m.group("article"), m.group("lid"), m.group("onder")),
        char_start=m.start(), char_end=m.end(),
        method="nl_avg_article", confidence=1.0,
    ) for m in AVG_ARTICLE_RE.finditer(text)]


# Dutch implementing law and the AP's decisions under it refer to the GDPR as "de
# verordening" once it has been introduced — ``Onverminderd artikel 10 van de
# verordening mogen persoonsgegevens…``. Standing alone that is ambiguous (every EU
# regulation is "de verordening"), so it is only read as the GDPR in a document that
# names the AVG somewhere: that is the definition the phrase is pointing back to.
# Without this the reference reached the generic carry-forward, which attached it to
# whichever statute was last named — usually the UAVG, i.e. the wrong instrument.
_DEFINED_AVG_RE = re.compile(
    r"\b(?-i:AVG)\b|\bAlgemene\s+verordening\s+gegevensbescherming\b", re.I)
VERORDENING_ARTICLE_RE = re.compile(
    r"\b(?:art(?:ikel)?\.?)\s+(?P<article>\d{1,3}[a-z]?)"
    rf"{_NL_LID}{_NL_ONDER}"
    r"\s*,?\s*van\s+de\s+verordening\b",
    re.I,
)


def verordening_citations(text: str) -> list[Citation]:
    if not _DEFINED_AVG_RE.search(text or ""):
        return []
    return [Citation(
        raw=m.group(0), entity_kind="regulation", candidate_id="32016R0679",
        pinpoint=_eu_pin(m.group("article"), m.group("lid"), m.group("onder")),
        char_start=m.start(), char_end=m.end(),
        method="nl_verordening_article", confidence=.93,
    ) for m in VERORDENING_ARTICLE_RE.finditer(text)]


def _pin(article: str | None, paragraph: str | None = None,
         sub: str | None = None, date: str | None = None,
         para: str | None = None, item: str | None = None) -> str | None:
    if not article:
        return None
    out = f"Artikel {article}"
    if paragraph:
        out += f", lid {paragraph}"
    # Belgian subdivisions keep their own notation: "§ 1" and "9°" are what the decision
    # says and what a reader looks for, so they are not rewritten as "lid"/"onder".
    if para:
        out += f", § {para}"
    if item:
        out += f", {item}°"
    if sub:
        out += f", onder {sub}"
    if date:
        out += f" (geldend op {date})"
    return out


# Full Juriconnect references occur both as plain text and inside overheid.nl URLs.
JURICONNECT_RE = re.compile(
    r"(?P<jci>jci1\.3:c:(?P<bwb>BWBR\d{7}))"
    r"(?P<params>(?:[?&;](?:amp;)?(?:hoofdstuk|artikel|lid|onderdeel|g|z)=[^\s&#;]+)*)",
    re.IGNORECASE,
)


def _params(raw: str | None) -> dict[str, str]:
    return {k.casefold(): v for k, v in re.findall(
        r"(hoofdstuk|artikel|lid|onderdeel|g|z)=([^&;\s#]+)", raw or "", re.I)}


def juriconnect_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
    for m in JURICONNECT_RE.finditer(text):
        p = _params(m.group("params"))
        effective = p.get("g") or p.get("z")
        bwb = m.group("bwb").upper()
        candidate = f"{bwb}@{effective}" if effective else bwb
        out.append(Citation(
            raw=m.group(0), entity_kind="act", candidate_id=candidate,
            pinpoint=_pin(p.get("artikel"), p.get("lid"), p.get("onderdeel"), effective),
            char_start=m.start(), char_end=m.end(), method="nl_juriconnect", confidence=1.0,
        ))
    return out


# ``artikel 6:162 BW``, ``art. 8:42, eerste lid, Awb``, ``artikel 10 Grondwet`` and
# ``artikel 33, vierde lid, aanhef en onder c, UAVG``.
# Belgian statutes are subdivided "§ 1" and "9°", not "lid 1 onder a". Both are
# optional and sit between the article and the law, so a Netherlands citation matches
# exactly as before and a Belgian one stops being an unrecognised reference.
_BE_PARA = r"(?:\s*,?\s*§+\s*(?P<para>\d+))?"
_BE_ITEM = r"(?:\s*,?\s*(?P<item>\d+)\s*°)?"

LAW_REFERENCE_RE = re.compile(
    # Belgium numbers by Book ("artikel XII.13 WER"), inserts articles with a slash
    # ("44/1"), and its codes run past three digits ("1382 Strafwetboek"). None of those
    # matched, so the acronyms alone would have been dead weight.
    rf"\b(?:art(?:ikel)?\.?\s+)(?P<article>(?:[IVXL]{{1,5}}\.)?\d{{1,4}}(?:[:./]\d{{1,4}})?[a-z]?)"
    rf"{_NL_LID}{_BE_PARA}{_BE_ITEM}{_NL_ONDER}"
    rf"(?:\s*,?\s*(?:van\s+)?(?:de|het)?\s*)?(?P<law>(?:Wet\s+)?(?:{_LAW_ALT}))\b", re.I)


def law_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
    for m in LAW_REFERENCE_RE.finditer(text):
        raw_law = m.group("law")
        short = re.sub(r"(?i)^Wet\s+(?=[A-Z]{2,6}$)", "", raw_law)
        title = _LAW_ALIASES.get(
            next((k for k in _LAW_ALIASES if k.casefold() == raw_law.casefold()), ""),
            _LAW_NAMES.get(next((k for k in _LAW_NAMES
                                 if k.casefold() == short.casefold()), ""), raw_law))
        out.append(Citation(
            raw=m.group(0), entity_kind="act", candidate_id=law_name_alias(title),
            pinpoint=_pin(m.group("article"), m.group("lid"), m.group("onder"),
                          para=m.group("para"), item=m.group("item")),
            char_start=m.start(),
            char_end=m.end(), method="nl_law_reference", confidence=.97,
        ))
    return out


# Regulator guidance often gives the host first and the article later:
# ``Telecommunicatiewet, zie artikel 11.7`` and ``Burgerlijk Wetboek Boek 6
# (oneerlijke handelspraktijken), in het bijzonder artikel 193h``. The generic
# carry-forward pass cannot safely infer these when an EU regulation was cited in
# between, so capture the local host literally.
HOST_BEFORE_RE = re.compile(
    r"\b(?P<law>Telecommunicatiewet|Burgerlijk\s+Wetboek(?:\s+Boek\s+(?P<book>\d+))?)"
    r"(?P<middle>[^.\n]{0,180}?)\b"
    r"(?:art(?:ikel)?\.?\s+)(?P<article>\d{1,3}(?:[:.]\d{1,4})?[a-z]?)"
    r"(?:\s*,?\s*(?P<lid>\d+|eerste|tweede|derde|vierde|vijfde)\s+lid)?",
    re.I,
)


def host_before_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
    for match in HOST_BEFORE_RE.finditer(text):
        law = match.group("law")
        title = "Telecommunicatiewet" if law.casefold().startswith("tele") \
            else "Burgerlijk Wetboek"
        article = match.group("article")
        if match.group("book") and ":" not in article and "." not in article:
            article = f"{match.group('book')}:{article}"
        out.append(Citation(
            raw=match.group(0), entity_kind="act", candidate_id=law_name_alias(title),
            pinpoint=_pin(article, match.group("lid")), char_start=match.start(),
            char_end=match.end(), method="nl_host_before_article", confidence=.96,
        ))
    return out


_NL_ECHR_RE = re.compile(
    r"\bartikel(?:en)?\s+(?P<article>\d{1,2})"
    r"(?:\s*,?\s*(?P<lid>\d+|eerste|tweede|derde|vierde|vijfde)\s+lid)?\s*,?\s*"
    r"(?:van\s+)?(?:het\s+)?Verdrag\s+tot\s+bescherming\s+van\s+de\s+rechten\s+"
    r"van\s+de\s+mens\s+en\s+de\s+fundamentele\s+vrijheden\b", re.I)


def echr_citations(text: str) -> list[Citation]:
    return [Citation(
        raw=m.group(0), entity_kind="treaty", candidate_id="echr/convention",
        pinpoint=_pin(m.group("article"), m.group("lid")), char_start=m.start(),
        char_end=m.end(), method="nl_echr_article", confidence=1.0,
    ) for m in _NL_ECHR_RE.finditer(text)]


# ACM publications use the Dutch name as often as the instrument number:
# ``Richtlijn oneerlijke handelspraktijken`` and ``Richtlijn 2005/29/EG``.
# Capture the host separately and walk backwards for governed article lists so
# orphan-looking ``artikelen 5, 6 en 7 van de Richtlijn …`` all retain a CELEX.
_NL_DIRECTIVE_HOST = re.compile(
    r"\b(?:(?P<ucpd>Richtlijn\s+oneerlijke\s+handelspraktijken)|"
    r"Richtlijn\s*(?:\((?:EU|EG|EEG)\)\s*)?"
    r"(?P<a>\d{2,4})\s*/\s*(?P<b>\d{1,4})"
    r"(?:\s*/?\s*(?:EU|EG|EEG))?)\b",
    re.I,
)
_NL_DIRECTIVE_ARTICLES_BEFORE = re.compile(
    r"(?P<whole>\bartikel(?:en)?\.?\s+"
    r"(?P<run>\d{1,3}[a-z]?(?:\s*,\s*\d{1,3}[a-z]?)*"
    r"(?:\s+(?:en|of)\s+\d{1,3}[a-z]?)*)"
    r"(?:\s*,?\s*(?P<lid>\d+|eerste|tweede|derde|vierde|vijfde)\s+lid)?"
    r"\s*,?\s*(?:van\s+)?(?:de|het)?\s*)$",
    re.I,
)
_NL_ORDINAL = _NL_ORDINALS


def eu_directive_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
    for host in _NL_DIRECTIVE_HOST.finditer(text):
        candidate = (
            "32005L0029" if host.group("ucpd")
            else _eu_celex("directive", host.group("a"), host.group("b"))
        )
        if not candidate:
            continue
        out.append(Citation(
            raw=host.group(0), entity_kind="directive", candidate_id=candidate,
            pinpoint=None, char_start=host.start(), char_end=host.end(),
            method="nl_eu_directive", confidence=1.0,
        ))
        before_start = max(0, host.start() - 180)
        before = text[before_start:host.start()]
        governed = _NL_DIRECTIVE_ARTICLES_BEFORE.search(before)
        if not governed:
            continue
        run = governed.group("run")
        run_start = before_start + governed.start("run")
        numbers = list(re.finditer(r"\d{1,3}[a-z]?", run, re.I))
        lid = governed.group("lid")
        lid_number = _NL_ORDINAL.get((lid or "").casefold(), lid)
        for index, number in enumerate(numbers):
            pinpoint = f"Article {number.group(0)}"
            if lid_number and len(numbers) == 1:
                pinpoint += f"({lid_number})"
            start = (
                before_start + governed.start("whole")
                if index == 0 else run_start + number.start()
            )
            end = run_start + number.end()
            out.append(Citation(
                raw=text[start:end], entity_kind="directive",
                candidate_id=candidate, pinpoint=pinpoint,
                char_start=start, char_end=end,
                method=(
                    "nl_eu_directive_article"
                    if index == 0 else "nl_eu_directive_article_list"
                ),
                confidence=.99,
            ))
    return out


LJN_RE = re.compile(r"\b(?:LJN|LJ[N]?[- ]?nummer|ELRO)\s*[:.= -]*\s*(?P<id>[A-Z]{2}\s*\d{4})\b", re.I)


def ljn_citations(text: str) -> list[Citation]:
    return [Citation(raw=m.group(0), entity_kind="case", candidate_id=ljn_alias(m.group("id")),
                     pinpoint=None, char_start=m.start(), char_end=m.end(),
                     method="nl_ljn", confidence=.98) for m in LJN_RE.finditer(text)]


def dutch_citations(text: str) -> list[Citation]:
    return (avg_citations(text) + be_avg_dotted_citations(text)
            + verordening_citations(text)
            + juriconnect_citations(text) + law_citations(text)
            + host_before_citations(text) + echr_citations(text)
            + eu_directive_citations(text) + ljn_citations(text))
