"""Parse a saved BAILII judgment page (the ``.html`` you get from bailii.org) into
everything the importer needs — the styled page is the richest form of many older UK
judgments, because its header carries what no other source states in one place:

  * the canonical **URL** line → the neutral-citation slug the corpus keys the case by
    (``https://www.bailii.org/uk/cases/UKHL/1986/10.html`` → ``ukhl/1986/10``);
  * the **"Cite as:"** list → every parallel report citation ("[1987] AC 460",
    "[1986] 3 WLR 972", …) — exactly the aliases report-only citations resolve by;
  * the ``<TITLE>`` → the case name + the decision date ("(2nd November, 2000)");
  * the ``<H1>`` database name → a human court label.

Parsing is bespoke string-slicing + BeautifulSoup over the sliced body: the page chrome
(nav tables, ICLR adverts, copyright footer) sits between well-known markers that have
been stable across BAILII's page generations, and the judgment body between them keeps
its numbered paragraphs either as visible "12." text or as ``<LI VALUE="12.">`` items.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from html import unescape as _unescape

from ..core.models import Segment
from .bailii_corpus import CleanName, bailii_path_to_slug, clean_case_name

# the canonical-URL line in the header block: URL: <I>https://www.bailii.org/...</I>
_URL_LINE = re.compile(
    r"URL:\s*(?:<[Ii]>)?\s*(https?://(?:www\.)?bailii\.org(/[^\s<]+))", re.IGNORECASE)
# the "Cite as:" run — everything up to the next tag-close of the <small> header block
_CITE_AS = re.compile(r"Cite\s+as:\s*(.*?)</(?:SMALL|small|p|P|TD|td)>", re.DOTALL)
# decision date in the <TITLE> tail: "(2nd November, 2000)" / "(19 November 1986)"
_TITLE_DATE = re.compile(
    r"\((?:\w+day,?\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})\)\s*$")
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}

# a numbered judgment paragraph at the start of a line of the extracted text:
# "12. The appellant …" (also matches the "12." we synthesise from <LI VALUE="12.">)
_PARA_LINE = re.compile(r"^(\d{1,3})\.\s+\S")


@dataclass(slots=True)
class ParsedBailii:
    """One judgment page, reduced to the fields the importer stores."""

    slug: str | None                       # FCL stable_id from the URL line / filename
    bailii_url: str | None
    title: str | None                      # bare party title (citation/date stripped)
    citations: tuple[str, ...] = ()        # the "Cite as:" list, as printed
    court_label: str | None = None         # the H1 database name
    decision_date: date | None = None
    text: str = ""
    segments: list[Segment] = field(default_factory=list)


def _decode(data: bytes) -> str:
    """BAILII pages declare iso-8859-1 (and older ones lie about even that); try the
    declared charset, then the two realistic candidates."""
    head = data[:2048].decode("ascii", errors="ignore")
    m = re.search(r"charset\s*=\s*['\"]?([\w-]+)", head, re.IGNORECASE)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "iso-8859-1"]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("iso-8859-1", errors="replace")


def slug_from_filename(filename: str | None) -> str | None:
    """Recover the slug from a saved file's name when the page lacks a URL line —
    BAILII "save as" names flatten the path with underscores:
    ``ew_cases_EWCA_Civ_2000_18.html`` → ``ewca/civ/2000/18``."""
    if not filename:
        return None
    stem = filename.rsplit("/", 1)[-1]
    stem = re.sub(r"(\s+copy(\s*\d*)?)?\.html?$", "", stem, flags=re.IGNORECASE)
    parts = stem.split("_")
    if "cases" not in [p.lower() for p in parts]:
        return None
    return bailii_path_to_slug("/" + "/".join(parts) + ".html")


def _body_slice(html: str) -> str:
    """The judgment body between the header chrome and the copyright footer.

    Start: the first ``<hr>`` after the header table that carries "Cite as:" closes —
    the ICLR "Buy report" adverts sit before it, the judgment after it. End: the footer
    block (``<B>BAILII:</B>`` copyright/disclaimer links). Missing markers degrade to
    the enclosing bounds rather than dropping the document."""
    low = html.lower()
    start = 0
    cite = low.find("cite as:")
    anchor = cite if cite != -1 else (low.find("you are here:") if "you are here:" in low else -1)
    if anchor != -1:
        table_end = low.find("</table>", anchor)
        hr = low.find("<hr", table_end if table_end != -1 else anchor)
        if hr != -1:
            start = low.find(">", hr) + 1
    end = len(html)
    foot = low.rfind("bailii:</b>")
    if foot != -1:
        last_hr = low.rfind("<hr", start, foot)
        if last_hr > start:
            end = last_hr
    return html[start:end]


def _body_text(fragment: str) -> str:
    """Flatten the judgment fragment to readable text, one block element per line —
    preserving ``<LI VALUE="12.">`` paragraph numbers as literal "12." prefixes (the
    post-2000 Court Service layout keeps its numbering only in that attribute)."""
    from bs4 import BeautifulSoup

    # literal newlines in the source are wrapping, not structure — block tags and
    # <br> alone decide where a line ends.
    soup = BeautifulSoup(re.sub(r"[\r\n]+", " ", fragment), "html.parser")
    for junk in soup(["script", "style"]):
        junk.decompose()
    # ICLR permission boilerplate (law-report-sourced pages) — but ONLY the
    # permission block: the page's running head shares the class and carries the
    # report citation ("12 QBD 271") the importer aliases the case by.
    for junk in soup.find_all("div", class_="topline_right"):
        if "permission for BAILII" in junk.get_text():
            junk.decompose()
    # white-on-white machine tags (ICLR_VOTE_BATCH_1 and kin)
    for junk in soup.find_all("font", color=re.compile(r"^white$", re.IGNORECASE)):
        junk.decompose()
    for li in soup.find_all("li"):
        v = li.get("value")
        if v is not None:
            num = re.match(r"\s*(\d+)", str(v))
            if num:
                li.insert(0, f"{num.group(1)}. ")
    # block-level elements end a line; inline markup must NOT split one (a paragraph
    # with <I>italics</I> would otherwise shatter into several "paragraphs").
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6",
                                "div", "tr", "center", "blockquote"]):
        block.append("\n")
    raw = soup.get_text()
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in raw.split("\n")]
    # drop BAILII's hidden classification tag (white-on-white "JISCBAILII_CASE_…")
    return "\n\n".join(ln for ln in lines if ln and not ln.startswith("JISCBAILII_"))


def _para_segments(text: str) -> list[Segment]:
    """Numbered-paragraph segments from the flattened text: a line starting "12. " is a
    paragraph seam. Only trusted when the numbering behaves like numbering — several
    paragraphs, ascending — so a bare "3." in prose can't fake a structure."""
    marks: list[tuple[int, int]] = []  # (para number, char offset)
    offset = 0
    for line in text.split("\n\n"):
        m = _PARA_LINE.match(line)
        if m:
            marks.append((int(m.group(1)), offset))
        offset += len(line) + 2
    ascending = [
        (n, at) for i, (n, at) in enumerate(marks)
        if (i == 0 or n > marks[i - 1][0]) and (i + 1 == len(marks) or n < marks[i + 1][0])
    ]
    if len(ascending) < 3:
        return []
    segs: list[Segment] = []
    for i, (n, at) in enumerate(ascending):
        end = ascending[i + 1][1] if i + 1 < len(ascending) else len(text)
        segs.append(Segment(label=f"para {n}", char_start=at, char_end=end, kind="paragraph"))
    return segs


def _title_date(raw_title: str) -> date | None:
    m = _TITLE_DATE.search(raw_title or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    try:
        return date(int(m.group(3)), mon, int(m.group(1)))
    except ValueError:
        return None


def parse_bailii_html(data: bytes, *, filename: str | None = None) -> ParsedBailii | None:
    """Parse one saved BAILII judgment page. Returns None only when the bytes aren't
    recognisably a BAILII page at all (no URL line, no title, no derivable slug)."""
    html = _decode(data)

    m = _URL_LINE.search(html)
    bailii_url = m.group(1) if m else None
    slug = bailii_path_to_slug(m.group(2)) if m else None
    if slug is None:
        slug = slug_from_filename(filename)

    tm = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    raw_title = _unescape(re.sub(r"\s+", " ", tm.group(1)).strip()) if tm else ""
    clean: CleanName = clean_case_name(raw_title)

    if slug is None and not clean.title:
        return None

    citations: list[str] = []
    cm = _CITE_AS.search(html)
    if cm:
        flat = _unescape(re.sub(r"<[^>]+>", " ", cm.group(1)))
        for part in flat.split(","):
            part = re.sub(r"\s+", " ", part).strip()
            if part and re.search(r"\d", part):
                citations.append(part)

    hm = re.search(r"<h1>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    court_label = _unescape(re.sub(r"\s+|<[^>]+>", " ", hm.group(1)).strip()) if hm else None

    text = _body_text(_body_slice(html))
    return ParsedBailii(
        slug=slug, bailii_url=bailii_url,
        title=clean.title or None,
        citations=tuple(dict.fromkeys(citations)),
        court_label=court_label or None,
        decision_date=_title_date(raw_title),
        text=text, segments=_para_segments(text),
    )
