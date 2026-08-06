"""Belgian DPA Dispute Chamber decisions: discovery off the register's own search view,
the decision number as identity, and the Belgian citation forms in BOTH languages.

The fixture is a real capture of the register's dispute-chamber filter.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from raglex.adapters.be_gba_decisions import (
    decision_id, decision_stubs, detect_language, parse_dutch_date, result_total,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.citations.dutch import dutch_citations
from raglex.citations.french import french_citations

FIXTURE = Path(__file__).parent / "data" / "be_gba_search.html"


def _html() -> bytes:
    return FIXTURE.read_bytes()


def test_registered_as_administrative_not_guidance():
    """A Dispute Chamber ruling is a regulator determination. Filed as guidance it would
    sit among the authority's explanatory material, where the enforcement record is
    exactly what you cannot then find."""
    key = "be-gba-decisions"
    assert key in ADAPTERS and key in SOURCE_INFO
    assert SOURCE_INFO[key].kind == "administrative"
    assert SOURCE_INFO[key].jurisdiction == "BE"
    assert INCREMENTAL_MODE[key] == "early-stop"
    assert ADAPTERS[key]().source == key


def test_discovery_reads_the_register_view():
    total = result_total(_html())
    assert total and total > 200          # the filter held 294 at capture
    stubs = decision_stubs(_html())
    assert len(stubs) == 50               # one page at l=50
    first = {s.stable_id: s for s in stubs}
    assert "be/gba/geschillenkamer/102-2026" in first
    got = first["be/gba/geschillenkamer/102-2026"]
    assert got.hint_date == date(2026, 5, 12)
    assert got.hints["decision_number"] == "102/2026"
    assert got.hints["decision_kind"] == "substance"
    assert got.raw_url.endswith(".pdf")   # the card links straight to the document
    assert got.court == "dpa-be"


def test_settlement_decisions_are_kept_and_labelled():
    """The same filter carries Schikkingsbeslissingen. They are decisions of the same
    chamber, so they are stored — with their own kind, not silently as substance."""
    kinds = {s.hints["decision_kind"] for s in decision_stubs(_html())}
    assert "settlement" in kinds and "substance" in kinds


def test_identity_is_the_decision_number_not_the_filename():
    """The register has spelt one ruling's file more than one way; keying on the filename
    would hold the same decision twice."""
    assert decision_id("102", "2026") == "be/gba/geschillenkamer/102-2026"
    assert decision_id("099", "2026") == decision_id("99", "2026")


def test_dutch_date():
    assert parse_dutch_date("12 mei 2026") == date(2026, 5, 12)
    assert parse_dutch_date("8 mei 2026") == date(2026, 5, 8)
    assert parse_dutch_date("geen datum") is None


def test_language_comes_from_the_document_not_the_listing():
    """Every listing title is Dutch, but the PDF is in the language of the procedure —
    decision 102/2026 is titled "Beslissing ten gronde" and is a French document."""
    assert detect_language(
        "Chambre Contentieuse Décision quant au fond 102/2026 ... du RGPD ...") == "fr"
    assert detect_language(
        "Geschillenkamer Beslissing ten gronde 99/2026 ... de AVG ...") == "nl"


def _pins(cites, method_prefix: str) -> set[tuple[str, str]]:
    return {(str(c.candidate_id), str(c.pinpoint)) for c in cites
            if c.method.startswith(method_prefix)}


def test_belgian_dutch_forms():
    """Belgium subdivides with § and °, and writes GDPR articles with dots. Neither is
    how the Netherlands drafts, so neither matched before."""
    got = _pins(dutch_citations("Gelet op artikel 100, § 1, 9° WOG"), "nl_law")
    assert any(pin == "Artikel 100, § 1, 9°" for _, pin in got)
    assert ("32016R0679", "Article 6(1)(f)") in _pins(
        dutch_citations("in strijd met artikel 6.1.f) AVG"), "be_avg")
    assert ("32016R0679", "Article 58(2)(i)") in _pins(
        dutch_citations("artikel 58.2.i) AVG"), "be_avg")


def test_dutch_netherlands_forms_still_work():
    """The Belgian additions sit beside the Dutch ones; they must not displace them."""
    assert ("32016R0679", "Article 6(1)(f)") in _pins(
        dutch_citations("artikel 6, eerste lid, aanhef en onder f, van de AVG"), "nl_avg")


def test_belgian_french_forms():
    """Roughly half the register is French, and the generic French grammar recognised
    almost none of it: 'Article 5.1.f du RGPD' is the EU institutional style, not
    France's 'article 5, paragraphe 1, point f'."""
    cites = french_citations(
        "Article 5.1.f du RGPD, article 4.12) du RGPD, et l'article 62, § 1er de la LCA "
        "ainsi que l'article 10/1 §2 de la LTD")
    rgpd = _pins(cites, "be_fr_rgpd")
    assert ("32016R0679", "Article 5(1)(f)") in rgpd
    assert ("32016R0679", "Article 4(12)") in rgpd
    laws = {pin for _, pin in _pins(cites, "be_fr_law")}
    assert "Article 62, § 1" in laws
    assert "Article 10/1, § 2" in laws


def test_belgian_article_numbering_shapes():
    """Belgium numbers by Book ("XII.13"), inserts articles with a slash ("44/1"), and its
    codes run past three digits ("1382"). None of those matched the Netherlands-shaped
    pattern, so the acronyms alone would have been dead weight."""
    def pin(text, prefix="nl_law"):
        return {p for _, p in _pins(dutch_citations(text), prefix)}
    assert "Artikel XII.13" in pin("artikel XII.13 WER")
    assert "Artikel 44/1" in pin("artikel 44/1 WPA")
    assert "Artikel 1382" in pin("artikel 1382 Strafwetboek")
    assert "Artikel 8, § 1" in pin("artikel 8, § 1 Camerawet")
    fr = {p for _, p in _pins(french_citations("article VI.110 du CDE"), "be_fr_law")}
    assert "Article VI.110" in fr


def test_widening_did_not_break_the_netherlands_forms():
    assert ("32016R0679", "Article 6(1)(f)") in _pins(
        dutch_citations("artikel 6, eerste lid, aanhef en onder f, van de AVG"), "nl_avg")
    assert any(pin == "Artikel 100, § 1, 9°" for _, pin in
               _pins(dutch_citations("artikel 100, § 1, 9° WOG"), "nl_law"))
