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
}
_LAW_ALT = "|".join(sorted((re.escape(x) for x in (*_LAW_NAMES, *_LAW_NAMES.values())),
                           key=len, reverse=True))

# In Dutch, AVG is the GDPR. ``artikel 15 AVG`` otherwise has exactly the shape
# accepted by the deliberately broad German-law parser and becomes the phantom
# ``de/gesetz/avg``. Capture the Dutch construction first and keep the EU article
# anchor in the same language-neutral form as Formex.
AVG_ARTICLE_RE = re.compile(
    r"\b(?:art(?:ikel)?\.?)\s+(?P<article>\d{1,3}[a-z]?"
    r"(?:\(\d+[a-z]?\))*)"
    r"(?:\s*,?\s*(?P<lid>\d+|eerste|tweede|derde|vierde|vijfde)\s+lid)?"
    r"\s+(?:van\s+de\s+)?(?-i:AVG)\b",
    re.I,
)


def avg_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
    for match in AVG_ARTICLE_RE.finditer(text):
        pinpoint = f"Article {match.group('article')}"
        if match.group("lid"):
            pinpoint += f", lid {match.group('lid')}"
        out.append(Citation(
            raw=match.group(0), entity_kind="regulation",
            candidate_id="32016R0679", pinpoint=pinpoint,
            char_start=match.start(), char_end=match.end(),
            method="nl_avg_article", confidence=1.0,
        ))
    return out


def _pin(article: str | None, paragraph: str | None = None,
         sub: str | None = None, date: str | None = None) -> str | None:
    if not article:
        return None
    out = f"Artikel {article}"
    if paragraph:
        out += f", lid {paragraph}"
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


# ``artikel 6:162 BW``, ``art. 8:42, eerste lid, Awb`` and ``artikel 10 Grondwet``.
LAW_REFERENCE_RE = re.compile(
    rf"\b(?:art(?:ikel)?\.?\s+)(?P<article>\d{{1,3}}(?:[:.]\d{{1,4}})?[a-z]?)"
    rf"(?:\s*,?\s*(?P<lid>\d+|eerste|tweede|derde|vierde|vijfde)\s+lid)?"
    rf"(?:\s*,?\s*(?:van\s+)?(?:de|het)?\s*)?(?P<law>(?:Wet\s+)?(?:{_LAW_ALT}))\b", re.I)


def law_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
    for m in LAW_REFERENCE_RE.finditer(text):
        raw_law = m.group("law")
        short = re.sub(r"(?i)^Wet\s+(?=[A-Z]{2,6}$)", "", raw_law)
        title = _LAW_NAMES.get(next((k for k in _LAW_NAMES
                                     if k.casefold() == short.casefold()), ""), raw_law)
        out.append(Citation(
            raw=m.group(0), entity_kind="act", candidate_id=law_name_alias(title),
            pinpoint=_pin(m.group("article"), m.group("lid")), char_start=m.start(),
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
_NL_ORDINAL = {
    "eerste": "1", "tweede": "2", "derde": "3",
    "vierde": "4", "vijfde": "5",
}


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
    return (avg_citations(text) + juriconnect_citations(text) + law_citations(text)
            + host_before_citations(text) + echr_citations(text)
            + eu_directive_citations(text) + ljn_citations(text))
