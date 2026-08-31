"""Estonia — Riigi Teataja, the statute book the Estonian case law cites.

The corpus holds ~393,000 Estonian judgments and **no Estonian legislation at all**. Those
judgments carry 4.07 million statutory citation edges, resolved by lahend.ee to an act
abbreviation and a §, and every one of them is `pending` because the act they name is not
held. `ee/seadus/tsms` alone is the target of 1.94 million edges. The layer that made
lahend.ee worth adopting over scraping — the pre-resolved provision citation — has been
landing in a void; 1,293 distinct acts are cited and none exists.

This adapter fills that void. It is small on purpose: 391 documents that between them
resolve 97% of those edges.

## The interface

Riigi Teataja is an Angular application and **every path under `www.riigiteataja.ee`
returns the same 51,763-byte shell with HTTP 200** — `/akt/106072023004`, `/tutvustus.html`
and an invented path are byte-identical. Nothing about the response says whether the act
exists. The data is behind the SPA's own backend, undocumented but unauthenticated:

```
GET /public-api/api/v1/akt/lyhendid          → [{id, lyhend, pealkiri}, …]  (391 acts)
GET /public-api/api/v1/akt/{id}              → JSON metadata
GET /public-api/api/v1/akt/{id}/blob-xml     → the consolidated text as Juurakt XML
```

`blob-xml` answers `406` to `Accept: application/json` and to `Accept: text/html`; it
needs `application/xml`. That is worth stating because a 406 here is indistinguishable
from a missing act unless you already know the endpoint negotiates.

`lyhendid` is a complete manifest in one request, so discovery costs one call and the
whole feed is enumerable without paging. The `id` it returns is the *currently in force
consolidated version* (`120062026017` encodes 20-06-2026), so re-running the source picks
up re-consolidations: the stable_id is unchanged, the payload differs, and the pipeline
archives the previous text and advances the version.

## The abbreviation is the identity, and two of them collide

`citations.estonian.law_id` mints `ee/seadus/<abbrev>` with diacritics folded away, because
that is the id the judgments' citations already resolve to. Folding makes four of Riigi
Teataja's 391 abbreviations collide in pairs:

| id | Riigi Teataja | |
|---|---|---|
| `ee/seadus/as` | **ÄS** äriseadustik | AS alkoholiseadus |
| `ee/seadus/ros` | **RÕS** riigi õigusabi seadus | ROS rahvusooperi seadus |
| `ee/seadus/kuts` | KutS kutseseadus | KüTS küberturvalisuse seadus |
| `ee/seadus/tuks` | TuKS tunnistajakaitse seadus | TÜKS Tartu Ülikooli seadus |

The first two matter immediately: 37,405 held citations point at `ee/seadus/as` and 12,256
at `ee/seadus/ros`, and in both cases they mean the act in bold, because that is the one
`citations.estonian.ACTS` declares and therefore the one the grammar mints the id from.
Harvesting by folded abbreviation alone, whichever act the manifest happened to list last
would win, and the Commercial Code's 37,405 citations would silently resolve to the
Alcohol Act. Nothing downstream could detect that: the id exists, the document exists, the
edges go green.

So the rule is that a contested id belongs to the abbreviation `ACTS` declares, and a
contest `ACTS` does not settle — `kuts`, `tuks`, which nothing in the corpus cites — is
**not guessed**. Both acts are skipped and named in `unresolved_abbreviations`, because
storing the wrong statute under a cited id is far worse than not storing one.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterator

from ..citations.estonian import ACTS, law_id
from ..core.adapter import BaseAdapter, option_flag, option_int, resume_floor
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..formats.riigiteataja_xml import parse_riigiteataja_xml

BASE = "https://www.riigiteataja.ee/public-api/api/v1"
SITE = "https://www.riigiteataja.ee"
MANIFEST = f"{BASE}/akt/lyhendid"

#: ``lyhendid`` is one request and one page; the constant exists only so the resume
#: arithmetic has a page size to floor against, like every other adapter's.
_PAGE = 25


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _iso(value) -> date | None:
    text = _clean(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def claim_ids(manifest: list[dict]) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Assign each ``ee/seadus/<abbrev>`` id to exactly one act, or to none.

    Returns ``(by_id, contested)`` — the acts that own their id, and the ids more than one
    abbreviation folds onto that ``ACTS`` does not settle. See the module docstring: the
    contested ones are reported, never guessed.
    """
    grouped: dict[str, list[dict]] = {}
    for row in manifest or []:
        abbrev = _clean(row.get("lyhend"))
        if not abbrev or not row.get("id"):
            continue
        grouped.setdefault(law_id(abbrev), []).append(row)

    by_id: dict[str, dict] = {}
    contested: dict[str, list[str]] = {}
    for stable_id, rows in grouped.items():
        if len(rows) == 1:
            by_id[stable_id] = rows[0]
            continue
        # The grammar's own table is the tie-break: whichever abbreviation ACTS declares
        # is the one a judgment's citation was folded from, so it is the one the held
        # edges mean.
        declared = [r for r in rows if _clean(r.get("lyhend")) in ACTS]
        if len(declared) == 1:
            by_id[stable_id] = declared[0]
        else:
            contested[stable_id] = sorted(_clean(r.get("lyhend")) for r in rows)
    return by_id, contested


class EstonianRiigiTeatajaAdapter(BaseAdapter):
    source = "ee-legislation"
    min_interval = 1.0
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        abbreviations: str | list[str] | None = None,
        include_repealed: bool | str | None = None,
        start_offset: int | str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        if isinstance(abbreviations, str):
            abbreviations = [a.strip() for a in abbreviations.split(",") if a.strip()]
        self.abbreviations = [a for a in (abbreviations or []) if a]
        self.include_repealed = option_flag(include_repealed, True)
        # Reported on every stub below, so it must be accepted back — AGENTS.md §1.
        self.start_offset = resume_floor(option_int(start_offset, 0), _PAGE)
        self.unresolved_abbreviations: dict[str, list[str]] = {}
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)

    # -- discovery -----------------------------------------------------------
    def manifest(self) -> list[dict]:
        """The whole statute book's abbreviation index, in one request."""
        response = self._client.get(
            MANIFEST, headers={"Accept": "application/json"})
        # A 200 here means nothing on its own: the SPA shell answers 200 to everything.
        # The manifest is a JSON *array*; anything else is the shell or an error page,
        # and must not read as "Riigi Teataja has no acts".
        try:
            rows = response.json()
        except ValueError as exc:
            raise FetchError(
                f"{self.source}: {MANIFEST} did not return JSON "
                f"({len(response.content)} bytes, {response.headers.get('content-type')})"
            ) from exc
        if not isinstance(rows, list) or not rows:
            raise FetchError(
                f"{self.source}: {MANIFEST} returned {type(rows).__name__} "
                f"with {len(rows) if hasattr(rows, '__len__') else '?'} entries")
        return rows

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        rows = self.manifest()
        by_id, contested = claim_ids(rows)
        self.unresolved_abbreviations = contested

        wanted = {a.casefold() for a in self.abbreviations}
        selected = [
            (stable_id, row) for stable_id, row in by_id.items()
            if not wanted or _clean(row.get("lyhend")).casefold() in wanted
        ]
        # A stable order, so a resume offset means the same position on the next run.
        # The manifest's own order is not documented to be stable and is not alphabetical.
        selected.sort(key=lambda pair: pair[0])

        total = len(selected)
        for offset, (stable_id, row) in enumerate(selected):
            if offset < self.start_offset:
                continue
            if max_pages is not None and offset - self.start_offset >= max_pages * _PAGE:
                return
            abbrev = _clean(row.get("lyhend"))
            yield Stub(
                stable_id=stable_id,
                landing_url=f"{SITE}/akt/{row['id']}",
                title=f"{abbrev} — {_clean(row.get('pealkiri'))}".strip(" —"),
                hints={"akt_id": str(row["id"]), "abbreviation": abbrev,
                       "title": _clean(row.get("pealkiri")),
                       "feed_total": total, "resume_offset": offset},
            )

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        akt_id = _clean(stub.hints.get("akt_id"))
        if not akt_id:
            return None
        meta = self._json(f"{BASE}/akt/{akt_id}") or {}
        params = meta.get("aktiParameetrid") or {}
        status = _clean(meta.get("aktiStaatus"))
        if not self.include_repealed and status.upper().startswith("KEHTETU"):
            return None

        raw = self._xml(f"{BASE}/akt/{akt_id}/blob-xml")
        if raw is None:
            return None
        parsed = parse_riigiteataja_xml(raw)
        if not (parsed.text or "").strip():
            return None

        abbrev = (_clean((parsed.metadata or {}).get("abbreviation"))
                  or _clean(stub.hints.get("abbreviation")))
        title = (_clean(parsed.title) or _clean(params.get("pealkiri"))
                 or _clean(stub.hints.get("title")) or stub.stable_id)
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.LEGISLATION,
            title=f"{title} ({abbrev})" if abbrev else title,
            decision_date=parsed.decision_date,
            language="et",
            source_language="et",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            text=parsed.text,
            segments=parsed.segments,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in {
                "jurisdiction": "ee",
                "abbreviation": abbrev or None,
                # The dated consolidated-version id. It changes on re-consolidation, which
                # is how a routine re-run notices that the text has moved on.
                "rt_id": akt_id,
                "act_kind": (parsed.metadata or {}).get("act_kind"),
                "issuer": (parsed.metadata or {}).get("issuer"),
                "adopted": (parsed.metadata or {}).get("adopted"),
                "sections": (parsed.metadata or {}).get("sections"),
                "status": status or None,
                "in_force_from": _clean(params.get("kehtivuseAlgus")) or None,
                "in_force_to": _clean(params.get("kehtivuseLopp")) or None,
                "published": _clean(params.get("avaldamiseKuupaev")) or None,
                # Estonia's abbreviation IS the citation, so it is also the alias every
                # judgment reaches this act by.
                "aliases": [abbrev] if abbrev else None,
            }.items() if v not in (None, "", [], {})},
        )

    # -- transport -----------------------------------------------------------
    def _json(self, url: str) -> dict | None:
        try:
            response = self._client.get(url, headers={"Accept": "application/json"})
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        try:
            payload = response.json()
        except ValueError:
            # The SPA shell, served with 200. Not an act, and not "no act".
            return None
        return payload if isinstance(payload, dict) else None

    def _xml(self, url: str) -> bytes | None:
        """The consolidated text. ``Accept: application/xml`` is required — the endpoint
        answers 406 to anything else, which would otherwise read as a missing act."""
        try:
            response = self._client.get(url, headers={"Accept": "application/xml"},
                                        raise_for_4xx=False)
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        if response.status_code >= 400:
            return None
        body = response.content or b""
        # The shell is HTML and ~51KB; a real act is XML rooted at <oigusakt>. Checking
        # the body rather than the status is the whole defence here.
        if b"<oigusakt" not in body[:4000]:
            return None
        return body
