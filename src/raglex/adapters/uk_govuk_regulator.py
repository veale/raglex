"""Legally relevant material from the GOV.UK publishing APIs.

One adapter for every GOV.UK feed — a single regulator's output (the CMA, Ofgem, Ofwat)
and the whole-of-government policy corpus alike. The Search API
(``/api/search.json``) is the update feed and the Content Store
(``/api/content``, documented at https://content-api.publishing.service.gov.uk/) is the
canonical structured body, so nothing here scrapes HTML or paginates a results page.

A feed is defined by its **filters**, not by its own class:

* ``organisation`` — everything one department or regulator publishes;
* ``document_type`` — one GOV.UK schema (``cma_case``);
* ``supergroup`` — a whole content-purpose bucket across government
  (``policy_and_engagement``: policy papers, impact assessments, consultations and
  calls for evidence, with their outcomes).

**Publisher.** A whole-of-government feed has no single publisher, so the record's
issuing body is read per item from the Search API's ``organisations`` facet — the
"From: Home Office" line on the page — rather than being fixed by the registration.

**Renditions.** A GOV.UK publication is a container, not a document. Its real text lives
in child ``html_publication`` records and/or attached PDFs, usually BOTH, as several
renditions of one thing ("(accessible)" HTML beside a "(print ready)" PDF). The
accessible HTML is preferred and its PDF twins are skipped; PDFs with no HTML rendition
are downloaded and extracted.

Broad organisation feeds intentionally opt into RagLex's legal relevance gate: all
fetched items remain held/deduped, while citation-free operational material is not
embedded, listed or returned by search.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterator, Mapping
from urllib.parse import urljoin

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError, RateLimitException
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://www.gov.uk"
SEARCH = f"{BASE}/api/search.json"
CONTENT = f"{BASE}/api/content"

_SKIP_TYPES = {
    "finder", "finder_email_signup", "organisation",
    "official_statistics_announcement", "statistics_announcement",
}


# GOV.UK publishes one document in several renditions and distinguishes them by a
# trailing parenthetical on the attachment title, not by any structured field:
#
#   Statement of changes to the Immigration Rules: HC 259 (accessible)   ← HTML
#   Statement of changes to the Immigration Rules: HC 259                ← PDF, 294 KB
#   Statement of changes to the Immigration Rules: HC 259 (print ready)  ← PDF, 2.03 MB
#
# The accessible HTML is the one to keep: it is the whole text, already structured, and
# it needs no PDF extraction. Comparing the titles verbatim never matched — the HTML
# rendition is the one carrying the qualifier — so both PDFs were downloaded and inlined
# alongside it, tripling the document's text. Strip the qualifier from both sides first.
_RENDITION_QUALIFIER = re.compile(
    r"\s*[\(\[]\s*(?:accessible(?:\s+version)?|print[\s-]?ready|web\s+version|"
    r"html|pdf|odt|ods|odf|large\s+print|easy\s+read|print\s+version|"
    r"revised|updated)\s*[\)\]]\s*$",
    re.IGNORECASE)


def _rendition_key(title: str | None) -> str:
    """An attachment title reduced to the DOCUMENT it is a rendition of."""
    text = str(title or "").strip()
    while True:
        stripped = _RENDITION_QUALIFIER.sub("", text)
        if stripped == text:
            break
        text = stripped
    return re.sub(r"\W+", " ", text).strip().lower()


def _publisher(item: Mapping) -> tuple[str | None, str | None]:
    """(name, slug) of the body a search result is published BY — the "From: Home
    Office" line on the page.

    A whole-of-government feed has no single publisher, so this is the only way to
    attribute an item. Where several organisations are listed the first is the lead
    (GOV.UK orders them that way); a sub-organisation keeps its own name rather than its
    parent department's, because "Office for Product Safety and Standards" is who a
    reader is looking for.
    """
    for org in item.get("organisations") or ():
        if not isinstance(org, dict):
            continue
        name = str(org.get("title") or "").strip()
        slug = str(org.get("slug") or "").strip()
        if name or slug:
            return name or None, slug or None
    return None, None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _html_text(value: str | None) -> str:
    from bs4 import BeautifulSoup

    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "nav"]):
        tag.decompose()
    lines = [" ".join(s.split()) for s in soup.get_text("\n").splitlines()]
    return "\n".join(s for s in lines if s).strip()


def content_text(content: dict) -> str:
    details = content.get("details") or {}
    parts: list[str] = []
    for key in ("body", "hidden_indexable_content", "summary"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(_html_text(value))
    for part in details.get("parts") or ():
        if not isinstance(part, dict):
            continue
        heading = str(part.get("title") or "").strip()
        body = _html_text(part.get("body"))
        if body:
            parts.append((f"{heading}\n{body}" if heading else body))
    for doc in details.get("documents") or ():
        if isinstance(doc, str):
            text = _html_text(doc)
            if text:
                parts.append(text)
        elif isinstance(doc, dict):
            text = _html_text(doc.get("body") or doc.get("content"))
            if text:
                parts.append(text)
    description = str(content.get("description") or "").strip()
    if description:
        parts.append(description)
    return "\n\n".join(dict.fromkeys(p for p in parts if p)).strip()


class GOVUKRegulatorAdapter(BaseAdapter):
    # GOV.UK publishes a rate limit of 10 requests per second. 0.75s serial was 1.33/s —
    # 13% of the allowance — and a publication costs ~3.6 requests, so the 25,174-item
    # policy corpus projected to 19 hours of almost pure sleeping. 0.2s is 5/s: half the
    # published limit, and the pacer is shared and thread-safe, so the concurrent
    # attachment fetches below queue on it rather than multiplying it.
    min_interval = 0.2
    page_size = 200

    def __init__(
        self,
        *,
        source: str,
        organisation: str | None = None,
        document_type: str | None = None,
        supergroup: str | None = None,
        court: str | None = None,
        id_prefix: str | None = None,
        search_filters: Mapping[str, str] | None = None,
        record_doc_type: DocType | None = None,
        require_recognized_legal_citation: bool = True,
        client: RateLimitedClient | None = None,
    ) -> None:
        if not (organisation or document_type or supergroup):
            raise ValueError("organisation, document_type or supergroup is required")
        if not (court or supergroup):
            raise ValueError("court is required unless the feed spans government")
        self.source = source
        self.organisation = organisation
        self.document_type = document_type
        self.supergroup = supergroup
        self.court = court
        # The namespace stable_ids are minted under. Defaults to the source key, which
        # is what every pre-existing registration relies on; a shared value lets two
        # feeds that overlap (the CMA's own register and the cross-government policy
        # corpus both hold the CMA's policy papers) land on ONE document rather than
        # storing the same GOV.UK page twice under two keys.
        self.id_prefix = (id_prefix or source).strip("/")
        self.search_filters = dict(search_filters or {})
        self.record_doc_type = record_doc_type
        self.require_recognized_legal_citation = require_recognized_legal_citation
        self._client = client or RateLimitedClient(
            source, min_interval=self.min_interval, timeout=60
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        start = pages = 0
        while True:
            params = {
                "count": self.page_size,
                "start": start,
                "order": "-public_timestamp",
                "fields": (
                    "title,link,description,public_timestamp,"
                    "content_store_document_type,content_purpose_subgroup,organisations"
                ),
            }
            if self.organisation:
                params["filter_organisations"] = self.organisation
            if self.document_type:
                params["filter_document_type"] = self.document_type
            if self.supergroup:
                params["filter_content_purpose_supergroup"] = self.supergroup
            params.update(self.search_filters)
            data = self._client.get(SEARCH, params=params).json()
            items = data.get("results") or []
            if not items:
                return
            for item in items:
                published = str(item.get("public_timestamp") or "")
                if since and published and published <= since:
                    return
                link = str(item.get("link") or "")
                kind = str(item.get("content_store_document_type") or "")
                if not link.startswith("/") or kind in _SKIP_TYPES:
                    continue
                org_name, org_slug = _publisher(item)
                yield Stub(
                    stable_id=f"{self.id_prefix}/{link.strip('/')}",
                    landing_url=urljoin(BASE, link),
                    raw_url=f"{CONTENT}{link}",
                    title=item.get("title"),
                    court=self.court or org_name,
                    hint_date=_date(published),
                    hints={
                        "watermark": published,
                        "contenthash": published,
                        "description": item.get("description"),
                        "content_type": kind,
                        "subgroup": item.get("content_purpose_subgroup"),
                        "organisation": org_name,
                        "organisation_slug": org_slug,
                        "feed_total": data.get("total"),
                        "resume_offset": start,
                    },
                )
            pages += 1
            start += len(items)
            if start >= int(data.get("total") or 0) or len(items) < self.page_size:
                return
            if max_pages is not None and pages >= max_pages:
                return

    #: How many of ONE document's attachments to fetch at once. The aggregate request
    #: rate is still the shared pacer's (see RateLimitedClient._pace), so this overlaps
    #: waiting rather than spending more of the source's allowance — GOV.UK publishes a
    #: 10 req/s limit and ``min_interval`` keeps us well inside it. Bounded small because
    #: a publication rarely has more than a handful.
    attachment_workers = 6

    def _fetch_many(self, urls, read):
        """``{url: value}`` for several of ONE document's attachments, fetched at once.

        A failure is an ABSENT KEY, never an exception: a 404 attachment or a PDF the
        extractor chokes on must cost that attachment, not the publication. The one
        exception is the source pushing back — a rate limit has to reach the pipeline so
        it can pause the queue rather than hammering on through the rest of the corpus.
        """
        from concurrent.futures import ThreadPoolExecutor

        urls = list(dict.fromkeys(u for u in urls if u))
        if not urls:
            return {}
        if len(urls) == 1:                      # no pool for the common case
            try:
                return {urls[0]: read(self._client.get(urls[0]))}
            except RateLimitException:
                raise
            except Exception:                   # noqa: BLE001
                return {}
        out: dict = {}
        rate_limited: list[BaseException] = []

        def one(url):
            try:
                return url, read(self._client.get(url))
            except RateLimitException as exc:
                rate_limited.append(exc)
                return url, None
            except Exception:                   # noqa: BLE001 — see docstring
                return url, None

        with ThreadPoolExecutor(max_workers=min(self.attachment_workers,
                                                len(urls))) as pool:
            for url, value in pool.map(one, urls):
                if value is not None:
                    out[url] = value
        if rate_limited:
            raise rate_limited[0]
        return out

    def _fetch_json_many(self, urls) -> dict:
        return self._fetch_many(urls, lambda r: r.json())

    def _fetch_bytes_many(self, urls) -> dict:
        return self._fetch_many(urls, lambda r: r.content)

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(stub.raw_url)
        try:
            content = response.json()
        except ValueError:
            return None
        text = content_text(content)
        details = content.get("details") or {}
        attachment_meta: list[dict] = []
        html_attachment_titles: set[str] = set()
        pdf_rendition_keys: set[str] = set()
        wanted_pdfs: list[tuple[str, str]] = []
        aliases: list[str] = []
        # A GOV.UK publication is a container. Its full, accessible guidance often
        # lives in one or more child ``html_publication`` Content Store records, not
        # in the parent's body (CMA207 is the canonical example). Follow those
        # internal attachments before falling back to the equivalent PDF.
        html_children = [a for a in (details.get("attachments") or ())
                         if isinstance(a, dict) and a.get("attachment_type") == "html"
                         and str(a.get("url") or "").startswith("/")]
        # A document's own attachments are INDEPENDENT fetches, and each was paid for one
        # at a time: a publication costs ~3.6 requests, so the wall clock was ~3.6× the
        # pacing floor per document — 19 hours for the 25,174-item policy corpus, almost
        # all of it sleeping. Fetched concurrently the document costs about one request's
        # latency instead. The pacer is shared and thread-safe, so the AGGREGATE request
        # rate is unchanged — this overlaps the waiting, it does not spend more of the
        # source's allowance. One document at a time still, so watermark, dedup, resume
        # and progress semantics are all untouched.
        children = self._fetch_json_many(f"{CONTENT}{str(a['url'])}" for a in html_children)
        for attachment in html_children:
            url = str(attachment.get("url") or "")
            child = children.get(f"{CONTENT}{url}")
            if child is None:
                continue
            body = content_text(child)
            if not body:
                continue
            title = str(attachment.get("title") or child.get("title") or "").strip()
            if title:
                html_attachment_titles.add(_rendition_key(title))
                text += f"\n\n{title}\n{body}"
            else:
                text += "\n\n" + body
            attachment_meta.append({
                "url": url, "title": title or None, "type": "html",
                "text_chars": len(body),
            })
        # Decisions are often a short landing page plus the legally operative PDF.
        # Include each English PDF before applying the citation gate.
        for attachment in details.get("attachments") or ():
            if not isinstance(attachment, dict):
                continue
            url = str(attachment.get("url") or "")
            mime = str(attachment.get("content_type") or "")
            if not url or ("pdf" not in mime.lower() and not url.lower().endswith(".pdf")):
                continue
            title = str(attachment.get("title") or "").strip()
            title_key = _rendition_key(title)
            reference = str(attachment.get("unique_reference") or "").strip()
            if reference:
                aliases.append(reference)
            # Prefer the accessible HTML rendition where GOV.UK publishes both. The key
            # ignores the rendition qualifier, so the "(print ready)" and unqualified
            # PDFs are both recognised as twins of the "(accessible)" HTML.
            if title_key and title_key in html_attachment_titles:
                attachment_meta.append({
                    "url": url, "title": title or None, "type": "pdf",
                    "skipped": "html-rendition-preferred",
                })
                continue
            # …and where GOV.UK publishes several PDF renditions of one document with no
            # HTML at all, take the first (the accessible one sorts before "print ready"
            # only by luck, so this is first-seen, not best — but it is ONE of them).
            if title_key and title_key in pdf_rendition_keys:
                attachment_meta.append({
                    "url": url, "title": title or None, "type": "pdf",
                    "skipped": "duplicate-rendition",
                })
                continue
            pdf_rendition_keys.add(title_key)
            wanted_pdfs.append((url, title))
        # Which PDFs to take is decided ABOVE without any I/O, because the rendition
        # rules are order-dependent (a PDF is skipped for a twin already seen). Only once
        # the set is settled are they downloaded, and then concurrently.
        pdf_bodies = self._fetch_bytes_many(url for url, _t in wanted_pdfs)
        for url, title in wanted_pdfs:
            pdf = pdf_bodies.get(url)
            if pdf is None:
                continue
            try:
                extracted = extract_bytes(pdf, ext="pdf", mime="application/pdf")
            except Exception:    # noqa: BLE001 — a PDF the extractor chokes on must cost
                continue         # this attachment, not the publication
            body = (extracted.text or "").strip()
            if body:
                text += "\n\n" + body
            attachment_meta.append({
                "url": url, "title": title or None, "type": "pdf", "bytes": len(pdf),
                "text_chars": len(body),
            })
        text = text.strip()
        if len(text) < 40:
            return None
        base_path = str(content.get("base_path") or "")
        updated = content.get("public_updated_at") or stub.hints.get("watermark")
        first = content.get("first_published_at")
        doc_type = self.record_doc_type or (
            DocType.DECISION if self.document_type == "cma_case" else DocType.GUIDANCE
        )
        for value in (content.get("title"), stub.title):
            aliases.extend(re.findall(r"\bCMA\d{1,4}[A-Z]?\b", str(value or ""), re.I))
        aliases = list(dict.fromkeys(a.upper() for a in aliases if a))
        default_instrument = None
        if any(code in aliases for code in ("CMA200", "CMA207", "CMA208")):
            default_instrument = {"id": "ukpga/2024/13", "kind": "act"}
        elif "CMA37" in aliases:
            default_instrument = {"id": "ukpga/2015/15", "kind": "act"}
        # Who published it: the registration's fixed body for a single-regulator feed,
        # the item's own "From:" organisation for a cross-government one.
        publisher = self.court or stub.hints.get("organisation") or stub.court
        org_slug = stub.hints.get("organisation_slug")
        content_type = content.get("document_type") or stub.hints.get("content_type")
        subgroup = stub.hints.get("subgroup")
        # The generic GOV.UK categorisation: the supergroup this feed is, the content
        # sub-group and schema GOV.UK itself assigns, and the publishing body — so the
        # corpus can be faceted the way the source is, without inventing a taxonomy.
        tags = ["regulatory", "govuk", *( [self.supergroup.replace("_", "-")]
                                          if self.supergroup else []),
                *([str(subgroup).replace("_", "-")] if subgroup else []),
                *([str(content_type).replace("_", "-")] if content_type else []),
                *([org_slug] if org_slug else []),
                *([publisher.lower()] if publisher and not org_slug else [])]
        return Record(
            source=self.source,
            stable_id=f"{self.id_prefix}/{base_path.strip('/') or stub.stable_id.split('/', 1)[-1]}",
            doc_type=doc_type,
            title=content.get("title") or stub.title,
            court=publisher,
            decision_date=_date(first) or stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=response.content,
            raw_ext="json",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=[t for t in dict.fromkeys(tags) if t],
            extra={
                "jurisdiction": "gb",
                "content_id": content.get("content_id"),
                "content_type": content_type,
                "content_purpose_supergroup": self.supergroup,
                "content_purpose_subgroup": subgroup,
                "organisation": publisher,
                "organisation_slug": org_slug,
                "first_published_at": first,
                "updated_at": updated,
                "contenthash": stub.hints.get("contenthash"),
                "attachments": attachment_meta,
                "aliases": aliases,
                "citation_default_instrument": default_instrument,
                "require_recognized_legal_citation": self.require_recognized_legal_citation,
            },
        )
