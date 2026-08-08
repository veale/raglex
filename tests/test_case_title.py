"""Re-casing HUDOC's upper-case docnames without losing what they said."""

import re

import pytest

from raglex.core.case_title import titlecase_case_name as tc


def test_the_basic_english_and_french_forms():
    assert tc("CASE OF TSANOVA-GECHEVA v. BULGARIA") == "Case of Tsanova-Gecheva v. Bulgaria"
    assert tc("AFFAIRE DI BELMONTE c. ITALIE") == "Affaire Di Belmonte c. Italie"
    assert tc("CASE OF PUGACH AND OTHERS v. RUSSIA") == "Case of Pugach and Others v. Russia"
    # French keeps "et autres" lower-case where English capitalises "and Others"
    assert tc("AFFAIRE MAGNIN ET AUTRES c. FRANCE") == "Affaire Magnin et autres c. France"


def test_the_article_of_a_state_stays_lower_but_of_a_party_does_not():
    """The Court writes "v. the United Kingdom" and "The Sunday Times v. …" — the same
    word, cased by where it sits."""
    assert tc("THE SUNDAY TIMES v. THE UNITED KINGDOM") == (
        "The Sunday Times v. the United Kingdom")
    assert tc("CASE OF THE SUNDAY TIMES v. THE UNITED KINGDOM (No. 2)") == (
        "Case of The Sunday Times v. the United Kingdom (No. 2)")
    assert tc('CASE OF DJIDROVSKI v. "THE FORMER YUGOSLAV REPUBLIC OF MACEDONIA"') == (
        'Case of Djidrovski v. "the former Yugoslav Republic of Macedonia"')
    assert tc("CATANA v THE REPUBLIC OF MOLDOVA - 43237/13") == (
        "Catana v the Republic of Moldova - 43237/13")


def test_particles_open_a_party_capitalised_and_continue_it_lower():
    assert tc("CASE OF VAN PELT v. FRANCE") == "Case of Van Pelt v. France"
    assert tc("CASE OF VON HANNOVER v. GERMANY (No. 2)") == (
        "Case of Von Hannover v. Germany (No. 2)")
    assert tc("CASE OF DE GEOUFFRE DE LA PRADELLE v. FRANCE") == (
        "Case of De Geouffre de la Pradelle v. France")
    # …and the "and" joining two applicants opens a new one
    assert tc("CASE OF CENTRO EUROPA 7 S.R.L. AND DI STEFANO v. ITALY") == (
        "Case of Centro Europa 7 S.R.L. and Di Stefano v. Italy")


def test_initials_and_dotted_abbreviations_are_not_words():
    assert tc("CASE OF A.B. AGAINST RUSSIA") == "Case of A.B. against Russia"
    assert tc("X. AND OTHERS v. AUSTRIA") == "X. and Others v. Austria"
    assert tc("CASE OF S.A.S. v. FRANCE") == "Case of S.A.S. v. France"
    # a double-barrelled anonymisation is still initials, hyphen and all
    assert tc("A.D.-K. AND OTHERS v. POLAND") == "A.D.-K. and Others v. Poland"


def test_turkish_dotted_i_does_not_leave_a_combining_dot():
    """Python has no Turkish locale, so "İ".lower() is "i" + COMBINING DOT ABOVE. The
    artefact is visible in the rendered title, and 139 titles carry the letter."""
    assert tc("CASE OF ÇETİNKAYA AGAINST TURKEY") == "Case of Çetinkaya against Turkey"
    assert tc("YAZICIOĞLU c. TÜRKİYE") == "Yazicioğlu c. Türkiye"
    # a word that STARTS with the dotted capital keeps it — "İş", not "Iş"
    assert tc("CASE OF İŞ BANKASI v. TÜRKİYE").startswith("Case of İş Bankasi")
    assert "̇" not in tc("DELİCE c. TURQUIE")


def test_the_state_the_court_renamed_gets_its_umlaut_back():
    """HUDOC writes the respondent State both as "TÜRKİYE" and as a flat-ASCII
    "TURKIYE"; only the second needs a table, and it is the only entry there is."""
    assert tc("KORKUT v TURKIYE - 3344/21") == "Korkut v Türkiye - 3344/21"


def test_already_cased_material_is_returned_byte_for_byte():
    """The gate is per token, not per title: HUDOC mixes a shouty party with a
    human-written descriptor, and only the shouty half may be touched."""
    got = tc("MARKOVIC v SERBIA - 70661/14 (Judgment : Violation of Article 6 - Right "
             "to a fair trial (Article 6 - Civil proceedings)) French Text")
    assert got.startswith("Markovic v Serbia - 70661/14 (Judgment : Violation of Article 6")
    assert got.endswith("French Text")
    assert tc("Danilo ZORKO v Slovenia - 24431/10") == "Danilo Zorko v Slovenia - 24431/10"
    assert tc("Hearing McElhinney v. Ireland & United Kingdom 09.02.00") == (
        "Hearing McElhinney v. Ireland & United Kingdom 09.02.00")


def test_codes_forms_and_prefixes():
    assert tc("FERENCSIK v HUNGARY - 33275/08 - HEJUD") == "Ferencsik v Hungary - 33275/08 - HEJUD"
    assert tc("CASE OF KRONE VERLAGS GMBH & CO KG v. AUSTRIA") == (
        "Case of Krone Verlags GmbH & Co KG v. Austria")
    assert tc("CASE OF MCELHINNEY v. IRELAND") == "Case of McElhinney v. Ireland"
    assert tc("CASE OF O'KEEFFE v. IRELAND") == "Case of O'Keeffe v. Ireland"
    assert "COE" in tc("CASE OF X v. FRANCE - [Armenian Translation] by the COE Trust Fund")


def test_counts_in_the_committee_titles_are_quantities_not_parties():
    assert tc("CASE OF BEREZOWSKI AND 109 OTHER CASES AGAINST POLAND") == (
        "Case of Berezowski and 109 other cases against Poland")
    assert tc("CASE OF ANDERSON AND THIRTEEN OTHER CASES AGAINST THE UNITED KINGDOM") == (
        "Case of Anderson and thirteen other cases against the United Kingdom")
    assert tc("GIKA AND FIVE OTHERS v GREECE - 394/03") == (
        "Gika and five others v Greece - 394/03")


@pytest.mark.parametrize("title", [
    "CASE OF EBEDİN ABİ v. TURKEY",              # ABİ is a surname, not the form "AB"
    "ROJ TV A/S v. DENMARK",                     # the slash is part of the form
    "WIKIMEDIA FOUNDATION, INC. v. TURKEY",      # the token's own full stop, once
    "CASE OF BERNH LARSEN HOLDING AS v. NORWAY",  # Norwegian AS is not "A.S."
    "TRANSPETROL, A.S., v. SLOVAKIA",
    "CASE OF BAJIĆ AND OTHERS AND 1 OTHER CASE AGAINST BOSNIA AND HERZEGOVINA",
])
def test_nothing_but_case_ever_changes(title):
    """The whole contract: fold the result and it must equal the folded input. A table
    lookup that swallowed the rest of a token would show up here — keying on ASCII
    letters alone turned "ABİ" into "AB" and "A/S" into "AS"."""
    def fold(s):
        return re.sub(r"\s+", " ", s).casefold().replace("i̇", "i")

    assert fold(tc(title)) == fold(title)


def test_idempotent_and_safe_on_empty():
    for title in ("CASE OF TSANOVA-GECHEVA v. BULGARIA", "Danilo ZORKO v Slovenia", ""):
        assert tc(tc(title)) == tc(title)
    assert tc(None) is None
    assert tc("   ") == "   "


def test_the_backfill_recases_and_keeps_the_registers_own_spelling(tmp_path, monkeypatch):
    """The rewrite has to be reversible: HUDOC's raw docname is already in the metadata,
    and where a register gives no such field the original title is recorded before the
    write. And it is a system re-casing, so it must not claim to be human curation."""
    monkeypatch.setenv("RAGLEX_DATA_DIR", str(tmp_path))
    from raglex.config import Config
    from raglex.core.models import DocType, ExtractedVia, Record
    from raglex.facade import Facade

    f = Facade(Config.from_env())
    with f._open() as (cat, _rs, ts):
        for sid, title, extra in (
                ("ECLI:CE:ECHR:2015:0001", "CASE OF TSANOVA-GECHEVA v. BULGARIA",
                 {"docname": "CASE OF TSANOVA-GECHEVA v. BULGARIA"}),
                ("ECLI:CE:ECHR:2015:0002", "CASE OF VAN PELT v. FRANCE", {}),
                ("ECLI:CE:ECHR:2015:0003", "Hearing Waite and Kennedy v. Germany", {})):
            rec = Record(source="echr", stable_id=sid, doc_type=DocType.JUDGMENT,
                         title=title, court="echr", text=title, raw_bytes=sid.encode(),
                         extracted_via=ExtractedVia.STRUCTURED, extra=extra)
            rec.ensure_payload_hash()
            cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, rec.text)))

    counted = f.recase_shouty_titles(dry_run=True)
    assert counted["recased"] == 2 and counted["unchanged"] == 1

    res = f.recase_shouty_titles()
    assert res["recased"] == 2
    with f._open() as (cat, _rs, _ts):
        assert cat.get_document("ECLI:CE:ECHR:2015:0001")["title"] == (
            "Case of Tsanova-Gecheva v. Bulgaria")
        assert cat.get_document("ECLI:CE:ECHR:2015:0002")["title"] == (
            "Case of Van Pelt v. France")
        # already-cased title untouched
        assert cat.get_document("ECLI:CE:ECHR:2015:0003")["title"] == (
            "Hearing Waite and Kennedy v. Germany")
        # HUDOC already keeps the raw spelling; only the one without it gains a copy
        assert "title_original" not in cat.document_meta("ECLI:CE:ECHR:2015:0001")
        assert cat.document_meta("ECLI:CE:ECHR:2015:0002")["title_original"] == (
            "CASE OF VAN PELT v. FRANCE")
        assert cat.get_document("ECLI:CE:ECHR:2015:0001")["added_by"] != "user"

    # idempotent: a second pass finds nothing left to do
    assert f.recase_shouty_titles()["recased"] == 0
