"""French citation grammars and canonical alias keys.

The public French identifiers are heterogeneous: ECLI identifies judgments, while
Légifrance uses LEGIARTI/JURITEXT/etc.  Citations printed in judgments commonly carry
only a pourvoi/decision number or a code article.  The latter forms are represented by
stable, namespace-scoped alias keys; French adapters mint the same keys on ingest.
"""

from __future__ import annotations

import re
import unicodedata

from .grammars import Grammar, Normalised, _eu_celex, register
from .models import Citation


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in value if not unicodedata.combining(c)).casefold()


def normalise_fr_number(value: str) -> str:
    """Fold typography in a French docket/text number without losing its series."""
    return re.sub(r"\s+", "", value or "").replace("–", "-").replace("—", "-").upper()


def decision_alias(number: str) -> str:
    return f"fr:decision:{normalise_fr_number(number)}"


def pourvoi_alias(number: str) -> str:
    return f"fr:pourvoi:{normalise_fr_number(number)}"


def text_alias(number: str) -> str:
    return f"fr:text:{normalise_fr_number(number)}"


_CODE_NAMES = {
    "cciv": ("code civil", "c. civ.", "c.civ."),
    "cprociv": ("code de procédure civile", "code de procedure civile", "c. pr. civ.", "cpc"),
    "ccom": ("code de commerce", "c. com."),
    "ctrav": ("code du travail", "c. trav."),
    "cpi": ("code de la propriété intellectuelle", "code de la propriete intellectuelle", "cpi"),
    "cpen": ("code pénal", "code penal", "c. pén.", "c. pen."),
    "cpp": ("code de procédure pénale", "code de procedure penale", "cpp"),
    "cassur": ("code des assurances", "c. assur."),
    "cconso": ("code de la consommation", "c. conso."),
    "csi": ("code de la sécurité intérieure", "code de la securite interieure", "csi"),
    "csp": ("code de la santé publique", "code de la sante publique", "csp"),
    "css": ("code de la sécurité sociale", "code de la securite sociale", "css"),
    "ceseda": ("code de l'entrée et du séjour des étrangers et du droit d'asile",
               "code de l’entree et du sejour des etrangers et du droit d’asile", "ceseda"),
    "cgct": ("code général des collectivités territoriales", "code general des collectivites territoriales", "cgct"),
    "cpce": ("code des postes et des communications électroniques",
             "code des postes et des communications electroniques", "cpce"),
    "cenv": ("code de l'environnement", "code de l’environnement", "c. env."),
    "cja": ("code de justice administrative", "cja"),
    "cgi": ("code général des impôts", "code general des impots", "cgi"),
    "crpa": ("code des relations entre le public et l'administration",
             "code des relations entre le public et l’administration", "crpa"),
}


def code_key(title: str) -> str | None:
    folded = re.sub(r"[^a-z0-9]+", " ", _fold(title)).strip()
    for key, names in _CODE_NAMES.items():
        if any(folded == re.sub(r"[^a-z0-9]+", " ", _fold(n)).strip() for n in names):
            return key
    return None


def normalise_article(value: str) -> str:
    value = normalise_fr_number(value).replace(".", "")
    return value


def code_article_alias(title: str, article: str) -> str | None:
    key = code_key(title)
    return f"fr:code:{key}:{normalise_article(article)}" if key and article else None


_CODE_ALT = "|".join(
    re.escape(name) for names in _CODE_NAMES.values() for name in sorted(names, key=len, reverse=True)
)
_ARTICLE = r"(?P<article>(?:L|R|D|A|LO)?\s*\.?\s*\d{1,5}(?:-\d+)*(?:-\d+)?(?:\s*(?-i:[A-Z]))?)"
_ARTICLE_TOKEN = r"(?:L|R|D|A|LO)?\s*\.?\s*\d{1,5}(?:-\d+)*(?:\s*(?-i:[A-Z]))?"


def _code_ref(m: re.Match[str]) -> Normalised:
    alias = code_article_alias(m.group("code"), m.group("article"))
    article = normalise_article(m.group("article"))
    return alias, f"Article {article}", "act"


register(Grammar(
    "fr_code_article", "act",
    re.compile(rf"\b(?:articles?|art\.)\s+{_ARTICLE}"
               rf"(?:\s*(?:,|et|à|a)\s*{_ARTICLE_TOKEN})*\s+"
               rf"(?:du|de la|des|d['’]u?)\s+(?P<code>{_CODE_ALT})\b",
               re.IGNORECASE),
    _code_ref,
))


_FR_ARTICLE_LIST = rf"(?P<list>{_ARTICLE_TOKEN}(?:\s*(?:,|et|à|a)\s*{_ARTICLE_TOKEN})*)"
_FR_ARTICLE_LIST_MULTI = rf"(?P<list>{_ARTICLE_TOKEN}(?:\s*(?:,|et|à|a)\s*{_ARTICLE_TOKEN})+)"
_FR_CODE_LIST = re.compile(
    rf"\b(?:articles?|arts?\.)\s+{_FR_ARTICLE_LIST_MULTI}\s+"
    rf"(?:du|de la|des|d['’]u?)\s+(?P<host>{_CODE_ALT})\b", re.IGNORECASE)
_FR_ECHR_LIST = re.compile(
    rf"\b(?:articles?|arts?\.)\s+{_FR_ARTICLE_LIST}\s+"
    r"(?:de\s+la|de\s+l['’]|du)\s+Convention\s+européenne"
    r"(?:\s+de\s+sauvegarde)?\s+des\s+droits\s+de\s+l['’](?:homme|Homme)"
    r"(?:\s+et\s+des\s+libertés\s+fondamentales)?\b", re.IGNORECASE)
_FR_ARTICLE_VALUE = re.compile(_ARTICLE_TOKEN, re.IGNORECASE)


def french_citations(text: str) -> list[Citation]:
    """Expand compact French article lists to canonical, pinpointed graph edges."""
    out: list[Citation] = []
    for rx, host_kind in ((_FR_CODE_LIST, "code"), (_FR_ECHR_LIST, "echr")):
        for m in rx.finditer(text):
            code = code_key(m.group("host")) if host_kind == "code" else None
            for am in _FR_ARTICLE_VALUE.finditer(m.group("list")):
                value = normalise_article(am.group(0))
                candidate = f"fr:code:{code}:{value}" if code else "echr/convention"
                out.append(Citation(
                    raw=m.group(0), entity_kind="act" if code else "treaty",
                    candidate_id=candidate, pinpoint=f"Article {value}",
                    char_start=m.start(), char_end=m.end(),
                    method="fr_code_articles" if code else "fr_echr_articles",
                    confidence=1.0,
                ))
    return out

# Légifrance identifiers and URLs are already canonical corpus identifiers.
register(Grammar(
    "fr_legifrance_id", "decision",
    re.compile(r"\b(?P<id>(?:LEGI(?:ARTI|TEXT)|JORF(?:ARTI|TEXT)|JURITEXT|CETATEXT|CONSTEXT|CNILTEXT)\d{8,})\b",
               re.IGNORECASE),
    lambda m: (m.group("id").upper(), None,
               "act" if m.group("id").upper().startswith(("LEGI", "JORF")) else "case"),
))


def _case_number(m: re.Match[str]) -> Normalised:
    number = m.group("number")
    court = _fold(m.group("court"))
    alias = pourvoi_alias(number) if "cass" in court else decision_alias(number)
    return alias, None, "case"


_DATE = r"\d{1,2}(?:er)?\s+(?:janv(?:ier)?|févr(?:ier)?|fevr(?:ier)?|mars|avr(?:il)?|mai|juin|juil(?:let)?|août|aout|sept(?:embre)?|oct(?:obre)?|nov(?:embre)?|déc(?:embre)?|dec(?:embre)?)\.?\s+\d{4}"
_COURT = (r"Cour\s+de\s+cassation|Cass\.(?:\s*(?:civ|com|crim|soc|ass\.\s*plén|ch\.\s*mixte)\.?)?"
          r"|Conseil\s+d['’](?:É|E)tat|Cons\.?\s*(?:É|E)tat|C\.?\s*E\.?|CE"
          r"|Conseil\s+constitutionnel|Cons\.?\s*const\."
          r"|Cour\s+administrative\s+d['’]appel|CAA|Tribunal\s+administratif|TA"
          r"|Cour\s+d['’]appel|CA|Tribunal\s+judiciaire|TJ")

register(Grammar(
    "fr_national_case", "case",
    re.compile(rf"\b(?P<court>{_COURT})\b[^;\n]{{0,80}}?{_DATE}\s*,?\s*(?:n(?:o|°|º)\.?\s*)?(?P<number>(?:\d{{2}}-\d{{2}}\.\d{{3}}|\d{{4,7}}|\d{{4}}-\d{{2,5}}(?:\s+[A-Z]{{1,4}})?))\b",
               re.IGNORECASE),
    _case_number,
))

# French EU drafting uses "règlement" and "décision" where the core grammar expects
# English descriptors.  It still resolves to the same CELEX nodes.
register(Grammar(
    "fr_eu_instrument", "eu_instrument",
    re.compile(r"\b(?P<kind>règlement|reglement|directive|décision|decision)\s*(?:\((?:UE|CE|CEE)\)\s*)?(?:n(?:o|°)\s*)?(?P<a>\d{1,4})/(?P<b>\d{1,4})(?:/(?:UE|CE|CEE))?\b", re.IGNORECASE),
    lambda m: (_eu_celex({"règlement": "regulation", "reglement": "regulation",
                           "décision": "decision", "decision": "decision"}.get(
                              m.group("kind").casefold(), "directive"),
                         m.group("a"), m.group("b")), None,
               {"règlement": "regulation", "reglement": "regulation",
                "décision": "decision", "decision": "decision"}.get(
                    m.group("kind").casefold(), "directive")),
))
