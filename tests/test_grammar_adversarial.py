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
    # the longest comma'd short titles must survive the bounded title-token run
    ("under the Local Government, Economic Development and Construction Act 2009 the",
     "Local Government, Economic Development and Construction Act 2009"),
    ("A New Tax System (Goods and Services Tax) Act 1999 (Cth) provides",
     "A New Tax System (Goods and Services Tax) Act 1999"),
    # multi-applicant ECtHR name must survive the bounded name run
    ("Von Hannover and Others v Germany (2012) 55 EHRR 15 applied", "55 EHRR 15"),
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
    # multi-paragraph runs are preserved in full ("para 32 and 33"); anchor
    # matching jumps to the first number
    assert ecli and ecli[0].pinpoint == "para 32 and 33"
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


# -- pathological inputs must terminate fast (GIL-pinning outage tripwires) ---

def test_tabular_capitalised_run_terminates_fast():
    """fca/2016/1034 (2026-07 outage): annexure tables of capitalised names — no
    "Act <year>" / " v " terminator anywhere — sent the unbounded title/name token
    runs superlinear, pinning the GIL (and the whole API) for hours. The bounded
    runs must chew through the same shape in linear time; the generous budget only
    exists to keep slow CI green while still catching a quadratic regression."""
    import time

    rows = "".join(
        f"{i:3d}   {first} {last:12s}          {award}\n"
        for i, (first, last, award) in enumerate(
            (("Tammy", f"May{i}", "Cleaning Services Award") if i % 3 == 0 else
             ("Christine", f"Meager{i}", "Clerks Modern Award") if i % 3 == 1 else
             ("Paul", f"Saint James{i}", "Award Free"))
            for i in range(800)))
    assert "Act 19" not in rows and "Act 20" not in rows
    t0 = time.perf_counter()
    cites = extract_citations(rows)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"pathological table took {elapsed:.1f}s — backtracking regression"
    # and over-inclusion check: no statute/case matches exist in the table
    assert not [c for c in cites if c.entity_kind == "act"], cites[:5]


# -- the unconsumed-cue scanner finds seeded misses ---------------------------

def test_scanner_flags_uncovered_citation_residue():
    text = ("The tribunal considered Smith v Jones [1998] 2 WLR 448 at length. "
            "It also discussed ECLI:EU:C:2020:559 and a bare Re Application 12/34.")
    cites = extract_citations(text)
    spans = [(c.char_start, c.char_end) for c in cites]
    audit = scan_unconsumed("doc/x", text, spans)
    # the extracted UK/EU forms count as covered, not missed
    assert not any(u.cue in ("ecli", "report_cite") for u in audit.unconsumed), \
        [(u.cue, u.text) for u in audit.unconsumed]


def test_us_citations_are_now_covered_not_flagged_as_residue():
    # US reporter citations used to be unhandled residue; the reporter matcher now recognises
    # them, so a US authority is a covered case, not an unconsumed miss
    text = "It also discussed 425 U.S. 748 and 519 U.S. 452 (1997) in passing."
    cites = extract_citations(text)
    us = [c for c in cites if c.method == "us_reporter"]
    assert {c.candidate_id for c in us} == {"us/us/425/748", "us/us/519/452"}
    spans = [(c.char_start, c.char_end) for c in cites]
    audit = scan_unconsumed("doc/x", text, spans)
    assert not any(u.cue == "us_style" for u in audit.unconsumed), \
        [(u.cue, u.text) for u in audit.unconsumed]


def test_scanner_clean_when_everything_extracted():
    text = "see ECLI:EU:C:2020:559, paragraph 168"
    cites = extract_citations(text)
    audit = scan_unconsumed("doc/y", text, [(c.char_start, c.char_end) for c in cites])
    assert not [u for u in audit.unconsumed if u.cue == "ecli"]


# -- Perreault v Canada regressions (flat-text CA corpus feedback round) ------

def test_multi_paragraph_pincite_list():
    cites = extract_citations(
        "(Lukács v Canada (Public Safety and Emergency Preparedness), 2020 FC 1142 at paras 8, 44).")
    fc = [c for c in cites if c.candidate_id == "fc/2020/1142"]
    assert fc and fc[0].pinpoint == "para 8, 44"


def test_canlii_interjection_does_not_block_pinpoint():
    cites = extract_citations(
        "Canada (Information Commissioner) v Canada, 2019 FC 1279 (CanLII) at para 40 [Public Safety].")
    fc = [c for c in cites if c.candidate_id == "fc/2019/1279"]
    assert fc and fc[0].pinpoint == "para 40"


def test_shorthand_two_criteria_linking():
    text = ("(Suncor Energy Inc v Canada, 2021 FC 138 at para 64 [Suncor]). Later in the "
            "judgment: as held in Suncor at paras 30–31, the review is de novo. "
            "[Emphasis added.] But Emphasis at nothing links, and a bare Suncor mention "
            "without a pincite does not either.")
    sh = [c for c in extract_citations(text) if c.method == "shorthand"]
    assert len(sh) == 1
    assert sh[0].candidate_id == "fc/2021/138"
    assert sh[0].pinpoint == "para 30–31"


def test_synthesised_segments_via_body(catalogue, tmp_path):
    """Flat-text Canadian judgment → [N] paragraphs become real segments (with
    the quote guard), so pinpoint anchors land in the reader/peek."""
    from datetime import date

    from raglex.core.models import DocType, Record
    from raglex.storage import TextStore

    ts = TextStore(tmp_path / "text")
    text = ("PERREAULT v CANADA\n[1] Intro.\n[2] More.\n[3] Quoting Dagg:\n"
            "[107] Section 49 directs the court.\n[4] Following Dagg, we hold.\n")
    rec = Record(source="ca-caselaw", stable_id="ca-case/fc/abc", doc_type=DocType.JUDGMENT,
                 title="Perreault v Canada", court="fc", decision_date=date(2024, 1, 1),
                 language="en", text=text, raw_bytes=text.encode())
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))

    from raglex.config import Config
    # go through the facade path that the reader hits
    import raglex.facade as fmod
    from raglex.core.segmentation import synthesise_numbered_segments
    segs = synthesise_numbered_segments(text)
    labels = [s.label for s in segs if s.kind == "paragraph"]
    assert labels == ["[1]", "[2]", "[3]", "[4]"]
    assert "[107]" not in labels


def test_cjeu_judgment_in_shortname_coref():
    # The CJEU/AG-opinion idiom (flagged for refinement in the corpus): a case is
    # introduced with a full citation and a "judgment in <Name>" label, then later
    # references are "Judgment in <Name>, paragraph N" — which must resolve locally
    # to the same case, carrying the pincite.
    text = ("The judgment of 8 April 2014, Digital Rights Ireland and Others, Cases "
            "C-293/12 and C-594/12, judgment in Digital Rights, EU:C:2014:238, "
            "concerned data retention. ... As the Court held, Judgment in Digital "
            "Rights, paragraph 57, the interference was serious. See also Judgment "
            "in Digital Rights, paragraph 65.")
    sh = [c for c in extract_citations(text) if c.method == "shorthand"]
    assert len(sh) == 2
    # the label sits between the case-number and the ECLI, both of which identify the
    # same case (62012CJ0293 resolves to ECLI:EU:C:2014:238 via the CELEX→ECLI alias)
    assert {c.candidate_id for c in sh} in ({"62012CJ0293"}, {"ECLI:EU:C:2014:238"})
    assert {c.pinpoint for c in sh} == {"para 57", "para 65"}


def test_cjeu_joined_case_long_intro_still_defines_shortname():
    text = ("Cases C-203/15 and C-698/15, the judgment in Tele2 Sverige and Watson, "
            "EU:C:2016:970. Later: Judgment in Tele2 Sverige and Watson, paragraph 105.")
    sh = [c for c in extract_citations(text) if c.method == "shorthand"]
    assert len(sh) == 1
    assert sh[0].candidate_id in {"62015CJ0203", "62015CJ0698", "ECLI:EU:C:2016:970"}
    assert sh[0].pinpoint == "para 105"
