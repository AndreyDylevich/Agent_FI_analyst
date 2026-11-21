"""Модель выбирает исходные ячейки; даты/статьи и проверки закреплены Python без расчёта сумм ИИ."""

import json

import pytest

from agent import config
from agent.extract.compact import expand,schema
from agent.extract.llm import Completion
from agent.extract.pipeline import extract_pdf
from tests.test_extraction_schema import PERIOD
from tests.test_ifrs_pipeline import pipeline_pdf


def compact_answer():
    return {'number_format':'space_grouped_decimal','unit':'млн руб.',
        'unit_evidence':{'page':2,'start_line':3,'end_line':3},'observations':{
        'total_assets__2026_06_30':{'original_name':'Итого активы','raw_value':'1 234',
            'evidence':{'page':2,'start_line':5,'end_line':5},'cash_flow_section':None},
        'total_assets__2025_12_31':None}}


def test_compact_pipeline_binds_periods_and_keeps_missing_fact_missing(tmp_path):
    class Provider:
        def extract(self,system,text,response_schema):
            assert set(response_schema['properties'])=={'number_format','unit','unit_evidence','observations'}
            assert set(response_schema['properties']['observations']['properties'])==set(compact_answer()['observations'])
            return Completion(json.dumps(compact_answer(),ensure_ascii=False),'test-model',123)
    run=extract_pdf(pipeline_pdf(),Provider(),ticker='TEST',entity_token='Тест',period=PERIOD,
                    line_items={'total_assets':config.line_items()['total_assets']},output=tmp_path,sections=('balance',),compact=True)
    assert len(run.facts)==1 and run.facts[0].value==1234 and str(run.facts[0].end)=='2026-06-30'
    assert run.batches[0]['response_contract']=='compact_v1'
    assert any('не равно нулю' in warning for warning in run.warnings)
    assert 'observations' in json.loads((tmp_path/run.batches[0]['file']).read_text())


def test_compact_response_cannot_add_dates_or_calculated_fields():
    plan=[{'item_id':'total_assets','period_start':None,'period_end':'2026-06-30','period_type':'AT_DATE','column_header':'2026'}]
    data=compact_answer();data['observations'].pop('total_assets__2025_12_31')
    assert json.loads(expand(json.dumps(data),plan=plan,context={'ticker':'TEST'},statement='balance'))['facts'][0]['raw_value']=='1 234'
    data['observations']['total_assets__2027_06_30']=None
    with pytest.raises(ValueError):expand(json.dumps(data),plan=plan,context={'ticker':'TEST'},statement='balance')
    data['observations'].pop('total_assets__2027_06_30')
    data['observations']['total_assets__2026_06_30']['check_status']='проверено_авто'
    with pytest.raises(ValueError):expand(json.dumps(data),plan=plan,context={'ticker':'TEST'},statement='balance')


def test_compact_context_must_be_verified_before_expansion():
    with pytest.raises(ValueError):expand(json.dumps(compact_answer()),plan=[],context={},statement='balance')
