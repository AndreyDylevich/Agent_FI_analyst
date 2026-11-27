import json
from dataclasses import replace
from decimal import Decimal
import pytest
from agent import db
from agent.extract.store import store_extraction
from agent.extract.restatements import original_status
from tests.test_extraction_schema import PERIOD
from tests.test_ifrs_pipeline import make_run,facts


def test_segment_scope_does_not_conflict_with_group_or_other_segment(tmp_path):
 conn=db.init_db(tmp_path/'segments.db');base=make_run(facts()[:1]);common=dict(ticker='TEST',name='Test',source_url='https://example.test/a.pdf',period=PERIOD,local_path='a.pdf')
 with conn:
  store_extraction(conn,base,**common)
  for name,value in [('Телеком','60'),('Финтех','40')]:
   f=replace(base.facts[0],value=Decimal(value),normalized=Decimal(value)*1000000,raw_value=value)
   store_extraction(conn,replace(base,facts=(f,)),scope='segment',segment_name=name,**common)
 rows=conn.execute('SELECT * FROM source_values ORDER BY id').fetchall()
 assert len(rows)==3 and [(r['scope'],r['segment_name']) for r in rows]==[('group',None),('segment','Телеком'),('segment','Финтех')]
 assert all(r['check_status']!='расхождение' for r in rows)
 for row in rows:original_status(conn,row)
 # Only the same segment and period conflicts, and the group is preserved.
 f=replace(base.facts[0],value=Decimal('61'),normalized=Decimal('61000000'),raw_value='61')
 with conn:result=store_extraction(conn,replace(base,facts=(f,)),scope='segment',segment_name='Телеком',**common)
 assert result['conflicts']
 assert conn.execute("SELECT check_status FROM source_values WHERE scope='group'").fetchone()[0]!='расхождение'
 assert conn.execute("SELECT check_status FROM source_values WHERE segment_name='Финтех'").fetchone()[0]!='расхождение'
 conn.close()


@pytest.mark.parametrize('scope,name',[('entity',None),('segment',None),('segment',''),('group','Телеком')])
def test_segment_scope_is_explicit_and_consistent(tmp_path,scope,name):
 conn=db.init_db(tmp_path/'bad.db')
 with pytest.raises(ValueError):
  with conn:store_extraction(conn,make_run(),ticker='TEST',name='Test',source_url='https://example.test/a.pdf',period=PERIOD,local_path='a.pdf',scope=scope,segment_name=name)
 assert conn.execute('SELECT count(*) FROM source_values').fetchone()[0]==0
 conn.close()
