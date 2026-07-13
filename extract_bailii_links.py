#!/usr/bin/env python3
"""
Extract case links from BAILII index HTML files in a folder.
Outputs a CSV with columns: index_page, case_name, live_url, wayback_url

Usage:
    python extract_bailii_links.py <folder> [output.csv]
"""

import csv
import re
import sys
from pathlib import Path
from html.parser import HTMLParser

BAILII_BASE = "https://www.bailii.org"

# Only accept hrefs that look like BAILII case paths, e.g. /ie/cases/IEHC/2000/1.html
_CASE_HREF_RE = re.compile(r"^/[a-z]{2,}/cases/[^/]+/\d{4}/[^/]+\.html$")


class BailiiIndexParser(HTMLParser):
    """Parse a BAILII year-index page and collect case entries from <li> items."""

    def __init__(self):
        super().__init__()
        self.entries: list[tuple[str, str]] = []  # (href, case_name_text)
        self._in_li = False
        self._li_depth = 0       # track nested tags inside the <li>
        self._primary_href = None
        self._text_parts: list[str] = []
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        self._tag_stack.append(tag)
        if tag == "li":
            self._in_li = True
            self._li_depth = 0
            self._primary_href = None
            self._text_parts = []
            return
        if self._in_li:
            self._li_depth += 1
            if tag == "a":
                attrs_dict = dict(attrs)
                href = attrs_dict.get("href", "")
                # The first <a> inside the <li> is the primary case link
                if self._primary_href is None and _CASE_HREF_RE.match(href):
                    self._primary_href = href

    def handle_endtag(self, tag):
        if self._tag_stack:
            self._tag_stack.pop()
        if tag == "li" and self._in_li:
            self._in_li = False
            if self._primary_href:
                # Collapse whitespace in collected text
                name = re.sub(r"\s+", " ", "".join(self._text_parts)).strip()
                # Remove duplicate semicolons / trailing punctuation artifacts
                name = re.sub(r"(;\s*)+$", "", name).strip()
                self.entries.append((self._primary_href, name))
            self._primary_href = None
            self._text_parts = []
        elif self._in_li:
            self._li_depth -= 1

    def handle_data(self, data):
        if self._in_li:
            self._text_parts.append(data)

    def handle_entityref(self, name):
        if self._in_li:
            entities = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "nbsp": " "}
            self._text_parts.append(entities.get(name, ""))

    def handle_charref(self, name):
        if self._in_li:
            try:
                cp = int(name[1:], 16) if name.startswith("x") else int(name)
                self._text_parts.append(chr(cp))
            except ValueError:
                pass


def extract_from_file(html_path: Path) -> list[dict]:
    text = html_path.read_text(encoding="utf-8", errors="replace")
    parser = BailiiIndexParser()
    parser.feed(text)

    rows = []
    for href, case_name in parser.entries:
        live_url = BAILII_BASE + href
        rows.append({
            "index_page": html_path.name,
            "case_name": case_name,
            "live_url": live_url,
            "wayback_url": "",
        })
    return rows


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    folder = Path(sys.argv[1])
    if not folder.is_dir():
        print(f"Error: {folder} is not a directory", file=sys.stderr)
        sys.exit(1)

    output_path = Path(sys.argv[2]) if len(sys.argv) >= 3 else folder / "extracted_cases.csv"

    html_files = sorted(folder.glob("*.html"))
    if not html_files:
        print(f"No .html files found in {folder}", file=sys.stderr)
        sys.exit(1)

    all_rows = []
    for html_file in html_files:
        rows = extract_from_file(html_file)
        print(f"{html_file.name}: {len(rows)} cases")
        all_rows.extend(rows)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["index_page", "case_name", "live_url", "wayback_url"])
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nTotal: {len(all_rows)} cases written to {output_path}")


if __name__ == "__main__":
    main()
