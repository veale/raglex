"""BEREC's document register — the whole ``/en/all-documents`` tree.

BEREC publishes everything it adopts into a Drupal category tree: opinions, guidelines,
common positions, recommendations, reports, decisions, its own and the BEREC Office's
administrative papers. Each category is a table of 16 rows carrying the BoR document
number, the document date and the title; the document itself is a landing page over one
or more attached files.

The site's ``/en/rss.xml`` is NOT a document feed — see ``RSS_IS_NOT_A_DOCUMENT_FEED``.
Keep-current therefore re-reads the first page of each category, which is newest-first,
and stops at the cursor.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://www.berec.europa.eu"
ROOT = f"{BASE}/en/all-documents"

# Why the obvious feed is not used. ``/en/rss.xml`` is the site's NEWS feed: a live
# sample carried 4 public-consultation pages, 4 news items, 1 task page and 1 event —
# and zero documents from /all-documents. Documents appear in it only when a news or
# task page happens to embed them (the DNA position-papers page embeds 19 PDFs in its
# description HTML), which is incidental, not coverage. There are per-document-type
# feeds at /en/taxonomy/term/<id>/feed/media, but their <link> element points at the
# site root rather than the document, and the term ids are not discoverable from the
# category tree. The category tables are newest-first and cheap, so they are the cursor.
RSS_IS_NOT_A_DOCUMENT_FEED = f"{BASE}/en/rss.xml"

# What BEREC's own category names mean for the corpus. The register mixes normative
# output (guidelines, common positions, opinions the Commission must take utmost account
# of) with meeting agendas and procurement notices, and the distinction is what makes a
# search for BEREC's *position* on something usable.
CATEGORY_DOC_TYPES: tuple[tuple[str, DocType], ...] = (
    ("regulatory-best-practices/guidelines", DocType.GUIDANCE),
    ("regulatory-best-practices/common-approachespositions", DocType.GUIDANCE),
    ("regulatory-best-practices/methodologies", DocType.GUIDANCE),
    ("regulatory-best-practices", DocType.GUIDANCE),
    ("recommendations", DocType.GUIDANCE),
    ("opinions", DocType.OPINION),
    ("berec-decisions", DocType.DECISION),
    ("decisions-of-the-management-board", DocType.DECISION),
    ("annual-reports", DocType.PREPARATORY),
    ("reports", DocType.PREPARATORY),
    ("berec-strategies-and-work-programmes", DocType.PREPARATORY),
    ("berec-office-work-programmes", DocType.PREPARATORY),
    ("berec-office-activity-reports", DocType.PREPARATORY),
    ("public-consultations", DocType.PREPARATORY),
)


def doc_type_for(path: str) -> DocType:
    """Longest matching category segment wins, so ``…/regulatory-best-practices/
    guidelines`` is guidance and not merely its parent's default."""
    trimmed = path.strip("/")
    for suffix, kind in CATEGORY_DOC_TYPES:
        if trimmed.endswith(suffix) or f"/{suffix}/" in f"/{trimmed}/":
            return kind
    return DocType.NOTE


def category_paths(html: bytes | str) -> list[str]:
    """The category tree, from the register's own navigation menu.

    Categories and documents share a URL shape — ``/en/all-documents/berec/opinions`` is
    a category and ``/en/all-documents/berec/number-ranges-update-8-may-2025-pdf`` is a
    document — so they cannot be told apart by the path. The menu block contains only
    categories; the table contains only documents. Read each from the right place.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for link in soup.select(".menu a[href], nav a[href]"):
        href = str(link.get("href") or "").split("#", 1)[0]
        path = urlsplit(urljoin(BASE, href)).path.rstrip("/")
        if not path.startswith("/en/all-documents/") or path in out:
            continue
        out.append(path)
    return out


def document_rows(html: bytes | str) -> list[dict]:
    """One dict per row of a category table."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for row in soup.select("tbody tr"):
        link = row.select_one(".views-field-name a[href]")
        if not link:
            continue
        stamp = row.select_one(".views-field-field-document-date time")
        published: date | None = None
        if stamp is not None and stamp.get("datetime"):
            try:
                published = datetime.fromisoformat(
                    str(stamp["datetime"]).replace("Z", "+00:00")).date()
            except ValueError:
                published = None
        number = row.select_one(".views-field-field-document-number")
        author = row.select_one(".views-field-field-author-of-the-document")
        out.append({
            "url": urljoin(BASE, str(link["href"]).split("#", 1)[0]),
            "title": " ".join(link.get_text(" ", strip=True).split()),
            # "BoR (26) 88_1" — BEREC's own citation for the document.
            "number": " ".join(number.get_text(" ", strip=True).split()) if number else None,
            "date": published,
            "author": " ".join(author.get_text(" ", strip=True).split()) if author else None,
        })
    return out


def last_page(html: bytes | str) -> int | None:
    soup = BeautifulSoup(html, "html.parser")
    best: int | None = None
    for link in soup.select('a[href*="page="]'):
        query = dict(parse_qsl(urlsplit(str(link.get("href") or "")).query))
        try:
            page = int(query.get("page", ""))
        except ValueError:
            continue
        best = page if best is None else max(best, page)
    return best


def parse_document(html: bytes | str, page_url: str) -> dict:
    """A document landing page: its metadata block, summary and attached files."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(".document-container") or soup.select_one("main") or soup
    heading = root.select_one("h1.node-title") or root.find("h1")
    meta: dict[str, str] = {}
    for item in root.select(".info-content"):
        label = item.select_one(".info-title")
        value = item.select_one(".info-details")
        if label and value:
            key = label.get_text(" ", strip=True).rstrip(":").casefold()
            meta[key] = " ".join(value.get_text(" ", strip=True).split())
    body = root.select_one(".field-body")
    summary = "\n".join(
        " ".join(line.split())
        for line in (body.get_text("\n") if body else "").splitlines() if line.strip())
    files: list[dict] = []
    for link in root.select(".doc-info a[href], a.download-button[href]"):
        url = urljoin(page_url, str(link["href"]).split("#", 1)[0])
        if any(row["url"] == url for row in files):
            continue
        files.append({"url": url,
                      "title": " ".join(link.get_text(" ", strip=True).split()) or None})
    return {
        "title": heading.get_text(" ", strip=True) if heading else None,
        "meta": meta,
        "summary": summary,
        "files": files,
    }


def _meta_date(value: str | None) -> date | None:
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(" ".join((value or "").split())[:30], fmt).date()
        except ValueError:
            continue
    return None


def document_alias(number: str | None) -> str | None:
    """``BoR (26) 88_1`` → a normalised alias, so the number as cited resolves."""
    if not number:
        return None
    compact = re.sub(r"\s+", " ", number).strip()
    return compact or None


class BERECAdapter(BaseAdapter):
    source = "eu-berec"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """Walk every category, newest first, stopping at the cursor.

        Each category is its own newest-first series, so the date early-stop is applied
        per category: reaching an old row in *Opinions* says nothing about *Reports*.
        An incremental run therefore costs one page per category.
        """
        try:
            root = self._client.get(ROOT)
        except FetchError:
            return
        categories = category_paths(root.content)
        seen: set[str] = set()
        for path in categories:
            page = 0
            end: int | None = None
            while max_pages is None or page < max_pages:
                try:
                    response = self._client.get(f"{BASE}{path}", params={"page": page})
                except FetchError:
                    break
                if end is None:
                    end = last_page(response.content)
                rows = document_rows(response.content)
                if not rows:
                    break
                stale = False
                for row in rows:
                    published = row.get("date")
                    if since and published and published.isoformat() < since[:10]:
                        # newest-first, so everything below this row is older too
                        stale = True
                        break
                    key = f"eu/berec/{_slug(row['url'])}"
                    if key in seen:
                        continue
                    seen.add(key)
                    yield Stub(
                        stable_id=key,
                        landing_url=row["url"], raw_url=row["url"],
                        title=row["title"], court="BEREC",
                        hint_date=published,
                        hints={
                            "category": path.removeprefix("/en/all-documents/"),
                            "number": row.get("number"),
                            "author": row.get("author"),
                            "watermark": published.isoformat() if published else None,
                        },
                    )
                if stale:
                    break
                page += 1
                if end is not None and page > end:
                    break

    def fetch(self, stub: Stub) -> Record | None:
        try:
            response = self._client.get(stub.raw_url)
        except FetchError:
            return None
        parsed = parse_document(response.content, str(response.url))
        text = str(parsed.get("summary") or "")
        raw, ext = response.content, "html"
        attachments: list[dict] = []
        for item in parsed.get("files") or []:
            # The register attaches spreadsheets (number-range exports) beside its PDFs;
            # only the PDFs carry text worth extracting, but the rest are still recorded
            # so the document's full apparatus is visible.
            if ".pdf" not in urlsplit(item["url"]).path.casefold():
                attachments.append({"url": item["url"], "title": item.get("title"),
                                    "bytes": None, "text_chars": 0})
                continue
            try:
                blob = self._client.get(item["url"]).content
                extracted = extract_bytes(blob, ext="pdf", mime="application/pdf")
            except (FetchError, ValueError):
                continue
            body = (extracted.text or "").strip()
            if body:
                text = f"{text}\n\n{body}".strip()
                if ext != "pdf":
                    raw, ext = blob, "pdf"
            attachments.append({"url": item["url"], "title": item.get("title"),
                                "bytes": len(blob), "text_chars": len(body)})
        if len(text.strip()) < 120:
            return None
        meta = parsed.get("meta") or {}
        number = meta.get("document number") or stub.hints.get("number")
        published = (_meta_date(meta.get("document date")) or stub.hint_date)
        alias = document_alias(number)
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=doc_type_for(str(stub.hints.get("category") or "")),
            title=parsed.get("title") or stub.title,
            court="BEREC",
            decision_date=published,
            language="en", source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw, raw_ext=ext, text=text.strip(),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["berec", "electronic-communications", "telecoms"],
            extra={
                "jurisdiction": "eu",
                "category": stub.hints.get("category"),
                "document_number": number,
                "registered": meta.get("date of registration"),
                "document_author": meta.get("author") or stub.hints.get("author"),
                "berec_document_type": meta.get("document type"),
                "attachments": attachments,
                "aliases": [alias] if alias else [],
                "require_recognized_legal_citation": False,
            },
        )


def _slug(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return path.removeprefix("/en/all-documents/").strip("/") or "document"
