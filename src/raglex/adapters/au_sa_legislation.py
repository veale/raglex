"""South Australian current consolidated legislation from the official XML feed.

The Office of Parliamentary Counsel publishes a fortnightly CKAN resource.  Each
outer ZIP contains A.zip and R.zip, whose XML files are the current consolidations
changed in that release.  Walking all releases seeds the corpus; polling resources
newer than the cursor is the live update path that the old bulk-only coverage lacked.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import zipfile
from datetime import date
from typing import Iterator
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub

CKAN_API = "https://data.sa.gov.au/data/api/3/action/package_show"
PACKAGE_ID = "database-update-package-xml"
DATASET_URL = "https://data.sa.gov.au/data/dataset/database-update-package-xml"

_DOCTYPE = re.compile(br"<!DOCTYPE[^>]*>", re.I)


def _iso(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def parse_sa_xml(raw: bytes, *, kind_hint: str | None = None) -> dict | None:
    """Parse one SAOPC Exchange XML consolidation without resolving its external DTD."""
    try:
        root = ET.fromstring(_DOCTYPE.sub(b"", raw))
    except ET.ParseError:
        return None
    year = str(root.attrib.get("year") or "")
    number = str(root.attrib.get("number") or "")
    if not (year.isdigit() and number.isdigit()):
        return None
    title = html.unescape(html.unescape(root.attrib.get("title") or "")).replace(
        "\xa0", " "
    ).strip()
    text = " ".join(" ".join(root.itertext()).replace("\xa0", " ").split())
    if len(text) < 100:
        return None
    raw_class = (root.attrib.get("doc.class") or kind_hint or "act").lower()
    kind = "regulation" if raw_class.startswith(("reg", "rule")) else "act"
    consolidated = root.attrib.get("first.valid.date")
    enacted = root.attrib.get("enact.or.made.date")
    return {
        "stable_id": f"au/sa/{kind}/{int(year)}/{int(number)}",
        "title": title or f"South Australia {kind} {number} of {year}",
        "year": int(year),
        "number": int(number),
        "kind": kind,
        "consolidated": consolidated,
        "enacted": enacted,
        "text": text,
    }


def unpack_sa_release(raw: bytes) -> Iterator[tuple[str, bytes, dict]]:
    """Yield ``(member path, XML bytes, parsed metadata)`` from a release ZIP."""
    try:
        outer = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return
    with outer:
        for inner_name in outer.namelist():
            if inner_name.rsplit("/", 1)[-1].upper() not in {"A.ZIP", "R.ZIP"}:
                continue
            kind = "regulation" if inner_name.rsplit("/", 1)[-1].upper() == "R.ZIP" else "act"
            try:
                inner_raw = outer.read(inner_name)
                inner = zipfile.ZipFile(io.BytesIO(inner_raw))
            except (KeyError, zipfile.BadZipFile):
                continue
            with inner:
                for member in inner.namelist():
                    if not member.lower().endswith(".xml"):
                        continue
                    try:
                        xml = inner.read(member)
                    except KeyError:
                        continue
                    parsed = parse_sa_xml(xml, kind_hint=kind)
                    if parsed:
                        yield member, xml, parsed


class SouthAustraliaLegislationAdapter(BaseAdapter):
    source = "au-sa"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180
        )
        self._documents: dict[str, tuple[bytes, dict]] = {}

    def _resources(self) -> list[dict]:
        response = self._client.get(CKAN_API, params={"id": PACKAGE_ID})
        payload = json.loads(response.content)
        if not payload.get("success"):
            return []
        rows = [
            row for row in payload.get("result", {}).get("resources", [])
            if str(row.get("url") or "").lower().endswith(".zip")
        ]
        return sorted(
            rows,
            key=lambda row: str(row.get("last_modified") or row.get("created") or ""),
            reverse=True,
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        seen: set[str] = set()
        releases = 0
        for resource in self._resources():
            changed = str(resource.get("last_modified") or resource.get("created") or "")
            if since and changed and changed <= since:
                break
            release = self._client.get(resource["url"]).content
            for member, xml, parsed in unpack_sa_release(release):
                stable_id = parsed["stable_id"]
                if stable_id in seen:
                    continue
                seen.add(stable_id)
                digest = hashlib.sha256(xml).hexdigest()
                self._documents[stable_id] = (xml, parsed)
                yield Stub(
                    stable_id=stable_id,
                    landing_url=DATASET_URL,
                    title=parsed["title"],
                    hint_date=_iso(parsed["consolidated"] or parsed["enacted"]),
                    hints={
                        "package_url": resource["url"],
                        "package_name": resource.get("name"),
                        "xml_member": member,
                        "watermark": changed,
                        "contenthash": digest,
                        **{k: v for k, v in parsed.items() if k != "text"},
                    },
                )
            releases += 1
            if max_pages is not None and releases >= max_pages:
                break

    def fetch(self, stub: Stub) -> Record | None:
        cached = self._documents.get(stub.stable_id)
        if cached is None:
            release = self._client.get(stub.hints["package_url"]).content
            cached = next(
                (
                    (xml, parsed)
                    for member, xml, parsed in unpack_sa_release(release)
                    if member == stub.hints.get("xml_member")
                ),
                None,
            )
        if cached is None:
            return None
        raw, parsed = cached
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.LEGISLATION,
            title=parsed["title"],
            decision_date=_iso(parsed["consolidated"] or parsed["enacted"]),
            language="en",
            source_language="en",
            landing_url=DATASET_URL,
            raw_bytes=raw,
            raw_ext="xml",
            text=parsed["text"],
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["legislation", "south-australia", "current-consolidation"],
            extra={
                "jurisdiction": "au-sa",
                "year": parsed["year"],
                "number": parsed["number"],
                "instrument_type": parsed["kind"],
                "enacted_date": parsed["enacted"],
                "effective_date": parsed["consolidated"],
                "current_consolidation": True,
                "is_authoritative": True,
                "package_url": stub.hints.get("package_url"),
                "xml_member": stub.hints.get("xml_member"),
                "contenthash": stub.hints.get("contenthash"),
            },
        )
