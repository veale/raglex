"""Parallel-citation mining: adjacency grouping + union-find with the one-neutral veto."""

from __future__ import annotations

from raglex.citations.parallel import (
    ClusterIndex,
    Occurrence,
    adjacency_groups,
    coref_key,
    neutral_slug,
)


def _occs(text: str, *raws: str) -> list[Occurrence]:
    """Build occurrences by locating each raw string in ``text`` (test convenience)."""
    occs, cursor = [], 0
    for r in raws:
        i = text.index(r, cursor)
        occs.append(Occurrence(r, i, i + len(r)))
        cursor = i + len(r)
    return occs


def test_adjacency_groups_semicolon_run():
    text = "As the House held in Pepper v Hart [1993] AC 593; [1992] 3 WLR 1032; [1992] STC 898, the rule is clear."
    occs = _occs(text, "[1993] AC 593", "[1992] 3 WLR 1032", "[1992] STC 898")
    groups = adjacency_groups(text, occs)
    assert groups == [["[1993] AC 593", "[1992] 3 WLR 1032", "[1992] STC 898"]]


def test_adjacency_tolerates_pinpoint_and_and():
    text = "see [1993] AC 593, at 599; and [1992] 3 WLR 1032 in this respect"
    occs = _occs(text, "[1993] AC 593", "[1992] 3 WLR 1032")
    assert adjacency_groups(text, occs) == [["[1993] AC 593", "[1992] 3 WLR 1032"]]


def test_prose_between_citations_is_not_a_group():
    text = "In [1993] AC 593 the court considered the earlier [1992] 3 WLR 1032 decision."
    occs = _occs(text, "[1993] AC 593", "[1992] 3 WLR 1032")
    assert adjacency_groups(text, occs) == []  # separated by real words, not a list


def test_two_neutrals_in_a_run_are_not_grouped():
    # appeal history — the UKSC and EWCA judgments are DIFFERENT documents, not parallels.
    # The extractor stores each one's case candidate; EWCA Civ's inline division is only
    # recognised there, so we supply it as the occurrence candidate (as the DB does).
    text = "affirming [2019] EWCA Civ 5; [2019] UKSC 1 on the point"
    a, b = _occs(text, "[2019] EWCA Civ 5", "[2019] UKSC 1")
    occs = [Occurrence(a.raw, a.char_start, a.char_end, candidate="ewca/civ/2019/5"),
            Occurrence(b.raw, b.char_start, b.char_end, candidate="uksc/2019/1")]
    assert adjacency_groups(text, occs) == []


def test_neutral_slug_only_for_neutrals():
    assert neutral_slug("[2015] EWHC 100 (Ch)") == "ewhc/ch/2015/100"
    assert neutral_slug("[1993] AC 593") is None


def test_cluster_index_unions_and_reports_neutral():
    idx = ClusterIndex()
    idx.add("[1993] ac 593", neutral=None)
    idx.add("ewhc/ch/1993/1", neutral="ewhc/ch/1993/1")
    assert idx.union("[1993] ac 593", "ewhc/ch/1993/1") is True
    assert idx.neutral_of("[1993] ac 593") == "ewhc/ch/1993/1"
    assert idx.clusters() == [["[1993] ac 593", "ewhc/ch/1993/1"]] or \
           sorted(idx.clusters()[0]) == ["[1993] ac 593", "ewhc/ch/1993/1"]


def test_cluster_veto_blocks_two_neutrals():
    idx = ClusterIndex()
    idx.add("a", neutral="uksc/2019/1")
    idx.add("b", neutral="ewca/civ/2019/5")
    assert idx.union("a", "b") is False       # would merge two distinct neutrals → vetoed
    assert idx.neutral_of("a") == "uksc/2019/1"


def test_veto_survives_transitive_merge():
    idx = ClusterIndex()
    idx.add("rep1", neutral=None)
    idx.add("neutralA", neutral="a/1/1")
    idx.add("neutralB", neutral="b/2/2")
    assert idx.union("rep1", "neutralA") is True
    assert idx.union("rep1", "neutralB") is False  # rep1's cluster already carries a/1/1


def test_coref_key_needs_two_tokens_and_a_year():
    assert coref_key("Pepper v Hart", "[1993] AC 593") == (frozenset({"pepper", "hart"}), 1993)
    assert coref_key("Brown", "[1993] AC 593") is None       # one distinctive token
    assert coref_key("Pepper v Hart", "AC 593") is None      # no year
