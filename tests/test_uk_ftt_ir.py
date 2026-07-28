"""The Information Rights tribunal's own decisions register (``uk-ftt-ir``).

The register is a plain ASP.NET results table paged by ``?Page=N``, newest first, whose
rows point at decision PDFs. What the tests pin is the identity ladder — a decision that
prints a neutral citation must land on the SAME node as the Find Case Law copy pulled by
``uk-grc``, or the corpus holds the case twice — plus the appeal-number canonicalisation
those decisions are actually cited by, and the incremental stop at the watermark.
"""

from __future__ import annotations

from datetime import date

from raglex.adapters.uk_ftt_ir import (
    InformationRightsAdapter,
    appeal_aliases,
    appeal_id,
    appeal_number,
    parse_decision_pdf,
    parse_results,
    total_results,
)
from raglex.core.models import Stub
from raglex.extraction.extractors import Extracted

# Two rows in the register's own markup: one with an additional party, one with the
# jurisdiction-tagged appeal number (EA/2023/0042/GDPR) and a doubled area cell.
RESULTS_HTML = """
<table summary="Decisions" class="search-results-table percent100"><thead><tr>
<th>Jurisdictional area</th><th>Case Title and Reference</th><th>Date</th>
<th>Case Summary</th><th>Appeal</th></tr></thead><tbody>
<tr><td>Decisions from 1 April 2019<br>
  </td>
  <td><a id="Repeater1_ctl01_Hyperlink1" title="View full decision"
        href="../DBFiles/Decision/i3253/Centre%20(EA.2022.0317)%20-%20Dismissed.pdf"
        target="_blank">Centre for Animals and Social Justice  v Information Commissioner</a>
    <br><span class="bold">Additional Party</span>
    Department for Environment, Food and Rural Affairs
    <br><span class="purple">
      EA/2022/0317
    </span></td>
  <td>11/08/2023              </td>
  <td><a id="Repeater1_ctl01_Hyperlink2" href="../DBFiles/Summary/i3253/" target="_blank"></a></td>
  <td>Dismissed
    <a id="Repeater1_ctl01_Hyperlink3" href="../DBFiles/Appeal/i3253/" target="_blank"></a>
    <a id="Repeater1_ctl01_Hyperlink4" target="_blank"></a></td></tr>
<tr><td>Decisions from 1 April 2019<br>
  Decisions from 1 April 2019
  </td>
  <td><a id="Repeater1_ctl02_Hyperlink1" title="View full decision"
        href="../DBFiles/Decision/i3254/Johnson,%20Lee%20(EA.2023.0042.GDPR)%20Struck%20Out.pdf"
        target="_blank">Lee Johnson  v Information Commissioner</a>
    <br><span class="bold">Additional Party</span>
    <br><span class="purple">
      EA/2023/0042/GDPR
    </span></td>
  <td>07/08/2023              </td>
  <td><a id="Repeater1_ctl02_Hyperlink2" href="../DBFiles/Summary/i3254/" target="_blank"></a></td>
  <td>Struck Out
    <a id="Repeater1_ctl02_Hyperlink3" href="../DBFiles/Appeal/i3254/" target="_blank"></a></td></tr>
</tbody></table>
<div class="CollectionPager"><div>Displaying results 1 to 10 (of 3167)</div></div>
"""

# The face of a decision, as pypdf renders it.
DECISION_TEXT = """1

Case Reference: EA/2022/0273
First-tier Tribunal
General Regulatory Chamber
Information Rights

On the Papers

Heard on: 2 August 2023.

Decision given on: 7 August 2023.

Before:

Tribunal Judge:  Brian Kennedy KC
Tribunal Member: Paul Taylor and
Tribunal Member: Dave Sivers

Between:

STEVEN DOWNES
Appellant
and
THE INFORMATION COMMISSIONER
Respondent

Decision: The appeal is dismissed.
REASONS
[1] This decision relates to an appeal brought under section 57 of the Freedom of
Information Act 2000, against the Commissioner's decision notice with reference
number IC -130630-R7Y9 (the "DN").
[2] Full details of the background are set out in the DN.
[3] The appeal is dismissed.
"""


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.text = content.decode("utf-8", "replace")


class _FakeClient:
    """Serves the fixture page for every ``?Page=`` and records what was asked for."""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages, self.seen = pages, []

    def get(self, url: str, **_kw):
        self.seen.append(url)
        for key, body in self.pages.items():
            if key in url:
                return _FakeResponse(body)
        return _FakeResponse(self.pages.get("*", b""))


# -- appeal numbers ----------------------------------------------------------

def test_appeal_number_canonicalises_every_written_form():
    for written in ("EA/2022/0273", "EA.2022.0273", "ea-2022-0273", "(EA.2022.0273)"):
        assert appeal_number(written) == "EA/2022/0273"
    assert appeal_number("EA/2023/0042/GDPR") == "EA/2023/0042/GDPR"
    assert appeal_number("no reference here") is None
    assert appeal_id("EA.2022.0273") == "uk/ftt-ir/ea/2022/0273"
    assert appeal_id(None) is None
    # every form the corpus might cite resolves to the one document
    assert appeal_aliases("EA.2022.0273") == [
        "EA/2022/0273", "EA.2022.0273", "EA-2022-0273"]


# -- the results table -------------------------------------------------------

def test_parse_results_reads_every_field_of_a_row():
    rows = parse_results(RESULTS_HTML)
    assert len(rows) == 2 and total_results(RESULTS_HTML) == 3167
    first, second = rows
    assert first.appeal_number == "EA/2022/0317"
    assert first.stable_id == "uk/ftt-ir/ea/2022/0317"
    assert first.title == "Centre for Animals and Social Justice v Information Commissioner"
    assert first.additional_party == "Department for Environment, Food and Rural Affairs"
    assert first.decided == date(2023, 8, 11) and first.outcome == "Dismissed"
    assert first.file_id == "i3253"
    assert first.decision_url.endswith("-%20Dismissed.pdf")
    assert first.summary_url.endswith("/DBFiles/Summary/i3253/")
    assert first.appeal_url.endswith("/DBFiles/Appeal/i3253/")
    # the area cell repeats itself on most rows — one line, not two identical ones
    assert first.area == second.area == "Decisions from 1 April 2019"
    # a jurisdiction-tagged appeal number keeps its tag, and no additional party is None
    assert second.stable_id == "uk/ftt-ir/ea/2023/0042/gdpr"
    assert second.additional_party is None


def test_rows_without_a_decision_pdf_are_skipped():
    assert parse_results(
        '<table class="search-results-table"><tbody><tr>'
        "<td>Area</td><td>No document yet</td><td>01/01/2020</td>"
        "</tr></tbody></table>") == []


# -- the decision's own face -------------------------------------------------

def test_parse_decision_pdf_lifts_reference_panel_and_notice():
    meta = parse_decision_pdf(DECISION_TEXT)
    assert meta["appeal_number"] == "EA/2022/0273"
    assert meta["heard_on"] == date(2023, 8, 2)
    assert meta["decided_on"] == date(2023, 8, 7)
    # the panel line runs on ("Paul Taylor and") — the trailing conjunction is not a name
    assert meta["panel"] == ["Brian Kennedy KC", "Paul Taylor", "Dave Sivers"]
    # the join to the ICO side, however the PDF spaces it
    assert meta["decision_notice"] == "IC-130630-R7Y9"
    assert meta["neutral_citation"] is None


def test_neutral_citation_is_read_when_the_decision_carries_one():
    meta = parse_decision_pdf(
        "Neutral citation number: [2023] UKFTT 00123 (GRC)\nCase Reference: EA/2023/0111\n")
    assert meta["neutral_citation"] == "[2023] UKFTT 00123 (GRC)"


# -- discovery + the identity ladder ----------------------------------------

def _adapter(monkeypatch, *, text: str) -> InformationRightsAdapter:
    a = InformationRightsAdapter(client=_FakeClient({"*": RESULTS_HTML.encode()}))
    monkeypatch.setattr(
        "raglex.adapters.uk_ftt_ir.PdfExtractor.extract",
        lambda self, data, **kw: Extracted(text=text, engine="pypdf", engine_version="t"))
    return a


def test_discover_walks_pages_newest_first(monkeypatch):
    a = _adapter(monkeypatch, text=DECISION_TEXT)
    stubs = list(a.discover(None, max_pages=2))
    # the fixture serves the same two rows for every page; the URL dedupe keeps one set
    assert [s.stable_id for s in stubs] == [
        "uk/ftt-ir/ea/2022/0317", "uk/ftt-ir/ea/2023/0042/gdpr"]
    assert stubs[0].hint_date == date(2023, 8, 11)
    assert stubs[0].hints["watermark"] == "2023-08-11"
    assert stubs[0].court == "ukftt/grc"


def test_incremental_run_stops_at_the_watermark(monkeypatch):
    a = _adapter(monkeypatch, text=DECISION_TEXT)
    # a cursor at the newest row: nothing is new, and the walk stops on the first page
    assert list(a.discover("2023-08-11", max_pages=5)) == []
    assert len(a._client.seen) == 1
    # a cursor between the two rows yields only the newer one
    got = list(a.discover("2023-08-07", max_pages=5))
    assert [s.stable_id for s in got] == ["uk/ftt-ir/ea/2022/0317"]


def test_fetch_keys_by_appeal_number_and_carries_the_register_metadata(monkeypatch):
    a = _adapter(monkeypatch, text=DECISION_TEXT)
    stub = list(a.discover(None, max_pages=1))[0]
    rec = a.fetch(stub)
    assert rec.stable_id == "uk/ftt-ir/ea/2022/0317"   # the row's number wins for the id
    assert rec.doc_type.value == "judgment" and rec.court == "ukftt/grc"
    assert rec.decision_date == date(2023, 8, 7)       # the PDF's own date, not the row's
    assert rec.extra["ico_decision_notice"] == "IC-130630-R7Y9"
    assert rec.extra["outcome"] == "Dismissed"
    assert rec.extra["additional_party"].startswith("Department for Environment")
    assert rec.extra["aliases"] == ["EA/2022/0317", "EA.2022.0317", "EA-2022-0317"]
    assert [s.label for s in rec.segments if s.kind == "paragraph"] == ["[1]", "[2]", "[3]"]


def test_a_decision_with_a_neutral_citation_keys_to_the_find_case_law_slug(monkeypatch):
    # the overlap with uk-grc: same case, one node — otherwise the corpus holds it twice
    a = _adapter(monkeypatch, text="Neutral citation number: [2023] UKFTT 00123 (GRC)\n"
                                   + DECISION_TEXT)
    stub = list(a.discover(None, max_pages=1))[0]
    rec = a.fetch(stub)
    assert rec.stable_id == "ukftt/grc/2023/00123"
    assert rec.extra["neutral_citation"] == "[2023] UKFTT 00123 (GRC)"
    # …and the appeal number stays an alias, so citations by it still resolve
    assert "EA/2022/0317" in rec.extra["aliases"]


def test_an_unreadable_scan_is_dropped_rather_than_stored_empty(monkeypatch):
    a = _adapter(monkeypatch, text="   ")
    stub = Stub(stable_id="uk/ftt-ir/i1", raw_url="https://x/y.pdf",
                landing_url="https://x/y.pdf", hints={})
    assert a.fetch(stub) is None
