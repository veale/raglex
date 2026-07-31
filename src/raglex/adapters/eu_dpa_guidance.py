"""National data-protection authorities' guidance libraries.

Six registers that publish the same *kind* of document — a supervisory authority's
interpretation of the GDPR — in six house styles: the CNIL's médiathèque, the AEPD's
guías, Datatilsynet's vejledninger, the DSK's Orientierungshilfen, the Belgian APD/GBA's
publications and the Garante's provvedimenti. They differ only in how the index is
paged and where the operative text sits, so the index parsers are per-authority
functions and everything after them — fetch the landing page, pull the attached PDFs,
concatenate, record — is shared.

The EDPB's own guidelines are deliberately NOT here: the ``edpb`` source already holds
them, and several of these authorities re-list them.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

# Month names, for the authorities that print a date instead of publishing one.
_MONTHS: dict[str, dict[str, int]] = {
    "de": {"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
           "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
           "november": 11, "dezember": 12},
    "it": {"gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5,
           "giugno": 6, "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10,
           "novembre": 11, "dicembre": 12},
    "es": {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
           "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
           "diciembre": 12},
}


def month_date(value: str | None, language: str) -> date | None:
    """``17 aprile 2026`` / ``Januar 2026`` → a date. A month-only stamp becomes the
    first of that month; the imprecision is recorded on the record, not hidden."""
    text = (value or "").casefold()
    months = _MONTHS.get(language, {})
    match = re.search(rf"(?:(\d{{1,2}})\s*(?:°|º)?\s+)?\b({'|'.join(months)})\b\s+((?:19|20)\d{{2}})",
                      text)
    if not match:
        return None
    try:
        return date(int(match.group(3)), months[match.group(2)],
                    int(match.group(1) or 1))
    except ValueError:
        return None


# Path segments that say nothing about which document this is, so including them in an
# id would only add noise. Anything else (the CNIL's ``2026-06`` upload month, a series
# folder) is kept, because two authorities do reuse a filename across folders and a
# collision would silently merge two documents into one.
_GENERIC_DIRS = {"sites", "default", "files", "media", "documents", "document",
                 "publications", "publication", "guias", "guides", "download",
                 "downloads", "uploads", "system", "fileadmin", "oh", "kp"}


def _slug(url: str) -> str:
    """A stable, readable key from the tail of the path, extension dropped."""
    parts = [part for part in urlsplit(url).path.split("/") if part]
    if not parts:
        return "document"
    name = re.sub(r"\.(pdf|html?|aspx)$", "", parts[-1], flags=re.I)
    parent = parts[-2] if len(parts) > 1 else ""
    if len(parent) > 1 and parent.casefold() not in _GENERIC_DIRS:
        name = f"{parent}-{name}"
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "document"


def _is_pdf_url(url: str) -> bool:
    return ".pdf" in urlsplit(url).path.casefold()


# ---------------------------------------------------------------------------
# France — CNIL médiathèque
# ---------------------------------------------------------------------------

CNIL_BASE = "https://www.cnil.fr"


def cnil_stubs(html: bytes | str) -> list[dict]:
    """The médiathèque, one dict per publication.

    Each ``.views-row`` renders the SAME item twice — once for the list layout and once
    for the grid — so a naive link sweep doubles the corpus. Take the first title per
    row. ``.collection__type`` is the CNIL's own classification (Guide, Lignes
    directrices, Recommandations, Fiche pratique…), which is worth keeping: it is the
    difference between a binding-in-practice recommendation and a leaflet.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for row in soup.select(".views-row"):
        link = row.select_one("h3.ctn-gen-liste-titre a[href]")
        if not link:
            continue
        url = urljoin(CNIL_BASE, str(link.get("href") or "").split("#", 1)[0])
        if url in seen:
            continue
        seen.add(url)
        kind = row.select_one(".collection__type")
        out.append({
            "url": url,
            "title": " ".join(link.get_text(" ", strip=True).split()),
            "category": " ".join(kind.get_text(" ", strip=True).split()) if kind else None,
            # The médiathèque prints no date; the upload folder carries the month.
            "date": pdf_month(url),
        })
    return out


_UPLOAD_MONTH_RE = re.compile(r"/((?:19|20)\d{2})-(0[1-9]|1[0-2])/")


def pdf_month(url: str) -> date | None:
    """Drupal files land in ``/sites/default/files/YYYY-MM/`` — for several of these
    authorities that folder is the only publication date exposed anywhere."""
    match = _UPLOAD_MONTH_RE.search(url or "")
    return date(int(match.group(1)), int(match.group(2)), 1) if match else None


# ---------------------------------------------------------------------------
# Spain — AEPD guías
# ---------------------------------------------------------------------------

AEPD_BASE = "https://www.aepd.es"


def aepd_stubs(html: bytes | str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for teaser in soup.select("article.node--view-mode-teaser"):
        link = teaser.select_one(".field--name-fichero a[href]") or teaser.select_one(
            'a[href$=".pdf"]')
        heading = teaser.select_one(".field--name-title h2, h2")
        if not link or not heading:
            continue
        stamp = teaser.select_one("time[datetime]")
        published = None
        if stamp is not None:
            try:
                published = datetime.fromisoformat(
                    str(stamp["datetime"]).replace("Z", "+00:00")).date()
            except (KeyError, ValueError):
                published = None
        out.append({
            "url": urljoin(AEPD_BASE, str(link.get("href") or "")),
            "title": " ".join(heading.get_text(" ", strip=True).split()),
            "category": "Guía",
            "date": published,
        })
    return out


# ---------------------------------------------------------------------------
# Denmark — Datatilsynet, "Regler og vejledning"
# ---------------------------------------------------------------------------

DK_BASE = "https://www.datatilsynet.dk"


def datatilsynet_stubs(html: bytes | str) -> list[dict]:
    """One flat hub page carrying every vejledning as a ``/Media/…`` PDF.

    There is no listing view and no pagination — the whole library is this page's link
    set, grouped under topic headings (Registreredes rettigheder, Politi og retsvæsen…)
    which are kept as the category. A handful of links are editing accidents pointing at
    someone's ``C:\\Users\\…\\Downloads`` folder; those are skipped rather than fetched.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    section: str | None = None
    root = soup.select_one("main") or soup
    for node in root.find_all(["h2", "h3", "a"]):
        if node.name in ("h2", "h3"):
            section = " ".join(node.get_text(" ", strip=True).split()) or section
            continue
        href = str(node.get("href") or "")
        if not _is_pdf_url(href) or "file:" in href.casefold():
            continue
        url = urljoin(DK_BASE, href.split("#", 1)[0])
        if url in seen:
            continue
        seen.add(url)
        title = " ".join(node.get_text(" ", strip=True).split())
        if not title:
            continue
        out.append({"url": url, "title": title, "category": section, "date": None})
    return out


# ---------------------------------------------------------------------------
# Germany — Datenschutzkonferenz
# ---------------------------------------------------------------------------

DSK_BASE = "https://www.datenschutzkonferenz-online.de"
# The DSK's three standing series. Separate pages, identical markup; they are the
# German supervisory authorities' joint position and are cited as such.
DSK_PAGES = {
    "orientierungshilfen": "Orientierungshilfe",
    "kurzpapiere": "Kurzpapier",
    "beschluesse-dsk": "Beschluss",
}


def dsk_stubs(html: bytes | str, page_url: str, category: str) -> list[dict]:
    """``<li class="thumbnail">`` per document; ``li.thumbnail.head`` is a year divider.

    The visible label is ``Januar 2026 - Orientierungshilfe zur …``, so the month is
    parsed out and the remainder is the title. An item may carry an annex as a second
    PDF in its ``.hint-box``; both are fetched into the one document.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for item in soup.select("li.thumbnail"):
        if "head" in (item.get("class") or []):
            continue
        link = item.find("a", href=True)
        if not link:
            continue
        label = " ".join(item.get_text(" ", strip=True).split())
        stamp = item.select_one(".date b")
        printed = " ".join(stamp.get_text(" ", strip=True).split()) if stamp else ""
        title = label
        if printed and label.startswith(printed):
            title = label[len(printed):].lstrip(" -–—")
        extras = [urljoin(page_url, str(a["href"]))
                  for a in item.select(".hint-box a[href]") if a.get("href")]
        out.append({
            "url": urljoin(page_url, str(link["href"]).split("#", 1)[0]),
            "title": title or label,
            "category": category,
            "date": month_date(printed, "de"),
            "extra_pdfs": extras,
        })
    return out


# ---------------------------------------------------------------------------
# Belgium — Autorité de protection des données / Gegevensbeschermingsautoriteit
# ---------------------------------------------------------------------------

GBA_BASE = "https://www.autoriteprotectiondonnees.be"
# The publication types the faceted search exposes. Recommendations and advice are the
# APD's interpretive output; "documentation" is its explanatory material.
GBA_TYPES = ("recommendation", "advice", "documentation")


def gba_stubs(html: bytes | str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for media in soup.select("#search-result .media, .media"):
        link = media.select_one("h3.media-title a[href]")
        if not link:
            continue
        url = urljoin(GBA_BASE, str(link.get("href") or "").split("#", 1)[0])
        if url in seen:
            continue
        seen.add(url)
        year = media.select_one(".media-date")
        summary = media.select_one(".media-description")
        printed = " ".join(year.get_text(" ", strip=True).split()) if year else ""
        match = re.search(r"\b((?:19|20)\d{2})\b", printed)
        out.append({
            "url": url,
            "title": " ".join(link.get_text(" ", strip=True).split()),
            "category": None,
            # The search prints the year only; keep it (it is what orders the register)
            # and mark the precision so nothing reads 1 January as a real date.
            "date": date(int(match.group(1)), 1, 1) if match else None,
            "date_precision": "year" if match else None,
            "summary": " ".join(summary.get_text(" ", strip=True).split())
            if summary else None,
        })
    return out


# ---------------------------------------------------------------------------
# Italy — Garante per la protezione dei dati personali
# ---------------------------------------------------------------------------

GARANTE_BASE = "https://www.garanteprivacy.it"
# The Liferay search portlet. Every parameter is required for the portlet to render;
# ``idsTipologia`` selects the document type and ``cur`` is the 1-based page.
_GARANTE_PORTLET = "_g_gpdp5_search_GGpdp5SearchPortlet_"
# Tipologia ids from the search UI's own facet links.
GARANTE_TYPES = {
    "10516": "Linee guida",
    "10515": "Provvedimenti",
}


def garante_search_url(type_id: str, page: int) -> tuple[str, dict]:
    params = {
        "p_p_id": "g_gpdp5_search_GGpdp5SearchPortlet",
        "p_p_lifecycle": "0", "p_p_state": "normal", "p_p_mode": "view",
        f"{_GARANTE_PORTLET}mvcRenderCommandName": "/renderSearch",
        f"{_GARANTE_PORTLET}text": "",
        f"{_GARANTE_PORTLET}idsTipologia": type_id,
        f"{_GARANTE_PORTLET}ordinamentoPer": "DESC",
        f"{_GARANTE_PORTLET}ordinamentoTipo": "data",
        f"{_GARANTE_PORTLET}cur": str(page),
    }
    return f"{GARANTE_BASE}/home/ricerca", params


_DOCWEB_RE = re.compile(r"/docweb-display/docweb/(?P<id>\d+)")


def garante_stubs(html: bytes | str) -> list[dict]:
    """Search results keyed on the *doc web* number.

    That number is the Garante's own permanent identifier — it appears in the URL, in
    square brackets at the end of the title and in the way every Italian lawyer cites
    the measure ("doc. web n. 10241943"), which makes it the right stable id. The title
    also carries the adoption date ("Provvedimento del 17 aprile 2026 - …").
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for link in soup.select("a.titolo-risultato[href]"):
        match = _DOCWEB_RE.search(str(link.get("href") or ""))
        if not match:
            continue
        docweb = match.group("id")
        if docweb in seen:
            continue
        seen.add(docweb)
        title = " ".join(link.get_text(" ", strip=True).split())
        out.append({
            "url": f"{GARANTE_BASE}/web/guest/home/docweb/-/docweb-display/docweb/{docweb}",
            "title": re.sub(rf"\s*\[{docweb}\]\s*$", "", title),
            "category": None,
            "date": month_date(title, "it"),
            "docweb": docweb,
        })
    return out


def garante_text(html: bytes | str) -> str:
    """The measure's text from a docweb page.

    Liferay renders the site's navigation through the same portlet classes as the
    content, so ``select_one`` on a class picks whichever portlet happens to come first
    in the document — on this site a social-links block, giving 56 characters of "Seguici
    su … instagram". Take the RICHEST match instead of the first; ``#div-to-print`` is
    the print view of the measure and is the reliable one when present.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script, style, nav, form, .nav, .dropdown-menu, footer"):
        tag.decompose()
    candidates = soup.select(
        "#div-to-print, .testo, .journal-content-article, #main-content, main")
    body = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)),
               default=None) or soup
    return "\n".join(" ".join(line.split())
                     for line in body.get_text("\n").splitlines() if line.strip())


# ---------------------------------------------------------------------------
# the shared adapter
# ---------------------------------------------------------------------------

class _DPAGuidanceAdapter(BaseAdapter):
    """Index → item → text, for a national DPA's guidance register.

    Subclasses supply ``_series()`` (the index pages to walk, newest first) and the parse
    function that turns one index page into item dicts. Everything below that is the
    same everywhere: an item is either a PDF outright or a landing page with PDFs
    attached, and either way the record is the concatenation.
    """

    court = ""
    language = "en"
    jurisdiction = ""
    tags: tuple[str, ...] = ()
    doc_type = DocType.GUIDANCE
    id_prefix = ""
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)

    def _series(self, max_pages: int | None) -> Iterator[Iterator[tuple[str, dict, dict]]]:
        """Yield one page-generator per independent series.

        A register is often several series that happen to share markup — the DSK's
        Orientierungshilfen / Kurzpapiere / Beschlüsse, the Belgian APD's advice /
        recommendations / documentation, the Garante's linee guida and provvedimenti.
        Exhausting one must move on to the next, not end the crawl, which is why this
        is a generator of generators rather than one flat page list.
        """
        raise NotImplementedError

    def _parse(self, html: bytes, url: str, context: dict) -> list[dict]:
        raise NotImplementedError

    def stable_id(self, item: dict) -> str:
        return f"{self.id_prefix}/{_slug(item['url'])}"

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # These registers publish revisions in place under the same URL and mostly
        # expose no usable date cursor, so they are walked whole; the payload hash is
        # what tells a revision from a re-read (INCREMENTAL_MODE "full-walk").
        seen: set[str] = set()
        for pages in self._series(max_pages):
            for url, params, context in pages:
                try:
                    response = self._client.get(url, params=params or None)
                except FetchError:
                    break
                items = self._parse(response.content, str(response.url), context)
                fresh = 0
                for item in items:
                    key = self.stable_id(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    fresh += 1
                    published = item.get("date")
                    yield Stub(
                        stable_id=key,
                        landing_url=item["url"], raw_url=item["url"],
                        title=item.get("title"), court=self.court,
                        hint_date=published,
                        hints={k: v for k, v in item.items()
                               if k not in ("url", "title", "date")} | {
                            "watermark": published.isoformat() if published else None},
                    )
                # A page that adds nothing is the end of THIS series: either it was
                # empty or the view has started repeating itself past its last page.
                if not fresh:
                    break

    def _document_text(self, html: bytes) -> str:
        soup = BeautifulSoup(html, "html.parser")
        body = soup.select_one("main") or soup.select_one("article") or soup
        for tag in body.select("script, style, nav, form, header, footer"):
            tag.decompose()
        return "\n".join(" ".join(line.split())
                         for line in body.get_text("\n").splitlines() if line.strip())

    def _pdf_links(self, html: bytes, page_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        body = soup.select_one("main") or soup.select_one("article") or soup
        out: list[str] = []
        for link in body.find_all("a", href=True):
            href = str(link["href"]).split("#", 1)[0]
            if not _is_pdf_url(href):
                continue
            url = urljoin(page_url, href)
            if url not in out:
                out.append(url)
        return out

    def fetch(self, stub: Stub) -> Record | None:
        text, raw, ext = "", None, None
        attachments: list[dict] = []
        pdfs: list[str] = list(stub.hints.get("extra_pdfs") or [])
        if _is_pdf_url(stub.raw_url):
            pdfs.insert(0, stub.raw_url)
        else:
            try:
                page = self._client.get(stub.raw_url)
            except FetchError:
                return None
            text = self._page_text(page.content)
            raw, ext = page.content, "html"
            for url in self._pdf_links(page.content, str(page.url)):
                if url not in pdfs:
                    pdfs.append(url)
        for url in pdfs:
            try:
                blob = self._client.get(url).content
                extracted = extract_bytes(blob, ext="pdf", mime="application/pdf")
            except (FetchError, ValueError):
                continue
            if not blob.startswith(b"%PDF"):
                continue
            body = (extracted.text or "").strip()
            if body:
                text = f"{text}\n\n{body}".strip()
                if ext != "pdf":
                    raw, ext = blob, "pdf"
            attachments.append({"url": url, "title": None, "bytes": len(blob),
                                "text_chars": len(body)})
        if len(text.strip()) < 120:
            return None
        category = stub.hints.get("category")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=self.doc_type,
            title=stub.title,
            court=self.court,
            decision_date=stub.hint_date,
            language=self.language, source_language=self.language,
            landing_url=stub.landing_url,
            raw_bytes=raw, raw_ext=ext, text=text.strip(),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["data-protection", *self.tags,
                        *([category.casefold()] if category else [])],
            extra={
                "jurisdiction": self.jurisdiction,
                "category": category,
                "date_precision": stub.hints.get("date_precision"),
                "summary": stub.hints.get("summary"),
                "attachments": attachments,
                # A DPA's guidance library is single-subject: everything in it is about
                # the GDPR and its national implementing act, and much of it explains
                # the law without formally citing it. Gating on a recognised citation
                # would drop exactly the plain-language guidance that is most read.
                "require_recognized_legal_citation": False,
            },
        )

    def _page_text(self, html: bytes) -> str:
        return self._document_text(html)


def _numbered(url: str, max_pages: int | None, *, key: str = "page",
              start: int = 0, context: dict | None = None,
              extra: dict | None = None) -> Iterator[tuple[str, dict, dict]]:
    """A plain ``?page=N`` series, bounded by ``max_pages`` when the caller asked."""
    page = start
    while max_pages is None or page - start < max_pages:
        yield url, {**(extra or {}), key: page}, dict(context or {})
        page += 1


class CNILGuidanceAdapter(_DPAGuidanceAdapter):
    source = "fr-cnil-guidance"
    court, language, jurisdiction = "dpa-fr", "fr", "fr"
    tags, id_prefix = ("france", "cnil"), "fr/cnil/guidance"

    def _series(self, max_pages):
        yield _numbered(f"{CNIL_BASE}/fr/mediatheque", max_pages)

    def _parse(self, html: bytes, url: str, context: dict) -> list[dict]:
        return cnil_stubs(html)


class AEPDGuidanceAdapter(_DPAGuidanceAdapter):
    source = "es-aepd-guias"
    court, language, jurisdiction = "dpa-es", "es", "es"
    tags, id_prefix = ("spain", "aepd"), "es/aepd"

    def _series(self, max_pages):
        yield _numbered(f"{AEPD_BASE}/guias-y-herramientas/guias", max_pages)

    def _parse(self, html: bytes, url: str, context: dict) -> list[dict]:
        return aepd_stubs(html)


class DatatilsynetGuidanceAdapter(_DPAGuidanceAdapter):
    source = "dk-datatilsynet"
    court, language, jurisdiction = "dpa-dk", "da", "dk"
    tags, id_prefix = ("denmark", "datatilsynet"), "dk/datatilsynet"

    def _series(self, max_pages):
        # One page holds the whole library; there is nothing to paginate.
        yield iter([(f"{DK_BASE}/regler-og-vejledning", {}, {})])

    def _parse(self, html: bytes, url: str, context: dict) -> list[dict]:
        return datatilsynet_stubs(html)


class DSKGuidanceAdapter(_DPAGuidanceAdapter):
    source = "de-dsk"
    court, language, jurisdiction = "dpa-de", "de", "de"
    tags, id_prefix = ("germany", "dsk"), "de/dsk"

    def _series(self, max_pages):
        for slug, category in DSK_PAGES.items():
            yield iter([(f"{DSK_BASE}/{slug}.html", {}, {"category": category})])

    def _parse(self, html: bytes, url: str, context: dict) -> list[dict]:
        return dsk_stubs(html, url, context["category"])


class GBAGuidanceAdapter(_DPAGuidanceAdapter):
    source = "be-gba"
    court, jurisdiction = "dpa-be", "be"
    # The register is published in French and Dutch; the French surface is harvested and
    # the language marked accordingly rather than claiming a bilingual document.
    language = "fr"
    tags, id_prefix = ("belgium", "gba"), "be/gba"

    def _series(self, max_pages):
        for kind in GBA_TYPES:
            yield _numbered(
                f"{GBA_BASE}/citoyen/chercher", max_pages, key="p",
                extra={"search_category[0]": "taxonomy:publications",
                       "search_type[0]": kind, "s": "recent", "l": 25},
                context={"category": kind})

    def _parse(self, html: bytes, url: str, context: dict) -> list[dict]:
        rows = gba_stubs(html)
        for row in rows:
            row["category"] = context["category"]
        return rows


class GaranteGuidanceAdapter(_DPAGuidanceAdapter):
    source = "it-garante"
    court, language, jurisdiction = "dpa-it", "it", "it"
    tags, id_prefix = ("italy", "garante"), "it/garante/docweb"

    def stable_id(self, item: dict) -> str:
        return f"{self.id_prefix}/{item['docweb']}"

    def _series(self, max_pages):
        def pages(type_id: str, label: str):
            page = 1  # the portlet's cursor is 1-based
            while max_pages is None or page <= max_pages:
                url, params = garante_search_url(type_id, page)
                yield url, params, {"category": label}
                page += 1

        for type_id, label in GARANTE_TYPES.items():
            yield pages(type_id, label)

    def _parse(self, html: bytes, url: str, context: dict) -> list[dict]:
        rows = garante_stubs(html)
        for row in rows:
            row["category"] = context["category"]
        return rows

    def _page_text(self, html: bytes) -> str:
        return garante_text(html)
