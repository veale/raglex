"""Irish Data Protection Commission decisions and guidance, and their operative PDFs."""

from __future__ import annotations

import re
import ssl
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

import httpx

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import DEFAULT_USER_AGENT, RateLimitedClient, get_proxy
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)
from ..extraction import extract_bytes

BASE = "https://www.dataprotection.ie"
LISTING = f"{BASE}/en/dpc-guidance/decisions"
GUIDANCE_HUB = f"{BASE}/en/dpc-guidance"
GDPR = "32016R0679"
IE_DPA_2018 = "ie/2018/act/7"

# The hub's own accordion of EDPB guidelines. Those documents are held already (the
# ``edpb`` source harvests the Board's register in every language), so re-fetching the
# DPC's link list would only mint duplicates under an Irish stable id.
EDPB_SECTION = "guidance from the european data protection board"

# The DPC server currently sends only its leaf certificate.  OpenSSL does not fetch
# Authority Information Access intermediates, so Linux clients reject the otherwise
# valid chain.  Pin the exact issuer certificate advertised by the leaf and allow that
# issuer to terminate this source-specific chain. Hostname, validity and signature checks
# remain active; this is deliberately not ``verify=False``.
_SECTIGO_DV_R36 = """-----BEGIN CERTIFICATE-----
MIIGTDCCBDSgAwIBAgIQOXpmzCdWNi4NqofKbqvjsTANBgkqhkiG9w0BAQwFADBf
MQswCQYDVQQGEwJHQjEYMBYGA1UEChMPU2VjdGlnbyBMaW1pdGVkMTYwNAYDVQQD
Ey1TZWN0aWdvIFB1YmxpYyBTZXJ2ZXIgQXV0aGVudGljYXRpb24gUm9vdCBSNDYw
HhcNMjEwMzIyMDAwMDAwWhcNMzYwMzIxMjM1OTU5WjBgMQswCQYDVQQGEwJHQjEY
MBYGA1UEChMPU2VjdGlnbyBMaW1pdGVkMTcwNQYDVQQDEy5TZWN0aWdvIFB1Ymxp
YyBTZXJ2ZXIgQXV0aGVudGljYXRpb24gQ0EgRFYgUjM2MIIBojANBgkqhkiG9w0B
AQEFAAOCAY8AMIIBigKCAYEAljZf2HIz7+SPUPQCQObZYcrxLTHYdf1ZtMRe7Yeq
RPSwygz16qJ9cAWtWNTcuICc++p8Dct7zNGxCpqmEtqifO7NvuB5dEVexXn9RFFH
12Hm+NtPRQgXIFjx6MSJcNWuVO3XGE57L1mHlcQYj+g4hny90aFh2SCZCDEVkAja
EMMfYPKuCjHuuF+bzHFb/9gV8P9+ekcHENF2nR1efGWSKwnfG5RawlkaQDpRtZTm
M64TIsv/r7cyFO4nSjs1jLdXYdz5q3a4L0NoabZfbdxVb+CUEHfB0bpulZQtH1Rv
38e/lIdP7OTTIlZh6OYL6NhxP8So0/sht/4J9mqIGxRFc0/pC8suja+wcIUna0HB
pXKfXTKpzgis+zmXDL06ASJf5E4A2/m+Hp6b84sfPAwQ766rI65mh50S0Di9E3Pn
2WcaJc+PILsBmYpgtmgWTR9eV9otfKRUBfzHUHcVgarub/XluEpRlTtZudU5xbFN
xx/DgMrXLUAPaI60fZ6wA+PTAgMBAAGjggGBMIIBfTAfBgNVHSMEGDAWgBRWc1hk
lfmSGrASKgRieaFAFYghSTAdBgNVHQ4EFgQUaMASFhgOr872h6YyV6NGUV3LBycw
DgYDVR0PAQH/BAQDAgGGMBIGA1UdEwEB/wQIMAYBAf8CAQAwHQYDVR0lBBYwFAYI
KwYBBQUHAwEGCCsGAQUFBwMCMBsGA1UdIAQUMBIwBgYEVR0gADAIBgZngQwBAgEw
VAYDVR0fBE0wSzBJoEegRYZDaHR0cDovL2NybC5zZWN0aWdvLmNvbS9TZWN0aWdv
UHVibGljU2VydmVyQXV0aGVudGljYXRpb25Sb290UjQ2LmNybDCBhAYIKwYBBQUH
AQEEeDB2ME8GCCsGAQUFBzAChkNodHRwOi8vY3J0LnNlY3RpZ28uY29tL1NlY3Rp
Z29QdWJsaWNTZXJ2ZXJBdXRoZW50aWNhdGlvblJvb3RSNDYucDdjMCMGCCsGAQUF
BzABhhdodHRwOi8v
b2NzcC5zZWN0aWdvLmNvbTANBgkqhkiG9w0BAQwFAAOCAgEAYtOC9Fy+TqECFw40
IospI92kLGgoSZGPOSQXMBqmsGWZUQ7rux7cj1du6d9rD6C8ze1B2eQjkrGkIL/O
F1s7vSmgYVafsRoZd/IHUrkoQvX8FZwUsmPu7amgBfaY3g+dq1x0jNGKb6I6Bzdl
6LgMD9qxp+3i7GQOnd9J8LFSietY6Z4jUBzVoOoz8iAU84OFh2HhAuiPw1ai0VnY
38RTI+8kepGWVfGxfBWzwH9uIjeooIeaosVFvE8cmYUB4TSH5dUyD0jHct2+8ceK
EtIoFU/FfHq/mDaVnvcDCZXtIgitdMFQdMZaVehmObyhRdDD4NQCs0gaI9AAgFj4
L9QtkARzhQLNyRf87Kln+YU0lgCGr9HLg3rGO8q+Y4ppLsOdunQZ6ZxPNGIfOApb
PVf5hCe58EZwiWdHIMn9lPP6+F404y8NNugbQixBber+x536WrZhFZLjEkhp7fFX
f9r32rNPfb74X/U90Bdy4lzp3+X1ukh1BuMxA/EEhDoTOS3l7ABvc7BYSQubQ249
0OcdkIzUh3ZwDrakMVrbaTxUM2p24N6dB+ns2zptWCva6jzWr8IWKIMxzxLPv5Kt
3ePKcUdvkBU/smqujSczTzzSjIoR5QqQA6lN1ZRSnuHIWCvhJEltkYnTAH41QJ6S
AWO66GrrUESwN/cgZzL4JLEqz1Y=
-----END CERTIFICATE-----
"""


def _dpc_http_client() -> httpx.Client:
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=_SECTIGO_DV_R36)
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return httpx.Client(
        verify=context,
        headers={"User-Agent": DEFAULT_USER_AGENT},
        timeout=90,
        follow_redirects=True,
        proxy=get_proxy(),
    )


def parse_dpc_listing(raw: bytes | str) -> list[dict]:
    """The decision register, one dict per decision.

    The register lays its results out two-up: a ``.views-row`` is a *row of the grid*
    holding two ``.faq-section-results-box`` cards, so keying on the row (and taking its
    first link) silently halved the register — 33 of the 63 published decisions. Key on
    the card. ``.views-row`` remains the fallback for the older single-column markup and
    for the migrated items that live directly below ``/en/``.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    cards = soup.select(".faq-section-results-box") or soup.select(".views-row")
    out, seen = [], set()
    for card in cards:
        link = card.select_one("h2 a[href], h3 a[href]")
        if not link:
            link = card.select_one("a[href*='/dpc-guidance/decisions/']")
        if not link:
            # A few migrated items live directly below /en/.
            link = card.select_one("a[aria-label^='Read this case study']")
        if not link:
            continue
        url = urljoin(BASE, str(link.get("href") or "").split("#", 1)[0])
        if url in seen:
            continue
        seen.add(url)
        title_link = card.select_one("h2 a, h3 a") or link
        # The card already carries the facets the detail page repeats — sector, date and
        # the GDPR/DPA articles in issue. Carrying them forward means a decision whose
        # PDF is unreachable still lands with its structured provision edges.
        published = card.select_one(".datetime")
        sector = card.select_one(".faq-section-category-link a")
        articles = [
            " ".join(a.get_text(" ", strip=True).split())
            for a in card.select(".classArticles a[href*='decision_articles=']")
        ]
        out.append({
            "url": url,
            "title": title_link.get_text(" ", strip=True),
            "date": published.get_text(" ", strip=True) if published else None,
            "sector": sector.get_text(" ", strip=True) if sector else None,
            "articles": articles,
        })
    return out


def parse_dpc_detail(raw: bytes | str) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    body_root = soup.select_one(".field--name-body")
    if not body_root:
        return {}
    title = body_root.find("h1")
    text_body = body_root.select_one(".field--name-body")
    text = "\n".join(
        " ".join(line.split())
        for line in (text_body or body_root).get_text("\n").splitlines()
        if line.strip()
    )
    # An inquiry can publish more than one operative document — a decision plus the
    # circuit-court judgment on appeal, or a decision split by respondent. Keep them all
    # in register order; the first is the decision the page is named for.
    pdfs: list[dict] = []
    for a in body_root.find_all("a", href=True):
        href = str(a["href"])
        if ".pdf" not in href.lower():
            continue
        url = urljoin(BASE, href)
        if any(row["url"] == url for row in pdfs):
            continue
        pdfs.append({"url": url, "title": a.get_text(" ", strip=True) or None})
    articles: list[str] = []
    reference = decided = sector = topic = None
    for block in body_root.select(".block-tags"):
        label = " ".join(block.get_text(" ", strip=True).split())
        if label.lower().startswith("articles:"):
            articles = [a.get_text(" ", strip=True) for a in block.find_all("a")]
        elif label.lower().startswith("dpc reference:"):
            reference = label.split(":", 1)[1].strip()
        elif label.lower().startswith("decision date:"):
            decided = label.split(":", 1)[1].strip()
        # The register's own two-axis classification: who was inquired into (Area —
        # Bank/Credit/Insurance, University, Garda…) and what the decision is about
        # (Topic — Data security, Transfers, LED, Children…).
        elif label.lower().startswith("area:"):
            sector = label.split(":", 1)[1].strip()
        elif label.lower().startswith("topic:"):
            topic = label.split(":", 1)[1].strip()
    return {
        "title": title.get_text(" ", strip=True) if title else None,
        "text": text, "pdf": pdfs[0]["url"] if pdfs else None, "pdfs": pdfs,
        "articles": articles, "reference": reference, "date": decided,
        "sector": sector, "topic": topic,
    }


def parse_dpc_guidance_hub(raw: bytes | str) -> list[dict]:
    """The guidance hub's accordion sections → one dict per DPC guidance page.

    The hub is the DPC's own topical index (General Guidance, Technological issues,
    GDPR requirements, Direct marketing/Electoral, COVID-19), so the section heading is
    the authority's classification of the document and is kept as a tag. The EDPB
    accordion and every off-site link are dropped: those documents belong to the
    ``edpb`` source, and the rest of the hub's outbound links are navigation.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    out: list[dict] = []
    index: dict[str, dict] = {}
    for item in soup.select(".accordion-item"):
        heading = item.select_one(".accordion-button, .btn-link, .accordion-header")
        section = " ".join((heading.get_text(" ", strip=True) if heading else "").split())
        if section.casefold() == EDPB_SECTION:
            continue
        body = item.select_one(".accordion-body, .card-body")
        for link in (body or item).find_all("a", href=True):
            url = urljoin(BASE, str(link["href"]).split("#", 1)[0]).rstrip("/")
            # The hub mixes bare paths, www and apex-host absolute URLs for the same
            # page; normalise to one host so the two spellings are one document.
            parts = urlsplit(url)
            if parts.netloc and parts.netloc.removeprefix("www.") != "dataprotection.ie":
                continue
            url = f"{BASE}{parts.path}"
            title = " ".join(link.get_text(" ", strip=True).split())
            if not title or parts.path.rstrip("/") in ("/en/dpc-guidance", "/en", ""):
                continue
            if url in index:
                if section and section not in index[url]["sections"]:
                    index[url]["sections"].append(section)
                continue
            index[url] = {"url": url, "title": title,
                          "sections": [section] if section else []}
            out.append(index[url])
    return out


def guidance_stable_id(url: str) -> str:
    """``/en/dpc-guidance/blogs/tips-avoiding-data-breaches`` → ``ie/dpc/guidance/
    blogs/tips-avoiding-data-breaches``.

    The whole path below ``/en/`` is kept, not just the final slug: the hub links
    guidance living under ``/en/organisations/…`` and ``/en/individuals/…`` as well as
    under ``/en/dpc-guidance/…``, and those trees reuse slugs
    (``data-protection-basics`` exists in two of them). Keeping the path also keeps
    guidance clear of the decisions' ``ie/dpc/<slug>`` namespace.
    """
    path = urlsplit(url).path.strip("/")
    path = re.sub(r"^en/", "", path)
    path = re.sub(r"^dpc-guidance/", "", path)
    return f"ie/dpc/guidance/{path}"


def parse_dpc_guidance_page(raw: bytes | str) -> dict:
    """A guidance landing page: its on-page text and every guidance PDF it links.

    Most DPC guidance is published two-step — the page carries a few hundred words of
    orientation and the operative text is the linked "Full Guidance Note" PDF. Some
    items (the blogs, the shorter notes) are complete on the page with no PDF at all.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    root = soup.select_one(".field--name-body") or soup.select_one("main") or soup
    for tag in root.select("script, style, nav, form, .breadcrumb, .nav-side"):
        tag.decompose()
    heading = soup.find("h1")
    text = "\n".join(
        " ".join(line.split()) for line in root.get_text("\n").splitlines() if line.strip()
    )
    pdfs: list[dict] = []
    for link in root.find_all("a", href=True):
        href = str(link["href"])
        if ".pdf" not in href.lower():
            continue
        url = urljoin(BASE, href.split("#", 1)[0])
        if any(row["url"] == url for row in pdfs):
            continue
        pdfs.append({"url": url,
                     "title": " ".join(link.get_text(" ", strip=True).split()) or None})
    return {
        "title": heading.get_text(" ", strip=True) if heading else None,
        "text": text,
        "pdfs": pdfs,
    }


# The DPC stamps its guidance PDFs with the month they were issued rather than
# publishing a date field, and the filename is where it survives:
# ``…/2022-04/Anonymisation and Pseudonymisation - latest April 2022.pdf``.
_PDF_MONTH_RE = re.compile(r"/(?P<year>19|20)(?P<yy>\d{2})-(?P<month>0[1-9]|1[0-2])/")


def guidance_pdf_date(url: str) -> date | None:
    match = _PDF_MONTH_RE.search(url or "")
    if not match:
        return None
    return date(int(f"{match.group('year')}{match.group('yy')}"),
                int(match.group("month")), 1)


def _date(value: str | None) -> date | None:
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((value or "").strip()[:30], fmt).date()
        except ValueError:
            pass
    return None


def dpc_article_relations(values: list[str]) -> list[TypedRelation]:
    """The DPC's own Articles facet distinguishes GDPR articles from Irish
    Data Protection Act sections with an explicit ``S`` prefix."""
    out: list[TypedRelation] = []
    for value in values:
        token = " ".join(value.split())
        if re.fullmatch(r"S(?:ection)?\s*\d+[A-Za-z]?(?:\([^)]*\))*", token, re.I):
            num = re.sub(r"(?i)^S(?:ection)?\s*", "", token)
            dst, anchor, raw = IE_DPA_2018, f"section {num}", f"section {num} of the Data Protection Act 2018"
        elif re.fullmatch(r"\d+[A-Za-z]?(?:\([^)]*\))*", token):
            dst, anchor, raw = GDPR, f"Article {token}", f"Article {token} GDPR"
        else:
            continue
        out.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            dst_id=dst,
            raw_citation_string=raw,
            dst_anchor=anchor,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))
    return out


class IrishDPCAdapter(BaseAdapter):
    source = "ie-dpc"
    min_interval = 1.5

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source,
            min_interval=self.min_interval,
            timeout=90,
            client=_dpc_http_client(),
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # The register is a single un-paginated view of every published decision, so
        # there is no cursor to honour: walk it whole and let the held-prefilter and
        # payload hashes decide what is actually new (INCREMENTAL_MODE "full-walk").
        for item in parse_dpc_listing(self._client.get(LISTING).content):
            slug = urlsplit(item["url"]).path.rstrip("/").rsplit("/", 1)[-1]
            published = _date(item.get("date"))
            yield Stub(
                stable_id=f"ie/dpc/{slug}",
                landing_url=item["url"], raw_url=item["url"],
                title=item["title"], court="dpa-ie",
                hint_date=published,
                hints={
                    "sector": item.get("sector"),
                    "articles": item.get("articles") or [],
                    "watermark": published.isoformat() if published else None,
                },
            )

    def fetch(self, stub: Stub) -> Record | None:
        page = self._client.get(stub.raw_url)
        parsed = parse_dpc_detail(page.content)
        text = str(parsed.get("text") or "")
        raw, ext = page.content, "html"
        attachments: list[dict] = []
        for item in parsed.get("pdfs") or []:
            try:
                pdf = self._client.get(item["url"]).content
                extracted = extract_bytes(pdf, ext="pdf", mime="application/pdf")
            except (FetchError, ValueError):
                continue
            body = (extracted.text or "").strip()
            if body:
                text += "\n\n" + body
                # Store the operative decision itself as the raw payload, not the
                # landing page that links to it.
                if ext != "pdf":
                    raw, ext = pdf, "pdf"
            attachments.append({
                "url": item["url"], "title": item.get("title"),
                "bytes": len(pdf), "text_chars": len(body),
            })
        if len(text) < 50:
            return None
        reference = parsed.get("reference")
        aliases = [reference] if reference else []
        # Prefer the detail page's Articles facet; fall back to the register card's,
        # which carries the same list, when the detail page omits the tag block.
        relations = dpc_article_relations(
            parsed.get("articles") or stub.hints.get("articles") or [])
        sector = parsed.get("sector") or stub.hints.get("sector")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.DECISION,
            title=parsed.get("title") or stub.title,
            court="dpa-ie",
            decision_date=_date(parsed.get("date")) or stub.hint_date,
            language="en", source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw, raw_ext=ext, text=text,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["data-protection", "ireland", "regulatory"],
            extra={
                "jurisdiction": "ie",
                "dpc_reference": reference,
                "articles": parsed.get("articles") or stub.hints.get("articles") or [],
                "sector": sector,
                "dpc_topic": parsed.get("topic"),
                "pdf_url": parsed.get("pdf"),
                "attachments": attachments,
                "aliases": aliases,
                "require_recognized_legal_citation": True,
            },
        )


class IrishDPCGuidanceAdapter(BaseAdapter):
    """The DPC's guidance library, as indexed by its own hub page.

    Separate from the decision register because it is a different kind of document
    with a different identity (a durable topical slug, revised in place, versus a
    dated inquiry outcome) — and because the hub is one page, so the guidance can be
    re-walked cheaply on its own schedule without re-reading the register.
    """

    source = "ie-dpc-guidance"
    min_interval = 1.5

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source,
            min_interval=self.min_interval,
            timeout=90,
            client=_dpc_http_client(),
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # One hub page, no cursor and no publication dates: guidance is revised in
        # place under a stable slug, so a date early-stop would permanently hide
        # revisions. Walk it whole and let the payload hash detect the change.
        for item in parse_dpc_guidance_hub(self._client.get(GUIDANCE_HUB).content):
            yield Stub(
                stable_id=guidance_stable_id(item["url"]),
                landing_url=item["url"], raw_url=item["url"],
                title=item["title"], court="dpa-ie",
                hints={"sections": item["sections"]},
            )

    def fetch(self, stub: Stub) -> Record | None:
        try:
            page = self._client.get(stub.raw_url)
        except FetchError:
            return None
        parsed = parse_dpc_guidance_page(page.content)
        text = str(parsed.get("text") or "")
        raw, ext = page.content, "html"
        published: date | None = None
        attachments: list[dict] = []
        for item in parsed.get("pdfs") or []:
            try:
                pdf = self._client.get(item["url"]).content
                extracted = extract_bytes(pdf, ext="pdf", mime="application/pdf")
            except (FetchError, ValueError):
                continue
            body = (extracted.text or "").strip()
            if body:
                # The landing page is a summary; the guidance note is the document.
                text += "\n\n" + body
                if ext != "pdf":
                    raw, ext = pdf, "pdf"
            issued = guidance_pdf_date(item["url"])
            if issued and (published is None or issued > published):
                published = issued
            attachments.append({
                "url": item["url"], "title": item.get("title"),
                "bytes": len(pdf), "text_chars": len(body),
                "issued": issued.isoformat() if issued else None,
            })
        if len(text.strip()) < 80:
            return None
        sections = [s for s in (stub.hints.get("sections") or []) if s]
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=parsed.get("title") or stub.title,
            court="dpa-ie",
            decision_date=published,
            language="en", source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw, raw_ext=ext, text=text.strip(),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["data-protection", "ireland", "guidance"],
            extra={
                "jurisdiction": "ie",
                "dpc_sections": sections,
                "attachments": attachments,
                # Guidance names the GDPR and the 2018 Act in prose rather than in a
                # citation the resolver would recognise, so requiring one would drop
                # most of the library from retrieval.
                "require_recognized_legal_citation": False,
            },
        )
