"""Official EU financial-regulator enforcement and appeal registers.

The registers are legally rich but mixed-law: none has a safe single default
instrument. Every record therefore opts into the shared recognised-citation gate.
Citation-free entries remain held and deduplicated, but are excluded from search.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub
from ..extraction import extract_bytes

ESMA_SOLR = (
    "https://registers.esma.europa.eu/solr/"
    "esma_registers_sanctions/select"
)
ESA_APPEALS = "https://www.eiopa.europa.eu/decisions-board-appeal_en"
SRB_REGISTER = "https://www.srb.europa.eu/en/cases/thematic-register-search"


def _clean(value: str | None) -> str:
    return " ".join(html.unescape(value or "").replace("\xa0", " ").split())


def _plain_html(value: str | None) -> str:
    return _clean(BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True))


def _iso_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _display_date(value: str | None) -> date | None:
    value = _clean(value)
    for fmt in ("%d %B %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _slug(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").casefold()).strip("-")


def parse_esma_sanctions(payload: bytes | str | dict) -> tuple[int, list[dict]]:
    """Normalise one Solr page while retaining every legally useful field."""
    if not isinstance(payload, dict):
        payload = json.loads(payload)
    response = payload.get("response") or {}
    rows: list[dict] = []
    for raw in response.get("docs") or []:
        doc_id = str(raw.get("id") or "")
        sanction_id = str(
            raw.get("sn_sanctionEsmaID") or doc_id.removeprefix("sn")
        )
        if not sanction_id:
            continue
        original = _clean(raw.get("sn_text"))
        translated = _clean(raw.get("sn_translatedText"))
        entity = _plain_html(raw.get("sn_entityName"))
        framework = _clean(raw.get("sn_sanctionLegalFrameworkName"))
        authority = _clean(raw.get("sn_ncaCodeFullName"))
        country = _clean(raw.get("sn_countryName"))
        nature = _clean(raw.get("sn_natureFullName"))
        changed = _iso_date(raw.get("sn_modificationDate"))
        decided = _iso_date(raw.get("sn_date"))
        heading = " — ".join(filter(None, (
            "ESMA Sanctions Register",
            entity or authority,
            framework,
            decided.isoformat() if decided else None,
        )))
        context = "\n\n".join(filter(None, (
            f"Legal framework: {framework}" if framework else None,
            f"Sanctioning authority: {authority}" if authority else None,
            f"Member State: {country}" if country else None,
            f"Nature: {nature}" if nature else None,
            translated,
            original if not translated or original != translated else None,
        )))
        rows.append({
            "stable_id": f"eu/esma/sanction/{sanction_id}",
            "sanction_id": sanction_id,
            "doc_id": doc_id or f"sn{sanction_id}",
            "title": heading,
            "text": context,
            "entity": entity,
            "framework": framework,
            "authority": authority,
            "country": country,
            "nature": nature,
            "decided": decided,
            "changed": changed,
            "original_language": _clean(raw.get("sn_lan_orig")),
            "translated": bool(translated),
            "raw": raw,
        })
    return int(response.get("numFound") or len(rows)), rows


def parse_esa_appeals(raw: bytes | str) -> list[dict]:
    """Parse the Commission design-system file cards on the ESAs appeal page."""
    soup = BeautifulSoup(raw, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for card in soup.select(".ecl-file"):
        link = card.select_one("a[href*='/document/download/']")
        title_node = card.select_one(".ecl-file__title")
        if link is None or title_node is None:
            continue
        url = urljoin(ESA_APPEALS, str(link.get("href") or ""))
        match = re.search(r"/document/download/([a-f0-9-]+)", url, re.I)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        title = re.sub(
            r"\.pdf$", "", _clean(title_node.get_text(" ", strip=True)), flags=re.I
        )
        published_node = card.select_one(".ecl-file__detail-meta-item")
        published = _display_date(
            published_node.get_text(" ", strip=True) if published_node else None
        )
        decision_match = re.match(r"(\d{4})-(\d{2})-(\d{2})\b", title)
        decided = (
            date(*(int(decision_match.group(i)) for i in range(1, 4)))
            if decision_match else None
        )
        respondent = next(
            (name for name in ("EBA", "EIOPA", "ESMA") if name in title.upper()),
            None,
        )
        out.append({
            "stable_id": f"eu/esas-boa/{match.group(1)}",
            "uuid": match.group(1),
            "title": title,
            "url": url,
            "published": published,
            "decided": decided,
            "respondent": respondent,
        })
    return out


def parse_srb_appeals(raw: bytes | str) -> list[dict]:
    """Parse one page of the SRB Appeal Panel thematic register."""
    soup = BeautifulSoup(raw, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for row in soup.select(".views-row"):
        link = row.select_one("h3 a[href*='.pdf']")
        if link is None:
            continue
        url = urljoin(SRB_REGISTER, str(link.get("href") or ""))
        identity = url.split("?", 1)[0]
        if identity in seen:
            continue
        seen.add(identity)
        case = _clean(link.get_text(" ", strip=True))
        description_node = row.select_one(".srb-case-document__srb-description")
        description = _clean(
            description_node.get_text(" ", strip=True)
            if description_node else None
        )
        published_node = row.select_one(".field-name-srb-publishing-date time")
        decided_node = row.select_one(".field-name-srb-decision-date time")
        published = _iso_date(
            str(published_node.get("datetime") or "")
            if published_node else None
        )
        decided = _iso_date(
            str(decided_node.get("datetime") or "") if decided_node else None
        )
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:14]
        title = f"SRB Appeal Panel — {case}"
        if description:
            title += f" — {description}"
        out.append({
            "stable_id": f"eu/srb/appeal/{digest}",
            "case": case,
            "title": title,
            "description": description,
            "url": url,
            "published": published,
            "decided": decided,
        })
    return out


def _pdf_record(
    *, source: str, stub: Stub, raw: bytes, issuer: str, tags: list[str],
    extra: dict,
) -> Record:
    extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
    text = (extracted.text or "").strip()
    segments = [
        Segment(label=f"p. {page}", char_start=start, char_end=end, kind="page")
        for page, start, end in (extracted.page_spans or [])
    ]
    return Record(
        source=source,
        stable_id=stub.stable_id,
        doc_type=DocType.DECISION,
        title=stub.title,
        court=issuer,
        decision_date=stub.hint_date,
        language="en",
        source_language="en",
        landing_url=stub.landing_url,
        raw_bytes=raw,
        raw_ext="pdf",
        text=text,
        segments=segments,
        extracted_via=ExtractedVia.STRUCTURED,
        topic_tags=["eu", "regulatory", *tags],
        extra={
            "jurisdiction": "eu",
            "issuer": issuer,
            "download_url": stub.raw_url,
            "needs_ocr": extracted.needs_ocr,
            "contenthash": stub.hints.get("contenthash"),
            "require_recognized_legal_citation": True,
            **extra,
        },
    )


class ESMASanctionsAdapter(BaseAdapter):
    source = "eu-esma-sanctions"
    min_interval = 1.0

    def __init__(
        self, *, page_size: int = 200, client: RateLimitedClient | None = None
    ) -> None:
        self.page_size = max(1, min(int(page_size), 500))
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120
        )

    def discover(
        self, since: str | None, *, max_pages: int | None = None
    ) -> Iterator[Stub]:
        start = 0
        pages = 0
        while True:
            params = {
                "q": "*:*",
                "wt": "json",
                "rows": self.page_size,
                "start": start,
                "sort": "sn_modificationDate desc,id asc",
            }
            if since:
                params["fq"] = (
                    f"sn_modificationDate:[{since[:10]}T00:00:00Z TO *]"
                )
            response = self._client.get(ESMA_SOLR, params=params)
            total, rows = parse_esma_sanctions(response.content)
            if not rows:
                return
            for row in rows:
                changed = row["changed"]
                yield Stub(
                    stable_id=row["stable_id"],
                    landing_url=(
                        "https://registers.esma.europa.eu/publication/details"
                        "?core=esma_registers_sanctions"
                        f"&docId={row['doc_id']}"
                    ),
                    raw_url=ESMA_SOLR,
                    hint_date=row["decided"],
                    title=row["title"],
                    court=row["authority"] or "ESMA Sanctions Register",
                    hints={
                        **row,
                        "watermark": changed.isoformat() if changed else None,
                        "contenthash": str(row["raw"].get("_version_") or ""),
                    },
                )
            pages += 1
            start += len(rows)
            if (
                start >= total
                or len(rows) < self.page_size
                or (max_pages is not None and pages >= max_pages)
            ):
                return

    def fetch(self, stub: Stub) -> Record | None:
        row = stub.hints
        text = (row.get("text") or "").strip()
        raw = json.dumps(
            row.get("raw") or {}, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.DECISION,
            title=stub.title,
            court=row.get("authority") or "ESMA Sanctions Register",
            decision_date=stub.hint_date,
            language="en" if row.get("translated") else None,
            source_language=row.get("original_language") or None,
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="json",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=[
                "eu", "regulatory", "enforcement", "esma", "sanctions",
                *([_slug(row.get("framework"))] if row.get("framework") else []),
            ],
            extra={
                "jurisdiction": "eu",
                "issuer": row.get("authority") or "ESMA",
                "sanction_id": row.get("sanction_id"),
                "entity": row.get("entity"),
                "legal_framework": row.get("framework"),
                "member_state": row.get("country"),
                "sanction_nature": row.get("nature"),
                "modification_date": (
                    row["changed"].isoformat() if row.get("changed") else None
                ),
                "contenthash": stub.hints.get("contenthash"),
                "require_recognized_legal_citation": True,
            },
        )


class ESAsBoardOfAppealAdapter(BaseAdapter):
    source = "eu-esas-boa"
    min_interval = 1.5

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180
        )

    def discover(
        self, since: str | None, *, max_pages: int | None = None
    ) -> Iterator[Stub]:
        if max_pages is not None and max_pages <= 0:
            return
        rows = parse_esa_appeals(self._client.get(ESA_APPEALS).content)
        for row in rows:
            watermark = row["published"].isoformat() if row["published"] else None
            if since and watermark and watermark <= since:
                return
            yield Stub(
                stable_id=row["stable_id"],
                landing_url=ESA_APPEALS,
                raw_url=row["url"],
                hint_date=row["decided"] or row["published"],
                title=row["title"],
                court="Joint Board of Appeal of the ESAs",
                hints={
                    **row,
                    "watermark": watermark,
                    "contenthash": row["url"],
                },
            )

    def fetch(self, stub: Stub) -> Record | None:
        raw = self._client.get(stub.raw_url).content
        return _pdf_record(
            source=self.source,
            stub=stub,
            raw=raw,
            issuer="Joint Board of Appeal of the ESAs",
            tags=["financial-services", "esas", "board-of-appeal"],
            extra={
                "document_uuid": stub.hints.get("uuid"),
                "respondent_authority": stub.hints.get("respondent"),
                "publication_date": (
                    stub.hints["published"].isoformat()
                    if stub.hints.get("published") else None
                ),
            },
        )


class SRBAppealPanelAdapter(BaseAdapter):
    source = "eu-srb-appeals"
    min_interval = 1.5

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180
        )

    def discover(
        self, since: str | None, *, max_pages: int | None = None
    ) -> Iterator[Stub]:
        page = 0
        seen: set[str] = set()
        while True:
            response = self._client.get(SRB_REGISTER, params={"page": page})
            rows = parse_srb_appeals(response.content)
            if not rows:
                return
            stop = False
            for row in rows:
                if row["stable_id"] in seen:
                    continue
                seen.add(row["stable_id"])
                watermark = (
                    row["published"].isoformat() if row["published"] else None
                )
                if since and watermark and watermark <= since:
                    stop = True
                    continue
                yield Stub(
                    stable_id=row["stable_id"],
                    landing_url=SRB_REGISTER,
                    raw_url=row["url"],
                    hint_date=row["decided"] or row["published"],
                    title=row["title"],
                    court="Single Resolution Board Appeal Panel",
                    hints={
                        **row,
                        "watermark": watermark,
                        "contenthash": row["url"],
                    },
                )
            page += 1
            if (
                stop
                or (max_pages is not None and page >= max_pages)
                or len(rows) < 20
            ):
                return

    def fetch(self, stub: Stub) -> Record | None:
        raw = self._client.get(stub.raw_url).content
        return _pdf_record(
            source=self.source,
            stub=stub,
            raw=raw,
            issuer="Single Resolution Board Appeal Panel",
            tags=["banking", "resolution", "srb", "appeal-panel"],
            extra={
                "case_number": stub.hints.get("case"),
                "description": stub.hints.get("description"),
                "publication_date": (
                    stub.hints["published"].isoformat()
                    if stub.hints.get("published") else None
                ),
            },
        )
