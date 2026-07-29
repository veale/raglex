"""German extract → normalise helpers.

German statutory references are one-to-many.  ``bundesrecht.normalise`` expands
ranges, i.V.m. joins and compact sub-provision lists before we create graph edges.
The destination is the federal law Work (the same ``de/gesetz/<jurabk>`` id minted by
GII); the exact canonical provision is retained as ``dst_anchor``.
"""

from __future__ import annotations

import re
import unicodedata

from bundesrecht import normalise

from .models import Citation


def law_id(abbreviation: str) -> str:
    return "de/gesetz/" + re.sub(r"[^a-z0-9]+", "", _fold_law(abbreviation))


def normalise_docket(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").upper()
    return re.sub(r"\s+", " ", value.replace("–", "-").replace("—", "-")).strip(" ,.;")


def case_alias(court: str, docket: str) -> str:
    court_raw = re.sub(r"[^A-ZÄÖÜ0-9]+", "", (court or "").upper())
    court_key = {
        "BUNDESVERFASSUNGSGERICHT": "BVERFG",
        "BUNDESGERICHTSHOF": "BGH",
        "BUNDESARBEITSGERICHT": "BAG",
        "BUNDESFINANZHOF": "BFH",
        "BUNDESSOZIALGERICHT": "BSG",
        "BUNDESVERWALTUNGSGERICHT": "BVERWG",
        "BUNDESPATENTGERICHT": "BPATG",
    }.get(court_raw, court_raw)
    docket_key = re.sub(r"[^A-Z0-9/.-]+", "", normalise_docket(docket))
    return f"de:case:{court_key}:{docket_key}"


# Deliberately bounded.  It starts on §/Art., accepts only provision vocabulary, and
# ends on a law abbreviation.  This avoids the unbounded legal-regex failure mode that
# previously wedged whole-corpus rescans.
_PARA = r"\d{1,5}[a-z]?"
_SUB = (r"(?:Abs(?:atz)?\.?|S(?:atz)?\.?|Nrn?\.?|Nummer|Buchst(?:abe)?\.?|"
        r"Alt(?:ernative)?\.?|Halbs(?:atz)?\.?|HS\.?)\s*(?:\d+[a-z]?|[a-z]|[IVX]+)")
_TAIL = rf"(?:\s*(?:{_SUB}|[,;]|und\b|oder\b|bis\s+{_PARA}|[-–—]\s*{_PARA}|f{{1,2}}\.))*"
_ONE = rf"(?:§§?|Art(?:ikel|\.)?)\s*{_PARA}{_TAIL}"
_COMPACT_ONE = rf"(?:§|Art(?:ikel|\.)?)\s*{_PARA}\s+(?:[IVX]{{1,4}}|\(\d+\))\s+\d+"
_IVM = rf"(?:\s+i\.?\s*V\.?\s*m\.?\s+{_ONE})?"
# The law abbreviation is matched CASE-SENSITIVELY (``(?-i:…)``) inside an otherwise
# case-insensitive pattern, because the rest of the reference is written both ways
# ("Art." / "art.", "Abs." / "abs.") but an abbreviation never is. Case-insensitivity
# here was minting a law out of the next word: "§ 8 Abs. 2 Nr. 1 MarkenG i.V.m. …" read
# the "i" as a book numeral ("MarkenG I" → de/gesetz/markeng1) and an English "v" in
# "BGB v Smith" the same way ("BGB V" → de/gesetz/bgb5) — thousands of phantom siblings
# of laws the corpus already holds. The book numeral itself is real (SGB V → sgb5, the
# key gesetze-im-internet uses), so it stays — but not before an ordinal point, which
# is an edition or a Halbsatz ("BGB 5. Aufl.", "BGB 1. Alt."), never a book.
_LAW = r"(?-i:[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9]*(?:\s+(?:[IVX]{1,4}|\d{1,2}(?!\.)))?)"
LAW_REFERENCE_RE = re.compile(
    rf"(?P<raw>(?:{_COMPACT_ONE}|{_ONE}{_IVM})\s+(?P<law>{_LAW}))", re.IGNORECASE)

# German runs on capitalised nouns, and the pattern must END on a law — so when a
# reference carries no abbreviation at all, backtracking hands back whatever word sits
# where a law would ("§ 100 Absatz 1 Satz 1" → "Satz 1"; "§ 100a Rn" → "Rn"; a statute
# section heading "§ 4 Geltungsbereich" → "Geltungsbereich"). Two guards separate a real
# abbreviation from an ordinary word:
#
#   1. an abbreviation carries at least TWO capitals (BGB, ZPO, MarkenG, TzBfG, SGB V,
#      AEUV, EMRK); a German noun and the structural vocabulary carry exactly one;
#   2. an explicit stop-list for the apparatus that passes (1) — the Halbsatz "HS",
#      "aaO", and the law-report series a citation trails off into (NJW, BGHZ, SozR…).
_LAW_STOPWORDS = {
    # structural / pinpoint vocabulary
    "rn", "rn.", "rdn", "rdnr", "rdnrn", "rz", "ziff", "ziffer", "satz", "saetze",
    "abs", "absatz", "nr", "nrn", "nummer", "buchst", "buchstabe", "halbs", "hs",
    "alt", "alternative", "var", "variante", "unterabs", "unterabsatz", "spiegelstrich",
    "abschn", "abschnitt", "kap", "kapitel", "teil", "anl", "anlage", "anh", "fn",
    "art", "artikel", "s", "seite", "ff", "f",
    # citation apparatus
    "aao", "mwn", "vgl", "rspr", "juris", "beckrs", "az",
    # law-report series a reference can trail into
    "njw", "nza", "nvwz", "njoz", "nzs", "nzm", "nzi", "grur", "mdr", "wm", "zip",
    "dstr", "bghz", "bghst", "bverfge", "bage", "bfhe", "bsge", "bverwge", "sozr",
    "euzw", "eugrz", "versr", "dvbl", "bb", "db",
}


def _is_law_abbreviation(law: str) -> bool:
    """Does this token read as a German law abbreviation rather than an ordinary word?

    See ``_LAW_STOPWORDS``: two capitals or more, and not a piece of citation apparatus."""
    stem = (law or "").split()[0] if (law or "").strip() else ""
    folded = re.sub(r"[^a-z]+", "", _fold_law(stem))
    if not stem or folded in _LAW_STOPWORDS:
        return False
    return sum(1 for ch in stem if ch.isupper()) >= 2


def _fold_law(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in folded if not unicodedata.combining(c)).casefold()


def _expand_compact(raw: str) -> str:
    """§ 19 IV 1 / § 19 (4) 1 → the explicit form bundesrecht canonicalises."""
    def _arabic(value: str) -> str:
        if value.isdigit():
            return value
        total, previous = 0, 0
        for ch in reversed(value.upper()):
            current = {"I": 1, "V": 5, "X": 10}[ch]
            total += current if current >= previous else -current
            previous = current
        return str(total)

    return re.sub(
        r"^((?:§|Art\.)\s*\d+[a-z]?)\s+(?:\((\d+)\)|([IVX]+))\s+(\d+)\s+",
        lambda m: f"{m.group(1)} Abs. {_arabic(m.group(2) or m.group(3))} Satz {m.group(4)} ",
        raw, flags=re.IGNORECASE,
    )


def _canonical_parts(canonical: str) -> tuple[str, str] | None:
    m = re.match(r"^(?P<prefix>§|Art\.)\s+(?P<body>.+?)\s+(?P<law>[A-ZÄÖÜ][\wÄÖÜäöüß]*(?:\s+\d+)?)$",
                 canonical)
    if not m:
        return None
    return m.group("law"), f"{m.group('prefix')} {m.group('body')}"


def law_citations(text: str) -> list[Citation]:
    found: list[Citation] = []
    for match in LAW_REFERENCE_RE.finditer(text):
        raw = match.group("raw")
        # CEDH is the French abbreviation for the European Convention/Court, not a
        # German statute abbreviation. In French judgments ``§ 95, CEDH 19`` is a
        # Strasbourg paragraph/report marker; treating it as de/gesetz/cedh19 creates
        # a cross-jurisdiction phantom node. German texts use EMRK for the Convention.
        if re.match(r"CEDH\b", match.group("law"), re.IGNORECASE):
            continue
        # …and the same for the ordinary German word backtracking leaves behind when a
        # reference carries no abbreviation at all (see _is_law_abbreviation).
        if not _is_law_abbreviation(match.group("law")):
            continue
        try:
            canonical_refs = normalise(_expand_compact(raw))
        except (ValueError, TypeError):
            continue
        for canonical in dict.fromkeys(canonical_refs):
            parts = _canonical_parts(canonical)
            if not parts:
                continue
            law, pinpoint = parts
            found.append(Citation(
                raw=raw, entity_kind="act", candidate_id=law_id(law), pinpoint=pinpoint,
                char_start=match.start(), char_end=match.end(), method="de_law_reference",
                confidence=1.0,
            ))
    return found


_COURT = r"BVerfG|BGH|BAG|BFH|BSG|BVerwG|BPatG|EuGH|OLG\s+[A-ZÄÖÜ][\wÄÖÜäöüß-]+|LG\s+[A-ZÄÖÜ][\wÄÖÜäöüß-]+"
# Registers longest-first, so "StB" isn't cut short to "B" and "AnwZ" to "AR".
_REGISTER = (r"AnwZ|NotZ|EnZR|EnVR|BvR|BvL|BvF|BvQ|BVR|AZR|ABR|KZR|KVR|StR|StB|"
             r"ZR|ZB|ZA|AR|CN|R|C|B|W|U|L|K|O")
# The senate prefix is a number or a Roman numeral, and the Roman one may carry a
# lower-case letter — the BGH's VIa, IVa and XIa senates. Without it "BGH VIa ZR 335/21"
# lost its senate and became de:case:BGH:ZR335/21, merging VIa and IVa ZR 335/21 into one
# node. The lookbehind keeps a register letter from being read off the tail of a word:
# "SozR 4-1500" (the social-law report series) was minting de:case:BSG:ZR4-1500, the
# most-cited German "case" in the corpus.
_DOCKET = (rf"(?<![A-Za-zÄÖÜäöüß])(?:(?:\d+|[IVX]+[a-z]?)\s+)?(?:{_REGISTER})"
           r"\s+\d{1,6}(?:[./-]\d{1,4})+")
# What may NOT appear between the court and the docket. A German judgment's header lists
# the courts below it ("BGH … Beschluss vorgehend KG Berlin, … Az: 10 U 54/19"), so a
# window that steps over another court's name attributes ITS docket to the first court.
_OTHER_COURT = (r"\b(?:OLG|LG|AG|KG|OVG|VG|VGH|LSG|LAG|SG|FG|ArbG|BVerfG|BGH|BAG|BFH|"
                r"BSG|BVerwG|BPatG|EuGH|EGMR)\b|vorgehend|nachgehend|Vorinstanz")
CASE_REFERENCE_RE = re.compile(
    rf"\b(?P<court>{_COURT})\b(?:(?!{_OTHER_COURT})[^;\n]){{0,80}}?(?P<docket>{_DOCKET})\b"
    rf"(?:\s*,?\s*[Rr]n\.?\s*(?P<rn>\d+(?:\s*(?:ff?\.|[-–,])\s*\d*)?))?")


def case_citations(text: str) -> list[Citation]:
    return [Citation(
        raw=m.group(0).strip(), entity_kind="case",
        candidate_id=case_alias(m.group("court"), m.group("docket")),
        pinpoint=f"Rn. {m.group('rn')}" if m.group("rn") else None,
        char_start=m.start(), char_end=m.end(), method="de_case_reference", confidence=0.95,
    ) for m in CASE_REFERENCE_RE.finditer(text)]


def german_citations(text: str) -> list[Citation]:
    return law_citations(text) + case_citations(text)
