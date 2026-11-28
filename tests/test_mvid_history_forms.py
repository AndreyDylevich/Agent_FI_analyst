"""Full original annual MVID forms retain special-purpose scope, OCR corrections and absent FX."""
import json
from pathlib import Path
from collections import Counter
from datetime import date

import pytest

from agent import config
from agent.checks.ifrs import validate_financials
from agent.checks.reviewed_tables import compile_reviewed_tables,validate_reviewed_columns
from agent.extract.pdf_text import PDFText,Page
from agent.extract.schemas import source_context,parse_extraction
from agent.extract.pipeline import extract_pdf
from agent.extract.llm import Completion
from agent.metrics.engine import Period

FIXTURES=Path(__file__).parent/'fixtures/ifrs'


def inputs(year):
 ref=json.loads((FIXTURES/f'mvid_fy{year}_income_cash_reference.json').read_text());saved=json.loads((FIXTURES/f'mvid_fy{year}_income_cash_pages.json').read_text());pdf=PDFText(saved['pdf_sha256'],tuple(Page(**p) for p in saved['pages']),tuple(saved.get('warnings',[])),saved.get('processing',{}));period=Period(date(year,1,1),date(year,12,31),'FY');review=compile_reviewed_tables(pdf,period);context=source_context(pdf.pages,ticker='MVID',period=period,entity_token='М.ВИДЕО',line_items=config.line_items());assert context
 facts=[]
 for r in ref['rows']:
  o=next(o for o in review.observations.values() if (o['item_id'],o['statement'],o['period_start'],o['period_end'])==(r['item_id'],r['statement'],r['start'],r['end']))
  row={'item_id':r['item_id'],'original_name':r['original_name'],'raw_value':r['raw_value'],'number_format':'space_grouped_decimal','unit':'млн руб.','unit_evidence':o['unit_evidence'],'evidence':r['evidence'],'column_header':o['column_header'],'period_start':r['start'],'period_end':r['end'],'period_type':o['period_type'],'statement':r['statement'],'cash_flow_section':r['cash_flow_section']}
  facts.extend(parse_extraction(json.dumps({**context,'facts':[row],'warnings':[]},ensure_ascii=False),pdf.pages,ticker='MVID',period=period,entity_token='М.ВИДЕО',line_items=config.line_items()).facts)
 return ref,pdf,period,review,tuple(facts)


@pytest.mark.parametrize('year,expected,ocr',[(2023,103,False),(2025,115,True)])
def test_full_money_rows_and_independent_identity_results_are_source_bound(year,expected,ocr):
 ref,pdf,period,review,facts=inputs(year);validate_reviewed_columns(facts,review,pdf.pages);checks,statuses=validate_financials(facts,reviewed_document=review)
 assert len(facts)==expected and all(f.from_ocr==ocr for f in facts)
 assert list(statuses)==[r['status'] for r in ref['rows']]
 for target in ref['checks_expected']:
  found=[c for c in checks if (c.rule,c.start,c.end)==(target['rule'],target['start'],target['end']) and (c.basis or {}).get('source_structure_reviewed')]
  assert found and all(c.result.status==target['status'] and ('delta'not in target or str(c.result.delta)==target['delta']) for c in found),target
 assert not any(f.item_id=='cf_fx_effect' for f in facts)
 if ocr:
  assert Counter(statuses)=={'не_проверено':115}
  audit=pdf.processing['visual_ocr_punctuation'];assert sum(len(p['visual_transcriptions']) for p in audit['pages'])==3
  assert not audit['source_pdf_modified'] and not audit['financial_values_approved']
 else:assert Counter(statuses)=={'проверено_авто':84,'не_проверено':19}


class Provider:
 def __init__(self,ref):self.ref=ref
 def extract(self,system,text,schema):
  req=json.loads(text.split('\nПовторная проверка')[0]);rows={f"{r['item_id']}__{r['end'].replace('-','_')}":r for r in self.ref['rows'] if r['statement']==req['source_statement']};obs={k:None if k not in rows else {n:rows[k][n] for n in ('original_name','raw_value','evidence','cash_flow_section')} for k in schema['properties']['observations']['properties']}
  return Completion(json.dumps({'number_format':'space_grouped_decimal','unit':'млн руб.','unit_evidence':schema['properties']['unit_evidence']['enum'][0],'observations':obs},ensure_ascii=False),'independent-test-fixture',0)


@pytest.mark.parametrize('year,positions,missing',[(2023,108,5),(2025,124,9)])
def test_every_original_position_requested_without_dash_or_fx_zero_creation(tmp_path,year,positions,missing):
 ref,pdf,period,review,facts=inputs(year);items=config.line_items();selected={o['item_id']:items[o['item_id']] for o in review.observations.values()}
 run=extract_pdf(pdf,Provider(ref),ticker='MVID',entity_token='М.ВИДЕО',period=period,line_items=selected,sections=('income','cash_flow'),output=tmp_path/'out',compact=True,reviewed_catalog=json.loads(__import__('agent.checks.reviewed_tables',fromlist=['CATALOG']).CATALOG.read_text()))
 assert len(run.facts)==len(ref['rows']) and run.requested_observations_count==positions and len(run.missing_observations)==missing
