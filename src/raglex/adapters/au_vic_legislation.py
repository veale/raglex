"""Current consolidated Victorian legislation from the official JSON:API."""

from __future__ import annotations

from datetime import date
from typing import Iterator
from urllib.parse import urljoin

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

API = "https://content.legislation.vic.gov.au/api/v1"
SITE = "https://www.legislation.vic.gov.au"
INCLUDE = (
    "field_in_force_version,"
    "field_in_force_version.field_in_force_version,"
    "field_in_force_version.field_in_force_version.field_media_file"
)
SPARSE_FIELDS = {
    "fields[node--act_in_force]": (
        "title,field_act_sr_year,field_legislation_year,field_act_sr_number,"
        "changed,path,field_in_force_version"
    ),
    "fields[node--sr_in_force]": (
        "title,field_act_sr_year,field_legislation_year,field_act_sr_number,"
        "changed,path,field_in_force_version"
    ),
    "fields[paragraph--in_force_act_version]": (
        "field_in_force_effective_date,field_in_force_version"
    ),
    "fields[paragraph--in_force_sr_version]": (
        "field_in_force_effective_date,field_in_force_version"
    ),
    "fields[media--document]": "changed,field_media_file",
    "fields[media--pdf]": "changed,field_media_file",
    "fields[file--file]": "changed,filemime,uri,url",
}


def _relationship_ids(resource: dict, name: str) -> list[str]:
    data = (
        ((resource.get("relationships") or {}).get(name) or {}).get("data")
    )
    # Drupal JSON:API represents entity-reference fields as arrays and file fields
    # as a single object.  Normalise both shapes here.
    rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    return [str(row["id"]) for row in rows if row.get("id")]


def parse_vic_page(data: dict, kind: str) -> tuple[list[dict], str | None]:
    included = {row.get("id"): row for row in data.get("included") or []}
    out: list[dict] = []
    for row in data.get("data") or []:
        attrs = row.get("attributes") or {}
        year = str(attrs.get("field_act_sr_year") or attrs.get("field_legislation_year") or "")
        number = str(attrs.get("field_act_sr_number") or "")
        if not (year.isdigit() and number):
            continue
        files = []
        version_ids = _relationship_ids(row, "field_in_force_version")
        version_date = None
        for vid in version_ids:
            version = included.get(vid) or {}
            va = version.get("attributes") or {}
            version_date = va.get("field_in_force_effective_date") or version_date
            media_ids = _relationship_ids(version, "field_in_force_version")
            for mid in media_ids:
                media = included.get(mid) or {}
                file_ids = _relationship_ids(media, "field_media_file")
                for fid in file_ids:
                    f = included.get(fid) or {}
                    fa = f.get("attributes") or {}
                    url = (fa.get("uri") or {}).get("url") or fa.get("url")
                    if url:
                        files.append({
                            "url": urljoin(API, url),
                            "mime": fa.get("filemime"),
                            "changed": fa.get("changed"),
                        })
        path = (attrs.get("path") or {}).get("alias")
        out.append({
            "id": row.get("id"),
            "stable_id": f"au/vic/{'act' if kind == 'act_in_force' else 'regulation'}/{int(year)}/{int(number)}",
            "title": attrs.get("title"),
            "year": int(year),
            "number": int(number),
            "landing_url": urljoin(SITE, path or ""),
            "changed": max(
                [str(attrs.get("changed") or "")] +
                [str(f.get("changed") or "") for f in files]
            ),
            "effective_date": version_date,
            "files": files,
        })
    return out, ((data.get("links") or {}).get("next") or {}).get("href")


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


class VictoriaLegislationAdapter(BaseAdapter):
    source = "au-vic"
    min_interval = 2.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        for kind in ("act_in_force", "sr_in_force"):
            pages = 0
            url = f"{API}/node/{kind}"
            # Without JSON:API sparse fieldsets Drupal expands large metatag and
            # relationship payloads for every included entity (tens of MB per page).
            # Ten compact records keeps both the official endpoint and routine watches
            # responsive while preserving the current-version media chain.
            params = {
                "site": 6,
                "page[limit]": 10,
                # A bounded watch must see the changed end of both registers.
                "sort": "-changed",
                "include": INCLUDE,
                **SPARSE_FIELDS,
            }
            while url:
                data = self._client.get(url, params=params).json()
                params = None
                rows, url = parse_vic_page(data, kind)
                # The register is sorted newest-first. Once a whole page is at or
                # behind the watch cursor, every later page is older: stop instead
                # of silently walking 40 pages while yielding no stubs/progress.
                reached_cursor = bool(
                    since and rows and all(
                        row.get("changed") and row["changed"] <= since for row in rows
                    )
                )
                for row in rows:
                    if since and row["changed"] and row["changed"] <= since:
                        continue
                    yield Stub(
                        stable_id=row["stable_id"],
                        landing_url=row["landing_url"],
                        hint_date=_date(row["effective_date"]),
                        title=row["title"],
                        hints={
                            **row,
                            "watermark": row["changed"],
                            "contenthash": row["changed"],
                        },
                    )
                pages += 1
                if reached_cursor or (max_pages is not None and pages >= max_pages):
                    break

    def fetch(self, stub: Stub) -> Record | None:
        files = stub.hints.get("files") or []
        # Prefer PDF over legacy binary .doc; current entries commonly expose both.
        files = sorted(files, key=lambda f: (0 if "pdf" in str(f.get("mime")).lower() else 1))
        for item in files:
            url = item["url"]
            mime = str(item.get("mime") or "")
            ext = url.rsplit(".", 1)[-1].lower()
            if ext not in {"pdf", "docx", "html", "htm"}:
                continue
            try:
                raw = self._client.get(url).content
                extracted = extract_bytes(raw, ext=ext, mime=mime or None)
            except (FetchError, ValueError):
                continue
            text = (extracted.text or "").strip()
            if len(text) < 100:
                continue
            return Record(
                source=self.source,
                stable_id=stub.stable_id,
                doc_type=DocType.LEGISLATION,
                title=stub.title,
                decision_date=stub.hint_date or date(stub.hints["year"], 1, 1),
                language="en", source_language="en",
                landing_url=stub.landing_url,
                raw_bytes=raw, raw_ext=ext, text=text,
                extracted_via=ExtractedVia.STRUCTURED,
                topic_tags=["legislation", "victoria", "current-consolidation"],
                extra={
                    "jurisdiction": "au-vic",
                    "year": stub.hints.get("year"),
                    "number": stub.hints.get("number"),
                    "effective_date": stub.hints.get("effective_date"),
                    "current_consolidation": True,
                    "download_url": url,
                    "contenthash": stub.hints.get("contenthash"),
                },
            )
        return None
