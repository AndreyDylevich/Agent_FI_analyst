"""Полный мост капитала: независимые исходные суммы, все строки и отсутствие двойного счёта."""

import copy
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from agent import config
from agent.checks.equity import CATALOG, compile_plans
from agent.checks.ifrs import validate_financials
from agent.extract.llm import Completion
from agent.extract.pdf_text import PDFText, Page
from agent.extract.pipeline import extract_pdf, observation_plan
from agent.extract.schemas import parse_extraction, source_context
from tests.test_extraction_schema import PERIOD

FIXTURES = Path(__file__).parent/'fixtures/ifrs'


def inputs():
    reference = json.loads((FIXTURES/'mts_h1_2026_equity_movement_reference.json').read_text())
    saved = json.loads((FIXTURES/'mts_h1_2026_equity_movement_pages.json').read_text())
    pdf = PDFText(saved['pdf_sha256'], tuple(Page(**page) for page in saved['pages']))
    context = source_context(pdf.pages, ticker='MTSS', period=PERIOD, entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',
                             line_items=config.line_items())
    raw = []
    for row in reference['rows']:
        year = int(row['end'][:4]) + (1 if row['item_id']=='eq_opening_total' else 0)
        raw.append({'item_id': row['item_id'], 'original_name': row['original_name'], 'raw_value': row['raw_value'],
                    'number_format': 'comma_grouped_dot_decimal', 'unit': reference['unit'],
                    'unit_evidence': {'page':9,'start_line':5,'end_line':5}, 'evidence': row['evidence'],
                    'column_header': str(year), 'period_start': row['start'], 'period_end': row['end'],
                    'period_type': 'AT_DATE' if row['start'] is None else 'H1', 'statement': 'equity', 'cash_flow_section': None})
    extraction = parse_extraction(json.dumps({**context, 'facts': raw, 'warnings': []}, ensure_ascii=False),
                                  pdf.pages, ticker='MTSS', period=PERIOD,
                                  entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ', line_items=config.line_items())
    return pdf, reference, extraction


def test_source_bound_full_movement_preserves_error_and_current_period_passes():
    pdf, reference, extraction = inputs()
    plans = compile_plans(pdf, PERIOD)
    checks, statuses = validate_financials(extraction.facts, equity_plans=plans)
    bridges = {check.end: check for check in checks if check.rule=='complete_equity_rollforward'}
    assert bridges['2026-06-30'].result.status == 'пройдено' and bridges['2026-06-30'].result.delta == 0
    assert bridges['2025-06-30'].result.status == 'расхождение'
    assert bridges['2025-06-30'].result.delta == Decimal('22468000000')
    assert list(statuses) == [row['status'] for row in reference['rows']]
    neutral = [check for check in checks if check.rule.startswith('equity_neutral_transfer_')]
    assert len(neutral)==2 and all(check.result.delta==0 for check in neutral)
    assert all(check.basis['pdf_sha256']==pdf.sha256 for check in (*bridges.values(),*neutral))


@pytest.mark.parametrize('change', ['omitted_row','overlap','repeat_item','double_profit','missing_neutral_component','wrong_date','hidden_movement'])
def test_invalid_completeness_definition_cannot_approve_source(change):
    pdf, *_ = inputs();catalog=json.loads(CATALOG.read_text())
    plan=catalog['documents'][0]['plans'][1]
    if change=='omitted_row':plan['rows']=[row for row in plan['rows'] if row['items']!=['eq_options_total']]
    if change=='overlap':plan['rows'][5]['evidence']['start_line']=30
    if change=='repeat_item':plan['rows'][-2]['items']=['eq_options_total']
    if change=='double_profit':plan['rows'][1]['items']=['eq_profit_total']
    if change=='missing_neutral_component':plan['rows'][5]['items']=plan['rows'][5]['items'][:1]
    if change=='wrong_date':plan['end']='2026-03-31'
    if change=='hidden_movement':plan['rows'][6].update(kind='comprehensive_component',items=[])
    with pytest.raises(ValueError):compile_plans(pdf,PERIOD,catalog)


def test_missing_movement_is_not_a_zero_and_wrong_window_cannot_fill_it():
    pdf, _, extraction=inputs();plans=compile_plans(pdf,PERIOD)
    rows=tuple(fact for fact in extraction.facts if not (fact.item_id=='eq_options_total' and fact.end.year==2026))
    checks,_=validate_financials(rows,equity_plans=plans)
    bridge=next(check for check in checks if check.rule=='complete_equity_rollforward' and check.end=='2026-06-30')
    assert bridge.result.status=='недостаточно_данных' and bridge.result.missing==('eq_options_total',)


def test_incorrect_neutral_transfer_blocks_whole_bridge_even_if_other_movements_offset():
    pdf, _, extraction=inputs();plans=compile_plans(pdf,PERIOD)
    rows=[]
    for fact in extraction.facts:
        if fact.end.year==2026 and fact.item_id=='eq_reclassification_retained':
            fact=replace(fact,value=fact.value+2,normalized=fact.normalized+2000000)
        if fact.end.year==2026 and fact.item_id=='eq_other_movement_total':
            fact=replace(fact,value=fact.value-2,normalized=fact.normalized-2000000)
        rows.append(fact)
    checks,statuses=validate_financials(tuple(rows),equity_plans=plans)
    bridge=next(check for check in checks if check.rule=='complete_equity_rollforward' and check.end=='2026-06-30')
    assert bridge.result.status=='расхождение'
    assert all(status=='расхождение' for fact,status in zip(rows,statuses) if fact.end.year==2026)


def test_wrong_row_cannot_supply_movement_and_unknown_pdf_has_no_approved_plan():
    pdf, _, extraction=inputs();plans=compile_plans(pdf,PERIOD)
    rows=list(extraction.facts)
    index=next(i for i,fact in enumerate(rows) if fact.item_id=='eq_options_total')
    rows[index]=replace(rows[index],evidence=replace(rows[index].evidence,start_line=23,end_line=23))
    with pytest.raises(ValueError):validate_financials(tuple(rows),equity_plans=plans)
    assert compile_plans(replace(pdf,sha256='f'*64),PERIOD)==()


def test_component_columns_are_explicit_and_not_guessed_from_id_suffix():
    items=config.line_items()
    plan=observation_plan({key:items[key] for key in ('eq_reclassification_retained','eq_reclassification_reserves')},PERIOD)
    assert 'Нераспределенная' in plan[0]['owner_column']
    assert 'Прочие резервы' in plan[2]['owner_column']


class ReferenceProvider:
    """Только тестовый ответ по независимому эталону; не имитирует свежую приёмку GigaChat."""
    def __init__(self, reference):
        self.reference=reference;self.calls=[]
    def extract(self, system, text, schema):
        request=json.loads(text.split('\nПовторная проверка')[0]);self.calls.append(request)
        rows={f"{row['item_id']}__{row['end'].replace('-','_')}":row for row in self.reference['rows']}
        result={}
        for key in schema['properties']['observations']['properties']:
            row=rows.get(key)
            result[key]=None if row is None else {field:row[field] for field in ('original_name','raw_value','evidence')}
            if result[key] is not None:result[key]['cash_flow_section']=None
        return Completion(json.dumps({'number_format':'comma_grouped_dot_decimal','unit':'млн руб.',
            'unit_evidence':{'page':9,'start_line':5,'end_line':5},'observations':result},ensure_ascii=False),
            'independent-reference-fixture',0)


def test_full_pipeline_uses_reviewed_rows_retries_once_and_preserves_all_source_values(tmp_path):
    pdf,reference,_=inputs();items=config.line_items()
    selected={key:items[key] for key in {row['item_id'] for row in reference['rows']}}
    provider=ReferenceProvider(reference)
    run=extract_pdf(pdf,provider,ticker='MTSS',entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',period=PERIOD,
                    line_items=selected,sections=('equity',),output=tmp_path/'output',compact=True)
    assert len(run.facts)==19 and len(run.missing_observations)==5
    assert set(run.statuses)=={'проверено_авто','расхождение'}
    values={(fact.item_id,str(fact.end)):str(fact.value) for fact in run.facts}
    assert values=={(row['item_id'],row['end']):row['value'] for row in reference['rows']}
    assert len(provider.calls)==6  # Четыре сальдо + движение + один повтор полного моста.
    assert any(observation.get('source_absent') for call in provider.calls for observation in call['requested_observations'])


def test_repeat_cannot_hide_original_discrepancy_by_dropping_movement(tmp_path):
    pdf,reference,_=inputs();items=config.line_items()
    class DroppingProvider(ReferenceProvider):
        def extract(self,system,text,schema):
            response=super().extract(system,text,schema)
            if len(self.calls)==6:
                data=json.loads(response.content)
                data['observations']['eq_dividends_declared_total__2025_06_30']=None
                return Completion(json.dumps(data,ensure_ascii=False),response.model,0)
            return response
    provider=DroppingProvider(reference)
    run=extract_pdf(pdf,provider,ticker='MTSS',entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',period=PERIOD,
                    line_items={key:items[key] for key in {row['item_id'] for row in reference['rows']}},
                    sections=('equity',),output=tmp_path/'output',compact=True)
    assert len(run.facts)==19
    assert next(check for check in run.checks if check.rule=='complete_equity_rollforward' and check.end=='2025-06-30').result.delta==22468000000
    assert any(record.get('retained_with_discrepancy') for record in run.batches)
