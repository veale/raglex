import io,zipfile
import pytest
from raglex.extraction.attachments import read_attachment
from raglex.core.errors import FetchError
from tests.test_docx_extractor import _make_docx


def test_word_attachment_is_extracted_by_magic_not_mislabelled_url():
 text,ext,asset=read_attachment(_make_docx(['Substantive annex','Article 5 applies.']),url='https://example.org/download/123')
 assert text=='Substantive annex\nArticle 5 applies.' and ext=='docx' and not asset


def test_spreadsheet_preserves_sparse_cells_shared_strings_and_formulas():
 out=io.BytesIO()
 with zipfile.ZipFile(out,'w') as z:
  z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Table One" r:id="r1"/></sheets></workbook>')
  z.writestr('xl/_rels/workbook.xml.rels','<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
  z.writestr('xl/sharedStrings.xml','<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Required count</t></si></sst>')
  z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1"><f>SUM(A2:A9)</f><v>42</v></c></row></sheetData></worksheet>')
 text,ext,asset=read_attachment(out.getvalue(),url='https://example.org/no-extension')
 assert ext=='xlsx' and not asset
 assert 'Sheet: Table One' in text and 'A1: Required count' in text and 'C1: =SUM(A2:A9) [cached: 42]' in text


def test_archive_is_retained_as_asset_not_zip_noise():
 out=io.BytesIO()
 with zipfile.ZipFile(out,'w') as z:z.writestr('image.svg','<svg/>')
 text,ext,asset=read_attachment(out.getvalue(),url='https://example.org/archive')
 assert text is None and ext=='zip' and asset


def test_short_csv_keeps_quoted_fields_and_empty_columns():
 text,ext,asset=read_attachment(b'count,label,empty\n2,"a,b",\n',url='https://example.org/download',headers={'content-disposition':'attachment; filename=data.csv'})
 assert ext=='csv' and not asset and "['2', 'a,b', '']" in text


def test_html_error_is_not_stored_as_pdf_attachment():
 with pytest.raises(FetchError):read_attachment(b'<html>error</html>',url='https://example.org/file.pdf')


def test_scanned_attachment_ocr_covers_all_pages_or_refuses_partial(monkeypatch):
    import sys
    from types import SimpleNamespace
    from raglex.extraction import attachments, ocr
    from raglex.core.errors import FetchError
    import pytest
    monkeypatch.setattr(attachments.PdfExtractor,'extract',lambda *a,**k:SimpleNamespace(text='',needs_ocr=True))
    class Pages:
        count=9
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def __len__(self):return self.count
    pages=Pages()
    monkeypatch.setitem(sys.modules,'fitz',SimpleNamespace(open=lambda **k:pages))
    calls=[]
    def recognize(data,**options):calls.append(options);return 'Readable complete scan. '*15
    monkeypatch.setattr(ocr,'ocr_pdf',recognize)
    info={}
    text,extension,asset=attachments.read_attachment(b'%PDF-scan',url='https://publisher.test/scan.pdf',extraction_info=info)
    assert len(text)>120 and extension=='pdf' and not asset
    assert calls[0]['max_pages']==9 and info['pages']==9 and info['ocr']
    pages.count=81
    with pytest.raises(FetchError,match='dedicated OCR'):
        attachments.read_attachment(b'%PDF-scan',url='https://publisher.test/scan.pdf')
    assert len(calls)==1
