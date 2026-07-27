from raglex.adapters.ie_tax_appeals import (
    parse_determination_detail,
    parse_determinations_page,
    parse_reference,
)
from raglex.citations.extractor import extract_citations


def test_parse_tax_appeal_reference_and_listing():
    assert parse_reference("79TACD2026 - Vehicle Registration Tax") == (
        "79TACD2026", 2026, 79
    )
    page = """<a href="/en/determinations/79tacd2026-vehicle-registration-tax">
      79TACD2026 - Vehicle Registration Tax</a>"""
    row = parse_determinations_page(page)[0]
    assert row["tax_type"] == "Vehicle Registration Tax"
    assert row["landing_url"].startswith("https://www.taxappeals.ie/")


def test_parse_tax_appeal_detail():
    detail = parse_determination_detail("""
      <p>By Tax_Appeals Wednesday, 15th July 2026 Filed under:</p>
      <a href="/_fileupload/Determinations/2026/79TACD2026.pdf">download</a>
    """)
    assert str(detail["published"]) == "2026-07-15"
    assert detail["pdf_url"].endswith("/79TACD2026.pdf")


def test_tacd_compact_citation_resolves():
    citation = next(c for c in extract_citations("The approach in 79TACD2026 applies."))
    assert citation.candidate_id == "tacd/2026/79"
