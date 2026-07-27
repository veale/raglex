"""Irish CCPC merger determinations from the official case register."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
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

BASE = "https://www.ccpc.ie"
REGISTER = BASE + "/enforcement-and-regulation/mergers/find-a-merger-case"
COMPETITION_ACT_2002 = "ie/2002/act/14"
_CASE = re.compile(
    r'\{\\"Id\\":\\"(?P<id>[^"]+)\\",'
    r'\\"ItemDefaultUrl\\":\\"(?P<url>[^"]+)\\",'
    r'\\"Title\\":\\"(?P<title>.*?)\\",'
    r'\\"TransactionReference\\":\\"M/(?P<year>\d{2})/(?P<number>\d+)\\",'
    r'\\"MediaMerger\\":(?P<media>true|false),'
    r'\\"NotificationDate\\":\\"(?P<date>\d{4}-\d{2}-\d{2})[^"]*\\"',
    re.S,
)
_BARE_SECTION = re.compile(
    r"\bsection(?:s)?\s+(\d+[A-Za-z]?(?:\(\d+[A-Za-z]?\))*)\b(?!\s+of\b)",
    re.I,
)


def _js_string(value: str) -> str:
    try:
        return json.loads('"' + value.replace('"', '\\"') + '"')
    except json.JSONDecodeError:
        return value.replace("\\u0026", "&").replace("\\/", "/")


def parse_ccpc_register(raw: bytes | str) -> list[dict]:
    """Read the merger records embedded in the official server-rendered page."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    out, seen = [], set()
    for match in _CASE.finditer(text):
        ref = f"M/{match.group('year')}/{int(match.group('number')):03d}"
        if ref in seen:
            continue
        seen.add(ref)
        year = 2000 + int(match.group("year"))
        number = int(match.group("number"))
        out.append({
            "stable_id": f"ie/ccpc/merger/{year}/{number}",
            "reference": ref,
            "title": _js_string(match.group("title")),
            "url": urljoin(BASE, _js_string(match.group("url"))),
            "notification_date": date.fromisoformat(match.group("date")),
            "media_merger": match.group("media") == "true",
        })
    return out


def parse_ccpc_detail(raw: bytes | str) -> dict:
    soup = BeautifulSoup(raw, "html.parser")
    main = soup.select_one("main") or soup.body or soup
    text = "\n".join(
        line for line in (
            " ".join(value.split()) for value in main.get_text("\n").splitlines()
        ) if line
    )
    pdfs: list[str] = []
    for link in main.select("a[href]"):
        href = str(link.get("href") or "")
        label = " ".join(link.get_text(" ", strip=True).split())
        if ".pdf" in href.lower() and (
            "determination" in href.lower() or "determination" in label.lower()
        ):
            pdfs.append(urljoin(BASE, href))
    issued = None
    match = re.search(
        r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
        r"\d{1,2}\s+[A-Za-z]+\s+\d{4})\s+Determination\s+Issued",
        text,
        re.I,
    )
    if match:
        try:
            issued = datetime.strptime(match.group(1), "%A, %d %B %Y").date()
        except ValueError:
            pass
    return {"text": text, "determinations": list(dict.fromkeys(pdfs)), "issued": issued}


def competition_act_relations(text: str) -> list[TypedRelation]:
    """Attach the single-regime merger feed and its genuinely bare sections."""
    relations = [TypedRelation(
        relationship_type=RelationshipType.INTERPRETS,
        raw_citation_string="Competition Act 2002",
        dst_id=COMPETITION_ACT_2002,
        extracted_via=ExtractedVia.STRUCTURED,
        resolution_status=ResolutionStatus.PENDING,
    )]
    seen: set[str] = set()
    for match in _BARE_SECTION.finditer(text):
        anchor = f"section {match.group(1)}"
        if anchor.lower() in seen:
            continue
        # A named foreign/EU law immediately after the section is not unmoored.
        tail = text[match.end():match.end() + 80]
        if re.match(r"\s+(?:of|under)\s+(?:the\s+)?[A-Z]", tail):
            continue
        seen.add(anchor.lower())
        relations.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string=f"{anchor} of the Competition Act 2002",
            dst_id=COMPETITION_ACT_2002,
            dst_anchor=anchor,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))
    return relations


class IrishCCPCMergerAdapter(BaseAdapter):
    source = "ie-ccpc-mergers"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120
        )

    def discover(
        self, since: str | None, *, max_pages: int | None = None
    ) -> Iterator[Stub]:
        rows = parse_ccpc_register(self._client.get(REGISTER).content)
        limit = (max_pages * 10) if max_pages is not None else len(rows)
        for row in rows[:limit]:
            watermark = row["notification_date"].isoformat()
            # Do not stop at the notification watermark: an active older case can
            # acquire its determination weeks later without its notification date
            # changing. Revisit the bounded newest window and let payload dedup make
            # completed cases cheap.
            yield Stub(
                stable_id=row["stable_id"],
                landing_url=row["url"],
                raw_url=row["url"],
                hint_date=row["notification_date"],
                title=f"{row['reference']} — {row['title']}",
                court="CCPC",
                hints={
                    **row,
                    "watermark": watermark,
                    # The detail page changes when the determination is added.
                    "contenthash": None,
                },
            )

    def fetch(self, stub: Stub) -> Record | None:
        detail = parse_ccpc_detail(self._client.get(stub.raw_url).content)
        if not detail["determinations"]:
            # Active notifications remain discoverable and will be retried on the next
            # watch; only operative determinations enter the legal corpus.
            return None
        pdf_url = detail["determinations"][-1]
        raw = self._client.get(pdf_url).content
        extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
        text = (extracted.text or "").strip()
        if len(text) < 100:
            return None
        reference = stub.hints["reference"]
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.DECISION,
            title=stub.title,
            court="CCPC",
            decision_date=detail["issued"] or stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text,
            relations=competition_act_relations(text),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["ie", "competition", "merger-control", "regulatory"],
            extra={
                "jurisdiction": "ie",
                "ccpc_reference": reference,
                "media_merger": stub.hints.get("media_merger"),
                "download_url": pdf_url,
                "regime": COMPETITION_ACT_2002,
                "aliases": [
                    reference,
                    reference.replace("/", "."),
                    reference.replace("/", "-"),
                ],
                "require_recognized_legal_citation": True,
            },
        )
