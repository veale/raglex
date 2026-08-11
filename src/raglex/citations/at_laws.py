"""The instruments an Austrian decision names — and why they cannot be German ones.

Austria and Germany write law in the same language and cite it in almost the same
notation, so the German grammar (``citations.german``) reads an Austrian judgment
fluently and gets it **wrong**: it mints ``de/gesetz/<abk>`` for every §-reference it
finds. That is not a near-miss. The abbreviations overlap while the statutes behind them
do not:

===========  ============================  ===================================
Abbreviation Austria                       Germany
===========  ============================  ===================================
``ZPO``      Zivilprozessordnung 1895      Zivilprozessordnung 1877
``KSchG``    Konsumentenschutzgesetz       Kündigungsschutzgesetz
``UrhG``     Urheberrechtsgesetz 1936      Urheberrechtsgesetz 1965
``GewO``     Gewerbeordnung 1994           Gewerbeordnung 1869
``StGB``     Strafgesetzbuch 1974          Strafgesetzbuch 1871
``AktG``     Aktiengesetz 1965 (AT)        Aktiengesetz 1965 (DE)
``MSchG``    Markenschutzgesetz            Mutterschutzgesetz
===========  ============================  ===================================

``KSchG`` and ``MSchG`` are the clearest: read as German, an Austrian consumer-protection
citation lands on German dismissal-protection law, and an Austrian trade-mark citation on
German maternity leave. And Austria's civil code is the **ABGB**, which has no German
counterpart at all, so half the citations in an Austrian judgment would have minted
phantom German laws that will never be harvested.

So Austrian statutory references get their own namespace, ``at/gesetz/<abk>``, and the
guard in ``citations.stage`` refuses German candidates inside an Austrian document (and
Austrian candidates outside one). See ``_gate_austrian_statutes`` there — the grammar
alone cannot decide this, because the *text* of the two citation styles is identical.

## The year is part of the abbreviation

Austrian drafting versions a re-enacted statute in its own short title — TKG 2021, AsylG
2005, GewO 1994, EStG 1988, UStG 1994, WEG 2002, VAG 2016, WAG 2018 — and the year is
load-bearing: TKG 2003 and TKG 2021 are different acts with different section numbers,
and the 2021 one is the European Electronic Communications Code transposition. The year
is therefore folded into the id (``at/gesetz/tkg2021``), and a bare "TKG" is left as its
own key rather than silently attributed to whichever version is current.

## EU acts are cited the same way and must NOT become Austrian statutes

An Austrian judgment writes "Art 6 Abs 1 lit f DSGVO" exactly as a German one does, and it
means Regulation (EU) 2016/679. The EU half of the vocabulary is therefore shared with
``de_laws`` — one CELEX per instrument, so an Austrian "Art 15 DSGVO", a German "Art. 15
DS-GVO" and an English "Article 15 GDPR" meet on one node — and extended here with the
forms Austrian practice uses that German practice does not (``GRC`` for the Charter rather
than ``GRCh``, ``KI-VO`` beside the bare English ``AI Act``, ``NIS-2-RL``).

## Austria's own digital acquis

The transposing acts are listed because they are what Austrian litigation is actually
about: DSG (GDPR), TKG 2021 (EECC), ECG (e-Commerce), KoPl-G (the platform act the DSA
displaced), NISG 2024 (NIS 2), AMD-G and ORF-G (AVMSD), SVG (eIDAS), FAGG and VKrG
(consumer directives). Each is an ``at/gesetz/…`` Work; the EU instrument it transposes is
recorded as ``transposes`` so the two are linked without pretending the citation named
both.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from . import de_laws, german
from .models import Citation


@dataclass(frozen=True, slots=True)
class Instrument:
    #: the abbreviation Austrian practice cites it by — the primary key
    abbrev: str
    #: ``at/gesetz/<abk>`` for an Austrian statute, a CELEX for an EU act
    candidate_id: str
    #: act | regulation | directive | treaty
    kind: str
    #: the long title(s) as the Bundesgesetzblatt or the OJ publishes them
    names: tuple[str, ...] = ()
    #: further abbreviations for the same instrument
    variants: tuple[str, ...] = field(default=())
    #: the CELEX this Austrian act transposes, where it transposes exactly one
    transposes: str | None = None


def _fold(value: str) -> str:
    v = unicodedata.normalize("NFKD", value or "")
    v = "".join(c for c in v if not unicodedata.combining(c))
    return v.replace("ß", "ss").casefold()


def law_id(abbreviation: str) -> str:
    """"TKG 2021" → ``at/gesetz/tkg2021``. The Work id an Austrian statute is held under."""
    return "at/gesetz/" + re.sub(r"[^a-z0-9]+", "", _fold(abbreviation))


def _act(abk: str, *names: str, variants: tuple[str, ...] = (),
         transposes: str | None = None) -> Instrument:
    return Instrument(abk, law_id(abk), "act", names, variants, transposes)


# --- EU acts, as an Austrian court names them ---------------------------------
# Shared with the German table wherever the abbreviation is identical, so the two
# jurisdictions' citations land on one CELEX. Listed explicitly rather than imported
# wholesale: an Austrian court does not write "EKEK" (it writes TKG 2021) and does not
# write "EGV", and a vocabulary that claims forms the jurisdiction never uses is a
# vocabulary that will one day match something else.
_EU: tuple[Instrument, ...] = (
    Instrument("DSGVO", "32016R0679", "regulation",
               ("Datenschutz-Grundverordnung", "Datenschutzgrundverordnung",
                "Verordnung (EU) 2016/679"),
               ("DS-GVO", "DSGVO", "EU-DSGVO")),
    Instrument("DSRL", "31995L0046", "directive",
               ("Datenschutzrichtlinie", "Richtlinie 95/46/EG"), ("DSRL",)),
    Instrument("ePrivacy-RL", "32002L0058", "directive",
               ("ePrivacy-Richtlinie", "Datenschutzrichtlinie für elektronische "
                "Kommunikation"),
               ("ePrivacyRL", "EK-DSRL")),
    Instrument("DSA", "32022R2065", "regulation",
               ("Verordnung über digitale Dienste", "Digitale-Dienste-Verordnung",
                "Gesetz über digitale Dienste"),
               ("DDV",)),
    Instrument("DMA", "32022R1925", "regulation",
               ("Verordnung über digitale Märkte", "Digitale-Märkte-Verordnung"),
               ("DMV",)),
    Instrument("KI-VO", "32024R1689", "regulation",
               ("KI-Verordnung", "Verordnung über künstliche Intelligenz",
                "Verordnung (EU) 2024/1689"),
               ("KIVO", "AI-Act", "AIAct")),
    Instrument("NIS-2-RL", "32022L2555", "directive",
               ("NIS-2-Richtlinie", "NIS2-Richtlinie"), ("NIS2", "NIS-2", "NIS2RL")),
    Instrument("DGA", "32022R0868", "regulation",
               ("Daten-Governance-Rechtsakt", "Datenverwaltungsverordnung"), ()),
    Instrument("Daten-VO", "32023R2854", "regulation",
               ("Datenverordnung", "Daten-Verordnung"), ("DatenVO",)),
    Instrument("P2B-VO", "32019R1150", "regulation",
               ("Plattform-zu-Unternehmen-Verordnung",), ("P2BVO",)),
    Instrument("ECRL", "32000L0031", "directive",
               ("E-Commerce-Richtlinie", "Richtlinie über den elektronischen "
                "Geschäftsverkehr"),
               ("E-Commerce-RL", "ECommerceRL")),
    Instrument("DSM-RL", "32019L0790", "directive",
               ("DSM-Richtlinie", "Richtlinie über das Urheberrecht im digitalen "
                "Binnenmarkt"),
               ("DSMRL",)),
    Instrument("InfoRL", "32001L0029", "directive",
               ("InfoSoc-Richtlinie", "Info-RL"), ("InfoSoc-RL",)),
    Instrument("AVMD-RL", "32010L0013", "directive",
               ("AVMD-Richtlinie", "Richtlinie über audiovisuelle Mediendienste"),
               ("AVMDRL",)),
    Instrument("eIDAS-VO", "32014R0910", "regulation",
               ("eIDAS-Verordnung",), ("eIDAS", "eIDASVO")),
    Instrument("EKEK", "32018L1972", "directive",
               ("Europäischer Kodex für die elektronische Kommunikation",
                "Richtlinie (EU) 2018/1972"),
               ("TK-Kodex",)),
    Instrument("JI-RL", "32016L0680", "directive",
               ("Richtlinie (EU) 2016/680", "JI-Richtlinie"), ("JIRL",)),
    Instrument("UGP-RL", "32005L0029", "directive",
               ("Richtlinie über unlautere Geschäftspraktiken", "UGP-Richtlinie"), ()),
    Instrument("VRRL", "32011L0083", "directive",
               ("Verbraucherrechterichtlinie",), ()),
    Instrument("Klausel-RL", "31993L0013", "directive",
               ("Klauselrichtlinie",), ("KlauselRL",)),
    Instrument("Geoblocking-VO", "32018R0302", "regulation",
               ("Geoblocking-Verordnung",), ("GeoblockingVO",)),
    Instrument("EMFA", "32024R1083", "regulation",
               ("Europäisches Medienfreiheitsgesetz",), ()),
    Instrument("EuGVVO", "32012R1215", "regulation",
               ("Brüssel Ia-Verordnung", "Verordnung (EU) Nr. 1215/2012",
                "Brüssel I-Verordnung"),
               ("Brüssel-Ia-VO", "EuGVVO 2012")),
    Instrument("Rom I-VO", "32008R0593", "regulation", ("Rom I-Verordnung",), ("RomI-VO",)),
    Instrument("Rom II-VO", "32007R0864", "regulation", ("Rom II-Verordnung",),
               ("RomII-VO",)),
    # Primary law and the Convention. ``GRC`` is the Austrian spelling of what German
    # practice writes ``GRCh``; both land on the consolidated Charter Work.
    Instrument("AEUV", "12016E", "treaty",
               ("Vertrag über die Arbeitsweise der Europäischen Union",), ()),
    Instrument("EUV", "12016M", "treaty",
               ("Vertrag über die Europäische Union",), ()),
    Instrument("GRC", "12012P", "treaty",
               ("Charta der Grundrechte der Europäischen Union", "Grundrechte-Charta",
                "GRC der EU"),
               ("GRCh", "EU-GRC", "GRC-EU", "EUGRC", "GRCharta")),
    Instrument("EMRK", "echr/convention", "treaty",
               ("Europäische Menschenrechtskonvention",
                "Konvention zum Schutze der Menschenrechte und Grundfreiheiten"),
               ("MRK",)),
)

# --- Austria's own digital and media acquis ----------------------------------
_AT_DIGITAL: tuple[Instrument, ...] = (
    _act("DSG", "Datenschutzgesetz", variants=("DSG 2000", "DSG2000"),
         transposes="32016R0679"),
    _act("TKG 2021", "Telekommunikationsgesetz 2021", transposes="32018L1972"),
    _act("TKG 2003", "Telekommunikationsgesetz 2003"),
    _act("TKG", "Telekommunikationsgesetz"),
    _act("ECG", "E-Commerce-Gesetz", transposes="32000L0031"),
    _act("KoPl-G", "Kommunikationsplattformen-Gesetz", variants=("KoPlG",)),
    _act("DSA-BeglG", "DSA-Begleitgesetz", variants=("DSABeglG",)),
    _act("NISG 2024", "Netz- und Informationssystemsicherheitsgesetz 2024",
         variants=("NISG2024",), transposes="32022L2555"),
    _act("NISG", "Netz- und Informationssystemsicherheitsgesetz"),
    _act("SVG", "Signatur- und Vertrauensdienstegesetz", transposes="32014R0910"),
    _act("E-GovG", "E-Government-Gesetz", variants=("EGovG",)),
    _act("AMD-G", "Audiovisuelle Mediendienste-Gesetz", variants=("AMDG",),
         transposes="32010L0013"),
    _act("ORF-G", "ORF-Gesetz", variants=("ORFG",)),
    _act("KOG", "KommAustria-Gesetz"),
    _act("MedienG", "Mediengesetz"),
    _act("PrR-G", "Privatradiogesetz", variants=("PrRG",)),
    _act("BaFG", "Barrierefreiheitsgesetz", transposes="32019L0882"),
    _act("ZaDiG 2018", "Zahlungsdienstegesetz 2018", variants=("ZaDiG2018", "ZaDiG")),
)

# --- the codes and procedural statutes every Austrian decision cites ----------
_AT_CORE: tuple[Instrument, ...] = (
    _act("B-VG", "Bundes-Verfassungsgesetz", variants=("BVG",)),
    _act("StGG", "Staatsgrundgesetz"),
    _act("ABGB", "Allgemeines bürgerliches Gesetzbuch"),
    _act("UGB", "Unternehmensgesetzbuch"),
    _act("HGB", "Handelsgesetzbuch"),
    _act("ZPO", "Zivilprozessordnung"),
    _act("JN", "Jurisdiktionsnorm"),
    _act("EO", "Exekutionsordnung"),
    _act("IO", "Insolvenzordnung"),
    _act("AußStrG", "Außerstreitgesetz", variants=("AusStrG", "AußStrG 2005")),
    _act("StGB", "Strafgesetzbuch"),
    _act("StPO", "Strafprozessordnung", variants=("StPO 1975",)),
    _act("VStG", "Verwaltungsstrafgesetz", variants=("VStG 1991",)),
    _act("AVG", "Allgemeines Verwaltungsverfahrensgesetz", variants=("AVG 1991",)),
    _act("VwGVG", "Verwaltungsgerichtsverfahrensgesetz"),
    _act("VwGG", "Verwaltungsgerichtshofgesetz", variants=("VwGG 1985",)),
    _act("VfGG", "Verfassungsgerichtshofgesetz", variants=("VfGG 1953",)),
    _act("BAO", "Bundesabgabenordnung"),
    _act("FinStrG", "Finanzstrafgesetz"),
    _act("GOG", "Gerichtsorganisationsgesetz"),
    _act("RAO", "Rechtsanwaltsordnung"),
    _act("NO", "Notariatsordnung"),
    _act("GebG", "Gebührengesetz", variants=("GebG 1957",)),
    _act("GGG", "Gerichtsgebührengesetz"),
    _act("RATG", "Rechtsanwaltstarifgesetz"),
)

# --- the substantive statutes Austrian litigation actually turns on -----------
_AT_SUBSTANCE: tuple[Instrument, ...] = (
    _act("KSchG", "Konsumentenschutzgesetz"),
    _act("FAGG", "Fern- und Auswärtsgeschäfte-Gesetz", transposes="32011L0083"),
    _act("VKrG", "Verbraucherkreditgesetz"),
    _act("PHG", "Produkthaftungsgesetz"),
    _act("EKHG", "Eisenbahn- und Kraftfahrzeughaftpflichtgesetz"),
    _act("UWG", "Bundesgesetz gegen den unlauteren Wettbewerb"),
    _act("KartG", "Kartellgesetz", variants=("KartG 2005",)),
    _act("WettbG", "Wettbewerbsgesetz"),
    _act("UrhG", "Urheberrechtsgesetz"),
    _act("MSchG", "Markenschutzgesetz"),
    _act("PatG", "Patentgesetz"),
    _act("MuSchG", "Mutterschutzgesetz"),
    _act("GmbHG", "GmbH-Gesetz"),
    _act("AktG", "Aktiengesetz"),
    _act("PSG", "Privatstiftungsgesetz"),
    _act("ArbVG", "Arbeitsverfassungsgesetz"),
    _act("AngG", "Angestelltengesetz"),
    _act("AVRAG", "Arbeitsvertragsrechts-Anpassungsgesetz"),
    _act("AZG", "Arbeitszeitgesetz"),
    _act("ARG", "Arbeitsruhegesetz"),
    _act("GlBG", "Gleichbehandlungsgesetz"),
    _act("BEinstG", "Behinderteneinstellungsgesetz"),
    _act("ASVG", "Allgemeines Sozialversicherungsgesetz"),
    _act("ASGG", "Arbeits- und Sozialgerichtsgesetz"),
    _act("MRG", "Mietrechtsgesetz"),
    _act("WEG 2002", "Wohnungseigentumsgesetz 2002", variants=("WEG2002", "WEG")),
    _act("GewO 1994", "Gewerbeordnung 1994", variants=("GewO",)),
    _act("EStG 1988", "Einkommensteuergesetz 1988", variants=("EStG",)),
    _act("KStG 1988", "Körperschaftsteuergesetz 1988", variants=("KStG",)),
    _act("UStG 1994", "Umsatzsteuergesetz 1994", variants=("UStG",)),
    _act("BWG", "Bankwesengesetz"),
    _act("WAG 2018", "Wertpapieraufsichtsgesetz 2018", variants=("WAG2018", "WAG")),
    _act("VAG 2016", "Versicherungsaufsichtsgesetz 2016", variants=("VAG2016", "VAG")),
    _act("VersVG", "Versicherungsvertragsgesetz"),
    _act("SPG", "Sicherheitspolizeigesetz"),
    _act("FPG", "Fremdenpolizeigesetz", variants=("FPG 2005",)),
    _act("AsylG 2005", "Asylgesetz 2005", variants=("AsylG2005", "AsylG")),
    _act("NAG", "Niederlassungs- und Aufenthaltsgesetz"),
    _act("BFA-VG", "BFA-Verfahrensgesetz", variants=("BFAVG",)),
    _act("StbG", "Staatsbürgerschaftsgesetz", variants=("StbG 1985",)),
    _act("GSpG", "Glücksspielgesetz"),
    _act("BVergG", "Bundesvergabegesetz", variants=("BVergG 2018", "BVergG2018")),
    _act("AWG", "Abfallwirtschaftsgesetz", variants=("AWG 2002",)),
    _act("UVP-G", "Umweltverträglichkeitsprüfungsgesetz", variants=("UVPG", "UVP-G 2000")),
    _act("AuskunftspflichtG", "Auskunftspflichtgesetz", variants=("AuskPflG",)),
    _act("IFG", "Informationsfreiheitsgesetz"),
    _act("GBK/GAW-Gesetz", "GBK/GAW-Gesetz", variants=("GBKGAWG",)),
)

INSTRUMENTS: tuple[Instrument, ...] = _EU + _AT_DIGITAL + _AT_CORE + _AT_SUBSTANCE


def _build() -> tuple[dict[str, Instrument], dict[str, Instrument]]:
    """(by folded abbreviation, by folded long title). First writer wins, so ``TKG
    2021`` keeps its own key while a bare ``TKG`` stays a separate, unversioned Work."""
    by_abbrev: dict[str, Instrument] = {}
    by_name: dict[str, Instrument] = {}
    for inst in INSTRUMENTS:
        for a in (inst.abbrev, *inst.variants):
            by_abbrev.setdefault(_fold(re.sub(r"[\s.\-/]+", "", a)), inst)
        for n in inst.names:
            by_name.setdefault(_fold(re.sub(r"\s+", " ", n)), inst)
    return by_abbrev, by_name


_BY_ABBREV, _BY_NAME = _build()

#: Every EU abbreviation an Austrian court may use, mapped to its CELEX. Includes the
#: German table's entries so a shared spelling ("DSGVO", "AEUV") resolves identically
#: whichever grammar sees it first — the point is that these are NOT Austrian statutes.
EU_IDS: dict[str, str] = {
    **{a: inst.candidate_id
       for a, inst in de_laws._BY_ABBREV.items() if not inst.candidate_id.startswith("de/")},
    **{a: inst.candidate_id
       for a, inst in _BY_ABBREV.items() if not inst.candidate_id.startswith("at/")},
}
#: Which of those are treaties rather than regulations/directives.
EU_TREATY_IDS: frozenset[str] = frozenset(
    inst.candidate_id for inst in (*INSTRUMENTS, *de_laws.INSTRUMENTS)
    if inst.kind == "treaty")


def resolve(token: str | None) -> Instrument | None:
    """The instrument an Austrian abbreviation or long title names, or None."""
    if not token:
        return None
    flat = _fold(re.sub(r"[\s.\-/]+", "", token))
    hit = _BY_ABBREV.get(flat)
    if hit is not None:
        return hit
    return _BY_NAME.get(_fold(re.sub(r"\s+", " ", token.strip())))


def eu_id(token: str | None) -> str | None:
    """The CELEX an abbreviation names, or None for an Austrian (or unknown) statute."""
    if not token:
        return None
    return EU_IDS.get(_fold(re.sub(r"[\s.\-/]+", "", token)))


#: The Austrian law-report series. Same job as ``german.LAW_REPORT_SERIES``: a
#: §-reference can trail INTO one and read it as the law ("§ 5 … JBl" →
#: at/gesetz/jbl), and "SZ 62/113" has the same shape as a docket. A report is not a
#: law and not a court. ``VfSlg``/``VwSlg`` are the constitutional and administrative
#: courts' own official collections and are the most-cited of all.
#:
#: The GERMAN series are folded in as well, because this pass runs over the whole corpus
#: and a report reference puts the statute it is filed under straight after the series
#: name: "BGHR StPO Abs. 3 Verfahrenshindernis 2" names a BGH report entry, not the
#: Austrian Strafprozessordnung. Sharing the list is also the point — a series is a
#: series whichever side of the border prints it.
_AT_REPORT_SERIES: frozenset[str] = frozenset({
    "vfslg", "vwslg", "sz", "evbl", "jbl", "rz", "ris-justiz", "risjustiz",
    "rdw", "ecolex", "wbl", "obl", "mr", "zvr", "zfrv", "immolex", "wobl", "miet",
    "anwbl", "zas", "drda", "ard", "asok", "efslg", "ifamz", "zak", "zfvb",
    "zib", "gesrz", "gru", "oba", "oja", "ojz", "ozw", "rfg", "rpa", "sprw",
    "zffr", "zpg", "zustellg", "beitrslg", "arbslg", "sslg", "amtsslg",
})
LAW_REPORT_SERIES: frozenset[str] = _AT_REPORT_SERIES | german.LAW_REPORT_SERIES

#: Structural and apparatus tokens that are not law abbreviations. Austria's
#: sub-provision vocabulary differs from Germany's — a numbered point is a **Ziffer**
#: ("Z 3"), not a Nummer, and a paragraph is written "Abs" without the full stop — so
#: the German stop-list does not cover it.
LAW_STOPWORDS: frozenset[str] = frozenset({
    "abs", "absatz", "z", "ziffer", "zif", "lit", "litera", "satz", "s", "halbsatz",
    "hs", "fall", "rz", "randziffer", "rn", "nr", "nrn", "nummer", "art", "artikel",
    "anh", "anhang", "anl", "anlage", "abschn", "abschnitt", "teil", "kap", "kapitel",
    "ff", "f", "idf", "idgf", "ivm", "va", "vgl", "aao", "mwn", "ua", "uva", "bgbl",
    "lgbl", "rgbl", "stgbl", "abl", "eu", "eg", "ewg", "mwh", "zb", "bzw",
    *LAW_REPORT_SERIES,
})


def is_law_abbreviation(token: str) -> bool:
    """Does this token read as an Austrian law abbreviation rather than an ordinary word?

    Two capitals or more (ABGB, ZPO, KSchG, B-VG, AußStrG) and not a piece of citation
    apparatus — the same test ``german._is_law_abbreviation`` applies, with Austria's
    stop-list. A recognised entry in the table passes regardless of shape, which is how
    the one-capital ``EO``/``IO``/``JN``/``NO`` codes stay recognisable.
    """
    stem = (token or "").split()[0] if (token or "").strip() else ""
    if not stem:
        return False
    folded = re.sub(r"[^a-z0-9]+", "", _fold(stem))
    if folded in LAW_STOPWORDS:
        return False
    if resolve(stem) is not None:
        return True
    return sum(1 for ch in stem if ch.isupper()) >= 2


# --- bare instrument mentions -------------------------------------------------
_ABBREVS = {a for inst in INSTRUMENTS for a in (inst.abbrev, *inst.variants)}
#: Austrian determiner set, used exactly as ``de_laws`` uses the German one: a short
#: abbreviation ("DSG", "DSA", "ECG", "EO") is ambiguous across languages, so it only
#: counts as an instrument mention behind a German-language article. Four characters and
#: up stand alone.
_DETERMINER = r"(?:der|die|das|des|dem|den|dieser|diese|dieses|eines|einer|nach|gemäß|iSd|iSv)"
_SHORT = "|".join(re.escape(a) for a in sorted(
    (a for a in _ABBREVS if len(re.sub(r"[\s.\-/]+", "", a)) < 4), key=len, reverse=True))
_LONG = "|".join(re.escape(a) for a in sorted(
    (a for a in _ABBREVS if len(re.sub(r"[\s.\-/]+", "", a)) >= 4), key=len, reverse=True))
BARE_INSTRUMENT_RE = re.compile(
    rf"(?<![\w§])(?:"
    rf"(?:{_DETERMINER}\s+)?(?P<abbrev>{_LONG})"
    rf"|(?i:{_DETERMINER})\s+(?P<short>{_SHORT})"
    rf")(?![\wÄÖÜäöüß])")

_NAMES = {n for inst in INSTRUMENTS for n in inst.names}
NAMED_INSTRUMENT_RE = re.compile(
    rf"(?<![\wÄÖÜäöüß])(?:(?:der|die|das|des|dem|den)\s+)?"
    rf"(?P<name>{'|'.join(re.escape(n) for n in sorted(_NAMES, key=len, reverse=True))})"
    rf"(?![\wÄÖÜäöüß])", re.IGNORECASE)

_PRECEDING_TOKEN = re.compile(r"([\w.\-]+)\s+$")


def _in_report_citation(text: str, start: int) -> bool:
    m = _PRECEDING_TOKEN.search(text[max(0, start - 24):start])
    return bool(m) and _fold(m.group(1)).strip(".") in LAW_REPORT_SERIES


def instrument_citations(text: str, *, occupied: list[tuple[int, int]] | None = None
                         ) -> list[Citation]:
    """Every listed instrument this text NAMES without a § — long title first, then bare
    abbreviation. ``occupied`` are spans a §-anchored citation already claimed."""
    spans: list[tuple[int, int]] = list(occupied or [])
    found: list[Citation] = []

    def free(start: int, end: int) -> bool:
        return not any(s < end and start < e for s, e in spans)

    for match in NAMED_INSTRUMENT_RE.finditer(text):
        inst = resolve(match.group("name"))
        if inst is None or not free(match.start(), match.end()):
            continue
        spans.append((match.start(), match.end()))
        found.append(Citation(
            raw=match.group(0), entity_kind=inst.kind, candidate_id=inst.candidate_id,
            pinpoint=None, char_start=match.start(), char_end=match.end(),
            method="at_instrument_name", confidence=0.9))
    for match in BARE_INSTRUMENT_RE.finditer(text):
        abbrev = match.group("abbrev") or match.group("short")
        inst = resolve(abbrev)
        if inst is None or not free(match.start(), match.end()):
            continue
        at = match.start("abbrev") if match.group("abbrev") else match.start("short")
        if _in_report_citation(text, at):
            continue
        spans.append((match.start(), match.end()))
        found.append(Citation(
            raw=match.group(0), entity_kind=inst.kind, candidate_id=inst.candidate_id,
            pinpoint=None, char_start=match.start(), char_end=match.end(),
            method="at_instrument_abbrev", confidence=0.85))
    return found
