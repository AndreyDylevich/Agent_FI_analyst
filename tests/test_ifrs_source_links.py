"""Совпадение чисел не подменяет состав, форму источника или мост начислений к платежам."""
import json
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import config,db
from agent.checks.ifrs import validate_financials
from agent.checks.source_links import CATALOG,compile_source_links
from agent.extract.llm import Completion
from agent.extract.pdf_text import PDFText,Page
from agent.extract.pipeline import extract_pdf
from agent.extract.schemas import parse_extraction,source_context
from agent.extract.store import store_extraction
from agent.tools.verify_ifrs_reference import verify
from tests.test_extraction_schema import PERIOD

FIXTURES=Path(__file__).parent/'fixtures/ifrs'


def inputs():
 ref=json.loads((FIXTURES/'mts_h1_2026_source_links_reference.json').read_text())
 saved=json.loads((FIXTURES/'mts_h1_2026_source_links_pages.json').read_text())
 pdf=PDFText(saved['pdf_sha256'],tuple(Page(**p) for p in saved['pages']))
 context=source_context(pdf.pages,ticker='MTSS',period=PERIOD,entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',line_items=config.line_items())
 facts=[]
 for statement in ('income','cash_flow','notes'):
  rows=[{'item_id':r['item_id'],'original_name':r['original_name'],'raw_value':r['raw_value'],
   'number_format':'comma_grouped_dot_decimal','unit':'млн руб.',
   'unit_evidence':{'page':r['pages'][0],'start_line':6 if statement=='cash_flow' else 5,'end_line':6 if statement=='cash_flow' else 5},
   'evidence':r['evidence'],'column_header':r['end'][:4],'period_start':r['start'],'period_end':r['end'],
   'period_type':'H1','statement':statement,'cash_flow_section':r['cash_flow_section']} for r in ref['rows'] if r['statement']==statement]
  extraction=parse_extraction(json.dumps({**context,'facts':rows,'warnings':[]},ensure_ascii=False),pdf.pages,
   ticker='MTSS',period=PERIOD,entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',line_items=config.line_items())
  facts.extend(extraction.facts)
 return pdf,ref,SimpleNamespace(facts=tuple(facts))


def test_16_reviewed_relations_pass_without_approving_cash_and_interest_bridges():
 pdf,ref,extraction=inputs();review=compile_source_links(pdf,PERIOD)
 checks,statuses=validate_financials(extraction.facts,source_review=review)
 for expected in ref['checks_expected']:
  c=next(c for c in checks if c.rule==expected['rule'] and c.end==expected['end'])
  assert c.result.status=='пройдено' and c.result.delta==0
 assert Counter(statuses)=={'проверено_авто':48,'не_проверено':4}
 assert all(f.item_id!='interest_paid' for f in extraction.facts)
 assert all(c.rule not in ('cash_composition_vs_balance','interest_expense_to_payment_bridge') for c in checks)
 assert len(review.gaps)==4


@pytest.mark.parametrize('change',['scope','rou_inclusion','omit_input','omit_block','omit_row','heading','wrong_form','wrong_section','wrong_terms'])
def test_unreviewed_scope_or_incomplete_source_composition_is_rejected(change):
 pdf,*_=inputs();catalog=json.loads(CATALOG.read_text());p=catalog['documents'][0]
 if change=='scope':p['depreciation_scope']['income_scope']={'page':7,'start_line':31,'end_line':31}
 if change=='rou_inclusion':p['depreciation_scope']['rou_included']={'page':21,'start_line':39,'end_line':39}
 if change=='omit_input':p['inputs'].pop('note_rou_other_da')
 if change=='omit_block':p['complete_blocks'].pop()
 if change=='omit_row':p['complete_blocks'][0]['inputs'].pop()
 if change=='heading':p['complete_blocks'][0]['headings']=[{'page':20,'start_line':51,'end_line':51}]
 if change=='wrong_form':p['inputs']['income_da']['statement']='cash_flow'
 if change=='wrong_section':p['inputs']['cf_capitalized_interest_ppe']['cash_flow_section']='operating'
 if change=='wrong_terms':p['checks']['finance_income_cash_adjustment_vs_note']['note_finance_income_total']=-1
 with pytest.raises(ValueError):compile_source_links(pdf,PERIOD,catalog)


@pytest.mark.parametrize('change',['missing','row','sign','period'])
def test_cross_form_identity_requires_correct_sources_and_full_period(change):
 pdf,_,extraction=inputs();facts=list(extraction.facts)
 n=next(i for i,f in enumerate(facts) if f.item_id=='d_and_a' and f.statement=='income' and f.end.year==2026)
 if change=='missing':facts.pop(n)
 if change=='row':facts[n]=replace(facts[n],evidence=replace(facts[n].evidence,page=10,start_line=15,end_line=16))
 if change=='sign':facts[n]=replace(facts[n],normalized=facts[n].normalized+Decimal('2000000'))
 if change=='period':facts[n]=replace(facts[n],start=PERIOD.shift_years(-1).start,end=PERIOD.shift_years(-1).end)
 if change=='row':
  with pytest.raises(ValueError):validate_financials(tuple(facts),source_review=compile_source_links(pdf,PERIOD))
 else:
  checks,_=validate_financials(tuple(facts),source_review=compile_source_links(pdf,PERIOD))
  c=next(c for c in checks if c.rule=='depreciation_composition_vs_cash_flow' and c.end=='2026-06-30')
  assert c.result.status==('расхождение' if change=='sign' else 'недостаточно_данных')


class ReferenceProvider:
 def __init__(self,ref):self.ref=ref;self.calls=[]
 def extract(self,system,text,schema):
  req=json.loads(text.split('\nПовторная проверка')[0]);self.calls.append(req)
  rows={f"{r['item_id']}__{r['end'].replace('-','_')}":r for r in self.ref['rows'] if r['statement']==req['source_statement']}
  obs={key:None if key not in rows else {k:rows[key][k] for k in ('original_name','raw_value','evidence','cash_flow_section')}
       for key in schema['properties']['observations']['properties']}
  page=req['target_pages'][0];line=6 if req['source_statement']=='cash_flow' else 5
  return Completion(json.dumps({'number_format':'comma_grouped_dot_decimal','unit':'млн руб.',
   'unit_evidence':{'page':page,'start_line':line,'end_line':line},'observations':obs},ensure_ascii=False),'independent-reference-fixture',0)


def extract_reference(tmp_path):
 pdf,ref,_=inputs();items=config.line_items();provider=ReferenceProvider(ref)
 selected={key:items[key] for key in sorted({r['item_id'] for r in ref['rows']})}
 run=extract_pdf(pdf,provider,ticker='MTSS',entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',period=PERIOD,
  line_items=selected,sections=('income','cash_flow','interest','leases'),output=tmp_path/'out',compact=True)
 return run,ref,provider


def test_pipeline_preserves_52_source_observations_in_five_requests(tmp_path):
 run,ref,provider=extract_reference(tmp_path)
 assert len(run.facts)==52 and not run.missing_observations
 assert run.requested_observations_count==52 and len(run.reviewed_disclosure_gaps)==4
 assert Counter(run.statuses)=={'проверено_авто':48,'не_проверено':4}
 assert len(provider.calls)==5 and all(len(c['target_pages'])==1 for c in provider.calls)
 assert {(f.item_id,f.statement,str(f.end)):str(f.value) for f in run.facts}=={(r['item_id'],r['statement'],r['end']):r['value'] for r in ref['rows']}
 assert len([f for f in run.facts if f.item_id=='d_and_a'])==4


def test_reference_cannot_substitute_another_form_for_requested_depreciation_source(tmp_path):
 run,ref,_=extract_reference(tmp_path);conn=db.init_db(tmp_path/'check.db')
 try:
  with conn:store_extraction(conn,run,ticker='MTSS',name='МТС',source_url=ref['source_url'],period=PERIOD,local_path='source.pdf')
  assert verify(conn,ref)['ok']
  rows=conn.execute("SELECT id,location FROM source_values WHERE item_id='d_and_a'").fetchall()
  with conn:
   for row in rows:
    if json.loads(row['location'])['page']==7:conn.execute('DELETE FROM source_values WHERE id=?',(row['id'],))
  result=verify(conn,ref)
  assert not result['ok']
  assert not result['selected_values'][0]['passed'] and result['selected_values'][2]['passed']
 finally:conn.close()


def test_unknown_pdf_cannot_inherit_reviewed_scope():
 pdf,*_=inputs();assert compile_source_links(replace(pdf,sha256='f'*64),PERIOD) is None


def test_missing_income_source_is_visible_even_when_cash_flow_has_same_amount(tmp_path):
 pdf,ref,_=inputs();items=config.line_items()
 altered={**ref,'rows':[r for r in ref['rows'] if not (r['item_id']=='d_and_a' and r['statement']=='income' and r['end']=='2025-06-30')]}
 selected={key:items[key] for key in sorted({r['item_id'] for r in ref['rows']})}
 run=extract_pdf(pdf,ReferenceProvider(altered),ticker='MTSS',entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',period=PERIOD,
  line_items=selected,sections=('income','cash_flow','interest','leases'),output=tmp_path/'out',compact=True)
 assert run.requested_observations_count==52 and len(run.facts)==51
 assert run.missing_observations==({'item_id':'d_and_a','period_start':'2025-01-01','period_end':'2025-06-30','statement':'income'},)
