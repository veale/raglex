"""Who decided a case, read off the judgment's own first page.

The corpus holds no bench metadata — a Find Case Law document's meta_json carries only
provenance keys — so the bench under a judgment's title comes from parsing its intituling.
The tests pin the layouts the UK courts actually use, including the letter-spaced labels
BAILII sets ("B e f o r e :") that a plain /^before/ never matches.
"""

from __future__ import annotations

from raglex.citations.intituling import (
    parse_coram,
    parse_intituling,
    parse_representation,
    standardise_judge,
)

EWCA = """[

Neutral Citation Number: [2005] EWCA Civ 1300

B e f o r e :

LORD JUSTICE CHADWICK

LORD JUSTICE LATHAM

and

LORD JUSTICE NEUBERGER

____________________

Between:

MARK TAYLOR
"""

EWHC = """Neutral Citation Number: [2026] EWHC 532 (KB)

B e f o r e :

MR JUSTICE COTTER

____________________

Between:

PING FAI YUEN

____________________

Jim Sturman KC (instructed by Egan Meyer Solicitors) for the Claimant

Josephine Davies KC & James Lamming (instructed by Russell-Cooke LLP) for the 1st Defendant

Hearing dates: 02 March 2026
"""

UKHL = """HOUSE OF LORDS

[2007] UKHL 17

Appellate Committee

Lord Hoffmann

Lord Hope of Craighead

Baroness Hale of Richmond

Counsel

Appellants:

Lucy Theis QC
"""

CSOH = """OUTER HOUSE, COURT OF SESSION

[2012] CSOH 25

OPINION OF LORD TYRE

in the cause
"""


def test_letter_spaced_before_block_is_the_common_uk_layout():
    assert parse_coram(EWCA) == ["Chadwick LJ", "Latham LJ", "Neuberger LJ"]
    assert parse_coram(EWHC) == ["Cotter J"]


def test_courts_that_never_write_before():
    # the Lords name an "Appellate Committee"; the Court of Session opens "OPINION OF"
    assert parse_coram(UKHL) == [
        "Lord Hoffmann", "Lord Hope of Craighead", "Baroness Hale of Richmond"]
    assert parse_coram(CSOH) == ["Lord Tyre"]


def test_counsel_lines_are_lifted_without_a_label():
    rep = parse_representation(EWHC)
    assert rep[0].startswith("Jim Sturman KC (instructed by Egan Meyer")
    assert any("1st Defendant" in r for r in rep)
    assert not any("Hearing dates" in r for r in rep)


def test_prose_is_never_mistaken_for_a_bench():
    # a sentence that happens to open with "Before" (this exact line matched the first
    # draft, and produced a "judge" called "the jury Ms Kingswood…")
    assert parse_coram("Before the jury Ms Kingswood said the appellant had an IQ of 68.") == []
    assert parse_coram("") == [] and parse_coram(None) == []
    assert parse_intituling("A judgment with no header at all.") == {}


def test_names_are_standardised_the_way_a_lawyer_writes_them():
    assert standardise_judge("LORD JUSTICE CHADWICK") == "Chadwick LJ"
    assert standardise_judge("MR JUSTICE COTTER") == "Cotter J"
    assert standardise_judge("LADY JUSTICE CARR DBE") == "Carr LJ"
    assert standardise_judge("MRS JUSTICE McGOWAN DBE") == "McGowan J"   # Mc keeps its cap
    assert standardise_judge("LORD JUSTICE MOORE-BICK") == "Moore-Bick LJ"
    assert standardise_judge("HER HONOUR JUDGE KARU") == "HHJ Karu"
    assert standardise_judge("SIR JULIAN FLAUX (SITTING IN RETIREMENT)") == "Sir Julian Flaux"
    # an office with no surname keeps its title
    assert standardise_judge("THE MASTER OF THE ROLLS") == "The Master of the Rolls"
    # post-nominals stay in capitals
    assert standardise_judge("HIS HONOUR JUDGE STEPHENS QC") == "HHJ Stephens QC"


def test_role_and_venue_lines_are_not_judges():
    assert parse_coram("Before:\n\nSitting at: Royal Courts of Justice\n\nBetween:") == []
    assert "Deputy President" not in parse_coram(
        "Before:\n\nLord Hope\n\nDeputy President\n\nBetween:")
