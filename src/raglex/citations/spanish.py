"""Spanish data-protection references — the AEPD's guías and resoluciones.

Spanish authors number a provision with dots as often as with words: ``artículo
6.1.f) del RGPD`` and ``artículo 6, apartado 1, letra f), del RGPD`` are the same
citation. Both are normalised to the Formex anchor (``Article 6(1)(f)``) so a Spanish
guide's pincite lands on the same GDPR article node as a German or Dutch one.
"""

from __future__ import annotations

import re

from .models import Citation

GDPR_ID = "32016R0679"
# Ley Orgánica 3/2018, de 5 de diciembre, de Protección de Datos Personales y garantía
# de los derechos digitales — the Spanish implementing Act, universally cited as
# LOPDGDD (and, in older material, LOPD).
LOPDGDD_ID = "es:ley:lo-3-2018"

_ES_ARTICLE = (
    r"\b(?:art(?:ículo|iculo|\.)?)\s*(?P<article>\d{1,3}(?:\s*(?:bis|ter|quater))?)"
    # 6.1.f) — the compact dotted form
    r"(?:\s*\.\s*(?P<dpara>\d{1,2}))?"
    r"(?:\s*\.\s*(?P<dletter>[a-z])\)?)?"
    # …or spelled out, in either order of the optional parts
    r"(?:\s*,?\s*(?:apartado|apdo\.?|párrafo|parrafo)\s*(?P<para>\d{1,2}))?"
    r"(?:\s*,?\s*letras?\s*(?P<letter>[a-z])\)?)?"
    r"\s*,?\s*(?:d[eo]l?\s+|de\s+la\s+)?"
)
# The GDPR: by acronym, by its Spanish name, or by number.
_ES_GDPR_HOST = (r"(?:(?-i:RGPD)|Reglamento\s+General\s+de\s+Protecci[óo]n\s+de\s+Datos"
                 r"|Reglamento\s*\(\s*UE\s*\)\s*2016\s*/\s*679)")
_ES_LOPDGDD_HOST = (r"(?:(?-i:LOPDGDD|LOPD)|Ley\s+Org[áa]nica\s+3\s*/\s*2018)")

GDPR_ARTICLE_RE = re.compile(_ES_ARTICLE + _ES_GDPR_HOST + r"\b", re.I)
LOPDGDD_ARTICLE_RE = re.compile(_ES_ARTICLE + _ES_LOPDGDD_HOST + r"\b", re.I)
# A bare mention of either instrument, so a guide that names the law without pinpointing
# a provision still links to it.
GDPR_NAME_RE = re.compile(r"\b" + _ES_GDPR_HOST + r"\b", re.I)
LOPDGDD_NAME_RE = re.compile(r"\b" + _ES_LOPDGDD_HOST + r"\b", re.I)


def _pin(match: re.Match) -> str:
    out = f"Article {re.sub(r'\\s+', ' ', match.group('article')).strip()}"
    para = match.group("dpara") or match.group("para")
    letter = match.group("dletter") or match.group("letter")
    if para:
        out += f"({para})"
    if letter:
        out += f"({letter.casefold()})"
    return out


def _articles(text: str, pattern: re.Pattern[str], candidate: str, kind: str,
              method: str) -> list[Citation]:
    return [Citation(
        raw=m.group(0), entity_kind=kind, candidate_id=candidate, pinpoint=_pin(m),
        char_start=m.start(), char_end=m.end(), method=method, confidence=.98,
    ) for m in pattern.finditer(text)]


def _names(text: str, pattern: re.Pattern[str], candidate: str, kind: str,
           method: str) -> list[Citation]:
    return [Citation(
        raw=m.group(0), entity_kind=kind, candidate_id=candidate, pinpoint=None,
        char_start=m.start(), char_end=m.end(), method=method, confidence=.97,
    ) for m in pattern.finditer(text)]


def spanish_citations(text: str) -> list[Citation]:
    if not text:
        return []
    return (
        _articles(text, GDPR_ARTICLE_RE, GDPR_ID, "regulation", "es_gdpr_article")
        + _articles(text, LOPDGDD_ARTICLE_RE, LOPDGDD_ID, "act", "es_lopdgdd_article")
        + _names(text, GDPR_NAME_RE, GDPR_ID, "regulation", "es_gdpr")
        + _names(text, LOPDGDD_NAME_RE, LOPDGDD_ID, "act", "es_lopdgdd")
    )
