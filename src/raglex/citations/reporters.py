"""Classic law-report citations (§5) — the pre-neutral-citation frontier.

Before neutral citations (England & Wales adopted them in 2001), a case was cited by its
*law report*: "[1982] AC 1", "(1985) 80 Cr App R 1", "Rylands v Fletcher (1868) LR 3 HL
330", "150 ER 1030". These resolve to no fetchable id — there is no neutral citation to
build a legislation.gov.uk / Find Case Law URL from — so, like the European Court Reports
(`ecr_report`), they are recorded **candidate-less**. But recognising them matters: a
heavily-cited but un-harvestable authority ("Donoghue v Stevenson [1932] AC 562") is
exactly what the operator needs to *see*, ranked by how often the corpus reaches for it,
even though the system can't fetch it. The reader then offers a BAILII link, and an
uploaded RTF resolves every pending citation to it at once.

Robustness goals (the grammar must catch reports as they really appear):
  * both bracket styles — ``[1982]`` (year is the finding key) and ``(1985)`` (running
    volume numbers);
  * punctuation variants — ``AC`` / ``A.C.``, ``All ER`` / ``All E.R.``, ``Cr App R`` /
    ``Cr. App. R.``;
  * three structural shapes — modern ``[year] (vol) SERIES page``, the English Reports
    reprint ``vol ER page``, and the old Law Reports first series ``(year) LR vol COURT
    page``.

Coverage is data, not code: add an abbreviation to ``REPORT_SERIES`` (or a court to
``_OLD_LR_COURTS``) and every citation of it is recognised.
"""

from __future__ import annotations

import re

# Report-series abbreviations. Order here doesn't matter — the alternation is built
# longest-first so "All ER (Comm)" wins over "All ER" and "QBD" over "QB". EHRR and ECR
# are deliberately absent: they have dedicated grammars routing to the ECtHR / CJEU
# adapters, so they are more than merely "unfetchable reports".
REPORT_SERIES: tuple[str, ...] = (
    # -- England & Wales: the Law Reports (ICLR) and the big general series --
    "AC", "App Cas", "QB", "QBD", "KB", "KBD", "Ch", "Ch D", "Ch App", "Fam", "P", "PD",
    "Prob", "LR", "Ex", "Ex D", "CPD", "CP", "CP Rep",
    "WLR", "WLR (D)", "All ER", "All ER (Comm)", "All ER (D)", "All ER Rep", "WLUK",
    "Bus LR", "PTSR", "PTSLR",
    # -- criminal --
    "Cr App R", "Cr App R (S)", "Crim LR", "Cox CC", "Cox's Crim Cas", "JC",
    # -- specialist / practitioner series --
    "ICR", "IRLR", "FSR", "RPC", "EMLR", "HRLR", "UKHRR", "LGR", "HLR", "EGLR", "EG",
    "P & CR", "BLR", "Con LR", "TCLR", "STC", "STC (SCD)", "SFTD", "STI", "WTLR", "FLR",
    "FCR", "Fam Law", "BMLR", "Med LR", "Env LR", "JPL", "PLR", "RVR", "RTR", "CTLC",
    "BCC", "BCLC", "B & CR", "BPIR", "Pens LR", "PNLR", "Costs LR", "ACD", "COD", "ELR",
    "Ed LR", "Imm AR", "INLR", "Info TLR", "ITLR", "ITR", "Tax Cas", "TC", "CLY",
    "LS Gaz", "LS Gaz R", "SJLB", "SJ", "Sol Jo", "JP", "JPN", "LT", "TLR", "FTLR",
    # -- Lloyd's Law Reports family --
    "Lloyd's Rep", "Lloyd's List Rep", "Ll L Rep", "Lloyd's Rep IR", "Lloyd's Rep Med",
    "Lloyd's Rep FC", "Lloyd's Rep PN", "Lloyd's Rep Ban",
    # -- Scotland --
    "SC", "SC (HL)", "SC (J)", "SLT", "SCLR", "SCCR", "SLCR", "F", "R", "M", "D", "S",
    "Sh Ct Rep", "GWD",
    # -- Northern Ireland --
    "NI", "NIJB", "NILR", "BNIL",
    # -- Ireland (the neutral-citation courts IESC/IEHC/IECA are deliberately NOT
    # here: they're real courts in citations.courts, and listing them would
    # suppress the iehc/2008/56 candidate their citations must mint) --
    "IR", "ILRM", "ILTR", "Ir Jur Rep", "Ir Jur", "LR Ir", "Frewen",
    # -- EU (parallel to CELEX) & frequently-cited Commonwealth --
    "CMLR", "CEC", "DLR", "CLR", "SCR", "NZLR", "HKLRD", "HKC", "SGCA", "SLR",
    # -- Canada (ordinal series print as "(1990) 70 DLR (4th) 385") --
    "DLR (2d)", "DLR (3d)", "DLR (4th)", "CCC", "CCC (2d)", "CCC (3d)", "WWR",
    "OR (2d)", "OR (3d)", "CRR", "CPR (3d)",
    # -- Australia --
    "ALR", "ALJR", "NSWLR", "VR", "WAR", "SASR", "Qd R", "A Crim R", "ACSR", "FamLR",
    # -- New Zealand --
    "NZAR", "NZFLR",
    # -- nominate / very old (year-bracketed usage) --
    "ER", "Term Rep", "East", "B & C", "B & Ald", "M & W", "Bing", "Taunt", "Camp",
    "Esp", "Stark", "H & N", "H Bl", "Wils KB", "Barn KB", "Burr", "Cowp", "Doug KB",
    "Mod", "Salk", "Ld Raym", "Show KB", "Vent", "Lev", "Keb", "Sid", "Style",
    "Co Rep", "Cro Eliz", "Cro Jac", "Cro Car", "Plowd", "Dyer", "Leon", "Moore KB",
    "Ves Jr", "Ves Sen", "Atk", "P Wms", "Swans", "Mer", "Russ", "My & K", "De G & J",
    "Hare", "Beav", "Drew", "Giff", "Sim", "Mad", "Jac & W",
)

# Court/series token in the OLD Law Reports first series (1865–1875): "LR 3 HL 330",
# "LR 7 QB 339", "LR 1 Ex 265", "LR 2 CP 311", "LR 4 Eq 1", "LR 2 A & E 5".
_OLD_LR_COURTS = (
    "HL", "PC", "HL Sc", "HL Ir", "Sc & Div", "QB", "CP", "Ex", "Eq", "Ch App", "Ch",
    "P & D", "A & E", "CCR", "Adm & Ecc", "Ir", "Ind App", "PC App",
)


def _dotify(token: str) -> str:
    """A dot- and space-tolerant regex for one abbreviation token: an initialism like
    ``AC`` matches ``A.C.``/``AC``/``A C``; a word like ``App`` allows an optional trailing
    dot. So the alternation catches ``A.C.``, ``All E.R.``, ``Cr. App. R.`` etc. Straight
    and curly apostrophes both match (``Lloyd's`` / ``Lloyd's``)."""
    if token.isalpha() and token.isupper() and len(token) >= 2:
        body = r"\.?\s?".join(re.escape(c) for c in token) + r"\.?"
    else:
        body = re.escape(token) + r"\.?"
    return body.replace(re.escape("'"), "['’]")


def _series_pattern(series: str) -> str:
    return r"\s*".join(_dotify(t) for t in series.split(" "))


# Alternation over all series, longest literal first so specific series win overlaps.
_SERIES_ALT = "|".join(
    _series_pattern(s) for s in sorted(REPORT_SERIES, key=len, reverse=True)
)
_YEAR = r"(?:1[5-9]|20)\d{2}"  # law reports run from the 16th c. (nominate) to date
_YEAR_BRACKET = rf"(?:\[{_YEAR}\]|\({_YEAR}\))"

# Shape 1 — modern: "[1982] AC 1", "[1996] 2 All ER 129", "(1985) 80 Cr App R 1".
REPORT_RE = re.compile(
    rf"{_YEAR_BRACKET}\s+(?:(?P<vol>\d{{1,3}})\s+)?(?P<series>{_SERIES_ALT})\s+(?P<page>\d{{1,4}})\b"
)

# Shape 2 — English Reports reprint: "150 ER 1030", "2 ER 55" (volume, ER, page; no year).
ENGLISH_REPORTS_RE = re.compile(r"\b(?P<vol>\d{1,3})\s+E\.?\s?R\.?\s+(?P<page>\d{1,4})\b")

# Shape 3 — old Law Reports first series: "LR 3 HL 330", "(1868) LR 7 QB 339".
_OLD_COURT_ALT = "|".join(_series_pattern(c) for c in sorted(_OLD_LR_COURTS, key=len, reverse=True))
OLD_LAW_REPORTS_RE = re.compile(
    rf"(?:\({_YEAR}\)\s+)?L\.?\s?R\.?\s+(?P<vol>\d{{1,2}})\s+(?P<court>{_OLD_COURT_ALT})\s+(?P<page>\d{{1,4}})\b"
)

# Shape 4 — Scottish & Justiciary reports conventionally omit the year bracket:
# "1999 SC 583", "2001 SLT 1213", "1998 SCCR 62", "1949 JC 1". Only the distinctive
# multi-letter series (never bare single letters like F/R/D, which would match anything).
SCOTS_SERIES = ("SC (HL)", "SC (J)", "SC", "SLT", "SCCR", "SCLR", "SLCR", "JC", "GWD")
_SCOTS_ALT = "|".join(_series_pattern(s) for s in sorted(SCOTS_SERIES, key=len, reverse=True))
SCOTS_BARE_RE = re.compile(
    rf"\b(?P<year>(?:1[89]|20)\d{{2}})\s+(?P<series>{_SCOTS_ALT})\s+(?P<page>\d{{1,4}})\b"
)

_ALL_REPORT_RES = (OLD_LAW_REPORTS_RE, REPORT_RE, SCOTS_BARE_RE, ENGLISH_REPORTS_RE)


# Report series that are NOT registered as grammars (they have dedicated adapters/grammars
# — ECR → CJEU, EHRR → ECtHR) but which still need a report *label* when they surface as
# unfetchable (a report page number resolves to no fetchable id either way).
_LABEL_ONLY = (
    (re.compile(r"\bE\.?\s?C\.?\s?R\.?\b"), "ECR"),
    (re.compile(r"\bE\.?\s?H\.?\s?R\.?\s?R\.?\b"), "EHRR"),
)


def report_series(raw: str | None) -> str | None:
    """The report series a citation string names ("AC", "All ER", "LR", "ER", "ECR"), or
    None if it isn't a recognised report citation. Labels and groups the unfetchable
    frontier — so a European Court Reports page ("[1974] ECR 837") reads as the *report*
    it is, not a spurious neutral citation."""
    if not raw:
        return None
    # a bracketed/parenthesised year with ECR/EHRR is that report (page number, no id)
    if re.search(r"[\[(](?:1[6-9]|20)\d{2}", raw):
        for pat, label in _LABEL_ONLY:
            if pat.search(raw):
                return label
    m = REPORT_RE.search(raw)
    if m:
        # normalise the matched series back to its canonical form (strip dots/spaces)
        got = re.sub(r"[.\s'’]", "", m.group("series")).upper()
        for s in REPORT_SERIES:
            if re.sub(r"[.\s'’]", "", s).upper() == got:
                return s
        return m.group("series")
    if OLD_LAW_REPORTS_RE.search(raw):
        return "LR"
    ms = SCOTS_BARE_RE.search(raw)
    if ms:
        return ms.group("series")
    if ENGLISH_REPORTS_RE.search(raw):
        return "ER"
    return None


def is_report_citation(raw: str | None) -> bool:
    return report_series(raw) is not None
