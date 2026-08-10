"""Ofgem's own publications register — the listing API's query grammar, teaser and
page parsing, the approximate-sort early stop, and the attachment format guard.
Network-free."""

from __future__ import annotations

import json

import pytest

from raglex.adapters.uk_ofgem import (
    FACET_FIELDS,
    OfgemPublicationsAdapter,
    STOP_AFTER_STALE_PAGES,
    doc_type_for,
    page_text,
    parse_documents,
    parse_listing,
    parse_metadata,
    parse_teaser,
    stable_id,
)
from raglex.core.models import DocType

TEASER = """
  <article class="h-full">
    <a class="text-inherit no-underline block" href="/consultation/proposed-data-centre-connection-reforms">
      <div class="c-teaser-inner">
        <h3 class="text-fl-base"><span class="animate-underline-target">
          <span>Proposed data centre connection reforms</span>
        </span></h3>
        <div  class="c-wysiwyg text-fl-sm mb-0">
          <p>We are consulting on our reforms to demand connections.</p>
        </div>
      </div>
      <div class="teaser__meta text-fl-xs">
        <div>
          <span class="font-bold">Publication type:</span>
          Consultation
        </div>
        <div>
          <span class="font-bold">Publication date:</span>
          <time datetime="2026-07-29T12:00:00Z">29 July 2026</time>
        </div>
        <div>
          <span class="font-bold">Status:</span>
          Open
        </div>
        <div>
          <span class="font-bold">Topic:</span>
            Electricity transmission,
            National Energy System Operator (NESO)
        </div>
      </div>
    </a>
  </article>
"""

DETAIL = """
<html><body><main>
  <h1 class="leading-extra-tight"><span>Proposed data centre connection reforms</span></h1>
  <dl class="relative py-5">
    <div><dt class="font-bold">Publication type:</dt><dd>Consultation</dd></div>
    <div><dt class="font-bold">Publication date:</dt>
      <dd><time datetime="2026-07-29T12:00:00Z">29 July 2026</time></dd></div>
    <div><dt class="font-bold">Closing date:</dt><dd>16 September 2026</dd></div>
    <div><dt class="font-bold">Status:</dt><dd>Open</dd></div>
    <dl>
      <div><dt class="font-bold inline">Topic:&#32;</dt>
        <dd class="inline">Electricity transmission&#44;</dd></div>
      <dd class="break-words">National Energy System Operator (NESO)</dd>
    </dl>
    <div><dt class="font-bold">Subtopic:</dt><dd>Connections</dd></div>
  </dl>
  <div class="c-wysiwyg"><p>A data centre commitment fee requires relevant projects
    to secure the fee from when they accept an offer.</p></div>
  <div id="block-numiko-socialshareblock"><p>Share on Facebook</p></div>
  <script>var tracking = 1;</script>
  <a  class="media media-default media-publication_document group flex"
      href="/sites/default/files/2026-07/consultation.pdf" target="_blank">
    <span class="animate-underline-target">Curate consultation document [PDF, 1.70MB]</span>
  </a>
  <a  class="media media-default media-publication_document"
      href="/sites/default/files/2026-07/response-template.docx">
    <span>Consultation response template [DOCX, 107.19KB]</span>
  </a>
  <a  class="media media-default media-publication_document"
      href="/sites/default/files/2026-07/model.xlsx">
    <span>Price control financial model [XLSX, 0.98MB]</span>
  </a>
</main></body></html>
"""


def _payload(hrefs, dates, *, total=None):
    return {
        "items": [
            {"id": f"uuid-{n}", "markup": TEASER
             .replace("/consultation/proposed-data-centre-connection-reforms", href)
             .replace("2026-07-29T12:00:00Z", when)}
            for n, (href, when) in enumerate(zip(hrefs, dates))
        ],
        "meta": {"count": len(hrefs) if total is None else total},
    }


def _full_page(prefix, when, total):
    """A page the API considers full — ten items — so pagination continues past it."""
    hrefs = [f"/decision/{prefix}-{n}" for n in range(10)]
    return _payload(hrefs, [when] * 10, total=total)


def test_parse_teaser_reads_link_title_abstract_and_every_facet_row():
    teaser = parse_teaser(TEASER, uuid="abc")
    assert teaser is not None
    assert teaser.href == "/consultation/proposed-data-centre-connection-reforms"
    assert teaser.title == "Proposed data centre connection reforms"
    assert teaser.summary.startswith("We are consulting")
    assert teaser.published == "2026-07-29T12:00:00Z"
    assert str(teaser.date) == "2026-07-29"
    # Topic and Scheme name are the LAST rows in the block and are the two a delimited
    # ``teaser__meta`` match kept losing.
    assert teaser.meta["publication type"] == "Consultation"
    assert teaser.meta["status"] == "Open"
    assert "National Energy System Operator" in teaser.meta["topic"]


def test_parse_metadata_keeps_every_value_of_a_multi_valued_facet():
    meta = parse_metadata(DETAIL)
    assert meta["publication type"] == ["Consultation"]
    assert meta["closing date"] == ["16 September 2026"]
    # one dt, two dd — the trailing comma of the first is presentation, not data
    assert meta["topic"] == ["Electricity transmission",
                             "National Energy System Operator (NESO)"]
    assert meta["subtopic"] == ["Connections"]


def test_parse_documents_reads_url_title_format_and_size():
    docs = parse_documents(DETAIL)
    assert [d.ext for d in docs] == ["pdf", "docx", "xlsx"]
    assert docs[0].title == "Curate consultation document"
    assert docs[0].size == "1.70MB"
    assert docs[0].url.startswith("https://www.ofgem.gov.uk/sites/default/files/")


def test_page_text_drops_the_chrome_and_keeps_the_prose():
    text = page_text(DETAIL)
    assert "data centre commitment fee" in text
    assert "Share on Facebook" not in text and "var tracking" not in text


def test_stable_id_and_doc_type_follow_the_site_path_and_publication_type():
    assert stable_id("/decision/foo-bar") == "ofgem/decision/foo-bar"
    assert doc_type_for("Decision") is DocType.DECISION
    assert doc_type_for("Licence modification") is DocType.DECISION
    assert doc_type_for("Consultation") is DocType.GUIDANCE
    assert doc_type_for(None) is DocType.GUIDANCE


class _Resp:
    def __init__(self, content: bytes):
        self.content = content
        self.status_code = 200

    def json(self):
        return json.loads(self.content.decode())


class _FakeClient:
    """Serves the listing pages by index and one detail page for everything else."""

    def __init__(self, pages, detail=DETAIL):
        self.pages, self.detail = pages, detail
        self.urls: list[str] = []

    def get(self, url, headers=None):
        self.urls.append(url)
        if "/api/listing/" in url:
            page = 0
            if "page=" in url:
                page = int(url.split("page=")[1].split("&")[0])
            payload = self.pages[page] if page < len(self.pages) else {"items": [],
                                                                       "meta": {}}
            return _Resp(json.dumps(payload).encode())
        if url.endswith(".pdf"):
            return _Resp(b"%PDF-1.7 fake")
        if url.endswith(".xlsx"):
            raise AssertionError("a spreadsheet must never be downloaded for text")
        return _Resp(self.detail.encode())


def test_listing_url_uses_the_sites_own_query_grammar():
    plain = OfgemPublicationsAdapter()
    # page 1 omits the parameter; the API's page is 0-indexed
    assert "page=" not in plain._listing_url(1)
    assert "page=2" in plain._listing_url(3)
    assert "sort[field_published][direction]=desc" in plain._listing_url(1)

    filtered = OfgemPublicationsAdapter(
        facet="facet_case_publication_type", facet_value="1602", query="data centre")
    url = filtered._listing_url(1)
    # the facet's PATH must accompany its value — a bare value is accepted with a 200
    # and silently ignored, which would quietly walk the whole register
    assert (f"filter[facet_case_publication_type][path]="
            f"{FACET_FIELDS['facet_case_publication_type']}") in url
    assert "filter[facet_case_publication_type][value][]=1602" in url
    assert "fulltext=data+centre" in url


def test_unknown_facet_is_refused_rather_than_silently_ignored():
    with pytest.raises(ValueError):
        OfgemPublicationsAdapter(facet="not-a-facet", facet_value="1")


def test_discover_yields_stubs_with_the_watermark_and_page_metadata():
    pages = [_payload(["/decision/a", "/decision/b"],
                      ["2026-07-29T12:00:00Z", "2026-07-28T12:00:00Z"])]
    adapter = OfgemPublicationsAdapter(client=_FakeClient(pages))
    stubs = list(adapter.discover(None, max_pages=1))
    assert [s.stable_id for s in stubs] == ["ofgem/decision/a", "ofgem/decision/b"]
    assert stubs[0].hints["watermark"] == "2026-07-29T12:00:00Z"
    assert stubs[0].court == "Ofgem" and str(stubs[0].hint_date) == "2026-07-29"


def test_discover_survives_the_feeds_approximate_date_order():
    """A single exhausted page does not end the run: the feed's sort is only roughly
    descending, and a later page really does carry newer items."""
    old, new = "2026-01-01T12:00:00Z", "2026-07-29T12:00:00Z"
    total = 200                                      # far more than we intend to read
    pages = [
        _full_page("fresh", new, total),
        _full_page("stale-a", old, total),           # one exhausted page …
        _full_page("late", new, total),              # … followed by real new items
    ] + [_full_page(f"stale-{n}", old, total)
         for n in range(STOP_AFTER_STALE_PAGES)]
    client = _FakeClient(pages)
    adapter = OfgemPublicationsAdapter(client=client)
    found = [s.stable_id for s in adapter.discover("2026-06-01T00:00:00Z")]
    assert len(found) == 20
    assert "ofgem/decision/fresh-0" in found and "ofgem/decision/late-9" in found
    # and it does stop once the run of exhausted pages is long enough — no further
    # listing request is made after the third consecutive empty one
    assert len(client.urls) == 3 + STOP_AFTER_STALE_PAGES


def test_fetch_inlines_readable_attachments_and_records_the_rest_unread():
    pages = [_payload(["/consultation/x"], ["2026-07-29T12:00:00Z"])]
    client = _FakeClient(pages)
    adapter = OfgemPublicationsAdapter(client=client)
    stub = next(iter(adapter.discover(None, max_pages=1)))
    record = adapter.fetch(stub)

    assert record.doc_type is DocType.GUIDANCE and record.court == "Ofgem"
    assert str(record.decision_date) == "2026-07-29"
    assert record.extra["status"] == "Open"
    assert record.extra["closing_date"] == "16 September 2026"
    assert record.extra["topics"] == ["Electricity transmission",
                                      "National Energy System Operator (NESO)"]
    assert record.extra["require_recognized_legal_citation"] is True
    # The XLSX has no text engine: it is held as an attachment and never downloaded,
    # because byte-decoding a zip yields megabytes of noise, not data.
    by_format = {a["format"]: a for a in record.extra["attachments"]}
    assert by_format["xlsx"]["skipped"] == "no-text-engine"
    assert "bytes" not in by_format["xlsx"]
    assert set(by_format) == {"pdf", "docx", "xlsx"}
    assert "electricity-transmission" in record.topic_tags


def test_fetch_can_be_told_not_to_download_anything():
    pages = [_payload(["/consultation/x"], ["2026-07-29T12:00:00Z"])]
    client = _FakeClient(pages)
    adapter = OfgemPublicationsAdapter(client=client, include_documents=False)
    record = adapter.fetch(next(iter(adapter.discover(None, max_pages=1))))
    assert "attachments" not in record.extra
    assert not any(u.endswith(".pdf") for u in client.urls)


def test_a_reported_resume_offset_is_accepted_back():
    """An adapter that reports ``resume_offset`` must accept it as ``start_offset``:
    ``get_adapter`` passes the resumed job's options straight to the constructor, so
    one that lacks the parameter does not restart from the top — it raises TypeError
    and the resume fails. A 24,059-item walk WILL be interrupted."""
    class _AnyPage(_FakeClient):
        """Serves a full page whatever index is asked for, so the assertion is about
        WHICH page the resumed walk requests."""

        def get(self, url, headers=None):
            self.urls.append(url)
            return _Resp(json.dumps(
                _full_page("p", "2026-07-29T12:00:00Z", 24059)).encode())

    client = _AnyPage([])
    adapter = OfgemPublicationsAdapter(client=client, start_offset=17780)
    stubs = list(adapter.discover(None, max_pages=1))
    assert len(stubs) == 10
    # the resumed walk asks for the page the checkpoint stopped on, not page one
    assert "page=1778" in client.urls[0]
    assert stubs[0].hints["resume_offset"] == 17780
