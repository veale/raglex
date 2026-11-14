import hashlib
from raglex.formats import verified_errata
from raglex.formats.formex import parse_formex_legislation


def test_erratum_is_exact_payload_bound_and_retains_explicit_deleted_marker(monkeypatch):
 raw=b'<ACT><ARTICLE IDENTIFIER="010"><TI.ART>Article 10</TI.ART><PARAG IDENTIFIER="010.005"><NO.PARAG>5.</NO.PARAG><ALINEA>Stale permission.</ALINEA></PARAG></ARTICLE></ACT>'
 assert 'Stale permission' in parse_formex_legislation(raw).text
 rule=next(iter(verified_errata.VERIFIED_DELETIONS.values()))
 monkeypatch.setitem(verified_errata.VERIFIED_DELETIONS,hashlib.sha256(raw).hexdigest(),rule)
 parsed=parse_formex_legislation(raw)
 assert 'Stale permission' not in parsed.text and '[Deleted by Regulation' in parsed.text
 assert parsed.metadata['editorial_corrections'][0]['amending_act']=='32026R1744'
 # A changed publisher payload is never silently edited by this reviewed exception.
 assert 'Stale permission' in parse_formex_legislation(raw+b' ').text
