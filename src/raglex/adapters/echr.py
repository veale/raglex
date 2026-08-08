"""European Court of Human Rights adapter — HUDOC (echr.coe.int).

Harvest ECtHR judgments by **either** their ECLI (``ECLI:CE:ECHR:2021:0525JUD005817013``)
**or** their application number (``58170/13``) — the two ways human-rights cases are cited.
The trick that unifies them: the ECHR ECLI *embeds* the application number
(``…JUD005817013`` → app no. 58170/13), so we resolve both through HUDOC's well-supported
``appno`` query, which returns the document ``itemid`` used to fetch the full text.

HUDOC API (no key):
- metadata: ``/app/query/results?query=contentsitename:ECHR AND <field>:"<value>"&select=…``
- full text: ``/app/conversion/docx/html/body?library=ECHR&id=<itemid>`` (HTML)

The stable_id is the ECLI when HUDOC gives one (the canonical, citable key), else a
fallback ``echr/<appno>`` slug. The application number(s) + itemid ride in ``extra``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date as _date
from typing import Iterator

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.case_title import titlecase_case_name
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..core.segmentation import assemble

log = logging.getLogger(__name__)

BASE = "https://hudoc.echr.coe.int"
# Pull the rich HUDOC metadata, not just the bare keys — importance level, the
# conclusion/outcome, the Convention articles engaged, violations found, the respondent
# state, separate opinions, keywords (kpthesaurus), representation, etc. — and keep all
# of it in the document's meta_json (nothing the source gives is discarded).
_META_FIELDS = (
    "itemid", "ecli", "appno", "extractedappno", "docname", "doctype", "doctypebranch",
    "importance", "conclusion", "article", "violation", "nonviolation", "scl",
    "respondent", "separateopinion", "representedby", "issue", "kpthesaurus",
    "judgementdate", "kpdate", "originatingbody", "languageisocode", "rulesofcourt",
)
_SELECT = ",".join(_META_FIELDS)

# ECHR ECLI → application number: ECLI:CE:ECHR:YYYY:MMDD{JUD|DEC|…}{7-digit no}{2-digit yr}
_ECLI_APPNO = re.compile(r"ECHR:\d{4}:\d{4}[A-Z]{2,4}(?P<num>\d{5,7})(?P<yr>\d{2})$", re.IGNORECASE)
_APPNO = re.compile(r"^\d{1,5}/\d{2,4}$")
_ITEMID = re.compile(r"^00[0-9]-\d+$")

# the canonical English Court judgment among the many HUDOC docs for one case
# (judgment vs. legal summary "CLIN" vs. resolution "…RES…").
_JUDGMENT_DOCTYPES = {"HEJUD", "HFJUD", "GRANDCHAMBER", "CHAMBER", "COMMITTEE", "DECGRANDCHAMBER"}


def appno_from_ecli(ecli: str) -> str | None:
    """The application number an ECHR ECLI encodes, e.g.
    ``ECLI:CE:ECHR:2021:0525JUD005817013`` → ``58170/13``."""
    m = _ECLI_APPNO.search(ecli or "")
    if not m:
        return None
    return f"{int(m.group('num'))}/{m.group('yr')}"


def _hudoc_query(value_field: str, value: str) -> str:
    from urllib.parse import quote
    q = f'contentsitename:ECHR AND {value_field}:"{value}"'
    return (f"{BASE}/app/query/results?query={quote(q)}"
            f"&select={_SELECT}&sort={quote('kpdate Descending')}&start=0&length=20")


# -- the recency feed (§keep-current) ---------------------------------------
# The Court's own "latest judgments" RSS (``/app/transform/rss?…``) is a *rendering* of
# this same query endpoint, and a lossy one: it carries a title, an RFC-822 date and an
# itemid, but no ECLI — which is precisely the identifier this adapter files judgments
# under. Following the RSS would mean a second HUDOC round trip per case merely to learn
# its stable_id. So take the query the feed is built from and read it as JSON, where
# ecli/appno/kpdate/doctypebranch all arrive in the same response.
_FEED_COLLECTIONS = ("GRANDCHAMBER", "CHAMBER")
# Press releases and the old Commission series are not judgments of the Court.
_FEED_EXCLUDE_DOCTYPES = ("PR", "HFCOMOLD", "HECOMOLD")
_FEED_PAGE = 500
# HUDOC is Elasticsearch underneath and enforces its default max_result_window: any
# ``start`` at or past 10,000 answers 200 OK with an EMPTY result set and
# ``resultcount: 0`` — not an error, just silence. The Chamber + Grand Chamber corpus is
# 71,067 documents, so a backfill that only pages ``start`` would stop dead at 2018 and
# report success. :meth:`_feed_pages` therefore re-anchors the query to a date window
# whenever it approaches the ceiling instead of paging through it.
_MAX_RESULT_WINDOW = 10000


def _feed_query(
    collections: tuple[str, ...], *, before: str | None = None, extra: str | None = None,
) -> str:
    """The Court's recency query: judgments of the Chamber/Grand Chamber, newest first.

    ``before`` bounds the window at the top (``kpdate:[… TO <date>]``), which is how the
    crawl gets past the 10,000-row ceiling. HUDOC accepts a bracketed range but rejects
    ``kpdate>=…``, so both ends are always given.
    """
    parts = ["contentsitename:ECHR"]
    if _FEED_EXCLUDE_DOCTYPES:
        excluded = " OR ".join(f"doctype={d}" for d in _FEED_EXCLUDE_DOCTYPES)
        parts.append(f"(NOT ({excluded}))")
    if collections:
        wanted = " OR ".join(f'(documentcollectionid="{c}")' for c in collections)
        parts.append(f"({wanted})")
    if before:
        parts.append(f"kpdate:[1959-01-01T00:00:00Z TO {before}T00:00:00Z]")
    if extra:
        parts.append(f"({extra})")
    return " AND ".join(parts)


def _feed_url(query: str, start: int, length: int) -> str:
    from urllib.parse import quote
    return (f"{BASE}/app/query/results?query={quote(query)}"
            f"&select={_SELECT}&sort={quote('kpdate Descending')}"
            f"&start={start}&length={length}")


def _kpdate(columns: dict) -> str:
    """The ISO date HUDOC sorts and filters on (``2026-07-23T00:00:00`` → ``2026-07-23``).

    Strictly ``kpdate`` and never ``judgementdate``, even though the latter is the date a
    lawyer means: kpdate is HUDOC's *publication* date, it is what the feed is ordered by,
    and a cursor has to be denominated in the same units as the ordering it follows.
    They genuinely differ — Big Brother Watch was decided 25/05/2021 and its supervision
    document published 11/12/2024 — so a cursor kept in judgment dates would let a newly
    published document about an old case slip past unseen. ``decision_date`` on the
    record still comes from ``judgementdate`` (see :meth:`fetch`).
    """
    return str(columns.get("kpdate") or "")[:10]


def _case_key(columns: dict) -> str:
    """What makes two HUDOC rows the SAME case rather than two documents.

    A judgment is published as several documents — the English text, the French text,
    sometimes a translation — each with its own itemid but ONE ECLI. Grouping on that is
    what stops the crawl storing a case twice under two ids, and what lets the French
    rendition serve as the fallback body when the English one will not convert.
    """
    return (str(columns.get("ecli") or "").strip()
            or str(columns.get("appno") or "").split(";")[0].strip()
            or str(columns.get("itemid") or "").strip())


def _rank_judgments(rows: list[dict]) -> list[dict]:
    """HUDOC's result set for one case, best document first: the authoritative English
    Court judgment, then the other renditions of the same case (the French text, a
    translation, a legal summary). The tail matters — see :meth:`ECHRAdapter.fetch`."""
    def score(c: dict) -> tuple:
        name = (c.get("docname") or "").upper()
        return (
            (c.get("doctype") or "").upper() in _JUDGMENT_DOCTYPES,  # a judgment doctype
            name.startswith("CASE OF"),                              # the English judgment
            bool(c.get("ecli")),                                     # has an ECLI
            (c.get("languageisocode") or "") == "ENG",
        )
    cols = [r["columns"] for r in rows if r.get("columns")]
    cols = [c for c in cols if not (c.get("docname") or "").upper().startswith(("[", "INFORMATION NOTE"))]
    return sorted(cols, key=score, reverse=True)


def _pick_judgment(rows: list[dict], appno: str | None) -> dict | None:
    """Choose the authoritative English Court judgment from HUDOC's result set."""
    ranked = _rank_judgments(rows)
    return ranked[0] if ranked else None


_PARA_NUM = re.compile(r"^(\d{1,4})\.\s")


def parse_body_html(html: bytes | str) -> tuple[str | None, list]:
    """ECHR judgment HTML → flat text + structural segments on the **numbered paragraphs**
    (``1.``, ``2.``, …), the citable units the Court pinpoints with ``§``. So "§ 35"
    deep-links to paragraph 35 (like CJEU ``§``/UK ``[n]`` paragraphs). Text before the
    first number is the header; the operative part ("FOR THESE REASONS") trails it."""
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style"]):
        s.extract()
    paras = [re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
             for p in (soup.body or soup).find_all(["p", "li"])]
    paras = [p for p in paras if p]
    if not paras:
        return None, []
    blocks: list[tuple[str, str, str]] = []
    label, kind, cur = "Header", "section", []
    for p in paras:
        m = _PARA_NUM.match(p)
        if m:
            if cur:
                blocks.append((label, kind, "\n".join(cur)))
            label, kind, cur = m.group(1), "paragraph", [p[m.end():].strip() or p]
        elif re.match(r"^FOR\s+THESE\s+REASONS", p, re.IGNORECASE) and len(p) < 80:
            if cur:
                blocks.append((label, kind, "\n".join(cur)))
            label, kind, cur = "Operative part", "section", []
        else:
            cur.append(p)
    if cur:
        blocks.append((label, kind, "\n".join(cur)))
    return assemble(blocks)


class ECHRAdapter(BaseAdapter):
    source = "echr"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(self, *, ids: str | tuple[str, ...] | None = None,
                 collections: str | tuple[str, ...] | None = None,
                 query: str | None = None,
                 client: RateLimitedClient | None = None) -> None:
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else ()
        if isinstance(collections, str):
            collections = tuple(c.strip().upper() for c in collections.split(",") if c.strip())
        self.collections = tuple(collections) if collections else _FEED_COLLECTIONS
        self.query = (query or "").strip() or None
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def _lookup(self, ident: str) -> dict | None:
        """Resolve an ECLI / app-number / itemid to a HUDOC judgment's metadata columns."""
        ranked = self._lookup_all(ident)
        return ranked[0] if ranked else None

    def _lookup_all(self, ident: str) -> list[dict]:
        """Every HUDOC document for this identifier, best first (see _rank_judgments)."""
        ident = ident.strip()
        if ident.lower().startswith("echr:"):  # an "echr:<case name>" candidate from the EHRR grammar
            ident = ident[5:].strip()
        appno = None
        if _ITEMID.match(ident):
            field, value = "itemid", ident
        elif (appno := appno_from_ecli(ident) or (ident if _APPNO.match(ident) else None)):
            field, value = "appno", appno
        elif ident.upper().startswith("ECLI:"):
            field, value = "ecli", ident
        else:
            # a case NAME ("Osman v. United Kingdom") — HUDOC has no EHRR-number index, but
            # it does index docname, so we resolve human-rights cases cited only by name/EHRR
            # via a name search. Fuzzier (inferred), but it's the only handle EHRR gives.
            field, value = "docname", re.sub(r"\bv\b\.?", "v.", ident).strip()
        try:
            resp = self._client.get(_hudoc_query(field, value))
        except FetchError:
            return []
        try:
            rows = json.loads(resp.content)["results"]
        except (ValueError, KeyError, TypeError):
            return []
        return _rank_judgments(rows)

    def _stub(self, ranked: list[dict]) -> Stub | None:
        """One case's HUDOC documents (best first) → the stub the pipeline fetches."""
        meta = ranked[0] if ranked else None
        if not meta or not meta.get("itemid"):
            return None
        itemid = meta["itemid"]
        ecli = (meta.get("ecli") or "").strip()
        appnos = (meta.get("appno") or "").replace(";", ", ")
        first_app = (meta.get("appno") or "").split(";")[0]
        stable_id = ecli or (f"echr/{first_app}" if first_app else f"echr/{itemid}")
        # keep every non-empty HUDOC field so nothing the source gives is lost
        meta_kept = {k: v for k, v in meta.items() if v not in (None, "", [])}
        day = _kpdate(meta)
        try:
            hint_date = _date.fromisoformat(day) if day else None
        except ValueError:
            hint_date = None
        return Stub(
            stable_id=stable_id,
            title=meta.get("docname"),
            court="echr",
            hint_date=hint_date,
            landing_url=f"{BASE}/?i={itemid}",
            raw_url=f"{BASE}/app/conversion/docx/html/body?library=ECHR&id={itemid}",
            hints={"itemid": itemid, "appno": appnos, "ecli": ecli,
                   # the other HUDOC documents for this same case, in preference
                   # order — the renditions fetch() falls back to (see below)
                   "alt": [{"itemid": c["itemid"],
                            "lang": (c.get("languageisocode") or "").upper(),
                            "docname": c.get("docname")}
                           for c in ranked[1:] if c.get("itemid")],
                   "date": meta.get("judgementdate") or meta.get("kpdate"),
                   "meta": meta_kept},
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """Named ids when given; otherwise the Court's own recency feed.

        Naming ids is a targeted fetch and stays exact. With none, this walks the
        Chamber/Grand Chamber judgment feed newest-first, which is what lets ``echr`` be
        watched rather than only queried — see :func:`_feed_query`.
        """
        if self.ids:
            for ident in self.ids:
                stub = self._stub(self._lookup_all(ident))
                if stub is not None:
                    yield stub
            return
        yield from self._discover_feed(since, max_pages=max_pages)

    def _feed_pages(self, max_pages: int | None) -> Iterator[list[dict]]:
        """Pages of the recency query, newest first, around the 10,000-row ceiling.

        When ``start`` approaches the ceiling the query is re-anchored to end at the
        oldest date already seen and ``start`` resets to zero, so the walk keeps
        descending instead of running into HUDOC's silent empty page. The re-anchored
        window re-serves that whole day; the caller's ``seen`` set absorbs the overlap,
        which is also what stops a day straddling the boundary from being skipped.
        """
        pages = 0
        before: str | None = None
        start = 0
        oldest: str | None = None
        while max_pages is None or pages < max_pages:
            query = _feed_query(self.collections, before=before, extra=self.query)
            try:
                resp = self._client.get(_feed_url(query, start, _FEED_PAGE))
                rows = json.loads(resp.content).get("results") or []
            except FetchError:
                raise
            except (ValueError, KeyError, TypeError):
                return
            pages += 1
            if not rows:
                return
            columns = [r["columns"] for r in rows if r.get("columns")]
            yield columns
            page_oldest = min((_kpdate(c) for c in columns if _kpdate(c)), default=None)
            if page_oldest and (oldest is None or page_oldest < oldest):
                oldest = page_oldest
            start += _FEED_PAGE
            if start + _FEED_PAGE > _MAX_RESULT_WINDOW:
                if not oldest or oldest == before:
                    return  # one day alone fills the window; no way to descend further
                before, start = oldest, 0

    def _discover_feed(
        self, since: str | None, *, max_pages: int | None = None,
    ) -> Iterator[Stub]:
        cursor = (since or "")[:10]
        seen: set[str] = set()
        # Renditions of one judgment share a kpdate and therefore arrive together, but a
        # page boundary can split them. Hold each day's rows until the day changes, so a
        # case is grouped from ALL its documents rather than from whichever rendition the
        # page happened to end on.
        day: str | None = None
        pending: dict[str, list[dict]] = {}

        def flush() -> Iterator[Stub]:
            for key, group in pending.items():
                if key in seen:
                    continue
                seen.add(key)
                stub = self._stub(_rank_judgments([{"columns": c} for c in group]))
                if stub is not None:
                    yield stub
            pending.clear()

        for columns in self._feed_pages(max_pages):
            for c in columns:
                this_day = _kpdate(c)
                if this_day != day:
                    yield from flush()
                    day = this_day
                # Newest-first, so the first item at or before the cursor means every
                # remaining item is older still: stop rather than walk the archive.
                if cursor and this_day and this_day < cursor:
                    yield from flush()
                    return
                pending.setdefault(_case_key(c), []).append(c)
        yield from flush()

    def _body(self, itemid: str) -> bytes | None:
        """One HUDOC rendition's HTML, or None if it converts to nothing. HUDOC answers
        204 No Content for an item it holds but cannot render — not an error, just empty."""
        resp = self._client.get(f"{BASE}/app/conversion/docx/html/body?library=ECHR&id={itemid}")
        return resp.content or None

    def fetch(self, stub: Stub) -> Record | None:
        try:
            resp = self._client.get(stub.raw_url)
        except FetchError as exc:
            # A transient failure (transport error, 5xx after retries) is NOT an
            # absence — returning None here files a routable reference onto the 90-day
            # harvest-miss list on a blip. Re-raise so the pipeline freezes the cursor
            # and retries; only a genuine 404-class failure counts as "nothing there".
            if exc.transient:
                raise
            return None
        raw = resp.content
        text, segments = parse_body_html(raw) if raw else (None, [])
        lang = "en"
        if not text:
            # HUDOC's docx conversion serves 204 No Content for renditions it can't
            # render, and it does so PERMANENTLY — the English text of Singh v. Belgium
            # has never converted, while the French text of the same ECLI converts fine.
            # Retrying the same itemid for ever (it was filed as "transient") harvested
            # nothing; so walk the other documents HUDOC lists for this case and take the
            # first that has a body. A judgment in French is the same judgment.
            for alt in stub.hints.get("alt") or []:
                try:
                    raw = self._body(alt["itemid"])
                except FetchError as exc:
                    if exc.transient:
                        raise
                    continue
                if not raw:
                    continue
                text, segments = parse_body_html(raw)
                if text:
                    lang = (alt.get("lang") or "").lower()[:2] or "en"
                    break
        if not text:
            # Every rendition converted to nothing, and none of them ERRORED — a
            # transient failure re-raises above, so reaching here means HUDOC answered
            # each one affirmatively with 204 No Content. That is absence, not an
            # outage: this module already records that 204 is permanent (it is why the
            # ``alt`` walk exists at all), and 1982 Commission decisions have no full
            # text in HUDOC and never will. Calling it transient made the pipeline
            # freeze the cursor and retry the same unconvertible records on every run
            # for ever — 1,689 warnings in three days, and the same handful of
            # documents blocking the queue each time. Returning None files it as a
            # genuine miss, which is what it is.
            log.info("no convertible rendition for %s (%d tried) — recording as absent",
                     stub.stable_id, 1 + len(stub.hints.get("alt") or []))
            return None
        # HUDOC dates come in two shapes: ``judgementdate`` as "25/05/2021 00:00:00" and
        # ``kpdate`` as ISO. Accept both — reading only the slashed form left every
        # judgment whose judgementdate is absent (HUDOC leaves it empty on some
        # renditions) with no decision_date at all, and an undated judgment drops out of
        # every date filter, every "newest" sort and the retained-EU-law cutoffs.
        date_raw = (stub.hints.get("date") or "")[:10]
        try:
            dec_date = (
                _date.fromisoformat("-".join(reversed(date_raw.split("/"))))
                if "/" in date_raw else
                (_date.fromisoformat(date_raw) if date_raw else None)
            )
        except ValueError:
            dec_date = None
        ecli = stub.hints.get("ecli") or (stub.stable_id if stub.stable_id.startswith("ECLI:") else None)
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            ecli=ecli,
            doc_type=DocType.JUDGMENT,
            # HUDOC ships docname in upper case; the raw form stays in extra["docname"]
            title=titlecase_case_name(stub.title),
            court="echr",
            decision_date=dec_date,
            language=lang,
            source_language=lang,
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="html",
            text=text,
            segments=segments,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={**(stub.hints.get("meta") or {}), "itemid": stub.hints.get("itemid"),
                   "appno": stub.hints.get("appno"), "format": "hudoc-html"},
        )
