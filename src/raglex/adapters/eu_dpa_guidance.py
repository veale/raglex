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
import unicodedata
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter, option_int, resume_floor
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes
from ._governing_instrument import GDPR, default_instrument

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


def _fold_title(value: str | None) -> str:
    """Compare two spellings of one publication's title. The feed and the listing differ
    in trailing whitespace and in typographic apostrophes, and nothing else."""
    text = unicodedata.normalize("NFKD", " ".join((value or "").split()))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def aepd_feed_items(xml: bytes | str) -> list[dict]:
    """The guías RSS, newest first. Unlike the resoluciones feed this one dates its
    items, so a watch can compare against a cursor rather than trusting depth."""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    out: list[dict] = []
    for item in ElementTree.fromstring(xml).iter("item"):
        title = " ".join((item.findtext("title") or "").split())
        url = " ".join((item.findtext("link") or "").split())
        if not title or not url:
            continue
        published = None
        stamp = " ".join((item.findtext("pubDate") or "").split())
        if stamp:
            try:
                published = parsedate_to_datetime(stamp).date()
            except (TypeError, ValueError):
                published = None
        out.append({"title": title, "url": url, "date": published})
    return out


def aepd_document_url(client: RateLimitedClient, page_url: str) -> str | None:
    """The document a feed item points at, or ``None`` where it is not a document.

    ``/recurso-multimedia/{slug}`` is not a landing page at all: it 301s straight to the
    file, which for a guía is ``/guias/{slug}.pdf`` — the very URL the listing links to,
    so a publication reached either way lands on ONE stable id rather than two.

    The redirect is read from the ``Location`` header of an UNFOLLOWED request rather
    than from the final URL of a followed one, and that is not a micro-optimisation:

    * ``HEAD`` on these paths returns **500** for most of the archive while ``GET``
      returns the file, so resolving by HEAD looks like a broken source. Worse, a 500 is
      retried five times with exponential backoff, so fifty feed items took hours.
    * Following the redirect downloads the document — several are over 8 MB — which the
      pipeline is about to do again anyway when it fetches the stub.

    A feed item that redirects to a ``.png`` is a campaign graphic and one that redirects
    nowhere is a video: both are returned as ``None`` rather than stored as documents.
    """
    try:
        response = client.request("GET", page_url, follow_redirects=False,
                                  raise_for_4xx=False)
    except FetchError:
        return None
    location = str(response.headers.get("location") or "")
    if not location:
        return None
    target = urljoin(page_url, location)
    return target if _is_pdf_url(target) else None


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
GARANTE_PAGE_SIZE = 10

#: **The tipologia tree is not expanded server-side.** ``10533`` is the parent node the
#: facet UI labels "Provvedimenti (13702)", and querying it ALONE returns eight pages —
#: about eighty measures, the handful tagged with the parent itself. The other 13,600 are
#: tagged with one of its thirty children and are simply absent, with no error, no
#: warning and a results page that looks perfectly ordinary. The search UI compensates by
#: sending every child id explicitly, and so must we; the first version of this adapter
#: asked for a "Provvedimenti" id that is not in the tree at all (``10515``, which
#: returns three unrelated rows) and harvested nothing but the 38 linee guida beside it.
#:
#: The counts are the facet's own, recorded here so the next person can tell a source
#: change from a parser change:
GARANTE_PROVVEDIMENTI: dict[str, str] = {
    "10533": "Provvedimenti",                       # 13702 — the parent
    "10498": "Decisione su ricorso",                # 6174
    "10526": "Ordinanza ingiunzione o revoca",      # 2195
    "10527": "Parere del Garante",                  # 1561
    "10499": "Deliberazione",                       # 1037
    "10529": "Prescrizioni del Garante",            # 1019
    "10530": "Prescrizioni e divieto del Garante",  # 421
    "10485": "Autorizzazione generale",             # 204
    "10546": "Verifica preliminare",                # 200
    "10532": "Provvedimenti a carattere generale",  # 153
    "10535": "Quesiti di soggetti pubblici e privati",  # 152
    "10500": "Divieto del trattamento",             # 108
    "2034210": "Autorizzazione trasferimento dati estero",  # 90
    "10488": "Blocco del trattamento",              # 81
    "10484": "Autorizzazione",                      # 55
    "2034211": "Bcr",                               # 51
    "2010735": "Provvedimenti ex art. 110 del Codice",  # 40
    "10516": "Linee guida",                         # 38
    "10492": "Consultazione pubblica",              # 37
    "9567234": "Ammonimento",                       # 35
    "10503": "Esonero informativa",                 # 27
    "2024563": "Autorizzazione trasferimento dati verso Paesi terzi",  # 20
    "9150852": "Consultazione preventiva per adozione della DPIA",  # 11
    "9660377": "Avvertimento",                      # 9
    "10528": "Particolari accertamenti",            # 4
    "9445099": "Accreditamento degli organismi di certificazione",  # 3
    # Four types the facet counts at zero today. They are the post-GDPR corrective
    # vocabulary — the categories the Garante will file future measures under — so they
    # are asked for now rather than discovered missing later.
    "9161733": "Estinzione di procedimento sanzionatorio",
    "9150851": "Provvedimenti correttivi anche non prescrittivi",
    "9271403": "Provvedimento correttivo e sanzionatorio",
    "9625871": "Provvedimento prescrittivo e sanzionatorio",
    "9126615": "Richiesta di parere a EDPB",
}
#: The top-level categories that are NOT under Provvedimenti and are still law: the
#: inspection programme, approved codes of conduct, the Garante's own regulations and
#: the regole deontologiche (which are binding under Article 2-quater of the Codice).
GARANTE_OTHER: dict[str, str] = {
    "10170750": "Attività ispettiva",               # 16
    "9119875": "Codice di condotta",                # 4
    "2038802": "Regolamento del Garante",           # 4
    "2005281": "AllegatiCodice",                    # 4
    "9615872": "Regole deontologiche",              # 1
    "7447479": "Note istituzionali",                # 1
}
#: One query, not one per type. The portlet takes a comma-separated id list, and asking
#: once keeps discovery a SINGLE ordered series — which is what lets a resumed backfill
#: start on the right page. A per-type series would give each slice its own counter, and
#: a resume would restart in the middle of whichever slice it happened to reach: the bug
#: Finlex had (AGENTS.md §1, "the cursor must count the whole feed").
GARANTE_TYPE_IDS = ",".join([*GARANTE_PROVVEDIMENTI, *GARANTE_OTHER])
GARANTE_TYPE_LABELS = {**GARANTE_PROVVEDIMENTI, **GARANTE_OTHER}
#: Kept for callers that still name the old two-entry mapping.
GARANTE_TYPES = GARANTE_TYPE_LABELS


def garante_search_url(type_id: str, page: int, *, date_from: str = "",
                       date_to: str = "") -> tuple[str, dict]:
    """The portlet URL for one page of results.

    ``dataInizio``/``dataFine`` really do filter — a 2020-01 window returns January 2020
    and nothing else, checked against the unfiltered walk — which is what makes the
    keep-current path a handful of requests instead of 1,380.
    """
    params = {
        "p_p_id": "g_gpdp5_search_GGpdp5SearchPortlet",
        "p_p_lifecycle": "0", "p_p_state": "normal", "p_p_mode": "view",
        f"{_GARANTE_PORTLET}mvcRenderCommandName": "/renderSearch",
        f"{_GARANTE_PORTLET}text": "",
        f"{_GARANTE_PORTLET}dataInizio": date_from,
        f"{_GARANTE_PORTLET}dataFine": date_to,
        f"{_GARANTE_PORTLET}idsTipologia": type_id,
        f"{_GARANTE_PORTLET}ordinamentoPer": "DESC",
        f"{_GARANTE_PORTLET}ordinamentoTipo": "data",
        f"{_GARANTE_PORTLET}cur": str(page),
    }
    return f"{GARANTE_BASE}/home/ricerca", params


_DOCWEB_RE = re.compile(r"/docweb-display/docweb/(?P<id>\d+)")


_GARANTE_DMY = re.compile(r"(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>(?:19|20)\d{2})")


def garante_stubs(html: bytes | str) -> list[dict]:
    """Search results keyed on the *doc web* number, with the card's own metadata.

    That number is the Garante's permanent identifier — it appears in the URL, in square
    brackets at the end of the title and in the way every Italian lawyer cites the
    measure ("doc. web n. 10241943"), which makes it the right stable id.

    Each result card also carries three things the docweb page does not put anywhere a
    parser can find: the **tipologia** (ordinanza ingiunzione, parere, ammonimento — the
    difference between a fine and an opinion), the **argomenti** (the Garante's own
    297-term subject taxonomy: telemarketing, diritto all'oblio, videosorveglianza,
    intelligenza artificiale) and the **adoption date** as ``dd/mm/yyyy``. The date is
    read from the card rather than parsed out of the title, because roughly a third of
    the archive is titled "Parere su istanza di accesso civico" or "Newsletter" with no
    date in it at all.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for card in soup.select(".card-risultato"):
        link = card.select_one("a.titolo-risultato[href]")
        if link is None:
            continue
        match = _DOCWEB_RE.search(str(link.get("href") or ""))
        if not match:
            continue
        docweb = match.group("id")
        if docweb in seen:
            continue
        seen.add(docweb)
        title = " ".join(link.get_text(" ", strip=True).split())
        stamp = card.select_one(".data-risultato")
        printed = _GARANTE_DMY.search(stamp.get_text(" ", strip=True)) if stamp else None
        published = None
        if printed:
            try:
                published = date(int(printed.group("y")), int(printed.group("m")),
                                 int(printed.group("d")))
            except ValueError:
                published = None
        # The tipologia links sit in the card's label block and the argomenti in its
        # tail; both are rendered as ``/home/ricerca/-/search/{facet}/{value}`` links,
        # which is the only thing distinguishing them from one another in the markup.
        kinds = [" ".join(a.get_text(" ", strip=True).split())
                 for a in card.select('a[href*="/search/tipologia/"]')]
        topics = [" ".join(a.get_text(" ", strip=True).split())
                  for a in card.select('a[href*="/search/argomento/"]')]
        excerpt = card.select_one(".estratto-risultato")
        out.append({
            "url": f"{GARANTE_BASE}/web/guest/home/docweb/-/docweb-display/docweb/{docweb}",
            "title": re.sub(rf"\s*\[{docweb}\]\s*$", "", title),
            "category": kinds[0] if kinds else None,
            "categories": kinds,
            "topics": topics,
            "summary": (" ".join(excerpt.get_text(" ", strip=True).split())
                        if excerpt else None),
            # The card's date is authoritative; the title's is the fallback for the few
            # cards the portlet renders without one.
            "date": published or month_date(title, "it"),
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
    #: Items per index page, for the registers whose index has a fixed one. Non-zero
    #: turns on the resume cursor: the position of each item in the whole walk is put on
    #: its stub as ``resume_offset``, and a resumed run restarts on that page. Zero
    #: means "this register is a few pages and reports no cursor" — which is honest for
    #: the five hub-page libraries here and is why they are not obliged to resume.
    page_size = 0
    #: The instrument this register's documents are about when their own title does not
    #: name one. See ``_governing_instrument``; ``None`` for a mixed register.
    default_instrument: dict | None = None

    def __init__(self, *, start_offset: int | str | None = None,
                 client: RateLimitedClient | None = None) -> None:
        # Accepted by every subclass even though only the paged ones report a cursor:
        # ``jobs`` passes it to whichever adapter it is resuming, and one that cannot
        # take the keyword raises TypeError and files the retry as **done**
        # (AGENTS.md §1). ``resume_floor`` backs off a page on purpose.
        self.start_offset = resume_floor(option_int(start_offset, 0), self.page_size or 1)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)

    def _resume_page(self) -> int:
        """The 0-based index page a resumed walk restarts on."""
        return self.start_offset // self.page_size if self.page_size else 0

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
        # Where in the WHOLE walk we are — across every series, not within one. A
        # per-series counter would restart a resumed run in the middle of whichever
        # series it happened to reach (AGENTS.md §1).
        emitted = self._resume_page() * self.page_size
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
                    hints = {k: v for k, v in item.items()
                             if k not in ("url", "title", "date")} | {
                        "watermark": published.isoformat() if published else None}
                    if self.page_size:
                        hints["resume_offset"] = emitted
                    emitted += 1
                    yield Stub(
                        stable_id=key,
                        landing_url=item["url"], raw_url=item["url"],
                        title=item.get("title"), court=self.court,
                        hint_date=published, hints=hints,
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
        topics = [t for t in (stub.hints.get("topics") or []) if t]
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
                        *([category.casefold()] if category else []),
                        *(t.casefold() for t in topics)],
            extra={
                "jurisdiction": self.jurisdiction,
                "category": category,
                "categories": stub.hints.get("categories") or (
                    [category] if category else []),
                "topics": topics,
                "date_precision": stub.hints.get("date_precision"),
                "summary": stub.hints.get("summary"),
                "attachments": attachments,
                # A register whose documents are all about one instrument declares it,
                # so a measure that names the Regulation once in its *visto* and then
                # argues in bare articles for ten pages still links to those articles.
                # A document whose own title names another instrument overrides it.
                "citation_default_instrument": default_instrument(
                    stub.title, self.default_instrument),
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
    """The AEPD's guías, from the listing AND its RSS feed.

    The two do not agree, and the feed is the larger. The listing view reports 111
    results across fourteen pages; the feed carries 160 distinct items back to 2016, a
    strict superset — reconciled item by item, every listing URL is in the feed and 49
    feed URLs are not in the listing. The extras are the same authority's decálogos,
    campaign infographics and institutional declarations, published as ``recurso-
    multimedia`` like the guías and simply not promoted into the guías view. Several are
    substantive (the *canal prioritario* material, the school-platform principles); the
    rest are videos, which resolve to no PDF and are dropped by the shared fetch.

    So both are walked. The listing first, because it links straight to the PDF and is
    fourteen requests; then the feed, whose items link to a landing page and need one
    request each to reach their document — paid only for the items the listing did not
    already supply, which is what keeps a monthly watch to about fifty requests instead
    of a hundred and sixty.
    """

    source = "es-aepd-guias"
    court, language, jurisdiction = "dpa-es", "es", "es"
    tags, id_prefix = ("spain", "aepd"), "es/aepd"
    default_instrument = GDPR

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        #: Titles the listing has already supplied this run. A feed item matching one of
        #: them is the same publication and needs no landing-page request.
        self._listing_titles: set[str] = set()

    def _series(self, max_pages):
        yield _numbered(f"{AEPD_BASE}/guias-y-herramientas/guias", max_pages)
        yield iter([(f"{AEPD_BASE}/guias-y-herramientas/guias/feed.xml", {},
                     {"feed": True})])

    def _parse(self, html: bytes, url: str, context: dict) -> list[dict]:
        if not context.get("feed"):
            rows = aepd_stubs(html)
            self._listing_titles.update(_fold_title(r["title"]) for r in rows)
            return rows
        return self._feed_rows(html)

    def _feed_rows(self, xml: bytes) -> list[dict]:
        out: list[dict] = []
        for item in aepd_feed_items(xml):
            if _fold_title(item["title"]) in self._listing_titles:
                continue
            document = aepd_document_url(self._client, item["url"])
            if not document:
                continue        # a campaign graphic or a video: no document to hold
            out.append({"url": document, "title": item["title"], "category": "Guía",
                        "date": item["date"]})
        return out


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
    """The Garante's whole measure archive: ~13,800 documents back to 1996.

    Ordinanze ingiunzione, decisioni su ricorso, pareri, prescrizioni, ammonimenti,
    autorizzazioni generali, BCR approvals, linee guida, codici di condotta and the
    regole deontologiche — every tipologia the search facet offers, asked for by id
    because the parent node does not expand (see :data:`GARANTE_PROVVEDIMENTI`).

    One ordered series of 1,380 pages, ten to a page, newest first, so the walk has a
    single durable cursor. Keep-current uses the portlet's ``dataInizio``/``dataFine``
    window instead of walking: the filter genuinely filters (a January 2020 window
    returns January 2020), so a monthly check costs a handful of requests.

    Roughly a hundred measures are published as an attachment rather than as a web page.
    Those docweb pages are a 23 kB shell whose only content is a link into
    ``/documents/10160/…``; the shared fetch follows it, so the measure's text comes from
    the PDF and the shell is not stored as though it were the document.
    """

    source = "it-garante"
    court, language, jurisdiction = "dpa-it", "it", "it"
    tags, id_prefix = ("italy", "garante"), "it/garante/docweb"
    page_size = GARANTE_PAGE_SIZE
    default_instrument = GDPR
    #: How far back a keep-current window reaches. Generous on purpose: the Garante
    #: publishes a measure weeks after it signs it, and the search sorts on the signature
    #: date, so a new document lands *behind* the cursor rather than in front of it.
    watch_days = 180

    def __init__(self, *, watch_days: int | str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.watch_days = max(1, option_int(watch_days, type(self).watch_days))

    def stable_id(self, item: dict) -> str:
        return f"{self.id_prefix}/{item['docweb']}"

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        self._window = None
        if since:
            cutoff = str(since)[:10]
            try:
                start = date.fromisoformat(cutoff) - timedelta(days=self.watch_days)
            except ValueError:
                start = date.today() - timedelta(days=self.watch_days)
            self._window = (start.isoformat(), "")
        yield from super().discover(since, max_pages=max_pages)

    def _series(self, max_pages):
        date_from, date_to = getattr(self, "_window", None) or ("", "")
        # 1-based cursor. A resumed backfill starts on the page its checkpoint names
        # rather than re-requesting everything before it — safe here, and only here,
        # because the page size is fixed and the whole walk is ONE series, so the
        # position of an item in the feed determines its page exactly.
        first = self._resume_page() + 1
        end = None if max_pages is None else first + max(0, max_pages) - 1

        def pages():
            page = first
            while end is None or page <= end:
                url, params = garante_search_url(
                    GARANTE_TYPE_IDS, page, date_from=date_from, date_to=date_to)
                yield url, params, {}
                page += 1

        yield pages()

    def _parse(self, html: bytes, url: str, context: dict) -> list[dict]:
        return garante_stubs(html)

    def _page_text(self, html: bytes) -> str:
        return garante_text(html)
