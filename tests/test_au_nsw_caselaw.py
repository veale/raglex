"""NSW Caselaw live adapter — incremental discovery (newest-first, watermark stop),
neutral-citation identity, and judgment-body fetch. Network-free (fake client)."""

from __future__ import annotations

import json

from raglex.adapters.au_nsw_caselaw import NSWCaselawAdapter
from raglex.core.models import DocType


class _Resp:
    def __init__(self, *, text: str = "", content: bytes = b"", payload=None):
        self.text = text
        self.content = content
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    """Serves the browse-list JSON pages and per-decision HTML by URL."""

    def __init__(self, pages: dict[int, list[dict]], decisions: dict[str, str]):
        self.pages = pages
        self.decisions = decisions
        self.min_interval = 0

    def get(self, url: str, **kw):
        if "/browse/list" in url:
            page = int(url.split("page=")[1])
            return _Resp(payload={"searchableDecisions": self.pages.get(page, [])})
        if "/decision/" in url:
            did = url.rsplit("/", 1)[-1]
            return _Resp(text=self.decisions.get(did, ""))
        return _Resp(text="")


def _entry(did, mnc, title, dtext, restricted=False):
    return {"id": did, "mnc": mnc, "title": title,
            "decisionDateText": dtext, "restricted": restricted}


P0 = [
    _entry("aaa", "[2024] NSWSC 900", "New v Case", "3 July 2024"),
    _entry("bbb", "[2024] NSWCA 88", "Appeal Matter", "1 July 2024"),
    _entry("sec", "", "Decision restricted", "1 July 2024", restricted=True),
]
P1 = [
    _entry("ccc", "[2024] NSWSC 800", "Older v Thing", "10 June 2024"),
]

DECISIONS = {
    "aaa": '<html><body><div class="judgment"><p>1. The plaintiff succeeds.</p></div></body></html>',
    "bbb": '<html><body><div class="judgment"><p>Appeal dismissed.</p></div></body></html>',
    "ccc": '<html><body><div class="judgment"><p>Earlier reasons.</p></div></body></html>',
}


def _adapter():
    return NSWCaselawAdapter(client=_FakeClient({0: P0, 1: P1}, DECISIONS))


def test_identity_matches_oalc_slug():
    stubs = list(_adapter().discover(None))
    ids = {s.stable_id for s in stubs}
    assert "nswsc/2024/900" in ids       # same slug au_case_slug mints
    assert "nswca/2024/88" in ids
    assert "nswsc/2024/800" in ids


def test_restricted_decision_skipped():
    stubs = list(_adapter().discover(None))
    assert all(s.hints.get("id") != "sec" for s in stubs)
    assert all("restricted" not in (s.title or "").lower() for s in stubs)


def test_incremental_stops_at_watermark():
    # cursor between the 1 July and 10 June decisions → only the newer two
    stubs = list(_adapter().discover("2024-06-15"))
    ids = {s.stable_id for s in stubs}
    assert ids == {"nswsc/2024/900", "nswca/2024/88"}


def test_court_bucket_from_neutral_citation():
    stubs = {s.stable_id: s for s in _adapter().discover(None)}
    assert stubs["nswsc/2024/900"].court == "nswsc"
    assert stubs["nswca/2024/88"].court == "nswca"


def test_fetch_builds_judgment_with_alias():
    ad = _adapter()
    stub = next(s for s in ad.discover(None) if s.stable_id == "nswsc/2024/900")
    rec = ad.fetch(stub)
    assert rec.doc_type is DocType.JUDGMENT
    assert rec.stable_id == "nswsc/2024/900"
    assert rec.court == "nswsc"
    assert "plaintiff succeeds" in rec.text
    assert rec.decision_date.isoformat() == "2024-07-03"
    assert rec.extra["neutral_citation"] == "[2024] NSWSC 900"
    assert "[2024] nswsc 900" in rec.extra["aliases"]
    assert rec.extra["jurisdiction"] == "new_south_wales"


def test_pdf_only_decision_flags_ocr_when_empty():
    html = ('<html><body>Reasons redacted. '
            '<a href="/asset/xyz">See Attachment (PDF)</a></body></html>')
    client = _FakeClient({0: [_entry("pdfid", "[2024] NSWSC 5", "PDF v Only", "2 July 2024")], 1: []},
                         {"pdfid": html})
    # the /asset/xyz PDF fetch returns empty bytes → no extractable text → needs_ocr
    ad = NSWCaselawAdapter(client=client)
    stub = next(iter(ad.discover(None)))
    rec = ad.fetch(stub)
    assert rec is not None
    assert rec.extra.get("needs_ocr") is True
    assert rec.raw_ext == "pdf"
