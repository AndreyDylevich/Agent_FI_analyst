"""Строгий автоматический путь МСФО блокирует сравнения без подтверждённого контекста."""

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from agent import config
from agent.metrics.engine import Engine, Fact, Period
from agent.metrics.registry import Status

PERIOD=Period(date(2026,1,1),date(2026,6,30),'H1')


def engine(rows, strict=True):
    return Engine(rows,company_id=1,standard='IFRS',scope='group',industry_id='telecom',
                  industries=config.industries(),require_comparability=strict)


def revenues():
    return [Fact(i,1,'revenue',D(value),date(year,6,30),date(year,1,1),'H1')
            for i,year,value in [(1,2026,'120'),(2,2025,'100')]]


def confirmed(rows):
    return [replace(row,accounting_basis='same-policy-v1',perimeter_basis='same-business-v1',
                    methodology_version='revenue-gross-v1',comparison_evidence='report.pdf#page=12') for row in rows]


def test_yoy_requires_context_despite_correct_dates_and_arithmetic():
    result=engine(revenues()).calculate('revenue_yoy',PERIOD)
    assert result.result.status is Status.INSUFFICIENT_DATA and result.result.value is None and result.blocked
    assert 'accounting_basis' in result.result.reason and result.result.explanation is None
    # Режим ручного просмотра прежнего API остаётся доступен и не означает приёмку контекста.
    assert engine(revenues(),False).calculate('revenue_yoy',PERIOD).result.value==20


def test_consistent_supported_context_allows_exact_yoy():
    result=engine(confirmed(revenues())).calculate('revenue_yoy',PERIOD)
    assert result.result.status is Status.CALCULATED and result.result.value==20 and not result.blocked


@pytest.mark.parametrize('field,value',[('accounting_basis','ifrs18-new-presentation'),
    ('perimeter_basis','without-sold-subsidiary'),('methodology_version','revenue-net-v2'),
    ('comparison_evidence',None)])
def test_changed_perimeter_policy_method_or_missing_evidence_blocks_comparison(field,value):
    rows=confirmed(revenues());rows[0]=replace(rows[0],**{field:value})
    result=engine(rows).calculate('revenue_yoy',PERIOD)
    assert result.result.value is None and result.blocked and result.result.status is Status.INSUFFICIENT_DATA


def test_single_date_liquidity_does_not_require_temporal_comparison():
    rows=[Fact(1,1,'current_assets',D('40'),PERIOD.end),Fact(2,1,'current_liabilities',D('20'),PERIOD.end)]
    assert engine(rows).calculate('current_ratio',PERIOD).result.value==2


def test_ltm_requires_consistent_basis_for_every_component():
    rows=[]
    for item,values in [('capex_ppe',(30,15,10)),('capex_intangibles',(15,10,10))]:
        for year,month,day,kind,value in [(2025,12,31,'FY',values[0]),
            (2026,6,30,'H1',values[1]),(2025,6,30,'H1',values[2])]:
            rows.append(Fact(len(rows)+1,1,item,D(value),date(year,month,day),date(year,1,1),kind))
    result=engine(rows).calculate('capex',PERIOD.ltm())
    assert result.result.value is None and result.blocked
    approved=confirmed(rows)
    result=engine(approved).calculate('capex',PERIOD.ltm())
    # (30+15-10) + (15+10-10) = 50; считает Python, контекст подтверждён отдельно.
    assert result.result.value==50
    approved[-1]=replace(approved[-1],methodology_version='different-definition')
    assert engine(approved).calculate('capex',PERIOD.ltm()).result.value is None
