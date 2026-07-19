"""BAILII RTF stripper — the pure regex parser in raglex.formats.rtf. Network-free."""

from __future__ import annotations

from raglex.formats.rtf import strip_rtf


def _strip(s: str) -> str:
    return strip_rtf(s.encode("cp1252"))


def test_digits_after_a_toggle_are_document_text_not_a_parameter():
    # A space before the digits means they are text, not the control word's parameter:
    # "{\b 1985}" is the bold text "1985". The stripper must keep years / paragraph
    # numbers / amounts that follow a bold/italic/underline toggle.
    out = _strip(r"{\rtf1 The case {\b 1985} concerned {\i 42} sheep at para \b 55.}")
    assert "1985" in out and "42" in out
    assert "para 55" in out.replace("  ", " ")


def test_glued_control_parameter_is_still_consumed():
    # A parameter glued to the control word ("\fs24") is the real parameter and must be
    # stripped along with the control, leaving no stray "24".
    out = _strip(r"{\rtf1\fs24 Hello \b0 world}")
    assert out.strip() == "Hello world"
    assert "24" not in out


def test_hex_char_escape_decoded():
    assert _strip(r"caf\'e9 society") == "café society"


def test_unicode_escape_with_hex_fallback_not_duplicated():
    # \u233 é, with a \'e9 low-ANSI fallback that must be swallowed as part of the
    # escape rather than separately decoded (which would duplicate the character).
    assert _strip(r"caf\u233\'e9 society") == "café society"
    # the plain "?" fallback form still works
    assert _strip(r"caf\u233? society") == "café society"
