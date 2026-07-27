from raglex.adapters.ca_lexum import (
    CanadianLexumHTTP,
    neutral_slug,
    parse_decision_html,
    parse_recent_additions,
    parse_rss,
)


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


def test_neutral_slug_normalises_french_federal_court_codes():
    assert neutral_slug("Example, 2026 CF 42") == "fc/2026/42"
    assert neutral_slug("Exemple, 2026 CAF 7") == "fca/2026/7"


def test_parse_fca_recent_additions():
    raw = b"""<ul class="collectionItemList"><li>
      <div class="info" lang="fr"><span class="title">
      <a href="/fca-caf/decisions/fr/item/521881/index.do">Pindi c. Canada</a>
      </span><span class="citation">2026 CAF 129</span>
      <span class="publicationDate">2026-07-09</span></div>
    </li></ul>"""
    row = parse_recent_additions(raw, "https://decisia.lexum.com/fca-caf/en/ann.do")[0]
    assert row["item_id"] == "521881"
    assert row["language"] == "fr"
    assert neutral_slug(row["title"]) == "fca/2026/129"


class _Response:
    status_code = 200
    content = b"<rss/>"


class _Session:
    def get(self, url, **kwargs):
        return _Response()


def test_canadian_lexum_http_accepts_injected_chrome_session():
    response = CanadianLexumHTTP(
        "ca-test", min_interval=0, session=_Session()
    ).get("https://norma.lexum.com/feed")
    assert response.content == b"<rss/>"


class _BlockedResponse:
    status_code = 403
    content = b"blocked"


class _BlockedSession:
    def get(self, url, **kwargs):
        return _BlockedResponse()


class _StealthPage:
    status = 200
    html = "<html>decision</html>"


class _Stealth:
    def fetch(self, url, **kwargs):
        return _StealthPage()


def test_canadian_lexum_http_escalates_blocked_html_to_scrapling():
    response = CanadianLexumHTTP(
        "ca-test", min_interval=0, session=_BlockedSession(), fetcher=_Stealth()
    ).get("https://norma.lexum.com/decision")
    assert response.content == b"<html>decision</html>"
