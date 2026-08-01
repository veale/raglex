"""The metadata a standalone import carries, and what the rest of the app does with it.

The load-bearing claim of the feature is that a hand-uploaded document is treated as a
document OF ITS JURISDICTION — the same citation grammar gates, the same facets — and
that claim rests entirely on the source key. So it is asserted here against the real
consumers of that key rather than against the importer's own return value.
"""

from __future__ import annotations

from datetime import date

from raglex.citations import stage
from raglex.core.models import DocType
from raglex.facade import Facade
from raglex.imports import import_file
from raglex.imports.service import (
    JURISDICTIONS,
    _section_segments,
    import_source_key,
    jurisdiction_of_source,
    structure_segments,
)
from raglex.storage import RawStore, TextStore


def _stores(tmp_path):
    return RawStore(tmp_path / "raw"), TextStore(tmp_path / "text")


# Both methods under test are pure lookups over class-level tables, so a bare instance
# is enough — and keeps the test off the disk and the DB entirely.
_facade = Facade.__new__(Facade)


# -- the source key IS the jurisdiction -------------------------------------
def test_every_import_jurisdiction_reaches_the_label_the_app_shows():
    """A code the form offers must bucket to the name the facets, filters and exports
    use — otherwise an import lands in "Other" and silently leaves the jurisdiction."""
    for code, label in JURISDICTIONS:
        source = import_source_key(code)
        assert _facade._jurisdiction_of(source) == label, code
        assert jurisdiction_of_source(source) == code


def test_unknown_jurisdiction_falls_back_rather_than_minting_a_source():
    assert import_source_key("zz") == "user-import"
    assert import_source_key("") == "user-import"
    assert import_source_key(None) == "user-import"


def test_irish_import_is_gated_as_an_irish_host(catalogue, tmp_path):
    """`_is_irish_host` is what stops "the 2018 Act" in an Irish document binding to the
    UK statute. It keys on the source prefix, so the import must earn it."""
    rs, ts = _stores(tmp_path)
    res = import_file(
        catalogue, rs, ts, data=b"<p>An inquiry under the Data Protection Act 2018.</p>",
        filename="inquiry.html", doc_type=DocType.DECISION, jurisdiction="ie",
    )
    doc = catalogue.get_document(res.stable_id)
    assert res.source == "ie-user-import"
    assert stage._is_irish_host(doc) is True

    plain = import_file(
        catalogue, rs, ts, data=b"<p>Other material.</p>", filename="other.html",
        doc_type=DocType.DECISION,
    )
    assert stage._is_irish_host(catalogue.get_document(plain.stable_id)) is False


def test_us_import_unlocks_the_us_reporter_grammar(catalogue, tmp_path):
    """A single-letter reporter series is a citation in American material and page
    notation almost everywhere else — the allowance is keyed on "us-"."""
    rs, ts = _stores(tmp_path)
    res = import_file(
        catalogue, rs, ts, data=b"<p>See 12 F. 13 (1881).</p>", filename="brief.html",
        doc_type=DocType.JUDGMENT, jurisdiction="us",
    )
    doc = catalogue.get_document(res.stable_id)
    assert stage._is_us_source(doc) is True
    assert stage._allows_us_reporters(doc) is True


def test_manual_import_sources_are_named_not_prettified(tmp_path):
    label = _facade.source_label("uk-user-import")
    assert label == "Manual imports (United Kingdom)"
    assert _facade.source_label("user-import") == "Manual imports"


# -- the metadata itself ----------------------------------------------------
def test_import_carries_court_date_citation_and_tags(catalogue, tmp_path):
    rs, ts = _stores(tmp_path)
    res = import_file(
        catalogue, rs, ts, data=b"<p>A judgment.</p>", filename="j.html",
        doc_type=DocType.JUDGMENT, title="Smith v Jones", jurisdiction="uk",
        court="UKSC", decision_date=date(2024, 3, 1), citation="[2024] UKSC 12",
        tags=["seminar", "  ", "reading-list"],
    )
    doc = catalogue.get_document(res.stable_id)
    assert doc["title"] == "Smith v Jones"
    assert doc["court"] == "UKSC"
    assert str(doc["decision_date"])[:10] == "2024-03-01"
    assert res.tags == ("seminar", "reading-list")     # blanks dropped, not stored
    # The citation the operator typed is how the corpus already refers to it, so a
    # pending edge keyed on that citation must now land on this upload.
    assert catalogue.find_document_id("[2024] UKSC 12") == res.stable_id


def test_title_defaults_to_the_filename(catalogue, tmp_path):
    rs, ts = _stores(tmp_path)
    res = import_file(catalogue, rs, ts, data=b"<p>x</p>", filename="handbook.html")
    assert res.title == "handbook.html"


# -- best-effort structure --------------------------------------------------
class _Extracted:
    def __init__(self, text, page_spans=()):
        self.text = text
        self.page_spans = list(page_spans)
        self.engine = "test"
        self.needs_ocr = False


_JUDGMENT = "\n\n".join(f"[{n}] Paragraph {n} of the judgment, at some length." for n in range(1, 9))
_GUIDANCE = "\n\n".join(f"{n}. Where a controller does a thing, it must do it well." for n in range(1, 9))
_STATUTE = "\n\n".join(f"Section {n}\nA provision about several matters." for n in range(1, 6))


def test_paragraph_parser_reads_both_numbering_styles():
    """One dropdown option, both conventions: the bracketed judgment form and the
    dotted form guidance uses. That is what "best effort" has to mean here."""
    assert [s.label for s in structure_segments(
        "paragraphs", DocType.COMMENTARY, _Extracted(_JUDGMENT))][:3] == ["[1]", "[2]", "[3]"]
    assert [s.label for s in structure_segments(
        "paragraphs", DocType.COMMENTARY, _Extracted(_GUIDANCE))][:3] == ["1.", "2.", "3."]


def test_section_parser_needs_an_ascending_run():
    assert [s.label for s in _section_segments(_STATUTE)][:2] == ["Section 1", "Section 2"]
    # A passing mention in prose is not a structure.
    assert _section_segments("As held in Article 6 the controller must act. See also s 3.") == []


def test_auto_uses_the_document_type_and_falls_back_to_pages():
    judgment = structure_segments("auto", DocType.JUDGMENT, _Extracted(_JUDGMENT))
    assert [s.label for s in judgment][:2] == ["[1]", "[2]"]
    guidance = structure_segments("auto", DocType.GUIDANCE, _Extracted(_GUIDANCE))
    assert guidance and guidance[0].kind == "paragraph"
    # Nothing numbered in it → the page anchors survive rather than being lost.
    pages = structure_segments("auto", DocType.JUDGMENT,
                               _Extracted("flat prose", [(1, 0, 10)]))
    assert [s.label for s in pages] == ["p. 1"]


def test_explicit_choices_are_honoured():
    ex = _Extracted(_JUDGMENT, [(1, 0, 20)])
    assert structure_segments("none", DocType.JUDGMENT, ex) == []
    assert [s.label for s in structure_segments("pages", DocType.JUDGMENT, ex)] == ["p. 1"]
    # An unrecognised choice must not silently mean "no structure".
    assert structure_segments("nonsense", DocType.JUDGMENT, ex) != []


def test_structure_choice_is_recorded_on_the_document(catalogue, tmp_path):
    rs, ts = _stores(tmp_path)
    res = import_file(
        catalogue, rs, ts, data=_JUDGMENT.encode(), filename="j.txt",
        doc_type=DocType.JUDGMENT, structure="paragraphs")
    assert res.structure == "paragraphs" and res.segments >= 8
    assert [s.label for s in ts.get_segments(
        catalogue.get_document(res.stable_id)["payload_hash"])][:2] == ["[1]", "[2]"]
