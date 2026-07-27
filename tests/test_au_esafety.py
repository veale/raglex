from raglex.adapters.au_esafety import (
    GUIDANCE_URL,
    ONLINE_SAFETY_ACT_2021,
    REGISTER_URL,
    online_safety_act_relations,
    parse_esafety_page,
)
from raglex.citations.extractor import extract_citations


def test_parse_register_keeps_official_title_and_heading_context():
    rows = parse_esafety_page(
        """
        <main>
          <h2>Codes for class 1A and class 1B material (Unlawful Material)</h2>
          <h3>Unlawful Material Codes registered 16 June 2023</h3>
          <a href="/sites/default/files/2023-06/schedule-1.pdf?v=7">
            Schedule 1 – Social Media Services Online Safety Code
            (Class 1A and Class 1B Material) (PDF, 403.81KB)
          </a>
          <p>Last updated: 17/06/2026</p>
        </main>
        """,
        page_url=REGISTER_URL,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == (
        "Schedule 1 – Social Media Services Online Safety Code "
        "(Class 1A and Class 1B Material)"
    )
    assert row["section"].startswith("Codes for class 1A")
    assert row["subsection"] == "Unlawful Material Codes registered 16 June 2023"
    assert str(row["published"]) == "2023-06-16"
    assert str(row["page_updated"]) == "2026-06-17"
    assert row["url"].endswith("schedule-1.pdf?v=7")


def test_register_bold_notice_dates_disambiguate_repeated_titles():
    rows = parse_esafety_page(
        """
        <main><h2>Notices issued to representatives of industry sections</h2>
          <h3>Notices issued: Age-Restricted Material Codes</h3>
          <p><strong>eSafety notice: 1 July 2024</strong></p>
          <a href="/2024-07/app.pdf">eSafety notice: App distribution service providers
            (PDF, 1MB)</a>
          <p><strong>eSafety variation notice: 27 February 2025</strong></p>
          <a href="/2025-02/app.pdf">eSafety variation notice: App distribution service
            providers (PDF, 1MB)</a>
        </main>
        """,
        page_url=REGISTER_URL,
    )
    assert rows[0]["title"].endswith("— 1 July 2024")
    assert rows[1]["title"].endswith("— 27 February 2025")
    assert rows[0]["stable_id"] != rows[1]["stable_id"]
    assert str(rows[1]["published"]) == "2025-02-27"


def test_guidance_identity_survives_updated_label_and_pdf_url():
    def one(label: str, href: str):
        return parse_esafety_page(
            f"""
            <main><h2>Basic Online Safety Expectations</h2>
            <a href="{href}">{label} (PDF, 1.67MB)</a></main>
            """,
            page_url=GUIDANCE_URL,
        )[0]

    old = one(
        "Basic Online Safety Expectations Regulatory Guidance (updated January 2025)",
        "/sites/default/files/2025-01/bose-guidance.pdf",
    )
    new = one(
        "Basic Online Safety Expectations Regulatory Guidance (updated February 2026)",
        "/sites/default/files/2026-02/bose-guidance.pdf?v=2",
    )
    assert old["stable_id"] == new["stable_id"]
    assert new["title"].endswith("(updated February 2026)")


def test_only_sections_explicitly_of_the_act_get_structured_pinpoints():
    relations = online_safety_act_relations(
        "Sections 135 and 136 of the Act establish the scheme. "
        "Section 2.1 of the Head Terms defines a service. "
        "Section 12 of the Regulatory Powers Act 2014 also applies."
    )
    assert relations[0].dst_id == ONLINE_SAFETY_ACT_2021
    assert {r.dst_anchor for r in relations[1:]} == {"s. 135", "s. 136"}


def test_online_safety_act_grammar_resolves_with_or_without_cth_tag():
    citations = extract_citations(
        "Section 135 of the Online Safety Act 2021 applies. "
        "See s 141 of the Online Safety Act 2021 (Cth)."
    )
    got = {
        (c.candidate_id, c.pinpoint)
        for c in citations
        if "Online Safety Act 2021" in c.raw
    }
    assert (ONLINE_SAFETY_ACT_2021, "s. 135") in got
    assert (ONLINE_SAFETY_ACT_2021, "s. 141") in got
