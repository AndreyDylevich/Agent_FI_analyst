"""Real original financing rows keep scope, unit, year column and separate source acceptance."""
import json
from copy import deepcopy
from datetime import date
from pathlib import Path
import pytest
from agent import config,db
from agent.checks.reviewed_tables import compile_reviewed_tables,validate_reviewed_columns
from agent.extract.pdf_text import PDFText,Page
from agent.extract.pipeline import extract_pdf
from agent.extract.llm import Completion
from agent.extract.store import store_extraction
from agent.metrics.engine import Period
from agent.tools.extract_ifrs_history import FINANCING_CATALOG
from agent.tools.verify_ifrs_financing_history import verify_year

FIXTURES=Path(__file__).parent/'fixtures/ifrs'
CASES=[('MTSS',2021),('MTSS',2023),('MTSS',2025),('SGZH',2021),('SGZH',2023),('SGZH',2025),('MVID',2023),('MVID',2025)]


class ReferenceProvider:
    def __init__(self,reference):self.reference=reference
    def extract(self,system,text,schema):
        by_key={f"{r['item_id']}__{r['end'].replace('-','_')}":r for r in self.reference['rows']}
        observations={key:{name:by_key[key][name] for name in ('original_name','raw_value','evidence','cash_flow_section')}
                      for key in schema['properties']['observations']['properties']}
        return Completion(json.dumps({'number_format':'comma_grouped_dot_decimal' if self.reference['ticker']=='MTSS' else 'space_grouped_decimal',
            'unit':self.reference['unit'],'unit_evidence':schema['properties']['unit_evidence']['enum'][0],
            'observations':observations},ensure_ascii=False),'independent-reference-fixture',0)


def inputs(ticker,year):
    prefix='mts' if ticker=='MTSS' else ticker.lower()
    ref=json.loads((FIXTURES/f'{prefix}_fy{year}_financing_reference.json').read_text())
    saved=json.loads((FIXTURES/f'{prefix}_fy{year}_financing_pages.json').read_text())
    pdf=PDFText(saved['pdf_sha256'],tuple(Page(**p) for p in saved['pages']))
    period=Period(date(year,1,1),date(year,12,31),'FY');catalog=json.loads(FINANCING_CATALOG.read_text())
    return ref,pdf,period,catalog


@pytest.mark.parametrize('ticker,year',CASES)
def test_source_balance_financing_roundtrip_never_approves_missing_identities(tmp_path,ticker,year):
    ref,pdf,period,catalog=inputs(ticker,year);review=compile_reviewed_tables(pdf,period,catalog)
    selected={r['item_id']:config.line_items()[r['item_id']] for r in ref['rows']}
    company=next(c for c in config.load('watchlist')['companies'] if c['ticker']==ticker)
    run=extract_pdf(pdf,ReferenceProvider(ref),ticker=ticker,entity_token=company['document_identity_token'],
        period=period,line_items=selected,sections=('balance',),output=tmp_path/'responses',compact=True,reviewed_catalog=catalog)
    assert len(run.facts)==(20 if ticker=='MTSS' else 12)
    assert not run.missing_observations and set(run.statuses)=={'не_проверено'}
    assert run.text_processing['reviewed_source_structure']==review.basis
    validate_reviewed_columns(run.facts,review,pdf.pages)
    assert not any(c.result.status=='пройдено' for c in run.checks)
    conn=db.init_db(tmp_path/'case.db')
    with conn:store_extraction(conn,run,ticker=ticker,name=ticker,source_url=ref['source_url'],period=period,local_path='original-test-reference.pdf')
    entry={'ticker':ticker,'expected_year':year,'pdf_sha256':pdf.sha256,'source_url':ref['source_url']}
    checked=verify_year(conn,entry,ref)
    assert checked['ok'] and not checked['source_accuracy_accepted'] and not checked['comparability_approved']
    assert ref['unit']==('тыс. руб.' if (ticker,year)==('SGZH',2021) else 'млн руб.')
    if ticker=='MTSS':assert len([f for f in run.facts if f.item_id.startswith('balance_bank_')])==8
    if (ticker,year)==('MVID',2023):assert {f.evidence.page for f in run.facts}=={9}
    if (ticker,year)==('MVID',2025):
        rou=next(f for f in run.facts if f.item_id=='right_of_use_assets' and f.end==period.end)
        assert str(rou.value)=='42179' and rou.from_ocr
    bad=deepcopy(ref);bad['rows'][0]['raw_value']=bad['rows'][1]['raw_value']
    with pytest.raises(ValueError,match='колонка'):
        verify_year(conn,entry,bad)
    conn.close()
