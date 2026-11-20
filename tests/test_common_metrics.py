"""Формулы на вручную сверенных числах; примеры синтетические, суммы в рублях."""

from decimal import Decimal

import pytest

from agent.metrics.registry import REGISTRY, Status, evaluate


@pytest.mark.parametrize('metric,values,expected', [
    ('quick_ratio', dict(trade_receivables='60', short_term_investments='20', cash_and_equivalents='40', current_liabilities='100'), '1.2'),
    ('absolute_ratio', dict(short_term_investments='20', cash_and_equivalents='40', current_liabilities='100'), '0.6'),
    ('cash_to_short_term_debt', dict(cash_and_equivalents='40', debt_short_term='50'), '0.8'),
    ('total_debt', dict(debt_long_term='300', debt_short_term='100'), '400'),
    ('total_debt_incl_leases', dict(total_debt='400', lease_liabilities_long='80', lease_liabilities_short='20'), '500'),
    ('debt_to_equity', dict(total_debt='400', equity_total='200'), '2'),
    ('short_term_debt_share', dict(debt_short_term='100', total_debt='400'), '25'),
    ('equity_ratio', dict(equity_total='-50', total_assets='200'), '-0.25'),
    ('net_debt_incl_leases_to_ebitda', dict(net_debt_incl_leases='450', ebitda='150'), '3'),
    ('ebit_interest_coverage', dict(operating_profit='-20', interest_expense='10'), '-2'),
    ('ebitda_interest_coverage', dict(ebitda='150', interest_expense='30'), '5'),
    ('ebitda_cash_interest_coverage', dict(ebitda='150', interest_paid='25'), '6'),
    ('rsbu_interest_coverage', dict(profit_before_tax='60', interest_expense='20'), '4'),
    ('capex', dict(capex_ppe='70', capex_intangibles='10'), '80'),
    ('fcf_simple', dict(cfo='60', capex='80'), '-20'),
    ('cash_conversion', dict(cfo='90', ebitda='150'), '0.6'),
    ('gross_margin', dict(gross_profit='40', revenue='200'), '20'),
    ('operating_margin', dict(operating_profit='30', revenue='200'), '15'),
    ('ebitda_margin', dict(ebitda='50', revenue='200'), '25'),
    ('net_margin', dict(net_profit='-10', revenue='200'), '-5'),
    ('roe', dict(net_profit_parent='20', equity_parent='100'), '20'),
    ('roa', dict(net_profit='20', total_assets='400'), '5'),
])
def test_manual_values(metric, values, expected):
    r = evaluate(metric, {k: Decimal(v) for k, v in values.items()})
    assert r.status is Status.CALCULATED
    assert r.value == Decimal(expected)
    assert r.explanation


@pytest.mark.parametrize('metric', [
    'quick_ratio', 'absolute_ratio', 'cash_to_short_term_debt', 'debt_to_equity',
    'short_term_debt_share', 'equity_ratio', 'net_debt_incl_leases_to_ebitda',
    'ebit_interest_coverage', 'ebitda_interest_coverage', 'ebitda_cash_interest_coverage',
    'rsbu_interest_coverage', 'cash_conversion', 'gross_margin', 'operating_margin',
    'ebitda_margin', 'net_margin', 'roe', 'roa',
])
def test_zero_inputs_do_not_cause_division_error(metric):
    d = REGISTRY[metric]
    r = evaluate(metric, {k: Decimal('0') for k in d.inputs})
    assert r.status is Status.NOT_APPLICABLE
    assert r.value is None


@pytest.mark.parametrize('metric', list(REGISTRY))
def test_missing_input_never_becomes_zero(metric):
    assert evaluate(metric, {}).status is Status.INSUFFICIENT_DATA


@pytest.mark.parametrize('number', ['NaN', 'sNaN', 'Infinity', '-Infinity'])
def test_nonfinite_input_rejected(number):
    with pytest.raises(ValueError, match='конечным'):
        evaluate('current_ratio', {'current_assets': Decimal(number), 'current_liabilities': Decimal('1')})
