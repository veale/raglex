"""The free-text query language.

Every case here is either something ``websearch_to_tsquery`` gets wrong (checked
against the live database before this parser was written) or something the literal
guarantee depends on. The compiled output is tsquery *source*: Postgres still stems
it and turns stopwords into position gaps, so ``'duty' <-> 'of' <-> 'care'`` becomes
``'duti' <2> 'care'`` — verified against Postgres 16, not assumed.
"""

from __future__ import annotations

from raglex.fulltext import parse, to_tsquery


def q(text: str, *, exact: bool = True) -> str | None:
    return to_tsquery(parse(text, exact=exact), exact=exact)


# -- what websearch_to_tsquery got wrong --------------------------------------
def test_grouping_is_preserved():
    """websearch compiles "(negligence or nuisance) damages" to
    'neglig' | 'nuisanc' & 'damag' — which, because & binds tighter than |, means
    *negligence OR (nuisance AND damages)*. Silently the wrong query."""
    assert q("(negligence or nuisance) damages") == \
        "(('negligence' | 'nuisance') & 'damages')"


def test_wildcards_survive():
    """websearch drops the star entirely, leaving a plain term."""
    assert q("neglig*") == "'neglig':*"
    assert q("neglig* damages") == "('neglig':* & 'damages')"


def test_proximity_is_available_in_the_forms_lawyers_type():
    # Westlaw and Lexis users arrive expecting /n; ~n and NEAR/n are the same idea
    for form in ("negligence NEAR/3 damages", "negligence /3 damages",
                 "negligence ~3 damages"):
        assert q(form) == "('negligence' <3> 'damages')", form


# -- the literal guarantee -----------------------------------------------------
def test_a_quoted_phrase_is_reported_for_verification():
    p = parse('"duty of care"')
    assert [ph.text for ph in p.literals] == ["duty of care"]
    # the tsquery still goes out — it is what NARROWS; the literal check decides
    assert to_tsquery(p) == "('duty' <-> 'of' <-> 'care')"


def test_a_negated_phrase_is_not_pushed_into_the_tsquery():
    """The correctness point. Postgres stems, so "duties of care" matches the query
    "duty of care". If -"duty of care" were compiled to a tsquery NOT, a document
    containing only "duties of care" would be excluded — although it does not contain
    the literal string — and verification can only filter candidates, never restore
    one that was never retrieved."""
    p = parse('"duty of care" -"contributory negligence"')
    assert to_tsquery(p) == "('duty' <-> 'of' <-> 'care')"      # no !(...)
    assert [ph.text for ph in p.excluded] == ["contributory negligence"]
    assert [ph.text for ph in p.literals] == ["duty of care"]


def test_relaxed_mode_pushes_the_negation_down_and_asks_for_no_verification():
    p = parse('"duty of care" -"contributory negligence"', exact=False)
    assert p.literals == [] and p.excluded == []
    compiled = to_tsquery(p, exact=False)
    assert "!(" in compiled and "'contributory' <-> 'negligence'" in compiled


def test_a_negated_bare_word_is_still_a_tsquery_not():
    # only *phrases* have the over-exclusion problem; a single stem is symmetric
    assert q("negligence -contributory") == "('negligence' & !('contributory'))"


# -- failure modes that must not be silent ------------------------------------
def test_an_all_stopword_phrase_is_reported_rather_than_returning_nothing():
    """Postgres compiles "in and of itself" to an empty tsquery and matches nothing,
    with only a NOTICE. A reader must be told, not shown zero results."""
    p = parse('"in and of itself"')
    assert p.notes and "cannot be looked up" in p.notes[0]


def test_ordinary_phrases_get_no_note():
    assert parse('"duty of care"').notes == []


# -- shapes real queries take ---------------------------------------------------
def test_punctuation_inside_a_phrase_becomes_adjacency():
    # Postgres tokenises "section 3(2)" to section/3/2; the literal check is what
    # puts the parentheses back
    p = parse('"section 3(2)"')
    assert to_tsquery(p) == "('section' <-> '3' <-> '2')"
    assert [ph.text for ph in p.literals] == ["section 3 2"]


def test_a_bare_word_carrying_punctuation_is_treated_as_one_thing():
    # "R(Miller)" is three lexemes to Postgres; adjacency keeps it meaning one thing
    assert q("R(Miller)") == "('R' <-> 'Miller')"


def test_phrase_slop():
    assert q('"reasonable excuse"~4') == "('reasonable' <4> 'excuse')"


def test_implicit_and_explicit_conjunction_agree():
    assert q("negligence damages") == q("negligence AND damages") == \
        "('negligence' & 'damages')"


def test_empty_and_junk_queries_are_not_errors():
    for junk in ("", "   ", "()", ")(", "-", "AND OR"):
        p = parse(junk)
        assert to_tsquery(p) is None, junk


def test_unbalanced_parenthesis_is_forgiven():
    # a search box, not a compiler
    assert q("(negligence or nuisance") == "('negligence' | 'nuisance')"


def test_quotes_cannot_inject_tsquery_syntax():
    # a lexeme is single-quoted in tsquery source; the user must not be able to
    # close it and append their own operators
    compiled = q("""o'brien""")
    assert compiled is not None and compiled.count("'") == 2
    assert q("""a' | 'b""") is not None


def test_a_citation_number_stays_one_lexeme():
    """Postgres keeps a NUMBER/NUMBER run whole — measured on the live index::

        to_tsvector('english', 'No 765/2008')              -> '765/2008':2
        to_tsvector('english', 'Regulation (EU) 2016/679') -> '2016/679':3 'eu':2 …

    Splitting it here compiled to `'765' <-> '2008'`, which cannot match that vector,
    so every quoted phrase carrying an EU citation number returned NOTHING and looked
    like an honest absence:

        "…affixed in violation of Article 30 of Regulation (EC) No 765/2008"   0
        the same phrase, cut before the number                                43
    """
    assert q('"Regulation (EC) No 765/2008"') == \
        "('Regulation' <-> 'EC' <-> 'No' <-> '765/2008')"
    assert q('"Regulation (EU) 2016/679"') == \
        "('Regulation' <-> 'EU' <-> '2016/679')"
    assert q('"1.5 million"') == "('1.5' <-> 'million')"
    # a sub-provision is still several lexemes, exactly as Postgres reads it
    assert q('"Article 6(1)(f)"') == "('Article' <-> '6' <-> '1' <-> 'f')"
