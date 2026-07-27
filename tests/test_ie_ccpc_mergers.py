from raglex.adapters.ie_ccpc_mergers import (
    COMPETITION_ACT_2002,
    competition_act_relations,
    parse_ccpc_detail,
    parse_ccpc_register,
)


REGISTER = r"""
{\"Id\":\"abc\",\"ItemDefaultUrl\":\"/enforcement-and-regulation/mergers/find-a-merger-case/details/acme-target\",\"Title\":\"Acme/Target\",\"TransactionReference\":\"M/26/044\",\"MediaMerger\":false,\"NotificationDate\":\"2026-06-29T15:25:00Z\"}
"""


def test_parse_ccpc_embedded_register():
    row = parse_ccpc_register(REGISTER)[0]
    assert row["stable_id"] == "ie/ccpc/merger/2026/44"
    assert row["reference"] == "M/26/044"
    assert str(row["notification_date"]) == "2026-06-29"


def test_parse_ccpc_detail_and_single_regime_sections():
    detail = parse_ccpc_detail("""
      <main><a href="https://assets.ccpc.ie/m-26-044-determination.pdf">
      Determination PDF</a>
      <p>Monday, 13 July 2026 Determination Issued</p></main>
    """)
    assert detail["determinations"][0].endswith("determination.pdf")
    assert str(detail["issued"]) == "2026-07-13"
    relations = competition_act_relations(
        "Under section 18 a notification was made. Article 4 of the EU Regulation."
    )
    assert relations[0].dst_id == COMPETITION_ACT_2002
    assert relations[1].dst_anchor == "section 18"
