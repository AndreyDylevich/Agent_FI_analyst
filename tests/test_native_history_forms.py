"""Annual figures retain original scope, unit, sign and reported rounding differences."""
import json
from pathlib import Path
from datetime import date
from dataclasses import asdict
from copy import deepcopy

import pytest

from agent import config,db
from agent.checks.ifrs import validate_financials
from agent.checks.reviewed_tables import compile_reviewed_tables,validate_reviewed_columns
from agent.extract.pdf_text import PDFText,Page
from agent.extract.schemas import source_context,parse_extraction
from agent.extract.pipeline import ExtractionRun
from agent.extract.store import store_extraction
from agent.metrics.engine import Period
from agent.tools import verify_ifrs_history_forms as tool

FIXTURES=Path(__file__).parent/'fixtures/ifrs'
CASES=['mts_fy2021','mts_fy2023','mts_fy2025','sgzh_fy2021','sgzh_fy2023','sgzh_fy2025']


def inputs(case):
 ref=json.loads((FIXTURES/f'{case}_income_cash_reference.json').read_text());saved=json.loads((FIXTURES/f'{case}_income_cash_pages.json').read_text());pdf=PDFText(saved['pdf_sha256'],tuple(Page(**p) for p in saved['pages']));period=Period(date(ref['year'],1,1),date(ref['year'],12,31),'FY');review=compile_reviewed_tables(pdf,period);items=config.line_items();token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ' if ref['ticker']=='MTSS' else 'СЕГЕЖА';context=source_context(pdf.pages,ticker=ref['ticker'],period=period,entity_token=token,line_items=items);assert context
 facts=[]
 for r in ref['rows']:
  o=next(o for o in review.observations.values() if (o['item_id'],o['statement'],o['period_start'],o['period_end'])==(r['item_id'],r['statement'],r['start'],r['end']))
  fact={'item_id':r['item_id'],'statement':r['statement'],'period_start':r['start'],'period_end':r['end'],'period_type':o['period_type'],'original_name':o['original_name_hints'][0],'raw_value':r['raw_value'],'number_format':review.basis['number_format'],'unit':ref['unit'],'unit_evidence':o['unit_evidence'],'evidence':r['evidence'],'column_header':o['column_header'],'cash_flow_section':r['cash_flow_section']}
  parsed=parse_extraction(json.dumps({**context,'facts':[fact],'warnings':[]},ensure_ascii=False),pdf.pages,ticker=ref['ticker'],period=period,entity_token=token,line_items=items);facts.extend(parsed.facts)
 validate_reviewed_columns(facts,review,pdf.pages)
 checks,statuses=validate_financials(tuple(facts),reviewed_document=review)
 return ref,pdf,period,review,tuple(facts),checks,statuses,context


@pytest.mark.parametrize('case',CASES)
def test_independent_annual_references_keep_different_source_scopes(case):
 ref,pdf,period,review,facts,checks,statuses,context=inputs(case)
 assert list(statuses)==[r['status'] for r in ref['rows']]
 for expected in ref['checks_expected']:
  matches=[c for c in checks if (c.rule,c.start,c.end)==(expected['rule'],expected['start'],expected['end']) and c.basis.get('source_structure_reviewed')]
  assert matches and all(c.result.status==expected['status'] and ('delta'not in expected or str(c.result.delta)==expected['delta']) for c in matches)
 assert not any(c.result.status=='расхождение' for c in checks)
 if case=='mts_fy2023':
  assert next(c for c in checks if c.rule=='annual_net_profit_vs_cf_start' and c.end=='2022-12-31').result.delta==1000000
 if case=='sgzh_fy2021':
  assert {f.unit for f in facts}=={'тыс. руб.'}
  assert all(f.item_id!='d_and_a' for f in facts)
 if case=='sgzh_fy2023':assert all(f.item_id!='d_and_a' for f in facts)


def sample(tmp_path):
 ref,pdf,period,review,facts,checks,statuses,context=inputs('mts_fy2023');conn=db.init_db(tmp_path/'check.db')
 run=ExtractionRun(pdf.sha256,(),(context,),facts,tuple(checks),tuple(statuses),(),reviewed_disclosure_gaps=())
 with conn:store_extraction(conn,run,ticker='MTSS',name='Тест',source_url=ref['source_url'],period=period,local_path='synthetic_test.pdf')
 return conn,{'ticker':'MTSS','expected_year':2023,'pdf_sha256':pdf.sha256,'source_url':ref['source_url']},ref


def test_history_forms_recheck_is_read_only_and_does_not_approve_comparability(tmp_path):
 conn,entry,ref=sample(tmp_path);before='\n'.join(conn.iterdump());result=tool.verify_history(conn,'MTSS',[entry],{2023:ref},[2023]);assert result['ok'],result
 assert result['reported_flow_years_verified']==[2022,2023] and not result['comparability_approved']
 assert not result['financial_history_validated'] and before=='\n'.join(conn.iterdump())
 conn.close()


@pytest.mark.parametrize('change',['source','year','missing','raw','address','currency','scope','protocol'])
def test_history_forms_cannot_accept_wrong_source_column_or_window(tmp_path,change):
 conn,entry,ref=sample(tmp_path)
 if change=='source':ref['pdf_sha256']='f'*64
 if change=='year':ref['year']=2024
 if change=='missing':ref['rows'].pop(0)
 if change=='raw':ref['rows'][0]['raw_value']='605,992'
 if change=='address':ref['rows'][0]['evidence']['start_line']+=1
 if change=='currency':conn.execute("UPDATE source_values SET currency='USD'")
 if change=='scope':conn.execute("UPDATE source_values SET scope='segment',segment_name='Other'")
 if change=='protocol':conn.execute("UPDATE meta SET value=value||' ' WHERE key LIKE 'ifrs_extraction:%'")
 assert not tool.verify_history(conn,'MTSS',[entry],{2023:ref},[2023])['ok'];conn.close()
