"""Independent visual MVID reference retains OCR restrictions and the printed OCI mismatch."""
import json
from collections import Counter
from pathlib import Path
from agent import config
from agent.checks.ifrs import validate_financials
from agent.checks.reviewed_tables import compile_reviewed_tables,validate_reviewed_columns
from agent.extract.pdf_text import PDFText,Page
from agent.extract.pipeline import extract_pdf
from agent.extract.schemas import parse_extraction,source_context
from tests.test_extraction_schema import PERIOD
from tests.test_sgzh_reviewed_forms import ReferenceProvider

FIXTURES=Path(__file__).parent/'fixtures/ifrs'

def inputs():
 ref=json.loads((FIXTURES/'mvid_h1_2026_full_reference.json').read_text());saved=json.loads((FIXTURES/'mvid_h1_2026_full_pages.json').read_text())
 pdf=PDFText(saved['pdf_sha256'],tuple(Page(**p) for p in saved['pages']),processing=saved['processing'])
 review=compile_reviewed_tables(pdf,PERIOD);items=config.line_items();context=source_context(pdf.pages,ticker='MVID',period=PERIOD,entity_token='М.ВИДЕО',line_items=items);assert context
 facts=[]
 for r in ref['rows']:
  o=next(o for o in review.observations.values() if (o['item_id'],o['statement'],o['period_start'],o['period_end'])==(r['item_id'],r['statement'],r['start'],r['end']))
  row={'item_id':r['item_id'],'statement':r['statement'],'period_start':r['start'],'period_end':r['end'],'period_type':'H1' if r['start'] else 'AT_DATE','original_name':r['original_name'],'raw_value':r['raw_value'],'number_format':'space_grouped_decimal','unit':'млн руб.','unit_evidence':o['unit_evidence'],'evidence':r['evidence'],'column_header':o['column_header'],'cash_flow_section':r['cash_flow_section']}
  parsed=parse_extraction(json.dumps({**context,'facts':[row],'warnings':[]},ensure_ascii=False),pdf.pages,ticker='MVID',period=PERIOD,entity_token='М.ВИДЕО',line_items=items,unlabelled_sources=tuple(review.observations.values()))
  facts.extend(parsed.facts)
 return pdf,ref,review,tuple(facts)


def assert_checks(checks,ref):
 for expected in ref['checks_expected']:
  matching=[c for c in checks if (c.rule,c.start,c.end)==(expected['rule'],expected['start'],expected['end'])]
  assert matching,expected
  assert all(c.result.status==expected['status'] for c in matching),(expected,[(c.result.status,c.result.delta,c.result.missing) for c in matching])
  if 'delta' in expected:assert all(str(c.result.delta)==expected['delta'] for c in matching),expected


def test_visual_reference_keeps_380m_source_discrepancy_and_blocks_ocr_acceptance():
 pdf,ref,review,facts=inputs();validate_reviewed_columns(facts,review,pdf.pages)
 checks,statuses=validate_financials(facts,reviewed_document=review);assert_checks(checks,ref)
 assert len(facts)==281 and all(f.from_ocr for f in facts)
 assert Counter(statuses)=={'не_проверено':273,'расхождение':8}
 assert all(s==r['status'] for s,r in zip(statuses,ref['rows']))
 assert next(c for c in checks if c.rule=='other_comprehensive_income_vs_equity' and c.end=='2025-06-30').result.delta==380000000
 assert any(p['financial_digits_modified'] for p in pdf.processing['visual_ocr_punctuation']['pages'])


def test_all_source_positions_are_requested_without_filling_dashes(tmp_path):
 pdf,ref,review,_=inputs();provider=ReferenceProvider(ref);items=config.line_items();selected={o['item_id']:items[o['item_id']] for o in review.observations.values()}
 run=extract_pdf(pdf,provider,ticker='MVID',entity_token='М.ВИДЕО',period=PERIOD,line_items=selected,
  sections=('balance','income','cash_flow','equity','debt','leases','interest'),output=tmp_path/'out',compact=True)
 assert len(run.facts)==281 and run.requested_observations_count==314 and len(run.missing_observations)==33
 assert_checks(run.checks,ref)
 assert Counter(run.statuses)=={'не_проверено':273,'расхождение':8}
 assert len(run.reviewed_disclosure_gaps)==8
 assert all(f.item_id!='lease_principal_paid' for f in run.facts)
