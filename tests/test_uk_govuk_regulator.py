import json

from raglex.adapters.uk_govuk_regulator import CONTENT, GOVUKRegulatorAdapter, content_text
from raglex.core.models import DocType, Stub


def test_content_text_combines_structured_parts_and_description():
    content = {
        "description": "A formal decision.",
        "details": {
            "body": "<p>Under the Competition Act 1998.</p>",
            "parts": [{"title": "Outcome", "body": "<p>See [2024] UKSC 1.</p>"}],
        },
    }
    text = content_text(content)
    assert "Competition Act 1998" in text
    assert "Outcome\nSee [2024] UKSC 1." in text
    assert text.endswith("A formal decision.")


class _Response:
    def __init__(self, data=None, *, content=None):
        self._data = data
        self.content = content if content is not None else json.dumps(data).encode()

    def json(self):
        return self._data


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses[url]
        return value() if callable(value) else value


def test_cma_guidance_follows_html_children_and_declares_safe_default():
    parent_url = f"{CONTENT}/government/publications/unfair-commercial-practices-cma207"
    child_url = f"{CONTENT}/government/publications/cma207/body"
    parent = {
        "base_path": "/government/publications/unfair-commercial-practices-cma207",
        "title": "Unfair commercial practices: CMA207",
        "first_published_at": "2025-04-04T00:00:00Z",
        "details": {
            "body": "<p>Overview.</p>",
            "attachments": [
                {"attachment_type": "html", "title": "Unfair commercial practices",
                 "url": "/government/publications/cma207/body"},
                {"attachment_type": "file", "title": "Unfair commercial practices",
                 "url": "https://assets.test/cma207.pdf",
                 "content_type": "application/pdf", "unique_reference": "CMA207"},
            ],
        },
    }
    child = {"title": "Unfair commercial practices",
             "details": {"body": "<p>Section 225 of the 2024 Act applies.</p>"}}
    client = _Client({parent_url: _Response(parent), child_url: _Response(child)})
    adapter = GOVUKRegulatorAdapter(
        source="uk-cma-guidance", organisation="competition-and-markets-authority",
        court="CMA", record_doc_type=DocType.GUIDANCE,
        require_recognized_legal_citation=False, client=client,
    )
    record = adapter.fetch(Stub(
        stable_id="x", raw_url=parent_url, landing_url="https://www.gov.uk/x", hints={}
    ))
    assert record and "Section 225" in record.text
    assert "https://assets.test/cma207.pdf" not in [url for url, _ in client.calls]
    assert record.extra["aliases"] == ["CMA207"]
    assert record.extra["citation_default_instrument"]["id"] == "ukpga/2024/13"


# ── renditions: accessible HTML wins over its PDF twins ──────────────────────
def test_accessible_html_rendition_beats_the_pdf_twins():
    """GOV.UK publishes one document three times and distinguishes the renditions only
    by a trailing parenthetical, so a verbatim title comparison never matched and both
    PDFs were inlined beside the HTML — tripling the text."""
    parent_url = f"{CONTENT}/government/publications/statement-of-changes-hc-259"
    child_url = f"{CONTENT}/government/publications/hc-259/body"
    title = "Statement of changes to the Immigration Rules: HC 259, 9 July 2026"
    parent = {
        "base_path": "/government/publications/statement-of-changes-hc-259",
        "title": title,
        "details": {"attachments": [
            {"attachment_type": "html", "title": f"{title} (accessible)",
             "url": "/government/publications/hc-259/body"},
            {"attachment_type": "file", "title": title,
             "url": "https://assets.test/hc259.pdf", "content_type": "application/pdf"},
            {"attachment_type": "file", "title": f"{title} (print ready)",
             "url": "https://assets.test/hc259-print.pdf",
             "content_type": "application/pdf"},
        ]},
    }
    child = {"title": title,
             "details": {"body": "<p>Paragraph 6 of the Immigration Rules is amended. "
                                 "See the Immigration Act 1971.</p>"}}
    client = _Client({parent_url: _Response(parent), child_url: _Response(child)})
    adapter = GOVUKRegulatorAdapter(source="uk-govuk-policy",
                                    supergroup="policy_and_engagement", client=client)
    record = adapter.fetch(Stub(stable_id="x", raw_url=parent_url,
                                landing_url="https://www.gov.uk/x", hints={}))
    assert record and "Paragraph 6 of the Immigration Rules" in record.text
    assert not [url for url, _ in client.calls if url.endswith(".pdf")]
    skipped = {a["title"]: a.get("skipped") for a in record.extra["attachments"]
               if a["type"] == "pdf"}
    assert set(skipped.values()) == {"html-rendition-preferred"}


def test_several_pdf_renditions_with_no_html_yield_one_copy():
    parent_url = f"{CONTENT}/government/publications/report"
    parent = {
        "base_path": "/government/publications/report", "title": "A report",
        "details": {"attachments": [
            {"attachment_type": "file", "title": "A report (accessible)",
             "url": "https://assets.test/a.pdf", "content_type": "application/pdf"},
            {"attachment_type": "file", "title": "A report (print ready)",
             "url": "https://assets.test/b.pdf", "content_type": "application/pdf"},
        ]},
    }
    pdfs = {"https://assets.test/a.pdf": _Response(None, content=b"%PDF-1.4 not-a-real-pdf")}
    client = _Client({parent_url: _Response(parent), **pdfs})
    adapter = GOVUKRegulatorAdapter(source="uk-govuk-policy",
                                    supergroup="policy_and_engagement", client=client)
    adapter.fetch(Stub(stable_id="x", raw_url=parent_url,
                       landing_url="https://www.gov.uk/x", hints={}))
    fetched = [url for url, _ in client.calls if url.endswith(".pdf")]
    assert fetched == ["https://assets.test/a.pdf"]   # the second rendition is skipped


# ── the cross-government feed: publisher and categorisation come from the data ─
def test_policy_feed_attributes_each_item_to_its_own_publisher():
    from raglex.adapters.uk_govuk_regulator import SEARCH, _publisher, _rendition_key

    assert _rendition_key("A doc (accessible)") == _rendition_key("A doc (print ready)")
    assert _rendition_key("A doc") == _rendition_key("A doc (accessible)")
    assert _rendition_key("A doc") != _rendition_key("Another doc")
    name, slug = _publisher({"organisations": [
        {"title": "Office for Product Safety and Standards",
         "slug": "office-for-product-safety-and-standards"}]})
    assert (name, slug) == ("Office for Product Safety and Standards",
                            "office-for-product-safety-and-standards")

    search = {"total": 1, "results": [{
        "title": "Fireworks and pyrotechnics in the UK",
        "link": "/government/consultations/fireworks",
        "content_store_document_type": "open_consultation",
        "content_purpose_subgroup": "consultations",
        "public_timestamp": "2026-07-15T23:01:00Z",
        "organisations": [{"title": "Home Office", "slug": "home-office"}],
    }]}
    content_url = f"{CONTENT}/government/consultations/fireworks"
    content = {"base_path": "/government/consultations/fireworks",
               "title": "Fireworks and pyrotechnics in the UK",
               "document_type": "open_consultation",
               "details": {"body": "<p>Consultation under the Fireworks Act 2003.</p>"}}
    client = _Client({SEARCH: _Response(search), content_url: _Response(content)})
    adapter = GOVUKRegulatorAdapter(source="uk-govuk-policy",
                                    supergroup="policy_and_engagement",
                                    id_prefix="govuk", client=client)
    stub = next(iter(adapter.discover(None)))
    assert client.calls[0][1]["params"]["filter_content_purpose_supergroup"] == (
        "policy_and_engagement")
    # the shared GOV.UK namespace, not the source key
    assert stub.stable_id == "govuk/government/consultations/fireworks"
    assert stub.court == "Home Office"

    record = adapter.fetch(stub)
    assert record.court == "Home Office"            # the page's own "From:" line
    assert record.stable_id == "govuk/government/consultations/fireworks"
    assert record.extra["organisation_slug"] == "home-office"
    assert record.extra["content_purpose_subgroup"] == "consultations"
    # GOV.UK's own categorisation, not one invented here
    assert set(record.topic_tags) >= {"govuk", "policy-and-engagement", "consultations",
                                      "open-consultation", "home-office"}


def test_govuk_feeds_share_one_id_namespace_so_overlaps_are_one_document():
    """268 CMA publications are also in the cross-government policy corpus. Keyed by
    source, that page would be stored twice."""
    from raglex.adapters.registry import get_adapter

    assert get_adapter("uk-cma").id_prefix == "govuk"
    assert get_adapter("uk-govuk-policy").id_prefix == "govuk"
    # the CMA feed is now the whole organisation, not the consumer-enforcement facet
    assert get_adapter("uk-cma").organisation == "competition-and-markets-authority"
    assert get_adapter("uk-cma").document_type is None


def test_a_feed_needs_a_filter_and_a_publisher():
    import pytest

    with pytest.raises(ValueError):
        GOVUKRegulatorAdapter(source="x", court="X")
    with pytest.raises(ValueError):          # one organisation, but nobody named
        GOVUKRegulatorAdapter(source="x", organisation="home-office")


def test_policy_source_is_registered_and_catalogued():
    from raglex.adapters.registry import SOURCE_INFO, get_adapter, source_catalog

    catalog = {s["key"]: s for s in source_catalog()}
    assert "uk-govuk-policy" in catalog and catalog["uk-govuk-policy"]["can_incremental"]
    for opt in SOURCE_INFO["uk-govuk-policy"].options:
        get_adapter("uk-govuk-policy", **{opt.name: "policy_and_engagement"
                                          if opt.name == "supergroup" else "home-office"})


# ── the one-time id migration ────────────────────────────────────────────────
def _govuk_facade():
    import os
    import tempfile

    from raglex.config import Config
    from raglex.facade import Facade

    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def _hold(facade, stable_id, source, *, cites=()):
    from datetime import date as _date

    from raglex.core.models import (DocType, ExtractedVia, Record, RelationshipType,
                                    ResolutionStatus, TypedRelation)

    rec = Record(
        source=source, stable_id=stable_id, doc_type=DocType.GUIDANCE,
        title=f"Doc {stable_id}", court="CMA", decision_date=_date(2025, 1, 1),
        language="en", raw_bytes=stable_id.encode(), raw_ext="json",
        text="Under the Enterprise Act 2002 the CMA may act.",
        extracted_via=ExtractedVia.STRUCTURED,
        relations=[TypedRelation(relationship_type=RelationshipType.INTERPRETS,
                                 raw_citation_string=c, dst_id=c,
                                 extracted_via=ExtractedVia.STRUCTURED,
                                 resolution_status=ResolutionStatus.PENDING)
                   for c in cites],
    )
    rec.ensure_payload_hash()
    with facade._open() as (cat, _rs, _ts):
        cat.upsert_document(rec)


def test_rekey_govuk_ids_is_a_dry_run_by_default():
    facade = _govuk_facade()
    _hold(facade, "uk-cma/government/publications/cma207", "uk-cma")
    plan = facade.rekey_govuk_ids()
    assert plan["applied"] is False and plan["rekeyed"] == 0
    assert plan["changes"] == [{
        "old": "uk-cma/government/publications/cma207",
        "new": "govuk/government/publications/cma207",
        "source": "uk-cma", "kind": "rename"}]
    with facade._open() as (cat, _rs, _ts):        # nothing moved
        assert cat.get_document("uk-cma/government/publications/cma207") is not None


def test_rekey_govuk_ids_merges_twins_and_keeps_their_edges():
    """The whole reason this is a re-key and not a delete-and-refetch: the same GOV.UK
    page held under two source keys must become ONE document without losing either
    copy's citation edges."""
    from raglex.core.text import fold

    facade = _govuk_facade()
    path = "government/publications/unfair-commercial-practices-cma207"
    _hold(facade, f"uk-cma/{path}", "uk-cma", cites=["ukpga/2002/40"])
    _hold(facade, f"uk-cma-guidance/{path}", "uk-cma-guidance", cites=["ukpga/2024/13"])

    st = facade.rekey_govuk_ids(apply=True)
    assert st["applied"] is True and st["scanned"] == 2
    assert st["merged"] == 1 and st["rekeyed"] == 1

    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document(f"govuk/{path}") is not None
        assert cat.get_document(f"uk-cma/{path}") is None
        assert cat.get_document(f"uk-cma-guidance/{path}") is None
        # both copies' edges survive on the one surviving document
        dsts = {r["dst_id"] for r in cat.conn.execute(
            "SELECT dst_id FROM relations WHERE src_id = ?", (f"govuk/{path}",))}
        assert {"ukpga/2002/40", "ukpga/2024/13"} <= dsts
        # the retired ids still resolve, so old links and exports don't break
        assert cat.get_alias(fold(f"uk-cma/{path}")) == f"govuk/{path}"
        assert cat.get_alias(fold(f"uk-cma-guidance/{path}")) == f"govuk/{path}"


def test_rekey_govuk_ids_is_idempotent():
    facade = _govuk_facade()
    _hold(facade, "uk-ofgem/government/publications/x", "uk-ofgem")
    first = facade.rekey_govuk_ids(apply=True)
    second = facade.rekey_govuk_ids(apply=True)
    assert first["rekeyed"] == 1
    assert second["scanned"] == 0 and second["changes"] == []


def test_rekey_govuk_ids_leaves_other_sources_alone():
    facade = _govuk_facade()
    _hold(facade, "uk-ico/enforcement/2026/x", "uk-ico-enforcement")
    _hold(facade, "govuk/government/publications/already", "uk-govuk-policy")
    st = facade.rekey_govuk_ids(apply=True)
    assert st["scanned"] == 0 and st["changes"] == []
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_document("uk-ico/enforcement/2026/x") is not None


def test_dry_run_predicts_the_merges_the_apply_will_do():
    """The plan is the number the reader decides on. A collision is usually created BY
    this run — both copies of one page want the same target — so asking the database
    alone would call every move a rename and under-report the merges."""
    facade = _govuk_facade()
    path = "government/publications/cma207"
    _hold(facade, f"uk-cma/{path}", "uk-cma")
    _hold(facade, f"uk-cma-guidance/{path}", "uk-cma-guidance")
    _hold(facade, "uk-ofwat/government/publications/z", "uk-ofwat")

    plan = facade.rekey_govuk_ids()
    kinds = [c["kind"] for c in plan["changes"]]
    assert kinds.count("merge") == 1 and kinds.count("rename") == 2

    applied = facade.rekey_govuk_ids(apply=True)
    assert applied["merged"] == 1 and applied["rekeyed"] == 2
    # …and the plan named the same survivor the apply kept
    assert [c["old"] for c in applied["changes"]] == [c["old"] for c in plan["changes"]]


# ── concurrency must overlap the waiting, not spend more of the allowance ─────
def test_the_pacer_holds_the_aggregate_rate_under_concurrency():
    """Concurrent fetching is only polite if the shared pacer is thread-safe. Without the
    lock two threads both observe the interval elapsed and fire together, quietly
    doubling the rate the source was promised."""
    import threading
    import time

    from raglex.core.http import RateLimitedClient

    slept: list[float] = []
    lock = threading.Lock()

    def fake_sleep(s):
        with lock:
            slept.append(s)
        time.sleep(min(s, 0.001))          # keep the test fast, keep the ordering

    client = RateLimitedClient("t", min_interval=0.05, sleep=fake_sleep,
                               client=object())
    threads = [threading.Thread(target=client._pace) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Eight callers, so seven of them must have waited, and the reservations must be
    # spread across the interval rather than all claiming "now".
    assert len(slept) >= 7, slept
    assert max(slept) >= 0.05 * 6, slept    # the last caller waits ~7 intervals


def test_one_documents_attachments_are_fetched_together():
    """A publication costs ~3.6 requests and each was paid serially. They are independent
    fetches for the SAME document, so they overlap — one document at a time still, which
    is what keeps watermark, dedup and resume semantics untouched."""
    import threading

    inflight, peak = [0], [0]
    lock = threading.Lock()

    class _SlowClient:
        def get(self, url, **kw):
            with lock:
                inflight[0] += 1
                peak[0] = max(peak[0], inflight[0])
            try:
                import time
                time.sleep(0.05)
                return _Response(None, content=b"%PDF-1.4")
            finally:
                with lock:
                    inflight[0] -= 1

    adapter = GOVUKRegulatorAdapter(source="uk-govuk-policy",
                                    supergroup="policy_and_engagement",
                                    client=_SlowClient())
    got = adapter._fetch_bytes_many([f"https://assets.test/{n}.pdf" for n in range(6)])
    assert len(got) == 6
    assert peak[0] > 1, "attachments were still fetched one at a time"


def test_a_failing_attachment_costs_only_itself():
    """A 404 attachment must cost that attachment, not the publication."""
    class _Flaky:
        def get(self, url, **kw):
            if url.endswith("2.pdf"):
                raise FetchError("404")
            return _Response(None, content=b"ok")

    adapter = GOVUKRegulatorAdapter(source="uk-govuk-policy",
                                    supergroup="policy_and_engagement",
                                    client=_Flaky())
    got = adapter._fetch_bytes_many([f"https://assets.test/{n}.pdf" for n in range(4)])
    assert set(got) == {f"https://assets.test/{n}.pdf" for n in (0, 1, 3)}


def test_a_rate_limit_still_reaches_the_pipeline():
    """The one failure that must NOT be swallowed: the pipeline pauses the source's queue
    on it, rather than hammering on through the rest of the corpus."""
    import pytest

    from raglex.core.errors import RateLimitException

    class _Limited:
        def get(self, url, **kw):
            raise RateLimitException("uk-govuk-policy", retry_after=30)

    adapter = GOVUKRegulatorAdapter(source="uk-govuk-policy",
                                    supergroup="policy_and_engagement",
                                    client=_Limited())
    with pytest.raises(RateLimitException):
        adapter._fetch_bytes_many([f"https://assets.test/{n}.pdf" for n in range(3)])
