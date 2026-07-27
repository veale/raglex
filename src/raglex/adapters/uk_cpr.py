"""Current consolidated Civil Procedure Rules and Practice Directions.

``legislation.gov.uk`` carries the Civil Procedure Rules 1998 (SI 1998/3132) and
the amending instruments.  The Ministry of Justice publishes the usable current
consolidation separately, one HTML page per Part and Practice Direction:

    https://www.justice.gov.uk/courts/procedure-rules/civil/rules

The URL slugs are historical and sometimes lag a renumbering (for example the
current PD 3D still has ``practice-direction-3e`` in its URL), so identity is
always taken from the visible index/H1, never inferred from the URL.

Rules are held one Part per record.  Each printed rule number is minted as an
alias (``uk/cpr/rule/3.9`` -> ``uk/cpr/part/3``), allowing the citation grammar
to target the exact current Part while retaining ``rule 3.9`` as its pinpoint.
Practice Directions are records of their own (``uk/cpr/pd/3d``).

The index has no reliable delta API.  A maintenance run walks the current index
and hashes each page's substantive HTML.  The shared pipeline consequently
re-fetches/re-extracts only changed pages while also noticing additions and
renumberings.  This is intentionally a ``full-walk`` currency source.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Segment,
    Stub,
    TypedRelation,
)

INDEX_URL = "https://www.justice.gov.uk/courts/procedure-rules/civil/rules"
CPR_ROOT_ID = "uk/cpr"
CPR_SI_ID = "uksi/1998/3132"
log = logging.getLogger(__name__)

_PART = re.compile(r"^\s*PART\s+(?P<code>\d+[A-Z]?)\b", re.IGNORECASE)
_PD = re.compile(
    r"^\s*(?P<welsh>CYFARWYDDYD\s+YMARFER|PRACTICE\s+DIRECTION)"
    r"\s*(?:[-–—:]\s*)?(?P<code>\d+(?:[A-Z]+(?:\d+)?)?)(?!\.)\b",
    re.IGNORECASE,
)
_PD_NAMED = re.compile(
    r"^\s*(?:PRACTICE\s+DIRECTION|CYFARWYDDYD\s+YMARFER)\s*[-–—:]\s*(?P<name>.+)",
    re.IGNORECASE,
)
_RULE_NUMBER = re.compile(
    r"(?m)^\s*(?P<n>\d+[A-Z]?\.\d+[A-Z]?(?:\([A-Za-z0-9]+\))*)"
    r"\s*(?:$|[.—–-])",
)
_PD_PARAGRAPH = re.compile(
    r"(?m)^\s*(?P<n>\d+(?:\.\d+)*[A-Z]?)(?:\.(?=\s|$)|(?=\s))",
)
_UPDATED = re.compile(r"Updated:\s*(?:[A-Za-z]+,\s*)?(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
                      re.IGNORECASE)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _slug(text: str) -> str:
    text = text.casefold().replace("’", "").replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _code(code: str) -> str:
    """Canonical printed Part/PD code (03 -> 3, suffix retained)."""
    m = re.fullmatch(r"0*(\d+)([A-Za-z]*)(\d*)", code.strip())
    if not m:
        return code.strip().casefold()
    return f"{int(m.group(1))}{m.group(2).lower()}{m.group(3)}"


@dataclass(frozen=True, slots=True)
class CPRPage:
    stable_id: str
    title: str
    url: str
    family: str  # root | part | pd
    code: str | None = None
    language: str = "en"


def page_identity(title: str, url: str, *, h1: str | None = None) -> CPRPage:
    """Derive identity from the printed title, never the legacy URL slug."""
    printed = _clean(h1 or title)
    m = _PART.match(printed)
    if m:
        code = _code(m.group("code"))
        return CPRPage(f"uk/cpr/part/{code}", printed, url, "part", code)
    m = _PD.match(printed)
    if m:
        code = _code(m.group("code"))
        welsh = m.group("welsh").casefold().startswith("cyfarwyddyd")
        suffix = "/cy" if welsh else ""
        return CPRPage(f"uk/cpr/pd/{code}{suffix}", printed, url, "pd", code,
                       "cy" if welsh else "en")
    m = _PD_NAMED.match(printed)
    if m:
        name = _slug(m.group("name"))
        return CPRPage(f"uk/cpr/pd/{name}", printed, url, "pd", name)
    # A few index labels omit the words "Practice Direction" although the H1 has
    # them.  If an unusual current page reaches here, keep it under a deterministic
    # named-PD identity rather than silently dropping official content.
    return CPRPage(f"uk/cpr/pd/{_slug(printed or title)}", printed or title, url, "pd",
                   _slug(printed or title))


def parse_index(html: str) -> list[tuple[str, str]]:
    """Current rule/PD links from the substantive index, in publication order."""
    soup = BeautifulSoup(html, "html.parser")
    rich = soup.select_one("article .rich-text") or soup.select_one("article")
    if rich is None:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in rich.select("a[href]"):
        title = _clean(anchor.get_text(" ", strip=True))
        href = urljoin(INDEX_URL, anchor.get("href") or "")
        parsed = urlparse(href)
        if parsed.netloc not in {"www.justice.gov.uk", "justice.gov.uk"}:
            continue
        # In-page range shortcuts and repeated "to the top" links are navigation,
        # not instruments.  Assets/forms linked from a PD are never on the index.
        if parsed.fragment or not title or title.casefold() == "to the top":
            continue
        href = href.split("#", 1)[0]
        if href.rstrip("/") == INDEX_URL.rstrip("/") or href in seen:
            continue
        seen.add(href)
        out.append((title, href))
    return out


def _updated_date(soup: BeautifulSoup) -> date | None:
    node = soup.select_one(".updated-date")
    match = _UPDATED.search(node.get_text(" ", strip=True) if node else "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%d %B %Y").date()
    except ValueError:
        return None


def _substantive(soup: BeautifulSoup) -> BeautifulSoup | None:
    rich = soup.select_one("article .rich-text")
    if rich is None:
        return None
    # The first figure/table is a local contents list.  Keeping it duplicates every
    # rule heading before the actual consolidated text and creates false citations.
    marker = next((p for p in rich.find_all("p", recursive=False)
                   if _clean(p.get_text(" ", strip=True)).casefold().startswith("contents of")), None)
    if marker is not None:
        nxt = marker.find_next_sibling()
        if nxt is not None and nxt.name in {"figure", "table"}:
            nxt.decompose()
        marker.decompose()
    for node in rich.select("script,style,noscript"):
        node.decompose()
    for anchor in rich.select("a"):
        if _clean(anchor.get_text(" ", strip=True)).casefold() in {"back to top", "back to text"}:
            anchor.decompose()
    return rich


def parse_page(html: str, page: CPRPage) -> tuple[str, list[Segment], date | None, list[str]]:
    """Extract clean text, native rule/paragraph segments, update date and aliases."""
    soup = BeautifulSoup(html, "html.parser")
    rich = _substantive(soup)
    if rich is None:
        return "", [], _updated_date(soup), []

    blocks: list[tuple[str, str]] = []
    for node in rich.find_all(["h2", "h3", "h4", "p", "li", "tr"]):
        # Avoid duplicating list/table cells that are already represented by their
        # parent li/tr block.
        if node.find_parent(["li", "tr"]) is not None:
            continue
        value = _clean(node.get_text(" ", strip=True))
        if value:
            blocks.append((node.name, value))

    text_parts = [page.title, *(value for _, value in blocks)]
    text = "\n\n".join(text_parts).strip()
    segments: list[Segment] = []
    aliases: list[str] = [page.title, page.stable_id]

    cursor = len(page.title) + 2
    for tag, value in blocks:
        start, end = cursor, cursor + len(value)
        cursor = end + 2
        label: str | None = None
        kind = "section"
        if page.family == "part":
            rm = _RULE_NUMBER.match(value)
            if rm:
                number = rm.group("n")
                # Subparagraphs never own a document; their pinpoint stays on the
                # printed rule while the complete token remains in the edge anchor.
                base = number.split("(", 1)[0]
                label, kind = f"rule {number}", "article"
                aliases.append(f"uk/cpr/rule/{base.casefold()}")
        else:
            pm = _PD_PARAGRAPH.match(value)
            if pm:
                label, kind = f"paragraph {pm.group('n')}", "paragraph"
        if label is None and tag in {"h2", "h3", "h4"}:
            label, kind = value, "section"
        if label:
            segments.append(Segment(label=label, char_start=start, char_end=end, kind=kind))

    # Common printed forms are aliases as well as grammar targets.  The generic
    # aliases help imported citations that were normalised before the CPR grammar
    # existed.
    if page.family == "part" and page.code:
        aliases.extend((f"CPR Part {page.code}", f"Part {page.code} of the CPR"))
    elif page.family == "pd" and page.code:
        prefix = "Cyfarwyddyd Ymarfer" if page.language == "cy" else "Practice Direction"
        aliases.extend((f"{prefix} {page.code}", f"PD {page.code}", f"PD{page.code}",
                        f"CPR PD {page.code}"))
    return text, segments, _updated_date(soup), list(dict.fromkeys(a for a in aliases if a))


def _page_hash(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    rich = soup.select_one("article .rich-text")
    body = str(rich) if rich is not None else html
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _response_html(response) -> str:
    content = response.content
    return content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)


class UKCivilProcedureRulesAdapter(BaseAdapter):
    source = "uk-cpr"
    min_interval = 0.25
    requires_js = False
    requires_proxy = False

    def __init__(self, *, ids: str | None = None,
                 client: RateLimitedClient | None = None) -> None:
        self.ids = {x.strip().casefold() for x in (ids or "").split(",") if x.strip()}
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def _wanted(self, page: CPRPage) -> bool:
        if not self.ids:
            return True
        candidates = {page.stable_id.casefold()}
        if page.family == "part" and page.code:
            candidates.add(f"uk/cpr/rule/{page.code}")
        for requested in self.ids:
            if requested in candidates:
                return True
            if requested.startswith("uk/cpr/rule/") and page.family == "part":
                rule = requested.removeprefix("uk/cpr/rule/")
                if rule.split(".", 1)[0].lstrip("0") == (page.code or "").rstrip("abcdefghijklmnopqrstuvwxyz"):
                    return True
        return False

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        index_html = _response_html(self._client.get(INDEX_URL))
        if not self.ids or CPR_ROOT_ID in self.ids:
            root_hash = _page_hash(index_html)
            yield Stub(
                stable_id=CPR_ROOT_ID, landing_url=INDEX_URL, raw_url=INDEX_URL,
                title="Civil Procedure Rules — current consolidated index",
                hints={"html": index_html, "contenthash": root_hash,
                       "page": CPRPage(CPR_ROOT_ID,
                                       "Civil Procedure Rules — current consolidated index",
                                       INDEX_URL, "root")},
            )

        yielded = 0
        for index_title, url in parse_index(index_html):
            preliminary = page_identity(index_title, url)
            if self.ids and not self._wanted(preliminary):
                continue
            actual_url = url
            try:
                page_html = _response_html(self._client.get(actual_url))
            except FetchError as exc:
                # The live index occasionally publishes a root-level PD link while
                # the page actually lives below its owning Part (observed for PD 49H,
                # July 2026). Recover deterministically before giving up; a broken
                # Ministry link must not abort refresh of every later Part/PD.
                fallback = None
                if preliminary.family == "pd" and preliminary.code \
                        and preliminary.code[:1].isdigit():
                    number = re.match(r"\d+", preliminary.code)
                    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
                    if number and tail:
                        fallback = f"{INDEX_URL}/part{int(number.group())}/{tail}"
                if not fallback:
                    log.warning("skipping broken CPR index link %s: %s", url, exc)
                    continue
                try:
                    page_html = _response_html(self._client.get(fallback))
                    actual_url = fallback
                except FetchError as fallback_exc:
                    log.warning("skipping broken CPR index link %s (fallback %s): %s",
                                url, fallback, fallback_exc)
                    continue
            soup = BeautifulSoup(page_html, "html.parser")
            h1 = soup.select_one("article h1")
            # The index is the authoritative identity list. Some substantive pages
            # omit the word "Part" from H1 (currently Part 79), so re-deriving the id
            # from H1 would misclassify a Rule Part as a named PD. Use H1 for the
            # display title only; the index's printed Part/PD code owns identity.
            h1_title = _clean(h1.get_text(" ", strip=True)) if h1 else preliminary.title
            page = CPRPage(preliminary.stable_id, h1_title, actual_url,
                           preliminary.family, preliminary.code, preliminary.language)
            if not self._wanted(page):
                continue
            updated = _updated_date(soup)
            yield Stub(
                stable_id=page.stable_id, landing_url=actual_url, raw_url=actual_url,
                hint_date=updated, title=page.title,
                hints={"html": page_html, "contenthash": _page_hash(page_html), "page": page,
                       **({"watermark": updated.isoformat()} if updated else {})},
            )
            yielded += 1
            # Here a "page" is one official instrument page.  This makes smoke runs
            # useful without changing the all-current default.
            if max_pages is not None and yielded >= max_pages:
                break

    def fetch(self, stub: Stub) -> Record | None:
        html = stub.hints.get("html")
        if not html:
            html = _response_html(self._client.get(stub.raw_url or stub.landing_url))
        page: CPRPage = stub.hints["page"]
        text, segments, updated, aliases = parse_page(html, page)

        relations: list[TypedRelation] = []
        if page.family in {"root", "part"}:
            relations.append(TypedRelation(
                relationship_type=RelationshipType.CONSOLIDATES,
                raw_citation_string="Civil Procedure Rules 1998 (SI 1998/3132)",
                dst_id=CPR_SI_ID, extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))
        elif page.family == "pd":
            relations.append(TypedRelation(
                relationship_type=RelationshipType.RELATED_TO,
                raw_citation_string="Civil Procedure Rules",
                dst_id=CPR_ROOT_ID, extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))

        return Record(
            source=self.source,
            stable_id=page.stable_id,
            doc_type=DocType.LEGISLATION if page.family in {"root", "part"} else DocType.GUIDANCE,
            title=page.title,
            court="Civil Procedure Rule Committee",
            decision_date=updated,
            language=page.language,
            source_language=page.language,
            landing_url=page.url,
            raw_bytes=html.encode("utf-8"),
            raw_ext="html",
            text=text or None,
            segments=segments,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["civil-procedure-rules", "cpr", page.family],
            extra={
                "contenthash": stub.hints.get("contenthash") or _page_hash(html),
                "aliases": aliases,
                "instrument_family": "Civil Procedure Rules",
                "consolidated": True,
                "consolidation_source": "Ministry of Justice",
                "underlying_instrument": CPR_SI_ID,
                "updated_at_source": updated.isoformat() if updated else None,
                "part_or_direction": page.code,
            },
        )
