from types import SimpleNamespace
from raglex.adapters._cellar_pdf import fetch_pdf


def test_multiple_choice_selects_judgment_and_never_hearing_report():
    manifest = b'''<ul><li title="item"><a href="http://publications.europa.eu/DOC_1">PDF</a><ul><li title="stream_name">C_1988_0215_ARRET_EN.pdf</li></ul></li><li title="item"><a href="https://publications.europa.eu/DOC_2">PDF</a><ul><li title="stream_name">C_1988_0215_RAPP_EN.pdf</li></ul></li></ul>'''
    class Client:
        calls = []
        def get(self, url, **kwargs):
            self.calls.append(url)
            return SimpleNamespace(content=manifest if len(self.calls)==1 else b'%PDF-judgment', status_code=300 if len(self.calls)==1 else 200)
    client = Client()
    assert fetch_pdf(client, 'https://publications.europa.eu/resource/celex/61988CJ0215', judgment=True) == b'%PDF-judgment'
    assert client.calls[-1] == 'https://publications.europa.eu/DOC_1'


def test_multiple_parts_are_not_silently_treated_as_complete():
    manifest=b'<ul>'+b''.join(b'<li title="item"><a href="https://publications.europa.eu/'+str(n).encode()+b'">PDF</a><li title="stream_name">ARRET.pdf</li></li>' for n in (1,2))+b'</ul>'
    class Client:
        def get(self, *args, **kwargs):
            return SimpleNamespace(content=manifest, status_code=300)
    assert fetch_pdf(Client(), 'https://publications.europa.eu/resource/celex/test', judgment=True) is None


def test_pre_1989_docket_is_not_given_a_modern_c_prefix():
    from raglex.citations.oscola import _celex_case_no
    assert _celex_case_no('61988CJ0215') == '215/88'
    assert _celex_case_no('62003CJ0403') == 'C-403/03'
