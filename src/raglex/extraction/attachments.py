"""Read publisher attachments by their bytes; never decode an archive as legal text."""
import csv
import io
import posixpath
import re
import zipfile
from urllib.parse import parse_qs, unquote, urlsplit
from xml.etree import ElementTree as ET

from ..core.errors import FetchError
from .extractors import DocxExtractor, PdfExtractor


def _xml(z, name):
    info = z.getinfo(name)
    if info.file_size > 50_000_000:
        raise FetchError('Attachment XML exceeds the complete-extraction limit', transient=False)
    return ET.fromstring(z.read(name))


def spreadsheet_text(data):
    """Keep sheet names, cell coordinates, formulas and cached values, including blanks."""
    ns = {'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(x.itertext()) for x in _xml(z,'xl/sharedStrings.xml').findall('s:si',ns)]
        rels = {r.attrib['Id']:r.attrib['Target'] for r in _xml(z,'xl/_rels/workbook.xml.rels')}
        parts = []
        for sheet in _xml(z,'xl/workbook.xml').findall('s:sheets/s:sheet',ns):
            rid=sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
            target=rels[rid]
            path=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
            parts.append('Sheet: '+sheet.attrib['name'])
            for row in _xml(z,path).findall('s:sheetData/s:row',ns):
                cells=[]
                for cell in row.findall('s:c',ns):
                    value=cell.findtext('s:v',default='',namespaces=ns)
                    kind=cell.get('t')
                    if kind=='s':value=strings[int(value)] if value else ''
                    elif kind=='inlineStr':value=''.join(cell.find('s:is',ns).itertext())
                    elif kind=='b':value='TRUE' if value=='1' else 'FALSE'
                    formula=cell.findtext('s:f',namespaces=ns)
                    if formula is not None:value='='+formula+(f' [cached: {value}]' if value else '')
                    if value:cells.append(f"{cell.get('r')}: {value}")
                if cells:parts.append('\t'.join(cells))
        return '\n'.join(parts)


def read_attachment(data, *, url, headers=None):
    """Return (text, extension, asset-only). Unsupported binary assets retain raw bytes."""
    headers=headers or {}
    disposition=headers.get('content-disposition','')
    match=re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"';]+)",disposition,re.I)
    name=unquote(match[1]) if match else parse_qs(urlsplit(url).query).get('filename',[urlsplit(url).path])[0]
    ext=name.rsplit('.',1)[-1].lower() if '.' in name else ''
    if data.startswith(b'%PDF-'):
        return (PdfExtractor().extract(data,ext='pdf',mime='application/pdf').text or '').strip(),'pdf',False
    if data.startswith(b'PK\x03\x04'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:names=set(z.namelist())
            if 'word/document.xml' in names:
                return DocxExtractor().extract(data,ext='docx').text,'docx',False
            if 'xl/workbook.xml' in names:
                return spreadsheet_text(data),'xlsx',False
            return None,'zip',True
        except (zipfile.BadZipFile,ET.ParseError,KeyError,ValueError) as exc:
            raise FetchError('Malformed Office/archive attachment',transient=False) from exc
    if data.startswith(b'\x89PNG\r\n\x1a\n'):return None,'png',True
    if data.startswith(b'\xff\xd8\xff'):return None,'jpg',True
    if data.startswith((b'GIF87a',b'GIF89a')):return None,'gif',True
    if data.startswith(b'\xd0\xcf\x11\xe0'):return None,ext if ext in {'doc','xls','ppt'} else 'bin',True
    if ext in {'csv','tsv'}:
        try:
            text=data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text=data.decode('cp1252')
        # Keep quoted field boundaries and empty columns rather than treating commas as prose.
        rows=list(csv.reader(io.StringIO(text),delimiter='\t' if ext=='tsv' else ','))
        return '\n'.join(f"row {i}: "+repr(row) for i,row in enumerate(rows,1)),ext,False
    raise FetchError(f'Attachment is neither supported text nor a recognised binary asset: {url}',transient=True)
