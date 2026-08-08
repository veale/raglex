"""Parliamentary and oversight research: the two Libraries, SPICe, IPCO and the ISC.

Each of these has one property that, got wrong, empties the source silently rather than
failing — the RSS parsed as HTML, the Scottish search's default window, the ISC's
collapsed accordions, IPCO's lastmod. Those are what these tests pin.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.adapters import scot_spice, uk_ipco, uk_isc, uk_parl_library
from raglex.core.models import Stub

DATA = Path(__file__).parent / "data" / "uk_research"

KEYS = ("uk-commons-library", "uk-lords-library", "scot-spice", "uk-ipco", "uk-isc")


# --- registration ------------------------------------------------------------

@pytest.mark.parametrize("key,kind,mode", [
    ("uk-commons-library", "preparatory", "early-stop"),
    ("uk-lords-library", "preparatory", "early-stop"),
    ("scot-spice", "preparatory", "early-stop"),
    ("uk-ipco", "guidance", "full-walk"),
    ("uk-isc", "guidance", "full-walk"),
])
def test_registered(key, kind, mode):
    assert key in ADAPTERS and key in SOURCE_INFO
    info = SOURCE_INFO[key]
    assert info.kind == kind
    assert info.jurisdiction == "GB"
    assert INCREMENTAL_MODE[key] == mode
    assert ADAPTERS[key]().source == key


def test_in_the_public_catalogue_under_one_united_kingdom_group():
    from raglex.adapters.registry import source_catalog

    rows = {row["key"]: row for row in source_catalog()}
    for key in KEYS:
        assert rows[key]["group_label"] == "United Kingdom"
        assert rows[key]["can_incremental"] is True


def test_every_declared_option_is_accepted_by_the_constructor():
    for key in KEYS:
        for option in SOURCE_INFO[key].options:
            ADAPTERS[key](**{option.name: option.placeholder or "1"})


def test_a_blank_option_means_the_default_not_false():
    """The forms send an untouched checkbox as None, and ``bool(None)`` is False — which
    turned OCR and press notices off for everyone who never opened the options."""
    assert ADAPTERS["uk-isc"](ocr=None, include_press=None, max_ocr_pages=None).ocr
    assert ADAPTERS["uk-isc"](include_press=None).include_press
    assert ADAPTERS["uk-isc"](max_ocr_pages=None).max_ocr_pages == 200
    assert ADAPTERS["uk-ipco"](ocr=None, include_news=None).include_news
    assert ADAPTERS["scot-spice"](ocr=None, page_size=None).page_size == 50
    assert ADAPTERS["uk-commons-library"](include_pdf=None, ocr=None).include_pdf
    # and an explicit "false" still turns it off
    assert ADAPTERS["uk-isc"](ocr="false").ocr is False


# --- Commons / Lords Library -------------------------------------------------

COMMONS = uk_parl_library.HOUSES["commons"]
LORDS = uk_parl_library.HOUSES["lords"]


def test_the_feed_carries_the_whole_briefing_not_a_teaser():
    rows = uk_parl_library.parse_feed(
        (DATA / "commons_library_feed.xml").read_bytes(), host=COMMONS["host"])
    assert rows, "the feed fixture parsed to nothing"
    text, segments = uk_parl_library.html_body(rows[0]["content_html"])
    # This is the property the whole adapter rests on: the RSS body is the document.
    assert len(text) > uk_parl_library.FULL_TEXT_FLOOR
    assert segments, "the briefing's own headings should survive as citable segments"
    assert all(seg.char_end <= len(text) for seg in segments)
    assert all(text[seg.char_start:seg.char_start + len(seg.label)] == seg.label
               for seg in segments)


def test_items_carry_a_canonical_url_a_date_and_a_slug():
    rows = uk_parl_library.parse_feed(
        (DATA / "commons_library_feed.xml").read_bytes(), host=COMMONS["host"])
    for row in rows:
        assert row["url"].startswith(COMMONS["host"])
        assert row["slug"] and row["slug"] == row["slug"].strip("/")
        assert isinstance(row["date"], date)


def test_the_staging_host_is_the_same_document():
    """Some guids point at local.parliament.uk, the Libraries' staging host. Left alone
    that stores the same briefing twice under two hosts."""
    assert uk_parl_library.canonical_url(
        "https://local.parliament.uk/research-briefings/sn02811/", COMMONS["host"]
    ) == "https://commonslibrary.parliament.uk/research-briefings/sn02811/"


def test_a_pdf_only_briefing_is_recognised_by_its_thin_body_not_its_age():
    rows = uk_parl_library.parse_feed(
        (DATA / "commons_library_feed_old.xml").read_bytes(), host=COMMONS["host"])
    assert rows
    for row in rows:
        text, _ = uk_parl_library.html_body(row["content_html"])
        assert len(text) < uk_parl_library.FULL_TEXT_FLOOR, (
            "a 1993 research paper's feed body is an abstract; if this passes the floor "
            "the adapter will store the abstract and never fetch the paper")


def test_the_pdf_url_prefers_the_link_the_page_publishes():
    page = ('<a href="https://researchbriefings.files.parliament.uk/documents/'
            'RP94-22/RP94-22-renamed.pdf">Download</a>')
    assert uk_parl_library.pdf_url_for(page, "rp94-22").endswith("RP94-22-renamed.pdf")
    # …and derives one from the id when the page publishes none
    assert uk_parl_library.pdf_url_for("", "rp94-22") == (
        "https://researchbriefings.files.parliament.uk/documents/RP94-22/RP94-22.pdf")
    for number in ("cbp-10974", "sn02811", "lln-2019-0042"):
        assert uk_parl_library.pdf_url_for("", number)


def test_a_prose_permalink_names_no_file_and_is_not_guessed_at():
    """The Lords have published under a bare slug since ~2021. Deriving a filename from
    one asks the browser for a URL that cannot exist, and waits to be told so."""
    assert uk_parl_library.pdf_url_for(
        "", "reducing-gambling-harm-among-young-people") is None


def test_a_targeted_fetch_tries_both_url_shapes(monkeypatch):
    """A modern Lords briefing has no /research-briefings/ prefix; assuming one 404s and
    the briefing reads as unavailable."""
    adapter = ADAPTERS["uk-lords-library"](slugs="reducing-gambling-harm")
    tried: list[str] = []
    page = ('<h1>Reducing gambling harm</h1><div class="component--text">'
            '<div class="reading-width"><p>' + "Body text. " * 80 + "</p></div></div>")

    def fake(url, referer=None):
        tried.append(url)
        return page.encode("utf-8") if not url.endswith("/research-briefings/"
                                                        "reducing-gambling-harm/") else None

    monkeypatch.setattr(adapter, "_bytes", fake)
    stub = next(iter(adapter.discover(None)))
    record = adapter.fetch(stub)
    assert record is not None and record.title == "Reducing gambling harm"
    assert record.landing_url == "https://lordslibrary.parliament.uk/reducing-gambling-harm/"
    assert tried[0].endswith("/research-briefings/reducing-gambling-harm/")


BRIEFING_PAGE = (DATA / "commons_briefing_page.html").read_text(encoding="utf-8")


def test_a_briefing_fetched_by_id_gets_its_real_title_and_date_from_the_page():
    """There is no feed row for a targeted fetch, so without the page the document is
    stored titled "RP94-22" and undated — unfindable under the name anyone would use."""
    meta = uk_parl_library.parse_page(BRIEFING_PAGE)
    assert meta["title"] == "The Tobacco Advertising Bill 1993/94"
    assert meta["date"] == date(1994, 2, 7)
    assert meta["authors"] == ["Antony Seely", "Grahame Danby"]
    assert meta["publication_type"] == "Research Briefing"


def test_the_page_body_is_read_when_there_is_neither_a_feed_row_nor_a_pdf():
    """A modern HTML-only briefing reached by id has no PDF to fall back to; before this
    the adapter looked for one, did not find it, and returned nothing at all."""
    text, _segments = uk_parl_library.page_body(BRIEFING_PAGE)
    assert text and "Tobacco Advertising Bill" in text


def test_page_parsing_is_tolerant_of_a_page_that_is_not_one():
    assert uk_parl_library.page_body("") == ("", [])
    assert uk_parl_library.page_body("<html><body>nothing here</body></html>") == ("", [])
    assert uk_parl_library.parse_page("")["title"] is None


def test_fetch_by_id_falls_back_to_the_pdf_and_dates_it(monkeypatch):
    adapter = ADAPTERS["uk-commons-library"](slugs="rp94-22")

    def fake(url, referer=None):
        return b"%PDF-1.2 scan" if url.endswith(".pdf") else BRIEFING_PAGE.encode("utf-8")

    monkeypatch.setattr(adapter, "_bytes", fake)
    monkeypatch.setattr(
        "raglex.extraction.ocr.text_or_ocr",
        lambda data, **kw: ("Research Paper 94/22 body. " * 40, False, [(1, 0, 90)],
                            "tesseract"))
    stub = next(iter(adapter.discover(None)))
    record = adapter.fetch(stub)
    assert record is not None
    assert record.title == "The Tobacco Advertising Bill 1993/94"
    assert record.decision_date == date(1994, 2, 7)
    assert record.raw_ext == "pdf" and record.extra["format"] == "tesseract"
    assert record.extra["authors"] == ["Antony Seely", "Grahame Danby"]
    assert record.extra["pdf_url"].endswith("RP94-22.pdf")


def test_the_lords_feed_parses_the_same_way_and_carries_its_author():
    rows = uk_parl_library.parse_feed(
        (DATA / "lords_library_feed.xml").read_bytes(), host=LORDS["host"])
    assert rows
    assert any(row["author"] for row in rows)
    # the Lords guid is a bare ?p=28885, so identity must come off the link
    assert all(row["slug"] and not row["slug"].startswith("?") for row in rows)


def test_a_feed_page_past_the_end_stops_discovery():
    """One page past the archive the feed is well-formed with no <item>. That is the
    only end-of-archive signal either Library gives."""
    empty = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
             b"<title>Page not found</title></channel></rss>")
    assert uk_parl_library.parse_feed(empty, host=COMMONS["host"]) == []


def test_bytes_that_are_not_a_feed_are_an_error_not_an_empty_archive():
    """A browser timeout, a challenge page, a truncated body — none of them mean the
    archive ended. Returning [] for these is how a 1,200-page walk stops at page 700
    wearing a success."""
    for bad in (b"", b"<html><body>not xml", b"<html><body>Just a moment...</body></html>"):
        with pytest.raises(uk_parl_library.FeedUnreadable):
            uk_parl_library.parse_feed(bad, host=COMMONS["host"])


def test_a_feed_page_that_misses_is_retried_before_being_believed(monkeypatch):
    adapter = ADAPTERS["uk-commons-library"]()
    calls = {"n": 0}
    good = (DATA / "commons_library_feed.xml").read_bytes()

    def flaky(url, referer=None):
        calls["n"] += 1
        return None if calls["n"] == 1 else good      # one hiccup, then fine

    monkeypatch.setattr(adapter, "_bytes", flaky)
    rows = adapter._feed_page(1, first=True)
    assert rows, "a single miss must not read as the end of the archive"
    assert calls["n"] == 2


def test_a_first_page_that_never_answers_fails_the_run(monkeypatch):
    """A run that read nothing must fail, not record "discovered 0 — done" over twelve
    thousand briefings it never looked at."""
    from raglex.core.errors import FetchError

    adapter = ADAPTERS["uk-commons-library"]()
    monkeypatch.setattr(adapter, "_bytes", lambda url, referer=None: None)
    with pytest.raises(FetchError):
        list(adapter.discover(None))


def test_an_xml_illegal_control_byte_does_not_cost_the_page():
    """Lords page 109 carries a raw 0x02 inside an image's alt text ("EN-1 to EN\\x025",
    a mangled en-dash). XML 1.0 forbids C0 controls, so that ONE byte made all 149 KB
    unparseable — and it is in the stored post, served identically every time, so no
    amount of retrying was ever going to help."""
    good = (DATA / "commons_library_feed.xml").read_bytes()
    poisoned = good.replace(b"<title>", b"<title>EN-1 to EN\x025 ", 1)
    with pytest.raises(ET.ParseError):
        ET.fromstring(poisoned)          # genuinely invalid, not a test artefact
    rows = uk_parl_library.parse_feed(poisoned, host=COMMONS["host"])
    assert rows, "the page must survive one illegal byte"


def test_the_feed_publishes_its_own_length_in_the_channel_title():
    """"All research - Page 108 of 281" — the walk's end, from the feed itself. Without
    it there is no way to tell a broken page from the bottom of the archive."""
    titled = (DATA / "commons_library_feed.xml").read_bytes().replace(
        b"<title>All briefings - House of Commons Library</title>",
        b"<title>All briefings - Page 108 of 281 - House of Commons Library</title>", 1)
    assert uk_parl_library.feed_position(titled) == (108, 281)
    assert uk_parl_library.feed_position(b"<rss><channel><title>x</title></channel></rss>") is None


def test_one_unreadable_page_does_not_abandon_the_archive_below_it(monkeypatch):
    """The bug this cost us for real: Commons stopped at page 750 of 1200 and Lords at
    109 of 281, each reporting `done` with 0 errors, leaving thousands of briefings
    unread and nothing in the result to say so."""
    adapter = ADAPTERS["uk-commons-library"]()
    good = (DATA / "commons_library_feed.xml").read_bytes().replace(
        b"<title>All briefings - House of Commons Library</title>",
        b"<title>All briefings - Page 1 of 4 - House of Commons Library</title>", 1)
    asked: list[int] = []

    def fake(url, referer=None):
        page = int(url.split("paged=")[1]) if "paged=" in url else 1
        asked.append(page)
        if page == 2:
            return b"<rss><channel>this is not xml at all"      # permanently broken
        if page == 3:
            return good.replace(b"Page 1 of 4", b"Page 3 of 4")  # fine
        if page == 4:
            return good.replace(b"Page 1 of 4", b"Page 4 of 4")
        return good

    monkeypatch.setattr(adapter, "_bytes", fake)
    list(adapter.discover(None))
    assert 3 in asked and 4 in asked, "pages below the broken one must still be walked"
    assert max(asked) == 4, "and it must stop at the feed's own declared last page"


def test_an_empty_page_before_the_declared_end_is_not_the_end(monkeypatch):
    """A page that failed to render comes back as a well-formed feed with no items.
    Believing it is the same bug in a quieter form."""
    adapter = ADAPTERS["uk-commons-library"]()
    good = (DATA / "commons_library_feed.xml").read_bytes().replace(
        b"<title>All briefings - House of Commons Library</title>",
        b"<title>All briefings - Page 1 of 3 - House of Commons Library</title>", 1)
    asked: list[int] = []

    def fake(url, referer=None):
        page = int(url.split("paged=")[1]) if "paged=" in url else 1
        asked.append(page)
        if page == 2:
            return (b'<rss version="2.0"><channel><title>All briefings - Page 2 of 3 - '
                    b"House of Commons Library</title></channel></rss>")
        return good.replace(b"Page 1 of 3", f"Page {page} of 3".encode())

    monkeypatch.setattr(adapter, "_bytes", fake)
    list(adapter.discover(None))
    assert 3 in asked, "an empty page short of the declared end must not stop the walk"


def test_a_run_of_dead_pages_stops_rather_than_walking_to_page_two_thousand(monkeypatch):
    """Skipping is for isolated damage. If the feed simply stops answering, asking every
    remaining page twice is not diligence — it is 4,000 browser fetches."""
    adapter = ADAPTERS["uk-commons-library"]()
    good = (DATA / "commons_library_feed.xml").read_bytes()
    seen: list[str] = []

    def fake(url, referer=None):
        seen.append(url)
        return good if "paged" not in url else None

    monkeypatch.setattr(adapter, "_bytes", fake)
    stubs = list(adapter.discover(None))
    assert stubs, "page 1 succeeded and its briefings must survive"
    pages = {int(u.split("paged=")[1]) for u in seen if "paged=" in u}
    assert len(pages) == adapter.MAX_CONSECUTIVE_MISSES
    assert max(pages) == 1 + adapter.MAX_CONSECUTIVE_MISSES
    # each was genuinely retried before being written off
    assert sum(1 for u in seen if "paged=2" in u) == adapter.FEED_ATTEMPTS


def test_discovery_stops_at_the_end_of_the_archive(monkeypatch):
    adapter = ADAPTERS["uk-commons-library"]()
    pages: list[str] = []

    def fake(url, referer=None):
        pages.append(url)
        if len(pages) == 1:
            return (DATA / "commons_library_feed.xml").read_bytes()
        return b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'

    monkeypatch.setattr(adapter, "_bytes", fake)
    stubs = list(adapter.discover(None))
    assert stubs and len(pages) == 2, "discovery must stop on the first empty feed page"
    assert all(s.stable_id.startswith("uk/commons-library/") for s in stubs)
    assert all(s.hints["resume_offset"] == 1 for s in stubs)


def test_an_incremental_run_skips_items_older_than_the_cursor(monkeypatch):
    adapter = ADAPTERS["uk-commons-library"]()
    calls = {"n": 0}

    def fake(url, referer=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (DATA / "commons_library_feed.xml").read_bytes()
        return b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'

    monkeypatch.setattr(adapter, "_bytes", fake)
    assert list(adapter.discover("2099-01-01")) == []


def test_fetch_builds_a_record_from_the_feed_alone(monkeypatch):
    """The point of the source: no per-document page fetch on the common path."""
    adapter = ADAPTERS["uk-commons-library"]()
    fetched: list[str] = []

    def fake(url, referer=None):
        fetched.append(url)
        if "feed" in url:
            return (DATA / "commons_library_feed.xml").read_bytes()
        raise AssertionError(f"fetch should not have gone to the page: {url}")

    monkeypatch.setattr(adapter, "_bytes", fake)
    stub = next(iter(adapter.discover(None)))
    before = len(fetched)
    record = adapter.fetch(stub)
    assert len(fetched) == before, "the body was already in hand from discovery"
    assert record is not None
    assert record.source == "uk-commons-library"
    assert record.extra["issuer"] == "House of Commons Library"
    assert record.extra["format"] == "rss"
    assert record.text and len(record.text) > uk_parl_library.FULL_TEXT_FLOOR
    assert record.decision_date == stub.hint_date


def test_a_cache_miss_re_reads_the_one_feed_page_it_came_from(monkeypatch):
    """Discovery caches bodies bounded; a stub fetched after eviction must still work."""
    adapter = ADAPTERS["uk-commons-library"]()
    monkeypatch.setattr(
        adapter, "_bytes",
        lambda url, referer=None: (DATA / "commons_library_feed.xml").read_bytes())
    stub = next(iter(adapter.discover(None)))
    adapter._items.clear()
    assert adapter.fetch(stub) is not None


def test_house_must_be_one_we_know():
    with pytest.raises(ValueError):
        uk_parl_library.ParliamentLibraryAdapter(house="senate")


# --- SPICe (Scottish Parliament) ---------------------------------------------

LISTING = (DATA / "scot_spice_listing.html").read_text(encoding="utf-8")


def test_the_widest_date_preset_is_chosen_from_the_form_not_hard_coded():
    """The presets' guids are bound server-side to their own ranges and the archive-wide
    one's label ends at TODAY, so its value changes daily. Reading the form is the only
    thing that keeps working tomorrow."""
    chosen = scot_spice.widest_date_option(LISTING)
    assert chosen and chosen.count("|") == 2
    start, end = chosen.split("|")[1], chosen.split("|")[2]
    assert scot_spice._label_date(start).year == 1999
    # …and it is genuinely the widest, not merely the first with a range
    spans = []
    import re as _re
    block = _re.search(r'(?s)<select[^>]*name="dateSelect"[^>]*>(.*?)</select>', LISTING)
    for value in _re.findall(r'value="([^"]*)"', block.group(1)):
        import html as _html
        parts = _html.unescape(value).split("|")
        if len(parts) == 3 and scot_spice._label_date(parts[1]):
            spans.append((scot_spice._label_date(parts[2])
                          - scot_spice._label_date(parts[1])).days)
    assert (scot_spice._label_date(end) - scot_spice._label_date(start)).days == max(spans)


def test_options_without_a_range_are_not_mistaken_for_one():
    """"All" and "custom" are values that filter nothing; picking either returns the
    default few-week window or zero results."""
    assert scot_spice.widest_date_option(
        '<select name="dateSelect"><option value="All"></option>'
        '<option value="custom"></option></select>') is None


def test_listing_rows_carry_title_number_subject_and_date():
    rows = scot_spice.parse_results(LISTING)
    assert rows
    first = rows[0]
    assert first["slug"].startswith("sb-")
    assert first["url"].startswith(scot_spice.HOST)
    assert first["briefing_number"] and first["briefing_number"].startswith("SB ")
    assert isinstance(first["date"], date)
    assert first["subject"]


def test_the_result_count_and_page_count_are_read_as_an_authoritative_total():
    assert scot_spice.result_total(LISTING) == 750
    assert scot_spice.page_total(LISTING) == 15


def test_discovery_sends_the_date_preset_and_paginates(monkeypatch):
    adapter = ADAPTERS["scot-spice"]()
    seen: list[dict] = []

    def fake(params=None):
        seen.append(dict(params or {}))
        return LISTING if len(seen) <= 1 else ""

    monkeypatch.setattr(adapter, "_listing", fake)
    adapter.date_select = scot_spice.widest_date_option(LISTING)
    stubs = list(adapter.discover(None))
    assert stubs
    assert seen[0]["dateSelect"] == adapter.date_select, (
        "without dateSelect the search answers with its default few-week window")
    assert seen[0]["pgsize"] == 50
    assert all(s.hints["feed_total"] == 750 for s in stubs)
    assert all(s.raw_url.endswith("/pdf") for s in stubs), (
        "the PDF is the complete briefing; the HTML view is paginated")
    assert stubs[0].stable_id.startswith("scot/spice/sb-")


def test_discovery_stops_at_the_last_page_rather_than_asking_for_one_more(monkeypatch):
    adapter = ADAPTERS["scot-spice"]()
    single = LISTING.replace("Page: 1 of 15", "Page: 1 of 1")
    calls = {"n": 0}

    def fake(params=None):
        calls["n"] += 1
        return single

    monkeypatch.setattr(adapter, "_listing", fake)
    adapter.date_select = ""
    list(adapter.discover(None))
    assert calls["n"] == 1


def test_fetch_reads_the_pdf_and_the_pages_metadata(monkeypatch):
    adapter = ADAPTERS["scot-spice"]()
    page = (DATA / "scot_spice_briefing.html").read_bytes()
    monkeypatch.setattr(
        adapter, "_get",
        lambda url, params=None: page if not url.endswith("/pdf") else b"%PDF-1.7 junk")
    monkeypatch.setattr(
        "raglex.extraction.ocr.text_or_ocr",
        lambda data, **kw: ("SPICe briefing body. " * 40, False, [(1, 0, 100)], "pdf"))
    stub = Stub(stable_id="scot/spice/sb-2650",
                landing_url=f"{scot_spice.LISTING}/2026/8/5/sb-2650",
                raw_url=f"{scot_spice.LISTING}/2026/8/5/sb-2650/pdf",
                hint_date=date(2026, 8, 5),
                hints={"subject": "Parliament and Government",
                       "briefing_number": "SB 26-50"})
    record = adapter.fetch(stub)
    assert record is not None
    assert record.raw_ext == "pdf"
    assert record.extra["jurisdiction"] == "gb-sct"
    assert record.extra["briefing_number"] == "SB 26-50"
    assert record.extra["authors"], "the author is only on the briefing's own page"
    assert record.extra["summary"]
    assert record.decision_date == date(2026, 8, 5)


# --- IPCO ---------------------------------------------------------------------

def test_the_sitemap_yields_urls_with_the_lastmod_that_records_a_revision():
    rows = uk_ipco.parse_sitemap((DATA / "ipco_sitemap.xml").read_bytes())
    assert rows
    assert all(row["url"].startswith("https://") for row in rows)
    assert any(isinstance(row["lastmod"], date) for row in rows), (
        "lastmod is the only thing on the site that says a report was reissued")


def test_sections_are_read_off_the_publication_path():
    assert uk_ipco.section_of(
        "https://www.ipco.org.uk/publication/annual-report/annual-report-2024/"
    ) == "annual-report"
    assert uk_ipco.section_of("https://www.ipco.org.uk/news/some-post/") == "news"


def test_attachments_keep_the_link_text_that_names_them():
    html = (DATA / "ipco_publication.html").read_text(encoding="utf-8")
    attachments = uk_ipco.parse_attachments(html)
    assert attachments, "the publication's PDF is the document"
    # the same href appears twice — thumbnail and Download button — and merges to one
    assert len({a["url"] for a in attachments}) == len(attachments)
    assert any(a["label"] for a in attachments)
    assert all(a["label"] != "Download" for a in attachments)


def test_a_pdf_hosted_somewhere_else_is_not_an_ipco_publication():
    assert uk_ipco.parse_attachments(
        '<a href="https://example.org/other.pdf">Someone else’s report</a>') == []


def test_the_publication_date_is_read_from_the_page():
    html = (DATA / "ipco_publication.html").read_text(encoding="utf-8")
    assert uk_ipco.parse_page_date(html) == date(2025, 12, 16)


def test_incremental_filters_the_one_sitemap_on_lastmod(monkeypatch):
    adapter = ADAPTERS["uk-ipco"]()
    monkeypatch.setattr(adapter, "_get",
                        lambda url: (DATA / "ipco_sitemap.xml").read_bytes())
    everything = list(adapter.discover(None))
    assert everything
    assert all(s.hints["feed_total"] == len(everything) for s in everything)
    assert list(adapter.discover("2099-01-01")) == []


def test_news_can_be_excluded(monkeypatch):
    adapter = ADAPTERS["uk-ipco"](include_news=False)
    monkeypatch.setattr(adapter, "_get",
                        lambda url: (DATA / "ipco_sitemap.xml").read_bytes())
    assert all("/news/" not in (s.landing_url or "") for s in adapter.discover(None))


def test_ipco_declares_no_default_instrument(monkeypatch):
    """It is named for the IPA 2016, but the inherited IOCCO and OSC reports oversee
    RIPA 2000 and the Police Act 1997 — a bare "section 22" there is not IPA."""
    adapter = ADAPTERS["uk-ipco"]()
    html = (DATA / "ipco_publication.html").read_bytes()
    monkeypatch.setattr(adapter, "_get",
                        lambda url: html if not url.endswith(".pdf") else b"%PDF-1.7 x")
    monkeypatch.setattr(
        "raglex.extraction.ocr.text_or_ocr",
        lambda data, **kw: ("IPCO annual report body. " * 40, False, [], "pdf"))
    record = adapter.fetch(Stub(
        stable_id="uk/ipco/annual-report-2024",
        landing_url="https://www.ipco.org.uk/publication/annual-report/annual-report-2024/",
        hints={"section": "annual-report"}))
    assert record is not None
    assert "citation_default_instrument" not in record.extra
    assert record.extra["attachments"]


# --- ISC ----------------------------------------------------------------------

ISC_HTML = (DATA / "isc_reports.html").read_text(encoding="utf-8")


def test_the_collapsed_accordions_are_parsed_not_dropped():
    """Rendered in a browser the collapsed per-Parliament sections vanish and the page
    yields four PDFs instead of 215 — three decades of reports, silently absent."""
    rows = uk_isc.parse_reports(ISC_HTML)
    assert any(row["period"] == "2019 - 2024" for row in rows), (
        "the fixture's collapsed accordion contains a publication and it must be found")
    assert any(row["section"] == "Current Parliament" for row in rows)


def test_the_last_publication_block_on_the_page_is_not_dropped():
    """The blocks are identical, deeply nested divs. Matching a closing boundary works
    for every block that has another one after it and silently loses the final one —
    106 of 107 on the live page."""
    blocks = ISC_HTML.count('class="publication-block__post"')
    rows = uk_isc.parse_reports(ISC_HTML)
    assert len({row["url"] for row in rows}) >= blocks


def test_an_accordion_does_not_carry_past_the_heading_that_ends_its_group():
    """The Transcripts accordions are single years ("2015"), not Parliaments. Classified
    by shape they read as section headings, and the 1992-97 Parliament stays in force
    beneath them — filing 2014 transcripts under a Parliament that ended in 1997."""
    html = (
        '<h2 id="previous-parliaments">Previous Parliaments</h2>'
        '<div class="accordion__content" aria-labelledby="1992 - 1997">'
        '<div class="publication-block__post"><div class="icon icon--pdf-file">'
        '<a href="https://x/old.pdf"><strong>Old report</strong></a></div>'
        '<p class="publication-block__date">Published: March 1996</p></div></div>'
        '<h2 id="transcripts">Transcripts and Public Evidence</h2>'
        '<div class="accordion__content" aria-labelledby="2014">'
        '<div class="publication-block__post"><div class="icon icon--pdf-file">'
        '<a href="https://x/new.pdf"><strong>2014 transcript</strong></a></div>'
        '<p class="publication-block__date">Published: October 2014</p></div></div>'
    )
    rows = {row["title"]: row for row in uk_isc.parse_reports(html)}
    assert rows["Old report"]["period"] == "1992 - 1997"
    assert rows["Old report"]["section"] == "Previous Parliaments"
    assert rows["2014 transcript"]["period"] == "2014"
    assert rows["2014 transcript"]["section"] == "Transcripts and Public Evidence"


def test_a_report_and_its_press_notice_are_separate_documents():
    rows = uk_isc.parse_reports(ISC_HTML)
    kinds = {row["kind"] for row in rows}
    assert {"report", "press-notice"} <= kinds
    # each is named by its own link text, which is what distinguishes them
    titles = [row["title"] for row in rows]
    assert len(titles) == len(set(titles))
    assert all(title and title.lower() != "download" for title in titles)


def test_a_published_month_becomes_the_first_of_that_month():
    assert uk_isc.parse_published("December 2023") == date(2023, 12, 1)
    assert uk_isc.parse_published("March 1996") == date(1996, 3, 1)
    assert uk_isc.parse_published("not a date") is None


def test_press_notices_can_be_excluded(monkeypatch):
    adapter = ADAPTERS["uk-isc"](include_press=False)
    monkeypatch.setattr(adapter, "_get", lambda url: ISC_HTML.encode("utf-8"))
    stubs = list(adapter.discover(None))
    assert stubs
    assert all(s.hints["kind"] != "press-notice" for s in stubs)


def test_discovery_reports_a_real_total_and_stable_ids(monkeypatch):
    adapter = ADAPTERS["uk-isc"]()
    monkeypatch.setattr(adapter, "_get", lambda url: ISC_HTML.encode("utf-8"))
    stubs = list(adapter.discover(None))
    assert stubs
    assert all(s.hints["feed_total"] == len(stubs) for s in stubs)
    assert len({s.stable_id for s in stubs}) == len(stubs)
    assert all(s.raw_url.endswith(".pdf") for s in stubs)


def test_a_scan_with_no_text_layer_is_ocrd_not_stored_empty(monkeypatch):
    """1995_ISC_AR.pdf extracts zero characters born-digital and OCRs to real prose."""
    adapter = ADAPTERS["uk-isc"]()
    monkeypatch.setattr(adapter, "_get", lambda url: b"%PDF-1.2 scanned image")
    monkeypatch.setattr(
        "raglex.extraction.ocr.text_or_ocr",
        lambda data, **kw: ("Intelligence and Security Committee Annual Report 1995. "
                            * 20, False, [], "tesseract"))
    record = adapter.fetch(Stub(
        stable_id="uk/isc/1995-isc-ar",
        raw_url="https://isc.independent.gov.uk/wp-content/uploads/2021/01/1995_ISC_AR.pdf",
        title="ISC Annual Report 1995", hint_date=date(1996, 3, 1),
        hints={"kind": "report", "section": "Previous Parliaments",
               "period": "1992 - 1997", "date": "1996-03-01"}))
    assert record is not None
    assert record.extra["format"] == "tesseract"
    assert record.extra["needs_ocr"] is None, "a successful OCR is not a review item"
    assert record.decision_date == date(1996, 3, 1)
    assert record.raw_ext == "pdf"


def test_a_scan_that_could_not_be_read_is_flagged_rather_than_dropped(monkeypatch):
    adapter = ADAPTERS["uk-isc"]()
    monkeypatch.setattr(adapter, "_get", lambda url: b"%PDF-1.2 scanned image")
    monkeypatch.setattr("raglex.extraction.ocr.text_or_ocr",
                        lambda data, **kw: ("", True, [], "pdf"))
    record = adapter.fetch(Stub(stable_id="uk/isc/x", raw_url="https://x/y.pdf",
                                title="Something", hints={}))
    assert record is not None and record.extra["needs_ocr"] is True


def test_something_that_is_not_a_pdf_is_dropped(monkeypatch):
    adapter = ADAPTERS["uk-isc"]()
    monkeypatch.setattr(adapter, "_get", lambda url: b"<html>404</html>")
    assert adapter.fetch(Stub(stable_id="uk/isc/x", raw_url="https://x/y.pdf")) is None


# --- the shared OCR tier ------------------------------------------------------

def test_ocr_moved_out_of_the_dpa_adapter_but_its_importers_still_work():
    from raglex.adapters.edpb import looks_unocrd as legacy_flag, ocr_pdf as legacy
    from raglex.extraction import looks_unocrd, ocr_pdf

    assert legacy is ocr_pdf and legacy_flag is looks_unocrd


def test_a_thin_extraction_across_many_pages_reads_as_a_scan():
    from raglex.extraction import looks_unocrd

    assert looks_unocrd("Cm 3198", 60) is True
    assert looks_unocrd("x" * 5000, 60) is False
    assert looks_unocrd("", 0) is False, "no pages is not a scan, it is not a PDF"


def test_text_or_ocr_prefers_a_real_text_layer_and_never_ocrs_it(monkeypatch):
    from raglex.extraction import ocr as ocr_mod

    monkeypatch.setattr(ocr_mod, "ocr_pdf",
                        lambda *a, **k: pytest.fail("should not have OCR'd"))
    monkeypatch.setattr(
        "raglex.extraction.extract_bytes",
        lambda data, **kw: type("E", (), {
            "text": "real text " * 60, "needs_ocr": False,
            "page_spans": [(1, 0, 100)], "engine": "pdfminer"})())
    text, needs_ocr, spans, engine = ocr_mod.text_or_ocr(b"%PDF-")
    assert needs_ocr is False and engine == "pdfminer" and spans


def test_text_or_ocr_escalates_an_empty_parse_and_drops_its_stale_page_spans(monkeypatch):
    from raglex.extraction import ocr as ocr_mod

    monkeypatch.setattr(
        "raglex.extraction.extract_bytes",
        lambda data, **kw: type("E", (), {
            "text": "", "needs_ocr": True,
            "page_spans": [(1, 0, 0), (2, 0, 0)], "engine": "pdfminer"})())
    monkeypatch.setattr(ocr_mod, "ocr_pdf", lambda *a, **k: "recovered text " * 40)
    text, needs_ocr, spans, engine = ocr_mod.text_or_ocr(b"%PDF-")
    assert text.startswith("recovered text") and needs_ocr is False
    assert engine == "tesseract"
    assert spans == [], (
        "the page spans described the parse we just replaced; keeping them would point "
        "citations at offsets that no longer exist")


def test_when_ocr_is_unavailable_the_document_is_flagged_not_silently_empty(monkeypatch):
    from raglex.extraction import ocr as ocr_mod

    monkeypatch.setattr(
        "raglex.extraction.extract_bytes",
        lambda data, **kw: type("E", (), {
            "text": "", "needs_ocr": True, "page_spans": [], "engine": "pdfminer"})())
    monkeypatch.setattr(ocr_mod, "ocr_pdf", lambda *a, **k: None)
    text, needs_ocr, _spans, _engine = ocr_mod.text_or_ocr(b"%PDF-")
    assert text == "" and needs_ocr is True
