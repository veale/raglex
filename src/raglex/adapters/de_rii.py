"""Germany — rechtsprechung-im-internet.de case-law bulk (``de-rii``), the no-key seed.

The federal-court decision bulk (BVerfG, BGH, BAG, BFH, BSG, BVerwG, BPatG, 2010→),
anonymised, ECLI-native. The bulk seed to ``de-neuris``'s live increment. Two shapes,
like ``de-gii``:

- **ToC-diff** (default): fetch ``rii-toc.xml`` (parallel to gii-toc), whose entries link
  to per-decision XML; pull each once, diff on later runs.
- **Local** (``path=``): read a folder of downloaded rii XML files offline.

Keyed by **ECLI**, so every seeded decision resolves the ``ECLI:DE:`` citations the
extractor already mints. Selected decisions only (a source-scope limit, not a bulk one).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..formats.rii_xml import parse_rii
from ..citations.german import case_alias

TOC_URL = "https://www.rechtsprechung-im-internet.de/rii-toc.xml"


def _read_zip_xml(path: str) -> bytes | None:
    """The decision XML inside a per-decision rii zip (the single .xml member)."""
    import zipfile
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return None
    name = next((n for n in zf.namelist() if n.endswith(".xml")), None)
    return zf.read(name) if name else None


class DeRiiAdapter(BaseAdapter):
    source = "de-rii"
    min_interval = 0.3
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        path: str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.path is not None:
            # the rii download ships one jb-JURE*.zip per decision (each holding the
            # decision XML); a plain folder of loose XML is also supported.
            zips = sorted(self.path.glob("*.zip"))
            if zips:
                for z in zips:
                    yield Stub(stable_id=z.stem, hints={"zip_file": str(z)})
            else:
                for xml in sorted(self.path.rglob("*.xml")):
                    yield Stub(stable_id=xml.stem, hints={"file": str(xml)})
            return
        resp = self._client.get(TOC_URL, raise_for_4xx=False)
        if resp.status_code >= 400:
            return
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return
        # The real rii-toc item carries gericht / entsch-datum (YYYYMMDD) / aktenzeichen /
        # link / modified — but NOT the ECLI (that is only inside the per-decision XML) and
        # its `link` is the HTML landing page. The XML sits at a `zip`-suffixed sibling of
        # that link; verify the exact derivation live before a backfill.
        for item in root.iter():
            if item.tag.rsplit("}", 1)[-1] != "item":
                continue
            link = (item.findtext("link") or "").strip()
            if not link:
                continue
            modified = (item.findtext("modified") or "").strip()
            if since and modified and modified < since:
                continue  # incremental: skip decisions unchanged since the watermark
            xml_url = link if link.endswith((".xml", ".zip")) else link.rstrip("/") + ".zip"
            yield Stub(
                stable_id=link.rstrip("/").rsplit("/", 1)[-1],  # doknr; real ECLI set at fetch
                hint_date=_compact_date(item.findtext("entsch-datum")),
                title=" ".join(x for x in (item.findtext("gericht"),
                                           item.findtext("aktenzeichen")) if x) or None,
                hints={"url": xml_url},
            )

    def fetch(self, stub: Stub) -> Record | None:
        if stub.hints.get("file"):
            data = Path(stub.hints["file"]).read_bytes()
        elif stub.hints.get("zip_file"):
            data = _read_zip_xml(stub.hints["zip_file"])
        elif stub.hints.get("url"):
            resp = self._client.get(stub.hints["url"], raise_for_4xx=False)
            data = resp.content if resp.status_code < 400 else None
        else:
            return None
        if not data:
            return None

        parsed = parse_rii(data)
        ecli = parsed.metadata.get("ecli") or (stub.stable_id if stub.stable_id.startswith("ECLI:") else None)
        stable_id = ecli or stub.stable_id
        court, docket = parsed.metadata.get("court"), parsed.metadata.get("aktenzeichen")
        return Record(
            source=self.source,
            stable_id=stable_id,
            ecli=ecli,
            doc_type=DocType.JUDGMENT,
            title=parsed.title,
            court=parsed.metadata.get("court"),
            decision_date=parsed.decision_date,
            language="de",
            source_language="de",
            landing_url=(parsed.metadata.get("identifier") or stub.hints.get("url")
                         or f"https://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/{stub.stable_id}.zip"),
            raw_bytes=data,
            raw_ext="xml",
            text=parsed.text,
            segments=parsed.segments,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in {
                "aktenzeichen": docket,
                "doktyp": parsed.metadata.get("doktyp"),
                "court_code": parsed.metadata.get("court_code"),
                "court_body": parsed.metadata.get("court_body"),
                "court_location": parsed.metadata.get("court_location"),
                "norms": parsed.metadata.get("norms"),
                "prior_instance": parsed.metadata.get("prior_instance"),
                "region": parsed.metadata.get("region"),
                "publisher": parsed.metadata.get("publisher"),
                "access_rights": parsed.metadata.get("access_rights"),
                "aliases": [case_alias(court, docket)] if court and docket else None,
            }.items() if v},
        )
