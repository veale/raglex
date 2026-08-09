"""A document's binding of a generic instrument noun to its own parent Act.

A statutory code of practice names its parent Act once — "…of the Investigatory
Powers Act 2016 ("the Act")" — and then says "the Act" for the rest of the document.
Those later mentions are the code's most important references, because a code of
practice IS guidance on those provisions, and every one of them used to be dropped:
"the Act" is refused as a corpus-wide shorthand (rightly — it is document-relative,
and the corpus holds 108,390 UK instruments), and carry-forward stops wherever the
text names its own host.

The contract here: the binding applies INSIDE the document that made it, only to
mentions carrying a provision, and it never travels to the store.
"""

from __future__ import annotations

from raglex.citations.extractor import declared_instrument_host, extract_citations

CODE = (
    "This code of practice relates to the powers in Part 2 (interception) of the "
    "Investigatory Powers Act 2016 (“the Act”).[footnote 1]\n\n"
    "2.1 Section 18 of the Act sets out who may apply for a warrant, and section "
    "19 of the Act says who may issue one.\n\n"
    "3.4 The duty in section 2 of the Act applies. See also Schedule 3 to the Act.\n"
)


def _by_method(cites, method):
    return {(c.candidate_id, c.pinpoint) for c in cites if c.method == method}


def test_the_act_binds_to_the_statute_the_document_names():
    got = _by_method(extract_citations(CODE), "doc_host")
    assert got == {
        ("ukpga/2016/25", "s. 18"),
        ("ukpga/2016/25", "s. 19"),
        ("ukpga/2016/25", "s. 2"),
        ("ukpga/2016/25", "Sch. 3"),
    }


def test_binding_requires_a_provision():
    # A bare "the Act" adds an edge the document already has from its full-title
    # mention, and carries nothing a reader could check. Only pinpointed uses link.
    text = CODE + "\n4.1 The Act is a long statute and the Act is much discussed.\n"
    assert all(p for _c, p in _by_method(extract_citations(text), "doc_host"))


def test_binding_never_reaches_the_corpus_wide_store():
    # The whole reason "the Act" is refused corpus-wide: it is document-relative.
    defs: list[dict] = []
    extract_citations(CODE, defs_out=defs)
    assert not [d for d in defs if d["shorthand"].lower().endswith("act")]


def test_role_nouns_are_still_refused():
    # "the Appellant" names a slot, not an authority — an instrument noun is the
    # only thing a document may bind to itself this way.
    text = (
        "The claim is under the Investigatory Powers Act 2016. The appellant "
        "(“the Appellant”) relies on section 18 of the Appellant.\n"
    )
    assert not _by_method(extract_citations(text), "doc_host")


def test_year_qualified_form_resolves():
    # "the 1967 Act" carries its own year, so it is distinctive enough to pass
    # ``valid_shorthand`` and is handled by the ordinary in-document shorthand pass.
    # It is asserted here because the two mechanisms are neighbours and the bare
    # "the Act" case must not regress the qualified one.
    text = (
        "The Leasehold Reform Act 1967 (“the 1967 Act”) applies here. "
        "Section 9 of the 1967 Act sets out the price payable.\n"
    )
    got = {(c.candidate_id, c.pinpoint) for c in extract_citations(text)}
    assert ("ukpga/1967/88", "s. 9") in got


def test_unresolved_host_binds_nothing():
    # If the named Act didn't resolve there is no id to file the binding under, and
    # a confident-looking link to nothing is worse than no link.
    text = (
        "This code is made under the Entirely Fictitious Powers Act 2099 "
        "(“the Act”). Section 4 of the Act applies.\n"
    )
    assert not _by_method(extract_citations(text), "doc_host")


# --- the alias pass carries pinpoints too -------------------------------------
# "section 16 of RIPA" is a citation OF s.16, but the alias pass recorded only the
# four letters and left the pinpoint to carry-forward, whose edges are 'inferred'
# and therefore excluded from citing_documents(). Big Brother Watch v UK pins to
# RIPA s.16 eighteen times and was absent from the anchor's citer list.

def test_alias_mentions_carry_their_provision():
    text = (
        "The Regulation of Investigatory Powers Act 2000 (\"RIPA\") is in issue. "
        "Section 16 of RIPA does the heavy lifting. See also section 8(4) of RIPA, "
        "s. 15 of RIPA and Schedule 2 to RIPA. Compare RIPA, s 17."
    )
    got = _by_method(extract_citations(text, aliases={"RIPA": "ukpga/2000/23"}),
                     "named_alias")
    for pin in ("s. 16", "s. 8(4)", "s. 15", "Sch. 2", "s. 17"):
        assert ("ukpga/2000/23", pin) in got, pin


def test_trailing_form_does_not_swallow_the_next_reference():
    # "section 8(4) of RIPA, s. 15 of RIPA" is two references, not one: the ", s. 15"
    # belongs to the mention that follows it.
    text = "See section 8(4) of RIPA, s. 15 of RIPA."
    got = _by_method(extract_citations(text, aliases={"RIPA": "ukpga/2000/23"}),
                     "named_alias")
    assert ("ukpga/2000/23", "s. 8(4)") in got
    assert ("ukpga/2000/23", "s. 15") in got


# --- the unquoted house style -------------------------------------------------
# `_SHORTHAND_DEF` requires quotes inside a round bracket, because a bare round-
# bracket name is far too loose to learn as a corpus-wide shorthand. That is right
# for shorthands and wrong for hosts: UK regulator drafting overwhelmingly writes
# "(the Act)" with no quotes at all, so the binding was invisible and the
# document's own provisions carried forward onto whatever statute a passing
# sentence last named.

UNQUOTED = (
    "This code is issued under the Equality Act 2010 (the Act). "
    "Section 13 of the Act defines direct discrimination, and section 19 of the "
    "Act defines indirect discrimination. See also Schedule 9 to the Act."
)


def test_an_unquoted_bracket_binds_the_host_too():
    got = _by_method(extract_citations(UNQUOTED), "doc_host")
    assert got == {
        ("ukpga/2010/15", "s. 13"),
        ("ukpga/2010/15", "s. 19"),
        ("ukpga/2010/15", "Sch. 9"),
    }


def test_an_unquoted_bracket_still_never_reaches_the_store():
    defs: list[dict] = []
    extract_citations(UNQUOTED, defs_out=defs)
    assert not [d for d in defs if d["shorthand"].lower().endswith("act")]


def test_only_an_instrument_noun_may_be_bound_unquoted():
    # "(the Commission)" is a body, not an instrument: binding it would let any
    # bracketed noun in the corpus claim the statute it happens to follow.
    text = ("A duty arises under the Equality Act 2010 (the Commission). "
            "Section 13 of the Commission is not a citation.")
    assert not _by_method(extract_citations(text), "doc_host")


def test_declared_instrument_host_reports_what_a_document_binds():
    assert declared_instrument_host(UNQUOTED) == ("ukpga/2010/15", "act")
    assert declared_instrument_host(CODE) == ("ukpga/2016/25", "act")
    # A document that binds nothing must not be given a host it never claimed.
    assert declared_instrument_host(
        "The Commission published a report on equality in Wales.") is None
