"""lahend.ee's Markdown rendition of an Estonian court decision.

lahend.ee serves a decision as Markdown with a metadata block on top and the court's own
text beneath it:

```markdown
# Korrakaitse › Vanglad — 3-25-3458/5

- **Kohus:** Tartu Halduskohus Jõhvi kohtumaja
- **Kohtuasja number:** 3-25-3458/5
- **Kuulutamise aeg:** 31.01.2026

## Lahendi tekst

### TARTU HALDUSKOHUS
### KOHTUMÄÄRUS
### RESOLUTSIOON
1. Tagastada Xi kaebus.
### ASJAOLUD JA MENETLUSE KÄIK
1. Tartu Vangla kinnipeetav Y esitas 14. oktoobril 2025 …
```

Two things this parser has to get right:

**The bullet block is metadata, not text.** The ``- **Kohus:** …`` lines restate the
court, the case number, the proceeding type and the date, all of which the API also
returns as structured fields. Leaving them in the body makes a full-text search for a
court name match every decision that merely prints one, and puts a header block between
the reader and the judgment. They are parsed into ``metadata`` and dropped from the text.

**The numbered paragraph is the citable unit.** An Estonian judgment numbers its reasons
"1.", "2.", … and lahend.ee keeps that numbering in the Markdown. It becomes the segment
label, so a passage retrieved from the middle of a decision can be cited back as the
paragraph it came from.

Headings are ``###`` (the court's own section headings — RESOLUTSIOON, ASJAOLUD,
KOHTU SEISUKOHT) under a ``##`` wrapper the service adds. The wrapper's level is kept so
the reader renders the court's headings as one flat level rather than as children of a
synthetic parent.
"""

from __future__ import annotations

import re

from ..core.segmentation import assemble
from .base import ParsedDoc, register

#: ``- **Kohus:** Tartu Halduskohus`` — the service's metadata bullets, and the English
#: name each maps to.
_META_KEYS = {
    "kohus": "court",
    "kohtuasja number": "case_number",
    "asja liik": "case_kind",
    "menetlusliik": "procedure",
    "kuulutamise aeg": "decided_at",
    "lahendi kuupäev": "decided_at",
    "staatus": "status",
    "url": "url",
    "kategooria": "category",
    "valdkond": "category",
    "kohtunik": "judge",
    "ettevõtted": "companies",
}
_BULLET_RE = re.compile(r"^\s*[-*]\s*\*\*(?P<key>[^:*]+):?\*\*:?\s*(?P<value>.*)$")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
#: "1." / "12." opening a paragraph — the Randnummer equivalent.
#:
#: The following word must be **capitalised**, and that is the whole guard. An Estonian
#: date is written "31. jaanuar 2026" with a lowercase month, and without this the
#: parser read the day as a paragraph number and deleted it: every ruling's own date
#: came out as "jaanuar 2026". A numbered paragraph always opens a sentence.
_NUMBER_RE = re.compile(r"^\s*(\d{1,3})\.\s+(?=[A-ZÕÄÖÜŠŽ\"„])")
#: The service's own wrapper heading, which introduces the court's text and is not part
#: of it. Kept as a zone marker but not as an outline level for what follows.
_BODY_HEADINGS = {"lahendi tekst", "lahendi sisu", "otsuse tekst"}


def parse_lahend_md(data: bytes | str) -> ParsedDoc:
    """A lahend.ee decision Markdown → flat text, outline segments and its metadata."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")
    blocks: list[tuple[str, str, str, int]] = []
    metadata: dict = {}
    zones: list[str] = []
    title: str | None = None
    label = "Lahend"
    level = 0
    in_body = False
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer, label
        body = " ".join(" ".join(buffer).split())
        buffer = []
        if not body:
            return
        kind = "zone"
        block_label = label
        if m := _NUMBER_RE.match(body):
            block_label = f"{int(m.group(1))}."
            kind = "paragraph"
            body = body[m.end():]
        if body:
            blocks.append((block_label, kind, body, level + 1))

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if bullet := _BULLET_RE.match(stripped):
            # A metadata bullet only counts BEFORE the body heading; the court's own text
            # contains bulleted lists too, and reading one of those as metadata would
            # overwrite the case number with a sentence from the reasoning.
            if not in_body:
                key = _META_KEYS.get(bullet.group("key").strip().casefold())
                if key:
                    metadata.setdefault(key, bullet.group("value").strip())
                    continue
        if heading := _HEADING_RE.match(stripped):
            flush()
            depth = len(heading.group("hashes"))
            head = heading.group("text").strip()
            if depth == 1 and title is None:
                title = head
                continue
            if head.casefold() in _BODY_HEADINGS:
                in_body = True
                level = 0
                label = head
                continue
            in_body = True
            level = max(0, depth - 3)
            label = head
            zones.append(head)
            blocks.append((head, "heading", head, level))
            continue
        buffer.append(stripped)
    flush()

    body_text, segments = assemble(blocks)
    if zones:
        metadata["zones"] = zones
    if title:
        metadata["heading"] = title
    return ParsedDoc(text=body_text or None, segments=segments, title=title,
                     metadata=metadata)


register("lahend-md", parse_lahend_md)
