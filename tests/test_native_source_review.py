"""Native financial accuracy can be reviewed explicitly without filling missing values or approving comparisons."""
import json
from copy import deepcopy
from collections import Counter

import pytest

from agent import config,db
from agent.extract.ocr_review import reference_sha256
from agent.extract.ocr_source_review import apply_native_source_review
from agent.extract.pipeline import ExtractionRun
from agent.extract.schemas import source_context,parse_extraction
from agent.extract.store import store_extraction
from agent.checks.ifrs import validate_financials
from agent.checks.document import reconcile_document
from agent.tools.review_ifrs_ocr import recalculate_reviewed
from tests.test_mts_primary_forms import inputs
from tests.test_extraction_schema import PERIOD


def setup(tmp_path):
 pdf,ref,structure,facts=inputs();ctx=source_context(pdf.pages,ticker='MTSS',period=PERIOD,entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',line_items=config.line_items());ctx=parse_extraction(json.dumps({**ctx,'facts':[],'warnings':[]}),pdf.pages,ticker='MTSS',period=PERIOD,entity_token='МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',line_items=config.line_items()).context;checks,statuses=validate_financials(facts,reviewed_document=structure);run=ExtractionRun(pdf.sha256,(),(ctx,),facts,tuple(checks),tuple(statuses),());conn=db.init_db(tmp_path/'source.db')
 with conn:store_extraction(conn,run,ticker='MTSS',name='Тест',source_url=ref['source_url'],period=PERIOD,local_path='synthetic-test.pdf')
 review={'version':3,'ticker':'MTSS','reviewer':'Независимый тест','reason':'Точность выбранных исходных native-строк по визуальному эталону; не полнота раскрытия/моста',
  'pdf_sha256':pdf.sha256,'reference_sha256':reference_sha256(ref),'reviewed_pages':[p.number for p in pdf.pages],'source_context_reviewed':True,'standard':'IFRS','scope':'group',
  'selected_observations':[{k:r[k] for k in ('item_id','statement','start','end')} for r in ref['rows'] if r['status']=='не_проверено']}
 return conn,pdf,ref,review


def money(conn):return [tuple(r) for r in conn.execute('SELECT id,item_id,value,value_norm,unit,location,superseded_by,is_restated FROM source_values ORDER BY id')]


def test_selected_native_accuracy_review_preserves_money_dashes_and_incomplete_checks(tmp_path):
 conn,pdf,ref,review=setup(tmp_path);before=money(conn)
 with conn:first=apply_native_source_review(conn,pdf,ref,review)
 assert len(first['changes'])==16 and Counter(r[0] for r in conn.execute('SELECT check_status FROM source_values'))=={'проверено_авто':150,'проверено_вручную':16}
 assert money(conn)==before and not first['comparability_approved']
 with conn:assert apply_native_source_review(conn,pdf,ref,review)==first
 with conn:metrics=recalculate_reviewed(conn,first,['current_ratio'])
 assert metrics[0]['status']=='рассчитан'
 result=reconcile_document([('source',conn)],pdf,'MTSS')
 assert result['source_integrity_verified'] and not result['cross_form_complete']
 assert next(c for c in result['checks'] if c['rule']=='complete_noncurrent_assets' and c['end']=='2025-12-31')['status']=='недостаточно_данных'
 assert money(conn)==before;conn.close()


@pytest.mark.parametrize('change',['source','value','status','address','reference','confirmation','auto_row','recognition'])
def test_native_review_cannot_promote_wrong_source_discrepancy_or_unreviewed_ocr(tmp_path,change):
 from dataclasses import replace
 conn,pdf,ref,review=setup(tmp_path);before='\n'.join(conn.iterdump())
 if change=='source':review['pdf_sha256']='f'*64
 if change=='value':conn.execute("UPDATE source_values SET value='999' WHERE check_status='не_проверено'");conn.commit();before='\n'.join(conn.iterdump())
 if change=='status':conn.execute("UPDATE source_values SET check_status='расхождение' WHERE check_status='не_проверено'");conn.commit();before='\n'.join(conn.iterdump())
 if change=='address':
  ref=deepcopy(ref);row=next(r for r in ref['rows'] if r['status']=='не_проверено');row['evidence']['start_line']-=1;review['reference_sha256']=reference_sha256(ref)
 if change=='reference':review['reference_sha256']='f'*64
 if change=='confirmation':review['source_context_reviewed']=False
 if change=='auto_row':review['selected_observations'].append({k:ref['rows'][0][k] for k in ('item_id','statement','start','end')})
 if change=='recognition':pdf=replace(pdf,pages=tuple(replace(p,method='ocr') for p in pdf.pages))
 with pytest.raises(ValueError):
  with conn:apply_native_source_review(conn,pdf,ref,review)
 assert '\n'.join(conn.iterdump())==before;conn.close()
