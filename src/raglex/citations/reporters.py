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
    # -- EU (parallel to CELEX) --
    "CMLR", "CEC",
    # -- pan-Commonwealth / wide-travelling series (see WIDE_TRAVELLING below) --
    "LRC", "BHRC", "ITELR", "WIR", "EA", "Cth LR",
    # -- Canada (ordinal series print as "(1990) 70 DLR (4th) 385") --
    "DLR", "DLR (2d)", "DLR (3d)", "DLR (4th)", "SCR", "RCS", "CCC", "CCC (2d)",
    "CCC (3d)", "CR", "WWR", "OR", "OR (2d)", "OR (3d)", "OAC", "CRR", "CPR", "CPR (3d)",
    "BCLR", "AR", "Alta LR", "Man R", "Sask R", "NSR", "NBR", "Nfld & PEIR", "RJQ",
    "ETR", "RFL", "CTC", "ATC", "Admin LR", "MVR",
    # -- Australia --
    "CLR", "ALR", "ALJR", "FCR", "FLR", "Fam LR", "ALD", "FLC",
    "NSWLR", "SR (NSW)", "VR", "VLR", "Qd R", "St R Qd", "SASR", "SRSA", "WAR", "WALR",
    "Tas R", "Tas SR", "NTLR", "NTR", "ACTLR", "ACTR",
    "A Crim R", "ACSR", "FamLR", "IR",
    # -- New Zealand --
    "NZLR", "NZAR", "NZFLR", "DCR", "NZBLC", "BCL",
    # -- Singapore & Malaysia (MLJ spans both, plus historical Brunei) --
    "SLR", "SLR(R)", "MLJ", "MLJU", "CLJ", "AMR",
    # -- Hong Kong --
    "HKLRD", "HKC", "HKCFAR", "HKLR", "HKEC", "HKCU",
    # -- South Africa --
    "SA", "All SA", "BCLR", "SACR", "ILJ", "SALR",
    # -- India --
    "SCC", "AIR", "SCALE", "JT", "Bom LR", "Ker LT", "All LJ",
    # -- Africa (other) --
    "KLR", "GLR", "SCGLR", "GLRD", "NWLR", "FWLR", "NSCC", "LPELR", "ZLR", "NR", "ZR",
    "ULR",
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


# Series that travel across jurisdictions — they name the *series*, never the country.
# A "[2015] 3 LRC 1" tells you the report, not whether the case is Ghanaian or Fijian, so
# jurisdiction must come from the court in the parallel neutral citation or the trailing
# "(COURT)" parenthetical. Inferring a country from these is simply wrong.
WIDE_TRAVELLING: frozenset[str] = frozenset({
    "LRC",          # Law Reports of the Commonwealth — any member state
    "BHRC",         # Butterworths Human Rights Cases — international
    "ITELR",        # International Trust & Estate Law Reports
    "WIR",          # West Indian Reports — regional, Commonwealth Caribbean
    "EA",           # East Africa Law Reports — Kenya/Uganda/Tanzania
    "MLJ", "MLJU",  # Malayan Law Journal — Malaysia + pre-1965 Singapore + Brunei
    "AC", "WLR", "QB", "KB", "Ch", "All ER", "ER",  # English series cited everywhere
    "Lloyd's Rep", "BCLC",
})

# Series that DO tie to one jurisdiction. Used to place a reported-only citation
# ("(2019) 12 NWLR (Pt 1685) 1" → Nigeria) that has no neutral citation to learn from.
# Deliberately omits the wide-travelling series above and the worst collisions below.
REPORTER_JURISDICTION: dict[str, str] = {
    # Canada
    **{r: "CA" for r in ("DLR", "SCR", "RCS", "CCC", "CR", "WWR", "OR", "OAC", "BCLR",
                         "AR", "Alta LR", "Man R", "Sask R", "NSR", "NBR",
                         "Nfld & PEIR", "RJQ", "ETR", "RFL", "CTC", "ATC", "MVR")},
    # Australia
    **{r: "AU" for r in ("CLR", "ALR", "ALJR", "FCR", "ALD", "FLC", "NSWLR", "SR (NSW)",
                         "VR", "VLR", "Qd R", "St R Qd", "SASR", "SRSA", "WAR", "WALR",
                         "Tas R", "Tas SR", "NTLR", "NTR", "ACTLR", "ACTR", "A Crim R",
                         "ACSR")},
    # New Zealand
    **{r: "NZ" for r in ("NZLR", "NZAR", "NZFLR", "DCR", "NZBLC", "BCL")},
    # Singapore / Hong Kong / South Africa / India
    **{r: "SG" for r in ("SLR", "SLR(R)")},
    **{r: "HK" for r in ("HKLRD", "HKC", "HKCFAR", "HKLR", "HKEC", "HKCU")},
    **{r: "ZA" for r in ("All SA", "SACR", "SALR")},
    **{r: "IN" for r in ("SCC", "AIR", "SCALE", "JT", "Bom LR", "Ker LT", "All LJ")},
    **{r: "MY" for r in ("CLJ", "AMR")},
    # Africa
    **{r: "KE" for r in ("KLR",)},
    **{r: "GH" for r in ("GLR", "SCGLR", "GLRD")},
    **{r: "NG" for r in ("NWLR", "FWLR", "NSCC", "LPELR")},
    **{r: "ZW" for r in ("ZLR",)}, **{r: "ZM" for r in ("ZR",)},
    **{r: "NA" for r in ("NR",)}, **{r: "UG" for r in ("ULR",)},
    # Ireland
    **{r: "IE" for r in ("IR", "ILRM", "ILTR", "Ir Jur Rep", "LR Ir", "Frewen")},
}

# Abbreviations that mean different things in different places and must NEVER be read as
# a jurisdiction signal on their own (reference Part 14). "SA" is the South African Law
# Reports *and* South Australia; "IR" is the Irish Reports *and* Australian Industrial
# Reports; "SC" is Session Cases, a Nigerian reporter, and "Supreme Court" in a dozen
# systems. These resolve only from surrounding court context.
COLLIDING_ABBREVS: frozenset[str] = frozenset({
    "SA", "SC", "CA", "LR", "IR", "AC", "CR", "SCR", "EA", "NR", "FCA", "HCA", "SCC",
})


def reporter_jurisdiction(series: str) -> str | None:
    """The jurisdiction a report series implies, or None when it implies none.

    Returns None for wide-travelling series and for the known collisions — the honest
    answer, because guessing a country from an ambiguous abbreviation is exactly the
    error the collision table warns about.
    """
    token = " ".join((series or "").split())
    if token in WIDE_TRAVELLING or token in COLLIDING_ABBREVS:
        return None
    return REPORTER_JURISDICTION.get(token)


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


# Case-insensitive twins of the report shapes, for restoring capitalisation to a
# citation that has been through fold(). Aliases are STORED casefolded so they
# compare reliably, which means the case-sensitive matchers above never fire on
# them — anything that wants to *display* a stored alias has to re-case it here.
_REPORT_RE_CI = re.compile(REPORT_RE.pattern, re.IGNORECASE)
_SCOTS_BARE_RE_CI = re.compile(SCOTS_BARE_RE.pattern, re.IGNORECASE)
_ENGLISH_REPORTS_RE_CI = re.compile(ENGLISH_REPORTS_RE.pattern, re.IGNORECASE)
_OLD_LAW_REPORTS_RE_CI = re.compile(OLD_LAW_REPORTS_RE.pattern, re.IGNORECASE)

# "[2002] ewca civ 1642" — court code uppercases, division takes its registry casing.
_NEUTRAL_CI = re.compile(
    r"(?P<pre>\[(?:1[5-9]|20)\d{2}\]\s+)(?P<court>[A-Za-z]{2,10})"
    r"(?P<mid>\s+)(?P<div>[A-Za-z]{2,12})?(?P<post>\s*)(?P<num>\d+)",
    re.IGNORECASE,
)


def _canonical_series(matched: str) -> str:
    """The canonical spelling of a series token however it was punctuated/cased
    ("all e.r. (comm)" → "All ER (Comm)")."""
    key = re.sub(r"[.\s'’]", "", matched).upper()
    for s in REPORT_SERIES:
        if re.sub(r"[.\s'’]", "", s).upper() == key:
            return s
    return matched


def display_citation(raw: str | None) -> str:
    """Restore conventional capitalisation to a folded citation string, so stored
    aliases read as lawyers write them: "[2003] 1 all e.r. (comm) 140" becomes
    "[2003] 1 All ER (Comm) 140", "[2002] ewca civ 1642" becomes
    "[2002] EWCA Civ 1642". Punctuation variants collapse to the canonical series
    spelling; anything unrecognised is returned untouched rather than guessed at."""
    if not raw:
        return raw or ""
    out = raw

    # report series — rewrite just the matched series span, so the surrounding
    # year/volume/page survive exactly as stored
    for rx, group in ((_REPORT_RE_CI, "series"), (_SCOTS_BARE_RE_CI, "series"),
                      (_OLD_LAW_REPORTS_RE_CI, "court")):
        m = rx.search(out)
        if m and m.group(group):
            s, e = m.span(group)
            out = out[:s] + _canonical_series(m.group(group)) + out[e:]
    m = _ENGLISH_REPORTS_RE_CI.search(out)
    if m:
        out = re.sub(r"(?<=\d\s)e\.?\s?r\.?(?=\s\d)", "ER", out, flags=re.IGNORECASE)

    # neutral citation — "ewca civ" → "EWCA Civ" (division casing from the registry)
    def _neutral_case(m: re.Match) -> str:
        from .courts import DIVISIONS

        div = m.group("div") or ""
        if div:
            canon = next((d for d in DIVISIONS if d.upper() == div.upper()), None)
            if canon is None:      # not a division: leave the tail alone entirely
                return m.group(0)
            div = canon
        return (m.group("pre") + m.group("court").upper() + m.group("mid")
                + div + m.group("post") + m.group("num"))

    out = _NEUTRAL_CI.sub(_neutral_case, out, count=1)

    # trailing chamber/division parenthetical — "[2012] UKUT 440 (aac)" → "(AAC)",
    # "[2019] EWHC 22 (admin)" → "(Admin)". Chambers are initialisms, divisions are
    # words; the registry decides which, and anything else is left alone.
    def _chamber_case(m: re.Match) -> str:
        from .courts import DIVISIONS

        tok = m.group(1)
        canon = next((d for d in DIVISIONS if d.upper() == tok.upper()), None)
        return f"({canon or tok.upper()})"

    out = re.sub(r"(?<=\d\s)\(([A-Za-z]{2,12})\)\s*$", _chamber_case, out)

    # bare series labels that carry no page shape of their own
    for pat, label in _LABEL_ONLY:
        ci = re.compile(pat.pattern, re.IGNORECASE)
        out = ci.sub(label, out)
    return out


# ── which jurisdiction a report series belongs to (Westlaw/Lexis export filter) ──
# The fine-grained country map ``REPORTER_JURISDICTION`` is the source of truth — it
# already carries Canada / Australia / NZ / Singapore / Hong Kong / South Africa / India /
# Malaysia. The export filter buckets PER JURISDICTION off it: the old coarse
# "commonwealth" slot both burned batch slots AND mislabelled Australian FCR / SR (NSW) /
# ALD, Canadian OR / RJQ etc. as UK, because those series were absent from the small
# hand-maintained set and fell through to the "uk" default. Series not listed still default
# to "uk" (England & Wales, Scotland, NI, the nominate and practitioner series — the bulk
# a UK subscription genuinely holds).
_EXPORT_SERIES_EXTRA: dict[str, str] = {
    # Wide-travelling / neutral-court forms REPORTER_JURISDICTION deliberately omits from
    # auto-resolution, but which are unambiguous enough for a manual export picker.
    "MLJ": "MY", "MLJU": "MY", "SGCA": "SG", "SGHC": "SG",
    # Canadian series REPORTER_JURISDICTION doesn't carry (editioned forms fold to the base
    # via _SERIES_EDITION_RE below; these two have no base entry).
    "CRR": "CA", "CPR": "CA", "FamLR": "AU",
    # Irish and EU series, kept distinct from the UK default.
    "IR": "IE", "ILRM": "IE", "ILTR": "IE", "Ir Jur Rep": "IE", "Ir Jur": "IE",
    "LR Ir": "IE", "Frewen": "IE",
    "CMLR": "EU", "CEC": "EU", "ECR": "EU", "EHRR": "EU",
}
_EXPORT_SERIES_JURISDICTION: dict[str, str] = {**REPORTER_JURISDICTION, **_EXPORT_SERIES_EXTRA}

# An ordinal edition suffix ("(2d)", "(3d)", "(4th)") never changes the jurisdiction, so
# strip it before the lookup — but a country/court parenthetical ("SR (NSW)") must survive.
_SERIES_EDITION_RE = re.compile(r"\s*\((?:\d+(?:st|nd|rd|th|d))\)\s*$", re.I)
# A round-bracketed year is the Australian-FCR signal; a square-bracketed one is English.
_ROUND_YEAR_RE = re.compile(r"\((?:1[6-9]|20)\d{2}\)")
# Report series whose jurisdiction is carried by bracket style, not the token itself.
_BRACKET_AMBIGUOUS: frozenset[str] = frozenset({"FCR"})


def series_jurisdiction(series: str | None, raw: str | None = None) -> str:
    """The jurisdiction a report series belongs to, as a country-code bucket the
    Westlaw/Lexis export filter groups by — ``uk`` (default: England & Wales, Scotland, NI,
    the nominate reports), ``ie``, ``eu``, or a specific Commonwealth jurisdiction (``ca``,
    ``au``, ``nz``, ``sg``, ``hk``, ``za``, ``in``, ``my``, and the African/other long
    tail). A UK subscription can't retrieve a foreign report, so labelling one correctly
    keeps it out of a UK batch where it would just burn a slot.

    ``raw`` (the full citation) disambiguates the handful of series whose jurisdiction is
    carried by bracket style, not the token: **FCR** is the *English* Family Court Reports
    when year-bracketed (``[1993] 1 FCR 553``) but the *Australian* Federal Court Reports
    when volume-numbered (``(1993) 43 FCR 280``)."""
    token = " ".join((series or "").split())
    if not token:
        return "uk"
    base = _SERIES_EDITION_RE.sub("", token).strip()
    if base in _BRACKET_AMBIGUOUS:
        # FCR: round-bracketed year → Australia; square-bracketed (or unknown) → England.
        return "au" if raw and _ROUND_YEAR_RE.search(raw) else "uk"
    j = _EXPORT_SERIES_JURISDICTION.get(token) or _EXPORT_SERIES_JURISDICTION.get(base)
    return j.lower() if j else "uk"


def report_citations(raw: str | None) -> list[str]:
    """Every law-report citation appearing in a string, as matched.

    Used by the bulk case-law importers to mint resolution aliases. A dataset gives a
    case's citation as a whole style-of-cause ("Mabo v Queensland (No 2) (1992) 175 CLR
    1"), but the extractor records only the report citation itself ("(1992) 175 CLR 1")
    as the edge's folded raw string — and an alias fires on an exact folded match. So
    the alias has to be the *matched substring*, produced by these same patterns, or it
    would never join to anything.
    """
    out: list[str] = []
    for pattern in _ALL_REPORT_RES:
        for m in pattern.finditer(raw or ""):
            text = " ".join(m.group(0).split())
            if text and text not in out:
                out.append(text)
    return out
