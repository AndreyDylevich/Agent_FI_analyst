"""Проверки не принимают неполную сверку за успех и не смешивают разные базы сравнения."""

from dataclasses import replace
from datetime import date
from decimal import Decimal as D, localcontext

import pytest

from agent.checks.validate import ComparisonContext, compare_periods, reconcile


def test_balance_and_missing_zero_are_distinct():
    terms = {'assets': 1, 'equity': -1, 'liabilities': -1}
    assert reconcile('balance', {'assets': D('100'), 'equity': D('0'),
                                  'liabilities': D('100')}, terms).status == 'пройдено'
    result = reconcile('balance', {'assets': D('100'), 'liabilities': D('100')}, terms)
    assert result.status == 'недостаточно_данных'
    assert result.delta is None and result.missing == ('equity',)


def test_cash_cross_form_bridge_includes_explicit_adjustments():
    # В балансе 110, в ОДДС 100; раскрытая корректировка состава денег равна 10.
    terms = {'balance_cash': 1, 'cash_flow_cash': -1, 'composition_bridge': -1}
    values = {'balance_cash': D('110'), 'cash_flow_cash': D('100')}
    assert reconcile('cash_bridge', values, terms).status == 'недостаточно_данных'
    result = reconcile('cash_bridge', {**values, 'composition_bridge': D('10')}, terms)
    assert result.status == 'пройдено' and result.delta == 0
    assert reconcile('cash_bridge', {**values, 'composition_bridge': D('0')}, terms).delta == 10


def test_debt_rollforward_needs_non_cash_changes():
    # Долг: 100 + привлечение 30 − погашение 20 + валютные/неденежные изменения 5 = 115.
    terms = {'opening': 1, 'proceeds': 1, 'repayments': -1, 'noncash': 1, 'closing': -1}
    values = {'opening': D('100'), 'proceeds': D('30'), 'repayments': D('20'), 'closing': D('115')}
    assert reconcile('debt', values, terms).missing == ('noncash',)
    assert reconcile('debt', {**values, 'noncash': D('5')}, terms).status == 'пройдено'


@pytest.mark.parametrize('delta,status', [('0', 'пройдено'), ('1', 'пройдено'),
                                         ('-1', 'пройдено'), ('1.01', 'расхождение')])
def test_rounding_tolerance_is_explicit_and_absolute(delta, status):
    result = reconcile('ifrs', {'a': D(delta)}, {'a': 1}, tolerance=D('1'))
    assert result.status == status and result.tolerance == 1
    if delta != '0':
        assert reconcile('rsbu', {'a': D(delta)}, {'a': 1}).status == 'расхождение'


def test_source_disagreement_blocks_even_if_sums_equal():
    result = reconcile('balance', {'a': D('1'), 'b': D('1')}, {'a': 1, 'b': -1}, disputed=('a',))
    assert result.status == 'расхождение' and result.delta is None
    assert result.reasons and 'a' in result.reasons[0]


def test_precision_does_not_hide_small_discrepancy_in_large_amounts():
    with localcontext() as ctx:
        ctx.prec = 3
        result = reconcile('exact', {'a': D('1000000000000000000000000000000.01'),
                                      'b': D('1000000000000000000000000000000')}, {'a': 1, 'b': -1})
    assert result.delta == D('0.01') and result.status == 'расхождение'


@pytest.mark.parametrize('value', [1.0, True, D('NaN'), D('Infinity')])
def test_invalid_numbers_rejected(value):
    with pytest.raises(ValueError):
        reconcile('bad', {'a': value}, {'a': 1})


@pytest.mark.parametrize('tolerance', [1.0, D('-1'), D('NaN')])
def test_invalid_tolerance_rejected(tolerance):
    with pytest.raises(ValueError):
        reconcile('bad', {'a': D('1')}, {'a': 1}, tolerance=tolerance)


@pytest.mark.parametrize('terms', [{}, {'a': 0}, {'a': True}, {'a': 1.0}])
def test_invalid_equations_rejected(terms):
    with pytest.raises(ValueError):
        reconcile('bad', {'a': D('1')}, terms)


def contexts():
    current = ComparisonContext(
        company='MVID', item_id='revenue', standard='IFRS', scope='group', currency='RUB', unit='руб.',
        start=date(2025, 1, 1), end=date(2025, 12, 31), kind='FY', source='report-2025#page=10',
        accounting_basis='IAS1-policy-v1', perimeter_basis='group-v1', methodology_version='revenue:1',
    )
    previous = replace(current, start=date(2024, 1, 1), end=date(2024, 12, 31),
                       source='report-2025#page=10:comparative')
    return current, previous


def test_yoy_allows_leap_year_and_separate_source():
    assert compare_periods(*contexts()).status == 'пройдено'


@pytest.mark.parametrize('change', [
    {'company': 'OTHER'}, {'item_id': 'gmv'}, {'standard': 'RSBU', 'scope': 'entity'},
    {'scope': 'entity'}, {'currency': 'USD'}, {'unit': 'млн руб.'},
    {'accounting_basis': 'IFRS18-policy-v2'}, {'perimeter_basis': 'acquired-new-business'},
    {'methodology_version': 'revenue:2'},
    {'start': date(2024, 1, 1), 'end': date(2024, 6, 30), 'kind': 'H1'},
    {'start': date(2023, 1, 1), 'end': date(2023, 12, 31)},
])
def test_mismatched_contexts_block_comparison(change):
    current, previous = contexts()
    result = compare_periods(current, replace(previous, **change))
    assert result.status == 'несопоставимо' and result.reasons


@pytest.mark.parametrize('key', ['accounting_basis', 'perimeter_basis', 'methodology_version'])
def test_unknown_basis_not_assumed_comparable(key):
    current, previous = contexts()
    result = compare_periods(current, replace(previous, **{key: None}))
    assert result.status == 'недостаточно_данных' and result.missing == (key,)


def test_ifrs18_restated_comparative_must_share_verified_basis():
    current, previous = contexts()
    result = compare_periods(replace(current, accounting_basis='IFRS18'), previous)
    assert result.status == 'несопоставимо'
    # Эту базу и источник обязан подтвердить адаптер извлечения, не календарная дата.
    assert compare_periods(replace(current, accounting_basis='IFRS18'),
                           replace(previous, accounting_basis='IFRS18')).status == 'пройдено'


def test_stock_comparison_and_flow_cannot_be_mixed():
    current, previous = contexts()
    stock = replace(current, start=None, kind='AT_DATE')
    assert compare_periods(stock, replace(previous, start=None, kind='AT_DATE')).status == 'пройдено'
    assert compare_periods(stock, previous).status == 'несопоставимо'


@pytest.mark.parametrize('change', [{'source': ''}, {'scope': 'segment'}, {'segment': 'bank'},
                                    {'kind': 'AT_DATE'}, {'end': date(2025, 11, 30)}])
def test_invalid_context_rejected(change):
    current, _ = contexts()
    with pytest.raises(ValueError):
        replace(current, **change)
