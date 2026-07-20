"""US case-citation recognition (eyecite), gated to American-looking text."""

from __future__ import annotations

from raglex.citations.us_cases import looks_american, us_case_citations


def test_gate_matches_us_reporters_only():
    assert looks_american("135 S. Ct. 2401 (2015)")
    assert looks_american("held at 325 U.S. 410")
    assert looks_american("519 U.S. 452")
    assert looks_american("347 F.3d 1200")
    assert looks_american("98 L. Ed. 2d 720")
    # not American: UK/EU/Commonwealth forms must not trip the gate (so eyecite
    # never runs on them)
    assert not looks_american("[1998] 2 WLR 448")
    assert not looks_american("Article 17 of Regulation (EU) 2016/679")
    assert not looks_american("100 D.L.R. (4th) 658")      # Canadian report series
    assert not looks_american("section 12 of the Data Protection Act 2018")


def test_us_case_citations_extract_candidate_and_pincite():
    cs = us_case_citations("In Kimble, 135 S. Ct. 2401, 2410 (2015); Auer v. Robbins, 519 U.S. 452.")
    by = {c.candidate_id: c for c in cs}
    assert by["us/sct/135/2401"].pinpoint == "p. 2410"
    assert by["us/sct/135/2401"].entity_kind == "case"
    assert "us/us/519/452" in by
    # the raw span is the citation itself
    assert by["us/us/519/452"].raw == "519 U.S. 452"


def test_non_american_text_yields_nothing_without_invoking_eyecite():
    # the gate short-circuits before eyecite is imported/run
    assert us_case_citations("a plain sentence about section 5 of an Act") == []


def test_parallel_reporters_stay_distinct_nodes():
    # the same case cited in two reporters → two candidates (the corpus's usual
    # treatment of report citations; a later parallel-cite pass can merge them)
    cs = us_case_citations("Auer v. Robbins, 519 U.S. 452, 117 S. Ct. 905 (1997)")
    cands = {c.candidate_id for c in cs}
    assert "us/us/519/452" in cands and "us/sct/117/905" in cands
