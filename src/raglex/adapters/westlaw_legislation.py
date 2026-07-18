"""Parse a Westlaw UK **legislation** export into a clean, section-segmented Act.

Westlaw delivers legislation from its "full text items" search as one RTF (often named
``.doc``) holding one *item per provision* — the Preamble, then ``s. 1``…``s. N``, then any
Schedules. Every item is wrapped in the same chrome, repeated ~45 times:

    Interpretation Act 1889 c. 63          ← running header (act title + chapter)
    © 2026 Thomson Reuters.
    For educational use only
    s. 38 Effect of repeal in future Acts. ← the item heading
    As Originally Enacted
    The text of this legislation is as originally enacted.
    38.— Effect of repeal in future Acts.  ← the provision, opening with its own num+heading
    (1.) Where this Act …
    Re-enactment of existing Rules. > s. 38 …   ← breadcrumb (crossheading > item)
    Contains public sector information licensed under the Open Government Licence v3.0.

That repetition is the "gunk": strip it and what remains is a well-ordered Act. This matters
for the older statutes legislation.gov.uk only holds as a scanned PDF (the Interpretation Act
1889 among them) — Westlaw is the only machine-readable text, so this parser is how such an
Act becomes a real, citable corpus document instead of a hanging reference.

Output is deliberately shaped like the Akoma Ntoso path so pinpoints resolve identically:
the **stable_id is the legislation.gov.uk id** (``ukpga/1889/63``, from "c. 63" + the year in
the title) and each provision is a ``Segment`` labelled ``s. 38 <heading>`` — the same
``s. {num} {heading}`` convention :mod:`raglex.formats.akoma_ntoso` uses, so a
"section 38 of the Interpretation Act 1889" edge lands on the right unit.

The version is recorded, not assumed: these exports say "As Originally Enacted", so the text
is the *as-enacted* Act, which for a much-amended statute is emphatically not current law.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..core.models import Segment
from .westlaw_rtf import _tidy, rtf_to_text

# The running header that opens every item: "<Act short title> c. 63" (or "c. 63A").
# Northern Ireland / older forms may use "(c. 63)"; both are accepted.
_ACT_HEADER = re.compile(r"^(?P<title>.+?)\s+\(?c\.\s*(?P<chapter>\d+[A-Za-z]*)\)?$")
# The year is the trailing 4 digits of the short title ("Interpretation Act 1889").
_TITLE_YEAR = re.compile(r"\b(1[6-9]\d{2}|20\d{2})\s*$")
# Per-item chrome, dropped wholesale.
_BOILERPLATE = (
    re.compile(r"^(©|\(c\))\s*\d{4}\s+Thomson Reuters", re.IGNORECASE),
    re.compile(r"^for educational use only$", re.IGNORECASE),
    re.compile(r"^the text of this legislation is as (originally enacted|it stands)", re.IGNORECASE),
    re.compile(r"^contains public sector information licensed under the open government",
               re.IGNORECASE),
)
# The version banner ("As Originally Enacted" / "As Amended To Date") — kept as metadata.
_VERSION_BANNER = re.compile(r"^as (originally enacted|amended.*|at .*)$", re.IGNORECASE)
# The breadcrumb closing an item: "<crossheading> > s. 38 <heading>" (may chain several " > ").
_BREADCRUMB = re.compile(r"^.+\s>\s(?:s\.|sch|schedule|para|pt\.|part|preamble)", re.IGNORECASE)
# An item heading: "s. 38 Effect of repeal…", "Schedule 1 ENACTMENTS REPEALED.", "Preamble".
_ITEM_HEADING = re.compile(
    r"^(?:(?P<section>s\.\s*(?P<num>\d+[A-Za-z]*))|(?P<sched>Sch(?:edule)?\.?\s*\d*[A-Za-z]*)"
    r"|(?P<preamble>Preamble)|(?P<part>Pt\.?\s*\d+[A-Za-z]*))\b",
    re.IGNORECASE)
# The provision's own opening line repeats its num + heading ("38.— Effect of repeal…"),
# which becomes the segment label, so it's dropped from the body (the Akoma Ntoso rule).
_PROVISION_OPENER = re.compile(r"^(?P<num>\d+[A-Za-z]*)\s*[.．]?\s*[—–-]?\s*(?P<heading>.*)$")
# The enacting date in the preamble: "[30th August 1889]".
_ENACTED = re.compile(r"\[(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})\]")
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


@dataclass(slots=True)
class ParsedWestlawLegislation:
    """One Westlaw legislation export, reduced to a citable Act."""

    title: str                              # "Interpretation Act 1889"
    chapter: str                            # "63"
    year: int | None                        # 1889
    stable_id: str | None                   # "ukpga/1889/63"
    text: str = ""
    segments: list[Segment] = field(default_factory=list)
    long_title: str | None = None           # "An Act for consolidating enactments relating to…"
    enacted_date: date | None = None
    version_note: str | None = None         # "As Originally Enacted"
    provisions: tuple[str, ...] = ()        # the item labels, in order
    crossheadings: dict = field(default_factory=dict)  # label → crossheading it sits under


def _is_boilerplate(line: str) -> bool:
    return any(p.match(line) for p in _BOILERPLATE)


def looks_like_westlaw_legislation(text: str) -> bool:
    """Does this tidied Westlaw text look like an Act rather than a judgment? Legislation
    exports carry the OGL notice and a "c. NN" running header; case exports instead carry
    "Where Reported"/"Judicial Consideration"/"Court"."""
    head = text[:6000]
    if re.search(r"judicial (consideration|treatment)|where reported|report citation", head, re.I):
        return False
    return bool(
        re.search(r"open government licence", text, re.IGNORECASE)
        or re.search(r"^.+\s\(?c\.\s*\d+[A-Za-z]*\)?$", head.split("\n")[0] if head else "",
                     re.MULTILINE))


def parse_westlaw_legislation(data: bytes, *, filename: str | None = None
                              ) -> ParsedWestlawLegislation | None:
    """Parse one Westlaw legislation RTF (``.rtf`` or a ``.doc`` that is really RTF).
    Returns None if the bytes aren't RTF, or don't read as a Westlaw Act."""
    if not data[:6].lstrip().startswith(b"{\\rtf"):
        return None
    text = _tidy(rtf_to_text(data))
    if not text or not looks_like_westlaw_legislation(text):
        return None

    lines = text.split("\n")
    header = lines[0].strip() if lines else ""
    m = _ACT_HEADER.match(header)
    if not m:
        return None
    act_title = m.group("title").strip()
    chapter = m.group("chapter")
    ym = _TITLE_YEAR.search(act_title)
    year = int(ym.group(1)) if ym else None
    # legislation.gov.uk keys a UK Public General Act as ukpga/<year>/<chapter> — the very id
    # the resolver already routes "the Interpretation Act 1889" to.
    stable_id = f"ukpga/{year}/{chapter}" if year else None

    # 1) split into items on the repeated running header
    items: list[list[str]] = []
    cur: list[str] = []
    for line in lines:
        if line.strip() == header:
            if cur:
                items.append(cur)
            cur = []
            continue
        cur.append(line)
    if cur:
        items.append(cur)

    # 2) clean each item down to (label, body)
    version_note: str | None = None
    long_title: str | None = None
    enacted: date | None = None
    crossheadings: dict[str, str] = {}
    blocks: list[tuple[str, str]] = []
    for raw_item in items:
        label: str | None = None
        body: list[str] = []
        crumb: str | None = None
        for line in raw_item:
            s = line.strip()
            if not s or _is_boilerplate(s):
                continue
            if _VERSION_BANNER.match(s):
                version_note = version_note or s
                continue
            if _BREADCRUMB.match(s):
                crumb = s
                continue
            if label is None:
                label = s          # first surviving line is the item heading
                continue
            body.append(s)
        if not label:
            continue
        if not _ITEM_HEADING.match(label):
            # not a provision item (a stray fragment) — skip rather than invent structure
            continue
        # the crossheading is the breadcrumb's first hop ("Re-enactment of existing Rules.")
        if crumb:
            head = crumb.split(" > ")[0].strip()
            if head and not _ITEM_HEADING.match(head):
                crossheadings[label] = head
        # drop the provision's duplicated "38.— Effect of repeal…" opener (it IS the label)
        num_m = re.match(r"^s\.\s*(\d+[A-Za-z]*)", label, re.IGNORECASE)
        if body and num_m:
            opener = _PROVISION_OPENER.match(body[0])
            if opener and opener.group("num").lower() == num_m.group(1).lower():
                body = body[1:]
        text_body = "\n".join(body).strip()
        if label.lower().startswith("preamble"):
            long_title = long_title or next(
                (b for b in body if b.lower().startswith("an act")), None)
            em = _ENACTED.search(text_body)
            if em:
                mon = _MONTHS.get(em.group(2).lower())
                if mon:
                    try:
                        enacted = date(int(em.group(3)), mon, int(em.group(1)))
                    except ValueError:
                        enacted = None
        blocks.append((label, text_body))

    if not blocks:
        return None

    # 3) assemble the clean Act text + one segment per provision (label = "s. 38 <heading>",
    #    matching the Akoma Ntoso convention so pinpoint edges land on the same unit).
    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for label, body in blocks:
        chunk = f"{label}\n{body}".strip() if body else label
        segments.append(Segment(label=label, char_start=cursor,
                                char_end=cursor + len(chunk),
                                kind="section", level=0))
        parts.append(chunk)
        cursor += len(chunk) + 2  # the "\n\n" joiner

    return ParsedWestlawLegislation(
        title=act_title, chapter=chapter, year=year, stable_id=stable_id,
        text="\n\n".join(parts), segments=segments,
        long_title=long_title, enacted_date=enacted, version_note=version_note,
        provisions=tuple(lbl for lbl, _ in blocks), crossheadings=crossheadings,
    )
