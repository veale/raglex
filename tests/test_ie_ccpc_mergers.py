from types import SimpleNamespace

import pytest

from raglex.adapters.ie_ccpc_mergers import (
    COMPETITION_ACT_2002,
    IrishCCPCMergerAdapter,
    competition_act_relations,
    parse_ccpc_detail,
    parse_ccpc_register,
)
from raglex.core.errors import FetchError
from raglex.core.models import Stub


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


def test_empty_determination_download_is_transient_not_a_pdf_parser_crash():
    class Client:
        def get(self, url):
            if url.endswith(".pdf"):
                return SimpleNamespace(content=b"")
            return SimpleNamespace(content=b'<a href="https://assets.ccpc.ie/x.pdf">Determination</a>')

    stub = Stub(stable_id="ie/ccpc/merger/2024/43", raw_url="https://ccpc.ie/case",
                hints={"reference": "M/24/043"})
    with pytest.raises(FetchError, match="did not return a PDF") as exc:
        IrishCCPCMergerAdapter(client=Client()).fetch(stub)
    assert exc.value.transient is True
