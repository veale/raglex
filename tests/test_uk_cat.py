from raglex.adapters.uk_cat import cat_slug, parse_cat_page, parse_cat_sitemap


def test_cat_sitemap_and_page_parsers():
    sitemap = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://www.catribunal.org.uk/judgments/example</loc>
      <lastmod>2026-07-20</lastmod></url>
      <url><loc>https://www.catribunal.org.uk/news/noise</loc></url>
    </urlset>"""
    assert parse_cat_sitemap(sitemap) == [{
        "url": "https://www.catribunal.org.uk/judgments/example",
        "lastmod": "2026-07-20",
    }]
    page = b"""<main><h1>Example v Authority</h1><time datetime="2026-07-19"/>
      <p>Neutral citation [2026] CAT 42</p>
      <a href="/sites/cat/files/decision.pdf">Download judgment</a></main>"""
    out = parse_cat_page(page)
    assert cat_slug(out["neutral"]) == "cat/2026/42"
    assert out["pdf"].endswith("/sites/cat/files/decision.pdf")
