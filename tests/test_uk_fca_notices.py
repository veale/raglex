from raglex.adapters.uk_fca_notices import notice_metadata, parse_sitemap


SITEMAP = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url>
<loc>https://www.fca.org.uk/publication/final-notices/example-limited-2026.pdf</loc>
<lastmod>2026-07-27T10:50Z</lastmod></url><url>
<loc>https://www.fca.org.uk/news/not-a-notice</loc></url></urlset>"""


def test_parse_fca_notice_sitemap():
    indexes, rows = parse_sitemap(SITEMAP)
    assert indexes == []
    assert rows == [{
        "url": "https://www.fca.org.uk/publication/final-notices/example-limited-2026.pdf",
        "notice_type": "final_notice",
        "filename": "example-limited-2026.pdf",
        "changed": "2026-07-27T10:50Z",
    }]


def test_fca_notice_metadata():
    meta = notice_metadata(
        "FINAL NOTICE\nTo: Example Limited\nFirm Reference Number: 123456\n"
        "Date: 27 July 2026\n",
        None,
        "example-limited-2026.pdf",
    )
    assert str(meta["date"]) == "2026-07-27"
    assert meta["subject"] == "Example Limited"
    assert meta["reference_number"] == "123456"
