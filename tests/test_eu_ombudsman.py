from raglex.adapters.eu_ombudsman import _date, _text


def test_ombudsman_html_and_date_normalisation():
    assert _text("<p>Article <b>41</b> of the Charter.</p>") == "Article 41 of the Charter."
    assert _date("2026-07-24T10:00:00+02:00").isoformat() == "2026-07-24"
