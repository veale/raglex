"""US case-citation recognition (self-contained reporter matcher), gated to
American-looking text."""

from __future__ import annotations

from raglex.citations.us_cases import looks_american, us_case_citations, us_court_name
from raglex.citations.stage import _allows_us_reporters


def test_year_as_volume_is_not_a_us_citation():
    # English "[1958] P 561" (Probate) must never be read as US "1958 P. 561": no US
    # reporter reaches volume 1500, so a year-in-the-volume-slot is a misparse that would
    # burn the CourtListener budget on a guaranteed 404.
    from raglex.citations.us_cases import plausible_us_volume, us_case_citations

    assert plausible_us_volume("325") and not plausible_us_volume("1958")
    assert us_case_citations("1958 P. 561") == []           # year volume → rejected
    assert us_case_citations("325 U.S. 410") and us_case_citations("561 P.2d 1234")


def test_misparsed_us_year_volume_is_not_routable():
    # …and if such a candidate is already in the graph (minted by an older extractor), it
    # must not be routed to the us-caselaw adapter.
    from raglex.citations.snowball import _classify

    assert _classify("us/p/1958/561", "case")[2] is None          # no adapter
    assert _classify("us/us/325/410", "case")[2] == "us-caselaw"  # a real one still routes


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


def test_document_scope_limits_us_reporters_to_us_and_common_law_cases():
    assert _allows_us_reporters({"source": "us-caselaw", "doc_type": "judgment"})
    assert _allows_us_reporters({"source": "uk-caselaw", "doc_type": "judgment"})
    assert _allows_us_reporters({"source": "ca-caselaw", "doc_type": "decision"})
    assert not _allows_us_reporters({"source": "eu-cellar", "doc_type": "judgment"})
    assert not _allows_us_reporters({"source": "fr-dila", "doc_type": "judgment"})
    assert not _allows_us_reporters({"source": "eu-preparatory", "doc_type": "preparatory"})
    assert not _allows_us_reporters({"source": "uk-legislation", "doc_type": "legislation"})


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
def test_official_journal_page_is_not_a_us_pacific_reporter():
    assert us_case_citations("published in OJ L 159 p. 1 and OJ L 143, p. 6") == []


# -- ambiguous reporters: never Pacific, and single letters only in US material ----

def test_bare_pacific_reporter_is_never_a_citation():
    # "X p. 100" is French for "X per cent" — the live worklist's entire US backlog was
    # phantom Pacific cases ("10 p. 100", "25 p. 100", "100 p. 100"), each routable and
    # each spending CourtListener quota on a case that does not exist.
    for phantom in ("une réduction de 10 p. 100 du montant",
                    "25 p. 100 des voix", "at 163 P. 1002", "2 p. 3"):
        assert us_case_citations(phantom) == [], phantom
    # the modern Pacific series is unambiguous and still recognised
    assert {c.candidate_id for c in us_case_citations("200 P.3d 9; 561 P.2d 1234")} == {
        "us/p3d/200/9", "us/p2d/561/1234"}


def test_bare_pacific_candidate_is_no_longer_routable():
    from raglex.citations.snowball import _classify

    # the edges an older extractor already wrote must not offer themselves for harvest
    assert _classify("us/p/10/100", "case")[2] is None
    assert _classify("us/p3d/200/9", "case")[2] == "us-caselaw"


def test_single_letter_series_are_flagged_ambiguous():
    from raglex.citations.us_cases import AMBIGUOUS_METHOD

    cs = us_case_citations("12 F. 13 and 4 A. 55 and 6 So. 7")
    assert {c.method for c in cs} == {AMBIGUOUS_METHOD}
    # …while the numbered series stay first-class
    assert all(c.method == "us_reporter"
               for c in us_case_citations("347 F.3d 1200; 12 A.3d 45"))


def test_ambiguous_reporters_survive_only_in_us_documents():
    from raglex.citations.stage import _is_us_source

    assert _is_us_source({"source": "us-caselaw", "doc_type": "judgment"})
    # a Canadian/UK judgment may cite US authority — but in F.2d/F.3d, not bare "F."
    assert not _is_us_source({"source": "ca-caselaw", "doc_type": "judgment"})
    assert not _is_us_source({"source": "uk-caselaw", "doc_type": "judgment"})
