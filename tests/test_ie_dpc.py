from raglex.adapters.ie_dpc import (
    GDPR,
    IE_DPA_2018,
    dpc_article_relations,
    parse_dpc_detail,
    parse_dpc_listing,
)


def test_dpc_listing_detail_and_explicit_provision_anchors():
    listing = b"""<div class="views-row"><h3><a
      href="/en/dpc-guidance/decisions/inquiry-example">Inquiry Example</a></h3></div>"""
    assert parse_dpc_listing(listing)[0]["url"].endswith("/inquiry-example")
    detail = b"""<div class="field--name-body"><h1>Inquiry Example</h1>
      <div class="block-tags"><p><strong>Articles:</strong>
      <a href="?decision_articles=1">5</a><a href="?decision_articles=2">S 79</a></p></div>
      <div class="block-tags"><p><strong>DPC Reference:</strong><span>IN-1</span></p></div>
      <div class="block-tags"><p><strong>Decision Date:</strong><span>30 April 2026</span></p></div>
      <div class="field--name-body"><p>Decision body under the GDPR.</p>
      <a href="/decision.pdf">PDF</a></div></div>"""
    parsed = parse_dpc_detail(detail)
    assert parsed["articles"] == ["5", "S 79"]
    rels = dpc_article_relations(parsed["articles"])
    assert [(r.dst_id, r.dst_anchor) for r in rels] == [
        (GDPR, "Article 5"), (IE_DPA_2018, "section 79"),
    ]
