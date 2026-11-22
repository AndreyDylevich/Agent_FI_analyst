"""Раскрытия привязаны к источнику и не становятся одобрением сопоставимости по одному слову."""

import json

import pytest

from agent.extract.disclosures import parse_disclosures
from agent.extract.pdf_text import Page


def answer(topic='ifrs16', first=1,last=2):
    return json.dumps({'findings':[{'topic':topic,'evidence':{'page':14,'start_line':first,'end_line':last}}]})


def test_preserves_negation_and_full_source_does_not_assert_comparability():
    pages=(Page(14,'Применён МСФО (IFRS) 16 к продаже с обратной арендой.\nВыбытие не классифицировано как прекращённая деятельность.'),)
    row=parse_disclosures(answer(),pages)[0]
    assert 'не классифицировано' in row.evidence.quote
    assert row.status=='источник_подтверждён_интерпретация_не_проверена'
    assert not row.from_ocr


def test_ifrs18_cannot_be_inferred_from_note_18():
    with pytest.raises(ValueError):
        parse_disclosures(answer('ifrs18',1,1),(Page(14,'18. Прочие обязательства'),))


def test_ocr_and_missing_topics_do_not_imply_verified_interpretation():
    row=parse_disclosures(answer('leases',1,1),(Page(14,'Обязательства по аренде','ocr'),))[0]
    assert row.from_ocr
    assert parse_disclosures('{"findings":[]}',(Page(14,'Прочее'),))==()


def test_unknown_fields_and_fabricated_line_ranges_rejected():
    pages=(Page(14,'МСФО (IFRS) 16'),)
    for data in [answer(last=3),'{"findings":[],"comparability_approved":true}']:
        with pytest.raises(ValueError):parse_disclosures(data,pages)


def test_long_disclosure_preserves_exceptions_without_relaxing_numeric_citations():
    from agent.extract.schemas import citation
    text='Учётная политика НЕ соответствует всем требованиям МСФО.\n'+'\n'.join('Уточнение основы подготовки.' for _ in range(30))
    pages=(Page(14,text),)
    row=parse_disclosures(answer('accounting_policy',1,31),pages)[0]
    assert row.evidence.quote==text and 'НЕ соответствует' in row.evidence.quote
    with pytest.raises(ValueError):citation({'page':14,'start_line':1,'end_line':31},{14:pages[0]})
    with pytest.raises(ValueError):parse_disclosures(answer('accounting_policy',1,81),(Page(14,text+'\n'*60),))


def test_incomplete_disclosure_response_retained_for_diagnosis(tmp_path):
    from agent.extract.disclosures import extract_disclosures
    from agent.extract.llm import Completion,IncompleteResponse
    from agent.extract.pdf_text import PDFText
    class Partial:
        def extract(self,*args):raise IncompleteResponse(Completion('{"findings":[','test',12),'length')
    pdf=PDFText('a'*64,(Page(1,'Примечания\n2. ОСНОВА ПОДГОТОВКИ\nУчётная политика по МСФО.'),))
    with pytest.raises(IncompleteResponse):extract_disclosures(pdf,Partial(),output=tmp_path/'out')
    audit=json.loads((tmp_path/'out/attempts.json').read_text())
    assert audit[0]['finish_reason']=='length' and not audit[0]['accepted']
    assert (tmp_path/'out'/audit[0]['file']).read_text()=='{"findings":['


def test_explicit_standard_mention_cannot_be_silently_omitted(tmp_path):
    from agent.extract.disclosures import extract_disclosures
    from agent.extract.llm import Completion
    from agent.extract.pdf_text import PDFText
    class Provider:
        calls=0
        def extract(self,system,text,schema):
            self.calls+=1
            if self.calls==1:return Completion('{"findings":[]}','test',10)
            assert 'ifrs18' in text
            return Completion(json.dumps({'findings':[{'topic':'ifrs18','evidence':{
                'page':1,'start_line':3,'end_line':3}}]}),'test',10)
    pdf=PDFText('a'*64,(Page(1,'Примечания\n2. ОСНОВА ПОДГОТОВКИ\nМСФО (IFRS) 18 ещё не применён.'),))
    provider=Provider();report=extract_disclosures(pdf,provider,output=tmp_path/'out')
    assert provider.calls==2 and not report['comparability_approved']
    assert 'ещё не применён' in report['findings'][0]['evidence']['quote']
