"""Adversarial grammar corpus — tricky real-world citation forms, both failure
directions: forms that MUST be extracted (misses are silent, so each of these is
a tripwire) and non-citations that must NOT be (over-inclusion). Grown from
incidents: every grammar bug found in the wild should leave a snippet here.

Also exercises `citations/audit.py`'s unconsumed-cue scanner — the tool that
finds these gaps in the live corpus rather than in a test file."""

from __future__ import annotations

import pytest

from raglex.citations import extract_citations
from raglex.citations.audit import scan_unconsumed


def _kinds(text):
    return [(c.entity_kind, c.raw, c.pinpoint) for c in extract_citations(text)]


def _raws(text):
    return [c.raw for c in extract_citations(text)]


# -- forms that MUST be recognised (miss tripwires) --------------------------

MUST_MATCH = [
    # CJEU parenthetical with NBSP + spaced commas (the Formex projection form)
    ("judgment of 17\xa0June 2021 , M.I.C.M ., C‑597/19 , EU:C:2021:492 , paragraph 107",
     "EU:C:2021:492"),
    # joined cases
    ("Joined Cases C-92/09 and C-93/09 Volker und Markus Schecke", "C-92/09"),
    # procedure suffixes
    ("the order in C-619/18 R and the appeal C-11/26 P were heard", "C-619/18 R"),
    # UK neutral citation with division
    ("as held in [2011] EWCA Civ 31 and applied since", "[2011] EWCA Civ 31"),
    # law report form
    ("Smith v Jones [1998] 2 WLR 448 establishes the point", "[1998] 2 WLR 448"),
    # legislation with section, full form
    ("under section 45(2) of the Data Protection Act 2018 the controller must",
     "Data Protection Act 2018"),
    # EU instrument by number
    ("Regulation (EU) 2016/679 applies to processing", "2016/679"),
    # ECHR application number
    ("Golder v the United Kingdom, no. 4451/70, § 35", "4451/70"),
    # ECLI anywhere in prose
    ("see, to that effect, ECLI:EU:C:2020:559, paragraphs 168 and 177", "ECLI:EU:C:2020:559"),
]


@pytest.mark.parametrize("text,expected", MUST_MATCH, ids=[m[1] for m in MUST_MATCH])
def test_must_extract(text, expected):
    raws = _raws(text)
    assert any(expected.replace("‑", "-") in r.replace("‑", "-") for r in raws), \
        f"grammar missed {expected!r}; got {raws}"


# -- pinpoint correctness in dense contexts ----------------------------------

def test_pinpoint_ranges_attach_to_the_case():
    cites = extract_citations(
        "see, to that effect, judgment of 4 May 2023, Österreichische Post, "
        "C-300/21, EU:C:2023:370, paragraphs 32 and 33")
    ecli = [c for c in cites if c.method == "ecli"]
    assert ecli and ecli[0].pinpoint == "para 32"
    # and the range's paragraphs must NOT become carried-forward provision edges
    cf = [c for c in cites if c.method == "carry_forward" and c.raw.lower().startswith("para")]
    assert not cf, cf


def test_echr_section_pinpoint():
    cites = extract_citations("Golder v the United Kingdom, no. 4451/70, § 35, the Court held")
    appno = [c for c in cites if "4451/70" in c.raw]
    assert appno and appno[0].pinpoint == "para 35"


def test_consecutive_citations_keep_separate_pinpoints():
    cites = extract_citations(
        "(see C-131/12, EU:C:2014:317, paragraph 80, and C-311/18, EU:C:2020:559, paragraph 168)")
    pins = {c.raw: c.pinpoint for c in cites if c.method == "ecli"}
    assert pins.get("EU:C:2014:317") == "para 80"
    assert pins.get("EU:C:2020:559") == "para 168"


# -- forms that must NOT be extracted (over-inclusion tripwires) --------------

MUST_NOT_MATCH_AS_CASE = [
    # a date range is not a case number
    "during the period 2016/17 the authority processed",
    # a price is not a citation
    "the fee of £2,016.79 was paid",
    # Acts of the Scottish Parliament are legislation, not cases (live kind_mismatch)
    "the Public Finance and Accountability (Scotland) Act 2000 (2000 ASP 1)",
]


@pytest.mark.parametrize("text", MUST_NOT_MATCH_AS_CASE)
def test_must_not_extract_as_case(text):
    kinds = [(c.entity_kind, c.raw) for c in extract_citations(text)
             if c.entity_kind in ("case", "opinion")]
    assert not kinds, f"over-inclusion: {kinds}"


def test_self_paragraph_reference_not_carried():
    # "[43] above" is an internal reference, not a citation of anything
    cites = extract_citations(
        "For the reasons at [43] above, and applying the Freedom of Information Act 2000, "
        "the notice stands.")
    para_cf = [c for c in cites if c.method == "carry_forward" and "43" in c.raw]
    # [43] alone must not be pinned to the Act as 'para 43'
    assert not para_cf, para_cf


# -- the unconsumed-cue scanner finds seeded misses ---------------------------

def test_scanner_flags_uncovered_citation_residue():
    text = ("The tribunal considered Smith v Jones [1998] 2 WLR 448 at length. "
            "It also discussed ECLI:EU:C:2020:559 and 425 U.S. 748 in passing.")
    cites = extract_citations(text)
    spans = [(c.char_start, c.char_end) for c in cites]
    audit = scan_unconsumed("doc/x", text, spans)
    # the US-style citation has no grammar — the scanner must surface it
    assert any(u.cue == "us_style" for u in audit.unconsumed), \
        [(u.cue, u.text) for u in audit.unconsumed]
    # while the extracted UK/EU forms count as covered, not missed
    assert not any(u.cue in ("ecli", "report_cite") for u in audit.unconsumed), \
        [(u.cue, u.text) for u in audit.unconsumed]


def test_scanner_clean_when_everything_extracted():
    text = "see ECLI:EU:C:2020:559, paragraph 168"
    cites = extract_citations(text)
    audit = scan_unconsumed("doc/y", text, [(c.char_start, c.char_end) for c in cites])
    assert not [u for u in audit.unconsumed if u.cue == "ecli"]
