from __future__ import annotations

from datetime import date

from raglex.adapters.uk_caselaw import parse_atom, parse_judgment

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Find Case Law</title>
  <link rel="self" href="https://caselaw.nationalarchives.gov.uk/atom.xml"/>
  <link rel="next" href="https://caselaw.nationalarchives.gov.uk/atom.xml?page=2"/>
  <entry>
    <title>Doe v Information Commissioner</title>
    <id>https://caselaw.nationalarchives.gov.uk/ukftt/grc/2024/123</id>
    <link rel="alternate" href="https://caselaw.nationalarchives.gov.uk/ukftt/grc/2024/123"/>
    <updated>2024-03-01T10:00:00Z</updated>
  </entry>
  <entry>
    <title>Smith v Jones</title>
    <id>https://caselaw.nationalarchives.gov.uk/d-abc123</id>
    <link rel="alternate" href="https://caselaw.nationalarchives.gov.uk/d-abc123"/>
    <updated>2024-02-10T09:00:00Z</updated>
  </entry>
</feed>
"""

JUDGMENT = b"""<?xml version="1.0" encoding="utf-8"?>
<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
  <judgment>
    <meta>
      <identification>
        <FRBRWork>
          <FRBRname value="[2024] UKFTT 123 (GRC)"/>
        </FRBRWork>
      </identification>
    </meta>
    <judgmentBody>
      <decision>
        <p>This appeal concerns the right to erasure of personal data.</p>
        <p>The tribunal considered <ref href="https://caselaw.nationalarchives.gov.uk/eu/c-311-18">Case C-311/18 (Schrems II)</ref>.</p>
      </decision>
    </judgmentBody>
  </judgment>
</akomaNtoso>
"""


def test_parse_atom_yields_stubs_and_next():
    page = parse_atom(ATOM)
    assert page.next_url == "https://caselaw.nationalarchives.gov.uk/atom.xml?page=2"
    assert len(page.stubs) == 2

    first = page.stubs[0]
    assert first.stable_id == "ukftt/grc/2024/123"
    assert first.court == "ukftt"
    assert first.hint_date == date(2024, 3, 1)
    assert first.raw_url.endswith("/ukftt/grc/2024/123/data.xml")

    second = page.stubs[1]
    assert second.stable_id == "d-abc123"  # new-style stable URI preserved


def test_parse_judgment_extracts_text_ncn_and_citation():
    text, relations, ncn, segments = parse_judgment(JUDGMENT)
    assert "right to erasure of personal data" in text
    assert ncn == "[2024] UKFTT 123 (GRC)"
    assert len(relations) == 1
    rel = relations[0]
    assert "c-311-18" in rel.raw_citation_string
    assert rel.resolution_status.value == "pending"
    # the <p> paragraphs become structural segments mapping into the text (§6b)
    assert len(segments) >= 1
    for s in segments:
        assert text[s.char_start:s.char_end].strip() != ""  # span indexes into text
