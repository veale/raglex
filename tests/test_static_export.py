from __future__ import annotations

import json
import re
from datetime import date

from raglex.config import Config
from raglex.core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Segment,
    TypedRelation,
)
from raglex.resolve import Resolver
from raglex.static_export import StaticLawExporter
from raglex.storage import Catalogue, TextStore


def _config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path,
        catalogue_path=tmp_path / "catalogue.sqlite",
        raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text",
        settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing",
        embed_model=None,
    )


def _store(cat: Catalogue, textstore: TextStore, record: Record) -> None:
    record.ensure_payload_hash()
    path = textstore.put(record.payload_hash, record.text or "")
    textstore.put_segments(record.payload_hash, record.segments)
    cat.upsert_document(record, text_path=str(path))


def test_static_export_contains_law_mentions_snippets_and_public_links(tmp_path):
    config = _config(tmp_path)
    cat = Catalogue(config.catalogue_path)
    textstore = TextStore(config.text_dir)

    law_text = (
        "Article 1 Subject matter\n1. This Regulation lays down rules.\n\n"
        "Article 15 Right of access\n"
        "1. The data subject has the right of access.\n"
        "3. The controller shall provide a copy."
    )
    article_15 = law_text.index("Article 15")
    law = Record(
        source="eu-legislation",
        stable_id="32016R0679",
        doc_type=DocType.LEGISLATION,
        title=(
            "Regulation (EU) 2016/679 of the European Parliament and of the Council "
            "of 27 April 2016 on the protection of natural persons with regard to the "
            "processing of personal data and on the free movement of such data, and "
            "repealing Directive 95/46/EC (General Data Protection Regulation)"
        ),
        decision_date=date(2016, 4, 27),
        language="en",
        source_language="en",
        landing_url="https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:32016R0679",
        text=law_text,
        raw_bytes=law_text.encode(),
        segments=[
            Segment("Article 1 Subject matter", 0, article_15, kind="article"),
            Segment("Article 15 Right of access", article_15, len(law_text), kind="article"),
        ],
        extracted_via=ExtractedVia.STRUCTURED,
    )
    _store(cat, textstore, law)

    citing_text = (
        "Article 1 GDPR states the scope. The claimant relied on Article 15 GDPR when "
        "asking for a copy, and Article 15(3) GDPR for the form of that copy."
    )
    article_1_start = citing_text.index("Article 1 GDPR")
    article_15_start = citing_text.index("Article 15 GDPR")
    article_15_3_start = citing_text.index("Article 15(3) GDPR")
    citer = Record(
        source="uk-caselaw",
        stable_id="ewhc/admin/2024/10",
        doc_type=DocType.JUDGMENT,
        title="Example v Commissioner",
        court="ewhc",
        decision_date=date(2024, 2, 1),
        language="en",
        source_language="en",
        landing_url="https://www.bailii.org/ew/cases/EWHC/Admin/2024/10.html",
        text=citing_text,
        raw_bytes=citing_text.encode(),
        relations=[
            TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string="Article 1 GDPR",
                dst_id="32016R0679",
                dst_anchor="Article 1",
                context_start=article_1_start,
                context_end=article_1_start + len("Article 1 GDPR"),
                resolution_status=ResolutionStatus.PENDING,
            ),
            TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string="Article 15 GDPR",
                dst_id="32016R0679",
                dst_anchor="Article 15",
                context_start=article_15_start,
                context_end=article_15_start + len("Article 15 GDPR"),
                resolution_status=ResolutionStatus.PENDING,
            ),
            TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string="Article 15(3) GDPR",
                dst_id="32016R0679",
                dst_anchor="Article 15(3)",
                context_start=article_15_3_start,
                context_end=article_15_3_start + len("Article 15(3) GDPR"),
                resolution_status=ResolutionStatus.PENDING,
            ),
        ],
        extracted_via=ExtractedVia.STRUCTURED,
    )
    _store(cat, textstore, citer)
    Resolver(cat).run()
    # A stale relation-span projection must not make the static page mark unrelated
    # nearby words. The exporter validates/re-locates the raw citation on read.
    cat.conn.execute(
        "UPDATE relations SET context_start=context_start-9, context_end=context_end-9 "
        "WHERE src_id=? AND raw_citation_string=?",
        ("ewhc/admin/2024/10", "Article 15 GDPR"),
    )
    cat.commit()
    cat.close()

    result = StaticLawExporter(config).build("32016R0679")
    page = result.html.decode()

    assert result.documents == 1
    assert result.mentions == 3
    assert result.filename.startswith("regulation-eu-2016-679-")
    assert "General Data Protection Regulation" in page
    assert "Article 15 Right of access" in page
    assert "Example v Commissioner" in page
    assert "Article%2015%20GDPR" in page
    assert "fetch(" not in page
    assert 'font-family: Times, "Times New Roman", serif' in page
    assert "--paper: #ffffff" in page
    assert 'id="contents-search"' not in page
    assert 'id="result-search"' not in page
    assert "<footer" not in page
    assert "Static snapshot" not in page
    assert "Document generated from a dataset held and maintained by" in page

    match = re.search(
        r'<script id="raglex-data" type="application/json">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert match
    data = json.loads(match.group(1))
    assert data["counts"]["art:15"] == 1
    assert data["counts"]["exact:art15(3)"] == 1
    group = data["groups"][0]
    exact = group["snippets"][group["snippet_indices"]["exact:art15(3)"][0]]
    assert exact["text"][exact["mark"][0]:exact["mark"][1]] == "Article 15(3) GDPR"
    article_15_snippet = group["snippets"][group["snippet_indices"]["bare:art:15"][0]]
    assert (
        article_15_snippet["text"][
            article_15_snippet["mark"][0]:article_15_snippet["mark"][1]
        ]
        == "Article 15 GDPR"
    )
    article_15_section = next(
        section for section in data["law"]["sections"]
        if section["label"].startswith("Article 15")
    )
    third_paragraph = next(
        paragraph for paragraph in article_15_section["paragraphs"]
        if paragraph["text"].startswith("3.")
    )
    assert third_paragraph["indent"] == 1
    assert third_paragraph["marks"][0]["key"] == "exact:art15(3)"
    assert data["flags"]["United Kingdom"].startswith("data:image/svg+xml;base64,")
    assert data["groups"][0]["links"][0]["url"].startswith("https://www.bailii.org/")


def test_static_export_escapes_script_terminators_and_sanitises_attribution(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "RAGLEX_STATIC_EXPORT_ATTRIBUTION",
        '<strong>Maintained</strong><script>alert(1)</script>'
        '<a href="javascript:alert(2)">bad link</a>',
    )
    config = _config(tmp_path)
    cat = Catalogue(config.catalogue_path)
    textstore = TextStore(config.text_dir)
    text = "Section 1\nA harmless </script> string in the source."
    record = Record(
        source="uk-legislation",
        stable_id="ukpga/2024/1",
        doc_type=DocType.LEGISLATION,
        title="Example Act",
        text=text,
        raw_bytes=text.encode(),
        extracted_via=ExtractedVia.STRUCTURED,
    )
    _store(cat, textstore, record)
    cat.close()

    page = StaticLawExporter(config).build("ukpga/2024/1").html.decode()
    data_block = page.split('<script id="raglex-data" type="application/json">', 1)[1].split(
        "</script>", 1)[0]
    assert "<\\/script>" in data_block
    assert "<strong>Maintained</strong>" in page
    assert "<script>alert(1)</script>" not in page
    assert 'href="javascript:' not in page
