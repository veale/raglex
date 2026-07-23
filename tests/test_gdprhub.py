"""GDPRhub adapter — feed/infobox/section parsing (pure), regime-edge mining,
record building, and offset pagination. Network-free (a fake fetcher stands in for
the Anubis-walled Atom feed)."""

from __future__ import annotations

import os

import pytest

from raglex.adapters.gdprhub import (
    GDPRhubAdapter,
    build_record,
    build_relations,
    parse_feed,
    parse_report,
    stable_id_for,
    _api_json,
    _offset_from,
)
from raglex.core.models import DocType, ExtractedVia, RelationshipType

GDPR_CELEX = "32016R0679"

# A DPA report: infobox + summary + analysis + a <pre> machine translation. The prose
# deliberately contains the English word "led" (which must NOT mine a Law-Enforcement-
# Directive edge) and a genuine "Directive 2016/680" reference (which must).
DPA_ENTRY = """<entry>
<id>https://gdprhub.eu/index.php?title=NAIH_(Hungary)_-_NAIH-11443-3/2026</id>
<title>NAIH (Hungary) - NAIH-11443-3/2026</title>
<link rel="alternate" type="text/html" href="https://gdprhub.eu/index.php?title=NAIH_(Hungary)_-_NAIH-11443-3/2026"/>
<updated>2026-07-23T13:23:26Z</updated>
<summary type="html">&lt;div&gt;{{DPAdecisionBOX&lt;br /&gt;
|Jurisdiction=Hungary&lt;br /&gt;
|Case_Number_Name=NAIH-11443-3/2026&lt;br /&gt;
|ECLI=&lt;br /&gt;
|DPA_Abbrevation=NAIH&lt;br /&gt;
|DPA_With_Country=NAIH (Hungary)&lt;br /&gt;
|Original_Source_Name_1=NAIH&lt;br /&gt;
|Original_Source_Link_1=https://naih.hu/hatarozatok&lt;br /&gt;
|Original_Source_Language_1=Hungarian&lt;br /&gt;
|Original_Source_Language__Code_1=HU&lt;br /&gt;
|Type=Investigation&lt;br /&gt;
|Outcome=Violation Found&lt;br /&gt;
|Date_Decided=22.07.2026&lt;br /&gt;
|Date_Published=23.07.2026&lt;br /&gt;
|Fine=2,000,000&lt;br /&gt;
|Currency=HUF&lt;br /&gt;
|GDPR_Article_1=Article 12(1) GDPR&lt;br /&gt;
|GDPR_Article_2=Article 13(1)(c) GDPR&lt;br /&gt;
|Initial_Contributor=av&lt;br /&gt;
}}&lt;br /&gt;
== English Summary ==&lt;br /&gt;
=== Facts ===&lt;br /&gt;
A complaint led to an investigation. Directive 2016/680 was cited by the parties.&lt;br /&gt;
=== Holding ===&lt;br /&gt;
The DPA found a violation of the transparency obligations.&lt;br /&gt;
== Comment ==&lt;br /&gt;
This decision illustrates the Hungarian DPA's focus on transparency.&lt;br /&gt;
== Further Resources ==&lt;br /&gt;
Share your comments here.&lt;br /&gt;
== English Machine Translation of the Decision ==&lt;br /&gt;
The decision below is a machine translation of the Hungarian original.&lt;br /&gt;
&lt;pre&gt;DECISION&lt;br /&gt;The Authority establishes a violation and imposes a fine.&lt;/pre&gt;
</summary>
</entry>"""

# A court judgment with a real ECLI and a Charter (CFR) reference to mine.
COURT_ENTRY = """<entry>
<id>https://gdprhub.eu/index.php?title=Rb._Noord-Holland_-_C/15/376188</id>
<title>Rb. Noord-Holland - C/15/376188</title>
<link rel="alternate" type="text/html" href="https://gdprhub.eu/index.php?title=Rb._Noord-Holland_-_C/15/376188"/>
<updated>2026-07-10T09:00:00Z</updated>
<summary type="html">&lt;div&gt;{{COURTdecisionBOX&lt;br /&gt;
|Jurisdiction=Netherlands&lt;br /&gt;
|Case_Number_Name=C/15/376188&lt;br /&gt;
|ECLI=ECLI:NL:RBNHO:2026:8438&lt;br /&gt;
|Court_Abbrevation=Rb. Noord-Holland&lt;br /&gt;
|Court_With_Country=Rb. Noord-Holland (Netherlands)&lt;br /&gt;
|Original_Source_Name_1=de Rechtspraak&lt;br /&gt;
|Original_Source_Link_1=https://uitspraken.rechtspraak.nl/x.pdf&lt;br /&gt;
|Original_Source_Language_1=Dutch&lt;br /&gt;
|Original_Source_Language__Code_1=NL&lt;br /&gt;
|GDPR_Article_1=Article 82 GDPR&lt;br /&gt;
}}&lt;br /&gt;
== English Summary ==&lt;br /&gt;
The court considered Article 8 CFR alongside the GDPR.&lt;br /&gt;
== Comment ==&lt;br /&gt;
A useful damages ruling.&lt;br /&gt;
</summary>
</entry>"""


def _feed(*entries: str) -> bytes:
    body = "\n".join(entries)
    return (
        '<?xml version="1.0"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en">\n'
        f"{body}\n</feed>"
    ).encode("utf-8")


# ── pure parsing ─────────────────────────────────────────────────────────────
def test_parse_feed_extracts_entries():
    entries = parse_feed(_feed(DPA_ENTRY, COURT_ENTRY))
    assert len(entries) == 2
    e = entries[0]
    assert e.page_title == "NAIH_(Hungary)_-_NAIH-11443-3/2026"
    assert e.display_title == "NAIH (Hungary) - NAIH-11443-3/2026"
    assert e.updated == "2026-07-23T13:23:26Z"


def test_parse_feed_tolerates_garbage():
    assert parse_feed(b"not xml at all") == []


def test_parse_report_infobox_and_sections():
    (entry,) = parse_feed(_feed(DPA_ENTRY))
    r = parse_report(entry.summary_html)
    assert r.box_type == "DPAdecisionBOX"
    assert r.params["Jurisdiction"] == "Hungary"
    assert r.params["Fine"] == "2,000,000"
    assert "focus on transparency" in r.analysis
    # the <pre> body is the translation; the boilerplate lead sentence is dropped
    assert "imposes a fine" in r.translation
    assert "machine translation of the Hungarian" not in r.translation


def test_double_escaped_browser_form_parses():
    """The stealth browser fetch delivers the feed one escape-level deeper (and MediaWiki
    double-escapes <pre>); canonicalisation must still recover the box + translation."""
    import html as h

    inner = (
        "{{DPAdecisionBOX<br />\n|Jurisdiction=Hungary<br />\n"
        "|Case_Number_Name=NAIH-1/2026<br />\n|GDPR_Article_1=Article 5 GDPR<br />\n}}<br />\n"
        "== English Machine Translation of the Decision ==<br />\n"
        "The decision below is a machine translation.<br />\n"
        "<pre>THE TRANSLATED BODY TEXT</pre>"
    )
    r = parse_report(h.escape(h.escape(inner)))   # doubly-escaped, as the browser returns
    assert r.box_type == "DPAdecisionBOX"
    assert r.params["Jurisdiction"] == "Hungary"
    assert "THE TRANSLATED BODY TEXT" in r.translation


def test_empty_infobox_fields_dropped():
    (entry,) = parse_feed(_feed(DPA_ENTRY))
    r = parse_report(entry.summary_html)
    assert "ECLI" not in r.params  # present-but-empty in the wikitext


# ── regime edge mining ───────────────────────────────────────────────────────
def test_gdpr_article_edges_are_structured():
    (entry,) = parse_feed(_feed(DPA_ENTRY))
    rels = build_relations(parse_report(entry.summary_html))
    gdpr = [x for x in rels if x.dst_id == GDPR_CELEX]
    assert {x.dst_anchor for x in gdpr} == {"Article 12(1)", "Article 13(1)(c)"}
    assert all(x.extracted_via is ExtractedVia.STRUCTURED for x in gdpr)
    assert all(x.relationship_type is RelationshipType.INTERPRETS for x in gdpr)


def test_led_reference_mined_but_not_the_word_led():
    """'Directive 2016/680' mines an LED edge; the English word 'led' does not."""
    (entry,) = parse_feed(_feed(DPA_ENTRY))
    rels = build_relations(parse_report(entry.summary_html))
    led = [x for x in rels if x.dst_id == "32016L0680"]
    assert len(led) == 1
    assert led[0].extracted_via is ExtractedVia.REGEX


def test_no_spurious_led_edge_without_directive():
    (entry,) = parse_feed(_feed(COURT_ENTRY))
    rels = build_relations(parse_report(entry.summary_html))
    assert not [x for x in rels if x.dst_id == "32016L0680"]


def test_charter_reference_mined():
    (entry,) = parse_feed(_feed(COURT_ENTRY))
    rels = build_relations(parse_report(entry.summary_html))
    assert any(x.dst_id == "12012P" for x in rels)


# ── record building ──────────────────────────────────────────────────────────
def test_dpa_record_identity_and_bucket():
    (entry,) = parse_feed(_feed(DPA_ENTRY))
    r = build_record(entry, "gdprhub")
    assert r.doc_type is DocType.DECISION
    assert r.court == "dpa-hu"
    assert r.stable_id == "gdprhub/naih-hungary-naih-11443-3-2026"
    assert r.ecli is None
    assert r.source_language == "hu"
    assert r.language == "en"
    assert r.decision_date.isoformat() == "2026-07-22"
    # native case number minted as a resolution alias
    assert "naih-11443-3/2026" in r.extra["aliases"]
    # the machine translation is the body
    assert "imposes a fine" in r.text
    assert r.extra["has_translation"] is True
    assert r.extra["fine"] == "2,000,000" and r.extra["currency"] == "HUF"
    assert r.extra["original_sources"][0]["url"] == "https://naih.hu/hatarozatok"


def test_court_record_uses_ecli_and_judgment_type():
    (entry,) = parse_feed(_feed(COURT_ENTRY))
    r = build_record(entry, "gdprhub")
    assert r.doc_type is DocType.JUDGMENT
    assert r.court == "court-nl"
    assert r.ecli == "ECLI:NL:RBNHO:2026:8438"


def test_body_falls_back_to_summary_when_no_translation():
    (entry,) = parse_feed(_feed(COURT_ENTRY))
    r = build_record(entry, "gdprhub")
    assert r.extra["has_translation"] is False
    assert "considered Article 8 CFR" in r.text  # summary stands in for the body
    assert r.extra["gdprhub_analysis"] == "A useful damages ruling."


# ── pagination ───────────────────────────────────────────────────────────────
class _FakePage:
    def __init__(self, html: str) -> None:
        self.html = html


class _FakeFetcher:
    """Serves feed pages keyed by the ``offset`` query param; empty when exhausted."""

    name = "fake"

    def __init__(self, pages: dict[str | None, bytes]) -> None:
        self.pages = pages
        self.calls: list[str | None] = []

    def fetch(self, url: str, *, headers=None) -> _FakePage:
        import re as _re
        m = _re.search(r"[?&]offset=(\d+)", url)
        off = m.group(1) if m else None
        self.calls.append(off)
        return _FakePage((self.pages.get(off) or _feed()).decode("utf-8"))

    def close(self) -> None:
        pass


def test_offset_from_iso():
    assert _offset_from("2026-07-23T13:23:26Z") == "20260723132326"
    assert _offset_from(None) is None


def test_backfill_walks_pages_by_offset():
    page1 = _feed(DPA_ENTRY)                 # oldest updated 2026-07-23T13:23:26Z
    page2 = _feed(COURT_ENTRY)               # oldest updated 2026-07-10T09:00:00Z
    fetcher = _FakeFetcher({None: page1, "20260723132326": page2})
    adapter = GDPRhubAdapter(fetcher=fetcher)
    stubs = list(adapter.discover(None))
    ids = [s.stable_id for s in stubs]
    assert "gdprhub/naih-hungary-naih-11443-3-2026" in ids
    assert "gdprhub/rb-noord-holland-c-15-376188" in ids
    # walked: head → offset of page1's oldest → empty page stops it
    assert fetcher.calls[:2] == [None, "20260723132326"]


def test_incremental_stops_at_watermark():
    fetcher = _FakeFetcher({None: _feed(DPA_ENTRY, COURT_ENTRY)})
    adapter = GDPRhubAdapter(fetcher=fetcher)
    # watermark newer than the court entry, older than the DPA entry
    stubs = list(adapter.discover("2026-07-15T00:00:00Z"))
    ids = [s.stable_id for s in stubs]
    assert ids == ["gdprhub/naih-hungary-naih-11443-3-2026"]


def test_fetch_builds_from_discovery_cache():
    fetcher = _FakeFetcher({None: _feed(DPA_ENTRY)})
    adapter = GDPRhubAdapter(fetcher=fetcher)
    (stub,) = list(adapter.discover("2026-07-01T00:00:00Z"))
    rec = adapter.fetch(stub)
    assert rec is not None and rec.stable_id == stub.stable_id


# ── API backfill mode ────────────────────────────────────────────────────────
# Raw wikitext as the ``prop=revisions`` API returns it: real newlines (no <br />),
# blank lines between infobox groups, N/A placeholders, a <pre> translation.
RAW_WIKITEXT = """{{DPAdecisionBOX

|Jurisdiction=Romania
|DPA_Abbrevation=ANSPDCP
|DPA_With_Country=ANSPDCP (Romania)

|Case_Number_Name=N/A
|ECLI=N/A

|Original_Source_Name_1=Romanian DPA
|Original_Source_Link_1=https://www.dataprotection.ro/?page=x&lang=ro
|Original_Source_Language_1=Romanian
|Original_Source_Language__Code_1=RO

|GDPR_Article_1=Article 5(1)(f) GDPR
|Date_Decided=31.08.2023
}}

== English Summary ==

=== Facts ===
A physician recorded a patient. Directive 2016/680 was mentioned.

=== Holding ===
The DPA imposed a fine.

== Comment ==
A useful example of Article 5 security obligations.

== English Machine Translation of the Decision ==
The decision below is a machine translation.
<pre>
DECISION: a fine of 1000 EUR is imposed.
</pre>"""


def _wrap_api_json(obj) -> str:
    """Mimic the stealth browser: JSON wrapped in <html><body> and HTML-escaped."""
    import html as _h
    import json as _j
    return f"<html><body>{_h.escape(_j.dumps(obj))}</body></html>"


class _ApiPage:
    def __init__(self, html: str) -> None:
        self.html = html


class _ApiFetcher:
    """Serves allpages + revisions API responses (wrapped + escaped) by URL shape."""

    name = "fake-api"

    def __init__(self, allpages_batches, revisions):
        self.allpages_batches = allpages_batches   # list of (pages, apcontinue|None)
        self.revisions = revisions                 # {title: wikitext}
        self._idx = 0

    def fetch(self, url: str, *, headers=None) -> _ApiPage:
        if "list=allpages" in url:
            pages, cont = self.allpages_batches[self._idx]
            self._idx += 1
            obj = {"query": {"allpages": pages}}
            if cont:
                obj["continue"] = {"apcontinue": cont, "continue": "-||"}
            return _ApiPage(_wrap_api_json(obj))
        if "prop=revisions" in url:
            import re as _re
            from urllib.parse import unquote
            m = _re.search(r"titles=([^&]+)", url)
            titles = unquote(m.group(1)).split("|") if m else []
            pages = {str(i): {"title": t, "revisions": [{"slots": {"main": {"*": self.revisions[t]}}}]}
                     for i, t in enumerate(titles) if t in self.revisions}
            return _ApiPage(_wrap_api_json({"query": {"pages": pages}}))
        return _ApiPage(_wrap_api_json({}))

    def close(self):
        pass


def test_api_json_unwraps_browser_escaped_json():
    assert _api_json(_wrap_api_json({"a": 1, "s": "x&y<z"})) == {"a": 1, "s": "x&y<z"}
    assert _api_json("<html><body>not json</body></html>") is None


def test_raw_wikitext_builds_record():
    entry = parse_feed  # noqa: F841 - ensure import kept
    from raglex.adapters.gdprhub import FeedEntry
    e = FeedEntry(page_title="ANSPDCP_(Romania)_-_Fine",
                  display_title="ANSPDCP (Romania) - Fine",
                  url="https://gdprhub.eu/index.php?title=ANSPDCP_(Romania)_-_Fine",
                  updated="", summary_html=RAW_WIKITEXT)
    r = build_record(e, "gdprhub")
    assert r is not None
    assert r.doc_type is DocType.DECISION
    assert r.court == "dpa-ro"
    assert r.extra.get("case_number") is None        # N/A dropped
    assert r.ecli is None                            # N/A not an ECLI
    assert "a fine of 1000 EUR" in r.text            # <pre> translation is the body
    assert any(x.dst_id == GDPR_CELEX for x in r.relations)
    assert any(x.dst_id == "32016L0680" for x in r.relations)  # LED mined


def test_api_discovery_paginates_and_batches_wikitext():
    batch1 = ([{"pageid": 1, "ns": 0, "title": "ANSPDCP (Romania) - Fine"},
               {"pageid": 2, "ns": 0, "title": "Not A Decision"}], "CONT")
    batch2 = ([{"pageid": 3, "ns": 0, "title": "AEPD (Spain) - PS-1"}], None)
    revisions = {
        "ANSPDCP (Romania) - Fine": RAW_WIKITEXT,
        "Not A Decision": "Just some project text, no infobox.",
        "AEPD (Spain) - PS-1": RAW_WIKITEXT.replace("Romania", "Spain").replace("ANSPDCP", "AEPD"),
    }
    ad = GDPRhubAdapter(fetcher=_ApiFetcher([batch1, batch2], revisions), api=True)
    stubs = list(ad.discover(None))
    # every allpages title becomes a stub carrying its wikitext
    assert {s.stable_id for s in stubs} == {
        "gdprhub/anspdcp-romania-fine", "gdprhub/not-a-decision", "gdprhub/aepd-spain-ps-1"}
    assert all(s.hints.get("wikitext") for s in stubs)
    # fetch builds from the cached wikitext; the non-decision page yields no record
    recs = [ad.fetch(s) for s in stubs]
    built = [r for r in recs if r is not None]
    assert {r.stable_id for r in built} == {"gdprhub/anspdcp-romania-fine", "gdprhub/aepd-spain-ps-1"}


@pytest.mark.skipif(
    not os.path.exists("raglex design docs/feed.xml"),
    reason="sample feed not present",
)
def test_real_sample_feed_all_records_build():
    with open("raglex design docs/feed.xml", "rb") as fh:
        entries = parse_feed(fh.read())
    assert len(entries) >= 50
    for e in entries:
        r = build_record(e, "gdprhub")
        if r is None:            # a NewPages entry that is not a case report
            continue
        assert r.stable_id.startswith("gdprhub/")
        assert r.text  # translation or summary, never empty
        assert r.court and (r.court.startswith("dpa-") or r.court.startswith("court-"))


def test_non_report_page_is_dropped():
    entry = _feed(
        """<entry>
<id>https://gdprhub.eu/index.php?title=Template:Something</id>
<title>Template:Something</title>
<link href="https://gdprhub.eu/index.php?title=Template:Something"/>
<updated>2026-07-01T00:00:00Z</updated>
<summary type="html">&lt;p&gt;Just a template page, no infobox.&lt;/p&gt;</summary>
</entry>"""
    )
    (e,) = parse_feed(entry)
    assert build_record(e, "gdprhub") is None


def test_case_number_label_prefix_stripped():
    raw = DPA_ENTRY.replace(
        "|Case_Number_Name=NAIH-11443-3/2026",
        "|Case_Number_Name=Case number: 97/2026",
    )
    (e,) = parse_feed(_feed(raw))
    r = build_record(e, "gdprhub")
    assert r.extra["case_number"] == "97/2026"
    assert "97/2026" in r.extra["aliases"]
