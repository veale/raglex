from raglex.adapters.eu_edps import EUDPR, parse_edps_page


PAGE = """
<article class="node node--type-edpsweb-publication node--view-mode-teaser">
 <div class="edpsweb-publication-date"><div>23</div><div>Jul</div><div>2026</div></div>
 <h3 class="node__title"><a href="/data-protection/our-work/publications/opinions/example">
 EDPS Opinion 12/2026 on an Example Regulation</a></h3>
 <div class="field--name-field-edpb-files">
  <a href="/system/files/2026-07/example_en.pdf" type="application/pdf">PDF</a>
 </div>
</article>
"""


def test_parse_edps_opinion_card():
    row = parse_edps_page(PAGE)[0]
    assert row["stable_id"] == "eu/edps/opinion/example"
    assert str(row["published"]) == "2026-07-23"
    assert row["pdf_url"] == "https://www.edps.europa.eu/system/files/2026-07/example_en.pdf"
    assert EUDPR == "32018R1725"
