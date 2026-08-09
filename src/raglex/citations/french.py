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


def _statute_number_ref(match: re.Match[str]) -> Normalised:
    article = normalise_article(match.group("article")) if match.group("article") else None
    return (text_alias(match.group("number")),
            f"Article {article}" if article else None, "act")


# Ordinary French statutes are cited by their official number, not only codes by article:
# ``loi n° 2004-801`` / ``article 2 de la loi n° 78-17``.  Before this grammar the
# corpus could hold every LEGI article of a law and still report that nothing cited the
# law itself.
register(Grammar(
    "fr_statute_number", "act",
    re.compile(
        r"\b(?:(?:l['’])?article\s+(?P<article>\d+[A-Z]?)\s+(?:de\s+la|du)\s+)?"
        # Older CNIL material commonly omits the ``n°`` marker (``loi 78-17``),
        # while OCR may render it as ``no``.  The word ``loi`` and year-sequence
        # shape keep the optional marker precise.
        r"loi\s+(?:n(?:o|°)\s*)?(?P<number>\d{2,4}-\d+)\b",
        re.IGNORECASE,
    ),
    _statute_number_ref,
))


def _fixed_statute(number: str):
    def normalise(match: re.Match[str]) -> Normalised:
        article = normalise_article(match.group("article")) if match.group("article") else None
        return text_alias(number), f"Article {article}" if article else None, "act"
    return normalise


# The founding data-protection law is very often cited by date or familiar title only,
# especially in early CNIL deliberations.  These are identities, not fuzzy title guesses:
# both phrases denote Law 78-17.  Keep this narrow rather than teaching the resolver that
# any date-only French law can be inferred without an official alias.
register(Grammar(
    "fr_informatique_libertes_law", "act",
    re.compile(
        r"\b(?:(?:l['’])?article\s+(?P<article>\d+[A-Z]?)\s+(?:de\s+la|du)\s+)?"
        r"loi(?:\s+modifi[eé]e)?\s+(?:"
        r"du\s+6\s+janvier\s+1978\b"
        r"|(?:relative\s+à\s+)?l['’]informatique,?\s+aux\s+fichiers\s+et\s+aux\s+libert[eé]s\b"
        r"|informatique\s+et\s+libert[eé]s\b)",
        re.IGNORECASE,
    ),
    _fixed_statute("78-17"),
))

# The 2004 implementing law likewise appears under its long subject title without its
# number in constitutional and administrative material.
register(Grammar(
    "fr_personal_data_2004_law", "act",
    re.compile(
        r"\b(?:(?:l['’])?article\s+(?P<article>\d+[A-Z]?)\s+(?:de\s+la|du)\s+)?"
        r"loi\s+relative\s+à\s+la\s+protection\s+des\s+personnes\s+physiques\s+"
        r"à\s+l['’][eé]gard\s+des\s+traitements\s+de\s+donn[eé]es\s+à\s+caract[eè]re\s+personnel",
        re.IGNORECASE,
    ),
    _fixed_statute("2004-801"),
))


# How each code is CITED — the names a judgment actually prints, which are also the
# extraction grammar's alternation. The official register titles are separate
# (``_CODE_TITLES``): nobody writes "Code général des impôts, CGI." in prose.
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
    # The rest of the codified law the corpus holds. Without a key here, an article of
    # these codes is held under its LEGIARTI id and NOTHING resolves to it: the CGI alone
    # was 3,042 unresolvable references over 145,000 citations while all 22,832 of its
    # articles sat in the corpus.
    "cgian1": ("code général des impôts annexe I", "code general des impots annexe i"),
    "cgian2": ("code général des impôts annexe II", "code general des impots annexe ii"),
    "cgian3": ("code général des impôts annexe III", "code general des impots annexe iii"),
    "cgian4": ("code général des impôts annexe IV", "code general des impots annexe iv"),
    "crural": ("code rural et de la pêche maritime", "code rural et de la peche maritime",
               "code rural", "c. rur."),
    "cmf": ("code monétaire et financier", "code monetaire et financier", "cmf",
            "c. mon. fin."),
    "cch": ("code de la construction et de l'habitation",
            "code de la construction et de l’habitation", "cch"),
    "ctransports": ("code des transports",),
    "ceduc": ("code de l'éducation", "code de l’éducation", "code de l'education",
              "c. éduc."),
    "curb": ("code de l'urbanisme", "code de l’urbanisme", "code de l'urbanisme", "c. urb."),
    "casf": ("code de l'action sociale et des familles",
             "code de l’action sociale et des familles", "casf"),
    "cdef": ("code de la défense", "code de la defense"),
    "cenergie": ("code de l'énergie", "code de l’énergie", "code de l'energie"),
    "ccia": ("code du cinéma et de l'image animée",
             "code du cinema et de l'image animee"),
    "ctravmayotte": ("code du travail applicable à Mayotte",
                     "code du travail applicable a mayotte"),
    "ccommunes": ("code des communes",),
    "ccommunesnc": ("code des communes de la Nouvelle-Calédonie",
                    "code des communes de la nouvelle caledonie"),
    "csport": ("code du sport",),
    "cjf": ("code des juridictions financières", "code des juridictions financieres", "cjf"),
    "coj": ("code de l'organisation judiciaire", "code de l’organisation judiciaire", "coj"),
    "cpmivg": ("code des pensions militaires d'invalidité et des victimes de la guerre",
               "code des pensions militaires d’invalidité et des victimes de la guerre",
               "cpmivg"),
    "celect": ("code électoral", "code electoral"),
    "cforestier": ("code forestier",),
    "cgfp": ("code général de la fonction publique", "code general de la fonction publique",
             "cgfp"),
    "croute": ("code de la route", "c. route"),
    "cpatrimoine": ("code du patrimoine",),
    "cmutualite": ("code de la mutualité", "code de la mutualite"),
    "cpenitentiaire": ("code pénitentiaire", "code penitentiaire"),
    "ccp": ("code de la commande publique",),
    "caviation": ("code de l'aviation civile", "code de l’aviation civile"),
    "cibs": ("code des impositions sur les biens et services",),
    "cdouanes": ("code des douanes",),
    "cdouanesmayotte": ("code des douanes de Mayotte",),
    "ctourisme": ("code du tourisme",),
    "cg3p": ("code général de la propriété des personnes publiques",
             "code general de la propriete des personnes publiques", "cg3p"),
    "csn": ("code du service national",),
    "cmp": ("code des marchés publics", "code des marches publics"),
    "crecherche": ("code de la recherche",),
    "cportsmaritimes": ("code des ports maritimes",),
    "cdomaineetat": ("code du domaine de l'Etat", "code du domaine de l’État",
                     "code du domaine de l'etat"),
    "cpcex": ("code des procédures civiles d'exécution",
              "code des procédures civiles d’exécution",
              "code des procedures civiles d'execution", "cpce"),
    "cminier": ("code minier",),
    "cjmil": ("code de justice militaire",),
    "cexpro": ("code de l'expropriation pour cause d'utilité publique",
               "code de l’expropriation pour cause d’utilité publique",
               "code de l'expropriation"),
    "cvoirie": ("code de la voirie routière", "code de la voirie routiere"),
    "cjpm": ("code de la justice pénale des mineurs", "code de la justice penale des mineurs",
             "cjpm"),
    "cartisanat": ("code de l'artisanat", "code de l’artisanat"),
    "cpcmr": ("code des pensions civiles et militaires de retraite",),
    "cfas": ("code de la famille et de l'aide sociale",
             "code de la famille et de l’aide sociale"),
    "ctacaa": ("code des tribunaux administratifs et des cours administratives d'appel",
               "code des tribunaux administratifs et des cours administratives d’appel"),
    "cdpf": ("code du domaine public fluvial et de la navigation intérieure",
             "code du domaine public fluvial et de la navigation interieure"),
    "cnat": ("code de la nationalité française", "code de la nationalite francaise"),
    "cvin": ("code du vin",),
    "clegion": ("code de la Légion d'honneur, de la Médaille militaire et de l'ordre "
                "national du Mérite",
                "code de la legion d'honneur de la medaille militaire et de l'ordre "
                "national du merite"),
    "ctravmaritime": ("code du travail maritime",),
    "cdebits": ("code des débits de boissons et des mesures contre l'alcoolisme",
                "code des debits de boissons et des mesures contre l'alcoolisme"),
}

# The titles the register publishes articles UNDER, where they differ from the cited
# form: DILA appends the abbreviation (", CGI."), a full stop, or a disambiguator. These
# only key held articles on ingest — they are not extraction grammar.
#
# A superseded or territorial version of a code gets its OWN key, never the current
# code's: "Code rural ancien" article L. 411-1 is a different text from the same-numbered
# article of the Code rural et de la pêche maritime, and sharing a key would resolve a
# citation to whichever of the two happened to be minted last.
_CODE_TITLES = {
    "Code général des impôts, CGI.": "cgi",
    "Code général des impôts annexe I, CGIANI.": "cgian1",
    "Code général des impôts, annexe II, CGIANII.": "cgian2",
    "Code général des impôts, annexe III, CGIANIII.": "cgian3",
    "Code général des impôts, annexe IV, CGIANIV.": "cgian4",
    "Code forestier (nouveau)": "cforestier",
    "Code forestier": "cforestieranc",
    "Code forestier de Mayotte": "cforestiermayotte",
    "Code minier (nouveau)": "cminier",
    "Code minier": "cminieranc",
    "Code rural ancien": "cruralanc",
    "Code de procédure civile (1807)": "cprociv1807",
    "Code des pensions militaires d'invalidité et des victimes de guerre.": "cpmivganc",
}


def _norm_title(value: str) -> str:
    """Fold accents/punctuation/case so a register title and a cited name compare equal
    ("Code de la sécurité sociale." == "code de la securite sociale")."""
    return re.sub(r"[^a-z0-9]+", " ", _fold(value)).strip()


_CODE_TITLE_INDEX = {_norm_title(t): key for t, key in _CODE_TITLES.items()}
_CODE_NAME_INDEX = {_norm_title(n): key for key, names in _CODE_NAMES.items() for n in names}


def code_key(title: str) -> str | None:
    """The stable key for a French code, from either its cited name or its register
    title. The register title wins: it is the more specific of the two, and it is what
    distinguishes "Code forestier (nouveau)" from the code it replaced."""
    folded = _norm_title(title)
    return _CODE_TITLE_INDEX.get(folded) or _CODE_NAME_INDEX.get(folded)


def normalise_article(value: str) -> str:
    value = normalise_fr_number(value).replace(".", "")
    return value


def code_article_alias(title: str, article: str) -> str | None:
    key = code_key(title)
    return f"fr:code:{key}:{normalise_article(article)}" if key and article else None


# Longest name first ACROSS the whole table, not just within one code: the alternation
# is first-match, so "code des douanes" listed before "code des douanes de Mayotte" would
# key every Mayotte citation to the metropolitan code.
_CODE_ALT = "|".join(
    re.escape(name)
    for name in sorted({n for names in _CODE_NAMES.values() for n in names},
                       key=len, reverse=True)
)
_ARTICLE = r"(?P<article>(?:L|R|D|A|LO)?\s*\.?\s*\d{1,5}(?:-\d+)*(?:-\d+)?(?:\s*(?-i:[A-Z]))?)"
# Atomic because this token sits inside a repeated article-list expression.  Without
# it, an English-language CJEU judgment containing many ordinary "Article N" phrases
# made ``re`` revisit every optional prefix/space/list split while looking in vain for
# a later French host: two 40k-character judgments exceeded the stage's 90-second
# budget.  There is no useful alternate parse once the token's digits have been read.
_ARTICLE_TOKEN = r"(?>(?:L|R|D|A|LO)?\s*\.?\s*\d{1,5}(?:-\d+)*(?:\s*(?-i:[A-Z]))?)"


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


_FR_ARTICLE_LIST = rf"(?P<list>(?>{_ARTICLE_TOKEN}(?:\s*(?:,|et|à|a)\s*{_ARTICLE_TOKEN})*))"
_FR_ARTICLE_LIST_MULTI = rf"(?P<list>(?>{_ARTICLE_TOKEN}(?:\s*(?:,|et|à|a)\s*{_ARTICLE_TOKEN})+))"
_FR_CODE_LIST = re.compile(
    rf"\b(?:articles?|arts?\.)\s+{_FR_ARTICLE_LIST_MULTI}\s+"
    rf"(?:du|de la|des|d['’]u?)\s+(?P<host>{_CODE_ALT})\b", re.IGNORECASE)
_FR_ECHR_LIST = re.compile(
    rf"\b(?:articles?|arts?\.)\s+{_FR_ARTICLE_LIST}\s+"
    r"(?:de\s+la|de\s+l['’]|du)\s+Convention\s+européenne"
    r"(?:\s+de\s+sauvegarde)?\s+des\s+droits\s+de\s+l['’](?:homme|Homme)"
    r"(?:\s+et\s+des\s+libertés\s+fondamentales)?\b", re.IGNORECASE)
_FR_ARTICLE_VALUE = re.compile(_ARTICLE_TOKEN, re.IGNORECASE)
# French judgments normally put the pinpoint *before* the formal EU instrument:
# ``l'article 13 du règlement (UE) 2016/679``.  The generic numeric-instrument
# grammar recognised the Regulation but threw the preceding article away, making
# hundreds of French GDPR decisions look instrument-level only.  Lists and the
# common paragraph/subpoint form are expanded here because one Grammar match can
# emit only one edge.
_FR_EU_KIND = r"règlement|reglement|directive|décision|decision"
_FR_EU_ARTICLES = r"\d+[a-z]?(?:\s*(?:,|et|à|a)\s*\d+[a-z]?)*"
_FR_EU_ARTICLE_REF = re.compile(
    rf"\b(?:l['’])?(?:articles?|arts?\.)\s+(?P<list>{_FR_EU_ARTICLES})"
    r"(?:\s*,?\s*(?:paragraphe|§)\s*(?P<para>\d+)"
    r"(?:\s*,?\s*(?:sous\s+)?(?P<letter>[a-z])\)?)?)?\s*,?\s+"
    rf"(?:du|de\s+la|de\s+l['’])\s+(?P<kind>{_FR_EU_KIND})\s*"
    r"(?:\((?:UE|CE|CEE)\)\s*)?(?:n(?:o|°)\s*)?"
    r"(?P<a>\d{1,4})/(?P<b>\d{1,4})(?:/(?:UE|CE|CEE))?\b",
    re.IGNORECASE,
)
_FR_EU_ARTICLE_VALUE = re.compile(r"\d+[a-z]?", re.IGNORECASE)

# CNIL deliberations frequently cite several provisions around a numbered law:
# ``articles 15 et 20 de la loi n° 78-17`` and ``loi n° 78-17, notamment ses
# articles 45 et 46``.  A single Grammar result cannot represent several pinpoints,
# so expand both word orders here just as we do for French code and EU article lists.
_FR_STATUTE_NUMBER = r"(?:n(?:o|°)\s*)?(?P<number>\d{2,4}-\d+)"
_FR_STATUTE_PRE_LIST = re.compile(
    rf"\b(?:articles?|arts?\.)\s+{_FR_ARTICLE_LIST}\s+"
    rf"(?:de\s+la|du)\s+loi\s+{_FR_STATUTE_NUMBER}\b", re.IGNORECASE)
_FR_STATUTE_POST_LIST = re.compile(
    rf"\bloi\s+{_FR_STATUTE_NUMBER}\b[^.;\n]{{0,60}}?"
    # The possessive is essential: without it, ``la loi 2004-801 modifie
    # l'article 2 de la loi 78-17`` incorrectly gives Article 2 to both laws.
    rf"(?:dans\s+)?ses\s+(?:articles?|arts?\.)\s+{_FR_ARTICLE_LIST}",
    re.IGNORECASE)


def _fr_eu_kind(value: str) -> str:
    return {
        "règlement": "regulation", "reglement": "regulation",
        "décision": "decision", "decision": "decision",
    }.get(value.casefold(), "directive")


# --- Belgian French: the APD's Chambre Contentieuse ---------------------------------
# The Belgian authority publishes each decision in the language of the procedure, so a
# Dutch-titled listing entry is frequently a French PDF. Belgian French cites a GDPR
# provision with dots — "Article 5.1.f du RGPD", "article 4.12) du RGPD" — which is the
# EU institutional style rather than France's "article 5, paragraphe 1, point f". On a
# 311k-character decision the generic French grammar found two citations and missed every
# one of these, so the decisions that turn on Article 5(1)(f) did not link to it at all.
_BE_RGPD_RE = re.compile(
    r"\b[Aa]rt(?:icle)?\.?\s+(?P<article>\d{1,3}[a-z]?)"
    r"(?:\.(?P<para>\d{1,2}))?"
    r"(?:\.(?P<point>\d{1,2}|[a-z])\)?)?"
    r"\s*(?:,?\s*(?:du|de\s+la)\s+)?RGPD\b",
)

# The two Belgian framework acts, cited by initialism throughout: the LCA created the
# authority and confers the Chamber's corrective and fining powers; the LTD carries
# Belgium's national derogations. Both subdivide with "§ 1er" and "9°", which no French
# pattern expected because France does not draft that way.
_BE_LAWS = {
    "LCA": "Loi du 3 décembre 2017 portant création de l'Autorité de protection des données",
    "LTD": ("Loi du 30 juillet 2018 relative à la protection des personnes physiques "
            "à l'égard des traitements de données à caractère personnel"),
    "LVP": "Loi du 8 décembre 1992 relative à la protection de la vie privée",
    # The same widening on the French side: direct-marketing and cookie decisions run on
    # the CDE, police-data ones on the LFP.
    "CDE": "Code de droit économique",
    "LFP": "Loi du 5 août 1992 sur la fonction de police",
}
_BE_LAW_RE = re.compile(
    # Same shapes on the French side: "article VI.110 du CDE", "article 44/1 de la LFP".
    r"\b[Aa]rt(?:icle)?\.?\s+(?P<article>(?:[IVXL]{1,5}\.)?\d{1,4}(?:/\d{1,2})?[a-z]?)"
    r"(?:\s*,?\s*§\s*(?P<para>\d+)(?:er)?)?"
    r"(?:\s*,?\s*(?P<item>\d+)\s*°)?"
    r"\s*(?:,?\s*(?:de\s+la|du)\s+)?(?P<law>LCA|LTD|LVP|CDE|LFP)\b",
)


def _be_pin(article: str, para: str | None = None, point: str | None = None) -> str:
    out = f"Article {article}"
    if para:
        out += f"({para})"
    if point:
        out += f"({point})"
    return out


def belgian_french_citations(text: str) -> list[Citation]:
    """Belgian French GDPR and framework-act references."""
    out: list[Citation] = []
    for m in _BE_RGPD_RE.finditer(text):
        out.append(Citation(
            raw=m.group(0), entity_kind="regulation", candidate_id="32016R0679",
            pinpoint=_be_pin(m.group("article"), m.group("para"), m.group("point")),
            char_start=m.start(), char_end=m.end(),
            method="be_fr_rgpd_article", confidence=1.0,
        ))
    for m in _BE_LAW_RE.finditer(text):
        title = _BE_LAWS[m.group("law").upper()]
        pin = f"Article {m.group('article')}"
        if m.group("para"):
            pin += f", § {m.group('para')}"
        if m.group("item"):
            pin += f", {m.group('item')}°"
        out.append(Citation(
            raw=m.group(0), entity_kind="act",
            candidate_id="be:law:" + re.sub(r"[^a-z0-9]+", " ", title.lower()).strip(),
            pinpoint=pin, char_start=m.start(), char_end=m.end(),
            method="be_fr_law_reference", confidence=.97,
        ))
    return out


def french_citations(text: str) -> list[Citation]:
    """Expand compact French article lists to canonical, pinpointed graph edges."""
    out: list[Citation] = list(belgian_french_citations(text))
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
    for m in _FR_EU_ARTICLE_REF.finditer(text):
        kind = _fr_eu_kind(m.group("kind"))
        candidate = _eu_celex(kind, m.group("a"), m.group("b"))
        articles = [am.group(0) for am in _FR_EU_ARTICLE_VALUE.finditer(m.group("list"))]
        for article in articles:
            pinpoint = f"Article {article}"
            # Paragraph/subpoint syntax can only follow one article, not a list.
            if len(articles) == 1 and m.group("para"):
                pinpoint += f"({m.group('para')})"
                if m.group("letter"):
                    pinpoint += f"({m.group('letter').lower()})"
            out.append(Citation(
                raw=m.group(0), entity_kind=kind, candidate_id=candidate,
                pinpoint=pinpoint, char_start=m.start(), char_end=m.end(),
                method="fr_eu_articles", confidence=1.0,
            ))
    for rx in (_FR_STATUTE_PRE_LIST, _FR_STATUTE_POST_LIST):
        for m in rx.finditer(text):
            candidate = text_alias(m.group("number"))
            for am in _FR_ARTICLE_VALUE.finditer(m.group("list")):
                value = normalise_article(am.group(0))
                out.append(Citation(
                    raw=m.group(0), entity_kind="act", candidate_id=candidate,
                    pinpoint=f"Article {value}", char_start=m.start(), char_end=m.end(),
                    method="fr_statute_articles", confidence=1.0,
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
    re.compile(rf"\b(?P<kind>{_FR_EU_KIND})\s*(?:\((?:UE|CE|CEE)\)\s*)?"
               r"(?:n(?:o|°)\s*)?(?P<a>\d{1,4})/(?P<b>\d{1,4})"
               r"(?:/(?:UE|CE|CEE))?\b", re.IGNORECASE),
    lambda m: (_eu_celex(_fr_eu_kind(m.group("kind")), m.group("a"), m.group("b")),
               None, _fr_eu_kind(m.group("kind"))),
))
