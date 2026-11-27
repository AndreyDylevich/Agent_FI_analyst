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
            request=json.loads(text)
            assert [page['page'] for page in request['pages']]==[2]
            assert 'verified_context' not in request
            return Completion(json.dumps(compact_answer(),ensure_ascii=False),'test-model',123)
    run=extract_pdf(pipeline_pdf(),Provider(),ticker='TEST',entity_token='Тест',period=PERIOD,
                    line_items={'total_assets':config.line_items()['total_assets']},output=tmp_path,sections=('balance',),compact=True)
    assert len(run.facts)==1 and run.facts[0].value==1234 and str(run.facts[0].end)=='2026-06-30'
    assert run.batches[0]['response_contract']=='compact_v1'
    assert run.batches[0]['sent_pages']==[2]
    assert run.facts[0].model_observation_key=='total_assets__2026_06_30'
    assert any('не равно нулю' in warning for warning in run.warnings)
    assert run.requested_observations_count==2
    assert run.missing_observations==({'item_id':'total_assets','period_start':None,'period_end':'2025-12-31','statement':'balance'},)
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


def test_dated_equity_input_excludes_other_period_rows_and_fixes_source_addresses(tmp_path):
    from agent.extract.pdf_text import PDFText,Page
    from tests.test_extraction_schema import TEXT
    rows={'2024-12-31':('Сальдо на 31 декабря 202      4 года','100'),
          '2025-12-31':('Сальдо на 31 декабря 2025 года','110')}
    page='Консолидированный отчет об изменениях капитала\n'+TEXT+'\n'+rows['2024-12-31'][0]+' 100\nСальдо на 30 июня 2025 года 120\n'+rows['2025-12-31'][0]+' 110'
    class Provider:
        def extract(self,system,text,response_schema):
            request=json.loads(text);plan=request['requested_observations'];assert len(plan)==1
            assert 'Сальдо на 30 июня 2025 года' not in request['pages'][0]['text']
            key='eq_opening_total__'+plan[0]['period_end'].replace('-','_')
            props=response_schema['properties']['observations']['properties'][key]['properties']
            assert props['evidence']['enum']==plan[0]['dated_row_hints']
            label,value=rows[plan[0]['period_end']]
            assert props['original_name']['enum']==[label]
            result={'number_format':'space_grouped_decimal','unit':'млн руб.',
                'unit_evidence':{'page':2,'start_line':3,'end_line':3},'observations':{key:{
                'original_name':label,'raw_value':value,'evidence':props['evidence']['enum'][0],'cash_flow_section':None}}}
            return Completion(json.dumps(result,ensure_ascii=False),'test',100)
    run=extract_pdf(PDFText('a'*64,(Page(1,TEXT),Page(2,page))),Provider(),ticker='TEST',entity_token='Тест',
        period=PERIOD,line_items={'eq_opening_total':config.line_items()['eq_opening_total']},
        output=tmp_path,sections=('equity',),compact=True)
    assert [row.value for row in run.facts]==[100,110]
