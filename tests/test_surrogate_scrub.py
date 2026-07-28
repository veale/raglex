"""Unencodable surrogates from a broken PDF CMap must not sink a whole run."""

from __future__ import annotations

import pytest

from raglex.core.text import scrub_surrogates
from raglex.extraction.extractors import PdfExtractor
from raglex.storage.textstore import TextStore

LONE = "the 1968 Act \ud83d applies"
PAIR = "the 1968 Act 😀 applies"


def test_clean_text_passes_through_unchanged():
    assert scrub_surrogates("Law Com No 401 — Article 8 ECHR") == (
        "Law Com No 401 — Article 8 ECHR")


def test_lone_surrogate_becomes_replacement_char():
    out = scrub_surrogates(LONE)
    assert out == "the 1968 Act � applies"
    out.encode("utf-8")  # the whole point: now encodable


def test_valid_pair_is_joined_back_into_its_astral_character():
    assert scrub_surrogates(PAIR) == "the 1968 Act \U0001f600 applies"


def test_join_pairs_off_is_strictly_length_preserving():
    # Char offsets (segments, citation spans) are already fixed by this point.
    for text in (LONE, PAIR):
        out = scrub_surrogates(text, join_pairs=False)
        assert len(out) == len(text)
        out.encode("utf-8")


def test_pdf_page_spans_match_the_scrubbed_text():
    pypdf = pytest.importorskip("pypdf")

    class _Page:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _Reader:
        pages = [_Page(PAIR), _Page("second page")]

    extractor = PdfExtractor()
    original = pypdf.PdfReader
    pypdf.PdfReader = lambda *a, **k: _Reader()  # noqa: E731
    try:
        out = extractor.extract(b"%PDF-1.4", ext="pdf", mime="application/pdf")
    finally:
        pypdf.PdfReader = original

    out.text.encode("utf-8")
    for _page, start, end in out.page_spans:
        assert out.text[start:end].strip()
    assert out.text[out.page_spans[1][1]:out.page_spans[1][2]] == "second page"


def test_textstore_stores_a_document_carrying_a_lone_surrogate(tmp_path):
    store = TextStore(tmp_path)
    store.put("a" * 40, LONE)
    assert store.get("a" * 40) == "the 1968 Act � applies"
