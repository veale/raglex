"""House of Lords scraping (§5a) + reporter-only citation matching (§5b).

The parsers are validated against the documented publications.parliament.uk structure;
the matcher against synthetic cases. Live fetching goes through the stealth tier and isn't
exercised here (bot-gated) — the pure logic is what must be right.
"""

from __future__ import annotations

import pytest

from raglex.adapters.hol import HolCase, parse_case_page, parse_index
from raglex.citations.report_match import (
    extract_preceding_name,
    match_report,
    score_candidate,
    surnames,
)

INDEX = """
<div id="maincontent1"><table id="AutoNumber1">
<tr><td><a name="2009"></a><h2>2009</h2></td></tr>
<tr><td><b>Title</b></td><td><b>Number</b></td><td><b>Date of Judgment</b></td></tr>
<tr><td><p><font><a href="/pa/ld200809/ldjudgmt/jd090617/assom.htm">AS (Somalia) (FC) v Secretary of State</a></font></p></td>
    <td><font>[2009] UKHL 32</font></td><td><font>17 June 2009</font></td></tr>
<tr><td><p><a href="../ld200809/ldjudgmt/jd090128/austin-1.htm">Austin (FC) v Commissioner of Police of the Metropolis</a></p></td>
    <td><font>[2009] UKHL 5</font></td><td><font>28 January 2009</font></td></tr>
<tr><td><a name="1993"></a><h2>1993</h2></td></tr>
<tr><td><p><a href="/pa/ld199293/ldjudgmt/jd930127/pepper.htm">Pepper (Inspector of Taxes) v Hart</a></p></td>
    <td><font></font></td><td><font>26 November 1992</font></td></tr>
</table></div>
"""


def test_parse_index_keys_neutral_and_surrogate():
    cases = {c.stable_id: c for c in parse_index(INDEX)}
    assert "ukhl/2009/32" in cases and "ukhl/2009/5" in cases
    # a pre-neutral-citation case (blank Number) gets a hol/ surrogate + a year from the date
    pepper = cases["hol/ld199293/pepper"]
    assert pepper.citation is None and pepper.year == 1992
    # links are urljoin-ed regardless of root- vs parent-relative style
    assert cases["ukhl/2009/5"].url.endswith("/ld200809/ldjudgmt/jd090128/austin-1.htm")


def test_parse_case_page_walks_and_cleans():
    p1 = ('<div id="maincontent"><table><tr><td>'
          '<p class="bigcov">OPINIONS</p><p class="para">1. First.</p>'
          '<p class="hditl">The facts</p><p class="para">2. Second.</p></td></tr></table>'
          '<table width="90%"><tr><td><a href="austin-2.htm">Continue</a></td></tr></table></div>')
    paras, cont = parse_case_page(p1)
    assert paras == ["1. First.", "The facts", "2. Second."]  # cover junk dropped
    assert cont == "austin-2.htm"

    last = ('<div id="maincontent"><table><tr><td><p class="para">3. Last.</p></td></tr></table>'
            '<table><tr><td><a href="austin-2.htm">Previous</a>'
            '<p>© Parliamentary copyright 2009</p></td></tr></table></div>')
    paras, cont = parse_case_page(last)
    assert paras == ["3. Last."] and cont is None  # footer + Previous dropped, no Continue

    soft = '<html class="pg-main-error-page"><h1>Page cannot be found</h1></html>'
    assert parse_case_page(soft) == ([], None)


# -- the matcher --------------------------------------------------------------

@pytest.mark.parametrize("ctx,name", [
    ("as held in Pepper v Hart ", "Pepper v Hart"),
    ("applying Austin v Commissioner of Police of the Metropolis ", "Austin v Commissioner of Police of the Metropolis"),
    ("in Smith and another v Jones and others ", "Smith and another v Jones and others"),
    ("under the Act ", None),
])
def test_extract_preceding_name(ctx, name):
    assert extract_preceding_name(ctx) == name


def test_surnames_strips_roles():
    got = surnames("Austin (FC) (Appellant) v Commissioner of Police of the Metropolis")
    # keeps distinctive tokens (the appellant's surname), drops party-role/procedural words
    assert "austin" in got
    assert "appellant" not in got and "commissioner" not in got and "fc" not in got


class _Case:
    def __init__(self, sid, title, year, opening=None):
        self.stable_id, self.title, self.year, self.opening = sid, title, year, opening


POOL = [
    _Case("hol/ld199293/pepper", "Pepper (Inspector of Taxes) v Hart", 1992),
    _Case("ukhl/2009/5", "Austin v Commissioner of Police of the Metropolis", 2009),
    _Case("hol/x/smith", "Smith v Jones", 1998),
]


@pytest.mark.parametrize("raw,name,expected", [
    ("[1993] AC 593", "Pepper v Hart", "hol/ld199293/pepper"),       # report lags judgment by a year
    ("[2009] 1 AC 564", "Austin v Commissioner of Police", "ukhl/2009/5"),
    ("[1998] AC 1", "Smith v Jones", "hol/x/smith"),
])
def test_match_report_hits(raw, name, expected):
    hit = match_report(raw, name, POOL, confirm_text=False)
    assert hit and hit[0] == expected


@pytest.mark.parametrize("raw,name", [
    ("[2005] 2 Cr App R 1", "Pepper v Hart"),   # wrong year window → no match
    ("[1993] 1 FooTribR 5", "Pepper v Hart"),   # a HoL case can't be in a made-up tribunal reporter
    ("[1993] AC 593", "Brown"),                 # single surname → too ambiguous
    ("[2004] EWCA Civ 1", "Pepper v Hart"),     # not a report citation at all
])
def test_match_report_refuses(raw, name):
    assert match_report(raw, name, POOL, confirm_text=False) is None


def test_match_report_refuses_ambiguous_pair():
    pool = [_Case("a/1", "Brown v Smith", 1998), _Case("a/2", "Brown v Smith", 1998)]
    assert match_report("[1998] AC 1", "Brown v Smith", pool, confirm_text=False) is None
