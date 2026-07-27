from raglex.adapters.au_wa_legislation import parse_wa_document, parse_wa_index


INDEX = b"""
<table class="if"><tbody><tr>
<td><a class="citation alive" href="law_a1.html&amp;view=consolidated">
Aboriginal Affairs Planning Authority Act 1972</a></td>
<td>024 of 1972</td>
<td><a href="RedirectURL?OpenAgent&amp;query=mrdoc_49436.pdf">PDF</a></td>
<td><a href="RedirectURL?OpenAgent&amp;query=mrdoc_49436.docx">Word</a></td>
<td><a href="RedirectURL?OpenAgent&amp;query=mrdoc_49436.htm">HTML</a></td>
</tr></tbody></table>
"""


def test_parse_wa_index_uses_instrument_identity_and_current_rendition():
    rows = parse_wa_index(INDEX, kind="act")
    assert rows[0]["stable_id"] == "au/wa/act/1972/24"
    assert rows[0]["mrdoc_id"] == "49436"
    assert rows[0]["raw_url"].endswith("mrdoc_49436.htm")


def test_parse_wa_document_keeps_sections():
    text, segments, _ = parse_wa_document(
        b"<html><body><p>As at 12 July 2026</p><p>1. Short title</p>"
        b"<p>This Act may be cited as the Example Act.</p></body></html>"
    )
    assert "Example Act" in text
    assert segments[0].label == "1. Short title"
