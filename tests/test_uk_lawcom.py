from raglex.adapters.uk_lawcom import (
    FOIA_2000,
    lawcom_year_act_relations,
    parse_completed_projects,
    parse_publication_documents,
    parse_project_documents,
)
from raglex.citations.extractor import extract_citations


def test_parse_completed_projects_and_nested_pdf_documents():
    projects = parse_completed_projects(
        """
        <main><h2>2020</h2><table><tr>
          <td>394</td><td><a href="/project/commonhold/">Commonhold</a></td>
          <td>Accepted in part</td><td>Commonhold and Leasehold Reform Act 2002</td>
        </tr></table></main>
        """
    )
    assert projects == [{
        "number": "394",
        "title": "Commonhold",
        "url": "https://lawcom.gov.uk/project/commonhold/",
        "year": 2020,
        "status": "Accepted in part",
        "measures": "Commonhold and Leasehold Reform Act 2002",
        "archived": False,
    }]
    docs = parse_project_documents(
        """
        <main><article><h1>Commonhold</h1>
          <a href="/outside.pdf">Outside documents</a>
          <h2>Documents</h2><h3>Report and related documents</h3>
          <a href="https://cdn.example/final.pdf">Final report</a>
          <a href="https://cdn.example/summary.pdf">Summary of final report</a>
          <h2>Contact</h2><a href="/contact.pdf">Contact PDF</a>
        </article></main>
        """,
        projects[0],
    )
    assert [doc["title"] for doc in docs] == [
        "Commonhold — Final report",
        "Commonhold — Summary of final report",
    ]
    assert all(doc["category"] == "Report and related documents" for doc in docs)


def test_parse_new_style_publication_page_to_nested_pdf():
    project = {
        "title": "New Funerary Methods",
        "url": "https://lawcom.gov.uk/project/new-funerary-methods/",
    }
    publications = parse_project_documents(
        """
        <main><article><h1>New Funerary Methods</h1><h2>Documents</h2>
          <h3>Final report</h3>
          <a href="/publication/new-funerary-methods-report/">Report and draft bills</a>
        </article></main>
        """,
        project,
    )
    assert len(publications) == 1
    assert publications[0]["publication"] is True
    docs = parse_publication_documents(
        """
        <main><article><h1>New funerary methods report and draft bills</h1>
          <a href="https://cdn.example/report.pdf"></a>
          <a href="https://cdn.example/report.pdf">Report and draft bills (PDF, 1.3 MB)</a>
        </article></main>
        """,
        project,
        publications[0],
    )
    assert len(docs) == 1
    assert docs[0]["url"] == "https://cdn.example/report.pdf"
    assert docs[0]["landing_url"].endswith("/publication/new-funerary-methods-report/")
    assert docs[0]["title"] == "New funerary methods report and draft bills"


def test_year_act_fallback_needs_three_uses_and_one_named_candidate():
    text = (
        "The Commonhold and Leasehold Reform Act 2002 established the scheme. "
        "Section 3 of the 2002 Act applies. The 2002 Act was amended. "
        "The 2002 Act remains important."
    )
    relations = lawcom_year_act_relations(text)
    # The first provision is already linked by ordinary same-sentence carry-forward;
    # the fallback fills the two otherwise-unresolved bare uses without duplicating it.
    assert any(
        citation.candidate_id == "ukpga/2002/15" and citation.pinpoint == "s. 3"
        for citation in extract_citations(text)
    )
    assert len(relations) == 2
    assert {relation.dst_id for relation in relations} == {"ukpga/2002/15"}
    assert all(relation.extracted_via.value == "inferred" for relation in relations)


def test_defined_year_act_outranks_and_suppresses_year_fallback():
    text = (
        "“the 1967 Act”: Leasehold Reform Act 1967.\n"
        "Section 1 of the 1967 Act applies. The 1967 Act was amended. "
        "The 1967 Act remains important."
    )
    assert lawcom_year_act_relations(text) == []


def test_foia_is_never_the_2000_year_fallback_candidate():
    text = (
        "Freedom of Information Act 2000 publication statement. "
        "The 2000 Act applies. The 2000 Act was amended. The 2000 Act remains."
    )
    assert lawcom_year_act_relations(text) == []
    assert FOIA_2000 == "ukpga/2000/36"


def test_year_fallback_refuses_two_non_foi_acts_of_same_year():
    text = (
        "The Terrorism Act 2000 and the Regulation of Investigatory Powers Act 2000 "
        "are relevant. The 2000 Act applies. The 2000 Act was amended. "
        "The 2000 Act remains."
    )
    assert lawcom_year_act_relations(text) == []
