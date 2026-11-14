"""Publisher-supplied version/link evidence, without inferring amendment direction."""
from xml.etree import ElementTree as ET


def dila_provenance(root):
    def text(tag):
        node=root.find('.//'+tag)
        return ''.join(node.itertext()).strip() if node is not None else None
    links=[]
    for link in root.iter('LIEN'):
        kind=(link.get('typelien') or link.get('nature') or '').upper()
        if any(x in kind for x in ['MODIF','CRE','ABROG','CODIF','TRANSF']):
            links.append({**link.attrib,'label':' '.join(''.join(link.itertext()).split())})
    return {'version_id':text('ID'),'valid_from':text('DATE_DEBUT'),'valid_to':text('DATE_FIN'),
            'state':text('ETAT'),'publisher_links':links}


def api_provenance(body):
    """Retain Légifrance's explicit modifying/creating links and dated article versions."""
    result={}
    def visit(node):
        if isinstance(node,list):
            for child in node:visit(child)
        elif isinstance(node,dict):
            number=node.get('num') or node.get('numeroArticle')
            ident=node.get('id') or node.get('cid')
            if number and str(ident or '').startswith(('LEGIARTI','JORFARTI')):
                row={'version_id':ident,'valid_from':node.get('dateDebut'),
                     'valid_to':node.get('dateFin'),'state':node.get('etat'),
                     'article_versions':node.get('articleVersions') or []}
                for key in ['liensModificateurs','liensCreateurs','liensModification','liensCreation']:
                    if node.get(key):row[key]=node[key]
                result.setdefault('Article '+str(number),[]).append(row)
            for key in ['text','article','articles','sections','children']:visit(node.get(key))
    visit(body)
    return result
