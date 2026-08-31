"""Riigi Teataja's ``Juurakt`` XML — the Estonian statute book's own consolidated text.

Riigi Teataja serves every consolidated act as a namespaced XML tree whose structure is
already the citation hierarchy Estonian judgments use:

```xml
<paragrahv id="para101">
  <paragrahvNr>101</paragrahvNr>
  <kuvatavNr>§ 101.</kuvatavNr>
  <paragrahvPealkiri>Õiguskaitsevahendid kohustuse rikkumise korral</paragrahvPealkiri>
  <loige id="para101lg1">
    <loigeNr>1</loigeNr><kuvatavNr>(1)</kuvatavNr>
    <sisuTekst><tavatekst>Kui võlgnik on kohustust rikkunud, võib võlausaldaja:</tavatekst></sisuTekst>
    <alampunkt id="para101lg1p1">
      <alampunktNr>1</alampunktNr><kuvatavNr>1)</kuvatavNr>
      <sisuTekst><tavatekst>nõuda kohustuse täitmist;</tavatekst></sisuTekst>
    </alampunkt>
  </loige>
</paragrahv>
```

``§`` → ``lg`` (lõige) → ``p`` (punkt) is exactly what ``citations.estonian`` parses out
of a judgment, so the segment labels this parser emits are the anchors those citations
already carry. Two things have to be got right for that to hold.

## ``paragrahvNr`` does not identify the section

**§ 22 and § 22¹ both carry ``<paragrahvNr>22</paragrahvNr>``.** The superscript survives
only in ``kuvatavNr``, and there it is *escaped character data* — the element's text is
the literal eleven characters ``§ 22<sup>1</sup>.``, not markup, so an XML parser hands it
over as a string and `itertext()` on the tree never sees a `sup` element.

Reading the number from ``paragrahvNr`` therefore labels two different provisions ``§ 22``
and merges them. That is not cosmetic: ``citations.estonian`` is explicit that ``§ 43¹`` is
a *different provision* from ``§ 43`` rather than a subdivision of it, and normalises it to
``§ 43-1``. TsMS alone has 122 held citations of ``§ 415-4``, 37 of ``§ 660-6`` and dozens
more; every one of them would land on the wrong section, or on a section that has silently
absorbed another's text. So the number comes from ``kuvatavNr`` when it parses, and
``paragrahvNr`` is only the fallback for the acts that have no superscript sections at all.

## ``<sup>`` inside running text is escaped too

The same escaping appears mid-sentence wherever the text cites a superscript section:
``§ 43<sup>1</sup> lõike 2`` as literal characters. Flattened naively that reads ``§ 431``
— a plausible-looking section number that does not exist, and one that a later extraction
pass would mint a confident citation to. Both cases are folded to the real Unicode
superscript (``§ 43¹``), which is the form ``citations.estonian._anchor`` already
normalises to ``-1``.

## There is more than one schema, and one act has no schema at all

An act Riigi Teataja never consolidated is served under the base-act namespace
``tyviseadus_1_10.02.2010`` with byte-identical element names, so every lookup here
matches the local name and ignores the namespace. Two acts parsed to empty text — with no
error and no status to notice — before that was true.

A handful of old repealed acts (RERS, the 1990s state-pensions act) carry no markup at
all: their ``sisu`` is an ``HTMLKonteiner`` pointing at an attached file. They parse to
zero sections, and the adapter declines them rather than storing an empty statute.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

from ..core.segmentation import assemble
from .base import ParsedDoc, register

#: The consolidated-text schema declares its namespace as the bare string "Juurakt"
#: rather than a URI. It is **not the only one**: an act Riigi Teataja never consolidated
#: is served under ``tyviseadus_1_10.02.2010`` — the base-act schema — with byte-identical
#: element names. Matching the namespace rather than the local name dropped two acts to
#: empty text and no error, so every lookup here is namespace-agnostic.


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(parent: ET.Element | None, name: str) -> ET.Element | None:
    """The first direct child with this local name, whatever namespace it carries."""
    if parent is None:
        return None
    for child in parent:
        if _local(child.tag) == name:
            return child
    return None


def _find_path(root: ET.Element | None, *names: str) -> ET.Element | None:
    node = root
    for name in names:
        node = _find(node, name)
    return node


def _iter(root: ET.Element, name: str):
    for element in root.iter():
        if _local(element.tag) == name:
            yield element


#: ``<sup>1</sup>`` arrives as escaped character data rather than as an element, in both
#: ``kuvatavNr`` and running text. Matched as text because that is what it is.
_SUP_RE = re.compile(r"<sup>\s*([0-9]{1,2})\s*</sup>", re.IGNORECASE)
_SUPERSCRIPTS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
#: "§ 22¹." / "§ 497²²." / "§ 101." — the section number as the act itself displays it.
#: The superscript run is matched whole: TsMS's collective-redress chapter numbers its
#: sections § 497 through § 497²², twenty-three distinct provisions that all report
#: ``<paragrahvNr>497</paragrahvNr>``. Capturing one superscript digit merges eleven of
#: them onto "§ 497-1" and another four onto "§ 497-2".
_KUVATAV_SECTION_RE = re.compile(r"§+\s*(\d{1,4}(?:[⁰¹²³⁴⁵⁶⁷⁸⁹]+|-\d{1,2})?)")
#: A run of superscript digits is ONE ordinal — ``¹¹`` is eleven, not one-then-one.
_SUP_RUN_RE = re.compile(f"[{_SUPERSCRIPTS}]+")


def _superscript(value: str) -> str:
    """``§ 22<sup>1</sup>`` → ``§ 22¹``, leaving everything else alone."""
    def one(match: re.Match) -> str:
        return "".join(_SUPERSCRIPTS[int(d)] for d in match.group(1))
    return _SUP_RE.sub(one, value or "")


def _flat(element: ET.Element | None) -> str:
    """An element's text, with escaped superscripts restored and whitespace collapsed."""
    if element is None:
        return ""
    return " ".join(_superscript("".join(element.itertext())).split())


def _anchor_number(raw: str) -> str:
    """``22¹`` → ``22-1``, ``497²²`` → ``497-22``. The spelling
    ``citations.estonian._anchor`` produces, so a judgment's ``§ 22-1`` anchor and this
    segment's label are the same string.

    The whole superscript run converts at once. Replacing digit by digit would render
    § 497²² as ``497-2-2`` and collide it with § 497².
    """
    def run(match: re.Match) -> str:
        return "-" + "".join(str(_SUPERSCRIPTS.index(c)) for c in match.group(0))
    return _SUP_RUN_RE.sub(run, raw or "")


def _section_label(paragraph: ET.Element) -> str | None:
    """The section's citable number, preferring ``kuvatavNr`` — see the module docstring:
    ``paragrahvNr`` cannot tell § 22 from § 22¹."""
    displayed = _flat(_find(paragraph, "kuvatavNr"))
    if match := _KUVATAV_SECTION_RE.search(displayed):
        return f"§ {_anchor_number(match.group(1))}"
    number = _flat(_find(paragraph, "paragrahvNr"))
    return f"§ {_anchor_number(number)}" if number else None


def _content(element: ET.Element) -> str:
    """The text of one lõige/punkt, prefixed with the number the act displays for it, so
    the flat text reads as the act reads: ``(1) Kohustus tuleb täita…``, ``1) nõuda…``."""
    displayed = _flat(_find(element, "kuvatavNr"))
    body_parts: list[str] = []
    for child in element:
        if _local(child.tag) in {"sisuTekst", "tavatekst"}:
            body_parts.append(_flat(child))
    body = " ".join(part for part in body_parts if part)
    return f"{displayed} {body}".strip() if displayed else body


def _section_text(paragraph: ET.Element) -> str:
    """One § as a block: its heading, then each lõige, then each punkt beneath it."""
    lines: list[str] = []
    label = _section_label(paragraph) or ""
    heading = _flat(_find(paragraph, "paragrahvPealkiri"))
    lines.append(f"{label}. {heading}".strip().rstrip("."))
    # A § with no lõiked carries its text directly; one with lõiked carries it beneath
    # them. Walking the children in document order handles both without a special case.
    for child in paragraph:
        name = _local(child.tag)
        if name in {"paragrahvNr", "kuvatavNr", "paragrahvPealkiri"}:
            continue
        if name == "loige":
            if body := _content(child):
                lines.append(body)
            for point in [c for c in child if _local(c.tag) == "alampunkt"]:
                if text := _content(point):
                    lines.append(text)
        elif name in {"sisuTekst", "tavatekst"}:
            if text := _flat(child):
                lines.append(text)
        elif name == "alampunkt":
            if text := _content(child):
                lines.append(text)
    return "\n".join(line for line in lines if line)


def _iso(value: str) -> date | None:
    text = (value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_riigiteataja_xml(data: bytes | str) -> ParsedDoc:
    """A Riigi Teataja consolidated act → flat text, one segment per §, and its metadata."""
    raw = data.encode("utf-8") if isinstance(data, str) else (data or b"")
    if not raw.strip():
        return ParsedDoc()
    root = ET.fromstring(raw)

    meta_element = _find(root, "metaandmed")
    title = _flat(_find_path(root, "aktinimi", "nimi", "pealkiri"))
    abbrev = _flat(_find(meta_element, "lyhend"))
    adopted = issuer = kind = ""
    if meta_element is not None:
        issuer = _flat(_find(meta_element, "valjaandja"))
        kind = _flat(_find(meta_element, "dokumentLiik"))
        adopted = _flat(_find_path(meta_element, "vastuvoetud", "aktikuupaev"))

    blocks: list[tuple[str, str, str, int]] = []
    chapter = division = ""
    for element in root.iter():
        name = _local(element.tag)
        if name == "peatykk":
            number = _flat(_find(element, "peatykkNr"))
            heading = _flat(_find(element, "peatykkPealkiri"))
            chapter = f"{number}. peatükk {heading}".strip()
            if chapter:
                blocks.append((chapter, "section", chapter, 0))
        elif name == "jagu":
            number = _flat(_find(element, "jaguNr"))
            heading = _flat(_find(element, "jaguPealkiri"))
            division = f"{number}. jagu {heading}".strip()
            if division:
                blocks.append((division, "section", division, 1))
        elif name == "paragrahv":
            label = _section_label(element)
            if not label:
                continue
            blocks.append((label, "section", _section_text(element), 2))

    text, segments = assemble(blocks)
    return ParsedDoc(
        text=text,
        segments=segments,
        title=title or None,
        decision_date=_iso(adopted),
        metadata={key: value for key, value in {
            "abbreviation": abbrev or None,
            "issuer": issuer or None,
            "act_kind": kind or None,
            "adopted": adopted or None,
            "sections": sum(1 for s in segments if s.kind == "section" and s.level == 2),
        }.items() if value not in (None, "", [], {})},
    )


register("riigiteataja-xml", parse_riigiteataja_xml)
