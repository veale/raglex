"""Spanish citation grammar — the whole provision ladder, EU and domestic.

Spanish legal drafting subdivides more finely than any other language in this corpus,
and it writes the same subdivision two ways. ``artículo 6.1.f) del RGPD`` and
``artículo 6, apartado 1, letra f), del RGPD`` are one citation, not two, and an AEPD
resolución will use both forms on the same page. Every reading here is normalised to
the anchor the corpus already holds for that instrument — the Formex form
(``Article 6(1)(f)``) for an EU act, whose Articles are stored as segments with exactly
those labels, and the Spanish dotted form (``Artículo 6.1.f)``) for a Spanish one.

The ladder, in the order Spanish stacks it:

    artículo → apartado → letra → punto/inciso/ordinal
    anexo (Roman) → punto/sección
    considerando
    disposición adicional / transitoria / final / derogatoria + ordinal
    título / capítulo / sección (Roman)

and every rung is optional, may be spelled out or compressed into a dot, and may be
followed by a list (``artículos 13 y 14``) or a range (``artículos 15 a 22``). A range
IS a citation of each member — the AEPD's standard recital of the rights provisions is
"artículos 15 a 22 del RGPD" — so it is expanded rather than read as two endpoints.

## Two word orders, because Spanish uses both

    artículo 5.1.a) del RGPD                  pinpoint first  (the ordinary form)
    el RGPD, en su artículo 5.1.a)            host first      (recitals and vistos)

Both are matched, because a grammar that only knew the first read the AEPD's standard
opening paragraph — "el RGPD, en sus artículos 13 y 14" — as no citation at all.

## Why the short names are guarded

This pass runs over every document in the corpus, not only Spanish ones. ``CE`` is the
Constitución in Madrid and the European Community, a court of appeal and a company
suffix everywhere else; ``ET`` is the Estatuto de los Trabajadores and an English verb
form. Anything at or below :data:`_NEEDS_CONTEXT_LEN` characters has to be accompanied
by a word of Spanish legal vocabulary, and the worst offenders are marked
``pinpoint_only``: they are only ever read inside an explicit ``artículo N X`` frame,
never as a bare mention. This is the same guard ``de_laws`` applies with the German
article and ``estonian`` with its own; it exists because "DSA" was minting Digital
Services Act references from a duty solicitor advice scheme.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Citation


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in value if not unicodedata.combining(c)).casefold()


# ---------------------------------------------------------------------------
# identifiers
# ---------------------------------------------------------------------------

GDPR_ID = "32016R0679"
AI_ACT_ID = "32024R1689"
# Ley Orgánica 3/2018, de 5 de diciembre, de Protección de Datos Personales y garantía
# de los derechos digitales — universally cited as LOPDGDD (LOPD in older material).
# The id shape predates this module and is preserved: ``es:ley:{form}-{number}-{year}``.
LOPDGDD_ID = "es:ley:lo-3-2018"

#: How a Spanish instrument's own name maps onto the id form. The FORM is part of the
#: identity, not decoration: Ley 3/2018 and Ley Orgánica 3/2018 are different laws
#: published in the same year, and dropping the ``lo`` would merge them.
_ES_FORMS: tuple[tuple[str, str], ...] = (
    # longest first — the alternation is first-match, and "Real Decreto Legislativo"
    # begins with "Real Decreto".
    (r"Real\s+Decreto\s+Legislativo", "rdleg"),
    (r"Real\s+Decreto[\s-]+ley", "rdl"),
    (r"Real\s+Decreto", "rd"),
    (r"Ley\s+Org[áa]nica", "lo"),
    (r"Ley", "l"),
)
_ES_FORM_LOOKUP = tuple((re.compile(rf"^{pattern}$", re.I), code)
                        for pattern, code in _ES_FORMS)


def es_law_id(form: str, number: str, year: str) -> str:
    """``("Ley Orgánica", "3", "2018")`` → ``es:ley:lo-3-2018``."""
    normalised = " ".join(str(form or "").split())
    code = next((c for rx, c in _ES_FORM_LOOKUP if rx.match(normalised)), "l")
    return f"es:ley:{code}-{number}-{year}"


def _named(slug: str) -> str:
    """An unnumbered Spanish instrument — the codes and the Constitution."""
    return f"es:ley:{slug}"


# ---------------------------------------------------------------------------
# the instruments, by the names Spain actually prints
# ---------------------------------------------------------------------------
#
# ``pinpoint_only`` marks a name too short or too common to be trusted on its own: it is
# read only inside an explicit article frame ("art. 18.4 CE"), never as a bare mention.

class _Host:
    __slots__ = ("candidate", "kind", "names", "pinpoint_only")

    def __init__(self, candidate: str, kind: str, names: tuple[str, ...],
                 pinpoint_only: bool = False) -> None:
        self.candidate, self.kind = candidate, kind
        self.names, self.pinpoint_only = names, pinpoint_only


#: EU instruments as Spanish names them. Spain translates the acquis and then
#: abbreviates the translation, so the same regulation is "RGPD", "Reglamento General de
#: Protección de Datos" and "Reglamento (UE) 2016/679" in three consecutive sentences.
EU_HOSTS: tuple[_Host, ...] = (
    _Host(GDPR_ID, "regulation",
          ("RGPD", "RGPD-UE", "Reglamento General de Protección de Datos",
           "Reglamento general de protección de datos")),
    _Host(AI_ACT_ID, "regulation",
          ("Reglamento de Inteligencia Artificial", "Reglamento de IA",
           "Reglamento (UE) de Inteligencia Artificial", "Reglamento europeo de IA",
           "Ley de Inteligencia Artificial", "Ley de IA", "RIA")),
    _Host("32022R2065", "regulation",
          ("Reglamento de Servicios Digitales", "Reglamento de servicios digitales",
           "RSD", "DSA")),
    _Host("32022R1925", "regulation",
          ("Reglamento de Mercados Digitales", "Reglamento de mercados digitales",
           "RMD", "DMA")),
    _Host("32018R1725", "regulation",
          ("Reglamento (UE) 2018/1725", "RPDUE")),
    _Host("32023R2854", "regulation", ("Reglamento de Datos", "Reglamento de datos")),
    _Host("32022R0868", "regulation",
          ("Reglamento de Gobernanza de Datos", "Reglamento de gobernanza de datos")),
    _Host("32014R0910", "regulation", ("Reglamento eIDAS", "eIDAS")),
    _Host("32022L2555", "directive",
          ("Directiva NIS2", "Directiva NIS 2", "NIS2", "NIS 2")),
    _Host("32002L0058", "directive",
          ("Directiva sobre la privacidad y las comunicaciones electrónicas",
           "Directiva de privacidad electrónica", "Directiva ePrivacy")),
    _Host("32016L0680", "directive",
          ("Directiva (UE) 2016/680", "Directiva de protección de datos en el ámbito penal")),
    _Host("31995L0046", "directive", ("Directiva 95/46/CE", "Directiva 95/46")),
    _Host("32005L0029", "directive",
          ("Directiva sobre las prácticas comerciales desleales",)),
    _Host("12016E", "treaty",
          ("Tratado de Funcionamiento de la Unión Europea", "TFUE")),
    _Host("12016M", "treaty", ("Tratado de la Unión Europea", "TUE")),
    _Host("12012P", "treaty",
          ("Carta de los Derechos Fundamentales de la Unión Europea",
           "Carta de los Derechos Fundamentales", "CDFUE")),
    _Host("echr/convention", "treaty",
          ("Convenio Europeo de Derechos Humanos",
           "Convenio para la Protección de los Derechos Humanos y de las Libertades "
           "Fundamentales", "CEDH")),
)

#: Spanish instruments. The data-protection block first — it is what the AEPD cites on
#: every page — then the procedural and codified law its resolutions run on.
ES_HOSTS: tuple[_Host, ...] = (
    _Host(LOPDGDD_ID, "act",
          ("LOPDGDD", "LOPDgdd",
           "Ley Orgánica 3/2018, de 5 de diciembre, de Protección de Datos Personales "
           "y garantía de los derechos digitales")),
    # The 1999 Act, still cited in resolutions about conduct predating 25 May 2018.
    _Host("es:ley:lo-15-1999", "act", ("LOPD",)),
    _Host("es:ley:rd-1720-2007", "act", ("RDLOPD", "RLOPD")),
    _Host("es:ley:l-34-2002", "act",
          ("LSSICE", "LSSI-CE", "LSSI",
           "Ley de servicios de la sociedad de la información")),
    _Host("es:ley:l-39-2015", "act",
          ("LPACAP", "LPAC", "Ley del Procedimiento Administrativo Común de las "
           "Administraciones Públicas")),
    _Host("es:ley:l-40-2015", "act", ("LRJSP", "Ley de Régimen Jurídico del Sector Público")),
    _Host("es:ley:l-29-1998", "act", ("LJCA",)),
    _Host("es:ley:lo-6-1985", "act", ("LOPJ",)),
    _Host("es:ley:l-1-2000", "act", ("LEC",), pinpoint_only=True),
    _Host("es:ley:lecrim", "act", ("LECrim", "LECRIM", "Ley de Enjuiciamiento Criminal")),
    _Host("es:ley:lo-7-2021", "act",
          ("Ley Orgánica 7/2021", "LO 7/2021")),
    _Host("es:ley:lo-8-2021", "act", ("LOPIVI",)),
    _Host("es:ley:l-11-2022", "act", ("LGTel", "Ley General de Telecomunicaciones")),
    _Host("es:ley:rdleg-1-2007", "act",
          ("TRLGDCU", "Texto Refundido de la Ley General para la Defensa de los "
           "Consumidores y Usuarios")),
    _Host("es:ley:rdleg-2-2015", "act",
          ("Estatuto de los Trabajadores", "ET"), pinpoint_only=True),
    _Host(_named("constitucion-1978"), "act",
          ("Constitución Española", "Constitución española", "CE"),
          pinpoint_only=True),
    _Host(_named("codigo-civil"), "act",
          ("Código Civil", "Código civil", "CC"), pinpoint_only=True),
    _Host(_named("codigo-penal"), "act",
          ("Código Penal", "Código penal", "CP"), pinpoint_only=True),
)

ALL_HOSTS: tuple[_Host, ...] = EU_HOSTS + ES_HOSTS
_HOST_BY_NAME: dict[str, _Host] = {}
for _host in ALL_HOSTS:
    for _name in _host.names:
        _HOST_BY_NAME.setdefault(_fold(_name), _host)
# Longest name first ACROSS the whole table: the alternation is first-match, so
# "Reglamento de Datos" listed before "Reglamento de Gobernanza de Datos" would key every
# governance reference to the Data Act.
_HOST_ALT = "|".join(
    re.escape(name) for name in
    sorted({n for h in ALL_HOSTS for n in h.names}, key=len, reverse=True))

#: An acronym at or below this length is also an ordinary word somewhere in a 700k
#: document corpus, so it needs a Spanish legal word beside it before it counts.
_NEEDS_CONTEXT_LEN = 5
_CONTEXT_RE = re.compile(
    r"(?:\b(?:art[íi]culos?|apartados?|reglamentos?|directivas?|ley(?:es)?|"
    r"resoluci[óo]n|sentencia|tratado|convenio|carta|considerando|anexo|"
    r"conformidad|virtud|tenor|dispone|establece|previsto|prevista|infracci[óo]n|"
    r"tipificada|Uni[óo]n\s+Europea|espa[ñn]ol[ao])\b|\bart\.)", re.IGNORECASE)
_CONTEXT_WINDOW = 80


#: Short names that are nevertheless unambiguous across the whole corpus, because
#: nothing else spells them. "RGPD" is the GDPR in Spanish, French, Portuguese and
#: Romanian and is not a word or an initialism of anything else — it is already in the
#: extractor's protected-shorthand table for that reason — so requiring a Spanish word
#: beside it only loses citations. Contrast "RIA", which is a Spanish river, an English
#: regulatory impact assessment and the AI Act, and stays guarded.
_UNAMBIGUOUS = frozenset(_fold(n) for n in (
    "RGPD", "RGPD-UE", "LOPD", "LOPDGDD", "LOPDgdd", "NIS2", "NIS 2", "eIDAS", "CDFUE",
    "TFUE", "CEDH", "LSSI", "LSSICE", "LSSI-CE", "LOPJ", "LJCA", "LPACAP", "LPAC",
    "LRJSP", "RDLOPD", "RLOPD", "RPDUE", "TRLGDCU", "LOPIVI", "LECrim", "LECRIM",
    "LGTel",
))


def _needs_context(name: str) -> bool:
    if _fold(name) in _UNAMBIGUOUS:
        return False
    return len(re.sub(r"[^0-9A-Za-zÀ-ɏ]+", "", name or "")) <= _NEEDS_CONTEXT_LEN


def _has_context(text: str, start: int, end: int) -> bool:
    window = (text[max(0, start - _CONTEXT_WINDOW):start] + " "
              + text[end:end + _CONTEXT_WINDOW])
    return bool(_CONTEXT_RE.search(window))


# ---------------------------------------------------------------------------
# the numbered forms — instruments cited by number rather than by name
# ---------------------------------------------------------------------------
#
# ``n.º`` is the ordinal-marker Spain prints; OCR renders it as ``n°``, ``nº``, ``no.``
# and ``num.`` indifferently, so all are accepted.
_NO = r"(?:n[\.º°ºo]{0,3}\s*|n[úu]m\.?\s*)?"
_EU_KIND = (r"Reglamento\s+Delegado|Reglamento\s+de\s+Ejecuci[óo]n|Reglamento"
            r"|Directiva\s+Delegada|Directiva"
            r"|Decisi[óo]n\s+de\s+Ejecuci[óo]n|Decisi[óo]n\s+Marco|Decisi[óo]n")
_EU_NUMBERED = (rf"(?P<eukind>{_EU_KIND})\s*"
                r"(?:\(\s*(?:UE|CE|CEE|Euratom)\s*\)\s*)?" + _NO +
                r"(?P<a>\d{1,4})\s*/\s*(?P<b>\d{1,4})"
                r"(?:\s*/\s*(?:UE|CE|CEE|JAI|Euratom))?")
_ES_KIND = (r"Real\s+Decreto\s+Legislativo|Real\s+Decreto[\s-]+ley|Real\s+Decreto"
            r"|Ley\s+Org[áa]nica|Ley")
_ES_NUMBERED = (rf"(?P<eskind>{_ES_KIND})\s*" + _NO +
                r"(?P<num>\d{1,4})\s*/\s*(?P<year>(?:19|20)\d{2})")

_EU_DESCRIPTOR = {"reglamento": "regulation", "directiva": "directive",
                  "decision": "decision", "decision marco": "framework decision"}


def _eu_kind_of(word: str) -> str:
    """"Reglamento Delegado" and "Reglamento de Ejecución" are still regulations — the
    qualifier changes the procedure, not the CELEX sector descriptor."""
    head = _fold(word).split()[0]
    if head == "decision" and "marco" in _fold(word):
        return "framework decision"
    return _EU_DESCRIPTOR.get(head, "regulation")


# ---------------------------------------------------------------------------
# the provision ladder
# ---------------------------------------------------------------------------

_ART_WORD = r"(?:art[íi]culos?|arts?\.|art\b)"
#: ``6``, ``43 bis``, ``12 ter``. The Latin ordinals are part of the article number, not
#: a subdivision of it: Article 43 bis is a different article from Article 43.
_SUFFIX = r"(?:\s*(?:bis|ter|qu[áa]ter|quater|quinquies|sexies|septies|octies|nonies|decies))"
#: The subdivisions spelled out. Order is not fixed in practice, so each rung is
#: optional and independent rather than sequenced.
_SPELLED = (r"(?:\s*,?\s*(?:apartados?|apdos?\.|aps?\.|p[áa]rrafos?|parr\.)\s*"
            r"(?P<para>\d{1,3}(?:\s*[.º°ª]{0,2})?))?"
            r"(?:\s*,?\s*(?:letras?|lets?\.)\s*(?P<letter>[a-z])\)?)?"
            r"(?:\s*,?\s*(?:puntos?|pto\.|incisos?|ordinales?|n[úu]meros?|n[úu]ms?\.)\s*"
            r"(?P<point>\d{1,3}|[a-z])\)?)?")
_TAIL = _SPELLED
#: ``artículos 13 y 14``, ``arts. 6, 9 y 32``, ``artículos 15 a 22``, and the compact
#: dotted ladder ``6.1.f)`` — which belongs to the TOKEN, not to the list, because each
#: member of a list carries its own: "artículos 6.1.f) y 9.2.a)". Kept atomic because
#: this token sits inside a repetition; without it a long EU judgment full of "artículo
#: N" phrases made ``re`` revisit every optional rung looking in vain for a host that
#: never comes. There is no useful alternate parse once the digits are read.
#: One rung of the compact dotted ladder. A LETTERED rung must either close its
#: parenthesis or follow the dot with no space at all: "el artículo 9. Recoge un
#: ejemplo…" is a sentence ending, and reading its "R" as a rung produced
#: ``Article 9(r)``. A numeric rung has no such ambiguity — a sentence does not begin
#: with a bare digit — so it stays permissive.
_RUNG = r"(?:\s*\.\s*\d{1,3}\)?|\s*\.\s*[a-z]\)|\.[a-z](?![^\W\d_]))"
#: The trailing ``\s+[a-z]\)`` is the AEPD's spacing habit — "art. 5.1 d)" — and needs
#: the closing parenthesis, or "artículo 5 y el artículo 6" would read "y" as a rung.
_ARTICLE_TOKEN = rf"(?>\d{{1,3}}{_SUFFIX}?{_RUNG}{{0,3}}(?:\s+[a-z]\))?)"
_SEP = r"(?:\s*,\s*|\s+(?:y|e|o|u)\s+|\s*(?:,\s*)?\ba\b\s+)"
_LIST = rf"(?P<list>(?>{_ARTICLE_TOKEN}(?:{_SEP}{_ARTICLE_TOKEN})*))"
#: The connector between pinpoint and host. ``del``/``de la``/``de los`` carry the
#: genitive; the AEPD also writes plain ``, RGPD`` in tables, and a bare space in the
#: form every Spanish lawyer uses for the constitution — "art. 18.4 CE". The bare space
#: is safe here only because the host alternation is a closed vocabulary of named
#: instruments and numbered forms, never an open noun.
_OF = (r"(?:\s*,?\s*(?:d[eo]l?|de\s+la|de\s+los|de\s+las|en\s+el|en\s+la)\s+"
       r"|\s*,\s*|\s+)")
#: The reverse order: "el RGPD, en sus artículos 13 y 14" — and the same with the law's
#: date wedged in, which is how Spain names a statute in full: "la Ley 39/2015, de 1 de
#: octubre, en su artículo 77". The possessive is what makes the gap safe. Without it,
#: "la Ley 40/2015 modifica el artículo 77 de la Ley 39/2015" would give Article 77 to
#: both laws — the mistake the French grammar records having made.
_POSSESSIVE = r"(?:[^.;:\n]{0,90}?)?\s*,?\s*(?:en\s+)?(?:su|sus)\s+"
#: …and the terse form, where the article follows the instrument immediately.
_ADJACENT = r"\s*,\s*"

_ROMAN = r"(?:[IVXLCDM]{1,7})"
#: ``anexo III``, ``Anexo I, punto 2``, ``anexo 4``. Roman is the drafting convention;
#: the arabic spelling occurs and folds onto the same anchor downstream.
_ANNEX = (rf"(?:anexos?)\s+(?P<annex>{_ROMAN}|\d{{1,2}})"
          r"(?:\s*,?\s*(?:secci[óo]n|parte)\s*(?P<section>[A-Z]|\d{1,2}|" + _ROMAN + r"))?"
          r"(?:\s*,?\s*(?:puntos?|pto\.|apartados?)\s*(?P<apoint>\d{1,3}[a-z]?))?")
#: …and its reverse, which the Commission's Spanish drafting prefers:
#: "el punto 2 del anexo III".
_ANNEX_REVERSED = (r"(?:puntos?|pto\.|apartados?)\s*(?P<rapoint>\d{1,3}[a-z]?)"
                   rf"\s+del\s+(?:anexos?)\s+(?P<rannex>{_ROMAN}|\d{{1,2}})")
_RECITAL = r"(?:considerandos?)\s*\(?\s*(?P<recital>\d{1,3})\s*\)?"

#: ``disposición adicional primera`` / ``disposición final séptima`` / ``d.a. 3.ª``.
#: These are numbered in ordinal WORDS, which no other language in the corpus does; they
#: are preserved verbatim because that is how the BOE labels the segment.
_DISPOSICION = (r"disposici[óo]n(?:es)?\s+(?P<dkind>adicional(?:es)?|transitoria(?:s)?"
                r"|final(?:es)?|derogatoria(?:s)?)\s+(?P<dord>[a-záéíóúñ]+"
                r"|\d{1,2}\s*[.ºª°]{0,2})")


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


_TOKEN_SPLIT = re.compile(
    rf"^\s*(?P<article>\d{{1,3}}{_SUFFIX}?)"
    rf"(?P<tail>{_RUNG}*(?:\s+[a-z]\))?)\s*$", re.IGNORECASE)
_TOKEN_RUNG = re.compile(r"[.\s]\s*(\d{1,3}|[a-z])(?![^\W\d_])", re.IGNORECASE)


def _split_token(token: str) -> tuple[str, list[str]]:
    """``"6.1.f)"`` → ``("6", ["1", "f"])``.

    The dotted rungs live on the token rather than on the surrounding match, because
    each member of a list carries its own: in ``artículos 6.1.f) y 9.2.a)`` the second
    article's subdivisions are not the first's.
    """
    match = _TOKEN_SPLIT.match(token or "")
    if not match:
        return _clean(token), []
    rungs = [m.group(1).casefold() for m in _TOKEN_RUNG.finditer(match.group("tail"))]
    return _clean(match.group("article")), rungs


def _spelled_rungs(match: re.Match[str]) -> list[str]:
    """The subdivisions the citation spelled out rather than compressed into a dot.

    Both notations are collected rather than chosen between, because Spanish mixes them
    freely inside one citation — ``artículo 83.5, letra b)`` is a real and common shape,
    and reading only one branch loses half of it.
    """
    groups = match.groupdict()
    parts = [groups.get(name) for name in ("para", "letter", "point")]
    return [re.sub(r"[.º°ª\s]+$", "", _clean(p)).casefold() for p in parts if p]


def _pin(token: str, rungs: list[str], kind: str) -> str:
    """The anchor, in whichever notation the instrument's own segments carry.

    An EU act's Articles are stored with Formex labels (``Article 6(1)(f)``), so an
    anchor written any other way cannot join them. Spanish acts are not held, so the
    dotted form is used for them — the point there is only that every spelling of one
    provision groups with the others, which reproducing the source's wording would
    defeat: ``artículo 6, apartado 1, letra f)`` would sit apart from ``artículo 6.1.f)``.
    """
    article, dotted = _split_token(token)
    ladder = dotted + rungs
    if kind != "act":
        return f"Article {article}" + "".join(f"({rung})" for rung in ladder)
    if not ladder:
        return f"Artículo {article}"
    tail = ".".join(ladder)
    return f"Artículo {article}.{tail})" if ladder[-1].isalpha() else f"Artículo {article}.{tail}"


#: How far a range may be expanded. "artículos 15 a 22" is eight real citations; a
#: mis-parse that read "artículos 5 a 2016/679" as a range would otherwise manufacture
#: two thousand. The ceiling is what makes expansion safe rather than the parse.
_MAX_RANGE = 30


def _expand(list_text: str) -> list[tuple[str, int, int]]:
    """Article tokens in a list, with a range (``15 a 22``) expanded to its members.

    Each expanded member carries the span of the range expression it came from, because
    it has no span of its own — the document never printed "artículo 18".
    """
    tokens = [(m.group(0), m.start(), m.end())
              for m in re.finditer(_ARTICLE_TOKEN, list_text, re.I)]
    out: list[tuple[str, int, int]] = []
    for index, (token, start, end) in enumerate(tokens):
        out.append((token, start, end))
        if index + 1 >= len(tokens):
            continue
        between = list_text[end:tokens[index + 1][1]]
        if not re.fullmatch(r"\s*(?:,\s*)?a\s*", between, re.I):
            continue
        low, high = token, tokens[index + 1][0]
        if not (low.isdigit() and high.isdigit()):
            continue
        first, last = int(low), int(high)
        if not 0 < last - first <= _MAX_RANGE:
            continue
        out.extend((str(n), start, tokens[index + 1][2])
                   for n in range(first + 1, last))
    return out


# ---------------------------------------------------------------------------
# the patterns
# ---------------------------------------------------------------------------

_NAMED_HOST = rf"(?P<name>{_HOST_ALT})"
_ANY_HOST = rf"(?:{_NAMED_HOST}|{_EU_NUMBERED}|{_ES_NUMBERED})"

#: "artículo 6.1.f) del RGPD" — pinpoint first, the ordinary Spanish order.
_ARTICLES_THEN_HOST = re.compile(
    rf"\b(?:los\s+|las\s+|el\s+|la\s+)?{_ART_WORD}\s*{_LIST}{_TAIL}{_OF}{_ANY_HOST}\b",
    re.IGNORECASE)
#: "el RGPD, en sus artículos 13 y 14" — host first, which vistos and recitals prefer.
_HOST_THEN_ARTICLES = re.compile(
    rf"\b{_ANY_HOST}{_POSSESSIVE}{_ART_WORD}\s*{_LIST}{_TAIL}", re.IGNORECASE)
#: "RGPD, artículo 5" — the header and table form, where nothing intervenes.
_HOST_ADJACENT_ARTICLES = re.compile(
    rf"\b{_ANY_HOST}{_ADJACENT}{_ART_WORD}\s*{_LIST}{_TAIL}", re.IGNORECASE)
#: The instrument alone.
_HOST_ONLY = re.compile(rf"\b{_ANY_HOST}\b", re.IGNORECASE)
#: "anexo III, punto 2 del Reglamento (UE) 2024/1689" and its reverse.
_ANNEX_THEN_HOST = re.compile(rf"\b{_ANNEX}{_OF}{_ANY_HOST}\b", re.IGNORECASE)
_ANNEX_REVERSED_THEN_HOST = re.compile(
    rf"\b{_ANNEX_REVERSED}{_OF}{_ANY_HOST}\b", re.IGNORECASE)
#: "considerando 47 del RGPD".
_RECITAL_THEN_HOST = re.compile(rf"\b{_RECITAL}{_OF}{_ANY_HOST}\b", re.IGNORECASE)
#: "disposición adicional primera de la LOPDGDD".
_DISPOSICION_THEN_HOST = re.compile(
    rf"\b{_DISPOSICION}{_OF}{_ANY_HOST}\b", re.IGNORECASE)

_ROMAN_RE = re.compile(rf"^{_ROMAN}$")
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _roman_to_int(value: str) -> int | None:
    if not _ROMAN_RE.match(value.upper()):
        return None
    digits = [_ROMAN_VALUES[c] for c in value.upper()]
    return sum(-d if i + 1 < len(digits) and d < max(digits[i + 1:]) else d
               for i, d in enumerate(digits))


def _annex_pin(annex: str, section: str | None, point: str | None) -> str:
    """``Annex III, point 2``. Roman is kept as written — the corpus folds the two
    spellings onto one anchor key, and the Roman form is what the instrument prints."""
    number = annex.upper() if _roman_to_int(annex) else annex
    out = f"Annex {number}"
    if section:
        out += f", section {section}"
    if point:
        out += f", point {point}"
    return out


def _host_of(match: re.Match[str]) -> tuple[str, str] | None:
    """Whichever host branch matched → ``(candidate_id, entity_kind)``."""
    groups = match.groupdict()
    name = groups.get("name")
    if name:
        host = _HOST_BY_NAME.get(_fold(name))
        return (host.candidate, host.kind) if host else None
    if groups.get("eukind"):
        from .grammars import _eu_celex

        kind = _eu_kind_of(groups["eukind"])
        celex = _eu_celex(kind, groups["a"], groups["b"])
        return (celex, kind) if celex else None
    if groups.get("eskind"):
        return (es_law_id(groups["eskind"], groups["num"], groups["year"]), "act")
    return None


def _bare_name_allowed(match: re.Match[str], text: str) -> bool:
    """Whether a host matched with no pinpoint may still mint an edge.

    A name marked ``pinpoint_only`` may not — "CE" alone is not a reference to the
    Spanish Constitution anywhere in a corpus this size. A short name may, but only with
    Spanish legal vocabulary beside it.
    """
    name = match.groupdict().get("name")
    if not name:
        return True                        # a numbered form states its own identity
    host = _HOST_BY_NAME.get(_fold(name))
    if host is None or host.pinpoint_only:
        return False
    return not _needs_context(name) or _has_context(text, match.start(), match.end())


#: Words that only a Spanish text contains. ``art.`` is not among them — French,
#: Italian, Dutch and Romanian all abbreviate the same way, and "art. 12 CE" in a
#: Conseil d'État judgment means the Conseil d'État, not the Constitución Española.
#: A ``pinpoint_only`` host needs one of these within :data:`_CONTEXT_WINDOW`.
_STRICT_CONTEXT_RE = re.compile(
    r"\b(?:art[íi]culos?|apartados?|letra|Constituci[óo]n|espa[ñn]ol\w*|"
    r"Ley(?:es)?|Real\s+Decreto|Tribunal\s+(?:Supremo|Constitucional)|sentencia|"
    r"conforme|en\s+virtud|dispone|establece|reclamante|resoluci[óo]n|"
    r"reclamaci[óo]n|derechos?\s+fundamentales?)\b", re.IGNORECASE)


def _pinpointed_host_allowed(match: re.Match[str], text: str) -> bool:
    """Whether a pinpointed reference to a short or ambiguous host may mint an edge.

    The article frame is strong evidence on its own for a distinctive name, so only the
    ``pinpoint_only`` hosts are re-checked here — and they are checked against Spanish
    vocabulary rather than the ``art.`` that half of Europe shares.
    """
    name = match.groupdict().get("name")
    if not name:
        return True
    host = _HOST_BY_NAME.get(_fold(name))
    if host is None:
        return False
    if not host.pinpoint_only:
        return True
    window = (text[max(0, match.start() - _CONTEXT_WINDOW):match.start()] + " "
              + text[match.end():match.end() + _CONTEXT_WINDOW])
    return bool(_STRICT_CONTEXT_RE.search(window)
                or re.search(r"art[íi]culos?", match.group(0), re.IGNORECASE))


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def _articles(text: str, pattern: re.Pattern[str], method: str,
              spans: list[tuple[int, int]]) -> list[Citation]:
    out: list[Citation] = []
    for m in pattern.finditer(text):
        host = _host_of(m)
        if not host or not _pinpointed_host_allowed(m, text):
            continue
        if any(s < m.end() and m.start() < e for s, e in spans):
            continue
        candidate, kind = host
        spelled = _spelled_rungs(m)
        members = _expand(m.group("list"))
        for index, (article, _start, _end) in enumerate(members):
            # Every member of one list carries the WHOLE match's span, not its own
            # token's. One Grammar match can only express one edge, so a list is
            # several citations of the same words — and ``_dedupe_overlaps`` keeps
            # them only for methods it knows do that (``exact_multi``). Giving each
            # member its own narrower span instead puts it inside the first member's,
            # and the dedupe drops every article after the first: "artículos 15 a 22
            # del RGPD" would resolve to Article 15 alone.
            out.append(Citation(
                raw=m.group(0), entity_kind=kind, candidate_id=candidate,
                # Only the FIRST article carries the SPELLED-OUT subdivisions:
                # "artículos 6, apartado 1, y 9" pins 6(1) and 9, not 9(1). Dotted
                # rungs ride on their own token, so "6.1.f) y 9.2.a)" keeps both.
                pinpoint=_pin(article, spelled if index == 0 else [], kind),
                char_start=m.start(), char_end=m.end(),
                # One method for the whole list, as the French list grammars do: the
                # overlap dedupe keeps several citations over one span only when they
                # share a method it knows expands lists. The confidence still separates
                # the articles the text printed from the members of an expanded range.
                method=method, confidence=.99 if index == 0 else .97,
            ))
        spans.append((m.start(), m.end()))
    return out


def spanish_citations(text: str) -> list[Citation]:
    """Every Spanish citation in ``text``.

    Ordered most specific first, and each match claims its span: a pinpointed reference
    must not also be recorded as a bare instrument mention, or the same sentence yields
    two edges, one of them anchor-less, and the instrument-level count inflates.
    """
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    out: list[Citation] = []

    out += _articles(text, _ARTICLES_THEN_HOST, "es_article", spans)
    out += _articles(text, _HOST_ADJACENT_ARTICLES, "es_host_article", spans)
    out += _articles(text, _HOST_THEN_ARTICLES, "es_host_article", spans)

    def free(start: int, end: int) -> bool:
        return not any(s < end and start < e for s, e in spans)

    for pattern, method in ((_ANNEX_REVERSED_THEN_HOST, "es_annex_point"),
                            (_ANNEX_THEN_HOST, "es_annex")):
        for m in pattern.finditer(text):
            host = _host_of(m)
            if not host or not free(m.start(), m.end()):
                continue
            groups = m.groupdict()
            annex = groups.get("annex") or groups.get("rannex") or ""
            point = groups.get("apoint") or groups.get("rapoint")
            spans.append((m.start(), m.end()))
            out.append(Citation(
                raw=m.group(0), entity_kind=host[1], candidate_id=host[0],
                pinpoint=_annex_pin(annex, groups.get("section"), point),
                char_start=m.start(), char_end=m.end(),
                method=method, confidence=.98))

    for m in _RECITAL_THEN_HOST.finditer(text):
        host = _host_of(m)
        if not host or not free(m.start(), m.end()):
            continue
        spans.append((m.start(), m.end()))
        out.append(Citation(
            raw=m.group(0), entity_kind=host[1], candidate_id=host[0],
            pinpoint=f"Recital {m.group('recital')}",
            char_start=m.start(), char_end=m.end(),
            method="es_recital", confidence=.98))

    for m in _DISPOSICION_THEN_HOST.finditer(text):
        host = _host_of(m)
        if not host or not free(m.start(), m.end()):
            continue
        spans.append((m.start(), m.end()))
        kind = _clean(m.group("dkind")).casefold().rstrip("s")
        # "adicionales" → "adicional", "transitorias" → "transitoria": the plural is the
        # heading of a group, and the ordinal that follows names one member of it.
        kind = {"adicionale": "adicional", "finale": "final",
                "transitoria": "transitoria", "derogatoria": "derogatoria"}.get(kind, kind)
        out.append(Citation(
            raw=m.group(0), entity_kind=host[1], candidate_id=host[0],
            pinpoint=f"Disposición {kind} {_clean(m.group('dord')).casefold()}",
            char_start=m.start(), char_end=m.end(),
            method="es_disposicion", confidence=.97))

    for m in _HOST_ONLY.finditer(text):
        host = _host_of(m)
        if not host or not free(m.start(), m.end()):
            continue
        if not _bare_name_allowed(m, text):
            continue
        spans.append((m.start(), m.end()))
        out.append(Citation(
            raw=m.group(0), entity_kind=host[1], candidate_id=host[0], pinpoint=None,
            char_start=m.start(), char_end=m.end(),
            method="es_instrument", confidence=.95))
    return out


def bare_pinpoint(cue: str, number: str, kind: str | None,
                  candidate_id: str | None = None) -> str:
    """An UNTETHERED Spanish pincite, rendered for the instrument it was tethered to.

    ``extractor._attach_carry_forward`` finds "el artículo 9" in a document that named
    its instrument several paragraphs earlier — or, for a single-subject guide, in one
    that declared it in ``citation_default_instrument`` and never names it in the body at
    all. Which notation the anchor needs depends on that instrument: the AI Act's
    Articles are stored as ``Article 9``, so an anchor written ``Artículo 9`` would never
    join them, and a Spanish act's provisions are grouped under the dotted form. The
    carry-forward pass therefore defers the rendering until it knows the host, and this
    is what it defers to.
    """
    cue_word = _clean(cue).rstrip(".").casefold()
    value = re.sub(r"\s+", "", number or "")
    if cue_word.startswith("considerando"):
        return f"Recital {value}"
    if cue_word.startswith("anexo"):
        return _annex_pin(value, None, None)
    spanish_act = str(candidate_id or "").startswith("es:") or (
        kind == "act" and not str(candidate_id or "")[:1].isdigit())
    return _pin(value, [], "act" if spanish_act else kind or "regulation")


#: Every method this module produces. Spanish does not collide with another national
#: grammar the way German and Austrian do, so this is not a gate list — it is what a
#: re-extraction scope and the audit screens select on.
SPANISH_METHODS: frozenset[str] = frozenset({
    "es_article", "es_host_article", "es_annex", "es_annex_point", "es_recital",
    "es_disposicion", "es_instrument",
})
