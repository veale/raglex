"""A Swedish judgment as it comes out of the court's own PDF.

Domstolsverket publishes the courts' signed decisions as PDFs, and the whole
``DOM_ELLER_BESLUT`` layer — every judgment recovered by expanding a publication group,
plus the 754 that were listed all along — arrives this way. Extracted naively it is
unreadable: the first two hundred characters of a Supreme Court decision are the
switchboard number and the opening hours.

```
Dok.Id 364143 Besöksadress
Riddarhustorget 8
Telefon
08-561 666 00
...
Sida 1 (8)
HÖGSTA DOMSTOLENS
BESLUT
```

There are two house styles and this parser reads both.

**Högsta domstolen and Högsta förvaltningsdomstolen** open with a contact block, then
``Sida N (M)`` and the court's formula. Every later page repeats ``Sida N (M)`` / ``HÖGSTA
DOMSTOLEN BESLUT Ö 4337-25`` / ``Dok.Id 364143``.

**The Svea hovrätt divisions** — Mark- och miljööverdomstolen, Patent- och
marknadsöverdomstolen, the hyresrättsliga avgöranden — open with ``Sid N (M)``, the court
and rotel, ``DOM``/``BESLUT``, the date, ``Mål nr``, and a ``Postadress Besöksadress
Telefon Telefax Expeditionstid`` table. Their running header is ``SVEA HOVRÄTT DOM F
8748-25`` over the division name.

Three things then have to happen, and each is the difference between a document a reader
can use and a wall of text:

**The furniture goes.** Page headers are cut wherever they fall — mid-sentence, which is
where they usually fall, since a page break does not wait for a full stop. The contact
block is only cut in the *header* zone: further down the page, ``Box 2066`` under
``PARTER`` is a party's address and belongs to the document.

**The lines are rejoined.** A PDF has no paragraphs, only lines, and a citation broken
across two of them ("NJA 1970\\ns. 274") matches no grammar. Lines are joined into
sentences, respecting Swedish's line-end hyphenation — ``rätte-\\ngångskostnad`` is one
word, while ``mark-\\noch miljödomstolen`` is a compound coordination and keeps its
hyphen and its space.

**The outline is recovered.** The ALL-CAPS lines are the court's own sections (``SAKEN``,
``ÖVERKLAGAT AVGÖRANDE``, ``SKÄL``, ``DOMSLUT``), and the short unpunctuated Title-case
lines beneath ``SKÄL`` are its subheadings ("Bakgrund", "Frågan i målet", "Tillämplig lag
i processuella frågor"). That gives the same two-level outline the HTML ``innehall`` gives
for a referat, so both renditions of a case read alike.

Numbered reasons are labelled where the extraction preserved the number. Often it did
not: Högsta domstolen sets the paragraph number in its own text frame, and the extractor
drops it, leaving the sentence to begin with a stray space. Nothing here can invent it
back, so the label is only applied when the number is really there.
"""

from __future__ import annotations

import re

from ..core.segmentation import assemble
from .base import ParsedDoc, register

#: ``Sida 3 (8)`` (Högsta domstolen) / ``Sid 4`` (the Svea divisions). The page break, and
#: the anchor from which the rest of the running header is measured.
#: An authority annexed to a judgment numbers its pages its own way: Konkurrensverket
#: closes the header line with a bare ``2 (165)``. Accepting that as a page marker is what
#: opens the header window on the 165-page decision a competition appeal carries with it.
_PAGE_RE = re.compile(
    r"^\s*Sid(?:a)?\.?\s+\d{1,4}\s*(?:\(\s*\d{1,4}\s*\))?\s*$"
    r"|\d{1,4}\s*\(\s*\d{1,4}\s*\)\s*$", re.I)
#: The document id the courts stamp on every page.
_DOKID_RE = re.compile(r"^\s*Dok\.?\s?Id[\s:]*\d+\s*$", re.I)
#: ``HÖGSTA DOMSTOLEN BESLUT Ö 4337-25`` / ``SVEA HOVRÄTT DOM F 8748-25`` — the running
#: header proper: a court in caps, the decision word, and the docket.
_RUNNING_RE = re.compile(
    r"^\s*[A-ZÅÄÖ][A-ZÅÄÖ\-\s.]{4,60}?\s+(?:DOM|BESLUT|PROTOKOLL|SLUTLIGT\s+BESLUT)"
    r"(?:\s+\S{1,6}\s*\d{1,6}-\d{2,4})?\s*$")
#: A word that can only be part of the court's own letterhead. Searched, not matched: the
#: courts set several of these on one line ("Dok.Id 364143 Besöksadress", "Postadress
#: Besöksadress Telefon Telefax Expeditionstid").
_CONTACT_OPENS = re.compile(
    r"(?:Postadress|Bes(?:ö|o)ksadress|Telefax|Expeditionstid|Öppettider|Webbplats|"
    r"Telefon\b|E-?post\b)", re.I)
#: The values that follow it — a phone number, a postcode and city, a box, opening hours,
#: a web address, an e-mail. Each on its own line, as the PDF sets them.
_CONTACT_VALUE = re.compile(
    r"^(?:www\.\S+|\S+@\S+\.\w+|[\d\s\-()+]{7,}|\d{3}\s?\d{2}\s+\S.*|Box\s+\d+"
    r"|m(?:å|a)ndag\s*[–-]\s*fredag|\d{2}[:.]\d{2}\s*[–-]\s*\d{2}[:.]\d{2}"
    r"|[A-ZÅÄÖ][\wåäö]*(?:gatan|vägen|torget|gata|väg|torg)\s+\d+.*)$", re.I)
#: Above this share of blank lines, the blanks are the extractor's line separator rather
#: than the document's paragraph breaks, and must be dropped before the lines are
#: rejoined. Born-digital extraction of the same court's PDFs comes out both ways —
#: Högsta förvaltningsdomstolen's arrives at one blank line in two (0.50) and Högsta
#: domstolen's at one in fifty (0.02) — and read literally the first kind never joins a
#: single line, so every citation stays broken across the wrap that produced it.
_DOUBLE_SPACED = 0.35
#: How many lines a letterhead run may swallow before it is assumed to have gone wrong.
_CONTACT_RUN = 20
#: A line short enough, and unpunctuated enough, to still be part of a letterhead.
_CONTACT_MAX = 44
#: The typographic rules a Swedish judgment sets between its top matter and its order.
_RULE_RE = re.compile(r"^\s*[_—–-]{3,}\s*$")
#: The e-signature trailer, and the division/rotel line that follows a running header.
_TRAILER_RE = re.compile(r"^\s*Avg(?:ö|o)randet\s+är\s+elektroniskt\s+undertecknat\s*$", re.I)

#: A section heading: the court's own ALL-CAPS zone names.
_CAPS_RE = re.compile(r"^[^a-zåäöéü]*[A-ZÅÄÖÉÜ][A-ZÅÄÖÉÜ0-9\s.,:'’\-()§/&]*$")
#: A capitalised token that is an anonymised party rather than a word: Domstolsverket
#: replaces names with initials, so a line reading "AA BB" is in caps for the same reason
#: "SAKEN" is and would otherwise open a section of its own on every judgment.
_INITIALS_RE = re.compile(r"^(?:[A-ZÅÄÖ](?:\.[A-ZÅÄÖ])*\.?|[A-ZÅÄÖ]{1,3}\.?)$")
#: The one-word headings that a letter count alone cannot keep — "SKÄL" and "DOM" are
#: shorter than some parties' initials.
_CAPS_ALWAYS = frozenset({
    "DOM", "BESLUT", "SKÄL", "SKAL", "DOMSKÄL", "DOMSLUT", "PARTER", "SAKEN", "YRKANDEN",
    "PROTOKOLL", "BAKGRUND", "NOTIS", "SLUT", "MOTIVERING", "RUBRIK", "REFERAT",
})
#: Under the reasoning the court sets its subheadings in sentence case: short,
#: unpunctuated, and followed by something that starts afresh. All three are needed —
#: a body line that merely wraps is also uncapitalised at its end.
_SUBHEAD_MAX = 45
#: Zones whose children are the reasoning — the only place a sentence-case short line is
#: a heading rather than a one-line paragraph.
_REASONING = ("SKÄL", "DOMSKÄL", "UTVECKLING AV TALAN", "PARTERNAS", "YRKANDEN",
              "BAKGRUND", "MOTIVERING")
#: A numbered reason, where the extraction kept the number.
_NUMBER_RE = re.compile(r"^(\d{1,3})\.\s+(?=[A-ZÅÄÖÉÜ«„”\"(])")
#: A list marker opening a block of its own: a bullet, a lettered sub-point ("a)"), or a
#: numbered order ("2)"). The court's orders are set this way and must not be glued on to
#: the sentence above them.
_MARKER_RE = re.compile(r"^(?:[-•*]\s|[a-zåäö]\)\s|\d{1,3}\)\s)")
#: A line-end hyphen that is a real coordination ("mark- och miljödomstolen") rather than
#: a word broken across the line ("rätte-gångskostnad").
_COORDINATION = re.compile(r"^(?:och|eller|samt|respektive)\b", re.I)


def _is_furniture(line: str) -> bool:
    return bool(_DOKID_RE.match(line) or _PAGE_RE.match(line) or _TRAILER_RE.match(line))


def _running_headers(lines: list[str]) -> frozenset[str]:
    """The lines this document repeats at the top of its pages.

    A judgment that carries the appealed decision as an annex carries that court's page
    headers too, and they are split over lines the single-line pattern cannot see —
    ``NACKA TINGSRÄTT`` above ``DOM F 6439-22``, ninety times. What identifies them is not
    their shape but their *repetition in header position*: no sentence of a judgment
    appears three times directly under a page break. Reading it off the document also
    means a court whose header this parser has never seen still loses it.
    """
    counts: dict[str, int] = {}
    header = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _PAGE_RE.match(stripped):
            header = 5
            continue
        if header > 0 and not _is_furniture(stripped):
            header -= 1
            counts[stripped] = counts.get(stripped, 0) + 1
    return frozenset(line for line, n in counts.items() if n >= 3)


def _strip_furniture(lines: list[str], repeated: frozenset[str] = frozenset()) -> list[str]:
    """Drop page headers wherever they fall, and letterheads only inside one.

    ``header`` counts down the lines still eligible to be read as page furniture: a page
    break opens the window and ordinary prose closes it. That window is what keeps a
    party's ``Box 2066`` — which looks exactly like the court's own — in the document,
    because it appears under ``PARTER``, well past any header.

    A letterhead is a *run*, not a set of independent lines. ``Telefon`` on one line and
    ``08-561 666 00`` on the next are only recognisable together, and the courts break
    the block differently on every page. So the first unmistakable contact word opens a
    run that consumes the short unpunctuated lines after it, and a heading, a page marker
    or a real sentence closes it again.
    """
    out: list[str] = []
    header = 14                     # the first page opens in a header
    run = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            # A page break is not a paragraph break. Blank lines around one belong to
            # the layout, and kept they would stop the sentence the break interrupted
            # from closing back up.
            if header <= 0:
                out.append("")
            continue
        if _PAGE_RE.match(stripped):
            header, run = 10, 0
            while out and not out[-1]:
                out.pop()
            continue
        if _is_furniture(stripped):
            continue
        heading = _is_heading(stripped, "") is not None
        if run and (heading or len(stripped) > _CONTACT_MAX
                    or stripped.endswith((".", "!", "?"))):
            run = 0                 # the letterhead is over; this line is the document
        elif run:
            run -= 1
            continue
        if header > 0 and (_RUNNING_RE.match(stripped) or stripped in repeated):
            header -= 1
            continue
        # An unmistakable contact *word* opens a letterhead wherever it appears — the
        # Svea divisions print theirs below the court's name, which is a heading and so
        # ends the header window before the address block has even started. An ambiguous
        # contact *value* is only furniture inside that window, because "Box 5553" under
        # PARTER is a party's address.
        if _CONTACT_OPENS.search(stripped) and (header > 0 or len(stripped) <= 60):
            run = _CONTACT_RUN
            continue
        if header > 0:
            header -= 1
            if _CONTACT_VALUE.match(stripped):
                run = _CONTACT_RUN
                continue
        if heading:
            header = 0              # past the furniture; this is the document
        out.append(stripped)
    return out


def _join(lines: list[str]) -> list[tuple[str, int | None]]:
    """Rejoin the PDF's visual lines into sentences, classifying as it goes.

    A line continues the previous one unless the previous one ended a sentence, or this
    one starts something new. Whether it starts something new includes whether it is a
    subheading, and *that* depends on the section it sits in — so the zone has to be
    tracked here rather than in a later pass. Deciding it afterwards is too late: by then
    "Frågan i målet" has already been glued onto the sentence above it.
    """
    out: list[tuple[str, int | None]] = []
    zone = ""
    for index, line in enumerate(lines):
        if not line or _RULE_RE.match(line):
            # A rule is the court's own separator between its top matter and its order.
            # It carries no text, and leaving it in the stream glued the heading that
            # follows it onto a row of underscores.
            out.append(("", None))
            continue
        following = next((n for n in lines[index + 1:] if n), "")
        heading = _is_heading(line, zone, following)
        if heading == 0:
            zone = line
        starts_block = heading is not None or bool(
            _NUMBER_RE.match(line) or _MARKER_RE.match(line))
        # A heading is complete by definition, whatever its punctuation. Testing the text
        # again would ask a level-1 subheading out of context, where it no longer looks
        # like one, and "Bakgrund" would swallow the paragraph it introduces.
        if (out and out[-1][0] and not starts_block and out[-1][1] is None
                and not _ends_sentence(out[-1][0])):
            prev, prev_level = out[-1]
            if prev.endswith("-") and not _COORDINATION.match(line):
                out[-1] = (prev[:-1] + line, prev_level)
            else:
                out[-1] = (f"{prev} {line}", prev_level)
            continue
        out.append((line, heading))
    return [(text, level) for text, level in out if text]


def _ends_sentence(text: str) -> bool:
    """Whether a line is finished, for the purpose of not gluing the next one onto it.

    A heading never continues, and nor does a line ending in terminal punctuation. An
    abbreviation's full stop ("jfr prop.", "s.", "dvs.") does not end a sentence, so the
    common Swedish ones are excluded rather than treating every "." as terminal.
    """
    if _is_heading(text, "") is not None:
        return True
    if not text.endswith((".", ":", "!", "?", ")", "”", "”")):
        return False
    tail = text.rsplit(" ", 1)[-1].rstrip(".:!?)””").casefold()
    return tail not in _ABBREV


#: Swedish legal abbreviations whose full stop is not a sentence end.
_ABBREV = frozenset({
    "s", "nr", "st", "p", "kap", "mom", "bl.a", "bl", "a", "dvs", "t.ex", "ex", "jfr",
    "prop", "not", "ref", "avd", "resp", "m.m", "m.fl", "osv", "f", "ff", "sk", "st.f",
})


def _is_heading(line: str, zone: str, following: str = "") -> int | None:
    """0 for a section heading, 1 for a subheading of the reasoning, None otherwise."""
    if not line or len(line) > 120:
        return None
    tokens = line.split()
    if _CAPS_RE.match(line) and not line.endswith("."):
        if line.upper() in _CAPS_ALWAYS:
            return 0
        # Not every run of capitals is a heading: anonymised parties are set the same way.
        if (len([c for c in line if c.isalpha()]) >= 4
                and not all(_INITIALS_RE.match(t) for t in tokens)):
            return 0
    if (zone.upper().startswith(_REASONING) and len(line) <= _SUBHEAD_MAX
            and not line.endswith((".", ":", ",", ";", "-"))
            and line[:1].isupper()
            and (not following or following[:1].isupper() or following[:1].isdigit())):
        return 1
    return None


def parse_se_judgment_pdf(data: bytes | str) -> ParsedDoc:
    """Extracted PDF text of a Swedish judgment → flat text and outline segments."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")
    if not text.strip():
        return ParsedDoc()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blank = sum(1 for line in lines if not line.strip())
    if blank >= _DOUBLE_SPACED * len(lines):
        lines = [line for line in lines if line.strip()]
    lines = _strip_furniture(lines, _running_headers(lines))

    blocks: list[tuple[str, str, str, int]] = []
    zones: list[str] = []
    label = "Avgörande"
    level = 0
    for line, heading in _join(lines):
        if heading is not None:
            if heading == 0:
                zones.append(line)
            label, level = line, heading
            blocks.append((line, "heading", line, heading))
            continue
        body, kind, name = line, "zone", label
        if m := _NUMBER_RE.match(line):
            name, kind, body = f"{int(m.group(1))}.", "paragraph", line[m.end():]
        if body.strip():
            blocks.append((name, kind, body, level + 1))

    flat, segments = assemble(blocks)
    return ParsedDoc(text=flat or None, segments=segments,
                     metadata={"zones": zones} if zones else {})


register("se-judgment-pdf", parse_se_judgment_pdf)
