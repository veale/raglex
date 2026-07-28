"""Who decided a case, and who argued it — read off the judgment's own first page.

A reader opening a judgment wants the bench before anything else ("If poss would be good
to have the judge's name at the top somewhere (and counsel/representation) if this can be
easily pulled but judge is most important" — the feedback that prompted this). The corpus
holds none of it as metadata: a Find Case Law document's ``meta_json`` carries only the
BAILII provenance keys, and the Akoma Ntoso the adapter parses drops the header block.

But every UK judgment prints it. A sample of 120 held judgments found a ``Before:`` or
``Coram`` line in 112 of them, and the representation block in most of those. So this
parses the intituling — the block above "JUDGMENT" — rather than adding a data source:

    Before:
    LORD JUSTICE MUMMERY, LORD JUSTICE JONATHAN PARKER and LORD JUSTICE LLOYD
    Between: …
    Representation:
    Mr X QC (instructed by Y) for the Appellant

Deliberately conservative. Every rule below fires only inside the first few thousand
characters, only on an explicit label, and returns nothing rather than a guess: a wrong
judge printed under a case name is worse than no judge at all.
"""

from __future__ import annotations

import re

# The block is always near the top; beyond this we are into the reasoning.
_HEAD_CHARS = 6000

# BAILII/Find Case Law set the intituling labels LETTER-SPACED — "B e f o r e :",
# "B e t w e e n" — so a plain /^before\b/ finds nothing at all on a real judgment (it
# matched 1 of 60, and that one was a sentence beginning "Before the jury…"). Every label
# below is therefore matched letter-spaced-or-not, and the same-line form REQUIRES a colon
# so prose can never be mistaken for a label.
def _spaced(word: str) -> str:
    return r"[ \t]*".join(word)


_BEFORE_RE = re.compile(
    rf"(?im)^[ \t]*(?:{_spaced('before')}|{_spaced('coram')}|{_spaced('present')})[ \t]*:?[ \t]*$"
    rf"|^[ \t]*(?:before|coram|present)[ \t]*:[ \t]*(?P<rest>\S.*)$")
# Courts that don't use a "Before:" block at all, each naming its bench its own way:
#   House of Lords   "The Appellate Committee comprised: Lord Bingham of Cornhill …"
#   Privy Council    "The Board comprised: …" / "Present at the hearing: …"
#   Court of Session "OPINION OF LORD DOHERTY" / "OPINION OF THE COURT delivered by …"
# Tested across the UK courts the corpus holds: without these, ukhl and scotcs parsed 0.
_COMMITTEE_RE = re.compile(
    r"(?im)^[ \t]*(?:the\s+)?(?:appellate\s+committee|board|committee)"
    r"(?:\s+(?:comprised|consisted\s+of|was\s+composed\s+of))?[ \t]*:?[ \t]*(?P<rest>.*)$")
_OPINION_OF_RE = re.compile(
    r"(?im)^[ \t]*opinion\s+of\s+(?:the\s+court\s+delivered\s+by\s+)?(?P<rest>[^\n]{3,90})$")
# where the bench block ends: the rule the template draws, the next labelled block, or the
# judgment proper
_BLOCK_END_RE = re.compile(
    rf"(?im)^[ \t]*(?:_{{4,}}|-{{4,}}|{_spaced('between')}|between\b|representation\b|"
    rf"appearances\b|counsel\b|hearing\s+dates?\b|judgment\b|approved\s+judgment\b|"
    rf"html\s+version\b|crown\s+copyright\b)")
# a judicial title, which is what makes a line a bench line rather than prose
_JUDICIAL_RE = re.compile(
    r"(?i)\b(?:l\.?j\.?|lj|jj?\.?|kc|qc|cj|mr|mrs|ms|sir|dame|lord|lady|master|"
    r"judge|justice|president|chancellor|recorder|chief|honou?rable|baron(?:ess)?)\b")
_SPLIT_RE = re.compile(r"(?i)\s*(?:,|;|\band\b|\&)\s*")
# "Mr Smith QC (instructed by Foo LLP) for the Appellant"
_REPRESENTATION_RE = re.compile(
    r"(?im)^[ \t]*(?:representation|appearances|counsel)\b[ \t]*:?[ \t]*$")
_FOR_PARTY_RE = re.compile(r"(?i)\bfor\s+the\s+[a-z0-9(). /-]{2,40}$")


def _clean(value: str) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


# lines that sit inside the bench block but name a ROLE, a venue or a date rather than a
# person ("Deputy President", "Sitting at: Royal Courts of Justice", "Heard on…")
_NOT_A_NAME_RE = re.compile(
    r"(?i)^(?:deputy\s+president|president|vice[- ]president|chair(?:man)?|sitting\b.*|"
    r"heard\b.*|hearing\b.*|dated?\b.*|between\b.*|and|&|the\s+court|in\s+public)$")


def _looks_judicial(line: str) -> bool:
    line = _clean(line)
    if not (3 < len(line) < 160):
        return False
    if _NOT_A_NAME_RE.match(line):
        return False
    if not _JUDICIAL_RE.search(line):
        return False
    # a sentence is not a bench line
    return not line.endswith(".") or line.isupper() or bool(re.search(r"(?i)\b[JL]\.?$", line))


# How a judge is NAMED on the page vs how a lawyer writes it. The page shouts an office
# ("LORD JUSTICE CHADWICK"); a citation says "Chadwick LJ". Offices with no surname (the
# Lord Chief Justice, the Master of the Rolls) keep their title.
_TITLE_FORMS = (
    (re.compile(r"(?i)^(?:the\s+)?(?:rt\s+hon\s+)?lord\s+justice\s+(?P<n>.+)$"), "{n} LJ"),
    (re.compile(r"(?i)^(?:the\s+)?(?:rt\s+hon\s+)?lady\s+justice\s+(?P<n>.+)$"), "{n} LJ"),
    (re.compile(r"(?i)^(?:the\s+)?(?:mr|mrs|ms)\s+justice\s+(?P<n>.+)$"), "{n} J"),
    (re.compile(r"(?i)^(?:the\s+)?(?:his|her)\s+honou?r\s+judge\s+(?P<n>.+)$"), "HHJ {n}"),
    (re.compile(r"(?i)^(?:the\s+)?judge\s+(?P<n>.+)$"), "Judge {n}"),
)
# post-nominals and sitting notes that are not part of the name
_STRIP_RE = re.compile(
    r"(?i)\s*(?:\((?:sitting|as\s+a\s+judge)[^)]*\)?|,?\s*\b(?:dbe|cbe|obe|mbe|pc)\b)\s*")


# post-nominals and ranks that stay in capitals through title-casing
_POST_NOMINALS = {"QC", "KC", "DBE", "CBE", "OBE", "MBE", "PC", "CJ", "LJ", "J", "JJ",
                  "SC", "VC", "MR", "DL", "TD", "RD", "FRS"}


def _titlecase_name(name: str) -> str:
    """"McCOMBE" → "McCombe", "MOORE-BICK" → "Moore-Bick", leaving a normally-cased name
    alone. SHOUTY is judged by proportion, not by ``isupper()``: "McCOMBE" has a lower-case
    letter in it and would otherwise pass through still shouting."""
    letters = [c for c in name if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) < 0.6 * len(letters):
        return name
    out = []
    for word in name.split():
        if word.upper() in _POST_NOMINALS:
            out.append(word.upper())
            continue
        parts = [p.capitalize() for p in word.split("-")]
        w = "-".join(parts)
        for prefix in ("Mc", "Mac", "O'"):
            if w.upper().startswith(prefix.upper()) and len(w) > len(prefix):
                w = prefix + w[len(prefix):].capitalize()
        out.append(w)
    return " ".join(out)


def standardise_judge(raw: str) -> str:
    """One judge, as a lawyer would write the name: "LORD JUSTICE CHADWICK" → "Chadwick LJ",
    "MR JUSTICE COTTER" → "Cotter J", "SIR JULIAN FLAUX (SITTING IN RETIREMENT)" → "Sir
    Julian Flaux". An office with no surname keeps its title."""
    name = _clean(_STRIP_RE.sub(" ", raw or "")).strip(" ,;-–—")
    if not name:
        return ""
    for pattern, form in _TITLE_FORMS:
        m = pattern.match(name)
        if m:
            return form.format(n=_titlecase_name(_clean(m.group("n"))))
    name = _titlecase_name(name)
    # "THE MASTER OF THE ROLLS" → "The Master of the Rolls": the connectives go down, the
    # first word stays up.
    name = re.sub(r"(?i)\b(of|the|and|in)\b", lambda m: m.group(1).lower(), name).strip()
    return name[:1].upper() + name[1:]


def parse_coram(text: str | None) -> list[str]:
    """The judges named under ``Before:`` / ``Coram``, one per entry.

    Returns [] when the block is absent or doesn't look like a bench — never a guess.
    """
    head = (text or "")[:_HEAD_CHARS]
    m = _BEFORE_RE.search(head) or _COMMITTEE_RE.search(head) or _OPINION_OF_RE.search(head)
    if not m:
        return []
    lines: list[str] = []
    if m.groupdict().get("rest") and _clean(m.group("rest")):
        lines.append(m.group("rest"))
    # …otherwise the names are on the lines beneath, one per line, blank-separated,
    # closed by the template's rule
    blanks = 0
    for raw in head[m.end():].split("\n")[:24]:
        if _BLOCK_END_RE.match(raw):
            break
        if not _clean(raw):
            blanks += 1
            if blanks >= 4 and lines:
                break
            continue
        blanks = 0
        lines.append(raw)
    judges: list[str] = []
    for line in lines:
        for part in _SPLIT_RE.split(_clean(line)):
            part = _clean(part).strip("-–—:;,")
            if not part or not _looks_judicial(part):
                continue
            part = re.sub(r"(?i)\s*\((?:the\s+)?(?:president|chair(?:man)?)\)\s*$",
                          " (President)", part).strip()
            if part.lower() not in {j.lower() for j in judges}:
                judges.append(part)
    # a parenthetical line names the holder of the office above it ("THE LORD CHIEF JUSTICE
    # OF ENGLAND" / "(Lord Thomas of Cwmgiedd)") — one judge, not two
    merged: list[str] = []
    for entry in judges[:9]:           # a bench, not everyone the page names
        if entry.startswith("(") and merged:
            merged[-1] = f"{merged[-1]} {entry}"
        else:
            merged.append(entry)
    return [x for x in (standardise_judge(m) for m in merged) if x]


def parse_representation(text: str | None) -> list[str]:
    """The counsel lines under ``Representation:`` / ``Appearances`` — "Mr X KC
    (instructed by Y) for the Appellant", one entry per party."""
    head = (text or "")[:_HEAD_CHARS]
    m = _REPRESENTATION_RE.search(head)
    out: list[str] = []
    if m:
        for raw in head[m.end():].split("\n")[:10]:
            line = _clean(raw)
            if not line:
                if out:
                    break
                continue
            if re.match(r"(?i)^(?:judgment|approved judgment|hearing date)", line):
                break
            out.append(line)
    else:
        # no label — but the "… for the Appellant" form is unmistakable on its own
        for raw in head.split("\n"):
            line = _clean(raw)
            if len(line) > 12 and _FOR_PARTY_RE.search(line):
                out.append(line)
    seen: set[str] = set()
    uniq = [x for x in out if not (x.lower() in seen or seen.add(x.lower()))]
    return uniq[:8]


def parse_intituling(text: str | None) -> dict:
    """``{"coram": [...], "representation": [...]}`` — omitting whichever isn't there."""
    out: dict = {}
    coram = parse_coram(text)
    if coram:
        out["coram"] = coram
    rep = parse_representation(text)
    if rep:
        out["representation"] = rep
    return out
