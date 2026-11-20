"""Рост выручки и CAGR: ручные эталоны, сопоставимость истории, сохранение источников."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from agent import config, db
from agent.metrics.data import load_facts
from agent.metrics.engine import Engine, Fact, Period
from agent.metrics.registry import REGISTRY, Status, evaluate, get, register, validate_registry

D = Decimal
M = D('1000000')
FY = Period(date(2025, 1, 1), date(2025, 12, 31), 'FY')


def period(year, kind='FY'):
    month, day = {'FY': (12, 31), 'Q1': (3, 31), 'H1': (6, 30), '9M': (9, 30)}[kind]
    return Period(date(year, 1, 1), date(year, month, day), kind)


def revenue(source_id, amount, p=FY, **kwargs):
    return Fact(source_id, 1, 'revenue', D(amount) * M, p.end, p.start, p.kind, **kwargs)


def engine(facts):
    return Engine(facts, company_id=1, standard='IFRS', scope='group', industry_id='retail',
                  industries=config.industries())


def history(values):
    # Полная годовая история заканчивается в 2025 году, без привязки к реальным компаниям.
    return [revenue(i + 1, value, period(2026 - len(values) + i)) for i, value in enumerate(values)]


@pytest.mark.parametrize('kind', ['FY', 'Q1', 'H1', '9M'])
def test_yoy_compares_same_kind_with_last_year(kind):
    current, previous = period(2025, kind), period(2024, kind)
    facts = [revenue(1, '125', current), revenue(2, '100', previous),
             revenue(3, '9999', period(2023, kind))]
    c = engine(facts).calculate('revenue_yoy', current)
    assert c.result.value == D('25')
    assert c.result.inputs_used == {'revenue_current': D('125') * M, 'revenue_previous': D('100') * M}
    assert c.inputs['revenue_previous'].source_ids == (2,)
    assert '125,0 / 100,0' in c.result.explanation
    assert any(str(previous.end) in n for n in c.result.notes)


@pytest.mark.parametrize('current,previous,expected', [('80', '100', '-20'), ('0', '100', '-100'),
                                                      ('100', '100', '0')])
def test_yoy_decline_zero_and_flat(current, previous, expected):
    r = evaluate('revenue_yoy', {'revenue_current': D(current), 'revenue_previous': D(previous)})
    assert r.value == D(expected)


@pytest.mark.parametrize('current,previous', [('100', '0'), ('100', '-10'), ('-10', '100')])
def test_yoy_invalid_base_or_negative_current_is_not_applicable(current, previous):
    r = evaluate('revenue_yoy', {'revenue_current': D(current), 'revenue_previous': D(previous)})
    assert r.status is Status.NOT_APPLICABLE
    assert r.value is None


def test_yoy_missing_previous_is_not_replaced_with_other_period():
    p = period(2025, 'H1')
    facts = [revenue(1, '125', p), revenue(2, '100', period(2024, 'FY')),
             revenue(3, '70', period(2024, 'Q1'))]
    c = engine(facts).calculate('revenue_yoy', p)
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert c.result.missing == ('revenue_previous',)


@pytest.mark.parametrize('change', [dict(standard='RSBU', scope='entity'), dict(currency='USD'),
                                   dict(company_id=2), dict(scope='segment', segment_name='Другой сегмент')])
def test_yoy_rejects_incompatible_history(change):
    other = replace(revenue(2, '100', period(2024)), **change)
    c = engine([revenue(1, '125'), other]).calculate('revenue_yoy', FY)
    assert c.result.status is Status.INSUFFICIENT_DATA


@pytest.mark.parametrize('metric,values,expected', [
    ('revenue_cagr_3y', ['100', '110', '121', '133.1'], '10'),
    ('revenue_cagr_5y', ['100', '110', '121', '133.1', '146.41', '161.051'], '10'),
    ('revenue_cagr_3y', ['100', '90', '81', '72.9'], '-10'),
    ('revenue_cagr_5y', ['100', '100', '100', '100', '100', '100'], '0'),
])
def test_cagr_known_annual_growth(metric, values, expected):
    c = engine(history(values)).calculate(metric, FY)
    assert c.result.status is Status.CALCULATED
    # Дробная степень вычисляется в Decimal; проверка против независимо известного темпа.
    assert abs(c.result.value - D(expected)) < D('1e-23')
    assert len(c.inputs) == len(values)
    assert len({source for p in c.inputs.values() for source in p.source_ids}) == len(values)
    assert '%' in c.result.explanation


def test_cagr_preserves_unrounded_value():
    c = engine(history(['100', '105', '110', '125'])).calculate('revenue_cagr_3y', FY)
    assert c.result.value != c.result.value.quantize(D('0.01'))
    # Независимая проверка: восстановление отношения конечной выручки к начальной.
    assert abs((1 + c.result.value / 100) ** 3 - D('1.25')) < D('1e-25')


@pytest.mark.parametrize('metric,values,missing_index', [
    ('revenue_cagr_3y', ['100', '110', '121', '133.1'], 0),
    ('revenue_cagr_3y', ['100', '110', '121', '133.1'], 1),
    ('revenue_cagr_5y', ['100', '110', '121', '133.1', '146.41', '161.051'], 3),
])
def test_cagr_incomplete_history_does_not_shorten_window(metric, values, missing_index):
    facts = history(values)
    del facts[missing_index]
    c = engine(facts).calculate(metric, FY)
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert c.result.value is None
    assert len(c.result.missing) == 1


@pytest.mark.parametrize('metric', ['revenue_cagr_3y', 'revenue_cagr_5y'])
@pytest.mark.parametrize('kind', ['Q1', 'H1', '9M', 'LTM'])
def test_cagr_requires_full_year_result_period(metric, kind):
    target = FY.ltm() if kind == 'LTM' else period(2025, kind)
    c = engine(history(['100', '110', '121', '133.1'])).calculate(metric, target)
    assert c.result.status is Status.NOT_APPLICABLE
    assert 'FY' in c.result.reason


@pytest.mark.parametrize('bad,index', [('0', 0), ('-10', 0), ('0', 3), ('-10', 1)])
def test_cagr_requires_positive_revenue_history(bad, index):
    values = ['100', '110', '121', '133.1']
    values[index] = bad
    c = engine(history(values)).calculate('revenue_cagr_3y', FY)
    assert c.result.status is Status.NOT_APPLICABLE
    assert c.result.value is None


def test_cagr_conflicting_middle_year_blocks_calculation():
    facts = history(['100', '110', '121', '133.1'])
    facts.append(replace(facts[1], source_id=99, value=D('999') * M))
    c = engine(facts).calculate('revenue_cagr_3y', FY)
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert any('противоречащие источники' in n for n in c.result.notes)


def test_history_outside_calendar_range_is_missing_not_crash():
    c = engine([]).calculate('revenue_cagr_5y', period(2))
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert any('календарный диапазон' in n for n in c.result.notes)


def test_save_cagr_keeps_each_year_and_its_alias(tmp_path):
    conn = db.init_db(tmp_path / 'history.db')
    conn.execute("INSERT INTO companies (id, ticker, name) VALUES (1, 'TEST', 'Синтетическая история')")
    for f in history(['100', '110', '121', '133.1']):
        conn.execute(
            'INSERT INTO source_values (id, company_id, item_id, value, unit, currency, period_start, '
            'period_end, period_type, standard, scope, source, extracted_by, check_status) '
            "VALUES (?, 1, 'revenue', ?, 'руб.', 'RUB', ?, ?, 'FY', 'IFRS', 'group', 'ir_page', 'manual', 'проверено_вручную')",
            (f.source_id, str(f.value), f.start.isoformat(), f.end.isoformat()))
    facts = load_facts(conn, company_id=1, standard='IFRS', scope='group', segment_name=None,
                       line_items=config.line_items())
    e = engine(facts)
    with conn:
        saved_id = e.save(conn, e.calculate('revenue_cagr_3y', FY))
    rows = conn.execute('SELECT i.input_name, s.period_end FROM metric_inputs i '
                        'JOIN source_values s ON s.id = i.source_value_id WHERE i.metric_value_id = ?',
                        (saved_id,)).fetchall()
    assert {r[0]: r[1] for r in rows} == {
        'revenue_start': '2022-12-31', 'revenue_year_1': '2023-12-31',
        'revenue_year_2': '2024-12-31', 'revenue_end': '2025-12-31'}
    conn.close()


@pytest.mark.parametrize('overrides', [dict(input_items={'unknown': 'revenue'}),
                                      dict(input_year_offsets={'revenue_current': 1}),
                                      dict(input_year_offsets={'revenue_current': -0.5}),
                                      dict(period_kinds=('quarter',))])
def test_invalid_input_metadata_rejected(overrides):
    with pytest.raises(ValueError):
        register(replace(get('revenue_yoy'), id='invalid_growth', **overrides))
    assert 'invalid_growth' not in REGISTRY


def test_registry_validates_canonical_item_behind_alias(monkeypatch):
    monkeypatch.setitem(REGISTRY, 'revenue_yoy', replace(get('revenue_yoy'),
                                                       input_items={'revenue_current': 'nonexistent',
                                                                    'revenue_previous': 'revenue'}))
    errors = validate_registry(config.line_items())
    assert len(errors) == 1
    assert 'revenue_current' in errors[0]
