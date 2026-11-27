"""Независимые исходные значения всех основных форм Сегежи и раскрытых примечаний."""
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from agent import config
from agent.checks.ifrs import validate_financials
from agent.checks.reviewed_tables import compile_reviewed_tables
from agent.extract.llm import Completion
from agent.extract.pdf_text import PDFText,Page
from agent.extract.pipeline import extract_pdf
from agent.extract.schemas import parse_extraction,source_context
from tests.test_extraction_schema import PERIOD

FIXTURES=Path(__file__).parent/'fixtures/ifrs'


def inputs():
 ref=json.loads((FIXTURES/'sgzh_h1_2026_full_reference.json').read_text());saved=json.loads((FIXTURES/'sgzh_h1_2026_full_pages.json').read_text())
 pdf=PDFText(saved['pdf_sha256'],tuple(Page(**p) for p in saved['pages']));review=compile_reviewed_tables(pdf,PERIOD)
 context=source_context(pdf.pages,ticker='SGZH',period=PERIOD,entity_token='СЕГЕЖА ГРУПП',line_items=config.line_items())
 assert context
 facts=[]
 for form in ('balance','income','cash_flow','equity','notes'):
  rows=[{'item_id':r['item_id'],'original_name':r['original_name'],'raw_value':r['raw_value'],'number_format':'space_grouped_decimal','unit':'млн руб.',
    'unit_evidence':{'page':r['pages'][0],'start_line':5 if form=='equity' else 6,'end_line':5 if form=='equity' else 6},
    'evidence':r['evidence'],'column_header':next(o['column_header'] for o in review.observations.values() if (o['item_id'],o['statement'],o['period_start'],o['period_end'])==(r['item_id'],r['statement'],r['start'],r['end'])),
    'period_start':r['start'],'period_end':r['end'],'period_type':'H1' if r['start'] else 'AT_DATE','statement':form,'cash_flow_section':r['cash_flow_section']}
    for r in ref['rows'] if r['statement']==form]
  parsed=parse_extraction(json.dumps({**context,'facts':rows,'warnings':[]},ensure_ascii=False),pdf.pages,ticker='SGZH',period=PERIOD,entity_token='СЕГЕЖА ГРУПП',line_items=config.line_items(),unlabelled_sources=tuple(o for o in review.observations.values() if o.get('original_name_hints')==['']))
  facts.extend(parsed.facts)
 return pdf,ref,SimpleNamespace(facts=tuple(facts))


def test_independent_visual_reference_preserves_full_forms_and_known_source_gaps():
 pdf,ref,extraction=inputs();checks,statuses=validate_financials(extraction.facts,reviewed_document=compile_reviewed_tables(pdf,PERIOD))
 assert len(extraction.facts)==284
 for expected in ref['checks_expected']:
  matching=[c for c in checks if (c.rule,c.start,c.end)==(expected['rule'],expected['start'],expected['end'])]
  assert matching,expected
  assert all(c.result.status==expected['status'] for c in matching),(expected,[(c.result.status,c.result.delta,c.result.missing) for c in matching])
  if 'delta' in expected:assert all(c.result.delta==0 for c in matching),expected
 expected={(r['item_id'],r['statement'],r['start'],r['end']):r['status'] for r in ref['rows']}
 for f,status in zip(extraction.facts,statuses):assert status==expected[(f.item_id,f.statement,str(f.start) if f.start else None,str(f.end))],(f.item_id,f.end,status,expected[(f.item_id,f.statement,str(f.start) if f.start else None,str(f.end))])
 assert next(c for c in checks if c.rule=='complete_lease_rollforward' and c.end=='2025-06-30').result.status=='пройдено'
 assert next(c for c in checks if c.rule=='complete_lease_rollforward' and c.end=='2026-06-30').result.status=='недостаточно_данных'
 assert all(f.item_id!='lease_principal_paid' for f in extraction.facts)


class ReferenceProvider:
 def __init__(self,ref):self.ref=ref;self.calls=[]
 def extract(self,system,text,schema):
  req=json.loads(text.split('\nПовторная проверка')[0]);self.calls.append(req)
  rows={f"{r['item_id']}__{r['end'].replace('-','_')}":r for r in self.ref['rows'] if r['statement']==req['source_statement']}
  obs={key:None if key not in rows else {k:rows[key][k] for k in ('original_name','raw_value','evidence','cash_flow_section')} for key in schema['properties']['observations']['properties']}
  unit=schema['properties']['unit_evidence']['enum'][0]
  return Completion(json.dumps({'number_format':'space_grouped_decimal','unit':'млн руб.','unit_evidence':unit,'observations':obs},ensure_ascii=False),'independent-reference-fixture',0)


def test_pipeline_requests_all_300_source_positions_without_filling_16_absences(tmp_path):
 pdf,ref,_=inputs();provider=ReferenceProvider(ref);items=config.line_items();selected={k:items[k] for k in sorted({r['item_id'] for r in ref['rows']})}
 run=extract_pdf(pdf,provider,ticker='SGZH',entity_token='СЕГЕЖА ГРУПП',period=PERIOD,line_items=selected,
  sections=('balance','income','cash_flow','equity','debt','leases','interest'),output=tmp_path/'out',compact=True)
 assert len(run.facts)==284 and run.requested_observations_count==300 and len(run.missing_observations)==16
 assert len(run.reviewed_disclosure_gaps)==8
 assert {(f.item_id,f.statement,str(f.end)):str(f.value) for f in run.facts}=={(r['item_id'],r['statement'],r['end']):r['value'] for r in ref['rows']}
 assert all(len(call['requested_observations'])<=20 for call in provider.calls)


def test_reviewed_numeric_source_cannot_silently_become_null(tmp_path):
 class DropsOnce(ReferenceProvider):
  def extract(self,system,text,schema):
   result=super().extract(system,text,schema)
   if len(self.calls)==1:
    data=json.loads(result.content)
    key=next(k for k,v in data['observations'].items() if v is not None)
    data['observations'][key]=None
    return Completion(json.dumps(data,ensure_ascii=False),result.model,result.tokens)
   return result
 pdf,ref,_=inputs();provider=DropsOnce(ref);items=config.line_items();selected={k:items[k] for k in sorted({r['item_id'] for r in ref['rows']})}
 run=extract_pdf(pdf,provider,ticker='SGZH',entity_token='СЕГЕЖА ГРУПП',period=PERIOD,line_items=selected,
  sections=('balance','income','cash_flow','equity','debt','leases','interest'),output=tmp_path/'out',compact=True)
 assert len(run.facts)==284 and len(run.missing_observations)==16
 assert run.batches[0]['accepted'] is False and 'null' in run.batches[0]['error'] and run.batches[1]['accepted']
