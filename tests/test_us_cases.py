"""US case-citation recognition (self-contained reporter matcher), gated to
American-looking text."""

from __future__ import annotations

from raglex.citations.us_cases import looks_american, us_case_citations, us_court_name


def test_us_court_names_from_courtlistener_slugs():
    # the seed set gets explicit names…
    assert us_court_name("scotus") == "Supreme Court of the United States"
    assert us_court_name("ca9") == "U.S. Court of Appeals, Ninth Circuit"
    assert us_court_name("cadc") == "U.S. Court of Appeals, D.C. Circuit"
    assert us_court_name("cafc") == "U.S. Court of Appeals, Federal Circuit"
    # …and the district courts are derived (state first: ca+n+d = N.D. Cal.)
    assert us_court_name("cand") == "U.S. District Court, N.D. Cal."
    assert us_court_name("nysd") == "U.S. District Court, S.D. N.Y."
    assert us_court_name("mdd") == "U.S. District Court, D. Md."
    # an unmappable slug stays None, so the caller keeps its own fallback (never invents one)
    assert us_court_name("nonesuch") is None
    assert us_court_name(None) is None


def test_gate_matches_us_reporters_only():
    assert looks_american("135 S. Ct. 2401 (2015)")
    assert looks_american("held at 325 U.S. 410")
    assert looks_american("519 U.S. 452")
    assert looks_american("347 F.3d 1200")
    assert looks_american("98 L. Ed. 2d 720")
    # not American: UK/EU/Commonwealth forms must not trip the gate
    assert not looks_american("[1998] 2 WLR 448")
    assert not looks_american("Article 17 of Regulation (EU) 2016/679")
    assert not looks_american("100 D.L.R. (4th) 658")      # Canadian report series
    assert not looks_american("section 12 of the Data Protection Act 2018")


def test_us_case_citations_extract_candidates():
    cs = us_case_citations("In Kimble, 135 S. Ct. 2401 (2015); Auer v. Robbins, 519 U.S. 452.")
    cands = {c.candidate_id for c in cs}
    assert "us/sct/135/2401" in cands
    assert "us/us/519/452" in cands
    assert all(c.entity_kind == "case" and c.method == "us_reporter" for c in cs)


def test_non_american_text_yields_nothing():
    # the gate short-circuits on non-US text
    assert us_case_citations("a plain sentence about section 5 of an Act") == []


def test_parallel_reporters_stay_distinct_nodes():
    # the same case cited in two reporters → two candidates; the ", 117" that opens
    # the parallel citation must NOT be swallowed as a pin page of the first
    cs = us_case_citations("Auer v. Robbins, 519 U.S. 452, 117 S. Ct. 905 (1997)")
    cands = {c.candidate_id for c in cs}
    assert "us/us/519/452" in cands and "us/sct/117/905" in cands


def test_federal_and_regional_reporters():
    cands = {c.candidate_id for c in us_case_citations(
        "347 F.3d 1200; 550 F. Supp. 2d 100; 12 A.3d 45; 200 P.3d 9")}
    assert {"us/f3d/347/1200", "us/fsupp2d/550/100", "us/a3d/12/45", "us/p3d/200/9"} <= cands
