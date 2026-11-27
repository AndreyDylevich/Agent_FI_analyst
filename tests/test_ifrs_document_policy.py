"""Source-bound policy review never promotes financial values or infers IFRS 18 adoption from year."""
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from agent import db
from agent.extract.document_policy import compile_document_policy,store_document_policy,load_document_policies
from agent.extract.pdf_text import PDFText,Page
from agent.extract.store import store_extraction
from tests.test_ifrs_pipeline import make_run
from tests.test_extraction_schema import PERIOD

FIXTURES=Path(__file__).parent/'fixtures/ifrs'


def inputs(ticker):
 saved=json.loads((FIXTURES/f'{ticker.lower()}_h1_2026_policy_pages.json').read_text())
 return PDFText(saved['pdf_sha256'],tuple(Page(**p) for p in saved['pages']))


@pytest.mark.parametrize('ticker',['MTSS','SGZH','MVID'])
def test_policy_evidence_preserves_interest_classification_and_does_not_approve_comparability(ticker):
 policy=compile_document_policy(inputs(ticker),ticker,PERIOD)
 assert policy['financial_reporting_basis']['kind']=='ias34'
 assert not policy['comparability_approved'] and not policy['source_numbers_modified']
 received=next(r for r in policy['cash_flow_classifications'] if r['item_id']=='cf_interest_received')
 assert received['section']==('operating' if ticker=='MTSS' else 'investing')
 if ticker=='MVID':
  assert policy['ifrs18']['adoption']=='not_early_adopted' and not policy['ifrs18']['transition_bridge_reviewed']
 else:assert policy['ifrs18']['adoption']=='not_disclosed_in_reviewed_notes'
 assert policy['ifrs16']['lease_payment_definition']==('principal_explicit' if ticker=='MTSS' else 'lease_payment_unspecified')


@pytest.mark.parametrize('change',['text','period','company','ocr_review','adoption','transition','missing_evidence','wrong_flow','contains'])
def test_unreviewed_or_different_policy_cannot_support_financial_comparison(change):
 pdf=inputs('MVID');from agent.extract.document_policy import CATALOG
 catalog=json.loads(CATALOG.read_text());profile=next(d for d in catalog['documents'] if d['ticker']=='MVID');period=PERIOD;ticker='MVID'
 if change=='text':pdf=replace(pdf,pages=(replace(pdf.pages[0],text=pdf.pages[0].text+'X'),*pdf.pages[1:]))
 if change=='period':period=PERIOD.shift_years(-1)
 if change=='company':ticker='MTSS'
 if change=='ocr_review':profile['ocr_visual_review_confirmed']=False
 if change=='adoption':profile['ifrs18']['adoption']='auto_from_2027'
 if change=='transition':profile['ifrs18'].update(adoption='adopted',transition_bridge_reviewed=True)
 if change=='missing_evidence':profile['financial_reporting_basis']['evidence_ids']=[]
 if change=='wrong_flow':profile['cash_flow_classifications'][0]['item_id']='revenue'
 if change=='contains':profile['evidence']['basis']['contains']=['Невозможная исходная строка']
 with pytest.raises(ValueError):compile_document_policy(pdf,ticker,period,catalog)


def test_policy_journal_is_idempotent_and_preserves_all_financial_source_rows(tmp_path):
 pdf=inputs('MTSS');conn=db.init_db(tmp_path/'check.db')
 with conn:store_extraction(conn,replace(make_run(),pdf_sha256=pdf.sha256),ticker='MTSS',name='Тест',source_url='https://example.test/policy.pdf',period=PERIOD,local_path='synthetic-test.pdf')
 before=[tuple(r) for r in conn.execute('SELECT * FROM source_values')]
 with conn:first=store_document_policy(conn,pdf,'MTSS',PERIOD)
 with conn:assert store_document_policy(conn,pdf,'MTSS',PERIOD)==first
 assert [tuple(r) for r in conn.execute('SELECT * FROM source_values')]==before
 assert len(load_document_policies(conn,first['policy']['document_id']))==1
 row=conn.execute('SELECT key,value FROM meta WHERE key=?',(first['meta_key'],)).fetchone();data=json.loads(row['value']);data['ifrs18']['adoption']='adopted'
 with conn:conn.execute('UPDATE meta SET value=? WHERE key=?',(json.dumps(data),row['key']))
 with pytest.raises(ValueError,match='Контрольная сумма'):load_document_policies(conn,first['policy']['document_id'])
 conn.close()
