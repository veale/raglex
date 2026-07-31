from raglex.adapters.au_vic_legislation import VictoriaLegislationAdapter, parse_vic_page


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


def test_bounded_victoria_watch_reads_acts_and_rules():
    class Response:
        def __init__(self, kind):
            self.kind = kind

        def json(self):
            is_act = self.kind == "act_in_force"
            return {
                "data": [{
                    "id": self.kind,
                    "attributes": {
                        "title": "Current Act" if is_act else "Current Rules",
                        "field_act_sr_year": "2026",
                        "field_act_sr_number": "1" if is_act else "2",
                        "changed": "2026-07-01",
                        "path": {"alias": "/current"},
                    },
                    "relationships": {"field_in_force_version": {"data": []}},
                }],
                # A next page exists, but max_pages=1 must bound each register.
                "links": {"next": {"href": "https://example.test/next"}},
            }

    class Client:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None):
            kind = "sr_in_force" if "sr_in_force" in url else "act_in_force"
            self.calls.append((kind, params))
            return Response(kind)

    client = Client()
    rows = list(VictoriaLegislationAdapter(client=client).discover(None, max_pages=1))
    assert [row.stable_id for row in rows] == [
        "au/vic/act/2026/1", "au/vic/regulation/2026/2",
    ]
    assert [call[0] for call in client.calls] == ["act_in_force", "sr_in_force"]
    assert all(call[1]["sort"] == "-changed" for call in client.calls)


def test_victoria_incremental_discovery_stops_at_cursor():
    class Response:
        def __init__(self, kind):
            self.kind = kind

        def json(self):
            return {
                "data": [{
                    "id": self.kind,
                    "attributes": {
                        "title": "Old item",
                        "field_act_sr_year": "2025",
                        "field_act_sr_number": "1",
                        "changed": "2026-07-01T00:00:00+10:00",
                        "path": {"alias": "/old"},
                    },
                    "relationships": {"field_in_force_version": {"data": []}},
                }],
                "links": {"next": {"href": "https://example.test/next"}},
            }

    class Client:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None):
            kind = "sr_in_force" if "sr_in_force" in url else "act_in_force"
            self.calls.append(kind)
            return Response(kind)

    client = Client()
    rows = list(VictoriaLegislationAdapter(client=client).discover(
        "2026-07-02T00:00:00+10:00", max_pages=40
    ))
    assert rows == []
    # One cursor-reaching page per register, not 40 silent pages per register.
    assert client.calls == ["act_in_force", "sr_in_force"]
