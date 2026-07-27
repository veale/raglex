"""European Commission DG COMP antitrust decision documents.

The Commission's open-data export is a compact live manifest for every AT case.
Only English attachments belonging to formal decisions are ingested; press releases,
market notices and unattached case publicity are deliberately left out.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Iterator

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

DATA_URL = (
    "https://compcases-open-data-portal-files-prod.s3.eu-west-1.amazonaws.com/"
    "case-data-AT.json"
)
CASE_BASE = "https://competition-cases.ec.europa.eu/cases/"
TFEU = "12016E"


def _first(mapping: dict, key: str, default=None):
    value = mapping.get(key)
    return value[0] if isinstance(value, list) and value else default


def _label(value: str | None) -> str:
    try:
        return str(json.loads(value or "{}").get("label") or "")
    except (json.JSONDecodeError, TypeError):
        return str(value or "")


def _iso(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def parse_dgcomp_cases(raw: bytes | str) -> list[dict]:
    payload = json.loads(raw)
    rows: list[dict] = []
    for case_ref, case in payload.items():
        case_meta = case.get("metadata") or {}
        title = _first(case_meta, "caseTitle", case_ref)
        legal_basis = [_label(item) for item in case_meta.get("caseLegalBasis", [])]
        for decision in case.get("decisions") or []:
            meta = decision.get("metadata") or {}
            decision_type = _label(_first(meta, "decisionTypes"))
            adopted = _iso(_first(meta, "decisionAdoptionDate"))
            for attachment in decision.get("decisionAttachments") or []:
                ameta = attachment.get("metadata") or {}
                language = str(
                    _first(ameta, "attachmentLanguage")
                    or _first(ameta, "language")
                    or ""
                ).lower()
                url = _first(ameta, "attachmentLink")
                sequence = _first(ameta, "attachmentIdSequence")
                if language != "en" or not url or not sequence:
                    continue
                published = _iso(
                    _first(ameta, "attachmentPublicationBusinessDate")
                    or _first(ameta, "attachmentDocumentDate")
                )
                category = _label(_first(ameta, "attachmentCategory"))
                rows.append({
                    "stable_id": (
                        f"eu/dgcomp/at/{case_ref.split('.')[-1].lower()}/"
                        f"{str(sequence).lower()}"
                    ),
                    "case_ref": case_ref,
                    "title": title,
                    "decision_type": decision_type,
                    "category": category,
                    "adopted": adopted,
                    "published": published,
                    "url": url,
                    "legal_basis": legal_basis,
                    "sequence": str(sequence),
                })
    return sorted(
        rows,
        key=lambda row: (
            row["published"] or row["adopted"] or date.min,
            row["case_ref"],
            row["sequence"],
        ),
        reverse=True,
    )


def dgcomp_legal_basis_relations(labels: list[str]) -> list[TypedRelation]:
    out: list[TypedRelation] = []
    seen: set[str] = set()
    for label in labels:
        for article in re.findall(r"\bArt(?:icle)?\.?\s*(101|102)\b", label, re.I):
            anchor = f"Article {article}"
            if anchor in seen:
                continue
            seen.add(anchor)
            out.append(TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=f"{anchor} TFEU",
                dst_id=TFEU,
                dst_anchor=anchor,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))
    return out


class DGCompAntitrustAdapter(BaseAdapter):
    source = "eu-dgcomp-antitrust"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180
        )

    def discover(
        self, since: str | None, *, max_pages: int | None = None
    ) -> Iterator[Stub]:
        rows = parse_dgcomp_cases(self._client.get(DATA_URL).content)
        limit = (max_pages * 50) if max_pages is not None else len(rows)
        for row in rows[:limit]:
            changed = row["published"] or row["adopted"]
            watermark = changed.isoformat() if changed else None
            if since and watermark and watermark <= since:
                return
            label = " — ".join(
                part for part in (
                    row["case_ref"], row["title"],
                    row["decision_type"] or row["category"],
                ) if part
            )
            yield Stub(
                stable_id=row["stable_id"],
                landing_url=CASE_BASE + row["case_ref"],
                raw_url=row["url"],
                hint_date=row["adopted"] or row["published"],
                title=label,
                court="European Commission",
                hints={
                    **row,
                    "watermark": watermark,
                    "contenthash": row["url"],
                },
            )

    def fetch(self, stub: Stub) -> Record | None:
        raw = self._client.get(stub.raw_url).content
        extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
        text = (extracted.text or "").strip()
        if len(text) < 100:
            return None
        relations = dgcomp_legal_basis_relations(stub.hints.get("legal_basis") or [])
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.DECISION,
            title=stub.title,
            court="European Commission",
            decision_date=stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["eu", "competition", "antitrust", "regulatory"],
            extra={
                "jurisdiction": "eu",
                "dgcomp_case": stub.hints.get("case_ref"),
                "decision_type": stub.hints.get("decision_type"),
                "document_category": stub.hints.get("category"),
                "legal_basis": stub.hints.get("legal_basis"),
                "download_url": stub.raw_url,
                "contenthash": stub.hints.get("contenthash"),
                "require_recognized_legal_citation": True,
            },
        )
