"""Оборачиваемость: ручные эталоны, периоды LTM, ограничения и цепочка источников."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import count

import pytest

from agent import config, db
from agent.metrics.data import load_facts
from agent.metrics.engine import Engine, Fact, Period
from agent.metrics.registry import Status, evaluate

D = Decimal
M = D('1000000')
IDS = count(1)
FY = Period(date(2025, 1, 1), date(2025, 12, 31), 'FY')
H1 = Period(date(2026, 1, 1), date(2026, 6, 30), 'H1')
OLD_H1 = H1.shift_years(-1)
PAIRS = [
    ('inventory_turnover', 'inventory_days', 'cost_of_sales', 'inventories'),
    ('receivables_turnover', 'receivables_days', 'revenue', 'trade_receivables'),
    ('payables_turnover', 'payables_days', 'cost_of_sales', 'trade_payables'),
]


def fact(item, value, p=H1, *, stock=False):
    return Fact(next(IDS), 1, item, D(value) * M, p.end,
                None if stock else p.start, None if stock else p.kind)


def engine(facts, *, standard='IFRS', industry='retail'):
    return Engine(facts, company_id=1, standard=standard,
                  scope='entity' if standard == 'RSBU' else 'group', industry_id=industry,
                  industries=config.industries())


def sample(flow, stock):
    # LTM=1000+600−400=1200; средний остаток=(100+140)/2=120;
    # оборотов=10; дней=365/10=36,5. Остаток на декабрь намеренно другой.
    return [fact(flow, '1000', FY), fact(flow, '600', H1), fact(flow, '400', OLD_H1),
            fact(stock, '100', OLD_H1, stock=True), fact(stock, '140', H1, stock=True),
            fact(stock, '9999', FY, stock=True)]


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
def test_manual_ltm_and_days(ratio, days, flow, stock):
    facts = sample(flow, stock)
    e = engine(facts)
    turnover = e.calculate(ratio, H1)
    duration = e.calculate(days, H1)
    assert turnover.result.value == D('10')
    assert turnover.result.inputs_used == {flow: D('1200') * M, stock: D('120') * M}
    assert duration.result.value == D('36.5')
    assert duration.period == H1.ltm()
    assert duration.result.explanation.endswith('= 36,50 дн.')
    dependency, = duration.inputs[ratio].calculations
    assert dependency is turnover
    assert dependency.period.start == date(2025, 7, 1)
    assert set(duration.inputs[ratio].source_ids) == {f.source_id for f in facts[:5]}


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
def test_annual_calculation_accepts_rsbu_and_keeps_entity_note(ratio, days, flow, stock):
    begin = FY.shift_years(-1)
    facts = [fact(flow, '1200', FY), fact(stock, '100', begin, stock=True),
             fact(stock, '140', FY, stock=True)]
    facts = [replace(f, standard='RSBU', scope='entity') for f in facts]
    e = engine(facts, standard='RSBU')
    assert e.calculate(ratio, FY).result.value == D('10')
    result = e.calculate(days, FY).result
    assert result.value == D('36.5')
    assert 'Отчётность отдельного юрлица, не группы' in result.notes


@pytest.mark.parametrize('kind,end', [('Q1', date(2026, 3, 31)), ('H1', date(2026, 6, 30)),
                                     ('9M', date(2026, 9, 30))])
def test_interim_ltm_periods_use_same_date_opening_balance(kind, end):
    target = Period(date(2026, 1, 1), end, kind)
    old = target.shift_years(-1)
    facts = [fact('cost_of_sales', '1000', FY), fact('cost_of_sales', '600', target),
             fact('cost_of_sales', '400', old), fact('inventories', '100', old, stock=True),
             fact('inventories', '140', target, stock=True)]
    assert engine(facts).calculate('inventory_days', target).result.value == D('36.5')


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
def test_missing_start_uses_end_with_note_propagated_to_days(ratio, days, flow, stock):
    facts = sample(flow, stock)
    del facts[3]  # начало LTM; декабрьский остаток не должен подменить его
    e = engine(facts)
    turnover = e.calculate(ratio, H1).result
    assert turnover.value == D('1200') / D('140')
    result = e.calculate(days, H1).result
    assert abs(result.value - D('365') * D('140') / D('1200')) < D('1e-24')
    assert any('начало периода отсутствует, использован конец' in n for n in result.notes)


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
def test_missing_closing_balance_blocks_calculation(ratio, days, flow, stock):
    facts = sample(flow, stock)
    del facts[4]
    r = engine(facts).calculate(ratio, H1).result
    assert r.status is Status.INSUFFICIENT_DATA
    assert r.value is None


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
def test_incomplete_ltm_is_not_replaced_with_ytd_annualization(ratio, days, flow, stock):
    facts = sample(flow, stock)
    del facts[2]
    r = engine(facts).calculate(ratio, H1).result
    assert r.status is Status.INSUFFICIENT_DATA
    assert any('неполный LTM' in n for n in r.notes)


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
@pytest.mark.parametrize('opening,closing', [('-10', '140'), ('100', '-10'), ('0', '0')])
def test_invalid_stock_balances_are_not_applicable(ratio, days, flow, stock, opening, closing):
    facts = sample(flow, stock)
    facts[3] = replace(facts[3], value=D(opening) * M)
    facts[4] = replace(facts[4], value=D(closing) * M)
    r = engine(facts).calculate(ratio, H1).result
    assert r.status is Status.NOT_APPLICABLE
    assert r.value is None


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
def test_zero_opening_stock_is_valid_if_average_is_positive(ratio, days, flow, stock):
    facts = sample(flow, stock)
    facts[3] = replace(facts[3], value=D('0'))
    r = engine(facts).calculate(ratio, H1).result
    assert r.status is Status.CALCULATED
    assert r.value == D('1200') / D('70')


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
def test_zero_flow_gives_zero_turnover_and_undefined_days(ratio, days, flow, stock):
    facts = sample(flow, stock)
    for i in range(3):
        facts[i] = replace(facts[i], value=D('0'))
    e = engine(facts)
    assert e.calculate(ratio, H1).result.value == D('0')
    r = e.calculate(days, H1).result
    assert r.status is Status.NOT_APPLICABLE
    assert r.value is None


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
def test_negative_flow_ltm_requires_review(ratio, days, flow, stock):
    facts = sample(flow, stock)
    facts[2] = replace(facts[2], value=D('2000') * M)
    r = engine(facts).calculate(ratio, H1).result
    assert r.status is Status.NOT_APPLICABLE
    assert r.value is None
    assert '< 0' in r.reason


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
def test_conflicting_opening_is_not_treated_as_absent(ratio, days, flow, stock):
    facts = sample(flow, stock)
    facts.append(replace(facts[3], source_id=next(IDS), value=D('200') * M))
    r = engine(facts).calculate(ratio, H1).result
    assert r.status is Status.INSUFFICIENT_DATA
    assert any('противоречащие источники' in n for n in r.notes)
    assert not any('использован конец' in n for n in r.notes)


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
@pytest.mark.parametrize('industry', ['banks', 'insurance'])
def test_turnover_excluded_for_financial_industries(ratio, days, flow, stock, industry):
    e = engine(sample(flow, stock), industry=industry)
    assert e.calculate(ratio, H1).result.status is Status.NOT_APPLICABLE
    assert e.calculate(days, H1).result.status is Status.NOT_APPLICABLE


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
def test_never_uses_other_scope_or_currency(ratio, days, flow, stock):
    facts = sample(flow, stock)
    facts[4] = replace(facts[4], currency='USD')
    facts.append(replace(facts[4], source_id=next(IDS), currency='RUB', scope='segment', segment_name='Другой'))
    r = engine(facts).calculate(ratio, H1).result
    assert r.status is Status.INSUFFICIENT_DATA


@pytest.mark.parametrize('ratio,days,flow,stock', PAIRS)
def test_direct_formulas_and_no_storage_rounding(ratio, days, flow, stock):
    turnover = evaluate(ratio, {flow: D('1000'), stock: D('121')})
    assert turnover.value == D('1000') / D('121')
    result = evaluate(days, {ratio: turnover.value})
    assert result.value == D('365') / turnover.value
    assert result.value != result.value.quantize(D('0.01'))
    assert result.explanation.endswith('дн.')


def test_leap_year_still_uses_365_by_methodology():
    p = Period(date(2024, 1, 1), date(2024, 12, 31), 'FY')
    facts = [fact('cost_of_sales', '1200', p), fact('inventories', '120', p, stock=True)]
    assert engine(facts).calculate('inventory_days', p).result.value == D('36.5')


def test_save_days_preserves_ltm_formula_and_all_source_references(tmp_path):
    conn = db.init_db(tmp_path / 'turnover.db')
    conn.execute("INSERT INTO companies (id, ticker, name) VALUES (1, 'TEST', 'Синтетический пример')")
    facts = sample('cost_of_sales', 'inventories')
    for f in facts:
        conn.execute(
            'INSERT INTO source_values (id, company_id, item_id, value, unit, currency, period_start, '
            'period_end, period_type, standard, scope, source, extracted_by, check_status) '
            "VALUES (?, 1, ?, ?, 'руб.', 'RUB', ?, ?, ?, 'IFRS', 'group', 'ir_page', 'manual', 'проверено_вручную')",
            (f.source_id, f.item_id, str(f.value), f.start.isoformat() if f.start else None,
             f.end.isoformat(), f.period_type))
    loaded = load_facts(conn, company_id=1, standard='IFRS', scope='group', segment_name=None,
                        line_items=config.line_items())
    e = engine(loaded)
    with conn:
        saved_id = e.save(conn, e.calculate('inventory_days', H1))
    rows = conn.execute('SELECT * FROM metric_values ORDER BY id').fetchall()
    assert len(rows) == 2  # дни и один коэффициент LTM, без сложения годовых коэффициентов
    assert {row['metric_id'] for row in rows} == {'inventory_days', 'inventory_turnover'}
    assert all(row['period_type'] == 'LTM' and row['period_start'] == '2025-07-01' for row in rows)
    assert D(rows[0]['value']) == D('36.5')
    dependency = conn.execute('SELECT * FROM metric_inputs WHERE metric_value_id = ?', (saved_id,)).fetchone()
    assert dependency['input_metric_value_id'] == rows[1]['id']
    referenced = {row[0] for row in conn.execute('SELECT source_value_id FROM metric_inputs '
                                               'WHERE source_value_id IS NOT NULL')}
    assert referenced == {f.source_id for f in facts[:5]}
    conn.close()
