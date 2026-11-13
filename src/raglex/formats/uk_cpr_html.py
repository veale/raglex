"""The Ministry of Justice's consolidated CPR/PD HTML, for local reparse jobs."""
from bs4 import BeautifulSoup

from .base import ParsedDoc, register


def parse_cpr_html(raw: bytes) -> ParsedDoc:
    from ..adapters.uk_cpr import page_identity, parse_page

    html = raw.decode("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    page = page_identity(title, "")
    text, segments, updated, aliases = parse_page(html, page)
    return ParsedDoc(text=text, segments=segments, title=title, decision_date=updated,
                     metadata={"aliases": aliases})


register("uk-cpr-html", parse_cpr_html)
