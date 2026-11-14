"""The Commission's digital-strategy library — policy documents and reports (DG CONNECT).

``digital-strategy.ec.europa.eu/en/library`` is the Commission's publication register for
digital policy: AI Act guidelines and Commission opinions, DSA and DMA material, codes of
practice, connectivity and cybersecurity reports. The corpus wants two of its types —
``type=25`` (Policy and legislation) and ``type=28`` (Report / Study) — which the site
filters server-side, so the feed is pre-narrowed to what is worth holding (~175 pages).

Each library ITEM is a news-style page carrying a short editorial summary and a downloads
panel; the substantive material is in the panel, behind
``ec.europa.eu/newsroom/dae/redirection/document/<id>`` links that serve the PDF straight
back (with a nonsense ``Content-Type: /``, hence the magic-byte check). A document per
FILE is stored, so each is separately citable and pinpointable, with the item page as the
landing URL and its summary in the metadata.

**Language variants.** Where a document is published in several languages the panel lists
them as one title per language — "… (English)", "… (French)", … Only the English one is
taken: files are grouped by their title with the language suffix removed, and a group with
more than one member keeps English alone. A title with no language suffix is never touched,
so a document published only in English (the common case) is unaffected.

Discovery is newest-first, so an incremental run stops at the first item at or before the
watermark and a backfill walks to the end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit, parse_qs

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import (DocType, ExtractedVia, Record, Stub, RelationshipType,
                           ResolutionStatus, TypedRelation, sha256_bytes)
from ..core.segmentation import synthesise_numbered_segments
from ..extraction.extractors import PdfExtractor

BASE = "https://digital-strategy.ec.europa.eu"
# type 25 = Policy and legislation, 28 = Report / Study — the site's own filter
LIBRARY_URL = f"{BASE}/en/library?type=25%7C28"

# The EU's own languages, as the panel spells them in English. A trailing "(English)" is
# what marks a title as one of several language versions of the same document.
_LANGUAGES = (
    "English", "Bulgarian", "Croatian", "Czech", "Danish", "Dutch", "Estonian", "Finnish",
    "French", "German", "Greek", "Hungarian", "Irish", "Italian", "Latvian", "Lithuanian",
    "Maltese", "Polish", "Portuguese", "Romanian", "Slovak", "Slovenian", "Spanish",
    "Swedish", "Norwegian", "Icelandic",
    "Gaelic", "Slovene", "Sloveme", "Finish",  # publisher labels, including typos
)
_LANG_SUFFIX_RE = re.compile(
    r"\s*[\(\[]\s*(" + "|".join(_LANGUAGES) + r")\s*[\)\]]+[.\s]*$", re.IGNORECASE)
# "1 - Guidelines on …" — the panel numbers its files; not part of the title
_LEADING_NUM_RE = re.compile(r"^\s*\d+\s*[-–—.]\s*")
_DATE_RE = re.compile(r"\b(\d{1,2}\s+[A-Za-z]+\s+(?:19|20)\d{2})\b")


def _clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").casefold()).strip("-")


def _parse_date(value: str | None) -> date | None:
    m = _DATE_RE.search(_clean(value))
    if not m:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(m.group(1), fmt).date()
        except ValueError:
            continue
    return None


@dataclass(slots=True)
class LibraryItem:
    slug: str
    url: str
    title: str
    kind: str | None = None          # "Policy and legislation" | "Report / Study"
    published: date | None = None
    summary: str | None = None
    files: list[dict] = field(default_factory=list)


def parse_library_page(html: bytes | str) -> list[LibraryItem]:
    """The items on one page of the filtered library listing, in the order shown."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[LibraryItem] = []
    seen: set[str] = set()
    for art in soup.select("article.ecl-content-item"):
        link = art.select_one('.ecl-content-block__title a[href]')
        if link is None:
            continue
        url = urljoin(BASE, _clean(str(link.get("href"))))
        if "/library/" not in url or url in seen:
            continue
        seen.add(url)
        meta = [_clean(li.get_text()) for li
                in art.select(".ecl-content-block__primary-meta-item")]
        desc = art.select_one(".ecl-content-block__description")
        out.append(LibraryItem(
            slug=_slug(urlsplit(url).path.rsplit("/", 1)[-1]),
            url=url,
            title=_clean(link.get_text(" ", strip=True)),
            kind=meta[0] if meta else None,
            published=_parse_date(meta[1]) if len(meta) > 1 else None,
            summary=_clean(desc.get_text(" ", strip=True)) if desc else None,
        ))
    return out


def has_next_page(html: bytes | str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.select_one('.ecl-pagination__link[aria-label="Go to next page"]'))


def english_only(files: list[dict]) -> list[dict]:
    """Drop the non-English language versions of a document.

    Files are grouped by their title with a trailing "(Language)" removed. A group with
    more than one member is a set of language versions: English wins, and if English is
    absent nothing is dropped (better to hold the document in some language than none).
    A single file keeps whatever its title says.
    """
    groups: dict[str, list[dict]] = {}
    for f in files:
        stem = _clean(_LANG_SUFFIX_RE.sub("", f["title"]))
        groups.setdefault(stem.casefold(), []).append(f)
    out: list[dict] = []
    for members in groups.values():
        if len(members) == 1:
            out.append(members[0])
            continue
        english = [m for m in members
                   if (_LANG_SUFFIX_RE.search(m["title"]) or [None])
                   and re.search(r"(?i)[\(\[]\s*english\s*[\)\]]\s*$", m["title"])]
        out.extend(english or members)
    # keep the panel's original order
    order = {id(f): i for i, f in enumerate(files)}
    return sorted(out, key=lambda f: order[id(f)])


def parse_item_page(html: bytes | str, item: LibraryItem) -> LibraryItem:
    """Fill an item in from its own page: the downloads panel, and the summary/date when
    the listing didn't carry them."""
    soup = BeautifulSoup(html, "html.parser")
    files: list[dict] = []
    seen: set[str] = set()
    for block in soup.select(".ecl-file"):
        link = block.select_one("a[href]")
        title_el = block.select_one(".ecl-file__title")
        if link is None:
            continue
        url = urljoin(BASE, _clean(str(link.get("href"))))
        if url in seen:
            continue
        seen.add(url)
        title = _clean(title_el.get_text(" ", strip=True)) if title_el else ""
        files.append({"url": url, "title": _clean(_LEADING_NUM_RE.sub("", title)) or item.title})
    item.files = english_only(files)
    if not item.published:
        for li in soup.select(".ecl-page-header__meta-item"):
            item.published = item.published or _parse_date(li.get_text())
    if not item.summary:
        lead = soup.select_one(".ecl-page-header__description, .ecl-paragraph--m")
        item.summary = _clean(lead.get_text(" ", strip=True)) if lead else None
    return item


# "Policy and legislation" is guidance in RagLex's vocabulary; a study is a report, which
# the corpus also files as guidance (see Facade._doc_kind — Guidance/Reports).
_KIND_TAG = {"policy and legislation": "policy", "report / study": "report"}


def _title_default_instrument(title: str | None) -> dict | None:
    """One deterministic instrument named by the publication title, or no default."""
    from ..citations.extractor import grammar_citations

    cites = [c for c in grammar_citations(title or "") if c.candidate_id]
    ids = list(dict.fromkeys(c.candidate_id for c in cites))
    if len(ids) != 1:
        return None
    host = next(c for c in cites if c.candidate_id == ids[0])
    return {"id": ids[0], "kind": host.entity_kind or "named"}


class DigitalStrategyLibraryAdapter(BaseAdapter):
    source = "eu-digital-strategy"
    min_interval = 1.5

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180)

    # -- discover -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        page = 0
        seen: set[str] = set()
        while True:
            if max_pages is not None and page >= max_pages:
                return
            url = LIBRARY_URL if page == 0 else f"{LIBRARY_URL}&page={page}"
            html = self._client.get(url).content
            items = parse_library_page(html)
            if not items:
                return
            for item in items:
                if item.url in seen:
                    continue
                seen.add(item.url)
                wm = item.published.isoformat() if item.published else None
                # newest first: the first item at or before the cursor ends the run
                if since and wm and wm <= since:
                    return
                yield Stub(
                    stable_id=f"eu/digital-strategy/{item.slug}",
                    landing_url=item.url,
                    raw_url=item.url,
                    title=item.title or None,
                    court="European Commission",
                    hint_date=item.published,
                    hints={"watermark": wm, "kind": item.kind, "slug": item.slug,
                           "summary": item.summary},
                )
            if not has_next_page(html):
                return
            page += 1

    # -- fetch --------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        """One library item → the FIRST document in its downloads panel, with the others
        recorded as siblings.

        The pipeline stores one Record per stub, so the panel's remaining files are listed
        in metadata rather than dropped: they are usually annexes to the same instrument,
        and the annex URLs are what a later pass would need to pull them in."""
        attachment = stub.hints.get("attachment")
        item = LibraryItem(slug=stub.hints.get("slug") or _slug(stub.stable_id),
                           url=stub.landing_url, title=stub.title or "",
                           kind=stub.hints.get("kind"), published=stub.hint_date,
                           summary=stub.hints.get("summary"))
        page = None
        if attachment and urlsplit(attachment["url"]).hostname == "eur-lex.europa.eu":
            uri = parse_qs(urlsplit(attachment["url"]).query).get("uri", [""])[0]
            match = re.fullmatch(r"COM:(\d{4}):(\d+):FIN", uri, re.I)
            if match:
                # Some advertised 'downloads' are EUR-Lex work links, not files.
                # Fetch the exact Commission identity through CELLAR, retaining
                # both this advertised attachment and its canonical identity.
                from .eu_preparatory import EUPreparatoryAdapter
                celex = f"5{match[1]}DC{int(match[2]):04d}"
                canonical = Stub(celex, raw_url=f"https://publications.europa.eu/resource/celex/{celex}",
                                 landing_url=attachment["url"], hints={"title": attachment.get("title")})
                record = EUPreparatoryAdapter(celex=celex, client=self._client).fetch(canonical)
                if record is None or not record.text:
                    raise FetchError(f"{self.source}: CELLAR has no readable body for {celex}", transient=True)
                record.source, record.stable_id = self.source, stub.stable_id
                record.landing_url = stub.landing_url
                record.extra = {k:v for k,v in record.extra.items() if k != "aliases"}
                record.extra.update(canonical_celex=celex, attachment_of=stub.hints.get("attachment_of"),
                                    library_page=stub.hints.get("library_page"), attachment_url=attachment["url"])
                record.relations.extend(TypedRelation(RelationshipType.RELATED_TO, dst_id=target,
                    raw_citation_string=target, extracted_via=ExtractedVia.STRUCTURED)
                    for target in [celex, stub.hints.get("attachment_of")] if target)
                return record
        if attachment:
            item.files = [attachment]
        else:
            try:
                page = self._client.get(stub.landing_url).content
            except FetchError as exc:
                if exc.transient:
                    raise
                return None
            item = parse_item_page(page, item)

        asset_only = False
        extraction_info = {}
        text, raw, raw_ext, doc_title = None, None, "html", stub.title
        if item.files:
            first = item.files[0]
            try:
                response = self._client.get(first["url"])
                blob = response.content
                if (attachment and not blob.startswith((b"%PDF-", b"PK"))
                        and urlsplit(first["url"]).hostname == "digital-strategy.ec.europa.eu"
                        and re.fullmatch(r"/document/download/[0-9a-f-]{36}_[a-z]{2}", urlsplit(first["url"]).path)):
                    # Commission document UUIDs are shared across the migrated
                    # download hosts. Accept this alternate only as a real PDF;
                    # an error/challenge page never becomes an attachment body.
                    alternative = first["url"].replace("https://digital-strategy.ec.europa.eu/",
                                                        "https://commission.europa.eu/", 1)
                    alternate = self._client.get(alternative)
                    if alternate.content.startswith(b"%PDF-"):
                        response, blob = alternate, alternate.content
            except FetchError as exc:
                if exc.transient or attachment:
                    raise
                blob = None
            if attachment and blob:
                from ..extraction.attachments import read_attachment
                text, raw_ext, asset_only = read_attachment(
                    blob, url=first["url"], headers=getattr(response, "headers", {}),
                    extraction_info=extraction_info)
                raw, doc_title = blob, first["title"] or stub.title
            # the redirection endpoint answers with Content-Type "/" — trust the bytes
            if not attachment and blob and blob[:5] == b"%PDF-":
                extracted = PdfExtractor().extract(blob, ext="pdf", mime="application/pdf")
                text, raw, raw_ext = (extracted.text or "").strip(), blob, "pdf"
                doc_title = first["title"] or stub.title
        if attachment and not asset_only and not (text or "").strip():
            raise FetchError(f"{self.source}: advertised attachment has no readable content: {attachment['url']}",
                             transient=True)
        if not attachment and (not text or len(text) < 200):
            # no readable attachment — keep the item page itself, which carries the
            # Commission's own summary of what was published
            from ..extraction import extract_bytes
            text = (extract_bytes(page, ext="html", mime="text/html").text or "").strip()
            raw, raw_ext = page, "html"
        if not attachment and len(text) < 200:
            return None

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=doc_title or stub.stable_id,
            court="European Commission",
            decision_date=item.published,
            language="en" if not attachment or raw_ext == "pdf" or re.search(r"(?i)english", doc_title or "") else None,
            source_language="en" if not attachment or raw_ext == "pdf" or re.search(r"(?i)english", doc_title or "") else None,
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext=raw_ext,
            text=text,
            segments=synthesise_numbered_segments(text) if text else [],
            extracted_via=ExtractedVia.SCRAPE,
            relations=([TypedRelation(RelationshipType.RELATED_TO,
                        dst_id=stub.hints["attachment_of"],
                        raw_citation_string=stub.hints["attachment_of"],
                        resolution_status=ResolutionStatus.RESOLVED,
                        extracted_via=ExtractedVia.SCRAPE)] if attachment else []),
            topic_tags=["eu", "digital-policy",
                        _KIND_TAG.get((item.kind or "").casefold(), "publication")],
            extra={k: v for k, v in {
                "jurisdiction": "eu",
                "issuer": "European Commission (DG CONNECT)",
                "library_type": item.kind,
                "summary": item.summary,
                "item_title": stub.title,
                "citation_default_instrument": _title_default_instrument(
                    doc_title or stub.title
                ),
                "download_url": item.files[0]["url"] if item.files else None,
                "attachment_of": stub.hints.get("attachment_of"),
                "asset_only": True if asset_only else None,
                "extraction_status": "binary asset retained; dedicated extraction required" if asset_only else None,
                "attachment_format": raw_ext if attachment else None,
                "attachment_extraction": extraction_info or None,
                "library_page": stub.hints.get("library_page"),
                # the annexes published alongside, so a later pass can pull them
                "other_files": [{"title": f["title"], "url": f["url"]}
                                for f in item.files[1:]] or None,
            }.items() if v},
        )


class DigitalStrategyAttachmentsAdapter(DigitalStrategyLibraryAdapter):
    """A bounded sweep of attachments already advertised by held library pages.

    Kept separate from the listing cursor: a held cover must not hide its unheld annexes.
    """
    def __init__(self, rows, *, client=None):
        super().__init__(client=client)
        self.rows = rows

    def discover(self, since, *, max_pages=None):
        import json
        for row in self.rows:
            meta = json.loads(row["meta_json"] or "{}")
            for file in meta.get("other_files") or []:
                title = str(file.get("title") or "")
                language = _LANG_SUFFIX_RE.search(title)
                if language and language.group(1).lower() != "english":
                    continue
                url = str(file.get("url") or "")
                if not url:
                    continue
                key = sha256_bytes(url.encode())[:16]
                day = date.fromisoformat(str(row["decision_date"])[:10]) if row["decision_date"] else None
                yield Stub(row["stable_id"] + "/attachment/" + key,
                           landing_url=url, raw_url=url,
                           title=title, hint_date=day, court="European Commission",
                           hints={"attachment": file, "attachment_of": row["stable_id"],
                                  "metadata_only_complete": True,
                                  "kind": meta.get("library_type"), "library_page": row["landing_url"]})
