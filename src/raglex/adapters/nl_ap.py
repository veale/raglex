"""Dutch DPA (Autoriteit Persoonsgegevens) publications — its whole document register.

The AP publishes everything it writes into one Drupal view at ``/documenten``: fining
decisions and other sanctions, licence decisions for blacklists, Woo (freedom of
information) decisions, its legislative-advice opinions (*wetgevingstoetsen*), policy
rules, normative interpretations, practical guidance and its annual reports. The view
is a flat newest-first list of ~1,700 items with no type shown on the card, so the
register's own ``document_type`` facet is swept separately to characterise each item —
see ``document_type_index``.

Backfill walks the paginated view; keep-current polls the publication RSS feed, which
is the same list with a 10-item window.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Iterator
from urllib.parse import parse_qsl, urljoin, urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://www.autoriteitpersoonsgegevens.nl"
LISTING = f"{BASE}/documenten"
RSS = f"{BASE}/feed/publication/rss.xml"

_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11,
    "december": 12,
}

# What the AP's own document type means for the corpus. The register mixes three
# genuinely different kinds of document under one view, and the distinction matters for
# retrieval: a *boete* is an administrative decision, a *wetgevingstoets* is the
# authority's opinion on a draft law, and a *handreiking* is guidance. Types not listed
# fall back to guidance, which is what the long tail (voorbeeldbrieven, infographics,
# formulieren, zelf-doen material) is.
DOC_TYPES: dict[str, DocType] = {
    # decisions — the operative, appealable acts
    "besluit": DocType.DECISION,
    "besluit vergunning": DocType.DECISION,
    "besluit op bezwaar": DocType.DECISION,
    "besluit gedragscode": DocType.DECISION,
    "woo-besluit": DocType.DECISION,
    "sanctie": DocType.DECISION,
    "boete": DocType.DECISION,
    "last onder dwangsom": DocType.DECISION,
    "waarschuwing": DocType.DECISION,
    "verwerkingsverbod": DocType.DECISION,
    # the AP's advisory output on draft legislation and codes
    "wetgevingstoets": DocType.OPINION,
    "normuitleg": DocType.OPINION,
    # research and reporting
    "jaarverslag": DocType.PREPARATORY,
    "rapportage": DocType.PREPARATORY,
    "klachtenrapportage": DocType.PREPARATORY,
    "datalekrapportage": DocType.PREPARATORY,
    "onderzoek": DocType.PREPARATORY,
    "extern onderzoek": DocType.PREPARATORY,
    # position pieces that are not guidance and not authority
    "speech": DocType.NOTE,
    "nieuwsbrief": DocType.NOTE,
    "politiek document": DocType.NOTE,
}


def parse_dutch_date(value: str | None) -> date | None:
    """``31 juli 2026`` / ``08 mei 2026`` → a date."""
    match = re.search(r"\b(\d{1,2})\s+([a-zA-Zà-ž]+)\s+((?:19|20)\d{2})\b", value or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(2).casefold())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def document_slug(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]


def stable_id(url: str) -> str:
    return f"nl/ap/{document_slug(url)}"


def document_stubs(html: bytes | str) -> list[Stub]:
    """One stub per card in the ``/documenten`` view.

    The card carries the publication date and whether a file is attached (``PDF, 398
    kB``), so an item whose substance is a PDF is known to be worth a second request
    before the detail page is fetched.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[Stub] = []
    seen: set[str] = set()
    for card in soup.select(".node-publication-card"):
        link = card.select_one(".node-publication-card__link[href], a[href]")
        if not link:
            continue
        url = urljoin(BASE, str(link.get("href") or "").split("#", 1)[0])
        title_node = card.select_one(".node-publication-card__title")
        title = " ".join((title_node or link).get_text(" ", strip=True).split())
        if not url or url in seen or not title:
            continue
        seen.add(url)
        submitted = card.select_one(".node-publication-card__submitted")
        published = parse_dutch_date(submitted.get_text(" ", strip=True)
                                     if submitted else None)
        files = [str(node.get("data-type") or "").casefold()
                 for node in card.select(".node-publication-card__files [data-type]")]
        out.append(Stub(
            stable_id=stable_id(url),
            landing_url=url, raw_url=url, title=title, court="dpa-nl",
            hint_date=published,
            hints={
                "file_types": [f for f in files if f],
                "watermark": published.isoformat() if published else None,
            },
        ))
    return out


def last_page(html: bytes | str) -> int | None:
    """The view's highest ``page=`` index, from its "Laatste" pager link.

    Knowing the end up front turns the backfill from "walk until a page comes back
    empty" into a bounded crawl, and lets the job report real progress.
    """
    soup = BeautifulSoup(html, "html.parser")
    best: int | None = None
    for link in soup.select(".pager__item a[href], .pager a[href]"):
        query = dict(parse_qsl(urlsplit(str(link.get("href") or "")).query))
        try:
            page = int(query.get("page", ""))
        except ValueError:
            continue
        best = page if best is None else max(best, page)
    return best


def facet_tree(html: bytes | str, field: str = "document_type") -> list[dict]:
    """The exposed-filter checkbox tree for ``field``, flattened, deepest known first.

    Returns ``{"id", "label", "count", "depth", "parent"}`` per option. The AP nests its
    types two deep (``Besluit`` → ``Sanctie`` → ``Boete``) and a parent's count includes
    its children, so a document typed *Boete* is returned by all three filters. Depth is
    what lets the sweep assign the narrowest one.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find(
        lambda tag: tag.name == "fieldset" and tag.get("name") == f"{field}[]")
    if root is None:
        return []
    out: list[dict] = []

    def walk(container, depth: int, parent: str | None) -> None:
        for item in container.find_all("li", recursive=False):
            box = item.find("input", attrs={"name": re.compile(rf"^{field}\[")})
            if box is None:
                continue
            value = str(box.get("value") or "")
            label_node = item.find("label")
            label = " ".join((label_node.get_text(" ", strip=True)
                              if label_node else "").split())
            amount = re.search(r"\((\d+)\)\s*$", label)
            out.append({
                "id": value,
                "label": re.sub(r"\s*\(\d+\)\s*$", "", label),
                "count": int(amount.group(1)) if amount else None,
                "depth": depth,
                "parent": parent,
            })
            nested = item.find("ul")
            if nested is not None:
                walk(nested, depth + 1, value)

    top = root.find("ul")
    if top is not None:
        walk(top, 0, None)
    return sorted(out, key=lambda row: -row["depth"])


def type_path(tree: list[dict], type_id: str | None) -> list[str]:
    """``Boete`` → ``["Besluit", "Sanctie", "Boete"]`` — the register's own hierarchy,
    kept so a search for the broad class still finds the narrow document."""
    by_id = {row["id"]: row for row in tree}
    path: list[str] = []
    seen: set[str] = set()
    node = by_id.get(type_id or "")
    while node and node["id"] not in seen:
        seen.add(node["id"])
        path.insert(0, node["label"])
        node = by_id.get(node["parent"] or "")
    return path


def rss_stubs(xml: bytes | str) -> list[Stub]:
    """The publication feed — the keep-current path. Same identity as the view, so a
    feed item and its listing card dedupe onto one document."""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out: list[Stub] = []
    for item in root.iter("item"):
        url = (item.findtext("link") or "").strip()
        if not url:
            continue
        published: date | None = None
        raw_date = (item.findtext("pubDate") or "").strip()
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date).date()
            except (TypeError, ValueError):
                published = None
        out.append(Stub(
            stable_id=stable_id(url), landing_url=url, raw_url=url,
            title=" ".join((item.findtext("title") or "").split()),
            court="dpa-nl", hint_date=published,
            hints={"watermark": published.isoformat() if published else None},
        ))
    return out


def parse_document(html: bytes | str) -> dict:
    """A ``/documenten/<slug>`` page: its summary, themes and attached files.

    Most items are two-step — an intro plus a couple of explanatory paragraphs on the
    page, with the decision or guidance itself as a linked PDF. A minority (older
    policy pages, some Woo decisions) are complete in HTML with no attachment, which
    is why the page text is always kept even when a PDF is found.
    """
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one(".node-publication-full") or soup.select_one("main") or soup
    heading = article.select_one(".node-publication-full__title") or soup.find("h1")
    submitted = article.select_one(".node-publication-full__meta-submitted")
    topics = [
        {"label": " ".join(a.get_text(" ", strip=True).split()),
         "url": urljoin(BASE, str(a.get("href") or ""))}
        for a in article.select(".node-publication-full__topics-item a[href]")
    ]
    parts: list[str] = []
    for selector in (".node-publication-full__intro",
                     ".node-publication-full__primary-content"):
        node = article.select_one(selector)
        if node is None:
            continue
        for tag in node.select("script, style"):
            tag.decompose()
        parts.extend(" ".join(line.split())
                     for line in node.get_text("\n").splitlines() if line.strip())
    files: list[dict] = []
    for link in article.select(".node-publication-full__files a[href]"):
        url = urljoin(BASE, str(link.get("href") or ""))
        if any(row["url"] == url for row in files):
            continue
        files.append({
            "url": url,
            # The download filename is the AP's own name for the document and is often
            # more precise than the page title ("Besluit bulkvergunningaanvraag CWV …").
            "filename": str(link.get("download") or "") or None,
            "kind": str(link.get("data-type") or "").casefold() or None,
        })
    return {
        "title": " ".join(heading.get_text(" ", strip=True).split()) if heading else None,
        "date": submitted.get_text(" ", strip=True) if submitted else None,
        "topics": topics,
        "text": "\n".join(parts).strip(),
        "files": files,
    }


class APDocumentsAdapter(BaseAdapter):
    source = "nl-ap"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)
        self._types: dict[str, dict] = {}

    # ---- discovery -------------------------------------------------------------

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """Backfill walks the view; an incremental run reads the RSS feed instead.

        The RSS feed is a 10-item window on the same list, which is ample for a daily
        poll and costs one request instead of a crawl. It carries no document type, so
        the type index is rebuilt from the *first page* of each type filter — the new
        items are by definition the newest, so page 0 of their own type is where they
        are. A backfill sweeps every page of every type.
        """
        first = self._client.get(LISTING, params={"page": 0})
        tree = facet_tree(first.content)
        incremental = bool(since)
        # The sweep costs one request per ten documents of every type, so it is bounded
        # the same way the crawl is: one page per type for an incremental run (the new
        # items are the newest, so they are on their type's first page), and otherwise
        # whatever depth the caller asked the backfill to go to.
        self._types = self.document_type_index(
            tree, pages=1 if incremental else max_pages)
        if incremental:
            yield from self._annotate(rss_stubs(self._client.get(RSS).content))
            return
        end = last_page(first.content)
        if max_pages is not None:
            end = max_pages - 1 if end is None else min(end, max_pages - 1)
        seen: set[str] = set()
        page = 0
        html = first.content
        while True:
            rows = [s for s in document_stubs(html) if s.stable_id not in seen]
            if not rows and page:
                return
            for stub in self._annotate(rows):
                seen.add(stub.stable_id)
                if end is not None:
                    stub.hints["feed_total"] = (end + 1) * 10
                yield stub
            page += 1
            if end is not None and page > end:
                return
            html = self._client.get(LISTING, params={"page": page}).content

    def _annotate(self, stubs: list[Stub]) -> Iterator[Stub]:
        for stub in stubs:
            characterisation = self._types.get(stub.stable_id)
            if characterisation:
                stub.hints.update(characterisation)
            yield stub

    def document_type_index(
        self, tree: list[dict], *, pages: int | None = None
    ) -> dict[str, dict]:
        """Sweep the ``document_type`` facet → ``{stable_id: {document_type, …}}``.

        The card markup names no type, so the only way to characterise the register is
        to ask it one type at a time. ``tree`` is ordered deepest-first and the first
        filter to claim a document wins, so an item that answers *Boete*, *Sanctie* and
        *Besluit* alike is recorded as the narrowest of the three, with the other two
        kept as its path. ``pages`` bounds each type's crawl (1 = just the newest page,
        which is all an incremental run needs).
        """
        index: dict[str, dict] = {}
        for option in tree:
            if not option["id"]:
                continue
            page = 0
            while True:
                params = {f"document_type[{option['id']}]": option["id"], "page": page}
                try:
                    html = self._client.get(LISTING, params=params).content
                except FetchError:
                    break
                rows = document_stubs(html)
                if not rows:
                    break
                for stub in rows:
                    index.setdefault(stub.stable_id, {
                        "document_type": option["label"],
                        "document_type_id": option["id"],
                        "document_type_path": type_path(tree, option["id"]),
                    })
                page += 1
                if pages is not None and page >= pages:
                    break
                if len(rows) < 10:  # the view is a fixed 10 rows per page
                    break
        return index

    # ---- fetch -----------------------------------------------------------------

    def fetch(self, stub: Stub) -> Record | None:
        try:
            page = self._client.get(stub.raw_url)
        except FetchError:
            return None
        parsed = parse_document(page.content)
        text = str(parsed.get("text") or "")
        raw, ext = page.content, "html"
        attachments: list[dict] = []
        for item in parsed.get("files") or []:
            try:
                blob = self._client.get(item["url"]).content
            except FetchError:
                continue
            kind = item.get("kind") or ("pdf" if blob.startswith(b"%PDF") else None)
            if kind != "pdf":
                continue
            try:
                extracted = extract_bytes(blob, ext="pdf", mime="application/pdf")
            except ValueError:
                continue
            body = (extracted.text or "").strip()
            if body:
                text += "\n\n" + body
                if ext != "pdf":
                    raw, ext = blob, "pdf"
            attachments.append({
                "url": item["url"], "title": item.get("filename"),
                "bytes": len(blob), "text_chars": len(body),
            })
        if len(text.strip()) < 80:
            return None
        label = str(stub.hints.get("document_type") or "")
        topics = [t["label"] for t in parsed.get("topics") or [] if t.get("label")]
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DOC_TYPES.get(label.casefold(), DocType.GUIDANCE),
            title=parsed.get("title") or stub.title,
            court="dpa-nl",
            decision_date=parse_dutch_date(parsed.get("date")) or stub.hint_date,
            language="nl", source_language="nl",
            landing_url=stub.landing_url,
            raw_bytes=raw, raw_ext=ext, text=text.strip(),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["data-protection", "netherlands", *(
                t.casefold() for t in topics)],
            extra={
                "jurisdiction": "nl",
                "ap_document_type": label or None,
                "ap_document_type_path": stub.hints.get("document_type_path") or [],
                "ap_topics": topics,
                "attachments": attachments,
                # The register carries speeches, newsletters and infographics beside its
                # decisions; the gate keeps those out of retrieval while still storing
                # them, and the Dutch grammar is what recognises "artikel 5 AVG" in the
                # ones that are legal documents.
                "require_recognized_legal_citation": True,
            },
        )
