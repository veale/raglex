"""Reviewed, payload-bound projection errata; immutable publisher raw is never edited.

Do not generalise a single bad consolidation into an automatic amendment engine.
Each entry requires the authentic amending act, exact affected payload and tests.
"""
import hashlib
from xml.etree import ElementTree as ET

VERIFIED_DELETIONS = {
 '655c4c6c957d1484426e1eb859a4fc65ad58978c1edd4d3a936fee87e4bf153b': {
    'document':'02024R1689-20260727', 'article':'010', 'paragraph':'010.005',
    'amending_act':'32026R1744', 'amending_provision':'Article 1(9)(b)',
    'effective_from':'2026-07-27',
    'source_url':'https://eur-lex.europa.eu/eli/reg/2026/1744/oj',
 },
}


def apply_verified_deletions(raw, root):
    rule=VERIFIED_DELETIONS.get(hashlib.sha256(raw).hexdigest())
    if not rule:return []
    article=next((a for a in root.iter('ARTICLE') if a.get('IDENTIFIER')==rule['article']),None)
    if article is None:raise ValueError('Verified erratum payload lacks expected article')
    paragraph=next((p for p in article.findall('PARAG') if p.get('IDENTIFIER')==rule['paragraph']),None)
    if paragraph is None:raise ValueError('Verified erratum payload lacks expected paragraph')
    number=paragraph.findtext('NO.PARAG') or '5.'
    paragraph.clear();paragraph.set('IDENTIFIER',rule['paragraph'])
    ET.SubElement(paragraph,'NO.PARAG').text=number
    ET.SubElement(paragraph,'ALINEA').text=(
        '[Deleted by Regulation (EU) 2026/1744, Article 1(9)(b). '
        'Editorial correction of a verified omission in the publisher consolidation; original raw retained.]')
    return [dict(rule,kind='verified deletion',affected_provision='Article 10(5)')]
