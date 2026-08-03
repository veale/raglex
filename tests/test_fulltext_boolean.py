"""Two ways the free-text layer answered a question it had not been asked.

Both are the doc_type failure again: the query is answerable, the machinery quietly
substitutes a different one, and the reader is handed a number rather than an error.

* **Verification re-read OR as AND.** The tsquery was a correct disjunction; the
  literal-verification pass that follows it required EVERY quoted phrase in the query
  at once, whatever branch each sat in. Measured on the live corpus:
  ``"duty of care"`` 3,920 documents, ``"duties of care"`` 571, and the OR of the two
  — **530**. A disjunction returning less than one seventh of one of its own halves.
* **A quoted phrase swallowed the NEAR operator.** ``"duty of care" NEAR/5 breach``
  compiled to ``('duty' <5> 'of' <5> 'care') & 'breach'``: the phrase lost the
  adjacency that made it a phrase, and the proximity decayed into a plain AND. Written
  the other way round it was correct, so the two spellings of one question disagreed.
"""

from __future__ import annotations

import pytest

from raglex.fulltext.index import verify
from raglex.fulltext.query import parse, to_tsquery


def _q(text: str, *, exact: bool = True):
    return parse(text, exact=exact)


# --- OR must widen, never narrow -----------------------------------------------------

def test_or_of_two_phrases_is_satisfied_by_either():
    p = _q('"duty of care" OR "duties of care"')
    assert verify("he owed a duty of care to the claimant", p) is not None
    assert verify("the duties of care owed were extensive", p) is not None
    assert verify("nothing relevant here at all", p) is None


def test_or_branch_offset_anchors_on_the_half_that_matched():
    """The snippet must open on the branch the document answers, not the first one
    written — otherwise a matching document shows no passage."""
    p = _q('"duty of care" OR "duties of care"')
    text = "preamble preamble the duties of care owed"
    assert verify(text, p) == text.index("duties of care")


def test_and_of_two_phrases_still_requires_both():
    p = _q('"duty of care" AND "reasonable foreseeability"')
    assert verify("a duty of care arose", p) is None
    assert verify("duty of care and reasonable foreseeability both", p) is not None


def test_implicit_and_still_requires_both():
    p = _q('"duty of care" "reasonable foreseeability"')
    assert verify("a duty of care arose", p) is None


def test_negation_is_scoped_to_its_own_branch():
    p = _q('"duty of care" -"duties of care"')
    assert verify("he owed a duty of care", p) is not None
    assert verify("duty of care and duties of care both appear", p) is None


def test_or_inside_and_reads_as_written():
    p = _q('"data protection" ("data subject" OR "data controller")')
    assert verify("data protection and the data subject", p) is not None
    assert verify("data protection and the data controller", p) is not None
    assert verify("data protection alone", p) is None


def test_relaxed_mode_never_literal_checks():
    """A quotation in relaxed mode asks for the STEMMED phrase, which the tsquery has
    already matched — re-rejecting it as a substring undoes the point of the index."""
    p = _q('"duty of care" OR "duties of care"', exact=False)
    assert p.literals == [] and not p.needs_verification
    assert verify("he owed duties of care", p) == 0


# --- a phrase is an operand, not a slop target ---------------------------------------

def test_phrase_on_the_left_of_near_keeps_its_adjacency():
    assert to_tsquery(_q('"duty of care" NEAR/5 breach')) == (
        "(('duty' <-> 'of' <-> 'care') <5> 'breach')")


def test_near_is_the_same_operator_from_either_side():
    left = to_tsquery(_q('"duty of care" NEAR/5 breach'))
    right = to_tsquery(_q('breach NEAR/5 "duty of care"'))
    assert "<5>" in left and "<5>" in right
    assert "&" not in left and "&" not in right
    assert "('duty' <-> 'of' <-> 'care')" in left
    assert "('duty' <-> 'of' <-> 'care')" in right


def test_tilde_after_a_phrase_is_still_slop():
    assert to_tsquery(_q('"duty of care"~3')) == "('duty' <3> 'of' <3> 'care')"


def test_tilde_between_terms_is_still_proximity():
    assert to_tsquery(_q("negligence ~3 damages")) == "('negligence' <3> 'damages')"


@pytest.mark.parametrize("query,expected", [
    ("(negligence OR nuisance) NEAR/3 damages",
     "(('negligence' | 'nuisance') <3> 'damages')"),
    ("damages NEAR/3 (negligence OR nuisance)",
     "('damages' <3> ('negligence' | 'nuisance'))"),
    ("(negligence OR nuisance) NEAR/3 (damages OR loss)",
     "(('negligence' | 'nuisance') <3> ('damages' | 'loss'))"),
])
def test_grouped_operands_survive_near(query, expected):
    assert to_tsquery(_q(query)) == expected


# --- a refused query is not an empty corpus -------------------------------------------

def test_a_refused_query_is_reported_rather_than_answered_with_zero():
    from raglex.fulltext import index as fts
    from raglex.storage.catalogue import FtsQueryError

    class _Refuses:
        def fts_total(self, *a, **k):
            raise FtsQueryError("syntax error in tsquery")

        def fts_search(self, *a, **k):
            raise FtsQueryError("syntax error in tsquery")

    res = fts.search(_Refuses(), None, "negligence")
    assert res.error and "refused" in res.error
    assert res.total == 0 and res.hits == []
