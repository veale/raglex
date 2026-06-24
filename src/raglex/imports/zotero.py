"""Zotero import (§1.9) — your reference library as secondary corpus material.

Commentary inputs — textbooks, paywalled journal articles you access via your
university, your own notes — are saved for personal research use alongside the
primary corpus (§1.9). Zotero is where most of that lives, so this importer pulls
a Zotero library's items in as secondary ``documents`` (``added_by=user``): the
metadata + abstract become the document, ECLI/CELEX references found in the
title/abstract become dangling ``mentions`` edges that resolve to the cited cases
(§5b), and any PDF attachment can be fetched and text-extracted (§5c).

Zotero Web API: ``GET https://api.zotero.org/{libType}/{libID}/items`` with a
``Zotero-API-Key`` header. The HTTP client is injected so it's testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.models import (
    AddedBy,
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    TypedRelation,
    sha256_bytes,
)
from ..extraction import extract_bytes
from ..resolve.matchers import extract_citation_strings
from ..storage import Catalogue, RawStore, TextStore

API_BASE = "https://api.zotero.org"

# Zotero itemType → our secondary doc_type (§1.9).
_ITEM_TYPE_DOCTYPE = {
    "journalArticle": DocType.ARTICLE,
    "conferencePaper": DocType.ARTICLE,
    "report": DocType.ARTICLE,
    "book": DocType.COMMENTARY,
    "bookSection": DocType.COMMENTARY,
    "thesis": DocType.ARTICLE,
    "blogPost": DocType.COMMENTARY,
    "note": DocType.NOTE,
}


@dataclass(slots=True)
class ZoteroItem:
    key: str
    item_type: str
    title: str
    abstract: str
    creators: str
    date: str | None
    url: str | None
    content_type: str | None  # for attachments


def _parse_item(raw: dict) -> ZoteroItem:
    d = raw.get("data", raw)
    creators = ", ".join(
        " ".join(p for p in (c.get("firstName"), c.get("lastName")) if p) or c.get("name", "")
        for c in d.get("creators", [])
    )
    return ZoteroItem(
        key=d.get("key") or raw.get("key", ""),
        item_type=d.get("itemType", ""),
        title=d.get("title") or d.get("note", "")[:120] or "(untitled)",
        abstract=d.get("abstractNote", "") or "",
        creators=creators,
        date=d.get("date") or None,
        url=d.get("url") or None,
        content_type=d.get("contentType") or None,
    )


@dataclass(slots=True)
class ZoteroImporter:
    http: object  # a client with .get(url, headers=..., params=...) -> resp(.json()/.content)
    library_id: str
    api_key: str
    library_type: str = "users"  # or 'groups'

    def _headers(self) -> dict:
        return {"Zotero-API-Key": self.api_key, "Zotero-API-Version": "3"}

    def fetch_items(self, *, limit: int = 50, start: int = 0) -> list[ZoteroItem]:
        url = f"{API_BASE}/{self.library_type}/{self.library_id}/items"
        resp = self.http.get(
            url, headers=self._headers(), params={"format": "json", "limit": limit, "start": start}
        )
        return [_parse_item(it) for it in resp.json()]

    def fetch_attachment(self, item_key: str) -> bytes | None:
        url = f"{API_BASE}/{self.library_type}/{self.library_id}/items/{item_key}/file"
        try:
            resp = self.http.get(url, headers=self._headers())
        except Exception:
            return None
        return getattr(resp, "content", None)

    def import_into(
        self,
        catalogue: Catalogue,
        rawstore: RawStore,
        textstore: TextStore,
        *,
        limit: int = 50,
        fetch_pdfs: bool = False,
    ) -> list[str]:
        """Import items as secondary documents; returns the stable_ids created."""
        created: list[str] = []
        for item in self.fetch_items(limit=limit):
            if item.item_type in {"attachment"}:
                continue  # handled as a PDF of its parent, not its own document
            created.append(self._import_one(item, catalogue, rawstore, textstore, fetch_pdfs))
        return created

    def _import_one(self, item, catalogue, rawstore, textstore, fetch_pdfs) -> str:
        doc_type = _ITEM_TYPE_DOCTYPE.get(item.item_type, DocType.ARTICLE)
        stable_id = f"zotero:{item.key}"

        text = "\n\n".join(p for p in (item.title, item.abstract) if p)
        raw_path = None
        if fetch_pdfs and (item.content_type == "application/pdf" or True):
            data = self.fetch_attachment(item.key)
            if data:
                extracted = extract_bytes(data, ext="pdf", mime="application/pdf")
                if extracted.text.strip():
                    text = extracted.text
                digest = rawstore.put(data, ext="pdf")
                raw_path = str(rawstore.path_for(digest, "pdf"))

        # ECLI/CELEX references in the metadata → dangling mentions edges (§5b).
        relations = [
            TypedRelation(
                relationship_type=RelationshipType.CITED_BY_COMMENTARY,
                raw_citation_string=cite,
                dst_id=None,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            )
            for cite in extract_citation_strings(f"{item.title}\n{item.abstract}")
        ]

        record = Record(
            source="zotero",
            stable_id=stable_id,
            doc_type=doc_type,
            title=item.title,
            decision_date=None,
            language=None,
            raw_bytes=text.encode("utf-8"),
            raw_ext="txt",
            payload_hash=sha256_bytes(text.encode("utf-8")),
            text=text or None,
            relations=relations,
            extracted_via=ExtractedVia.MANUAL,
            added_by=AddedBy.USER,
            extra={"zotero_key": item.key, "creators": item.creators, "url": item.url},
        )
        text_path = str(textstore.put(record.payload_hash, text)) if text.strip() else None
        catalogue.upsert_document(record, raw_path=raw_path, text_path=text_path)
        return stable_id
