"""Состав ОДДС: пропуски и прочерки не становятся нулями, оттоки не теряют знак вклада."""

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from agent import config
from agent.checks.cash_sections import CATALOG, compile_cash_sections
from agent.checks.ifrs import validate_financials
from agent.extract.llm import Completion
from agent.extract.pdf_text import PDFText, Page
from agent.extract.pipeline import extract_pdf
from agent.extract.schemas import parse_extraction, source_context
from tests.test_extraction_schema import PERIOD

FIXTURES=Path(__file__).parent/'fixtures/ifrs'


def inputs():
    reference=json.loads((FIXTURES/'mts_h1_2026_cash_sections_reference.json').read_text())
    saved=json.loads((FIXTURES/'mts_h1_2026_cash_sections_pages.json').read_text())
    pdf=PDFText(saved['pdf_sha256'],tuple(Page(**page) for page in saved['pages']))
    context=source_context(pdf.pages,ticker='MTSS',period=PERIOD,entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',line_items=config.line_items())
    facts=[{'item_id':row['item_id'],'original_name':row['original_name'],'raw_value':row['raw_value'],
            'number_format':'comma_grouped_dot_decimal','unit':'млн руб.',
            'unit_evidence':{'page':row['pages'][0],'start_line':6,'end_line':6},'evidence':row['evidence'],
            'column_header':row['end'][:4],'period_start':row['start'],'period_end':row['end'],'period_type':'H1',
            'statement':'cash_flow','cash_flow_section':row['cash_flow_section']} for row in reference['rows']]
    extraction=parse_extraction(json.dumps({**context,'facts':facts,'warnings':[]},ensure_ascii=False),pdf.pages,
        ticker='MTSS',period=PERIOD,entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',line_items=config.line_items())
    return pdf,reference,extraction


def test_independent_reference_checks_two_complete_windows_and_keeps_four_incomplete():
    pdf,reference,extraction=inputs();plans=compile_cash_sections(pdf,PERIOD)
    checks,statuses=validate_financials(extraction.facts,cash_plans=plans)
    sections=[check for check in checks if check.rule.startswith('complete_')]
    assert len(sections)==6
    for expected in reference['checks_expected']:
        actual=next(check for check in sections if check.rule==expected['rule'] and check.end==expected['end'])
        assert actual.result.status==expected['status']
        if 'delta' in expected:assert actual.result.delta==0
    assert list(statuses)==[row['status'] for row in reference['rows']]
    assert Counter(statuses)=={'проверено_авто':33,'не_проверено':51}
    assert all(check.basis['dash_is_zero'] is False for check in sections)


@pytest.mark.parametrize('change',['omitted','overlap','duplicate','wrong_total','wrong_sign','hide_as_heading'])
def test_incomplete_or_invalid_composition_is_rejected(change):
    pdf,*_=inputs();catalog=json.loads(CATALOG.read_text());section=catalog['documents'][0]['sections'][0]
    if change=='omitted':section['rows'].pop(3)
    if change=='overlap':section['rows'][3]['evidence']['start_line']-=1
    if change=='duplicate':section['rows'][3]['item_id']=section['rows'][1]['item_id']
    if change=='wrong_total':section['total_item']='cfi'
    if change=='wrong_sign':section['rows'][3]['coefficient']=-1
    if change=='hide_as_heading':section['rows'][3]={'kind':'heading','evidence':section['rows'][3]['evidence']}
    with pytest.raises(ValueError):compile_cash_sections(pdf,PERIOD,catalog)


@pytest.mark.parametrize('change',['missing','wrong_section','wrong_row','wrong_period','wrong_sign'])
def test_source_identity_and_missing_inputs_cannot_be_bypassed(change):
    pdf,_,extraction=inputs();rows=list(extraction.facts);plans=compile_cash_sections(pdf,PERIOD)
    index=next(i for i,fact in enumerate(rows) if fact.item_id=='cf_expected_credit_losses' and fact.end.year==2026)
    if change=='missing':rows.pop(index)
    if change=='wrong_period':rows[index]=replace(rows[index],start=PERIOD.shift_years(-1).start,end=PERIOD.shift_years(-1).end)
    if change=='wrong_section':rows[index]=replace(rows[index],cash_flow_section='financing')
    if change=='wrong_row':rows[index]=replace(rows[index],evidence=replace(rows[index].evidence,start_line=18,end_line=18))
    if change=='wrong_sign':rows[index]=replace(rows[index],value=-rows[index].value,normalized=-rows[index].normalized)
    if change in ('wrong_section','wrong_row'):
        with pytest.raises(ValueError):validate_financials(tuple(rows),cash_plans=plans)
    else:
        checks,_=validate_financials(tuple(rows),cash_plans=plans)
        current=next(check for check in checks if check.rule=='complete_cfo_components' and check.end=='2026-06-30')
        assert current.result.status==('расхождение' if change=='wrong_sign' else 'недостаточно_данных')


def test_paid_interest_net_of_capitalization_does_not_become_full_interest_paid():
    _,_,extraction=inputs()
    assert any(f.item_id=='cf_interest_paid_ex_capitalization' and f.value<0 for f in extraction.facts)
    assert all(f.item_id!='interest_paid' for f in extraction.facts)
    for fact in extraction.facts:
        if fact.item_id in ('capex_ppe','capex_intangibles','lease_principal_paid','dividends_paid'):
            assert fact.value<0 and fact.normalized>0


def test_explicit_dash_cannot_be_filled_from_other_year_even_when_number_is_in_quote():
    pdf,_,extraction=inputs();plans=compile_cash_sections(pdf,PERIOD)
    prior=next(fact for fact in extraction.facts if fact.item_id=='cf_swap_cash_flows')
    wrong=replace(prior,start=PERIOD.start,end=PERIOD.end)
    with pytest.raises(ValueError,match='Прочерк'):
        validate_financials((*extraction.facts,wrong),cash_plans=plans)


class ReferenceProvider:
    """Тестовый эталонный ответ; не свежая генерация GigaChat."""
    def __init__(self,reference):self.reference=reference;self.calls=[]
    def extract(self,system,text,schema):
        request=json.loads(text.split('\nПовторная проверка')[0]);self.calls.append(request)
        rows={f"{row['item_id']}__{row['end'].replace('-','_')}":row for row in self.reference['rows']}
        observations={}
        for key in schema['properties']['observations']['properties']:
            row=rows.get(key)
            observations[key]=None if row is None else {field:row[field] for field in ('original_name','raw_value','evidence','cash_flow_section')}
        page=request['target_pages'][0]
        return Completion(json.dumps({'number_format':'comma_grouped_dot_decimal','unit':'млн руб.',
            'unit_evidence':{'page':page,'start_line':6,'end_line':6},'observations':observations},ensure_ascii=False),'independent-reference-fixture',0)


def test_pipeline_extracts_84_values_without_filling_dashes_or_sending_wrong_sections(tmp_path):
    pdf,reference,_=inputs();items=config.line_items();provider=ReferenceProvider(reference)
    selected={key:items[key] for key in sorted({row['item_id'] for row in reference['rows']})}
    run=extract_pdf(pdf,provider,ticker='MTSS',entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',period=PERIOD,
                    line_items=selected,sections=('cash_flow',),output=tmp_path/'out',compact=True)
    assert len(run.facts)==84 and len(run.missing_observations)==4
    assert Counter(run.statuses)=={'проверено_авто':33,'не_проверено':51}
    assert {(f.item_id,str(f.end)):str(f.value) for f in run.facts}=={(r['item_id'],r['end']):r['value'] for r in reference['rows']}
    assert len(provider.calls)==6 and all(len(call['target_pages'])==1 for call in provider.calls)
    assert sum(bool(row.get('source_absent')) for call in provider.calls for row in call['requested_observations'])==4


def test_unapproved_pdf_cannot_obtain_full_section_completeness():
    pdf,*_=inputs()
    assert compile_cash_sections(replace(pdf,sha256='f'*64),PERIOD)==()
