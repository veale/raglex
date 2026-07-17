"""Article 29 Working Party archives (§1.9/§4a) — the EDPB's predecessor body
(1997–2018), whose WP-numbered papers are still constantly cited ("WP29 guidance",
"WP248 rev.01"). The body dissolved in May 2018, so both surfaces are **closed
archives**: there is no incremental cursor to keep — a harvest is a one-shot
backfill, and a re-run dedups against what's held.

Two surfaces, one source (``a29wp``):

**Justice archive** — the old static index at
``ec.europa.eu/justice/article-29/documentation/opinion-recommendation/index_en.htm``:
one page, ~250 opinions/recommendations 1997–2016, grouped under ``<h2>{year}</h2>``
lists whose links go straight to ``files/{year}/wp240_en.pdf``. The WP number is in
the filename; the ``<span>`` text is a clean title ("Opinion 03/2016 on the
evaluation and review of the ePrivacy Directive").

**Newsroom archive** — ``ec.europa.eu/newsroom/article29``: the 2016–2018 items
(guidelines, opinions, letters, press releases, plenary records) discovered via the
per-item-type RSS feeds (seven types, ~122 items — the per-type feeds also give each
item its kind, which the unfiltered feed omits). Each feed item links to an item
*page*, which in turn links to its attachments via ``redirection/document/{id}`` —
the EN PDF among them is the document body. Feed ``pubDate`` is the *newsroom upload*
date (often years after adoption), so it is deliberately ignored; the adoption date
comes from the PDF text via guidance classification.

Identity: a WP number in the filename or title keys the stable_id (``a29wp/wp240``)
— so the same paper reached via both surfaces lands once — and mints the ``wp240`` /
``wp 240`` aliases citations resolve against. Items with no WP number key by
newsroom item id. Early-years PDFs can be scans: the EDPB adapter's OCR detection
and tesseract fallback apply here too, as does its WAF handling (same europa.eu WAF,
same slow pacing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterator
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from .edpb import looks_unocrd, ocr_pdf, waf_get, _pdf_pages

JUSTICE_BASE = "https://ec.europa.eu"
JUSTICE_INDEX = (
    "https://ec.europa.eu/justice/article-29/documentation/opinion-recommendation/index_en.htm"
)
NEWSROOM_BASE = "https://ec.europa.eu/newsroom/article29"

# Newsroom item types (from the items landing page). The per-type feeds are the
# discovery surface — the type tells us what each item IS, which the unfiltered
# feed doesn't. Press releases and plenary agendas/minutes are context, not
# guidance, so they type as notes.
NEWSROOM_TYPES: dict[int, tuple[str, DocType]] = {
    1306: ("press-release", DocType.NOTE),
    1307: ("letter", DocType.GUIDANCE),
    1308: ("opinion", DocType.GUIDANCE),
    1309: ("plenary", DocType.NOTE),
    1310: ("consultation", DocType.GUIDANCE),
    1358: ("a29wp-document", DocType.GUIDANCE),
    1360: ("guidelines", DocType.GUIDANCE),
}

_WP_NUM = re.compile(r"\bWP\s?(?P<num>\d{2,3})(?:\s?rev\.?\s?0?(?P<rev>\d+))?\b", re.IGNORECASE)


# ── justice archive (pure parsers) ──────────────────────────────────────────
_JUSTICE_SECTION = re.compile(r"<h2>(?P<year>(?:19|20)\d{2})</h2>(?P<body>.*?)(?=<h2>|\Z)", re.S)
_JUSTICE_ITEM = re.compile(
    r'<a[^>]+href="(?P<href>[^"]*?/files/\d{4}/[^"]+\.pdf)"[^>]*>\s*(?:<span>)?(?P<title>.*?)(?:</span>|</a>)',
    re.S,
)


@dataclass(frozen=True, slots=True)
class JusticeDoc:
    pdf_url: str      # absolute
    title: str
    year: int
    stem: str         # wp240 | wp179_update — the filename identity, language stripped


def parse_justice_index(html: str) -> list[JusticeDoc]:
    import html as _html

    out: list[JusticeDoc] = []
    seen: set[str] = set()
    for sec in _JUSTICE_SECTION.finditer(html):
        year = int(sec.group("year"))
        for m in _JUSTICE_ITEM.finditer(sec.group("body")):
            href = m.group("href")
            title = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", m.group("title")))).strip()
            stem = href.rsplit("/", 1)[-1].removesuffix(".pdf")
            stem = re.sub(r"_(?:en|fr|de|es|it|nl|pl|pt)(?=_|$)", "", stem, flags=re.IGNORECASE)
            if not stem or stem in seen:
                continue
            seen.add(stem)
            out.append(JusticeDoc(
                pdf_url=href if href.startswith("http") else f"{JUSTICE_BASE}{href}",
                title=title or stem, year=year, stem=stem.lower(),
            ))
    return out


# ── newsroom (pure parsers) ─────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class NewsItem:
    item_id: str
    title: str
    page_url: str


def parse_newsroom_feed(xml_bytes: bytes) -> list[NewsItem]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out: list[NewsItem] = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        m = re.search(r"redirection/item/(\d+)", link)
        if not m:
            continue
        out.append(NewsItem(
            item_id=m.group(1),
            title=re.sub(r"\s+", " ", item.findtext("title") or "").strip(),
            page_url=f"{NEWSROOM_BASE}/items/{m.group(1)}/en",
        ))
    return out


_NEWS_DOC = re.compile(r'href="(?P<href>[^"]*redirection/document/\d+[^"]*)"')


def parse_newsroom_item(html: str) -> dict:
    """The item page's attachments, each with the visible label preceding its download
    link ("WP225_EN English (326 KB - PDF)") — the label carries language + filetype,
    which is how the EN PDF is chosen over the all-languages ZIP."""
    import html as _html

    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    docs, seen = [], set()
    for m in _NEWS_DOC.finditer(html):
        href = _html.unescape(m.group("href"))
        if href in seen:
            continue
        seen.add(href)
        ctx = re.sub(r"<[^>]+>", " ", html[max(0, m.start() - 900):m.start()])
        ctx = re.sub(r"\s+", " ", _html.unescape(ctx)).strip()
        docs.append({"href": href, "label": ctx[-160:]})
    return {
        "title": re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", h1.group(1)))).strip() if h1 else None,
        "docs": docs,
    }


def pick_en_pdf(docs: list[dict]) -> dict | None:
    """The English PDF among an item's attachments: label says PDF and EN/English;
    fall back to any PDF, then to the first attachment at all."""
    def _is_pdf(d):
        return "PDF" in d["label"].upper()

    def _is_en(d):
        lab = d["label"].upper()
        return "_EN" in lab or "ENGLISH" in lab

    return (next((d for d in docs if _is_pdf(d) and _is_en(d)), None)
            or next((d for d in docs if _is_pdf(d)), None)
            or (docs[0] if docs else None))


def sentence_case(title: str) -> str:
    """The newsroom shouts its titles (all caps). Sentence-case them, preserving
    tokens that carry identity — anything with a digit or a slash (WP225, C-131/12,
    95/46/EC) stays exactly as written. Mixed-case titles pass through untouched."""
    letters = [c for c in title if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.8:
        return title
    words = title.split()
    out = []
    for i, w in enumerate(words):
        if any(ch.isdigit() for ch in w) or "/" in w:
            out.append(w)
        elif i == 0:
            out.append(w.capitalize())
        else:
            out.append(w.lower())
    return " ".join(out)


class A29WPAdapter(BaseAdapter):
    source = "a29wp"
    min_interval = 6.0   # same europa.eu WAF as the EDPB — same slow drip
    requires_js = False
    requires_proxy = False

    def __init__(self, *, surface: str = "both",
                 client: RateLimitedClient | None = None) -> None:
        self.surface = surface if surface in ("justice", "newsroom", "both") else "both"
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def _get(self, url: str, *, expect_pdf: bool = False):
        return waf_get(self._client, self.source, url, expect_pdf=expect_pdf)

    @staticmethod
    def _wp_id(text: str) -> tuple[str | None, list[str]]:
        """WP-number identity from a filename stem or title → (stable-id tail, aliases)."""
        m = _WP_NUM.search(text or "")
        if not m:
            return None, []
        num, rev = m.group("num"), m.group("rev")
        tail = f"wp{num}" + (f"-rev{int(rev):02d}" if rev else "")
        aliases = [f"wp{num}", f"wp {num}"] if not rev else [f"wp{num} rev.{int(rev):02d}"]
        return tail, aliases

    # -- discovery (a closed archive: no cursor; max_pages doesn't apply — the
    # -- listing cost is fixed at 1 index page + 7 type feeds) ------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        seen: set[str] = set()
        if self.surface in ("justice", "both"):
            # A transient failure fetching the index must not crash the whole run (the
            # job would error instead of simply retrying next time); a RateLimitException
            # still propagates so the pipeline pauses the source correctly.
            try:
                resp = self._get(JUSTICE_INDEX)
            except FetchError:
                resp = None
            html = "" if resp is None else (
                resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else str(resp.content))
            for d in parse_justice_index(html):
                tail, aliases = self._wp_id(d.stem)
                sid = f"a29wp/{tail or d.stem}"
                if sid in seen:
                    continue
                seen.add(sid)
                yield Stub(
                    stable_id=sid, landing_url=JUSTICE_INDEX, raw_url=d.pdf_url,
                    hint_date=date(d.year, 1, 1), title=d.title,
                    hints={"surface": "justice", "year": d.year, "aliases": aliases},
                )
        if self.surface in ("newsroom", "both"):
            for type_id, (kind, doc_type) in NEWSROOM_TYPES.items():
                try:
                    resp = self._get(f"{NEWSROOM_BASE}/feed?item_type_id={type_id}"
                                     f"&lang=en&orderby=item_date")
                except FetchError:
                    continue  # one broken type feed shouldn't sink the other six
                for item in parse_newsroom_feed(resp.content):
                    tail, aliases = self._wp_id(item.title)
                    sid = f"a29wp/{tail}" if tail else f"a29wp/item/{item.item_id}"
                    if sid in seen:
                        continue  # same paper on both surfaces / in two feeds
                    seen.add(sid)
                    yield Stub(
                        stable_id=sid, landing_url=item.page_url, raw_url=item.page_url,
                        title=sentence_case(item.title),
                        hints={"surface": "newsroom", "kind": kind, "doc_type": doc_type,
                               "item_id": item.item_id, "aliases": aliases},
                    )

    # -- fetch -------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        if stub.hints.get("surface") == "newsroom":
            return self._fetch_newsroom(stub)
        return self._fetch_justice(stub)

    def _extract(self, raw: bytes) -> tuple[str | None, bool]:
        from ..extraction import extract_bytes

        text = extract_bytes(raw, ext="pdf", mime="application/pdf").text
        if looks_unocrd(text, _pdf_pages(raw)):
            # 1997-2005 papers in particular are scans with no text layer
            ocr = ocr_pdf(raw)
            if ocr:
                return ocr, False
            return text, True
        return text, False

    def _fetch_justice(self, stub: Stub) -> Record | None:
        resp = self._get(stub.raw_url, expect_pdf=True)
        raw = resp.content
        text, needs_ocr = self._extract(raw)
        tail, _ = self._wp_id(stub.stable_id.rsplit("/", 1)[-1])
        title = stub.title or stub.stable_id
        if tail and tail.upper().replace("-REV", " rev.") not in title.upper():
            title = f"{title} ({tail.upper().replace('-REV', ' rev.')})"
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.GUIDANCE,
            title=title,
            # the archive files by year only — precise adoption dates come from the
            # PDF text via guidance classification
            decision_date=stub.hint_date,
            language="en", source_language="en",
            landing_url=stub.landing_url, raw_bytes=raw, raw_ext="pdf",
            text=text or None, extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["a29wp"],
            extra={k: v for k, v in {
                "a29wp_surface": "justice", "date_precision": "year",
                "url": stub.raw_url, "aliases": stub.hints.get("aliases") or [],
                **({"needs_ocr": True} if needs_ocr else {}),
            }.items() if v not in (None, [], "")},
        )

    def _fetch_newsroom(self, stub: Stub) -> Record | None:
        resp = self._get(stub.landing_url)
        html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else str(resp.content)
        item = parse_newsroom_item(html)
        pick = pick_en_pdf(item["docs"])
        if pick is None:
            return None  # an item with no attachment at all — nothing to hold
        pdf = self._get(pick["href"], expect_pdf=True)
        raw = pdf.content
        text, needs_ocr = self._extract(raw)
        return Record(
            source=self.source, stable_id=stub.stable_id,
            doc_type=stub.hints.get("doc_type") or DocType.GUIDANCE,
            title=sentence_case(item["title"] or stub.title or stub.stable_id),
            # the feed's pubDate is the newsroom UPLOAD date (often years after
            # adoption) — deliberately not used; classification reads the PDF
            decision_date=None,
            language="en", source_language="en",
            landing_url=stub.landing_url, raw_bytes=raw, raw_ext="pdf",
            text=text or None, extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["a29wp", stub.hints.get("kind") or "document"],
            extra={k: v for k, v in {
                "a29wp_surface": "newsroom",
                "newsroom_kind": stub.hints.get("kind"),
                "newsroom_item_id": stub.hints.get("item_id"),
                "url": pick["href"],
                "other_files": [d for d in item["docs"] if d["href"] != pick["href"]],
                "aliases": stub.hints.get("aliases") or [],
                **({"needs_ocr": True} if needs_ocr else {}),
            }.items() if v not in (None, [], "")},
        )
