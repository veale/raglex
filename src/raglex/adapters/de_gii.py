"""Germany — gesetze-im-internet.de legislation bulk (``de-gii``), the no-key seed.

The legacy federal-statute bulk: every law as a per-law juris ``gii-norm`` XML file.
Two access shapes, mirroring the ``us-caselaw`` / ``us-caselaw-bulk`` split — this is the
*bulk seed*; ``de-neuris`` is the richer live increment:

- **Local clone** (``path=``): read a checkout of the gesetze-im-internet XML corpus
  (one folder per law, e.g. ``gesetze/zappro/zappro.xml``). Offline enumeration + change
  detection off each file's ``builddate`` — the same "the repo IS the distribution
  channel" pattern as ``ca-federal``.
- **ToC-diff** (no ``path``): fetch ``gii-toc.xml`` (~6,450 entries → per-law XML zips),
  pull each once, then re-fetch and diff on later runs.

Keyed by the familiar abbreviation (``de/gesetz/{jurabk}``, e.g. BGB → ``de/gesetz/bgb``)
so a citation to a German statute resolves against the seed. Only current (in-force)
versions — point-in-time is a NeuRIS-future gap (§2.3). Licence: free reuse.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..formats.gii_xml import parse_gii

TOC_URL = "https://www.gesetze-im-internet.de/gii-toc.xml"
_BASE = "https://www.gesetze-im-internet.de"
_BUILDDATE_RE = re.compile(rb'builddate="(\d{8,14})"')
_JURABK_RE = re.compile(rb"<jurabk[^>]*>([^<]+)</jurabk>")


def _slug(jurabk: str) -> str:
    return "de/gesetz/" + re.sub(r"[^a-z0-9]+", "", jurabk.lower())


def _head_meta(path: Path) -> tuple[str | None, str | None]:
    """Cheap (jurabk, builddate) from a file's head — no full parse for enumeration."""
    head = path.read_bytes()[:4000]
    bd = _BUILDDATE_RE.search(head)
    jb = _JURABK_RE.search(head)
    return (jb.group(1).decode("utf-8", "replace") if jb else None,
            bd.group(1).decode() if bd else None)


class DeGiiAdapter(BaseAdapter):
    source = "de-gii"
    min_interval = 0.3
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        path: str | None = None,
        ids: str | list[str] | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = {i.lower() for i in (ids or [])}
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    # -- discover ----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.path is None:
            yield from self._discover_toc(since)
            return
        # The gii-toc download ships one <law>.zip per law (each holding a single
        # BJNR*.xml); a git clone ships <law>/<law>.xml folders. Detect which we have.
        zips = sorted(self.path.glob("*.zip"))
        if zips:
            yield from self._discover_zipdir(zips)
        else:
            yield from self._discover_local(since)

    def _discover_zipdir(self, zips: list[Path]) -> Iterator[Stub]:
        """A directory of per-law zips (the gii-toc download). The real identity
        (jurabk) is inside the zipped XML, so it is derived at fetch; the stub keys on
        the zip stem, which is also what the `ids` filter matches here."""
        for z in zips:
            stem = z.stem
            if self.ids and stem.lower() not in self.ids:
                continue
            yield Stub(stable_id=stem, hints={"zip_file": str(z)})

    def _discover_local(self, since: str | None) -> Iterator[Stub]:
        root = self.path / "gesetze" if (self.path / "gesetze").is_dir() else self.path
        since_c = (since or "").replace("-", "")[:8]
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            # canonical is <folder>.xml; fall back to any XML incl. the BJNR* file, which
            # in the download format is the ONLY member (same gii-norm content).
            xml = folder / f"{folder.name}.xml"
            if not xml.exists():
                xml = (next((c for c in folder.glob("*.xml")
                             if not c.name.startswith("BJNR")), None)
                       or next(iter(folder.glob("*.xml")), None))
            if xml is None:
                continue
            jurabk, builddate = _head_meta(xml)
            if not jurabk:
                continue
            if self.ids and jurabk.lower() not in self.ids and folder.name not in self.ids:
                continue
            if since_c and builddate and builddate[:8] < since_c:
                continue
            yield Stub(stable_id=_slug(jurabk),
                       hint_date=_compact_date(builddate),
                       hints={"file": str(xml), "jurabk": jurabk, "builddate": builddate})

    def _discover_toc(self, since: str | None) -> Iterator[Stub]:
        """Network seed: the gii ToC lists per-law XML zips."""
        resp = self._client.get(TOC_URL, raise_for_4xx=False)
        if resp.status_code >= 400:
            return
        from xml.etree import ElementTree as ET

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return
        for item in root.iter():
            if item.tag.rsplit("}", 1)[-1] != "item":
                continue
            link = (item.findtext("link") or "").strip()
            title = (item.findtext("title") or "").strip()
            if not link.endswith(".zip"):
                continue
            yield Stub(stable_id=title or link, title=title,
                       hints={"zip_url": link})

    # -- fetch -------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        file = stub.hints.get("file")
        if file:
            data = Path(file).read_bytes()
        elif stub.hints.get("zip_file"):
            data = self._read_local_zip(stub.hints["zip_file"])
        elif stub.hints.get("zip_url"):
            data = self._fetch_zip_xml(stub.hints["zip_url"])
        else:
            return None
        if not data:
            return None

        parsed = parse_gii(data)
        jurabk = parsed.metadata.get("jurabk") or stub.hints.get("jurabk")
        stable_id = _slug(jurabk) if jurabk else stub.stable_id
        return Record(
            source=self.source,
            stable_id=stable_id,
            doc_type=DocType.LEGISLATION,
            title=parsed.title or stub.title,
            decision_date=parsed.decision_date or stub.hint_date,
            language="de",
            source_language="de",
            landing_url=f"{_BASE}/{jurabk.lower()}" if jurabk else _BASE,
            raw_bytes=data,
            raw_ext="xml",
            text=parsed.text,
            segments=parsed.segments,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in {
                "jurabk": jurabk, "doknr": parsed.metadata.get("doknr"),
            }.items() if v},
        )

    def _read_local_zip(self, path: str) -> bytes | None:
        """The gii-norm XML inside a local per-law zip (the single .xml member)."""
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError):
            return None
        name = next((n for n in zf.namelist() if n.endswith(".xml")), None)
        return zf.read(name) if name else None

    def _fetch_zip_xml(self, url: str) -> bytes | None:
        resp = self._client.get(url, raise_for_4xx=False)
        if resp.status_code >= 400 or not resp.content:
            return None
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
        except zipfile.BadZipFile:
            return None
        name = next((n for n in zf.namelist() if n.endswith(".xml")), None)
        return zf.read(name) if name else None


def _compact_date(builddate: str | None):
    if not builddate or len(builddate) < 8:
        return None
    from datetime import date
    try:
        return date(int(builddate[:4]), int(builddate[4:6]), int(builddate[6:8]))
    except ValueError:
        return None
