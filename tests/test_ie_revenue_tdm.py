from raglex.adapters.ie_revenue_tdm import parse_tdm_index


PAGE = """
<ul class="documents-list"><li>
 <a class="pdf" href="/en/tax-professionals/tdm/income-tax/part-01/01-00-02.pdf">
 Part 01-00-02</a><span class="spanDesTDM">Interpretation of Corporation Tax Acts</span>
 <p class="older-versions">
  <a href="/en/tax-professionals/tdm/income-tax/part-01/01-00-02-20260212110911.pdf">
  12-Feb-2026</a>
 </p>
</li></ul>
<a href="/en/tax-professionals/tdm/income-tax/part-02/index.aspx">Part 2</a>
"""


def test_parse_revenue_manual_and_revision_signal():
    manuals, children = parse_tdm_index(
        PAGE, "https://www.revenue.ie/en/tax-professionals/tdm/income-tax/index.aspx"
    )
    assert manuals[0]["stable_id"] == "ie/revenue-tdm/income-tax/part-01/01-00-02"
    assert str(manuals[0]["changed"]) == "2026-02-12"
    assert "Interpretation of Corporation Tax Acts" in manuals[0]["title"]
    assert children[0].endswith("/part-02/index.aspx")
