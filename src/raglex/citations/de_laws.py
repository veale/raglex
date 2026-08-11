"""The instruments a German judgment names — by abbreviation, and by full title.

``citations.german.LAW_REFERENCE_RE`` reads a reference that RUNS FROM a § or an Art. TO
a law abbreviation: "§ 3 TKG", "Art. 6 Abs. 1 lit. f DSGVO". That is the shape of most
German statutory citation and it is why the German grammar works at all. It also means
two very common things were invisible:

**1. The instrument named without a provision.** "Das TKG regelt …", "Verstoß gegen die
DSGVO", "nach dem Digitale-Dienste-Gesetz" — the judgment says which law it is about and
the reference carries no §, so nothing matched. In a judgment ABOUT an instrument this is
usually the majority of its mentions.

**2. The instrument named in full.** German drafting introduces a law by its long title
and then uses the abbreviation: "Telekommunikationsgesetz (TKG)", "Verordnung (EU)
2016/679 (Datenschutz-Grundverordnung)". The long title matched nothing, so a judgment
that spelled the name out and never wrote the abbreviation had no edge to it at all.

Both are fixed here, and both are **gated on a closed list**. That is the point of this
module: an abbreviation is only allowed to resolve on a bare mention when it is one of
the instruments below — official, unambiguous, and actually the subject of litigation.
A bare-mention rule applied to the open set of German abbreviations would resolve every
capitalised token in the corpus; applied to this list it resolves "DSGVO", "TKG" and
"UrhDaG" and nothing else. (A reference that DOES carry a § keeps the open behaviour —
``german.law_citations`` mints ``de/gesetz/<abk>`` for any abbreviation, listed or not.)

## EU acts belong here as much as German ones

A German court cites the GDPR as "DSGVO" and the EECC as "EKEK", and both must land on
the CELEX Work the corpus holds, not on a fictitious ``de/gesetz/dsgvo``. Where an
instrument is EU, the candidate is its CELEX, so a German "Art. 15 DSGVO" and an English
"Article 15 GDPR" meet on one node. German-language long titles are listed alongside the
German abbreviations because that is how the OJ publishes them.

## English names are NOT here

"Data Act", "AI Act", "GDPR", "LED", "AVMSD" and their siblings live in ``grammars.py``,
behind the guards that open set needs — a determiner requirement for the ambiguous
acronyms, and a prefix check so "Data Act" cannot be read out of the tail of "Personal
Data Act" (which is 26% of that instrument's citer graph in Nordic DPA decisions). This
module's entries carry no such guard because a German long title needs none; adding the
English names here would route them around the guards that exist.

## What is deliberately NOT here

- **Anything ambiguous across fields.** "VO" (any regulation), "RL" (any directive),
  "GG"-adjacent single-capital forms, and any abbreviation that is also an ordinary word.
- **Länder statutes with per-Land variants** (the LDSGs, the Polizeigesetze): "PolG"
  names sixteen different acts, and the corpus cannot tell which from the abbreviation.
- **Proposals that are not yet instruments** — the CSA Regulation ("Chatkontrolle") has
  no CELEX to resolve to, and mapping it to a draft would mint a confidently wrong edge.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .models import Citation


@dataclass(frozen=True, slots=True)
class Instrument:
    #: the abbreviation German practice cites it by — the primary key
    abbrev: str
    #: ``de/gesetz/<abk>`` for a German statute, a CELEX for an EU act
    candidate_id: str
    #: act | regulation | directive | treaty — the entity_kind the pipeline keys on
    kind: str
    #: the long title(s), as the Bundesgesetzblatt or the OJ publishes them, plus the
    #: settled colloquial names ("ePrivacy-Richtlinie", "AI Act")
    names: tuple[str, ...] = ()
    #: further abbreviations for the same instrument ("DS-GVO", "TDDDG" ← "TTDSG")
    variants: tuple[str, ...] = field(default=())


def _de(abk: str) -> str:
    return "de/gesetz/" + re.sub(r"[^a-z0-9]+", "", _fold(abk))


def _fold(value: str) -> str:
    v = unicodedata.normalize("NFKD", value or "")
    v = "".join(c for c in v if not unicodedata.combining(c))
    return v.replace("ß", "ss").casefold()


# --- EU acts, as a German court names them ------------------------------------
_EU: tuple[Instrument, ...] = (
    Instrument("DSGVO", "32016R0679", "regulation",
               ("Datenschutz-Grundverordnung", "Datenschutzgrundverordnung",
                "Datenschutz-Grundverordnung (EU) 2016/679",
                "Verordnung (EU) 2016/679"),
               ("DS-GVO", "DSGVO", "EU-DSGVO")),
    Instrument("ePrivacy-RL", "32002L0058", "directive",
               ("Datenschutzrichtlinie für elektronische Kommunikation",
                "Richtlinie über den Datenschutz in der elektronischen Kommunikation",
                "ePrivacy-Richtlinie", "e-Privacy-Richtlinie",
                "Datenschutzrichtlinie für die elektronische Kommunikation"),
               ("EK-DSRL", "ePrivacyRL", "EKDSRL")),
    Instrument("EKEK", "32018L1972", "directive",
               ("europäischer Kodex für die elektronische Kommunikation",
                "Europäischer Kodex für die elektronische Kommunikation",
                "Richtlinie (EU) 2018/1972", "Kodex für die elektronische Kommunikation"),
               ("TK-Kodex", "EU-Kodex")),
    Instrument("DSA", "32022R2065", "regulation",
               ("Gesetz über digitale Dienste", "Verordnung über digitale Dienste",
                "Digitale-Dienste-Verordnung"),
               ("DDV",)),
    Instrument("DMA", "32022R1925", "regulation",
               ("Gesetz über digitale Märkte", "Verordnung über digitale Märkte",
                "Digitale-Märkte-Verordnung"),
               ("DMV",)),
    Instrument("KI-VO", "32024R1689", "regulation",
               ("KI-Verordnung", "Verordnung über künstliche Intelligenz",
                "Verordnung (EU) 2024/1689"),
               ("KIVO", "KI-VO")),
    Instrument("NIS-2-RL", "32022L2555", "directive",
               ("NIS-2-Richtlinie", "NIS2-Richtlinie",
                "Richtlinie über Maßnahmen für ein hohes gemeinsames Cybersicherheitsniveau"),
               ("NIS2", "NIS-2", "NIS2RL")),
    Instrument("DGA", "32022R0868", "regulation",
               ("Daten-Governance-Rechtsakt", "Datenverwaltungsverordnung"),
               ("DGRA",)),
    Instrument("DA", "32023R2854", "regulation",
               ("Datenverordnung", "Daten-Verordnung"),
               ("DatenVO",)),
    Instrument("P2B-VO", "32019R1150", "regulation",
               ("Plattform-zu-Unternehmen-Verordnung",
                "Verordnung zur Förderung von Fairness und Transparenz für gewerbliche "
                "Nutzer von Online-Vermittlungsdiensten"),
               ("P2B", "P2BVO")),
    Instrument("ECRL", "32000L0031", "directive",
               ("Richtlinie über den elektronischen Geschäftsverkehr",
                "E-Commerce-Richtlinie", "e-Commerce-Richtlinie"),
               ("ECommerceRL", "E-Commerce-RL")),
    Instrument("DSM-RL", "32019L0790", "directive",
               ("Richtlinie über das Urheberrecht im digitalen Binnenmarkt",
                "DSM-Richtlinie", "Urheberrechtsrichtlinie im digitalen Binnenmarkt"),
               ("DSMRL",)),
    Instrument("InfoSoc-RL", "32001L0029", "directive",
               ("Richtlinie zur Harmonisierung bestimmter Aspekte des Urheberrechts",
                "InfoSoc-Richtlinie", "Urheberrechtsrichtlinie"),
               ("InfoSocRL",)),
    Instrument("AVMD-RL", "32010L0013", "directive",
               ("Richtlinie über audiovisuelle Mediendienste", "AVMD-Richtlinie",
                "Audiovisuelle-Mediendienste-Richtlinie"),
               ("AVMDRL",)),
    Instrument("eIDAS-VO", "32014R0910", "regulation",
               ("eIDAS-Verordnung",
                "Verordnung über elektronische Identifizierung und Vertrauensdienste"),
               ("eIDAS", "eIDASVO")),
    Instrument("JI-RL", "32016L0680", "directive",
               ("Richtlinie (EU) 2016/680", "JI-Richtlinie", "Datenschutzrichtlinie Justiz "
                "und Inneres"),
               ("JIRL", "DSRL-JI")),
    Instrument("VDSRL", "32006L0024", "directive",
               ("Vorratsdatenspeicherungsrichtlinie",
                "Richtlinie über die Vorratsspeicherung von Daten"),
               ("VDS-RL",)),
    Instrument("TSM-VO", "32015R2120", "regulation",
               ("Verordnung über den Zugang zum offenen Internet", "Netzneutralitätsverordnung",
                "Open-Internet-Verordnung"),
               ("TSMVO", "NetzneutralitätsVO")),
    Instrument("Geoblocking-VO", "32018R0302", "regulation",
               ("Geoblocking-Verordnung", "Verordnung über Maßnahmen gegen "
                "ungerechtfertigtes Geoblocking"),
               ("GeoblockingVO",)),
    Instrument("EAA", "32019L0882", "directive",
               ("Barrierefreiheitsrichtlinie",
                "Richtlinie über die Barrierefreiheitsanforderungen für Produkte und "
                "Dienstleistungen"),
               ("BFR-RL",)),
    Instrument("CRA", "32024R2847", "regulation",
               ("Cyberresilienz-Verordnung",),
               ("CyberRVO",)),
    Instrument("EMFA", "32024R1083", "regulation",
               ("Europäisches Medienfreiheitsgesetz", "Verordnung über die Medienfreiheit"),
               ("EMFV",)),
    Instrument("UGP-RL", "32005L0029", "directive",
               ("Richtlinie über unlautere Geschäftspraktiken", "UGP-Richtlinie"),
               ("UGPRL",)),
    Instrument("VRRL", "32011L0083", "directive",
               ("Verbraucherrechterichtlinie", "Richtlinie über die Rechte der Verbraucher"),
               ("VRRL",)),
    Instrument("Klausel-RL", "31993L0013", "directive",
               ("Klauselrichtlinie", "Richtlinie über missbräuchliche Klauseln in "
                "Verbraucherverträgen"),
               ("KlauselRL",)),
    # Primary law + the Convention. Same Works the English and French treaty grammars
    # mint, so "Art. 267 AEUV" and "Article 267 TFEU" are one node.
    Instrument("AEUV", "12016E", "treaty",
               ("Vertrag über die Arbeitsweise der Europäischen Union",), ()),
    Instrument("EUV", "12016M", "treaty",
               ("Vertrag über die Europäische Union",), ()),
    Instrument("GRC", "12012P", "treaty",
               ("Charta der Grundrechte der Europäischen Union", "Grundrechtecharta",
                "EU-Grundrechtecharta"),
               ("GRCh", "GrCh", "EUGrCh", "GrCharta", "EUGRCharta")),
    Instrument("EMRK", "echr/convention", "treaty",
               ("Europäische Menschenrechtskonvention",
                "Konvention zum Schutze der Menschenrechte und Grundfreiheiten"),
               ("MRK",)),
    Instrument("EGV", "12002E", "treaty", ("Vertrag zur Gründung der Europäischen "
                                           "Gemeinschaft",), ("EG",)),
)

# --- German federal statutes ---------------------------------------------------
def _act(abk: str, kind: str, *names: str, variants: tuple[str, ...] = ()) -> Instrument:
    return Instrument(abk, _de(abk), kind, names, variants)


_DE: tuple[Instrument, ...] = (
    # -- the digital acquis, which is what this corpus is for ------------------
    _act("TKG", "act", "Telekommunikationsgesetz"),
    _act("TMG", "act", "Telemediengesetz"),
    _act("DDG", "act", "Digitale-Dienste-Gesetz", "Gesetz über digitale Dienste (DDG)"),
    _act("TDDDG", "act", "Telekommunikation-Digitale-Dienste-Datenschutz-Gesetz",
         variants=("TTDSG",)),
    _act("TTDSG", "act", "Telekommunikation-Telemedien-Datenschutz-Gesetz",
         "Telekommunikation-Telemedien-Datenschutzgesetz"),
    _act("BDSG", "act", "Bundesdatenschutzgesetz"),
    _act("NetzDG", "act", "Netzwerkdurchsetzungsgesetz",
         "Gesetz zur Verbesserung der Rechtsdurchsetzung in sozialen Netzwerken"),
    _act("DDG-DurchfG", "act", "Digitale-Dienste-Durchführungsgesetz"),
    _act("BSIG", "act", "BSI-Gesetz", "Gesetz über das Bundesamt für Sicherheit in der "
         "Informationstechnik"),
    _act("VDG", "act", "Vertrauensdienstegesetz"),
    _act("OZG", "act", "Onlinezugangsgesetz"),
    _act("EGovG", "act", "E-Government-Gesetz"),
    _act("IFG", "act", "Informationsfreiheitsgesetz"),
    _act("UrhG", "act", "Urheberrechtsgesetz", "Gesetz über Urheberrecht und verwandte "
         "Schutzrechte"),
    _act("UrhDaG", "act", "Urheberrechts-Diensteanbieter-Gesetz"),
    _act("KUG", "act", "Kunsturhebergesetz", "Gesetz betreffend das Urheberrecht an "
         "Werken der bildenden Künste und der Photographie", variants=("KunstUrhG",)),
    _act("GeschGehG", "act", "Geschäftsgeheimnisgesetz"),
    _act("UWG", "act", "Gesetz gegen den unlauteren Wettbewerb"),
    _act("GWB", "act", "Gesetz gegen Wettbewerbsbeschränkungen"),
    _act("EnWG", "act", "Energiewirtschaftsgesetz"),
    _act("MStV", "act", "Medienstaatsvertrag"),
    _act("RStV", "act", "Rundfunkstaatsvertrag"),
    _act("JMStV", "act", "Jugendmedienschutz-Staatsvertrag"),
    _act("JuSchG", "act", "Jugendschutzgesetz"),
    _act("PAuswG", "act", "Personalausweisgesetz"),
    _act("SigG", "act", "Signaturgesetz"),
    # -- the codes every judgment cites ---------------------------------------
    _act("GG", "act", "Grundgesetz", "Grundgesetz für die Bundesrepublik Deutschland"),
    _act("BGB", "act", "Bürgerliches Gesetzbuch"),
    _act("HGB", "act", "Handelsgesetzbuch"),
    _act("StGB", "act", "Strafgesetzbuch"),
    _act("StPO", "act", "Strafprozessordnung"),
    _act("ZPO", "act", "Zivilprozessordnung"),
    _act("GVG", "act", "Gerichtsverfassungsgesetz"),
    _act("FamFG", "act", "Gesetz über das Verfahren in Familiensachen"),
    _act("InsO", "act", "Insolvenzordnung"),
    _act("VwGO", "act", "Verwaltungsgerichtsordnung"),
    _act("VwVfG", "act", "Verwaltungsverfahrensgesetz"),
    _act("VwVG", "act", "Verwaltungs-Vollstreckungsgesetz"),
    _act("AO", "act", "Abgabenordnung"),
    _act("EStG", "act", "Einkommensteuergesetz"),
    _act("UStG", "act", "Umsatzsteuergesetz"),
    _act("KStG", "act", "Körperschaftsteuergesetz"),
    _act("FGO", "act", "Finanzgerichtsordnung"),
    _act("SGG", "act", "Sozialgerichtsgesetz"),
    _act("ArbGG", "act", "Arbeitsgerichtsgesetz"),
    _act("BetrVG", "act", "Betriebsverfassungsgesetz"),
    _act("KSchG", "act", "Kündigungsschutzgesetz"),
    _act("AGG", "act", "Allgemeines Gleichbehandlungsgesetz"),
    _act("TzBfG", "act", "Teilzeit- und Befristungsgesetz"),
    _act("BUrlG", "act", "Bundesurlaubsgesetz"),
    _act("AktG", "act", "Aktiengesetz"),
    _act("GmbHG", "act", "GmbH-Gesetz", "Gesetz betreffend die Gesellschaften mit "
         "beschränkter Haftung"),
    _act("MarkenG", "act", "Markengesetz"),
    _act("PatG", "act", "Patentgesetz"),
    _act("DesignG", "act", "Designgesetz"),
    _act("BVerfGG", "act", "Bundesverfassungsgerichtsgesetz"),
    _act("BeamtStG", "act", "Beamtenstatusgesetz"),
    _act("AufenthG", "act", "Aufenthaltsgesetz"),
    _act("AsylG", "act", "Asylgesetz"),
    _act("BauGB", "act", "Baugesetzbuch"),
    _act("StVG", "act", "Straßenverkehrsgesetz"),
    _act("StVO", "act", "Straßenverkehrs-Ordnung"),
    _act("OWiG", "act", "Gesetz über Ordnungswidrigkeiten"),
    _act("RVG", "act", "Rechtsanwaltsvergütungsgesetz"),
    _act("GKG", "act", "Gerichtskostengesetz"),
    _act("BRAO", "act", "Bundesrechtsanwaltsordnung"),
    _act("GewO", "act", "Gewerbeordnung"),
    _act("IfSG", "act", "Infektionsschutzgesetz"),
)

INSTRUMENTS: tuple[Instrument, ...] = _EU + _DE


def _build() -> tuple[dict[str, Instrument], dict[str, Instrument]]:
    """(by folded abbreviation, by folded long title).

    First writer wins, so an earlier entry keeps a spelling a later one also claims —
    which is how ``TTDSG`` stays its own (repealed-and-renamed) act while ``TDDDG``
    lists it as a variant, and how ``EG`` remains the EC Treaty.
    """
    by_abbrev: dict[str, Instrument] = {}
    by_name: dict[str, Instrument] = {}
    for inst in INSTRUMENTS:
        for a in (inst.abbrev, *inst.variants):
            by_abbrev.setdefault(_fold(re.sub(r"[\s.\-]+", "", a)), inst)
        for n in inst.names:
            by_name.setdefault(_fold(re.sub(r"\s+", " ", n)), inst)
    return by_abbrev, by_name


_BY_ABBREV, _BY_NAME = _build()

#: The abbreviations, longest-first so "NIS-2-RL" is not cut short to "NIS". Matched
#: case-SENSITIVELY (an abbreviation is never lower-cased in German legal writing), which
#: is what stops "da" (Data Act) and "ao" from firing on ordinary words.
_ABBREV_ALTERNATION = "|".join(
    re.escape(a) for a in sorted(
        {a for inst in INSTRUMENTS for a in (inst.abbrev, *inst.variants)},
        key=len, reverse=True))
#: A short abbreviation is the length that collides across languages, and this pass runs
#: over the whole corpus, not only over German documents. "DSA" is also a duty-solicitor
#: scheme in an English judgment and "DMA" the Syndicat Data et Marketing France; both
#: were minting confident references to EU regulations from documents in other languages.
#:
#: The fix is the one ``grammars._NEEDS_DETERMINER`` already applies to the English
#: acronyms: require the article. German writes "das BGB", "die DSGVO", "nach dem DSA" —
#: a German determiner in front is both idiomatic German and something English and French
#: text cannot supply, so it gates the pass on the language without guessing at it. Four
#: characters and up ("DSGVO", "NetzDG", "UrhDaG", "TzBfG") are distinctive enough to
#: stand alone.
_NEEDS_DETERMINER_LEN = 4
_DE_DETERMINER = r"(?:der|die|das|des|dem|den|dieser|diese|dieses|eines|einer)"
_SHORT = "|".join(re.escape(a) for a in sorted(
    {a for inst in INSTRUMENTS for a in (inst.abbrev, *inst.variants)
     if len(re.sub(r"[\s.\-]+", "", a)) < _NEEDS_DETERMINER_LEN}, key=len, reverse=True))
_LONG = "|".join(re.escape(a) for a in sorted(
    {a for inst in INSTRUMENTS for a in (inst.abbrev, *inst.variants)
     if len(re.sub(r"[\s.\-]+", "", a)) >= _NEEDS_DETERMINER_LEN}, key=len, reverse=True))
#: A bare instrument mention: the abbreviation, behind its German article where the
#: abbreviation is short enough to be ambiguous. Bounded by a non-word character on both
#: sides so "DSGVO-Verstoß" still matches but "XDSGVO" does not. It must not be
#: immediately preceded by a § or Art. — that is ``german.LAW_REFERENCE_RE``'s span, and
#: re-matching it here would produce two overlapping citations of one instrument.
BARE_INSTRUMENT_RE = re.compile(
    rf"(?<![\w§])(?:"
    rf"(?:{_DE_DETERMINER}\s+)?(?P<abbrev>{_LONG})"
    rf"|(?i:{_DE_DETERMINER})\s+(?P<short>{_SHORT})"
    rf")(?![\wÄÖÜäöüß])")

#: The long titles, longest-first so "Richtlinie über das Urheberrecht im digitalen
#: Binnenmarkt" wins over a shorter title it contains. Case-insensitive: a long title is
#: a German noun phrase and appears mid-sentence in either case.
_NAME_ALTERNATION = "|".join(
    re.escape(n) for n in sorted({n for inst in INSTRUMENTS for n in inst.names},
                                 key=len, reverse=True))
NAMED_INSTRUMENT_RE = re.compile(
    rf"(?<![\wÄÖÜäöüß])(?:(?:der|die|das|des|dem|den)\s+)?(?P<name>{_NAME_ALTERNATION})"
    rf"(?![\wÄÖÜäöüß])", re.IGNORECASE)


#: The German law-report series, which put the STATUTE the entry is filed under straight
#: after the series name: "BGHR StPO Abs. 3 Verfahrenshindernis 2", "BGHSt 42, 107",
#: "NStZ-RR 2007, 173". That "StPO" is a filing key inside a citation of a report, not
#: the judgment reaching for the statute, and reading it as a bare mention would attach a
#: statutory edge to every headnote reference in the corpus. Same list as the one
#: ``german._LAW_STOPWORDS`` keeps for the other end of the same problem.
_REPORT_SERIES = frozenset({
    "bghr", "bghz", "bghst", "bverfge", "bverwge", "bage", "bfhe", "bsge", "sozr",
    "njw", "njw-rr", "nza", "nza-rr", "nvwz", "nvwz-rr", "njoz", "nzs", "nzm", "nzi",
    "nstz", "nstz-rr", "grur", "grur-rr", "mdr", "wm", "zip", "dstr", "euzw", "eugrz",
    "versr", "dvbl", "jurisdb", "beckrs", "juris", "openjur",
})
_PRECEDING_TOKEN = re.compile(r"([\w.\-]+)\s+$")


def _in_report_citation(text: str, start: int) -> bool:
    m = _PRECEDING_TOKEN.search(text[max(0, start - 24):start])
    return bool(m) and _fold(m.group(1)).strip(".") in _REPORT_SERIES


def resolve(token: str | None) -> Instrument | None:
    """The instrument an abbreviation or a long title names, or None."""
    if not token:
        return None
    flat = _fold(re.sub(r"[\s.\-]+", "", token))
    hit = _BY_ABBREV.get(flat)
    if hit is not None:
        return hit
    return _BY_NAME.get(_fold(re.sub(r"\s+", " ", token.strip())))


def instrument_citations(text: str, *, occupied: list[tuple[int, int]] | None = None
                         ) -> list[Citation]:
    """Every listed instrument this text NAMES — long title first, then bare abbreviation.

    ``occupied`` are spans an existing citation already covers (the §-anchored references
    ``german.law_citations`` found); a mention inside one of those is the same reference
    seen twice and is skipped, so the pinpointed citation keeps the span.
    """
    spans: list[tuple[int, int]] = list(occupied or [])
    found: list[Citation] = []

    def _free(start: int, end: int) -> bool:
        return not any(s < end and start < e for s, e in spans)

    for match in NAMED_INSTRUMENT_RE.finditer(text):
        inst = resolve(match.group("name"))
        if inst is None or not _free(match.start(), match.end()):
            continue
        spans.append((match.start(), match.end()))
        found.append(Citation(
            raw=match.group(0), entity_kind=inst.kind, candidate_id=inst.candidate_id,
            pinpoint=None, char_start=match.start(), char_end=match.end(),
            method="de_instrument_name", confidence=0.9,
        ))
    for match in BARE_INSTRUMENT_RE.finditer(text):
        abbrev = match.group("abbrev") or match.group("short")
        inst = resolve(abbrev)
        if inst is None or not _free(match.start(), match.end()):
            continue
        if _in_report_citation(text, match.start("abbrev") if match.group("abbrev")
                               else match.start("short")):
            continue
        spans.append((match.start(), match.end()))
        found.append(Citation(
            raw=match.group(0), entity_kind=inst.kind, candidate_id=inst.candidate_id,
            pinpoint=None, char_start=match.start(), char_end=match.end(),
            method="de_instrument_abbrev", confidence=0.85,
        ))
    return found
