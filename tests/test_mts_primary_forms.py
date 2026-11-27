"""All displayed money in MTS balance, P&L and OCI; bank lines stay separate from corporate debt."""
import json
from collections import Counter
from pathlib import Path

from agent import config
from agent.checks.ifrs import validate_financials
from agent.checks.reviewed_tables import compile_reviewed_tables,validate_reviewed_columns
from agent.extract.pdf_text import PDFText,Page
from agent.extract.pipeline import extract_pdf
from agent.extract.schemas import parse_extraction,source_context
from agent.extract.llm import Completion
from tests.test_extraction_schema import PERIOD

FIXTURES=Path(__file__).parent/'fixtures/ifrs'


def inputs():
 ref=json.loads((FIXTURES/'mts_h1_2026_primary_forms_reference.json').read_text());saved=json.loads((FIXTURES/'mts_h1_2026_primary_forms_pages.json').read_text());pdf=PDFText(saved['pdf_sha256'],tuple(Page(**p) for p in saved['pages']));review=compile_reviewed_tables(pdf,PERIOD);items=config.line_items();context=source_context(pdf.pages,ticker='MTSS',period=PERIOD,entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',line_items=items);assert context
 facts=[]
 for r in ref['rows']:
  o=next(o for o in review.observations.values() if (o['item_id'],o['statement'],o['period_start'],o['period_end'])==(r['item_id'],r['statement'],r['start'],r['end']))
  row={'item_id':r['item_id'],'statement':r['statement'],'period_start':r['start'],'period_end':r['end'],'period_type':o['period_type'],'original_name':r['original_name'],'raw_value':r['raw_value'],'number_format':'comma_grouped_dot_decimal','unit':'млн руб.','unit_evidence':o['unit_evidence'],'evidence':r['evidence'],'column_header':o['column_header'],'cash_flow_section':None}
  parsed=parse_extraction(json.dumps({**context,'facts':[row],'warnings':[]},ensure_ascii=False),pdf.pages,ticker='MTSS',period=PERIOD,entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',line_items=items);facts.extend(parsed.facts)
 return pdf,ref,review,tuple(facts)


def assert_checks(checks,ref):
 for expected in ref['checks_expected']:
  found=[c for c in checks if (c.rule,c.start,c.end)==(expected['rule'],expected['start'],expected['end']) and (c.basis or {}).get('source_structure_reviewed')]
  assert found and all(c.result.status==expected['status'] and ('delta'not in expected or str(c.result.delta)==expected['delta']) for c in found),expected


def test_all_primary_form_money_matches_visual_reference_without_inventing_dash_zero():
 pdf,ref,review,facts=inputs();validate_reviewed_columns(facts,review,pdf.pages);checks,statuses=validate_financials(facts,reviewed_document=review);assert_checks(checks,ref)
 assert len(facts)==166 and Counter(statuses)=={'проверено_авто':150,'не_проверено':16}
 assert list(statuses)==[r['status'] for r in ref['rows']]
 assert next(c for c in checks if c.rule=='complete_noncurrent_assets' and c.end=='2025-12-31').result.status=='недостаточно_данных'
 assert all(f.item_id not in ('bank_loans','other_borrowings') for f in facts)


class Provider:
 def __init__(self,ref):self.ref=ref
 def extract(self,system,text,schema):
  req=json.loads(text.split('\nПовторная проверка')[0]);rows={f"{r['item_id']}__{r['end'].replace('-','_')}":r for r in self.ref['rows'] if r['statement']==req['source_statement']};obs={k:None if k not in rows else {n:rows[k][n] for n in ('original_name','raw_value','evidence','cash_flow_section')} for k in schema['properties']['observations']['properties']}
  return Completion(json.dumps({'number_format':'comma_grouped_dot_decimal','unit':'млн руб.','unit_evidence':schema['properties']['unit_evidence']['enum'][0],'observations':obs},ensure_ascii=False),'independent-test-fixture',0)


def test_pipeline_covers_both_columns_of_every_displayed_primary_row(tmp_path):
 pdf,ref,review,facts=inputs();items=config.line_items();selected={o['item_id']:items[o['item_id']] for o in review.observations.values()}
 run=extract_pdf(pdf,Provider(ref),ticker='MTSS',entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',period=PERIOD,line_items=selected,sections=('balance','income'),output=tmp_path/'out',compact=True)
 assert len(run.facts)==166 and run.requested_observations_count==168 and len(run.missing_observations)==2
 assert_checks(run.checks,ref)
 assert Counter(run.statuses)=={'проверено_авто':150,'не_проверено':16}
