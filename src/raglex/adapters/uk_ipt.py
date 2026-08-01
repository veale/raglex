"""Investigatory Powers Tribunal — judgments from investigatorypowerstribunal.org.uk.

The Tribunal publishes each judgment as its own HTML page, and the pages are unusually
tractable: the neutral citation sits on an early line ("Neutral Citation Number: [2025]
UKIPTrib 10"), the case numbers under it, and the body is numbered paragraphs. So this
adapter reads the judgment itself rather than a PDF, and segments it by the paragraph
number a later judgment will cite.

**One entry is not an IPT judgment.** *Kennedy v the United Kingdom* is the Strasbourg
judgment in the same litigation, republished here for convenience. It is identified the
way the Tribunal's own page furniture identifies it — its infobox carries "Application
no. 26839/05" where a Tribunal judgment carries a case number — and skipped, because the
corpus holds ECtHR judgments from HUDOC under their proper identity and a second copy
keyed to an IPT slug would be a duplicate of a different court's work.

**Discovery has two modes.** A backfill reads the listing page, which carries the whole
set (a hundred judgments) in one request. A watch posts to the site's JetSmartFilters
endpoint with a date range, which is what makes an incremental check cheap: it returns
only the judgments published in the window, so the ordinary case is an empty answer.

**RIPA and IPA are shorthand here, always.** In a Tribunal judgment those letters mean
the Regulation of Investigatory Powers Act 2000 and the Investigatory Powers Act 2016
every single time — the Tribunal exists to apply them. Elsewhere they are ambiguous
enough to be dangerous, so the expansion is scoped to this source (see
``citations.stage._SOURCE_ALIASES``) rather than let loose on the corpus.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin

from ..core.adapter import BaseAdapter
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub

BASE = "https://investigatorypowerstribunal.org.uk"
LISTING = f"{BASE}/judgments/"
AJAX = f"{BASE}/wp-admin/admin-ajax.php"

# The Acts the Tribunal applies. Their ids are the resolution targets for the
# source-scoped shorthands.
RIPA_2000 = "ukpga/2000/23"
IPA_2016 = "ukpga/2016/25"

# "Neutral Citation Number: [2025] UKIPTrib 10" — the identity, on an early line.
_NEUTRAL_RE = re.compile(
    r"Neutral\s+Citation\s+(?:Number|No\.?)\s*:?\s*\[?(?P<year>\d{4})\]?\s*"
    r"UKIPTrib\s*(?P<num>[0-9]+[A-Za-z]?)", re.IGNORECASE)
# An ECtHR judgment republished on the site: its infobox gives an application number
# where a Tribunal judgment gives a case number.
_APPLICATION_NO_RE = re.compile(r"\bApplication\s+no", re.IGNORECASE)
# "IPT/17/110-112/CH" — the Tribunal's own case reference.
_CASE_NO_RE = re.compile(r"\bIPT[/\\][0-9]", re.IGNORECASE)
# A numbered paragraph opening a line: "1.This is the judgment", "7. The issues".
_PARA_RE = re.compile(r"^\s*(\d{1,3})\.\s*(?=\S)")
_DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Z][a-z]+)\s+(\d{4})\b")

_ARTICLE_RE = re.compile(r"<article\b.*?</article>", re.DOTALL | re.IGNORECASE)
_LINK_RE = re.compile(r'href="([^"]*/judgement/[^"]+/)"')
_HEADING_RE = re.compile(r"<h[34][^>]*>(.*?)</h[34]>", re.DOTALL | re.IGNORECASE)
_TITLE_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.DOTALL | re.IGNORECASE)


def _text(html: str) -> str:
    from html import unescape

    return unescape(re.sub(r"<[^>]+>", " ", html or "")).strip()


def _lines(html: str) -> list[str]:
    """The page as visible lines, page furniture removed."""
    from html import unescape

    body = re.sub(r"(?is)<(script|style|nav|header|footer|noscript)[^>]*>.*?</\1>", " ",
                  html or "")
    out = unescape(re.sub(r"<[^>]+>", "\n", body))
    return [line.strip() for line in out.split("\n") if line.strip()]


def _parse_date(value: str) -> date | None:
    match = _DATE_RE.search(value or "")
    if not match:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(" ".join(match.groups()), fmt).date()
        except ValueError:
            continue
    return None


def _provisional_id(url: str) -> str:
    slug = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return f"uk-ipt/{slug}" if slug else "uk-ipt/unknown"


def neutral_citation_id(text: str) -> str | None:
    """``[2025] UKIPTrib 10`` → ``ukiptrib/2025/10`` — the corpus's identity for a UK
    neutral citation, and what makes a later judgment's citation of it resolve."""
    match = _NEUTRAL_RE.search(text or "")
    if not match:
        return None
    return f"ukiptrib/{match.group('year')}/{match.group('num').lower()}"


def parse_listing(html: str) -> list[dict]:
    """The judgment cards: link, case name, case reference, published date.

    Both discovery modes render the same card markup — the listing page inlines it and
    the AJAX endpoint returns it as a JSON string — so one parser serves both.
    """
    found: list[dict] = []
    seen: set[str] = set()
    for block in _ARTICLE_RE.findall(html or ""):
        link = _LINK_RE.search(block)
        if not link:
            continue
        url = urljoin(BASE, link.group(1))
        if url in seen:
            continue
        seen.add(url)
        headings = [_text(h) for h in _HEADING_RE.findall(block)]
        title = _text((_TITLE_RE.search(block) or [None, ""])[1]) if _TITLE_RE.search(block) else ""
        found.append({
            "url": url,
            "title": title or (headings[0] if headings else ""),
            "case_number": next((h for h in headings if _CASE_NO_RE.search(h)), None),
            "published": next((d for h in headings if (d := _parse_date(h))), None),
        })
    if found:
        return found
    # The plain listing page is not wrapped in <article> for every theme revision; fall
    # back to the links themselves so a template change degrades to fewer FIELDS rather
    # than to no judgments at all.
    for url in dict.fromkeys(_LINK_RE.findall(html or "")):
        found.append({"url": urljoin(BASE, url), "title": "",
                      "case_number": None, "published": None})
    return found


def _ajax_body(date_range: str) -> str:
    """The JetSmartFilters query the site's own judgments page issues.

    ``posts_per_page=-1`` returns the whole window in one response, and the skin id is
    load-bearing: without ``custom_skin_template`` the endpoint renders a different card
    that carries no link at all.
    """
    from urllib.parse import urlencode

    return urlencode({
        "action": "jet_smart_filters",
        "provider": "epro-posts/default",
        "query[_date_query_|date]": date_range,
        "defaults[post_type]": "judgement",
        "defaults[paged]": "1",
        "defaults[posts_per_page]": "-1",
        "settings[_skin]": "custom",
        "settings[custom_skin_template]": "871",
        "settings[posts_post_type]": "judgement",
        "settings[posts_include_term_ids][]": "11",
        "settings[posts_orderby]": "post_date",
        "settings[posts_order]": "desc",
        "settings[posts_query_id]": "jet-smart-filters",
        "settings[_el_widget_id]": "d006685",
        "props[found_posts]": "100",
        "props[page]": "1",
    })


class UKIPTAdapter(BaseAdapter):
    source = "uk-ipt"
    min_interval = 1.5

    def __init__(self) -> None:
        self._client = None

    def _get(self):
        if self._client is None:
            from ..core.http import build_client

            self._client = build_client(timeout=60)
        return self._client

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        client = self._get()
        if since:
            # Incremental: ask only for the window since the watermark. The endpoint
            # wants Y.M.D-Y.M.D, and an empty window is the ordinary answer.
            start = _parse_iso(since) or date(2000, 1, 1)
            today = date.today()
            body = _ajax_body(f"{start.year}.{start.month}.{start.day}"
                              f"-{today.year}.{today.month}.{today.day}")
            response = client.post(
                AJAX, content=body,
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                         "X-Requested-With": "XMLHttpRequest",
                         "Referer": LISTING, "Origin": BASE})
            response.raise_for_status()
            try:
                html = (response.json() or {}).get("content") or ""
            except (ValueError, json.JSONDecodeError):
                html = response.text
        else:
            response = client.get(LISTING)
            response.raise_for_status()
            html = response.text

        for card in parse_listing(html):
            yield Stub(
                # The real id is the neutral citation inside the page; until it is
                # fetched the URL slug keys the stub (see nz-caselaw, same shape).
                stable_id=_provisional_id(card["url"]),
                landing_url=card["url"],
                raw_url=card["url"],
                title=card["title"] or None,
                published_at=card["published"].isoformat() if card["published"] else None,
                hints={k: v for k, v in card.items() if v and k != "url"},
            )

    def fetch(self, stub: Stub) -> Record | None:
        response = self._get().get(stub.landing_url)
        response.raise_for_status()
        html = response.text
        lines = _lines(html)
        head = "\n".join(lines[:14])

        # Strasbourg's own judgment, republished here. Its infobox gives an application
        # number where a Tribunal judgment gives a case number; the corpus holds ECtHR
        # judgments under their HUDOC identity, so a copy keyed to an IPT slug would be
        # a second, worse record of another court's work.
        if _APPLICATION_NO_RE.search(head) and not _CASE_NO_RE.search(head):
            return None

        body = "\n".join(lines[1:])          # drop the <title> duplicate
        stable_id = neutral_citation_id(body) or _provisional_id(stub.landing_url)
        case_number = (stub.hints.get("case_number")
                       or next((line for line in lines[:10] if _CASE_NO_RE.search(line)), None))
        decided = (_parse_date(next((line for line in lines[:20]
                                     if line.lower().startswith("date")), ""))
                   or _parse_iso(stub.published_at or "")
                   or _parse_date(head))
        title = stub.title or (lines[1] if len(lines) > 1 else stable_id)

        return Record(
            source=self.source,
            stable_id=stable_id,
            doc_type=DocType.JUDGMENT,
            title=title,
            court="ukiptrib",
            decision_date=decided,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=html.encode("utf-8"),
            raw_ext="html",
            text=body,
            segments=paragraph_segments(body),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["investigatory-powers", "surveillance"],
            extra={k: v for k, v in {
                "case_number": case_number,
                "url": stub.landing_url,
                "neutral_citation": _neutral_citation_text(body),
            }.items() if v},
        )


def _neutral_citation_text(text: str) -> str | None:
    match = _NEUTRAL_RE.search(text or "")
    return f"[{match.group('year')}] UKIPTrib {match.group('num')}" if match else None


def _parse_iso(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def paragraph_segments(text: str) -> list[Segment]:
    """One segment per numbered paragraph — the unit a later judgment pinpoints.

    The Tribunal numbers its reasoning "1.", "2." … and cites other judgments the same
    way, so these are the citable units. Everything before paragraph 1 (the intituling,
    the bench, counsel) is one opening segment rather than being dropped: it is where
    the case numbers and the coram live.
    """
    segments: list[Segment] = []
    offset = 0
    open_label: str | None = None
    open_start = 0
    for line in (text or "").split("\n"):
        match = _PARA_RE.match(line)
        if match:
            number = int(match.group(1))
            # Restarting numbering means a new part (a closed annex, a schedule of
            # issues); only advance, so a stray "1." mid-judgment cannot rewrite it.
            if open_label is None or number > int(open_label.split()[-1]):
                if open_label is not None and offset > open_start:
                    segments.append(Segment(open_label, open_start, offset,
                                            kind="paragraph", level=1))
                elif open_label is None and offset > open_start:
                    segments.append(Segment("Opening", open_start, offset,
                                            kind="opening", level=0))
                open_label = f"para {number}"
                open_start = offset
        offset += len(line) + 1
    end = len(text or "")
    if open_label is not None and end > open_start:
        segments.append(Segment(open_label, open_start, end, kind="paragraph", level=1))
    return segments
