"""NL BWB bulk discovery — the multi-part 7z drop, zip index skipping, and the plain
folder path (no regression). Extraction is monkeypatched (no 7z binary needed)."""

from __future__ import annotations

import zipfile

from raglex.adapters import nl_legislation as nl
from raglex.adapters.nl_legislation import NLLegislationAdapter

# A minimal BWB toestand XML the identity reader recognises (BWB id + validity date).
BWB_XML = (
    b'<?xml version="1.0"?><toestand bwb-id="BWBR0011468">'
    b'<geldigheidsdatum>2026-01-01</geldigheidsdatum>'
    b'<wetgeving><titel>Test Act</titel></wetgeving></toestand>'
)


def test_plain_xml_folder_still_works(tmp_path):
    (tmp_path / "a.xml").write_bytes(BWB_XML)
    ad = NLLegislationAdapter(path=str(tmp_path))
    stubs = list(ad.discover(None))
    assert [s.hints["bwbid"] for s in stubs] == ["BWBR0011468"]
    assert stubs[0].hints["geldig"] == "2026-01-01"


def test_bwbidlist_zip_is_skipped(tmp_path):
    # an index zip must not be mined for toestanden
    with zipfile.ZipFile(tmp_path / "BWBIDLIST.zip", "w") as zf:
        zf.writestr("BWBIDList.xml", b"<list><id>BWBR9999999</id></list>")
    with zipfile.ZipFile(tmp_path / "data.zip", "w") as zf:
        zf.writestr("doc.xml", BWB_XML)
    ad = NLLegislationAdapter(path=str(tmp_path))
    ids = [s.hints["bwbid"] for s in ad.discover(None)]
    assert ids == ["BWBR0011468"]        # only the real toestand, not the index id


def test_multipart_7z_is_extracted_once_and_enumerated(tmp_path, monkeypatch):
    # lay down split 7z volumes + the index (contents are irrelevant — extraction is faked)
    for suf in ("001", "002", "003"):
        (tmp_path / f"BWB_20260716_143624.7z.{suf}").write_bytes(b"7z-volume")
    (tmp_path / "BWBIDLIST.zip").write_bytes(b"PK")

    calls = {"n": 0}

    def fake_extract(volume001, dest):
        calls["n"] += 1
        assert volume001.name == "BWB_20260716_143624.7z.001"
        # the real tree: <BWBID>/<date>_0/xml/{main, manifest, assets}, two toestanden
        for d in ("2026-01-01", "2020-01-01"):
            xdir = dest / "BWBR0011468" / f"{d}_0" / "xml"
            xdir.mkdir(parents=True)
            (xdir / "BWBR0011468.xml").write_bytes(BWB_XML)   # the main content
            (xdir / "manifest.xml").write_bytes(b"<manifest/>")  # must be skipped
            (xdir / "9000.xml").write_bytes(b"<img-meta/>")      # deprioritised

    monkeypatch.setattr(nl, "_extract_7z", fake_extract)
    ad = NLLegislationAdapter(path=str(tmp_path))

    stubs = list(ad.discover(None))
    # one stub per toestand (deduped), NOT one per xml file; identity from the path
    sids = sorted(s.stable_id for s in stubs)
    assert sids == ["BWBR0011468", "BWBR0011468@2020-01-01"]
    # the latest is the bare Work node and points at the main content file, not manifest
    latest = next(s for s in stubs if s.stable_id == "BWBR0011468")
    assert latest.hints["file"].endswith("2026-01-01_0/xml/BWBR0011468.xml")
    assert calls["n"] == 1
    # the cache marker persists → a second discover does NOT re-extract
    list(ad.discover(None))
    assert calls["n"] == 1
    assert (tmp_path / "BWB_20260716_143624_extracted" / ".extracted_ok").exists()


def test_extract_raises_without_tooling(tmp_path, monkeypatch):
    monkeypatch.setattr(nl, "_find_7z", lambda: None)
    monkeypatch.setitem(__import__("sys").modules, "py7zr", None)
    vol = tmp_path / "x.7z.001"
    vol.write_bytes(b"x")
    try:
        nl._extract_7z(vol, tmp_path / "out")
        raised = False
    except RuntimeError as e:
        raised = "7z" in str(e)
    assert raised
