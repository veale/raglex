"""Official live Canadian decisions from the Lexum/Decisia court portals.

The A2AJ import is the historical seed.  These feeds are the current, authoritative
overlay for the Supreme Court of Canada and Tax Court of Canada: their RSS channels
explicitly publish new, translated, amended and corrected decisions, and the linked
HTML contains the complete judgment plus native paragraph anchors.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterator
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub

_ITEM_ID = re.compile(r"/item/(\d+)/")
_NEUTRAL = re.compile(r"\b((?:19|20)\d{2})\s+(SCC|TCC)\s+(\d+)\b", re.I)
_CHANGE_DATE = re.compile(
    r"(?:published|updated|translated|amended|corrected)\s+on\s+((?:19|20)\d{2}-\d{2}-\d{2})",
    re.I,
)


def neutral_slug(value: str | None) -> str | None:
    m = _NEUTRAL.search(value or "")
    return f"{m.group(2).lower()}/{m.group(1)}/{int(m.group(3))}" if m else None


def parse_rss(raw: bytes | str) -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, TypeError):
        return []
    out: list[dict] = []
    for item in root.findall("./channel/item"):
        title = " ".join((item.findtext("title") or "").split())
        link = (item.findtext("link") or "").strip()
        desc = item.findtext("description") or ""
        decided = item.findtext("{http://lexum.com/decision/}date")
        changed = (_CHANGE_DATE.search(desc).group(1) if _CHANGE_DATE.search(desc) else decided)
        item_id = (_ITEM_ID.search(link).group(1) if _ITEM_ID.search(link) else None)
        if link and item_id:
            out.append({
                "title": title, "link": link, "item_id": item_id,
                "decision_date": decided, "changed": changed,
            })
    return out


def parse_decision_html(raw: bytes | str) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    metadata: dict[str, str] = {}
    for row in soup.select("#decisia-document-header .metadata table tr"):
        cells = row.find_all("td")
        if len(cells) >= 2:
            metadata[" ".join(cells[0].get_text(" ", strip=True).lower().split())] = (
                " ".join(cells[1].get_text(" ", strip=True).split())
            )
    body = soup.select_one("#document-content .documentcontent")
    title_node = soup.select_one("#decisia-document-header h3.title")
    if body is None:
        return {"metadata": metadata, "title": None, "text": "", "segments": []}

    blocks: list[str] = []
    labels: list[str | None] = []
    for node in body.find_all(["p", "li"], recursive=True):
        text = " ".join(node.get_text(" ", strip=True).split())
        if not text:
            continue
        anchor = node.select_one("a.reflex-paragAnchor[name^=par]")
        label = None
        if anchor:
            m = re.search(r"\d+", anchor.get("name") or "")
            label = f"[{int(m.group())}]" if m else None
        blocks.append(text)
        labels.append(label)
    if not blocks:
        blocks = [" ".join(body.get_text(" ", strip=True).split())]
        labels = [None]

    text_parts: list[str] = []
    segments: list[Segment] = []
    pos = 0
    for block, label in zip(blocks, labels):
        if text_parts:
            text_parts.append("\n\n")
            pos += 2
        start = pos
        text_parts.append(block)
        pos += len(block)
        if label:
            segments.append(Segment(label=label, char_start=start, char_end=pos))
    return {
        "metadata": metadata,
        "title": title_node.get_text(" ", strip=True) if title_node else None,
        "text": "".join(text_parts),
        "segments": segments,
    }


def _iso(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


class CanadianLexumAdapter(BaseAdapter):
    min_interval = 1.0

    def __init__(
        self,
        *,
        court: str = "scc",
        client: RateLimitedClient | None = None,
    ) -> None:
        if court not in {"scc", "tcc"}:
            raise ValueError("court must be scc or tcc")
        self.court = court
        self.source = f"ca-{court}-live"
        if court == "scc":
            self.rss_url = "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/rss.do"
        else:
            self.rss_url = "https://decision.tcc-cci.gc.ca/tcc-cci/decisions/en/rss.do"
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=60
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # The official channel is a bounded rolling list, not a pageable back catalogue.
        # It deliberately includes old decisions whose translation/correction changed.
        items = parse_rss(self._client.get(self.rss_url).content)
        for item in items:
            changed = item["changed"]
            if since and changed and changed <= since:
                continue
            provisional = neutral_slug(item["title"]) or f"{self.court}/lexum/{item['item_id']}"
            yield Stub(
                stable_id=provisional,
                landing_url=item["link"],
                raw_url=item["link"] + ("&" if "?" in item["link"] else "?") + "iframe=true",
                hint_date=_iso(item["decision_date"]),
                title=item["title"],
                court=self.court,
                hints={
                    "item_id": item["item_id"],
                    "watermark": changed,
                    # Pipeline compares this to stored metadata, so a correction in the
                    # RSS re-fetches a held bulk/live judgment instead of prefiltering it.
                    "contenthash": changed,
                },
            )

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(stub.raw_url)
        parsed = parse_decision_html(response.content)
        text = parsed["text"].strip()
        meta = parsed["metadata"]
        neutral = (
            meta.get("neutral citation")
            or meta.get("neutral citation / référence neutre")
            or stub.title
        )
        stable_id = neutral_slug(neutral) or stub.stable_id
        if len(text) < 100:
            return None
        decision_date = _iso(meta.get("date")) or stub.hint_date
        landing = stub.landing_url
        return Record(
            source=self.source,
            stable_id=stable_id,
            doc_type=DocType.JUDGMENT,
            title=parsed["title"] or stub.title or stable_id,
            court=self.court,
            decision_date=decision_date,
            language="en",
            source_language="en",
            landing_url=landing,
            raw_bytes=response.content,
            raw_ext="html",
            text=text,
            segments=parsed["segments"],
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["ca-caselaw", f"{self.court}-official"],
            extra={
                "jurisdiction": "ca",
                "lexum_item_id": stub.hints.get("item_id"),
                "neutral_citation": neutral,
                "file_numbers": meta.get("file numbers"),
                "judges": meta.get("judges") or meta.get("judges and taxing officers"),
                "subjects": meta.get("subjects"),
                "contenthash": stub.hints.get("contenthash"),
                "canonical_url": urljoin(landing, landing),
            },
        )

