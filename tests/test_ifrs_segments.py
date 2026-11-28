import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
import pytest
from agent import db
from agent.extract.llm import Completion
from agent.extract.pdf_text import PDFText,Page
from agent.extract.segments import extract_segments,store_segments
from agent.extract.restatements import original_status
from tests.test_extraction_schema import PERIOD

FIXTURE=Path(__file__).parent/'fixtures/ifrs/segments_h1_2026_reference.json'


class SegmentReferenceProvider:
 def __init__(self,ticker,reference):self.ticker=ticker;self.rows=[r for r in reference['references'] if r['ticker']==ticker];self.calls=[]
 def extract(self,system,text,schema):
  req=json.loads(text.split('\nПовторная проверка')[0]);self.calls.append(req);observations={}
  for p in req['requested_observations']:
   key=p['item_id']+'__'+p['period_end'].replace('-','_')
   matches=[r for r in self.rows if (r['item_id'],r['end'],r['owner_column'])==(p['item_id'],p['period_end'],p['owner_column']) and r['evidence'] in p['source_row_hints']]
   assert len(matches)<=1,(p,matches)
   r=matches[0] if matches else None
   observations[key]=None if not r else {'original_name':r['original_name'],'raw_value':r['raw_value'],'evidence':r['evidence'],'cash_flow_section':None}
  return Completion(json.dumps({'number_format':'comma_grouped_dot_decimal' if self.ticker=='MTSS' else 'space_grouped_decimal','unit':'млн руб.','unit_evidence':schema['properties']['unit_evidence']['enum'][0],'observations':observations},ensure_ascii=False),'independent-segment-reference',0)


def source(ticker):
 reference=json.loads(FIXTURE.read_text());catalog=json.loads((Path(__file__).parents[1]/'config/ifrs_segments.json').read_text());doc=next(d for d in catalog['documents'] if d['ticker']==ticker);p=reference['pdfs'][doc['pdf_sha256']]
 pdf=PDFText(p['sha256'],tuple(Page(**p) for p in p['pages']),tuple(p['warnings']),p['processing'])
 return pdf,reference,catalog


@pytest.mark.parametrize('ticker,count,auto,missing',[('SGZH',81,79,7),('MTSS',50,50,2)])
def test_segment_tables_preserve_scopes_hierarchy_restatement_and_exact_values(tmp_path,ticker,count,auto,missing):
 pdf,reference,catalog=source(ticker);provider=SegmentReferenceProvider(ticker,reference)
 report,finished=extract_segments(pdf,provider,ticker=ticker,entity_token='СЕГЕЖА ГРУПП' if ticker=='SGZH' else 'МОБИЛЬНЫЕ ТЕЛЕСИСТЕМЫ',period=PERIOD,output=tmp_path/'responses',catalog=catalog)
 expected=[r for r in reference['references'] if r['ticker']==ticker]
 actual={(s['key'],f.item_id,str(f.end)):(str(f.value),str(f.normalized),status) for s,run in finished for f,status in zip(run.facts,run.statuses)}
 assert len(actual)==count
 assert actual=={(r['segment_key'],r['item_id'],r['end']):(r['value'],r['normalized_value'],r['status']) for r in expected}
 assert sum(len(r.missing_observations) for _,r in finished)==missing
 assert sum(v[2]=='проверено_авто' for v in actual.values())==auto
 assert all(c['result']['status'] in ('пройдено','недостаточно_данных') for c in report['checks'])
 assert not report['comparability_approved'] and 'пересчитаны' in report['comparative_basis']
 conn=db.init_db(tmp_path/'check.db')
 with conn:stored=store_segments(conn,report,finished,ticker=ticker,name=ticker,source_url='https://example.test/source.pdf',period=PERIOD,local_path='source.pdf')
 assert sum(s['inserted'] for s in stored)==count and all(not s['conflicts'] for s in stored)
 from agent.tools.check_ifrs_segments import verify_segments
 assert verify_segments(conn,ticker,reference,pdf.sha256)['ok']
 for row in conn.execute('SELECT * FROM source_values'):
  original_status(conn,row)
  assert (row['scope']=='group')==(row['segment_name'] is None)
 # Group revenue is stored as the group total, never the sum of a subtotal and its children.
 revenue=conn.execute("SELECT value_norm FROM source_values WHERE item_id='revenue' AND scope='group' AND period_end='2026-06-30'").fetchall()
 assert len(revenue)==1 and revenue[0][0]==('37984000000' if ticker=='SGZH' else '414729000000')
 conn.close()


def test_adding_children_to_group_sum_detects_double_counting(tmp_path):
 pdf,reference,catalog=source('SGZH');catalog=deepcopy(catalog);doc=next(d for d in catalog['documents'] if d['ticker']=='SGZH')
 identity=next(c for c in doc['identities'] if c['rule']=='group_segments_sum_revenue' and c['year']==2026)
 identity['terms']['double_count']={'segment':'boards','item_id':'revenue','coefficient':1}
 report,_=extract_segments(pdf,SegmentReferenceProvider('SGZH',reference),ticker='SGZH',entity_token='СЕГЕЖА ГРУПП',period=PERIOD,output=tmp_path/'bad',catalog=catalog)
 check=next(c for c in report['checks'] if c['rule']=='group_segments_sum_revenue' and c['end']=='2026-06-30')
 assert check['result']['status']=='расхождение' and check['result']['delta']==3952000000


def test_single_segment_narrative_never_creates_zero_or_group_as_segment_facts(tmp_path):
 saved=json.loads((Path(__file__).parent/'fixtures/ifrs/mvid_h1_2026_policy_pages.json').read_text());pdf=PDFText(saved['pdf_sha256'],tuple(Page(**p) for p in saved['pages']))
 class NoProvider:
  def extract(self,*a):raise AssertionError('Качественное раскрытие не вызывает денежное извлечение')
 report,finished=extract_segments(pdf,NoProvider(),ticker='MVID',entity_token='М.ВИДЕО',period=PERIOD,output=tmp_path/'absence')
 assert not finished and report['quantitative_table_not_disclosed'] and report['single_operating_segment_reported']
 assert report['provider_requests']==0 and report['selected_values_count']==0
 assert not report['financial_values_verified'] and not report['absence_means_zero'] and report['disclosure_reviewed']
 assert 'единственного операционного сегмента' in report['source_evidence'][0]['quote']


@pytest.mark.parametrize('change',['text','period','unreviewed_empty'])
def test_missing_segment_values_require_exact_reviewed_disclosure(tmp_path,change):
 from dataclasses import replace
 saved=json.loads((Path(__file__).parent/'fixtures/ifrs/mvid_h1_2026_policy_pages.json').read_text());pdf=PDFText(saved['pdf_sha256'],tuple(Page(**p) for p in saved['pages']));catalog=json.loads((Path(__file__).parents[1]/'config/ifrs_segments.json').read_text());period=PERIOD
 if change=='text':pdf=replace(pdf,pages=tuple(replace(p,text=p.text+'X') if p.number==14 else p for p in pdf.pages))
 if change=='period':period=PERIOD.shift_years(-1)
 if change=='unreviewed_empty':next(d for d in catalog['documents'] if d['ticker']=='MVID')['quantitative_table_not_disclosed']=False
 with pytest.raises(ValueError):extract_segments(pdf,None,ticker='MVID',entity_token='М.ВИДЕО',period=period,output=tmp_path/'bad',catalog=catalog)
