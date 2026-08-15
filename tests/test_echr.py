from __future__ import annotations

import json
import re

from raglex.adapters.echr import ECHRAdapter, appno_from_ecli, parse_body_html
from raglex.citations import extract_citations
from raglex.citations.snowball import _classify

_RESULTS = json.dumps({"resultcount": 2, "results": [
    {"columns": {"itemid": "001-99999", "docname": "[legal summary] X v Y", "doctype": "CLIN", "ecli": ""}},
    {"columns": {"itemid": "001-210077", "ecli": "ECLI:CE:ECHR:2021:0525JUD005817013",
                 "appno": "58170/13;62322/14", "docname": "CASE OF BIG BROTHER WATCH v. UK",
                 "doctype": "HEJUD", "judgementdate": "25/05/2021 00:00:00", "languageisocode": "ENG"}},
]}).encode()
_HTML = (b"<html><body><p>I. PROCEDURE</p><p>1. The case originated in an application.</p>"
         b"<p>THE FACTS</p><p>2. The applicants are journalists.</p>"
         b"<p>THE LAW</p><p>3. The Court considers Article 8.</p>"
         b"<p>FOR THESE REASONS, THE COURT</p><p>Holds that there has been a violation.</p></body></html>")


class _FakeClient:
    def get(self, url, **kw):
        class R:
            content = _HTML if "conversion" in url else _RESULTS
        return R()


def test_appno_from_ecli():
    assert appno_from_ecli("ECLI:CE:ECHR:2021:0525JUD005817013") == "58170/13"
    assert appno_from_ecli("ECLI:CE:ECHR:1975:0221JUD000445170") == "4451/70"


def test_echr_adapter_resolves_by_ecli_and_appno_to_full_judgment():
    for ident in ("58170/13", "ECLI:CE:ECHR:2021:0525JUD005817013", "001-210077"):
        ad = ECHRAdapter(ids=ident, client=_FakeClient())
        stub = next(iter(ad.discover(None)))
        assert stub.stable_id == "ECLI:CE:ECHR:2021:0525JUD005817013"  # the judgment, not the summary
        rec = ad.fetch(stub)
        labels = {s.label for s in rec.segments}
        assert rec.court == "echr" and "violation" in rec.text
        # segmented on the numbered paragraphs (the § citable units) + operative part
        assert "1" in labels and "Operative part" in labels
        assert any(s.kind == "paragraph" for s in rec.segments)
        assert rec.extra["appno"].startswith("58170/13")


def test_hudoc_spaced_markers_form_one_complete_main_judgment_run():
    """HUDOC emits ``12 .`` as well as ``12.`` and appends opinions restarting at 1.

    Quoted numbered lists and the opinion must remain within their host text: only the
    Court's principal, longest consecutive run supplies the citable ``§`` anchors.
    """
    html = """<html><body>
      <p>PROCEDURE</p>
      <p>1. First.</p><p>2 . Second.</p>
      <p>Quoted rules:</p><p>1. Not judgment paragraph one.</p>
      <p>3. Third.</p><p>4 . Fourth.</p><p>5. Fifth.</p>
      <p>FOR THESE REASONS, THE COURT</p><p>Holds unanimously.</p>
      <p>SEPARATE OPINION</p><p>1. Opinion one.</p><p>2. Opinion two.</p>
    </body></html>"""
    text, segments = parse_body_html(html)
    paragraphs = [segment for segment in segments if segment.kind == "paragraph"]
    assert [segment.label for segment in paragraphs] == ["1", "2", "3", "4", "5"]
    assert text[paragraphs[1].char_start:paragraphs[1].char_end].startswith("Second.")
    assert "Not judgment paragraph one" in text[paragraphs[1].char_start:paragraphs[1].char_end]


def test_hudoc_chooses_long_judgment_after_short_numbered_front_matter():
    html = """<body>
      <p>Contents</p><p>1. Procedure</p><p>2. Facts</p>
      <p>THE JUDGMENT</p>
      <p>1 . Merits one.</p><p>2 . Merits two.</p><p>3 . Merits three.</p>
      <p>4 . Merits four.</p><p>5 . Merits five.</p><p>6 . Merits six.</p>
    </body>"""
    text, segments = parse_body_html(html)
    paragraphs = [segment for segment in segments if segment.kind == "paragraph"]
    assert [segment.label for segment in paragraphs] == [str(n) for n in range(1, 7)]
    assert text[paragraphs[0].char_start:paragraphs[0].char_end].startswith("Merits one.")


def test_hudoc_format_is_registered_for_held_corpus_reparses():
    from raglex.formats import available, parse

    assert "hudoc-html" in available()
    parsed = parse("hudoc-html", b"<body><p>1 . One.</p><p>2. Two.</p><p>3 . Three.</p></body>")
    assert [segment.label for segment in parsed.segments if segment.kind == "paragraph"] == [
        "1", "2", "3"]


def test_echr_grammars_and_routing():
    # application number (the resolvable key) routes to the HUDOC adapter
    appno = next(c for c in extract_citations("Handyside v United Kingdom, Application no. 5493/72")
                 if c.method == "echr_appno")
    assert appno.candidate_id == "5493/72"
    assert _classify("5493/72", "case") == ("ECHR application no.", "CoE", "echr")
    # an ECHR ECLI routes to the adapter too
    assert _classify("ECLI:CE:ECHR:1975:0221JUD000445170", "case")[2] == "echr"
    # EHRR cited WITH a case name → captured as an "echr:<name>" candidate, routed to the
    # echr adapter for a HUDOC docname (name) search; classified as a by-name ECHR case.
    named = next(c for c in extract_citations("Osman v United Kingdom (2000) 29 EHRR 245")
                 if c.method == "echr_report")
    assert named.candidate_id == "echr:Osman v United Kingdom"
    assert _classify(named.candidate_id, "case") == ("ECHR case (by name)", "CoE", "echr")
    # EHRR with NO recoverable name stays a candidate-less "maybe"
    assert all(c.candidate_id is None for c in extract_citations("see (2010) 51 EHRR 10 alone"))
    # § paragraph pinpoint attaches to the app-number citation
    pin = next(c for c in extract_citations("Application no. 4451/70, § 35") if c.candidate_id == "4451/70")
    assert pin.pinpoint == "para 35"


def test_echr_appno_resilient_to_surface_forms_and_traps():
    def app(text):
        return [c.candidate_id for c in extract_citations(text) if c.method == "echr_appno"]
    # OSCOLA "App no" (no full stop), Bluebook "App. No.", short number, joined set, [GC]/(dec.)
    assert app("App no 47940/99 (ECtHR, 20 July 2004)") == ["47940/99"]
    assert app("App. No. 60561/14") == ["60561/14"]
    assert app("D.D. v France (striking out), no. 3/02") == ["3/02"]
    assert app("nos. 16064/90 and 2 others") == ["16064/90"]   # first of a joined set
    assert app("(dec.) [GC], no. 36022/97") == ["36022/97"]
    # MUST NOT grab EU instruments ("No 1/2003", "No 17/62") or Series A numbers
    assert app("Regulation No 1/2003") == []
    assert app("Council Regulation No 17/62") == []
    assert app("Series A no. 139") == []


# -- HUDOC renders some judgments in one language only -------------------------

_TWO_LANGUAGES = json.dumps({"resultcount": 2, "results": [
    {"columns": {"itemid": "001-113656", "ecli": "ECLI:CE:ECHR:2012:1002JUD003321011",
                 "appno": "33210/11", "docname": "CASE OF SINGH AND OTHERS v. BELGIUM",
                 "doctype": "HEJUD", "languageisocode": "ENG"}},
    {"columns": {"itemid": "001-113660", "ecli": "ECLI:CE:ECHR:2012:1002JUD003321011",
                 "appno": "33210/11", "docname": "AFFAIRE SINGH ET AUTRES c. BELGIQUE",
                 "doctype": "HEJUD", "languageisocode": "FRE"}},
]}).encode()


class _EnglishConvertsToNothing:
    """HUDOC's docx conversion answers 204 No Content for the English text of Singh v.
    Belgium — permanently — while the French text of the same ECLI converts fine."""

    def __init__(self):
        self.fetched = []

    def get(self, url, **kw):
        class R:
            content = b""
        if "conversion" not in url:
            R.content = _TWO_LANGUAGES
        else:
            self.fetched.append(url)
            R.content = b"" if "001-113656" in url else _HTML
        return R()


def test_empty_conversion_falls_back_to_the_other_language_rendition():
    client = _EnglishConvertsToNothing()
    ad = ECHRAdapter(ids="ECLI:CE:ECHR:2012:1002JUD003321011", client=client)
    stub = next(iter(ad.discover(None)))
    assert stub.hints["itemid"] == "001-113656"          # the English judgment is preferred
    rec = ad.fetch(stub)
    assert rec is not None and "violation" in rec.text   # …but the French text is the fetch
    assert rec.language == "fr" and rec.source_language == "fr"
    assert len(client.fetched) == 2                      # tried English, then the sibling


def test_all_renditions_empty_is_recorded_as_absent_not_retried_for_ever():
    """Reversal of an earlier decision, on the evidence.

    This used to raise a TRANSIENT FetchError, reasoning that the conversion service
    comes back. In production it does not, for this class of record: HUDOC answers 204
    No Content for documents it holds only as metadata — 1980s Commission decisions have
    no full text and never will — and this module already records elsewhere that a 204 is
    PERMANENT (it is why the ``alt`` rendition walk exists at all). Calling it transient
    froze the cursor and re-tried the same unconvertible records on every run: 1,689
    warnings in three days, from one fingerprint.

    A genuine outage is still transient, and still re-raises — it arrives as a 5xx or a
    transport error from ``_body``, above. Reaching here means every rendition answered
    affirmatively with nothing.
    """
    class _NothingConverts(_EnglishConvertsToNothing):
        def get(self, url, **kw):
            class R:
                content = _TWO_LANGUAGES if "conversion" not in url else b""
            return R()

    ad = ECHRAdapter(ids="ECLI:CE:ECHR:2012:1002JUD003321011", client=_NothingConverts())
    stub = next(iter(ad.discover(None)))
    assert ad.fetch(stub) is None          # a genuine miss, filed as one


def test_a_transient_failure_on_a_rendition_still_re_raises():
    """The half that must not regress: an outage must never be filed as absence."""
    from raglex.core.errors import FetchError

    class _ServiceDown(_EnglishConvertsToNothing):
        def get(self, url, **kw):
            if "conversion" in url:
                raise FetchError("HTTP 503", transient=True)
            class R:
                content = _TWO_LANGUAGES
            return R()

    ad = ECHRAdapter(ids="ECLI:CE:ECHR:2012:1002JUD003321011", client=_ServiceDown())
    stub = next(iter(ad.discover(None)))
    try:
        ad.fetch(stub)
    except FetchError as exc:
        assert exc.transient
    else:
        raise AssertionError("a transient fetch failure must propagate, not return None")


class _FakeFeed:
    """A HUDOC feed of ``pages`` rows, honouring start/length and the 10,000 ceiling.

    Modelled on the real service: past ``start=10000`` it answers 200 OK with an EMPTY
    result set rather than an error, which is what makes an unguarded deep page look like
    the end of the corpus.
    """

    CEILING = 10000

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def get(self, url, **kw):
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(url).query)
        query = q["query"][0]
        self.queries.append(query)
        start, length = int(q["start"][0]), int(q["length"][0])
        rows = self.rows
        m = re.search(r"kpdate:\[\S+ TO (\d{4}-\d{2}-\d{2})T", query)
        if m:
            rows = [r for r in rows if r["columns"]["kpdate"][:10] <= m.group(1)]
        page = [] if start >= self.CEILING else rows[start:start + length]

        class R:
            content = json.dumps({"resultcount": len(rows), "results": page}).encode()
        return R()


def _feed_rows(n: int, *, start_day: int = 0):
    """``n`` cases, two renditions each (ENG + FRE), one case per day, newest first."""
    import datetime as dt

    rows = []
    for i in range(n):
        day = (dt.date(2026, 7, 23) - dt.timedelta(days=start_day + i)).isoformat()
        ecli = f"ECLI:CE:ECHR:2026:0723JUD{i:07d}13"
        for lang, name, doctype in (("FRE", f"AFFAIRE {i} c. FRANCE", "CHAMBER"),
                                    ("ENG", f"CASE OF {i} v. FRANCE", "CHAMBER")):
            rows.append({"columns": {
                "itemid": f"001-{i}{lang}", "ecli": ecli, "appno": f"{i}/13",
                "docname": name, "doctype": doctype, "languageisocode": lang,
                "kpdate": f"{day}T00:00:00", "judgementdate": None,
            }})
    return rows


def test_echr_feed_groups_renditions_and_stops_at_the_cursor():
    """One case, not one row per language — and the crawl breaks at the watermark.

    HUDOC publishes a judgment as several documents (English text, French text,
    translations) that share ONE ECLI. Yielding them as they arrive would store the same
    judgment twice under two ids and lose the fallback rendition that :meth:`fetch` needs
    when the English text will not convert.
    """
    feed = _FakeFeed(_feed_rows(6))
    ad = ECHRAdapter(client=feed)

    stubs = list(ad.discover(None, max_pages=1))
    assert len(stubs) == 6                                    # 12 rows, 6 cases
    assert len({s.stable_id for s in stubs}) == 6
    # the English judgment leads; the French rendition rides along as the fallback body
    assert stubs[0].title.startswith("CASE OF")
    assert [a["lang"] for a in stubs[0].hints["alt"]] == ["FRE"]
    assert stubs[0].hint_date.isoformat() == "2026-07-23"     # the cursor the pipeline stores

    # newest-first, so the first item older than the cursor ends the walk
    recent = list(ad.discover("2026-07-21", max_pages=1))
    assert [s.hint_date.isoformat() for s in recent] == [
        "2026-07-23", "2026-07-22", "2026-07-21"]


def test_echr_feed_walks_past_the_10000_row_ceiling():
    """HUDOC serves an empty page past start=10000 instead of an error.

    The Chamber/Grand Chamber series is ~71,000 documents, so a backfill that only pages
    ``start`` stops dead partway through and reports success. The crawl must re-anchor
    the query to a date window and start over.
    """
    feed = _FakeFeed(_feed_rows(7000))          # 14,000 rows: past the ceiling
    ad = ECHRAdapter(client=feed)

    stubs = list(ad.discover(None, max_pages=40))
    dates = [s.hint_date.isoformat() for s in stubs]

    assert len({s.stable_id for s in stubs}) == len(stubs)     # the re-anchor overlap dedupes
    assert all(dates[i] >= dates[i + 1] for i in range(len(dates) - 1))
    # got past where an unguarded ``start`` walk would have stopped
    assert len(stubs) > _FakeFeed.CEILING // 2
    assert any("kpdate:[" in q for q in feed.queries)


def test_closed_archives_do_not_raise_a_stale_source_alert():
    """"No new documents" is the CORRECT state for an archive that is closed.

    The Article 29 Working Party wound up in 2018 and the House of Lords stopped being a
    court in 2009; neither will ever yield again. Alerting "possible silent parser break"
    on them is a standing false alarm, and permanent noise is how a review queue stops
    being read. The registry already declares these modes, so the alert reads them from
    there rather than keeping a second list that can drift.
    """
    from raglex.ops.alerts import _never_yields_again

    assert _never_yields_again("a29wp") is True        # closed archive
    assert _never_yields_again("uk-hol") is True       # closed archive
    assert _never_yields_again("au-nsw") is True       # fetch-by-id only
    assert _never_yields_again("au-caselaw") is True   # local-file seed
    # a source with a live crawl must still be able to alert
    assert _never_yields_again("uk-caselaw") is False
    assert _never_yields_again("echr") is False        # now that it has a feed
    assert _never_yields_again("edpb-oss") is False
