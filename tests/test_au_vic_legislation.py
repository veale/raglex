from raglex.adapters.au_vic_legislation import parse_vic_page


def test_victoria_jsonapi_identity_and_file_chain():
    data = {
        "data": [{
            "id": "act", "attributes": {
                "title": "Example Act 2020", "field_act_sr_year": "2020",
                "field_act_sr_number": "12", "changed": "2026-01-01",
                "path": {"alias": "/in-force/acts/example-act-2020"},
            },
            "relationships": {"field_in_force_version": {"data": [{"id": "v"}]}},
        }],
        "included": [
            {"id": "v", "attributes": {"field_in_force_effective_date": "2026-01-01"},
             "relationships": {"field_in_force_version": {"data": [{"id": "m"}]}}},
            # File relationships are to-one objects in the live Drupal JSON:API,
            # while the two parent relationships above are to-many arrays.
            {"id": "m", "relationships": {"field_media_file": {"data": {"id": "f"}}}},
            {"id": "f", "attributes": {"filemime": "application/pdf", "changed": "2026-02-01",
                                      "uri": {"url": "/sites/default/files/example.pdf"}}},
        ],
        "links": {},
    }
    rows, next_url = parse_vic_page(data, "act_in_force")
    assert next_url is None
    assert rows[0]["stable_id"] == "au/vic/act/2020/12"
    assert rows[0]["changed"] == "2026-02-01"
    assert rows[0]["files"][0]["url"].endswith("/sites/default/files/example.pdf")
