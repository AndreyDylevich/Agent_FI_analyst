import json
from collections import Counter
from copy import deepcopy
import pytest
from agent import config,db
from agent.extract.ocr_review import reference_sha256
from agent.extract.ocr_source_review import apply_ocr_source_review
from agent.extract.pipeline import extract_pdf
from agent.extract.store import store_extraction
from tests.test_extraction_schema import PERIOD
from tests.test_mvid_reviewed_forms import inputs
from tests.test_sgzh_reviewed_forms import ReferenceProvider


def setup(tmp_path):
 pdf,ref,structure,_=inputs();items=config.line_items()
 selected={o['item_id']:items[o['item_id']] for o in structure.observations.values()}
 run=extract_pdf(pdf,ReferenceProvider(ref),ticker='MVID',entity_token='М.ВИДЕО',period=PERIOD,line_items=selected,
  sections=('balance','income','cash_flow','equity','debt','leases','interest'),output=tmp_path/'responses',compact=True)
 conn=db.init_db(tmp_path/'source.db')
 with conn:store_extraction(conn,run,ticker='MVID',name='М.Видео',source_url=ref['source_url'],period=PERIOD,local_path='scan.pdf')
 review={'version':2,'ticker':'MVID','reviewer':'Тест независимой приёмки','reason':'Просмотрены все выбранные строки и метаданные',
  'pdf_sha256':pdf.sha256,'reference_sha256':reference_sha256(ref),'reviewed_pages':list(range(1,39)),
  'visual_review_confirmed':True,'standard':'IFRS','scope':'group','selected_observations':[
   {k:r[k] for k in ('item_id','statement','start','end')} for r in ref['rows'] if r['status']=='не_проверено']}
 return conn,pdf,ref,review


def snapshot(conn):
 return [tuple(r) for r in conn.execute('SELECT id,item_id,value,value_norm,unit,location,superseded_by,is_restated FROM source_values ORDER BY id')]


def test_selected_273_rows_gain_manual_status_without_changing_numbers_or_eight_disputes(tmp_path):
 conn,pdf,ref,review=setup(tmp_path);before=snapshot(conn)
 with conn:result=apply_ocr_source_review(conn,pdf,ref,review)
 assert len(result['changes'])==273 and snapshot(conn)==before
 assert Counter(r[0] for r in conn.execute('SELECT check_status FROM source_values'))=={'проверено_вручную':273,'расхождение':8}
 assert not result['source_numbers_modified'] and not result['comparability_approved'] and not result['financial_audit_complete']
 with conn:assert apply_ocr_source_review(conn,pdf,ref,review)==result
 conn.close()


@pytest.mark.parametrize('problem',['disputed','page','hash','value','status','scope','duplicate'])
def test_review_rejects_wrong_or_disputed_values_atomically(tmp_path,problem):
 conn,pdf,ref,review=setup(tmp_path);before=snapshot(conn)
 if problem=='disputed':review['selected_observations'].append({k:next(r for r in ref['rows'] if r['status']=='расхождение')[k] for k in ('item_id','statement','start','end')})
 if problem=='page':review['reviewed_pages'].remove(6)
 if problem=='hash':review['reference_sha256']='f'*64
 if problem=='value':ref['rows'][0]['value']='999';review['reference_sha256']=reference_sha256(ref)
 if problem=='status':conn.execute("UPDATE source_values SET check_status='расхождение' WHERE id=(SELECT MIN(id) FROM source_values)");conn.commit()
 if problem=='scope':review['scope']='entity'
 if problem=='duplicate':review['selected_observations'].append(deepcopy(review['selected_observations'][0]))
 states=[tuple(r) for r in conn.execute('SELECT id,check_status FROM source_values')]
 with pytest.raises(ValueError):
  with conn:apply_ocr_source_review(conn,pdf,ref,review)
 assert snapshot(conn)==before and [tuple(r) for r in conn.execute('SELECT id,check_status FROM source_values')]==states
 assert not conn.execute("SELECT 1 FROM meta WHERE key LIKE 'ifrs_ocr_review:%'").fetchall()
 conn.close()
