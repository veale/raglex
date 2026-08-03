"""One judgment, one node: the UK chamber/division identity work.

The corpus mints a chamber-less alias for every UK judgment, because judges leave the
division out ("[2013] EWHC 3560" for a Commercial Court case). That is right and
load-bearing — until two judgments share a number, which they do, because UK numbering
runs per division and not per court:

    ewhc/admin/2025/177   Guzdek v Circuit Court in Kalisz
    ewhc/ch/2025/177      Cheshire East Borough Council v M      (different cases)
    ewca/civ/1964/1       Pinion, Re
    ewca/crim/1964/1      R v Chandler                           (1,551 such pairs held)

The alias key is unique, so the last import wins and every bare citation follows it.
"""

from __future__ import annotations

from datetime import date

import pytest

from raglex.core.models import DocType, ExtractedVia, Record
from raglex.ops import uk_identity
from raglex.storage import TextStore


def _case(cat, stable_id, title, text=None, ts=None):
    rec = Record(source="uk-caselaw", stable_id=stable_id, doc_type=DocType.JUDGMENT,
                 title=title, decision_date=date(2015, 6, 1), text=text or title,
                 raw_bytes=(text or title).encode(),
                 extracted_via=ExtractedVia.STRUCTURED)
    rec.ensure_payload_hash()
    path = str(ts.put(rec.payload_hash, text or title)) if ts else None
    cat.upsert_document(rec, text_path=path)
    return stable_id


def _cite(cat, src, candidate, *, dst=None, start=None):
    """One citing edge, written as the extraction stage writes it (the candidate_id is
    the chamber-less form the citation itself yields)."""
    cat.conn.execute(
        "INSERT INTO relations (src_id, dst_id, raw_citation_string, candidate_id, "
        " resolution_status, relationship_type, extracted_via, context_start, "
        " context_end) VALUES (?,?,?,?,?,?,?,?,?)",
        (src, dst, candidate, candidate, "resolved" if dst else "pending",
         "mentions", "regex", start, start))
    cat.conn.commit()


@pytest.fixture
def corpus(catalogue, tmp_path):
    """Two 1964 EWCA judgments sharing number 1 — one civil, one criminal — plus the
    chamber-less alias that (wrongly) names only one of them."""
    ts = TextStore(tmp_path / "text")
    _case(catalogue, "ewca/civ/1964/1", "Pinion, Re", ts=ts)
    _case(catalogue, "ewca/crim/1964/1", "R v Chandler", ts=ts)
    catalogue.put_alias("ewca/1964/1", "ewca/crim/1964/1", source="chamber-alias")
    return catalogue, ts


def test_an_alias_that_names_two_judgments_is_found(corpus):
    cat, _ts = corpus
    out = uk_identity.audit_chamber_aliases(cat)
    assert out["ambiguous_numbers"] == 1
    assert out["samples"][0]["cited_as"] == "ewca/1964/1"
    assert {d["id"] for d in out["samples"][0]["held"]} == {
        "ewca/civ/1964/1", "ewca/crim/1964/1"}


def test_the_ambiguous_alias_is_deleted_and_its_edges_demoted(corpus):
    """Not repointed to a better guess — there is nothing in the key to guess WITH."""
    cat, ts = corpus
    _case(cat, "ewhc/qb/2015/9", "Citing Judgment", ts=ts)
    _cite(cat, "ewhc/qb/2015/9", "ewca/1964/1", dst="ewca/crim/1964/1")

    dry = uk_identity.repair_chamber_aliases(cat, dry_run=True)
    assert dry["ambiguous_numbers"] == 1 and dry["aliases_deleted"] == 0
    assert cat.get_alias("ewca/1964/1") == "ewca/crim/1964/1"

    done = uk_identity.repair_chamber_aliases(cat, dry_run=False)
    assert done["aliases_deleted"] == 1 and done["edges_demoted"] == 1
    assert cat.get_alias("ewca/1964/1") is None
    row = cat.conn.execute(
        "SELECT resolution_status, dst_id FROM relations "
        "WHERE candidate_id = 'ewca/1964/1'").fetchone()
    assert row["resolution_status"] == "pending" and row["dst_id"] is None


def test_the_name_beside_the_citation_settles_it(corpus):
    """The discriminating evidence is a CLOSED candidate set: two judgments, one name.
    That is not the corpus-wide name matching that collides on "R (…) v SSHD" — here the
    question is only which of these two the writer meant."""
    cat, ts = corpus
    text = ("The court considered the reasoning in Pinion, Re [1964] EWCA 1 and applied "
            "it to the facts of this appeal.")
    _case(cat, "ewhc/qb/2015/10", "Citing Judgment", text=text, ts=ts)
    _cite(cat, "ewhc/qb/2015/10", "ewca/1964/1", dst="ewca/crim/1964/1",
          start=text.index("[1964]"))

    dry = uk_identity.tiebreak_ambiguous_divisions(cat, ts, dry_run=True)
    assert dry["settled"] == 1 and dry["repointed"] == 1
    assert dry["samples"][0]["now"] == "ewca/civ/1964/1"
    assert cat.conn.execute(
        "SELECT dst_id FROM relations WHERE candidate_id = 'ewca/1964/1'"
    ).fetchone()["dst_id"] == "ewca/crim/1964/1", "a dry run must not write"

    done = uk_identity.tiebreak_ambiguous_divisions(cat, ts, dry_run=False)
    assert done["repointed"] == 1
    assert cat.conn.execute(
        "SELECT dst_id FROM relations WHERE candidate_id = 'ewca/1964/1'"
    ).fetchone()["dst_id"] == "ewca/civ/1964/1"


def test_an_undecidable_reference_is_left_alone(corpus):
    """No name, nothing criminal or public-law in the run-up: two candidates explain the
    text equally well, so the edge stays exactly as it was."""
    cat, ts = corpus
    text = "As was held in [1964] EWCA 1, the position is settled."
    _case(cat, "ewhc/qb/2015/11", "Citing Judgment", text=text, ts=ts)
    _cite(cat, "ewhc/qb/2015/11", "ewca/1964/1", dst="ewca/crim/1964/1",
          start=text.index("[1964]"))
    out = uk_identity.tiebreak_ambiguous_divisions(cat, ts, dry_run=False)
    assert out["repointed"] == 0
    assert cat.conn.execute(
        "SELECT dst_id FROM relations WHERE candidate_id = 'ewca/1964/1'"
    ).fetchone()["dst_id"] == "ewca/crim/1964/1"


def test_r_v_points_at_the_criminal_division(corpus):
    cat, ts = corpus
    text = "The appellant relies on R v Chandler [1964] EWCA 1 at para 12."
    _case(cat, "ewhc/qb/2015/12", "Citing Judgment", text=text, ts=ts)
    _cite(cat, "ewhc/qb/2015/12", "ewca/1964/1", start=text.index("[1964]"))
    out = uk_identity.tiebreak_ambiguous_divisions(cat, ts, dry_run=False)
    assert out["settled"] == 1
    assert cat.conn.execute(
        "SELECT dst_id FROM relations WHERE candidate_id = 'ewca/1964/1'"
    ).fetchone()["dst_id"] == "ewca/crim/1964/1"


# -- duplicates, which are a different thing from ambiguity ------------------------
def test_two_slugs_for_one_court_are_one_judgment(catalogue, tmp_path):
    """ewhc/pat and ewhc/patents are the same court. 312 judgments are held twice under
    them, which splits their citations, their authority and their search results."""
    ts = TextStore(tmp_path / "text")
    _case(catalogue, "ewhc/patents/2025/375", "Lufthansa Technik AG v Astronics",
          text="full judgment text", ts=ts)
    _case(catalogue, "ewhc/pat/2025/375", "Lufthansa Technik AG v Astronics", ts=ts)
    _case(catalogue, "ewhc/qb/2026/1", "Citing Judgment", ts=ts)
    _cite(catalogue, "ewhc/qb/2026/1", "ewhc/pat/2025/375", dst="ewhc/pat/2025/375")

    assert uk_identity.audit_chamber_aliases(catalogue)["ambiguous_numbers"] == 0, \
        "one court under two names is not an ambiguity"

    dry = uk_identity.unify_synonym_slugs(catalogue, dry_run=True)
    assert dry["duplicate_nodes"] == 1 and catalogue.get_document("ewhc/pat/2025/375")

    done = uk_identity.unify_synonym_slugs(catalogue, dry_run=False)
    assert done["duplicate_nodes"] == 1 and done["edges_moved"] == 1
    # the node is unified, and every way it was named still reaches it
    assert catalogue.get_document("ewhc/pat/2025/375") is None
    assert catalogue.find_document_id("ewhc/pat/2025/375") == "ewhc/patents/2025/375"
    assert catalogue.conn.execute(
        "SELECT dst_id FROM relations WHERE src_id = 'ewhc/qb/2026/1'"
    ).fetchone()["dst_id"] == "ewhc/patents/2025/375"
    meta = catalogue.document_meta("ewhc/patents/2025/375")
    assert {"source": "uk-caselaw", "id": "ewhc/pat/2025/375"} in meta["renditions"]


def test_qb_and_kb_are_only_folded_when_they_are_the_same_case(catalogue, tmp_path):
    """The Queen's Bench became the King's Bench in September 2022, so a citation may
    name either. But two DIFFERENT judgments can hold one number either side of the
    rename — five do — and those must not be merged on the strength of the number."""
    ts = TextStore(tmp_path / "text")
    _case(catalogue, "ewhc/qb/2022/3080", "Alpha v Beta", ts=ts)
    _case(catalogue, "ewhc/kb/2022/3080", "Gamma v Delta", ts=ts)
    assert uk_identity.unify_synonym_slugs(catalogue, dry_run=True)["duplicate_nodes"] == 0

    _case(catalogue, "ewhc/qb/2021/500", "Epsilon v Zeta", text="text", ts=ts)
    _case(catalogue, "ewhc/kb/2021/500", "Epsilon v Zeta", ts=ts)
    out = uk_identity.unify_synonym_slugs(catalogue, dry_run=True)
    assert out["duplicate_nodes"] == 1
    assert out["samples"][0]["kept"] == "ewhc/qb/2021/500"


# -- the date the interface uses ----------------------------------------------------
def test_a_judgment_dates_itself_when_the_metadata_does_not(catalogue, tmp_path):
    """68,495 held common-law judgments carry no decision_date, and 68,158 of them carry
    the year in their own identifier. Without a fallback they sort as undated — bottom
    of every newest-first browse, absent from every year range — although the citation
    says plainly what year they are."""
    from raglex.storage.catalogue import effective_date

    assert effective_date("2015-06-01", None, "ewca/civ/2015/1") == \
        ("2015-06-01", "decision_date")
    assert effective_date(None, None, "ewca/civ/1975/5") == ("1975-12-31", "identifier")
    assert effective_date(None, "ECLI:EU:C:2020:559", "62019CJ0311") == \
        ("2020-12-31", "ecli")
    # the judgment date WINS where the two disagree: a December judgment is often
    # numbered in the following year, and there the metadata is the right answer
    assert effective_date("2014-12-19", None, "ewca/civ/2015/3")[0] == "2014-12-19"
    # nothing to go on stays nothing — no year is invented from a docket number
    assert effective_date(None, None, "edpb/binding-decision-1-2026")[0] is None
    assert effective_date(None, None, "ukut/aac/12345")[0] is None


def test_ukut_acc_typo_and_zero_padding_reach_the_held_aac_judgment(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _case(catalogue, "ukut/aac/2014/0310", "Farrand v Information Commissioner", ts=ts)
    assert catalogue.find_document_id("ukut/aac/2014/310") == "ukut/aac/2014/0310"
    assert catalogue.find_document_id("ukut/acc/2014/310") == "ukut/aac/2014/0310"
    assert catalogue.find_existing(["ukut/acc/2014/310"]) == {
        "ukut/acc/2014/310": "ukut/aac/2014/0310"
    }


def test_the_fallback_date_reaches_sorting_filtering_and_the_citation(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _case(catalogue, "ewca/civ/1975/5", "Rose v Plenty", ts=ts)      # no decision_date
    catalogue.conn.execute(
        "UPDATE documents SET decision_date = NULL, effective_date = NULL, "
        "date_provenance = NULL WHERE stable_id = 'ewca/civ/1975/5'")
    catalogue.conn.commit()

    out = catalogue.backfill_effective_dates(dry_run=True)
    assert out["would_update"] >= 1 and out["updated"] == 0
    catalogue.backfill_effective_dates(dry_run=False)
    row = catalogue.get_document("ewca/civ/1975/5")
    assert row["effective_date"] == "1975-12-31"
    assert row["date_provenance"] == "identifier"

    # a year filter finds it, and a date sort places it by its real year
    found = catalogue.search_documents(year_from="1970", year_to="1980", limit=10)
    assert "ewca/civ/1975/5" in {r["stable_id"] for r in found}
    # …and its OSCOLA citation carries the year rather than none
    from raglex.citations.oscola import cite
    assert "1975" in (cite(row, {}) or {}).get("text", "")
