"""Judicial guidance on judiciary.uk (``uk-judiciary``).

The site's download buttons read "Download <hidden filename> file", so the only thing
distinguishing one document from another is the filename — and the monthly watch must not
re-download a 1.6MB bench book because a nav item changed, which is what the landing-page
fingerprint is for.
"""

from __future__ import annotations

from raglex.adapters.uk_judiciary import (
    JudiciaryGuidanceAdapter,
    citation_reference,
    page_fingerprint,
    parse_documents,
    parse_item_links,
)

PAGE = """
<main>
  <h1 class="single__title">Crown Court Compendium</h1>
  <a href="/subjects/criminal/">Criminal</a>
  <a class="govuk-button related-content__link"
     href="https://www.judiciary.uk/wp-content/uploads/2022/06/Crown-Court-Compendium-Part-I-Oct-25-Mar-26-update.pdf">
     Download <span class="govuk-visually-hidden">Crown-Court-Compendium-Part-I-Oct-25-Mar-26-update.pdf</span> file
  </a>
  <a class="govuk-button related-content__link"
     href="https://www.judiciary.uk/wp-content/uploads/2022/06/Crown-Court-Compendium-Part-II-Oct-25-Mar-26-update.pdf">
     Download <span class="govuk-visually-hidden">Crown-Court-Compendium-Part-II-Oct-25-Mar-26-update.pdf</span> file
  </a>
</main>
"""

CORONER_LIST = """
<main>
  <h1>Chief Coroner’s Guidance, Advice and Law Sheets</h1>
  <a href="https://www.judiciary.uk/guidance-and-resources/chief-coroners-guidance-no-15-dealing-with-the-possibility-of-apparent-bias/">Guidance No 15: Apparent Bias</a>
  <a href="https://www.judiciary.uk/guidance-and-resources/chief-coroners-law-sheet-no-1/">Law Sheet No.1: Unlawful killing</a>
  <a href="https://www.judiciary.uk/wp-content/uploads/2025/09/Guidance-No.-46-Online.pdf">Guidance No 46: Obtaining information</a>
  <a href="https://www.judiciary.uk/guidance-and-resources/">Guidance</a>
</main>
"""


def test_the_filename_is_the_title_when_the_button_says_nothing():
    docs = parse_documents(PAGE, page_url="https://www.judiciary.uk/x/")
    assert [d["title"] for d in docs] == [
        "Crown Court Compendium Part I Oct 25 Mar 26 update",
        "Crown Court Compendium Part II Oct 25 Mar 26 update"]
    assert all(d["ext"] == "pdf" for d in docs)
    # the subject link is not a document
    assert all("subjects" not in d["url"] for d in docs)


def test_per_item_pages_are_found_but_the_section_index_is_not():
    items = parse_item_links(CORONER_LIST, page_url="https://www.judiciary.uk/y/",
                             item_href="/guidance-and-resources/")
    assert [i["title"] for i in items] == [
        "Guidance No 15: Apparent Bias", "Law Sheet No.1: Unlawful killing"]
    # a direct PDF on the listing is picked up by the document scan instead
    assert [d["title"] for d in parse_documents(CORONER_LIST, page_url="https://x/")] == [
        "Guidance No 46: Obtaining information"]


def test_the_citable_reference_is_lifted_from_the_title():
    assert citation_reference("Guidance No 16A: Deprivation of Liberty Safeguards") == "Guidance No 16A"
    assert citation_reference("Chief Coroner's Guidance No. 7 — Service Deaths") == "Guidance No 7"
    assert citation_reference("Law Sheet No.1: Unlawful killing") == "Law Sheet No 1"
    assert citation_reference("Treasure: A Practical Guide for Coroners") is None


def test_the_fingerprint_tracks_the_documents_not_the_markup():
    docs = parse_documents(PAGE, page_url="https://www.judiciary.uk/x/")
    before = page_fingerprint(docs, [])
    # an unrelated site edit (a new nav link, a changed image) leaves it alone…
    noisy = PAGE.replace("<h1", '<a href="/news/">News</a><img src="new.png"><h1')
    assert page_fingerprint(parse_documents(noisy, page_url="https://www.judiciary.uk/x/"), []) == before
    # …a reissued document changes it
    reissued = PAGE.replace("Oct-25-Mar-26", "Apr-26-Sep-26")
    assert page_fingerprint(parse_documents(reissued, page_url="https://www.judiciary.uk/x/"), []) != before


class _Resp:
    def __init__(self, content: bytes) -> None:
        self.content, self.text = content, content.decode()


class _Client:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages, self.seen = pages, []

    def get(self, url: str, **_kw):
        self.seen.append(url)
        for key, body in self.pages.items():
            if key in url:
                return _Resp(body.encode())
        return _Resp(b"<main></main>")


def _adapter(pages: dict[str, str]) -> JudiciaryGuidanceAdapter:
    a = JudiciaryGuidanceAdapter(client=_Client(pages), collection="crown-court-compendium")
    return a


def test_an_unchanged_page_downloads_nothing_at_all():
    """The point of the monthly check: these are 1.6MB bench books revised twice a year."""
    a = _adapter({"crown-court-compendium": PAGE})
    stubs = list(a.discover(None))
    assert len(stubs) == 2
    cursor = stubs[0].hints["watermark"]
    assert cursor.startswith("crown-court-compendium:")

    a2 = _adapter({"crown-court-compendium": PAGE})
    assert list(a2.discover(cursor)) == []          # nothing yielded → nothing fetched
    assert len(a2._client.seen) == 1                # only the landing page was read

    # a reissue does yield, under the same ids (so it supersedes rather than duplicates)
    a3 = _adapter({"crown-court-compendium": PAGE.replace("Oct-25-Mar-26", "Apr-26-Sep-26")})
    fresh = list(a3.discover(cursor))
    assert len(fresh) == 2


def test_documents_sharing_a_page_get_distinct_ids():
    a = _adapter({"crown-court-compendium": PAGE})
    ids = [s.stable_id for s in a.discover(None)]
    assert len(set(ids)) == 2
    assert ids[0].startswith("uk/judiciary/crown-court-compendium/")
