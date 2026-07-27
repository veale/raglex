"""Financial Conduct Authority decision and final notices from its official sitemap."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Iterator
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

SITEMAP = "https://www.fca.org.uk/sitemap.xml"
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def parse_sitemap(raw: bytes | str) -> tuple[list[str], list[dict]]:
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, TypeError):
        return [], []
    indexes = [
        loc.text.strip() for loc in root.findall(".//s:sitemap/s:loc", NS)
        if loc.text
    ]
    notices: list[dict] = []
    for node in root.findall(".//s:url", NS):
        url = node.findtext("s:loc", namespaces=NS) or ""
        match = re.search(r"/publication/(decision-notices|final-notices)/([^/?#]+\.pdf)$", url, re.I)
        if not match:
            continue
        notices.append({
            "url": url,
            "notice_type": "decision_notice" if match.group(1).lower().startswith("decision") else "final_notice",
            "filename": unquote(match.group(2)),
            "changed": node.findtext("s:lastmod", namespaces=NS),
        })
    return indexes, notices


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def notice_metadata(text: str, fallback: date | None, filename: str) -> dict:
    header = text[:5000]
    published = None
    match = re.search(r"\bDate\s*:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})\b", header, re.I)
    if match:
        try:
            published = datetime.strptime(match.group(1), "%d %B %Y").date()
        except ValueError:
            pass
    subject = None
    for pattern in (
        r"\bTo\s*:\s*([^\n]{2,160})",
        r"\bFINAL NOTICE\s+To\s*:\s*([^\n]{2,160})",
        r"\bDECISION NOTICE\s+To\s*:\s*([^\n]{2,160})",
    ):
        found = re.search(pattern, header, re.I)
        if found:
            subject = " ".join(found.group(1).split()).strip(" :")
            break
    if not subject:
        subject = re.sub(r"[-_]+", " ", PurePosixPath(filename).stem).title()
    ref = re.search(
        r"\b(?:Firm\s+Reference\s+Number|Reference\s+Number|FRN|IRN)\s*:\s*([A-Z0-9/-]+)",
        header, re.I,
    )
    return {
        "date": published or fallback,
        "subject": subject,
        "reference_number": ref.group(1) if ref else None,
    }


class FCANoticesAdapter(BaseAdapter):
    source = "uk-fca-notices"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        queue = [SITEMAP]
        seen_indexes: set[str] = set()
        notices: list[dict] = []
        while queue:
            url = queue.pop(0)
            if url in seen_indexes:
                continue
            seen_indexes.add(url)
            indexes, rows = parse_sitemap(self._client.get(url).content)
            queue.extend(index for index in indexes if index not in seen_indexes)
            notices.extend(rows)
        ordered = sorted(
            notices, key=lambda item: item.get("changed") or "", reverse=True
        )
        # The FCA puts all 5k notices in one sitemap leaf.  Treat max_pages as
        # 100-document result pages, not XML-recursion depth: a first watch run is
        # bounded while every run can still inspect the newest notices.
        if max_pages is not None:
            ordered = ordered[:max_pages * 100]
        for row in ordered:
            changed = row.get("changed")
            if since and changed and changed <= since:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", PurePosixPath(row["filename"]).stem.lower()).strip("-")
            kind = "decision" if row["notice_type"] == "decision_notice" else "final"
            yield Stub(
                stable_id=f"uk/fca/{kind}/{slug}",
                landing_url=row["url"],
                raw_url=row["url"],
                hint_date=_date(changed),
                title=f"{'Decision' if kind == 'decision' else 'Final'} Notice: {slug.replace('-', ' ').title()}",
                court="FCA (UK)",
                hints={
                    **row,
                    "watermark": changed,
                    "contenthash": changed,
                },
            )

    def fetch(self, stub: Stub) -> Record | None:
        raw = self._client.get(stub.raw_url).content
        extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
        text = (extracted.text or "").strip()
        if len(text) < 100:
            return None
        meta = notice_metadata(
            text, stub.hint_date, str(stub.hints.get("filename") or "")
        )
        label = "Decision Notice" if stub.hints.get("notice_type") == "decision_notice" else "Final Notice"
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.DECISION,
            title=f"{label}: {meta['subject']}",
            court="Financial Conduct Authority",
            decision_date=meta["date"],
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["uk", "fca", "enforcement", stub.hints.get("notice_type")],
            extra={
                "jurisdiction": "uk",
                "notice_type": stub.hints.get("notice_type"),
                "subject_name": meta["subject"],
                "reference_number": meta["reference_number"],
                "contenthash": stub.hints.get("contenthash"),
                "require_recognized_legal_citation": True,
            },
        )
