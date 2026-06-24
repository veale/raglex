from __future__ import annotations

import json

from raglex.adapters.echr import ECHRAdapter, appno_from_ecli, parse_body_html
from raglex.citations import extract_citations
from raglex.citations.snowball import _classify

_RESULTS = json.dumps({"resultcount": 2, "results": [
    {"columns": {"itemid": "001-99999", "docname": "[legal summary] X v Y", "doctype": "CLIN", "ecli": ""}},
    {"columns": {"itemid": "001-210077", "ecli": "ECLI:CE:ECHR:2021:0525JUD005817013",
                 "appno": "58170/13;62322/14", "docname": "CASE OF BIG BROTHER WATCH v. UK",
                 "doctype": "HEJUD", "judgementdate": "25/05/2021 00:00:00", "languageisocode": "ENG"}},
]}).encode()
_HTML = (b"<html><body><p>I. PROCEDURE</p><p>1. The case originated in an application.</p>"
         b"<p>THE FACTS</p><p>2. The applicants are journalists.</p>"
         b"<p>THE LAW</p><p>3. The Court considers Article 8.</p>"
         b"<p>FOR THESE REASONS, THE COURT</p><p>Holds that there has been a violation.</p></body></html>")


class _FakeClient:
    def get(self, url, **kw):
        class R:
            content = _HTML if "conversion" in url else _RESULTS
        return R()


def test_appno_from_ecli():
    assert appno_from_ecli("ECLI:CE:ECHR:2021:0525JUD005817013") == "58170/13"
    assert appno_from_ecli("ECLI:CE:ECHR:1975:0221JUD000445170") == "4451/70"


def test_echr_adapter_resolves_by_ecli_and_appno_to_full_judgment():
    for ident in ("58170/13", "ECLI:CE:ECHR:2021:0525JUD005817013", "001-210077"):
        ad = ECHRAdapter(ids=ident, client=_FakeClient())
        stub = next(iter(ad.discover(None)))
        assert stub.stable_id == "ECLI:CE:ECHR:2021:0525JUD005817013"  # the judgment, not the summary
        rec = ad.fetch(stub)
        labels = {s.label for s in rec.segments}
        assert rec.court == "echr" and "violation" in rec.text
        # segmented on the numbered paragraphs (the § citable units) + operative part
        assert "1" in labels and "Operative part" in labels
        assert any(s.kind == "paragraph" for s in rec.segments)
        assert rec.extra["appno"].startswith("58170/13")


def test_echr_grammars_and_routing():
    # application number (the resolvable key) routes to the HUDOC adapter
    appno = next(c for c in extract_citations("Handyside v United Kingdom, Application no. 5493/72")
                 if c.method == "echr_appno")
    assert appno.candidate_id == "5493/72"
    assert _classify("5493/72", "case") == ("ECHR application no.", "CoE", "echr")
    # an ECHR ECLI routes to the adapter too
    assert _classify("ECLI:CE:ECHR:1975:0221JUD000445170", "case")[2] == "echr"
    # EHRR cited WITH a case name → captured as an "echr:<name>" candidate, routed to the
    # echr adapter for a HUDOC docname (name) search; classified as a by-name ECHR case.
    named = next(c for c in extract_citations("Osman v United Kingdom (2000) 29 EHRR 245")
                 if c.method == "echr_report")
    assert named.candidate_id == "echr:Osman v United Kingdom"
    assert _classify(named.candidate_id, "case") == ("ECHR case (by name)", "CoE", "echr")
    # EHRR with NO recoverable name stays a candidate-less "maybe"
    assert all(c.candidate_id is None for c in extract_citations("see (2010) 51 EHRR 10 alone"))
    # § paragraph pinpoint attaches to the app-number citation
    pin = next(c for c in extract_citations("Application no. 4451/70, § 35") if c.candidate_id == "4451/70")
    assert pin.pinpoint == "para 35"


def test_echr_appno_resilient_to_surface_forms_and_traps():
    def app(text):
        return [c.candidate_id for c in extract_citations(text) if c.method == "echr_appno"]
    # OSCOLA "App no" (no full stop), Bluebook "App. No.", short number, joined set, [GC]/(dec.)
    assert app("App no 47940/99 (ECtHR, 20 July 2004)") == ["47940/99"]
    assert app("App. No. 60561/14") == ["60561/14"]
    assert app("D.D. v France (striking out), no. 3/02") == ["3/02"]
    assert app("nos. 16064/90 and 2 others") == ["16064/90"]   # first of a joined set
    assert app("(dec.) [GC], no. 36022/97") == ["36022/97"]
    # MUST NOT grab EU instruments ("No 1/2003", "No 17/62") or Series A numbers
    assert app("Regulation No 1/2003") == []
    assert app("Council Regulation No 17/62") == []
    assert app("Series A no. 139") == []
