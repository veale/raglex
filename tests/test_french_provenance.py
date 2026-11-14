from xml.etree import ElementTree as ET
from raglex.formats.dila_xml import parse_dila_article
from raglex.formats.french_provenance import api_provenance
from raglex.citations.oscola import cite
from tests.test_fr_de_bulk_adapters import DILA_ARTICLE


def test_dila_native_modification_link_and_version_dates_are_retained():
 article=parse_dila_article(ET.fromstring(DILA_ARTICLE))
 assert article.provenance['version_id']=='LEGIARTI000032041571'
 assert article.provenance['valid_from']=='2016-10-01'
 assert article.provenance['publisher_links'][0]['cidtexte']=='JORFTEXT000000000002'
 assert article.provenance['publisher_links'][0]['typelien']=='MODIFICATION'


def test_api_preserves_explicit_modifying_links_and_article_versions():
 body={'article':{'id':'LEGIARTI123','num':'31','dateDebut':'2014-03-19',
  'liensModificateurs':[{'textCid':'JORFTEXT456','numArticle':'154'}],
  'articleVersions':[{'id':'LEGIARTI789','dateDebut':'2010-05-14'}]}}
 history=api_provenance(body)['Article 31'][0]
 assert history['liensModificateurs'][0]['numArticle']=='154'
 assert history['article_versions'][0]['id']=='LEGIARTI789'


def test_french_citation_never_turns_pdf_page_into_print_page():
 doc={'source':'fr-legislation','doc_type':'legislation','title':'Loi n° 2010-476 du 12 mai 2010'}
 rendered=cite(doc,{'datePubli':'2010-05-13','numParution':'0110','numTexte':1,'pagePdf':4})['text']
 assert 'Journal officiel' in rendered and 'texte n° 1' in rendered and '13 May 2010' in rendered
 assert ', p ' not in rendered
 assert ', p 8899' in cite(doc,{'datePubli':'2010-05-13','pageDebut':'8899'})['text']
