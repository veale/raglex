import io
import zipfile

from raglex.adapters.au_sa_legislation import parse_sa_xml, unpack_sa_release


XML = b"""<?xml version="1.0"?><!DOCTYPE exdoc SYSTEM "Exchange.dtd">
<exdoc title="Example Act&amp;#x00A0;2024" year="2024" number="7"
 enact.or.made.date="2024-03-01" first.valid.date="2026-07-01" doc.class="act">
 <head><heading>Example Act 2024</heading></head>
 <content><level type="clause"><head><no>1</no><heading>Purpose</heading></head>
 <block><txt>This Act applies to the example and contains sufficiently long
 consolidated legislative text for extraction and citation linking.</txt></block>
 </level></content></exdoc>"""


def _release() -> bytes:
    inner_buffer = io.BytesIO()
    with zipfile.ZipFile(inner_buffer, "w") as inner:
        inner.writestr("Example Act 2024/Current/2024.7.xml", XML)
    outer_buffer = io.BytesIO()
    with zipfile.ZipFile(outer_buffer, "w") as outer:
        outer.writestr("A.zip", inner_buffer.getvalue())
    return outer_buffer.getvalue()


def test_parse_sa_xml_current_consolidation():
    parsed = parse_sa_xml(XML)
    assert parsed["stable_id"] == "au/sa/act/2024/7"
    assert parsed["title"] == "Example Act 2024"
    assert parsed["consolidated"] == "2026-07-01"
    assert "sufficiently long consolidated legislative text" in parsed["text"]


def test_unpack_nested_release():
    rows = list(unpack_sa_release(_release()))
    assert len(rows) == 1
    assert rows[0][0].endswith("2024.7.xml")
    assert rows[0][2]["number"] == 7
