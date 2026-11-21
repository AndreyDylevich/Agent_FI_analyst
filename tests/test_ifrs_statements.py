"""ОПУ/ОДДС: сверки разных форм и дат, без обнуления пропусков и сложения остатков в LTM."""

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from agent import config
from agent.checks.ifrs import validate_financials
from agent.extract.pdf_text import Page
from agent.metrics.registry import PeriodRule, REGISTRY, MetricDef, register, validate_registry
from tests.test_extraction_schema import payload, parse, TEXT, cite


def source(item,value,start,end,statement='cash_flow'):
    base=parse(payload()).facts[0]
    return replace(base,item_id=item,value=D(value),normalized=D(value)*1000000,raw_value=value,
                   start=date.fromisoformat(start) if start else None,end=date.fromisoformat(end),
                   statement=statement,period_type='H1' if end.endswith('06-30') else 'FY')


def test_profit_attribution_and_cash_flow_base_use_same_window():
    rows=tuple(source(item,value,'2026-01-01','2026-06-30','income' if not item.startswith('cf_') else 'cash_flow')
               for item,value in [('net_profit','75858'),('net_profit_parent','74030'),('net_profit_nci','1828'),('cf_starting_net_profit','75858')])
    checks,statuses=validate_financials(rows)
    assert next(c for c in checks if c.rule=='profit_attribution').result.status=='пройдено'
    assert next(c for c in checks if c.rule=='profit_vs_cash_flow_base').result.status=='пройдено'
    assert set(statuses)=={'проверено_авто'}
    wrong_window=replace(rows[-1],start=date(2025,1,1),end=date(2025,6,30))
    checks,_=validate_financials((*rows[:-1],wrong_window))
    assert all(c.result.status=='недостаточно_данных' for c in checks if c.rule=='profit_vs_cash_flow_base')


def test_cash_rollforward_connects_beginning_date_and_ending_date():
    rows=(source('cf_cash_change','-13479','2026-01-01','2026-06-30'),
          source('cf_cash_opening','77966',None,'2025-12-31'),
          source('cf_cash_closing_gross','64487',None,'2026-06-30'))
    checks,statuses=validate_financials(rows)
    check=next(c for c in checks if c.rule=='cash_rollforward')
    assert check.result.status=='пройдено' and check.result.delta==0
    assert check.start=='2026-01-01' and check.end=='2026-06-30'
    assert set(statuses)=={'проверено_авто'}
    checks,_=validate_financials((rows[0],replace(rows[1],end=date(2024,12,31)),rows[2]))
    check=next(c for c in checks if c.rule=='cash_rollforward')
    assert check.result.status=='недостаточно_данных' and check.result.missing==('cf_cash_opening',)


def test_held_for_sale_bridge_requires_explicit_adjustment():
    rows=tuple(source(item,value,None,'2025-06-30') for item,value in
               [('cf_cash_closing_gross','71309'),('cf_cash_held_for_sale','55'),('cf_cash_closing_after_hfs','71254')])
    checks,statuses=validate_financials(rows)
    assert checks[0].rule=='cash_held_for_sale_bridge' and checks[0].result.status=='пройдено'
    checks,_=validate_financials((rows[0],rows[2]))
    assert checks[0].result.status=='недостаточно_данных'
    assert checks[0].result.missing==('cf_cash_held_for_sale',)


def test_cash_opening_column_year_is_flow_year_and_date_is_prior_end():
    data=payload();row=data['facts'][0]
    row.update(item_id='cf_cash_opening',original_name='Деньги на начало',raw_value='77',
               evidence=cite('Деньги на начало 77'),period_end='2025-12-31',period_type='AT_DATE',
               column_header='2026',statement='cash_flow')
    pages=(Page(1,TEXT+'\nДеньги на начало 77'),)
    fact=parse(data,pages).facts[0]
    assert fact.start is None and fact.end==date(2025,12,31)
    row['period_end']='2025-06-30'
    with pytest.raises(ValueError):parse(data,pages)


def test_stock_in_cash_flow_is_not_accepted_as_flow():
    assert config.is_stock(config.line_items()['cf_cash_opening'])
    assert not config.is_stock(config.line_items()['cfo'])
    data=payload();row=data['facts'][0]
    row.update(item_id='cf_cash_opening',original_name='Деньги на начало',raw_value='77',
               evidence=cite('Деньги на начало 77'),period_start='2026-01-01',statement='cash_flow')
    with pytest.raises(ValueError):parse(data,(Page(1,TEXT+'\nДеньги на начало 77'),))


def test_ocr_unit_evidence_prevents_automatic_native_fact_approval():
    data=payload();data['facts'][0]['unit_evidence']={'page':2,'quote':'В миллионах российских рублей'}
    result=parse(data,(Page(1,TEXT),Page(2,'В миллионах российских рублей','ocr')))
    assert result.facts[0].from_ocr


def test_registry_rejects_treating_cash_opening_as_flow(monkeypatch):
    import agent.metrics.registry as registry
    definition=replace(REGISTRY['current_ratio'],id='bad_cf_stock',inputs={'cf_cash_opening':PeriodRule.LTM})
    monkeypatch.setattr(registry,'REGISTRY',{'bad_cf_stock':definition})
    errors=validate_registry(config.line_items())
    assert any('остаток' in error and 'LTM' in error for error in errors)


def test_database_loader_keeps_cash_stock_and_cash_movement_distinct(tmp_path):
    from agent.db import init_db
    from agent.extract.store import store_extraction
    from agent.metrics.data import load_facts
    from tests.test_ifrs_pipeline import make_run
    from tests.test_extraction_schema import PERIOD
    rows=(source('cf_cash_change','-13479','2026-01-01','2026-06-30'),
          source('cf_cash_opening','77966',None,'2025-12-31'),
          source('cf_cash_closing_gross','64487',None,'2026-06-30'))
    conn=init_db(tmp_path/'cash.db')
    with conn:
        stored=store_extraction(conn,make_run(rows),ticker='TEST',name='Тест',source_url='https://example.test/report.pdf',period=PERIOD,local_path='report.pdf')
    loaded=load_facts(conn,company_id=stored['company_id'],standard='IFRS',scope='group',segment_name=None,line_items=config.line_items())
    by_item={fact.item_id:fact for fact in loaded}
    assert by_item['cf_cash_opening'].start is None
    assert by_item['cf_cash_opening'].end==date(2025,12,31)
    assert by_item['cf_cash_change'].start==date(2026,1,1)
    conn.close()


def test_annual_flow_not_accepted_as_half_year_comparative():
    data=payload();row=data['facts'][0]
    row.update(item_id='revenue',original_name='Выручка',raw_value='90',evidence=cite('Выручка 100 90'),
               statement='income',period_start='2025-01-01',period_end='2025-12-31',period_type='FY',column_header='2025')
    with pytest.raises(ValueError):parse(data)


def test_observation_plan_supplies_dates_without_supplying_financial_values():
    from agent.extract.pipeline import observation_plan
    from tests.test_extraction_schema import PERIOD
    items=config.line_items()
    plan=observation_plan({key:items[key] for key in ('cf_cash_opening','cf_cash_closing_gross','cf_starting_net_profit')},PERIOD)
    assert plan[0]=={'item_id':'cf_cash_opening','column_header':'2026','period_start':None,'period_end':'2025-12-31','period_type':'AT_DATE'}
    assert plan[1]['period_end']=='2024-12-31' and plan[1]['column_header']=='2025'
    assert plan[2]['period_end']=='2026-06-30' and plan[3]['period_end']=='2025-06-30'
    assert plan[4]['period_start']=='2026-01-01' and plan[4]['period_type']=='H1'
    assert all('value' not in record for record in plan)
