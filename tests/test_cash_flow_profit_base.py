"""IAS 7/IFRS 18 cash-flow starting profits cannot silently replace one another."""
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from agent import config
from agent.extract.schemas import cash_flow_profit_base,parse_extraction
from agent.extract.pdf_text import Page
from agent.checks.ifrs import validate_financials
from tests.test_extraction_schema import payload,TEXT,PERIOD,cite
from tests.test_ifrs_statements import source


@pytest.mark.parametrize('caption,item',[
 ('Прибыль за период','cf_starting_net_profit'),('Чистый убыток за год','cf_starting_net_profit'),
 ('Profit for the year','cf_starting_net_profit'),('Net loss','cf_starting_net_profit'),
 ('Операционная прибыль','cf_starting_operating_profit'),('Убыток от операционной деятельности','cf_starting_operating_profit'),
 ('Operating loss','cf_starting_operating_profit'),('Прибыль до налогообложения','cf_starting_profit_before_tax'),
 ('Loss before income tax','cf_starting_profit_before_tax')])
def test_original_caption_selects_only_its_published_starting_basis(caption,item):
 assert cash_flow_profit_base(caption)==item
 data=payload();data['facts'][0].update(item_id=item,statement='cash_flow',period_start='2026-01-01',original_name=caption,raw_value='100',evidence=cite(caption+' 100'),cash_flow_section='operating')
 parsed=parse_extraction(json.dumps(data), (Page(1,TEXT+'\n'+caption+' 100'),),ticker='TEST',period=PERIOD,entity_token='Тест',line_items=config.line_items())
 assert parsed.facts[0].item_id==item and parsed.facts[0].value==100
 for other in {'cf_starting_net_profit','cf_starting_operating_profit','cf_starting_profit_before_tax'}-{item}:
  data['facts'][0]['item_id']=other
  with pytest.raises(ValueError,match='базу CFO'):parse_extraction(json.dumps(data),(Page(1,TEXT+'\n'+caption+' 100'),),ticker='TEST',period=PERIOD,entity_token='Тест',line_items=config.line_items())


def test_unknown_profit_caption_does_not_default_to_net_profit():
 assert cash_flow_profit_base('Прибыль') is None and cash_flow_profit_base('Расчётная EBITDA') is None


@pytest.mark.parametrize('item,pnl,rule',[('cf_starting_operating_profit','operating_profit','operating_profit_vs_cash_flow_base'),('cf_starting_profit_before_tax','profit_before_tax','profit_before_tax_vs_cash_flow_base')])
def test_same_window_cross_form_check_uses_declared_base_and_leaves_net_profit_bridge_missing(item,pnl,rule):
 rows=(source('net_profit','20','2026-01-01','2026-06-30','income'),source(pnl,'30','2026-01-01','2026-06-30','income'),source(item,'30','2026-01-01','2026-06-30'))
 checks,_=validate_financials(rows)
 assert next(c for c in checks if c.rule==rule).result.status=='пройдено'
 assert next(c for c in checks if c.rule=='profit_vs_cash_flow_base').result.status=='недостаточно_данных'
 checks,_=validate_financials((*rows[:-1],replace(rows[-1],start=PERIOD.shift_years(-1).start,end=PERIOD.shift_years(-1).end)))
 assert all(c.result.status=='недостаточно_данных' for c in checks if c.rule==rule)


def test_cross_form_coverage_selects_declared_operating_or_pbt_basis_without_promoting_ambiguity():
 from dataclasses import asdict
 from agent.checks.document import coverage
 specification=config.load('ifrs_cross_form_checks')
 for item,pnl in [('cf_starting_operating_profit','operating_profit'),('cf_starting_profit_before_tax','profit_before_tax')]:
  rows=tuple(source(k,'30',f'{y}-01-01',f'{y}-06-30','income' if k==pnl else 'cash_flow') for y in (2026,2025) for k in (item,pnl))
  checks,_=validate_financials(rows);checks=[{**asdict(c),'status':c.result.status} for c in checks]
  family=next(f for f in coverage(checks,PERIOD,specification,facts=rows) if f['id']=='profit_cash_flow')
  assert family['status']=='пройдено' and all(c['declared_cash_flow_base']==item for c in family['requirements'])
  ambiguous=(*rows,source('cf_starting_net_profit','30','2026-01-01','2026-06-30'))
  family=next(f for f in coverage(checks,PERIOD,specification,facts=ambiguous) if f['id']=='profit_cash_flow')
  assert family['status']=='недостаточно_данных' and any(c['basis_limitation'] for c in family['requirements'])
