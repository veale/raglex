from raglex.adapters.ca_lexum import neutral_slug, parse_decision_html, parse_rss


def test_parse_lexum_rss_tracks_corrections_not_only_decision_date():
    raw = b"""<rss xmlns:decision="http://lexum.com/decision/"><channel><item>
      <title>Old Case - 2022 SCC 48 - 2022-11-25</title>
      <link>https://decisions.scc-csc.ca/x/en/item/19563/index.do</link>
      <description><![CDATA[Document updated on 2026-07-15]]></description>
      <decision:date>2022-11-25</decision:date>
    </item></channel></rss>"""
    row = parse_rss(raw)[0]
    assert row["changed"] == "2026-07-15"
    assert neutral_slug(row["title"]) == "scc/2022/48"


def test_parse_lexum_html_preserves_native_paragraphs():
    raw = b"""<div id="decisia-document-header"><div class="metadata">
      <h3 class="title">Example v The King</h3><table>
      <tr><td class="label">Date</td><td class="metadata">2026-07-22</td></tr>
      <tr><td class="label">Neutral citation</td><td class="metadata">2026 TCC 138</td></tr>
      </table></div></div><div id="document-content"><div class="documentcontent">
      <p>[<a class="reflex-paragAnchor" name="par1">1</a>] First paragraph.</p>
      <p>[<a class="reflex-paragAnchor" name="par2">2</a>] Applies 2024 SCC 1.</p>
      </div></div>"""
    out = parse_decision_html(raw)
    assert out["title"] == "Example v The King"
    assert out["metadata"]["neutral citation"] == "2026 TCC 138"
    assert [s.label for s in out["segments"]] == ["[1]", "[2]"]
    assert out["text"][out["segments"][1].char_start:].startswith("[ 2 ]")
