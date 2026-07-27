from raglex.adapters.uk_govuk_regulator import content_text


def test_content_text_combines_structured_parts_and_description():
    content = {
        "description": "A formal decision.",
        "details": {
            "body": "<p>Under the Competition Act 1998.</p>",
            "parts": [{"title": "Outcome", "body": "<p>See [2024] UKSC 1.</p>"}],
        },
    }
    text = content_text(content)
    assert "Competition Act 1998" in text
    assert "Outcome\nSee [2024] UKSC 1." in text
    assert text.endswith("A formal decision.")
