"""Полный состав долга не подменяется движением денег или выборочными операциями аренды."""
import json
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from agent import config
from agent.checks.debt_bridges import CATALOG, compile_debt_bridges
from agent.checks.ifrs import validate_financials
from agent.extract.llm import Completion
from agent.extract.pdf_text import PDFText, Page
from agent.extract.pipeline import extract_pdf
from agent.extract.schemas import parse_extraction, source_context
from tests.test_extraction_schema import PERIOD

FIXTURES = Path(__file__).parent/'fixtures/ifrs'


def inputs():
    ref=json.loads((FIXTURES/'mts_h1_2026_debt_movement_reference.json').read_text())
    saved=json.loads((FIXTURES/'mts_h1_2026_debt_movement_pages.json').read_text())
    pdf=PDFText(saved['pdf_sha256'],tuple(Page(**p) for p in saved['pages']))
    context=source_context(pdf.pages,ticker='MTSS',period=PERIOD,entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',line_items=config.line_items())
    facts=[{'item_id':r['item_id'],'original_name':r['original_name'],'raw_value':r['raw_value'],
            'number_format':'comma_grouped_dot_decimal','unit':'млн руб.',
            'unit_evidence':{'page':r['pages'][0],'start_line':5,'end_line':5},'evidence':r['evidence'],
            'column_header':r['end'][:4],'period_start':r['start'],'period_end':r['end'],
            'period_type':'H1' if r['start'] else 'AT_DATE','statement':'notes','cash_flow_section':None} for r in ref['rows']]
    extraction=parse_extraction(json.dumps({**context,'facts':facts,'warnings':[]},ensure_ascii=False),pdf.pages,
            ticker='MTSS',period=PERIOD,entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',line_items=config.line_items())
    return pdf,ref,extraction


def test_both_component_bridges_and_total_match_independent_reference():
    pdf,ref,extraction=inputs()
    checks,statuses=validate_financials(extraction.facts,debt_plans=compile_debt_bridges(pdf,PERIOD))
    for expected in ref['checks_expected']:
        c=next(c for c in checks if c.rule==expected['rule'] and c.end==expected['end'])
        assert c.result.status==expected['status'] and c.result.delta==Decimal(expected['delta'])
    assert Counter(statuses)=={'проверено_авто':21}
    assert not any(c.rule=='complete_lease_rollforward' for c in checks)
    assert all(c.start!='2025-01-01' for c in checks if c.rule.startswith('complete_'))


@pytest.mark.parametrize('change',['omitted','overlap','hide_as_heading','wrong_date','no_loans','duplicate','unknown_item','no_movement'])
def test_incomplete_composition_cannot_be_approved(change):
    pdf,*_=inputs();catalog=json.loads(CATALOG.read_text());profile=catalog['documents'][0];b=profile['components'][0]
    if change=='omitted':b['rows'].pop(3)
    if change=='overlap':b['rows'][3]['evidence']['start_line']-=1
    if change=='hide_as_heading':b['rows'][3]={'kind':'heading','evidence':b['rows'][3]['evidence']}
    if change=='wrong_date':b['rows'][0]['evidence']={'page':19,'start_line':18,'end_line':18}
    if change=='no_loans':profile['components'].pop()
    if change=='duplicate':b['rows'][3]['item_id']=b['rows'][2]['item_id']
    if change=='unknown_item':b['rows'][3]['item_id']='lease_principal_paid'
    if change=='no_movement':profile['components'][1]['rows']=profile['components'][1]['rows'][::len(profile['components'][1]['rows'])-1]
    with pytest.raises(ValueError):compile_debt_bridges(pdf,PERIOD,catalog)


@pytest.mark.parametrize('change',['missing','wrong_row','wrong_statement','wrong_period','wrong_sign'])
def test_source_provenance_and_absence_cannot_be_bypassed(change):
    pdf,_,extraction=inputs();facts=list(extraction.facts);plans=compile_debt_bridges(pdf,PERIOD)
    n=next(i for i,f in enumerate(facts) if f.item_id=='note_loans_deconsolidation')
    if change=='missing':facts.pop(n)
    if change=='wrong_row':facts[n]=replace(facts[n],evidence=replace(facts[n].evidence,start_line=52,end_line=52))
    if change=='wrong_statement':facts[n]=replace(facts[n],statement='cash_flow')
    if change=='wrong_period':facts[n]=replace(facts[n],start=PERIOD.shift_years(-1).start,end=PERIOD.shift_years(-1).end)
    if change=='wrong_sign':facts[n]=replace(facts[n],value=-facts[n].value,normalized=-facts[n].normalized)
    if change in ('wrong_row','wrong_statement','wrong_period'):
        with pytest.raises(ValueError):validate_financials(tuple(facts),debt_plans=plans)
    else:
        checks,_=validate_financials(tuple(facts),debt_plans=plans)
        c=next(c for c in checks if c.rule=='complete_debt_rollforward')
        assert c.result.status==('недостаточно_данных' if change=='missing' else 'расхождение')


def test_compensating_component_errors_cannot_pass_total_bridge():
    pdf,_,extraction=inputs();facts=[]
    for f in extraction.facts:
        delta=Decimal('2000000') if f.item_id=='note_bonds_other_change' else Decimal('-2000000') if f.item_id=='note_loans_other_change' else Decimal('0')
        facts.append(replace(f,normalized=f.normalized+delta))
    checks,_=validate_financials(tuple(facts),debt_plans=compile_debt_bridges(pdf,PERIOD))
    assert all(c.result.status=='расхождение' for c in checks if c.rule.startswith('complete_'))


def test_debt_cash_movements_match_cash_flow_without_replacing_noncash_changes():
    from tests.test_ifrs_cash_sections import inputs as cash_inputs
    pdf,_,extraction=inputs();_,_,cash=cash_inputs()
    selected=tuple(f for f in cash.facts if f.item_id in
                   ('cf_bonds_issued','cf_bonds_repaid','cf_loans_raised','cf_loans_repaid'))
    checks,_=validate_financials((*extraction.facts,*selected),debt_plans=compile_debt_bridges(pdf,PERIOD))
    links=[c for c in checks if c.rule.startswith('debt_') and c.rule.endswith('_vs_cash_flow')]
    assert len(links)==4 and all(c.result.status=='пройдено' and c.result.delta==0 for c in links)
    # Сравнительные потоки из ОДДС не заполняют отсутствующий сравнительный мост примечания.
    assert all(c.start=='2026-01-01' for c in links)
    missing=tuple(f for f in extraction.facts if f.item_id!='note_loans_deconsolidation')
    checks,_=validate_financials((*missing,*selected),debt_plans=compile_debt_bridges(pdf,PERIOD))
    assert next(c for c in checks if c.rule=='complete_debt_rollforward').result.status=='недостаточно_данных'


class ReferenceProvider:
    """Только тест кода на эталоне; не свежий ответ модели."""
    def __init__(self,ref):self.ref=ref;self.calls=[]
    def extract(self,system,text,schema):
        request=json.loads(text.split('\nПовторная проверка')[0]);self.calls.append(request)
        rows={f"{r['item_id']}__{r['end'].replace('-','_')}":r for r in self.ref['rows']}
        obs={key:None if key not in rows else {k:rows[key][k] for k in ('original_name','raw_value','evidence','cash_flow_section')}
             for key in schema['properties']['observations']['properties']}
        page=request['target_pages'][0]
        return Completion(json.dumps({'number_format':'comma_grouped_dot_decimal','unit':'млн руб.',
          'unit_evidence':{'page':page,'start_line':5,'end_line':5},'observations':obs},ensure_ascii=False),'independent-reference-fixture',0)


def test_pipeline_extracts_21_source_values_and_keeps_15_undisclosed_comparatives(tmp_path):
    pdf,ref,_=inputs();items=config.line_items();provider=ReferenceProvider(ref)
    selected={key:items[key] for key in sorted({r['item_id'] for r in ref['rows']})}
    run=extract_pdf(pdf,provider,ticker='MTSS',entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',period=PERIOD,
        line_items=selected,sections=('debt',),output=tmp_path/'out',compact=True)
    assert len(run.facts)==21 and len(run.missing_observations)==15
    assert Counter(run.statuses)=={'проверено_авто':21}
    assert len(provider.calls)==4 and all(len(c['target_pages'])==1 for c in provider.calls)
    assert {(f.item_id,str(f.end)):str(f.value) for f in run.facts}=={(r['item_id'],r['end']):r['value'] for r in ref['rows']}
    assert sum(bool(r.get('source_absent')) for c in provider.calls for r in c['requested_observations'])==15


def test_unknown_sha_and_wrong_window_cannot_inherit_structure():
    pdf,*_=inputs()
    assert compile_debt_bridges(replace(pdf,sha256='f'*64),PERIOD)==()
    with pytest.raises(ValueError):compile_debt_bridges(pdf,PERIOD.shift_years(-1))
