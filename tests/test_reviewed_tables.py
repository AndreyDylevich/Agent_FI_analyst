"""Структура полного источника не подменяет пропуски нулями и не смешивает окна."""
import hashlib,json
from copy import deepcopy
from dataclasses import replace

import pytest

from agent.checks.reviewed_tables import compile_reviewed_tables
from agent.extract.pdf_text import PDFText,Page
from tests.test_extraction_schema import PERIOD


def inputs():
 text='Проверка\nНачало 100\nДвижение 20\nКонец 120'
 pdf=PDFText('a'*64,(Page(2,text),))
 def obs(item,start,end,kind,line):return {'item_id':item,'statement':'cash_flow','period_start':start,'period_end':end,'period_type':kind,'column_header':'2026','cash_flow_section':None,'evidence':{'page':2,'start_line':line,'end_line':line},'source_absent':False}
 observations={'opening':obs('cf_cash_opening',None,'2025-12-31','AT_DATE',2),
  'movement':obs('cf_other_cash_changes','2026-01-01','2026-06-30','H1',3),
  'closing':obs('cf_cash_closing_gross',None,'2026-06-30','AT_DATE',4)}
 rows=[{'kind':'value','evidence':o['evidence'],'inputs':[key],'bridge_inputs':[key]} for key,o in observations.items()]
 profile={'pdf_sha256':pdf.sha256,'report_start':'2026-01-01','report_end':'2026-06-30','report_kind':'H1','reviewed':True,'reviewer':'Независимый тест','reason':'Полный учебный мост','number_format':'space_grouped_decimal','page_text_sha256':{'2':hashlib.sha256(text.encode()).hexdigest()},'observations':observations,'complete_blocks':{'cash':{'evidence':{'page':2,'start_line':2,'end_line':4},'rows':rows}},'identities':[{'rule':'complete_cash_bridge','start':'2026-01-01','end':'2026-06-30','terms':{'opening':1,'movement':1,'closing':-1},'full_block':'cash'}],'unavailable_checks':[]}
 return pdf,{'version':1,'documents':[profile]}


def test_reviewed_source_compiles_only_exact_text_and_window():
 pdf,catalog=inputs();r=compile_reviewed_tables(pdf,PERIOD,catalog)
 assert len(r.observations)==3 and len(r.identities)==1
 assert r.basis['source_structure_reviewed']
 assert compile_reviewed_tables(replace(pdf,sha256='b'*64),PERIOD,catalog) is None
 with pytest.raises(ValueError):compile_reviewed_tables(pdf,PERIOD.shift_years(-1),catalog)


@pytest.mark.parametrize('change',['text','row','heading','missing_term','double_subtotal','window','stock_date','flow_date','no_full_block','duplicate'])
def test_incomplete_or_mixed_review_is_rejected(change):
 pdf,catalog=inputs();p=catalog['documents'][0];identity=p['identities'][0]
 if change=='text':pdf=replace(pdf,pages=(Page(2,pdf.pages[0].text+' другое'),))
 if change=='row':p['complete_blocks']['cash']['rows'].pop(1)
 if change=='heading':p['complete_blocks']['cash']['rows'][1]={'kind':'heading','evidence':{'page':2,'start_line':3,'end_line':3},'label':'Движение 20'}
 if change=='missing_term':identity['terms'].pop('movement')
 if change=='double_subtotal':identity['excluded_subtotals']=['movement']
 if change=='window':identity['end']='2025-06-30'
 if change=='stock_date':p['observations']['opening']['period_end']='2024-12-31';p['observations']['opening']['column_header']='2025'
 if change=='flow_date':p['observations']['movement']['period_start']='2025-01-01';p['observations']['movement']['period_end']='2025-06-30';p['observations']['movement']['column_header']='2025'
 if change=='no_full_block':identity.pop('full_block')
 if change=='duplicate':p['identities'].append(deepcopy(identity))
 with pytest.raises(ValueError):compile_reviewed_tables(pdf,PERIOD,catalog)


def test_empty_original_caption_requires_explicit_numeric_only_source(monkeypatch):
 from agent import config
 from agent.extract.schemas import parse_extraction
 from tests.test_extraction_schema import payload,TEXT
 data=payload();data['facts'][0].update(original_name='',evidence={'page':1,'quote':'1 234 1 000'})
 pages=(Page(1,TEXT+'\n1 234 1 000'),);items=config.line_items()
 kwargs={'ticker':'TEST','period':PERIOD,'entity_token':'Тест','line_items':items}
 with pytest.raises(ValueError):parse_extraction(json.dumps(data),pages,**kwargs)
 items['total_assets']={**items['total_assets'],'allow_unlabelled_source':True}
 with pytest.raises(ValueError):parse_extraction(json.dumps(data),pages,**kwargs)
 kwargs['unlabelled_sources']=(dict(data['facts'][0]),)
 result=parse_extraction(json.dumps(data),pages,**kwargs)
 assert result.facts[0].original_name=='' and result.facts[0].value==1234
 data['facts'][0]['evidence']={'page':1,'quote':'Итого активы 1 234 1 000'}
 kwargs['unlabelled_sources']=(dict(data['facts'][0]),)
 with pytest.raises(ValueError):parse_extraction(json.dumps(data),pages,**kwargs)


def test_compiled_identity_uses_all_three_exact_sources():
 from agent.checks.ifrs import validate_financials
 from agent.extract.schemas import Citation
 from tests.test_ifrs_statements import source
 pdf,catalog=inputs();review=compile_reviewed_tables(pdf,PERIOD,catalog)
 facts=[]
 for key,value in [('opening','100'),('movement','20'),('closing','120')]:
  o=review.observations[key];p=o['evidence']
  fact=source(o['item_id'],value,o['period_start'],o['period_end'],'cash_flow')
  facts.append(replace(fact,evidence=Citation(p['page'],pdf.pages[0].text.splitlines()[p['start_line']-1],p['start_line'],p['end_line'])))
 checks,statuses=validate_financials(tuple(facts),reviewed_document=review)
 c=next(c for c in checks if c.rule=='complete_cash_bridge')
 assert c.result.status=='пройдено' and c.result.delta==0 and set(statuses)=={'проверено_авто'}
 checks,_=validate_financials((facts[0],facts[2]),reviewed_document=review)
 assert next(c for c in checks if c.rule=='complete_cash_bridge').result.status=='недостаточно_данных'
