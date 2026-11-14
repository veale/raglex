"""Danish drafting through the production extractor, not just isolated regexes."""
import pytest
from raglex.citations.extractor import extract_citations, shorthand_defs


@pytest.mark.parametrize('text,target,pin', [
 ('artikel 6, stk. 1, litra f, i forordning (EU) 2016/679','32016R0679','Article 6(1)(f)'),
 ('forordning (EF) nr. 1907/2006','32006R1907',None),
 ('direktiv 95/46/EF','31995L0046',None),
 ('direktiv (EU) 2016/680','32016L0680',None),
 ('afgørelse 2000/520/EF','32000D0520',None),
 ('sag C-311/18, præmis 53 og 54','62018CJ0311','para 53 and 54'),
 ('forenede sager C-293/12 og C-594/12, præmis 27','62012CJ0594','para 27'),
 ('ECLI:EU:C:2020:559, præmis 52','ECLI:EU:C:2020:559','para 52'),
 ('artikel 15, stk. 2 i Rådets forordning (EØF) nr. 3821/85','31985R3821','Article 15(2)'),
 ('Europa-Parlamentets og Rådets forordning (EF) nr. 882 af 29. april 2004','32004R0882',None),
 ('TEUF','12012E',None),
 ('artikel 267, stk. 1, i TEUF','12012E','Article 267(1)'),
 ('BS-12345/2020-HJR','dk/docket/BS-12345/2020-HJR',None),
 ('U.2020.1234 H','dk/ufr/2020/1234/h',None),
 ('databeskyttelseslovens § 22, stk. 2, nr. 4','dk:lov:databeskyttelsesloven','§ 22, stk. 2, nr. 4'),
])
def test_danish_crossreferences(text,target,pin):
    cites=extract_citations(text)
    assert (target,pin) in {(c.candidate_id,c.pinpoint) for c in cites}
    assert all(text[c.char_start:c.char_end]==c.raw for c in cites)


@pytest.mark.parametrize('cue',['herefter','i det følgende','efterfølgende benævnt'])
def test_danish_defined_act_shorthand_and_pinpoint(cue):
    text=f'forordning (EU) 2024/1689 ({cue} AI-forordningen). AI-forordningen, artikel 5, stk. 1, litra a.'
    cites=extract_citations(text)
    assert any(c.method=='shorthand' and c.candidate_id=='32024R1689' and c.pinpoint=='Article 5(1)(a)' for c in cites)


def test_danish_case_short_name_requires_pinpoint():
    cites=extract_citations('C-311/18 (herefter Schrems). Schrems, præmis 52. Schrems er omtalt.')
    uses=[c for c in cites if c.method=='shorthand']
    assert len(uses)==1 and uses[0].pinpoint=='para 52'


def test_danish_ambiguous_noun_stays_document_local():
    text='forordning (EU) 2024/1689 (herefter forordningen). Forordningen, artikel 5, stk. 1.'
    cites=extract_citations(text)
    assert any(c.method=='doc_host' and c.candidate_id=='32024R1689' and c.pinpoint=='Article 5(1)' for c in cites)
    assert not any(d['shorthand'].lower()=='forordningen' for d in shorthand_defs(text,cites))
    assert not any(c.candidate_id=='32024R1689' for c in extract_citations('Forordningen, artikel 5, stk. 1.'))


def test_danish_carry_forward_switches_act_and_preserves_subdivisions():
    text='forordning (EU) 2024/1689. Artikel 5, stk. 1, litra a. Direktiv 2000/31/EF. Artikel 14, stk. 1.'
    pairs={(c.candidate_id,c.pinpoint) for c in extract_citations(text)}
    assert ('32024R1689','Article 5(1)(a)') in pairs
    assert ('32000L0031','Article 14(1)') in pairs


def test_no_phantom_eu_year_or_arbitrary_regulation():
    cites=extract_citations('forordning 9999/8888. Artikel 5 i forordningen om test.')
    assert not any(c.candidate_id=='32016R0679' or (c.candidate_id or '').startswith('39999') for c in cites)


def test_domestic_dated_decision_is_not_guessed_as_eu_law():
    assert not any(c.method=='dk_eu_dated_instrument' for c in extract_citations('afgørelse nr. 882 af 29. april 2004'))
