from raglex.formats.echr_pdf import parse_echr_convention_pages


def test_official_convention_parser_builds_nested_article_anchors():
    # The production PDF has the same layout: headings, paragraph numbers and letters
    # are independent text lines, and page headers repeat.
    pages = [
        """
        European Treaty Series - No. 5
        The governments signatory hereto, being members of the Council of Europe,
        Have agreed as follows:
        Article 1 – Obligation to respect human rights
        The High Contracting Parties shall secure the rights.
        """,
        """
        ETS 5 – Human Rights (Convention), 4.XI.1950
        2
        Article 2 – Right to life
        1
        Everyone's right to life shall be protected by law.
        2
        Force must be absolutely necessary:
        a
        in defence of any person;
        b
        to effect a lawful arrest.
        """
        + "\n".join(
            f"Article {n} – Heading {n}\nText of article {n}."
            for n in range(3, 60)
        ),
    ]
    text, segments = parse_echr_convention_pages(pages)
    labels = {segment.label for segment in segments}
    assert {
        "Preamble", "Article 1", "Article 2", "Article 2(1)",
        "Article 2(2)", "Article 2(2)(a)", "Article 2(2)(b)", "Article 59",
    } <= labels
    point = next(segment for segment in segments if segment.label == "Article 2(2)(a)")
    assert text[point.char_start:point.char_end] == "in defence of any person;"
    assert "European Treaty Series" not in text
