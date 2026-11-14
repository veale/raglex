import pytest
from raglex.adapters.fr_conseil_etat import parse_ariane, ariane_url, FrConseilEtatAdapter
from raglex.core.errors import FetchError
from raglex.citations.snowball import _classify

ID='ECLI:FR:CESSR:2012:351163.20121126'
RAW=f'''<div class="article__main"><div class="ezrichtext-field"><strong>{ID}</strong><br>Lecture du 26 novembre 2012<br>1. Considérant que le recours concerne Betclic.<br>2. Le recours est rejeté.<br>3. Les motifs sont exposés.<br>4. La demande est examinée.<br>5. La décision est notifiée.</div></div>'''.encode()


def test_ariane_checks_identity_and_preserves_publisher_bytes():
    record=parse_ariane(RAW,ID)
    assert record.ecli==ID and record.raw_bytes==RAW
    assert str(record.decision_date)=='2012-11-26'
    assert '351163' in record.landing_url and 'Betclic' in record.text
    assert len(record.segments)>=5
    with pytest.raises(FetchError):parse_ariane(RAW,ID.replace('351163','999999'))
    with pytest.raises(FetchError):parse_ariane(b'<html>Challenge</html>',ID)


def test_real_conseil_court_code_routes_to_administrative_order():
    # classify returns (kind, jurisdiction, adapter).
    assert _classify(ID,"case")[2]=='fr-conseil-etat'
    assert '2012-11-26/351163' in ariane_url(ID)
    stub=next(FrConseilEtatAdapter(ids=ID).discover(None))
    assert stub.stable_id==ID and stub.raw_url==ariane_url(ID)
