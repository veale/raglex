"""European Ombudsman decisions from the institution's public REST API."""

from __future__ import annotations

from datetime import date
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub

API = "https://www.ombudsman.europa.eu/rest/documents"


def _text(value: str | None) -> str:
    from bs4 import BeautifulSoup

    return " ".join(BeautifulSoup(value or "", "html.parser").get_text(" ").split())


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


class EUOmbudsmanAdapter(BaseAdapter):
    source = "eu-ombudsman"
    min_interval = 0.5

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        page = 1
        while True:
            data = self._client.get(API, params={
                "page": page, "lang": "en", "format": "EODECISION",
            }).json()
            rows = data.get("documents") or []
            if not rows:
                return
            for row in rows:
                tech = row.get("techKey")
                if not tech:
                    continue
                version = row.get("docVersionContent") or {}
                changed = version.get("updateDate") or row.get("documentDate")
                if since and changed and str(changed) <= since:
                    continue
                yield Stub(
                    stable_id=f"eu/ombudsman/{tech}",
                    landing_url=f"https://www.ombudsman.europa.eu/en/decision/en/{tech}",
                    hint_date=_date(row.get("documentDate")),
                    title=version.get("title"),
                    court="EU Ombudsman",
                    hints={
                        "row": row,
                        "watermark": changed,
                        "contenthash": changed,
                        "feed_total": (data.get("pageItem") or {}).get("totalResult"),
                    },
                )
            page += 1
            if max_pages is not None and page > max_pages:
                return
            total = int((data.get("pageItem") or {}).get("totalResult") or 0)
            if total and page * len(rows) >= total + len(rows):
                return

    def fetch(self, stub: Stub) -> Record | None:
        row = stub.hints.get("row") or {}
        version = row.get("docVersionContent") or {}
        content = _text(version.get("content"))
        summary = _text(version.get("summary"))
        text = "\n\n".join(p for p in (summary, content) if p)
        if len(text) < 50:
            return None
        raw = __import__("json").dumps(row, ensure_ascii=False).encode()
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.DECISION,
            title=_text(version.get("title")) or stub.title,
            court="EU Ombudsman",
            decision_date=_date(row.get("documentDate")),
            language="en",
            source_language=str(version.get("codeIso") or "en").lower(),
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="json",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["eu", "ombudsman", "regulatory"],
            extra={
                "jurisdiction": "eu",
                "case_ref": row.get("caseRef"),
                "case_id": row.get("caseId"),
                "updated_at": version.get("updateDate"),
                "contenthash": stub.hints.get("contenthash"),
                "aliases": [row.get("caseRef")] if row.get("caseRef") else [],
                "require_recognized_legal_citation": True,
            },
        )

