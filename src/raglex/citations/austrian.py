"""Austrian citation grammar — statutes, dockets, Rechtssätze and the RIS ``Normen`` field.

Austria writes law in German and cites it *almost* the way Germany does, which is why
this module exists rather than a few extra entries in ``citations.german``. Four things
are genuinely different, and each of them breaks the German grammar:

**1. The numbered point is a Ziffer, not a Nummer.** Austria writes "§ 611 Abs 2 Z 1
ZPO"; Germany writes "§ 611 Abs. 2 Nr. 1 ZPO". The German pattern must run unbroken from
the § to the law abbreviation, so an unrecognised rung in the middle does not merely lose
the pinpoint — it ends the match early and reads the rung as the law. A bare ``Z`` there
mints ``de/gesetz/z``.

**2. The full stops are absent.** "Abs 2", "iVm", "idF", "Rz 14" — Austrian legal
writing drops the abbreviating point that German keeps ("Abs.", "i.V.m.", "Rn.").

**3. The abbreviation may carry a year, and it is load-bearing.** TKG 2003 and TKG 2021
are different acts; so are AsylG 2005 and its predecessor, GewO 1994, EStG 1988, WEG
2002, VAG 2016, WAG 2018. See ``at_laws``.

**4. The same abbreviation names a different statute.** KSchG is consumer protection in
Vienna and dismissal protection in Berlin; MSchG is trade marks in one and maternity
leave in the other; ABGB has no German counterpart at all. This is the reason the
candidate namespace is ``at/gesetz/…`` and the reason ``citations.stage`` gates the two
grammars against each other per document — see ``_gate_austrian_statutes``.

## Dockets identify their own court

An Austrian case number announces which court decided it, which no German docket does:

* ``6 Ob 127/20z`` — OGH. Senate number, register letters, running number/year, and a
  check letter computed from the rest (so "6 Ob 127/20z" and "6 Ob 127/20b" cannot both
  exist). The registers are the subject-matter: ``Ob`` civil, ``ObA`` labour, ``ObS``
  social, ``Os`` criminal, ``Ok`` cartel, ``OCg`` arbitration annulment, ``Ds``
  disciplinary, ``Ns``/``Nc`` ancillary.
* ``Ra 2020/04/0123`` / ``Ro 2019/13/0012`` — VwGH (revision / revision on a point of
  law admitted by the court below).
* ``G 123/2020``, ``V 45/2021``, ``E 2493/2023``, ``B 1234/09`` — VfGH, by proceeding
  type: ``G`` statute review, ``V`` regulation review, ``E``/``B`` individual complaint.
  These letters are a single character and match far too much on their own, so they are
  only read inside a VfGH window.
* ``W123 2284751-3`` — BVwG (chamber + file number + sequence).
* ``LVwG-2023/27/0673-5`` — a Landesverwaltungsgericht.

## Rechtssatznummern are citations

The OGH's documentation office abstracts each proposition into a **Rechtssatz** with a
permanent number, and Austrian judgments cite the proposition rather than the case:
"RIS-Justiz RS0042418". Those are first-class documents in RIS (with their own ECLI), so
``RS0042418`` resolves to one, via the ``at/rs/RS0042418`` alias the adapter registers.
``RW…``/``RL…`` are the same thing for the Oberlandesgerichte.

## VfSlg and VwSlg resolve; the commercial series do not

The constitutional and administrative courts number their own official collections, and
RIS publishes that number as a field — so "VfSlg 19.632/2012" can be linked to the
decision through the ``at/vfslg/19632`` alias. The commercial series (SZ, JBl, EvBl,
ecolex, RdW, wbl…) have no such key in any open source, so they are recognised only in
order to be *excluded*: a §-reference that trails into one would otherwise read the
series name as the law, and "SZ 62/113" has the same shape as a docket.
"""

from __future__ import annotations

import re
import unicodedata

from . import at_laws
from .models import Citation


def law_id(abbreviation: str) -> str:
    return at_laws.law_id(abbreviation)


def _fold(value: str) -> str:
    v = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in v if not unicodedata.combining(c)).replace("ß", "ss").casefold()


# ── statutory references ─────────────────────────────────────────────────────
_PARA = r"\d{1,5}[a-z]?"
#: Austria's sub-provision rungs, with the full stop optional throughout. ``Z`` (Ziffer)
#: is the one that matters most: it is the level German writes as ``Nr.``, it appears in
#: nearly every administrative citation, and the German pattern cannot cross it.
_SUB = (r"(?:Abs(?:atz)?\.?|Z(?:iff|iffer)?\.?|lit(?:\.|era)?|Satz|S\.?|"
        r"Halbs(?:atz)?\.?|HS\.?|Fall|Nr\.?|Anh(?:ang)?\.?|Anl(?:age)?\.?)"
        r"\s*(?:\d+[a-z]?|[a-z]\b|[IVX]+)")
_TAIL = (rf"(?:\s*(?:{_SUB}|[,;]|und\b|oder\b|bis\s+{_PARA}|[-–—]\s*{_PARA}|"
         rf"ff?\.?(?=\s|$)))*")
_ONE = rf"(?:§§?|Art(?:ikel|\.)?)\s*{_PARA}{_TAIL}"
#: "iVm" — in Verbindung mit — joins two provisions of (usually) the same act. Austria
#: writes it closed up and without stops far more often than Germany does.
_IVM = rf"(?:\s+i\.?\s?V\.?\s?m\.?\s+{_ONE})?"
#: Matched case-SENSITIVELY inside an otherwise case-insensitive pattern, for the reason
#: ``german._LAW`` gives: the rest of a reference is written both ways but an
#: abbreviation never is, and case-insensitivity here reads the next ordinary word as a
#: law. The optional trailing year is Austria's versioned short title (TKG 2021).
#: Two joined parts, not one: Austria hyphenates and slashes its short titles more
#: freely than Germany does — B-VG, KoPl-G, UVP-G, BFA-VG, GBK/GAW-Gesetz, AMD-G. A
#: single continuation stopped at the second separator and minted ``at/gesetz/gbkgaw``,
#: a law that does not exist, for every citation of the equal-treatment procedure act.
_LAW = (r"(?-i:[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9]*"
        r"(?:[-/][A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9]*){0,2}"
        r"(?:\s+(?:19|20)\d{2})?)")
LAW_REFERENCE_RE = re.compile(
    rf"(?P<raw>(?:{_ONE}{_IVM})\s+(?P<law>{_LAW}))", re.IGNORECASE)

#: The RIS ``Normen`` field puts the law FIRST and the provision after it, with no
#: spaces inside the rungs: "ZPO §611 Abs2 Z1", "DSGVO Art4 Z11", "GewO 1994 §87 Abs1".
#: It is the structured, editorially-assigned norm index — the single most reliable
#: statutory signal Austria publishes — so it is parsed on its own terms rather than
#: being pushed through the running-text pattern, which expects the opposite order.
#: ``AEUV Lissabon Art267`` — RIS qualifies a Treaty by the amending treaty that
#: produced the numbering, and does the same for a few consolidated acts. The qualifier
#: sits between the abbreviation and the Article, so a pattern that expects the provision
#: to follow the law immediately rejected every Treaty reference in the corpus.
#: The nine Länder, as RIS abbreviates them in a norm reference. They are not
#: decoration: every Land has a Bauordnung, a Raumplanungsgesetz and a
#: Landes-Datenschutzgesetz, so "RPG Vlbg" and "Oö BauO 1994" are nine different acts
#: sharing one abbreviation. The Land is folded into the id, which is the same rule
#: ``de_laws`` states for the German Länder statutes it declines to list at all.
_LAND = (r"Wr|Wien|NÖ|Nö|OÖ|Oö|Sbg|Szbg|Stmk|Ktn|Krnt|Tir|Vlbg|Bgld|"
         r"Niederösterreich|Oberösterreich|Salzburg|Steiermark|Kärnten|Tirol|"
         r"Vorarlberg|Burgenland")
NORM_FIELD_RE = re.compile(
    rf"^\s*(?:(?P<land_pre>{_LAND})\s+)?"
    r"(?P<law>[A-ZÄÖÜ][\wÄÖÜäöüß.\-/]*(?:\s+(?:19|20)\d{2})?)"
    rf"(?:\s+(?P<land_post>{_LAND}))?"
    r"(?P<qualifier>(?:\s+(?:Lissabon|Nizza|Amsterdam|Maastricht|idF|Anh|Anlage))*)"
    r"(?:\s+(?P<rest>(?:§§?|Art|ErwGr)\s*\S.*))?\s*$")
#: …and the OTHER order, which the Gleichbehandlungskommission, the disciplinary
#: authorities and the older tribunals use: "§13 Abs1 Z5 B-GlBG", "§126 Abs2 BDG". Same
#: field, same register, opposite word order — reading only one of them lost every
#: structured norm reference those bodies publish.
NORM_FIELD_TRAILING_RE = re.compile(
    rf"^\s*(?P<rest>(?:§§?|Art|ErwGr)\s*[\d\s\w.,;/§-]*?)\s+"
    rf"(?:(?P<land_pre>{_LAND})\s+)?"
    r"(?P<law>[A-ZÄÖÜ][\wÄÖÜäöüß.\-/]*(?:\s+(?:19|20)\d{2})?)\s*$")
#: One field, several norms: "AsylG 1997 §7 AsylG 1997 §12". Split before a law
#: abbreviation that follows a completed provision rather than treating the tail as part
#: of the first pinpoint. The optional year is what makes the versioned short titles
#: split — without it "AsylG 1997 §7 AsylG 1997 §12" stayed one reference whose pinpoint
#: was the whole string.
_NORM_SPLIT_RE = re.compile(
    r"(?<=\d)\s+(?=[A-ZÄÖÜ][\wÄÖÜäöüß.\-/]*(?:\s+(?:19|20)\d{2})?\s+(?:§|Art))")
#: RIS appends its own index term after a slash ("B-VG Art7 Abs1 / Gerichtsakt") and
#: sometimes leaves a dangling conjunction ("WVRG 2007 §31 in Verbindung mit").
_NORM_TAIL_RE = re.compile(
    r"(?:\s*/\s*.*$)|(?:\s+(?:in\s+Verbindung\s+mit|iVm)\s*$)", re.IGNORECASE)


def _canonical_pinpoint(body: str) -> str:
    """Normalise a provision string to the spacing Austrian practice prints.

    "§611 Abs2 Z1" and "§ 611 Abs 2 Z 1" are the same pinpoint written by a database and
    by a judge; storing both spellings would split one provision's citers in two.
    """
    text = re.sub(r"\s+", " ", body or "").strip()
    # Read the rung chain rather than rewriting the string in place. Two reasons:
    #
    # No ``\b`` works in front of "§" — it is not a word character, so a word boundary
    # before it only exists when the PREVIOUS character is one, which in "ZPO §611" it is
    # not. A substitution therefore left RIS's "§611" unspaced while a judge's "§ 611"
    # was normalised: one provision, two anchors, two halves of its citer list.
    #
    # And RIS appends its own register index after the provision — "ABGB §833 A", "ZPO
    # §500 Abs2 IIA1", "AußStrG §2 Abs2 Z5 F2". Those letters are the norm register's
    # internal classification, not part of any citation a court writes, so an anchor that
    # keeps them can never match "§ 833 ABGB" as a judgment prints it. Stopping at the
    # first token that is not a rung drops them and nothing else.
    tokens = text.split(" ")
    out: list[str] = []
    i = 0
    head = re.match(r"(?i)^(§§?|Art(?:ikel)?\.?)\s*(\d.*)?$", tokens[0] if tokens else "")
    if not head:
        return ""
    out.append(_rung(head.group(1)).strip())
    if head.group(2):
        out.append(head.group(2))
        i = 1
    else:
        i = 1
        if i < len(tokens) and re.match(r"^\d", tokens[i]):
            out.append(tokens[i])
            i += 1
    while i < len(tokens):
        rung = re.match(rf"(?i)^({_RUNG_ALT})\s*(.*)$", tokens[i])
        if not rung:
            break
        value = rung.group(2)
        i += 1
        if not value and i < len(tokens):
            value = tokens[i]
            i += 1
        if not value:
            break
        out.append(_rung(rung.group(1)).strip())
        out.append(value)
    return " ".join(out).strip()


_RUNGS = {"§": "§ ", "§§": "§§ ", "art": "Art ", "artikel": "Art ", "art.": "Art ",
          "abs": "Abs ", "absatz": "Abs ", "abs.": "Abs ", "z": "Z ", "ziffer": "Z ",
          "z.": "Z ", "lit": "lit ", "lit.": "lit ", "litera": "lit ", "satz": "Satz ",
          "halbs": "Halbs ", "halbsatz": "Halbs ", "halbs.": "Halbs ", "hs": "HS ",
          "hs.": "HS ", "fall": "Fall ", "nr": "Nr ", "nr.": "Nr ",
          "sublit": "sublit ", "erwgr": "ErwGr "}
#: The rung keywords as an alternation, longest-first so "absatz" is not cut to "abs" and
#: "sublit" not to "lit".
_RUNG_ALT = "|".join(re.escape(k) for k in sorted(_RUNGS, key=len, reverse=True)
                     if k not in ("§", "§§"))


def _rung(token: str) -> str:
    return _RUNGS.get(token.casefold(), token + " ")


def eu_pinpoint(pinpoint: str) -> str:
    """An Austrian EU-law pinpoint in the Formex anchor vocabulary the corpus stores.

    "Art 6 Abs 1 lit f DSGVO" is the most-cited provision in European data-protection
    law; it has to arrive as ``Article 6(1)(f)`` or it will not meet the same provision
    cited in English. Austria's ``Z`` is the Union's numbered point, so "Art 4 Z 11
    DSGVO" is ``Article 4(11)`` — the German translator does not know that rung and
    dropped it.
    """
    # RIS indexes recitals as norms in their own right ("DSGVO ErwGr101"), and the corpus
    # holds them as ``Recital N`` segments of the base act — so the Erwägungsgrund is a
    # resolvable anchor, not a pinpoint to drop.
    if recital := re.search(r"(?i)^ErwGr\.?\s*(\d{1,3})", pinpoint or ""):
        return f"Recital {recital.group(1)}"
    article = re.search(r"(?i)^Art(?:ikel)?\.?\s*(\d{1,3}[a-z]?)", pinpoint or "")
    if not article:
        return pinpoint
    out = f"Article {article.group(1)}"
    if sub := re.search(r"(?i)\bAbs(?:atz)?\.?\s*(\d+[a-z]?)", pinpoint):
        out += f"({sub.group(1)})"
    elif point := re.search(r"(?i)\bZ(?:iffer)?\.?\s*(\d+[a-z]?)", pinpoint):
        out += f"({point.group(1)})"
    if letter := re.search(r"(?i)\blit\.?\s*([a-z])\b", pinpoint):
        out += f"({letter.group(1).casefold()})"
    return out


def _emit(law: str, pinpoint: str, raw: str, start: int, end: int,
          method: str) -> Citation | None:
    if not at_laws.is_law_abbreviation(law):
        return None
    celex = at_laws.eu_id(law)
    if celex:
        kind = "treaty" if celex in at_laws.EU_TREATY_IDS else "regulation"
        return Citation(raw=raw, entity_kind=kind, candidate_id=celex,
                        pinpoint=eu_pinpoint(pinpoint) or None,
                        char_start=start, char_end=end,
                        method=f"{method}_eu", confidence=1.0)
    return Citation(raw=raw, entity_kind="act", candidate_id=law_id(law),
                    pinpoint=_canonical_pinpoint(pinpoint) or None,
                    char_start=start, char_end=end, method=method, confidence=1.0)


def law_citations(text: str) -> list[Citation]:
    """"§ 1295 Abs 1 ABGB", "Art 6 Abs 1 lit f DSGVO" → one citation each."""
    found: list[Citation] = []
    for match in LAW_REFERENCE_RE.finditer(text or ""):
        law = match.group("law")
        raw = match.group("raw")
        provision = raw[:raw.rfind(law)] if law in raw else raw
        cite = _emit(law, provision, raw, match.start(), match.end(), "at_law_reference")
        if cite is not None:
            found.append(cite)
    return found


def norm_citations(items) -> list[tuple[str, str | None, str]]:
    """RIS ``Normen`` entries → ``(candidate_id, anchor, raw)``.

    The adapter turns these into structured ``INTERPRETS`` edges. Returned rather than
    emitted as ``Citation``s because they carry no offsets: the field is metadata about
    the decision, not a span inside its text.
    """
    out: list[tuple[str, str | None, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items or ():
        whole = " ".join(str(item or "").split())
        if not whole:
            continue
        for raw in _NORM_SPLIT_RE.split(_NORM_TAIL_RE.sub("", whole)) or ():
            raw = raw.strip()
            if not raw:
                continue
            m = NORM_FIELD_RE.match(raw) or NORM_FIELD_TRAILING_RE.match(raw)
            if not m:
                continue
            law = m.group("law").strip()
            rest = (m.groupdict().get("rest") or "").strip()
            if not at_laws.is_law_abbreviation(law):
                continue
            celex = at_laws.eu_id(law)
            land = (m.groupdict().get("land_pre") or m.groupdict().get("land_post") or "")
            target = celex or law_id(f"{land} {law}" if land and not celex else law)
            anchor = (eu_pinpoint(rest) if celex else _canonical_pinpoint(rest)) or None
            key = (target, anchor or "")
            if key in seen:
                continue
            seen.add(key)
            out.append((target, anchor, raw))
    return out


# ── decisions ────────────────────────────────────────────────────────────────
#: OGH registers, longest-first so "ObA"/"ObS"/"OCg" are not cut short to "Ob"/"O".
_OGH_REGISTER = r"ObA|ObS|OCg|Ok|Ob|Os|Ns|Nc|Bs|Ds|Fsc|Fs|Nd|Präs"
#: "6 Ob 127/20z", "18OCg3/25b", "1 Ob 346/54" (pre-check-letter), "10 ObS 45/21b".
#: RIS writes the compact form in its own metadata and judges write the spaced one; both
#: fold to the same key.
OGH_DOCKET_RE = re.compile(
    rf"(?<![\w/])(?P<senate>\d{{1,2}})\s?(?P<register>{_OGH_REGISTER})\s?"
    rf"(?P<number>\d{{1,4}})/(?P<year>\d{{2}})(?P<check>[a-z])?(?![\w/])")
#: VwGH revisions. Distinctive enough to stand alone — nothing else is written "Ra
#: YYYY/NN/NNNN".
VWGH_DOCKET_RE = re.compile(
    r"(?<![\w/])(?P<kind>Ra|Ro)\s?(?P<year>(?:19|20)\d{2})/(?P<field>\d{2})/"
    r"(?P<number>\d{4})(?:-\d{1,2})?(?![\w/])")
#: The pre-2014 VwGH form has no letter prefix, so it is only read next to the court's
#: own name or the "Zl" (Zahl) marker that always introduces it.
VWGH_OLD_RE = re.compile(
    r"(?<![\w/])(?:VwGH\s+(?:vom\s+\d{1,2}\.\d{1,2}\.\d{4},?\s*)?|Zl\.?\s*)"
    r"(?P<year>(?:19|20)\d{2})/(?P<field>\d{2})/(?P<number>\d{4})(?![\w/])")
#: VfGH proceeding types. Single letters, so a court window is mandatory.
_VFGH_TYPE = r"G|V|B|E|A|W|F|KR|WI|SV|UV|KG|K|SU|WII"
VFGH_DOCKET_RE = re.compile(
    rf"(?<![\w/])(?P<type>{_VFGH_TYPE})\s?(?P<number>\d{{1,4}})/(?P<year>\d{{2,4}})"
    rf"(?![\w/])")
#: BVwG: chamber letter + three digits, then the file number and its sequence.
BVWG_DOCKET_RE = re.compile(
    r"(?<![\w/])(?P<chamber>[A-Z]\d{3})\s+(?P<file>\d{6,8})-(?P<seq>\d{1,2})(?![\w/])")
#: The Landesverwaltungsgerichte number their own way per Land; the "LVwG" head is the
#: only stable part, so the rest is taken as an opaque, bounded tail.
LVWG_DOCKET_RE = re.compile(
    r"(?<![\w/])(?P<docket>LVwG[-\s][A-Za-zÄÖÜäöü0-9]{1,12}(?:[-/.][A-Za-z0-9]{1,8}){0,5})"
    r"(?![\w])")
#: A Rechtssatz number. ``RS`` is the OGH's, ``RW``/``RL`` the Oberlandesgerichte's.
RECHTSSATZ_RE = re.compile(r"(?<![\w])(?:RIS[-\s]?Justiz\s+)?(?P<rs>R[SWL]\d{7})(?![\w])")
#: "VfSlg 19.632/2012" / "VwSlg 17.123 A/2007" — the two official collections, whose
#: numbers RIS publishes as a field, so they resolve. The thousands separator is a dot.
SLG_RE = re.compile(
    r"(?<![\w])(?P<series>VfSlg|VwSlg)\.?\s*(?P<number>\d{1,2}\.?\d{3})"
    r"(?:\s*[AF])?(?:/(?P<year>(?:19|20)\d{2}))?(?![\w])")

#: How close a VfGH mention has to be for a bare "G 123/2020" to be read as its docket.
_VFGH_WINDOW = 120
_VFGH_NAME = re.compile(r"(?i)\b(?:VfGH|Verfassungsgerichtshof|VfSlg)\b")


def case_id(court: str, docket: str) -> str:
    """The key an Austrian decision is cited by when the citing text has no ECLI.

    Mirrors ``german.case_alias``: court code + normalised docket, so "6 Ob 127/20z" and
    RIS's own "6Ob127/20z" reach the same node.
    """
    flat = re.sub(r"\s+", "", (docket or "").strip())
    return f"at:case:{(court or '').upper()}:{flat}"


def rechtssatz_id(number: str) -> str:
    return f"at/rs/{(number or '').upper()}"


def collection_id(series: str, number: str) -> str:
    """"VfSlg 19.632/2012" → ``at/vfslg/19632``. The dot is a thousands separator."""
    return f"at/{(series or '').casefold()}/{re.sub(r'[^0-9]', '', number or '')}"


def case_citations(text: str) -> list[Citation]:
    """Every Austrian decision this text names, keyed so a harvested one resolves."""
    body = text or ""
    found: list[Citation] = []

    def add(court: str, docket: str, match: re.Match[str], *, method: str) -> None:
        found.append(Citation(
            raw=match.group(0).strip(), entity_kind="case",
            candidate_id=case_id(court, docket), pinpoint=None,
            char_start=match.start(), char_end=match.end(),
            method=method, confidence=0.95))

    for m in OGH_DOCKET_RE.finditer(body):
        docket = (f"{m.group('senate')} {m.group('register')} "
                  f"{m.group('number')}/{m.group('year')}{m.group('check') or ''}")
        add("OGH", docket, m, method="at_ogh_docket")
    for m in VWGH_DOCKET_RE.finditer(body):
        add("VWGH", f"{m.group('kind')} {m.group('year')}/{m.group('field')}/"
                    f"{m.group('number')}", m, method="at_vwgh_docket")
    for m in VWGH_OLD_RE.finditer(body):
        add("VWGH", f"{m.group('year')}/{m.group('field')}/{m.group('number')}", m,
            method="at_vwgh_docket")
    for m in BVWG_DOCKET_RE.finditer(body):
        add("BVWG", f"{m.group('chamber')} {m.group('file')}-{m.group('seq')}", m,
            method="at_bvwg_docket")
    for m in LVWG_DOCKET_RE.finditer(body):
        add("LVWG", m.group("docket"), m, method="at_lvwg_docket")
    for m in VFGH_DOCKET_RE.finditer(body):
        window = body[max(0, m.start() - _VFGH_WINDOW):m.start()]
        if not _VFGH_NAME.search(window):
            continue
        add("VFGH", f"{m.group('type')} {m.group('number')}/{m.group('year')}", m,
            method="at_vfgh_docket")
    for m in RECHTSSATZ_RE.finditer(body):
        found.append(Citation(
            raw=m.group(0).strip(), entity_kind="case",
            candidate_id=rechtssatz_id(m.group("rs")), pinpoint=None,
            char_start=m.start(), char_end=m.end(),
            method="at_rechtssatz", confidence=1.0))
    for m in SLG_RE.finditer(body):
        found.append(Citation(
            raw=m.group(0).strip(), entity_kind="case",
            candidate_id=collection_id(m.group("series"), m.group("number")),
            pinpoint=None, char_start=m.start(), char_end=m.end(),
            method="at_collection", confidence=0.9))
    return found


#: Methods this module mints. ``citations.stage`` uses the set to keep Austrian readings
#: out of non-Austrian documents (and German readings out of Austrian ones).
AUSTRIAN_METHODS: frozenset[str] = frozenset({
    "at_law_reference", "at_law_reference_eu", "at_instrument_name",
    "at_instrument_abbrev", "at_ogh_docket", "at_vwgh_docket", "at_vfgh_docket",
    "at_bvwg_docket", "at_lvwg_docket", "at_rechtssatz", "at_collection",
})
#: The subset that names an AUSTRIAN authority — as opposed to an EU one, which is
#: correct in any document and must survive the gate.
AUSTRIAN_DOMESTIC_METHODS: frozenset[str] = AUSTRIAN_METHODS - {"at_law_reference_eu"}


def austrian_citations(text: str) -> list[Citation]:
    """Every Austrian citation in ``text``: §-anchored references, decisions, and the
    instruments the decision merely names.

    The named-instrument pass runs last and is handed the spans the first two claimed, so
    an instrument cited with a provision ("Art 6 DSGVO") is not also reported as a bare
    mention of itself — the same discipline ``german.german_citations`` follows.
    """
    laws = law_citations(text)
    cases = case_citations(text)
    occupied = [(c.char_start, c.char_end) for c in laws + cases]
    return laws + cases + at_laws.instrument_citations(text, occupied=occupied)
