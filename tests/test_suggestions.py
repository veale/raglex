"""The human-confirmable "Possibly: …?" suggestion layer (§5b) — party-name extraction
from citing text, the near-miss suggesters, and the tick/cross decision flow."""

from __future__ import annotations

import pytest

from raglex.citations.extractor import extract_citations
from raglex.citations.report_match import extract_name_candidates


# -- ECtHR scoring: the respondent state must never carry a match -------------
# (real wrong suggestions from the 2026-07 live queue, each at score 1.0 before)

def test_echr_respondent_only_name_does_not_match():
    from raglex.citations.report_match import score_echr_candidate

    # "Er v Turkey": "er" is a short token, so the old scorer matched on {turkey}
    # alone and gave Z.N.S. v Turkey a perfect score
    assert score_echr_candidate("Er v Turkey", "Z.N.S. v. TURKEY", 2010, 2012) == 0.0
    # "HL v United Kingdom" matched any UK case in the window
    assert score_echr_candidate("HL v. United Kingdom",
                                "E. AND OTHERS v. THE UNITED KINGDOM", 2002, 2004) == 0.0


def test_echr_initials_match_at_subconfident_score():
    from raglex.citations.report_match import score_echr_candidate

    # correct initialised match: suggestion territory (0.45), never auto-alias (≥0.5)
    s = score_echr_candidate("In MC v Bulgaria", "M.C. v. BULGARIA", 2003, 2005)
    assert 0.3 <= s < 0.5
    # and the initial sequences must actually agree
    assert score_echr_candidate("Er v Turkey", "Z.N.S. v. TURKEY", 2010, 2010) == 0.0


def test_echr_full_name_match_and_respondent_disagreement():
    from raglex.citations.report_match import score_echr_candidate

    assert score_echr_candidate("Soering v United Kingdom",
                                "SOERING v. THE UNITED KINGDOM", 1989, 1989) >= 0.5
    # right applicant, wrong respondent state → no match
    assert score_echr_candidate("Soering v Germany",
                                "SOERING v. THE UNITED KINGDOM", 1989, 1989) == 0.0
    # year outside the reporting-lag window → no match
    assert score_echr_candidate("Soering v United Kingdom",
                                "SOERING v. THE UNITED KINGDOM", 1984, 1989) == 0.0


# -- multi-candidate party extraction -----------------------------------------

@pytest.mark.parametrize("ctx,expect_in", [
    # plain "A v B" right before the citation
    ("as held in Pepper v Hart ", "Pepper v Hart"),
    # prose swept into side A → the tightened candidate recovers the real name
    ("Simon had stated in Cricklewood Property and Investment Trust v Leightons Investment Trust ",
     "Cricklewood Property and Investment Trust v Leightons Investment Trust"),
    # a neutral citation between the name and the report → stripped, name recovered
    ("see R v May [2008] UKHL 28, ", "R v May"),
    ("of the Supreme Court in Pinnock [2010] UKSC 45, [2011] 2 AC 104 and Powell [2011] UKSC 8, ",
     "Powell"),
    # judicial-review form
    ("the merits ( R (Bourgass) v Secretary of State for Justice [2015] UKSC 54 , ",
     "R (Bourgass) v Secretary of State for Justice"),
    # Re form
    ("as explained In re Spectrum Plus Ltd ", "Re Spectrum Plus Ltd"),
])
def test_extract_name_candidates(ctx, expect_in):
    assert expect_in in extract_name_candidates(ctx)


def test_extract_name_candidates_none_on_plain_prose():
    assert extract_name_candidates("pursuant to the provisions of the schedule ") == []


# -- the (EEC)-parenthesised EU instrument is not an ECHR application number ---

def test_regulation_number_not_minted_as_echr_appno():
    cites = extract_citations("in accordance with Regulation (EEC) No 1408/71 of the Council")
    assert not any(c.candidate_id == "1408/71" for c in cites)


def test_genuine_appno_still_minted():
    cites = extract_citations("the applicant lodged Application no. 4451/70 with the Commission")
    assert any(c.candidate_id == "4451/70" for c in cites)


# -- suggestion engine + decision flow over a seeded corpus --------------------

@pytest.fixture
def seeded_facade(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGLEX_DATA_DIR", str(tmp_path))
    from raglex.config import Config
    from raglex.facade import Facade

    f = Facade(Config.from_env())
    with f._open() as (cat, _rs, _ts):
        cat.conn.execute(
            "INSERT INTO documents (stable_id, source, doc_type, title, decision_date, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            ("ukpga/1997/40", "uk-legislation", "legislation",
             "Protection from Harassment Act 1997", "1997-03-21", "2026-01-01"))
        cat.conn.execute(
            "INSERT INTO documents (stable_id, source, doc_type, title, decision_date, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            ("ewhc/qb/2001/12", "uk-caselaw", "judgment", "Citing Case v Somebody",
             "2001-05-01", "2026-01-01"))
        cat.conn.execute(
            "INSERT INTO relations (src_id, raw_citation_string, raw_fold, resolution_status, "
            "relationship_type, extracted_via) VALUES (?,?,?,?,?,?)",
            ("ewhc/qb/2001/12", "the Harassment Act 1997", "the harassment act 1997",
             "pending", "mentions", "regex"))
        cat.conn.commit()
    return f


def test_nested_title_suggestion_and_accept(seeded_facade):
    f = seeded_facade
    r = f.suggest_matches(report_limit=0)
    assert r["statute"] >= 1
    rows = f.unresolved_references(limit=10)
    row = next(x for x in rows if "harassment" in (x["raw"] or "").lower())
    sugg = [s for s in row["suggestions"] if s["suggested_id"] == "ukpga/1997/40"]
    assert sugg and sugg[0]["kind"] == "legislation-nested"
    # tick → alias + resolve: the pending edge goes live
    out = f.decide_suggestion(ref=sugg[0]["ref"], suggested_id="ukpga/1997/40", accept=True)
    assert out["resolved_edges"] >= 1
    assert not any("harassment" in (x["raw"] or "").lower()
                   for x in f.unresolved_references(limit=10))


def test_pending_suggestions_enriched(seeded_facade):
    """The review list carries target metadata, citing evidence and flags — here the
    'Harassment Act 1997' ref cited by a UK judgment matching UK legislation: evidence
    present, no jurisdiction flag."""
    f = seeded_facade
    f.suggest_matches(report_limit=0)
    out = f.list_pending_suggestions()
    s = next(x for x in out["suggestions"] if x["suggested_id"] == "ukpga/1997/40")
    assert s["target"]["title"] == "Protection from Harassment Act 1997"
    assert s["target"]["jurisdiction"] == "United Kingdom"
    assert s["occurrences"] == 1
    assert s["citing_jurisdictions"] == {"United Kingdom": 1}
    assert s["flags"] == []


def test_pending_suggestions_citing_jurisdiction_flag(seeded_facade):
    """Legislation cited (almost) only from another jurisdiction's documents gets the
    red citing-jurisdiction flag — the Irish 'Companies Act 1990' → UK act class."""
    f = seeded_facade
    with f._open() as (cat, _rs, _ts):
        for i in (1, 2):
            cat.conn.execute(
                "INSERT INTO documents (stable_id, source, doc_type, title, decision_date, fetched_at) "
                "VALUES (?,?,?,?,?,?)",
                (f"iehc/2001/{i}", "ie-caselaw", "judgment", f"Irish Case {i} v Somebody",
                 "2001-05-01", "2026-01-01"))
            cat.conn.execute(
                "INSERT INTO relations (src_id, raw_citation_string, raw_fold, resolution_status, "
                "relationship_type, extracted_via) VALUES (?,?,?,?,?,?)",
                (f"iehc/2001/{i}", "the Harassment Act 1997", "the harassment act 1997",
                 "pending", "mentions", "regex"))
        # the UK citing edge from the base fixture is outvoted 2:1 → majority Irish
        cat.conn.execute("DELETE FROM relations WHERE src_id = 'ewhc/qb/2001/12'")
        cat.conn.commit()
    f.suggest_matches(report_limit=0)
    out = f.list_pending_suggestions()
    s = next(x for x in out["suggestions"] if x["suggested_id"] == "ukpga/1997/40")
    assert s["citing_jurisdictions"] == {"Ireland": 2}
    assert any(fl["id"] == "citing-jurisdiction" and fl["level"] == "red" for fl in s["flags"])


def test_rejected_suggestion_never_resurfaces(seeded_facade):
    f = seeded_facade
    f.suggest_matches(report_limit=0)
    rows = f.unresolved_references(limit=10)
    row = next(x for x in rows if "harassment" in (x["raw"] or "").lower())
    s = row["suggestions"][0]
    f.decide_suggestion(ref=s["ref"], suggested_id=s["suggested_id"], accept=False)
    f.suggest_matches(report_limit=0)  # re-run must not re-ask
    rows = f.unresolved_references(limit=10)
    row = next(x for x in rows if "harassment" in (x["raw"] or "").lower())
    assert not any(x["suggested_id"] == s["suggested_id"] for x in row["suggestions"])


def test_refinement_flag_roundtrip(seeded_facade):
    f = seeded_facade
    f.flag_refinement(doc_id="ewhc/qb/2001/12", selected_text="section 5",
                      anchor="12.", current_links="[]", note="should link the 1997 Act")
    flags = f.list_refinement_flags()
    assert flags and flags[0]["selected_text"] == "section 5"
    f.resolve_refinement_flag(flag_id=flags[0]["flag_id"])
    assert f.list_refinement_flags() == []
