"""Irish Data Protection Commission decisions and their operative PDFs."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)
from ..extraction import extract_bytes

BASE = "https://www.dataprotection.ie"
LISTING = f"{BASE}/en/dpc-guidance/decisions"
GDPR = "32016R0679"
IE_DPA_2018 = "ie/2018/act/7"


def parse_dpc_listing(raw: bytes | str) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    out, seen = [], set()
    for row in soup.select(".views-row"):
        link = row.select_one("a[href*='/dpc-guidance/decisions/']")
        if not link:
            # A few migrated items live directly below /en/.
            link = row.select_one("a[aria-label^='Read this case study']")
        if not link:
            continue
        url = urljoin(BASE, str(link.get("href") or "").split("#", 1)[0])
        if url in seen:
            continue
        seen.add(url)
        title_link = row.select_one("h2 a, h3 a") or link
        out.append({"url": url, "title": title_link.get_text(" ", strip=True)})
    return out


def parse_dpc_detail(raw: bytes | str) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    body_root = soup.select_one(".field--name-body")
    if not body_root:
        return {}
    title = body_root.find("h1")
    text_body = body_root.select_one(".field--name-body")
    text = "\n".join(
        " ".join(line.split())
        for line in (text_body or body_root).get_text("\n").splitlines()
        if line.strip()
    )
    pdf = None
    for a in body_root.find_all("a", href=True):
        href = str(a["href"])
        if ".pdf" in href.lower():
            pdf = urljoin(BASE, href)
            break
    articles: list[str] = []
    reference = decided = None
    for block in body_root.select(".block-tags"):
        label = " ".join(block.get_text(" ", strip=True).split())
        if label.lower().startswith("articles:"):
            articles = [a.get_text(" ", strip=True) for a in block.find_all("a")]
        elif label.lower().startswith("dpc reference:"):
            reference = label.split(":", 1)[1].strip()
        elif label.lower().startswith("decision date:"):
            decided = label.split(":", 1)[1].strip()
    return {
        "title": title.get_text(" ", strip=True) if title else None,
        "text": text, "pdf": pdf, "articles": articles,
        "reference": reference, "date": decided,
    }


def _date(value: str | None) -> date | None:
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((value or "").strip()[:30], fmt).date()
        except ValueError:
            pass
    return None


def dpc_article_relations(values: list[str]) -> list[TypedRelation]:
    """The DPC's own Articles facet distinguishes GDPR articles from Irish
    Data Protection Act sections with an explicit ``S`` prefix."""
    out: list[TypedRelation] = []
    for value in values:
        token = " ".join(value.split())
        if re.fullmatch(r"S(?:ection)?\s*\d+[A-Za-z]?(?:\([^)]*\))*", token, re.I):
            num = re.sub(r"(?i)^S(?:ection)?\s*", "", token)
            dst, anchor, raw = IE_DPA_2018, f"section {num}", f"section {num} of the Data Protection Act 2018"
        elif re.fullmatch(r"\d+[A-Za-z]?(?:\([^)]*\))*", token):
            dst, anchor, raw = GDPR, f"Article {token}", f"Article {token} GDPR"
        else:
            continue
        out.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            dst_id=dst,
            raw_citation_string=raw,
            dst_anchor=anchor,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))
    return out


class IrishDPCAdapter(BaseAdapter):
    source = "ie-dpc"
    min_interval = 1.5

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        for item in parse_dpc_listing(self._client.get(LISTING).content):
            slug = urlsplit(item["url"]).path.rstrip("/").rsplit("/", 1)[-1]
            yield Stub(
                stable_id=f"ie/dpc/{slug}",
                landing_url=item["url"], raw_url=item["url"],
                title=item["title"], court="dpa-ie",
            )

    def fetch(self, stub: Stub) -> Record | None:
        page = self._client.get(stub.raw_url)
        parsed = parse_dpc_detail(page.content)
        text = str(parsed.get("text") or "")
        raw, ext = page.content, "html"
        if parsed.get("pdf"):
            try:
                pdf = self._client.get(parsed["pdf"]).content
                extracted = extract_bytes(pdf, ext="pdf", mime="application/pdf")
                if (extracted.text or "").strip():
                    text += "\n\n" + extracted.text.strip()
                    raw, ext = pdf, "pdf"
            except (FetchError, ValueError):
                pass
        if len(text) < 50:
            return None
        reference = parsed.get("reference")
        aliases = [reference] if reference else []
        relations = dpc_article_relations(parsed.get("articles") or [])
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.DECISION,
            title=parsed.get("title") or stub.title,
            court="dpa-ie",
            decision_date=_date(parsed.get("date")),
            language="en", source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw, raw_ext=ext, text=text,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["data-protection", "ireland", "regulatory"],
            extra={
                "jurisdiction": "ie",
                "dpc_reference": reference,
                "articles": parsed.get("articles") or [],
                "pdf_url": parsed.get("pdf"),
                "aliases": aliases,
                "require_recognized_legal_citation": True,
            },
        )

