from raglex.adapters.uk_cps_guidance import parse_cps_guidance, parse_cps_library


def test_parse_cps_library_keeps_guidance_and_pdf_not_navigation():
    rows = parse_cps_library(
        """
        <main><div class="az-library">
          <h2>A</h2>
          <a href="/prosecution-guidance/abuse-process">Abuse of Process</a>
          <a href="/sites/default/files/annex-a.pdf">Annex A - Explosives.pdf</a>
          <a href="/prosecution-guidance-library-search">Search library</a>
          <a href="https://example.com/news">External news</a>
        </div></main>
        """
    )
    assert [row["title"] for row in rows] == [
        "Abuse of Process", "Annex A - Explosives.pdf"
    ]
    assert rows[0]["stable_id"] == "uk/cps/guidance/abuse-process"
    assert rows[1]["is_pdf"] is True


def test_parse_cps_guidance_title_date_text_and_heading_segments():
    parsed = parse_cps_guidance(
        """
        <main><div class="cps-content">
          <h1>Abuse of Process</h1>
          <span class="cps-content__date">Rewritten: 15 March 2023</span>
          <div class="cps-content__tags"><a>Prosecution Guidance</a></div>
          <div class="cps-content__body">
            <h2>Introduction</h2>
            <p>The Criminal Procedure and Investigations Act 1996 applies.</p>
            <h3>Procedure</h3><p>Follow the Criminal Procedure Rules.</p>
          </div>
        </div></main>
        """
    )
    assert parsed["title"] == "Abuse of Process"
    assert str(parsed["date"]) == "2023-03-15"
    assert parsed["tags"] == ["Prosecution Guidance"]
    assert parsed["text"].startswith("Introduction\n\n")
    assert [segment.label for segment in parsed["segments"]] == [
        "Introduction", "Procedure"
    ]
