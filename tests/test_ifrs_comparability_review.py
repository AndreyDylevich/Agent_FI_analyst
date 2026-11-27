import json
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import pytest
from agent import config,db
from agent.extract.comparability_review import apply_comparability_review
from agent.extract.pdf_text import PDFText,Page
from agent.extract.store import store_extraction
from agent.metrics.data import load_facts
from agent.metrics.engine import Engine
from tests.test_extraction_schema import PERIOD
from tests.test_ifrs_pipeline import make_run,facts


def setup(tmp_path):
 conn=db.init_db(tmp_path/'review.db')
 base=facts()[0];rows=tuple(replace(base,item_id='revenue',value=Decimal(value),normalized=Decimal(value)*1000000,
  raw_value=value,start=date(year,1,1),end=date(year,6,30),period_type='H1') for year,value in ((2026,'120'),(2025,'100')))
 run=replace(make_run(),facts=rows,statuses=('проверено_авто','проверено_авто'),checks=())
 with conn:store_extraction(conn,run,ticker='TEST',name='Test',source_url='https://example.test/a.pdf',period=PERIOD,local_path='a.pdf')
 pdf=PDFText('a'*64,(Page(1,'Учётная политика одинакова\nПериметр сравнений подтверждён\nВыручка от внешних клиентов'),))
 review={'version':1,'ticker':'TEST','reviewer':'Независимый просмотр','reason':'Политика, периметр и определение выручки подтверждены для выбранных строк',
  'decision':'approved','scope':'group','segment_name':None,'item_id':'revenue','accounting_basis':'policy-v1','perimeter_basis':'business-v1',
  'methodology_version':'external-revenue-v1','source_context_reviewed':True,
  'observations':[{'source_id':r['id'],'pdf_sha256':'a'*64,'normalized_value':r['value_norm']} for r in conn.execute('SELECT * FROM source_values')],
  'evidence':[{'pdf_sha256':'a'*64,'topic':topic,'evidence':{'page':1,'start_line':line,'end_line':line}}
   for line,topic in enumerate(('accounting_policy','perimeter','definition'),1)]}
 return conn,pdf,review


def calculate(conn):
 facts=load_facts(conn,company_id=1,standard='IFRS',scope='group',segment_name=None,line_items=config.line_items())
 return Engine(facts,company_id=1,standard='IFRS',scope='group',industry_id='telecom',industries=config.industries(),require_comparability=True).calculate('revenue_yoy',PERIOD)


def test_addressed_review_survives_db_reload_and_unblocks_only_selected_comparison(tmp_path):
 conn,pdf,review=setup(tmp_path);before=[tuple(r) for r in conn.execute('SELECT * FROM source_values')]
 assert calculate(conn).result.value is None
 with conn:first=apply_comparability_review(conn,{pdf.sha256:pdf},review)
 assert calculate(conn).result.value==20 and not calculate(conn).blocked
 assert [tuple(r) for r in conn.execute('SELECT * FROM source_values')]==before
 with conn:assert apply_comparability_review(conn,{pdf.sha256:pdf},review)==first
 conn.close()
 conn=db.connect(tmp_path/'review.db') if hasattr(db,'connect') else __import__('sqlite3').connect(tmp_path/'review.db')
 conn.row_factory=__import__('sqlite3').Row
 assert calculate(conn).result.value==20
 conn.close()


def test_negative_and_conflicting_reviews_keep_comparison_blocked(tmp_path):
 conn,pdf,review=setup(tmp_path)
 bad=deepcopy(review);bad['decision']='not_comparable';bad['reason']='Изменился периметр; мост не раскрыт'
 with conn:apply_comparability_review(conn,{pdf.sha256:pdf},bad)
 result=calculate(conn);assert result.blocked and result.result.value is None and 'Изменился периметр' in result.result.reason
 with conn:apply_comparability_review(conn,{pdf.sha256:pdf},review)
 assert calculate(conn).blocked and 'Противоречивые решения' in calculate(conn).result.reason
 conn.close()


@pytest.mark.parametrize('problem',['status','value','superseded'])
def test_changed_peer_invalidates_the_whole_reviewed_set(tmp_path,problem):
 conn,pdf,review=setup(tmp_path)
 with conn:apply_comparability_review(conn,{pdf.sha256:pdf},review)
 assert calculate(conn).result.value==20
 with conn:
  if problem=='status':conn.execute("UPDATE source_values SET check_status='не_проверено' WHERE id=2")
  if problem=='value':conn.execute("UPDATE source_values SET value='101',value_norm='101000000' WHERE id=2")
  if problem=='superseded':conn.execute('UPDATE source_values SET superseded_by=1 WHERE id=2')
 assert calculate(conn).result.value is None
 if problem!='superseded':assert calculate(conn).blocked
 remaining=load_facts(conn,company_id=1,standard='IFRS',scope='group',segment_name=None,line_items=config.line_items())
 assert all('устарело' in f.comparison_issue for f in remaining)
 conn.close()


@pytest.mark.parametrize('problem',['topic','quote','hash','value','scope','unverified','same_date'])
def test_wrong_or_incomplete_evidence_cannot_approve_comparison(tmp_path,problem):
 conn,pdf,review=setup(tmp_path)
 if problem=='topic':review['evidence']=review['evidence'][:2]
 if problem=='quote':review['evidence'][0]['evidence']['end_line']=99
 if problem=='hash':review['observations'][0]['pdf_sha256']='b'*64
 if problem=='value':review['observations'][0]['normalized_value']='999'
 if problem=='scope':review['scope']='segment';review['segment_name']='Телеком'
 if problem=='unverified':conn.execute("UPDATE source_values SET check_status='не_проверено' WHERE id=1");conn.commit()
 if problem=='same_date':review['observations']=[review['observations'][0],review['observations'][0]]
 with pytest.raises(ValueError):
  with conn:apply_comparability_review(conn,{pdf.sha256:pdf},review)
 assert not conn.execute("SELECT 1 FROM meta WHERE key LIKE 'ifrs_comparability_review:%'").fetchall()
 assert calculate(conn).blocked
 conn.close()


def test_changed_top_level_decision_cannot_bypass_signed_payload(tmp_path):
 conn,pdf,review=setup(tmp_path)
 with conn:apply_comparability_review(conn,{pdf.sha256:pdf},review)
 row=conn.execute("SELECT key,value FROM meta WHERE key LIKE 'ifrs_comparability_review:%'").fetchone()
 record=json.loads(row['value']);record['decision']='not_comparable'
 with conn:conn.execute('UPDATE meta SET value=? WHERE key=?',(json.dumps(record),row['key']))
 with pytest.raises(ValueError,match='Контрольная сумма'):calculate(conn)
 conn.close()
