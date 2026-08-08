"""Re-case a SHOUTY case name — "CASE OF TSANOVA-GECHEVA v. BULGARIA" — without
destroying anything the register actually told us.

HUDOC publishes ``docname`` in upper case, so 38,191 Strasbourg titles in the corpus
read as shouting. The obvious fix, ``str.title()`` over the whole string, is wrong in
every interesting way: it lower-cases the second half of "TSANOVA-GECHEVA", turns the
initials "A.B." into "A.b.", capitalises the "the" in "the United Kingdom" that the
Court itself keeps lower-case, mangles "GmbH" and "S.R.L.", flattens the HUDOC document
codes ("HEJUD"), and — because Python has no Turkish locale — renders "ÇETİNKAYA" as
"Çeti̇nkaya", with a stray COMBINING DOT ABOVE.

Two ideas keep this honest:

**Only shouty tokens are touched.** A token that already contains a lower-case letter is
returned byte-for-byte. That is what makes the function safe to run over the whole
collection rather than a hand-picked subset: HUDOC titles mix cased and uncased material
freely, and the cased material is always the part a human wrote —

    MARKOVIC v SERBIA - 70661/14 (Judgment : Violation of Article 6 …) French Text
    Danilo ZORKO v Slovenia - 24431/10
    CASE OF DE SOUZA RIBEIRO v. FRANCE - [Armenian Translation] by the COE …

— so the parenthetical descriptor, the translation note and the given name all survive
untouched while the party and the State are re-cased.

**Position decides the minor words.** "the" is lower-case in the respondent State ("v.
the United Kingdom") but capitalised when it opens a party ("The Sunday Times v. …"), and
the same is true of the nobiliary particles: "Van Pelt", but "De Geouffre de la
Pradelle". One rule covers both — a minor word is capitalised only when it opens a
segment, and a segment opens at the start of the title, after "Case of"/"Affaire", and
after the "and"/"et" that joins two applicants.

What this cannot do is invent information. HUDOC's docname is ASCII-folded for a large
part of the collection ("GORMUS", "JUHASZ", "GAVRIC"), so the diacritics of "Görmüş",
"Juhász" and "Gavrić" are not recoverable from the title — this function will not
pretend otherwise, and leaves those spellings as it finds them.
"""

from __future__ import annotations

import re

# HUDOC's own document-type codes, which ride on the end of a docname after " - ".
# They are identifiers, not words: "Hejud" would be a small lie in 194 titles.
_HUDOC_CODES = frozenset({
    "HEJUD", "HEDEC", "HECOM", "HFJUD", "HFDEC", "HFCOM", "HEADV", "CLIN",
})

# Institutional acronyms that turn up inside the otherwise-cased tail of a docname —
# "[Armenian Translation] by the COE Human Rights Trust Fund". They are the one kind of
# shouty token in that region that is shouting on purpose.
_ACRONYMS = frozenset({
    "COE", "ECHR", "CEDH", "EU", "UN", "UNHCR", "NATO", "OSCE", "CPT", "ICJ", "ICC",
    "USSR", "KGB", "FYROM", "BBC", "RTL", "TV", "NGO", "GRECO", "ECRI",
})

# States whose canonical spelling cannot be reached by casing the shouty form. Almost
# every respondent State can — "RUSSIA" → "Russia", "SUÈDE" → "Suède" — because the
# diacritics survive in the source. Türkiye is the exception: HUDOC writes both "TÜRKİYE"
# (which cases correctly once the dotted İ is handled) and a flat-ASCII "TURKIYE", and
# only a table can put the umlaut back.
_STATES = {
    "turkiye": "Türkiye",
}
_STATES_RE = re.compile(r"\b(?:%s)\b" % "|".join(sorted(_STATES, key=len, reverse=True)),
                        re.IGNORECASE)

# Spelled-out counts in the committee-resolution titles: "AND THIRTEEN OTHER CASES
# AGAINST THE UNITED KINGDOM". They are quantities, not the start of a party.
_NUMBER_WORDS = frozenset({
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "hundred", "thousand",
})

# The elided articles French and Italian names carry. "D'Amico" opens a party and keeps
# its capital; the "d'" of "…et d'édition" is mid-name and does not.
_ELISIONS = frozenset({"d", "l", "dell", "nell", "all", "sull"})

# Legal/company forms, in the spelling their own jurisdiction uses.
#
# These re-case the letters and NOTHING else. An earlier draft mapped "AS" to "A.S." and
# "INC" to "Inc.", which reads better in isolation and is a lie about the record: it
# turned Norwegian "TYVIK AS" into "Tyvik A.S." and, because the token's own full stop is
# re-attached afterwards, "WIKIMEDIA FOUNDATION, INC." into "Inc..". A dotted form that
# the register actually wrote ("S.R.L.", "A.S.") is preserved by the abbreviation rule
# above instead, so nothing here needs to add punctuation of its own.
_LEGAL_FORMS = {
    "GMBH": "GmbH", "MBH": "mbH", "LTD": "Ltd", "PLC": "plc", "LLC": "LLC",
    "AG": "AG", "KG": "KG", "SA": "SA", "SARL": "SARL", "SRL": "SRL",
    "SPA": "SpA", "AS": "AS", "ASA": "ASA", "NV": "NV", "BV": "BV",
    "OY": "Oy", "AB": "AB", "APS": "ApS", "DOO": "DOO", "SP": "SP",
    "INC": "Inc", "CO": "Co", "SC": "SC", "SE": "SE", "KFT": "Kft", "ZRT": "Zrt",
}

# Words that stay lower-case inside a name. Split by language only where the Court's own
# usage differs: English "and Others", French "et autres".
_MINOR = frozenset({
    # English structure
    "of", "and", "the", "against", "or", "other", "case", "cases", "in", "at", "on",
    "for", "to", "with", "former", "no", "nos", "versus", "vs",
    # French / Italian / Spanish / Portuguese structure
    "et", "autres", "contre", "de", "des", "du", "la", "le", "les", "l", "d",
    "da", "das", "do", "dos", "di", "del", "della", "dei", "degli", "e", "y",
    # Germanic / Nordic / Dutch particles
    "van", "von", "der", "den", "ten", "ter", "af", "av", "zu", "vom",
    # other particles seen in Strasbourg party names
    "el", "al", "bin", "ibn", "ben", "abu",
}) | _NUMBER_WORDS

# Minor words that must NEVER be promoted by the segment rule — they are structural
# apparatus, never the first word of somebody's name. Without this, "and 1 OTHER CASE"
# comes back as "and 1 Other case".
_NEVER_PROMOTED = frozenset({
    "of", "and", "et", "against", "contre", "other", "case", "cases", "former", "or",
    "versus", "vs", "no", "nos",
}) | _NUMBER_WORDS

# A segment is a fresh naming context: the start of the title, the party after
# "Case of"/"Affaire", and the applicant after the "and"/"et" that joins two of them.
_SEGMENT_OPENERS = frozenset({"of", "affaire", "and", "et", "case"})

# Words whose cased form is fixed regardless of position.
_FIXED = {
    "others": "Others",     # "X and Others v. Y" — the Court capitalises it
    "autres": "autres",     # "X et autres c. Y" — and does not, in French
    "affaire": "Affaire",
}

# "A.B.", and the hyphenated form a double-barrelled anonymisation takes: "A.D.-K.".
_INITIALS_RE = re.compile(r"^(?:[A-Z]\.)+(?:[-‐-―](?:[A-Z]\.)+)*$")
_DOTTED_ABBREV_RE = re.compile(r"^[A-Z]{1,3}(?:\.[A-Z]{1,3})+\.?$")
_HAS_LOWER_RE = re.compile(r"[a-zà-öø-ÿœ]")
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
# A French/Italian elided article welded onto the front of a shouty word: "contre
# l'ITALIE". The lower-case "l" is enough to make the token look cased already, so the
# State behind it was the one thing the whole-token gate let through untouched.
_ELIDED_PREFIX_RE = re.compile(r"^(\W*[a-zà-öø-ÿ]{1,4}['’])(.+)$")


def _lower(text: str) -> str:
    """``str.lower`` that does not manufacture a combining dot.

    Python lower-cases U+0130 LATIN CAPITAL LETTER I WITH DOT ABOVE to "i" + U+0307,
    which is correct only if you are about to re-compose it. Turkish party names and the
    respondent State "TÜRKİYE" go through here 139 times in this collection alone, and
    the artefact is visible in the rendered title."""
    return text.replace("İ", "i").lower()


def _cap(word: str) -> str:
    """Capitalise one alphabetic run, leaving the rest of the casing lowered."""
    if not word:
        return word
    lowered = _lower(word)
    # A word that STARTS with the Turkish dotted capital keeps it: "İŞ" is "İş", never
    # "Iş" (that is a different letter) and never "i̇ş" (that is a bug).
    if word[0] == "İ":
        return "İ" + lowered[1:]
    return lowered[0].upper() + lowered[1:]


def _cap_compound(word: str, *, segment_start: bool = True) -> str:
    """Capitalise across the joiners a surname is built from.

    "TSANOVA-GECHEVA" is two names, "O'KEEFFE" and "D'AMICO" are a particle plus a name,
    and "MCELHINNEY" is the Gaelic prefix — each part needs its own capital, which is
    exactly what a single ``capitalize()`` over the token cannot give."""
    out = re.split(r"([-‐-―'’/])", word)
    parts = []
    for i, part in enumerate(out):
        if i % 2:                       # the separator itself
            parts.append(part)
            continue
        # A leading elided article mid-name stays lower ("…et d'Édition"); the same
        # letters opening a party do not ("D'Amico v. Italy").
        if (i == 0 and len(out) > 1 and not segment_start
                and _lower(part) in _ELISIONS):
            parts.append(_lower(part))
            continue
        capped = _cap(part)
        # "Mc" is a prefix, not a syllable: McElhinney, McFarlane, McCann. "Mac" is
        # deliberately left alone — Mackay and MacKay are both real spellings and the
        # title gives no way to tell which one this is.
        if len(capped) > 2 and capped[:2] == "Mc":
            capped = "Mc" + capped[2].upper() + capped[3:]
        parts.append(capped)
    return "".join(parts)


def _recase_token(token: str, *, segment_start: bool, title_start: bool,
                  after_count: bool = False) -> str:
    """One shouty token → its cased form. ``token`` may carry punctuation on either side."""
    lead, core, trail = re.match(r"^(\W*)(.*?)(\W*)$", token, re.DOTALL).groups()
    if not core:
        return token

    # The lookup key must account for EVERY letter in the token, or a table entry can
    # swallow the rest of it: keying on ASCII letters alone turned the Turkish surname
    # "ABİ" into the company form "AB", and keying on letters alone turned the Danish
    # "A/S" into "AS". Only a token that is nothing but letters may match these tables;
    # a dotted form the register wrote itself ("S.A.", "S.R.L.") is preserved below.
    key = core.upper() if core.isalpha() else ""

    if key and (key in _HUDOC_CODES or key in _ACRONYMS):
        return token
    # "A.B.", "G.L.C.", "X." — initials are the applicant's anonymity, not a word.
    # Matched against core+trail: the token's own final stop is split off as trailing
    # punctuation, and "A.D.-K" without it is not recognisably a set of initials.
    if _INITIALS_RE.match(core + trail) or _DOTTED_ABBREV_RE.match(core + trail):
        return token
    if key in _LEGAL_FORMS:
        return f"{lead}{_LEGAL_FORMS[key]}{trail}"

    lowered = _lower(core)
    # "X and Others v. Y" is the Court's own styling, but a COUNT in front turns it back
    # into an ordinary quantity — "Gika and five others", not "and Five Others".
    if lowered == "others" and after_count:
        return f"{lead}others{trail}"
    if lowered in _FIXED:
        return f"{lead}{_FIXED[lowered]}{trail}"
    if lowered in _MINOR:
        promote = title_start or (segment_start and lowered not in _NEVER_PROMOTED)
        return f"{lead}{_cap(core) if promote else lowered}{trail}"
    return f"{lead}{_cap_compound(core, segment_start=segment_start or title_start)}{trail}"


def titlecase_case_name(title: str | None) -> str | None:
    """Re-case the upper-case runs of a case name; leave everything else untouched.

    Idempotent, and a no-op on a title that carries no shouty token — so it is safe to
    apply on every import and to re-run over the whole collection."""
    if not title or not title.strip():
        return title

    pieces = re.split(r"(\s+)", title)
    out: list[str] = []
    segment_start = True
    seen_word = False
    after_count = False

    for piece in pieces:
        if not piece or piece.isspace():
            out.append(piece)
            continue

        has_letter = bool(_HAS_LETTER_RE.search(piece))
        shouty = has_letter and not _HAS_LOWER_RE.search(piece)
        elided = None if shouty else _ELIDED_PREFIX_RE.match(piece)
        if elided and not _HAS_LOWER_RE.search(elided.group(2)):
            # keep the article exactly as written, re-case only what it is glued to
            out.append(elided.group(1) + _recase_token(
                elided.group(2), segment_start=segment_start,
                title_start=not seen_word, after_count=after_count))
        elif shouty:
            out.append(_recase_token(piece, segment_start=segment_start,
                                     title_start=not seen_word,
                                     after_count=after_count))
        else:
            out.append(piece)

        bare_word = _lower(re.sub(r"[^A-Za-zÀ-ɏ]", "", piece))
        after_count = bool(re.fullmatch(r"\d+", piece.strip("()[[],.")) ) or (
            bare_word in _NUMBER_WORDS)

        # The next token opens a segment if this one closed a naming context: the
        # "of" of "Case of", the "and" joining two applicants, or a bare separator
        # ("v.", "c.", "—"). A punctuation-only piece is transparent.
        if has_letter:
            seen_word = True
            bare = re.sub(r"[^A-Za-zÀ-ɏ]", "", piece)
            segment_start = _lower(bare) in _SEGMENT_OPENERS
    return _STATES_RE.sub(lambda m: _STATES[m.group(0).lower()], "".join(out))
