"""Riigi Teataja — the Estonian statute book, and the ids 4.07 million edges point at.

Every assertion here exists because getting it wrong is invisible: the document is stored,
the edges resolve, and the corpus reports a successful backfill while pointing Estonian
case law at the wrong provision or the wrong Act.
"""

from __future__ import annotations

import pytest

from raglex.adapters import registry
from raglex.adapters.ee_riigiteataja import EstonianRiigiTeatajaAdapter, claim_ids
from raglex.citations.estonian import act_key, law_id
from raglex.formats.riigiteataja_xml import parse_riigiteataja_xml

NS = 'xmlns="Juurakt"'


def _act(paragraphs: str, *, abbrev: str = "TsMS", title: str = "Tsiviilkohtumenetluse seadustik") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<oigusakt {NS} id="1">
  <metaandmed>
    <valjaandja>Riigikogu</valjaandja>
    <dokumentLiik>seadus</dokumentLiik>
    <lyhend>{abbrev}</lyhend>
    <vastuvoetud><aktikuupaev>2005-04-20</aktikuupaev></vastuvoetud>
  </metaandmed>
  <aktinimi><nimi><pealkiri>{title}</pealkiri></nimi></aktinimi>
  {paragraphs}
</oigusakt>""".encode("utf-8")


def _para(nr: str, displayed: str, heading: str, body: str) -> str:
    return f"""<paragrahv id="para{nr}">
    <paragrahvNr>{nr}</paragrahvNr>
    <kuvatavNr>{displayed}</kuvatavNr>
    <paragrahvPealkiri>{heading}</paragrahvPealkiri>
    <loige id="para{nr}lg1"><loigeNr>1</loigeNr><kuvatavNr>(1)</kuvatavNr>
      <sisuTekst><tavatekst>{body}</tavatekst></sisuTekst></loige>
  </paragrahv>"""


# ── the superscript section, which is a different provision ──────────────────
def test_superscript_sections_stay_distinct():
    """§ 22 and § 22¹ are two provisions, and RT reports ``paragrahvNr`` 22 for both.

    The superscript survives only in ``kuvatavNr``, where it is escaped character data
    rather than markup. Numbering from ``paragrahvNr`` merges them — and in TsMS's
    collective-redress chapter it merges twenty-three sections (§ 497 … § 497²²) into one.
    """
    xml = _act(
        _para("22", "§ 22.", "Üldine kohtualluvus", "Esimene.")
        + _para("22", "§ 22&lt;sup&gt;1&lt;/sup&gt;.", "Erandlik kohtualluvus", "Teine.")
        + _para("497", "§ 497&lt;sup&gt;2&lt;/sup&gt;.", "Lubatavus", "Kolmas.")
        + _para("497", "§ 497&lt;sup&gt;22&lt;/sup&gt;.", "Trahv", "Neljas.")
    )
    labels = [s.label for s in parse_riigiteataja_xml(xml).segments if s.level == 2]
    assert labels == ["§ 22", "§ 22-1", "§ 497-2", "§ 497-22"]
    assert len(set(labels)) == len(labels)


def test_a_multi_digit_superscript_is_one_ordinal():
    """``§ 497²²`` is the twenty-second, not the second twice. Converting digit by digit
    yields ``497-2-2``, and ``§ 497²`` is already ``497-2``."""
    xml = _act(_para("497", "§ 497&lt;sup&gt;11&lt;/sup&gt;.", "Kompromiss", "Tekst."))
    (segment,) = [s for s in parse_riigiteataja_xml(xml).segments if s.level == 2]
    assert segment.label == "§ 497-11"


def test_escaped_sup_in_running_text_does_not_invent_a_section():
    """``§ 186<sup>1</sup>`` mid-sentence flattens to ``§ 1861`` — a section number that
    does not exist, and one a later extraction pass would cite with confidence."""
    xml = _act(_para("1", "§ 1.", "Viide",
                     "Kohus võib täitemenetluse seadustiku "
                     "§ 186&lt;sup&gt;1&lt;/sup&gt; alusel trahvi määrata."))
    text = parse_riigiteataja_xml(xml).text
    assert "§ 186¹" in text
    assert "§ 1861" not in text


def test_segment_labels_are_the_anchors_estonian_citations_carry():
    """``citations.estonian`` normalises ``§ 415⁴`` to ``§ 415-4``; a segment labelled any
    other way is a pinpoint that never lands."""
    xml = _act(_para("415", "§ 415&lt;sup&gt;4&lt;/sup&gt;.", "Pealkiri", "Tekst."))
    (segment,) = [s for s in parse_riigiteataja_xml(xml).segments if s.level == 2]
    assert segment.label == "§ 415-4"


def test_text_and_segments_are_aligned():
    xml = _act(_para("76", "§ 76.", "Kohustuse täitmine", "Kohustus tuleb täita."))
    parsed = parse_riigiteataja_xml(xml)
    (segment,) = [s for s in parsed.segments if s.level == 2]
    body = parsed.text[segment.char_start:segment.char_end]
    assert body.startswith("§ 76. Kohustuse täitmine")
    assert "(1) Kohustus tuleb täita." in body
    assert parsed.metadata["abbreviation"] == "TsMS"
    assert str(parsed.decision_date) == "2005-04-20"


def test_an_empty_or_unparseable_body_is_not_an_empty_act():
    assert parse_riigiteataja_xml(b"").text is None
    with pytest.raises(Exception):
        parse_riigiteataja_xml(b"<html><body>shell</body></html>" * 10)


# ── the id every held citation resolves through ──────────────────────────────
MANIFEST = [
    {"id": 1, "lyhend": "TsMS", "pealkiri": "Tsiviilkohtumenetluse seadustik"},
    {"id": 2, "lyhend": "ÄS", "pealkiri": "Äriseadustik"},
    {"id": 3, "lyhend": "AS", "pealkiri": "Alkoholiseadus"},
    {"id": 4, "lyhend": "RÕS", "pealkiri": "Riigi õigusabi seadus"},
    {"id": 5, "lyhend": "ROS", "pealkiri": "Rahvusooperi seadus"},
    {"id": 6, "lyhend": "KutS", "pealkiri": "Kutseseadus"},
    {"id": 7, "lyhend": "KüTS", "pealkiri": "Küberturvalisuse seadus"},
]


def test_a_contested_id_goes_to_the_abbreviation_the_grammar_declares():
    """Folding diacritics away puts ÄS and AS on one id. 37,405 held citations of
    ``ee/seadus/as`` were minted from ÄS, so ÄS owns it — otherwise the Commercial Code's
    citations silently resolve to the Alcohol Act, and every check downstream goes green."""
    by_id, contested = claim_ids(MANIFEST)
    assert by_id["ee/seadus/as"]["pealkiri"] == "Äriseadustik"
    assert by_id["ee/seadus/ros"]["pealkiri"] == "Riigi õigusabi seadus"


def test_a_contest_the_grammar_does_not_settle_is_refused_not_guessed():
    """Neither KutS nor KüTS is in ACTS, so nothing says which the id means. Storing
    either would be a coin flip recorded as fact."""
    by_id, contested = claim_ids(MANIFEST)
    assert "ee/seadus/kuts" not in by_id
    assert contested["ee/seadus/kuts"] == ["KutS", "KüTS"]


def test_stable_ids_match_what_the_case_law_cites():
    """The adapter's ids and the ids ``act_key`` mints from a judgment must be the same
    strings, or the 4.07 million pending edges stay pending."""
    by_id, _ = claim_ids(MANIFEST)
    assert "ee/seadus/tsms" in by_id
    for abbrev, expected in (("TsMS", "ee/seadus/tsms"), ("VÕS", "ee/seadus/vos"),
                             ("ÄS", "ee/seadus/as")):
        assert law_id(abbrev) == expected
        assert act_key(abbrev) == expected
    assert act_key("Tsiviilkohtumenetluse seadustik") == "ee/seadus/tsms"


# ── a 200 that is not an act ─────────────────────────────────────────────────
class _Response:
    def __init__(self, content: bytes, status: int = 200, ctype: str = "text/html"):
        self.content, self.status_code = content, status
        self.headers = {"content-type": ctype}

    def json(self):
        import json
        return json.loads(self.content)


class _Client:
    def __init__(self, response):
        self._response = response

    def get(self, url, **kwargs):
        return self._response


SHELL = b"<!doctype html><html><body><app-root></app-root></body></html>"


def test_the_spa_shell_is_not_an_empty_statute_book():
    """Every path on riigiteataja.ee answers 200 with the same 51,763-byte Angular shell.
    A manifest that reads as "no acts" would empty the source without failing."""
    adapter = EstonianRiigiTeatajaAdapter(client=_Client(_Response(SHELL)))
    with pytest.raises(Exception):
        adapter.manifest()


def test_the_shell_is_not_an_acts_text_either():
    adapter = EstonianRiigiTeatajaAdapter(client=_Client(_Response(SHELL)))
    assert adapter._xml("https://example.invalid/blob-xml") is None


# ── the catalogue and the resume contract ────────────────────────────────────
def test_the_source_is_in_the_catalogue_as_estonian_legislation():
    row = next(r for r in registry.source_catalog() if r["key"] == "ee-legislation")
    assert row["jurisdiction"] == "EE"
    assert row["group_label"] == "Estonia"
    assert row["kind"] == "legislation"
    assert row["incremental_mode"] == "full-walk"
    assert len(row["description"]) > 120


def test_every_declared_option_is_accepted_by_the_constructor():
    info = registry.SOURCE_INFO["ee-legislation"]
    adapter = registry.get_adapter(
        "ee-legislation", **{opt.name: None for opt in info.options})
    assert adapter.source == "ee-legislation"
    # A blank option must mean the default, not False — include_repealed off would drop
    # the repealed acts that Estonian judgments cite constantly.
    assert adapter.include_repealed is True


def test_a_reported_resume_cursor_is_accepted_back():
    """AGENTS.md §1: the stubs carry ``resume_offset``, so ``jobs`` will hand
    ``start_offset`` to the constructor on the retry."""
    adapter = registry.get_adapter("ee-legislation", start_offset=100)
    assert adapter.start_offset <= 100


def test_discovery_resumes_early_and_in_a_stable_order():
    manifest = [{"id": i, "lyhend": f"X{i:03d}S", "pealkiri": f"Act {i}"}
                for i in range(120)]

    class _Adapter(EstonianRiigiTeatajaAdapter):
        def manifest(self):
            return manifest

    everything = [s.stable_id for s in _Adapter().discover(None)]
    assert everything == sorted(everything), "order must not depend on manifest order"
    resumed = [s.stable_id for s in _Adapter(start_offset=50).discover(None)]
    # resume_floor backs off a page, so the resumed run re-covers rather than skips.
    assert resumed[0] == everything[25]
    assert everything[-1] == resumed[-1]
    assert [s.hints["resume_offset"] for s in _Adapter().discover(None)][:3] == [0, 1, 2]


# ── the level names, and why their order matters ─────────────────────────────
@pytest.mark.parametrize("text,expected", [
    # The bug: `p` listed before `punkti` matched first, and the trailing [a-z]? ate the
    # "u" of the word it had just truncated. 161 held edges cited a GDPR point (u).
    ("IKÜM art 6 lg 1 punkti f alusel", "Article 6(1)(f)"),
    ("IKÜM art 6 lg 1 p f", "Article 6(1)(f)"),
])
def test_an_eu_point_is_not_invented_from_the_word_punkti(text, expected):
    from raglex.citations.estonian import law_citations
    (citation,) = [c for c in law_citations(text) if c.candidate_id == "32016R0679"]
    assert citation.pinpoint == expected


@pytest.mark.parametrize("text,expected", [
    ("TsMS § 162 lõike 1 alusel", "§ 162 lg 1"),
    # `lõige` matched the prefix of "lõiget", [a-z]? ate the "t", and the number was then
    # never consumed — 96,380 held edges decayed to the bare section this way.
    ("TsMS § 162 lõiget 1 kohaldades", "§ 162 lg 1"),
    ("VÕS § 101 punktis 3", "§ 101 p 3"),
    ("HKMS § 121 lõike 2 punkti 1", "§ 121 lg 2 p 1"),
    ("KarS § 199 lg 2 p 1", "§ 199 lg 2 p 1"),
    ("TsMS § 415-4 lõikes 2", "§ 415-4 lg 2"),
])
def test_a_declined_level_name_keeps_its_number(text, expected):
    from raglex.citations.estonian import law_citations
    (citation,) = [c for c in law_citations(text) if c.entity_kind == "act"]
    assert citation.pinpoint == expected


def test_the_base_act_namespace_parses_too():
    """An act Riigi Teataja never consolidated arrives under ``tyviseadus_1_10.02.2010``
    with identical element names. Matching on the namespace dropped two acts to empty
    text with no error and no status to notice."""
    xml = _act(_para("3", "§ 3.", "Erastamine", "Tekst.")).replace(
        b'xmlns="Juurakt"', b'xmlns="tyviseadus_1_10.02.2010"')
    parsed = parse_riigiteataja_xml(xml)
    assert [s.label for s in parsed.segments if s.level == 2] == ["§ 3"]
    assert parsed.metadata["abbreviation"] == "TsMS"


def test_an_act_with_no_markup_yields_no_sections_rather_than_a_wrong_one():
    """Old repealed acts keep their body in an attached HTML file. Zero sections is the
    honest answer; the adapter declines them rather than storing an empty statute."""
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<oigusakt xmlns="Juurakt"><metaandmed><lyhend>RERS</lyhend></metaandmed>
<aktinimi><nimi><pealkiri>Riiklike elatusrahade seadus</pealkiri></nimi></aktinimi>
<sisu><sisuTekst><HTMLKonteiner><fail>x.html</fail></HTMLKonteiner></sisuTekst></sisu>
</oigusakt>"""
    parsed = parse_riigiteataja_xml(xml)
    assert [s for s in parsed.segments if s.level == 2] == []
    assert not (parsed.text or "").strip()
