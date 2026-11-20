"""ROIC: ручные эталоны, налоговые допущения, состав капитала и происхождение расчёта.

Все финансовые числа синтетические; номинальные ставки — конфигурационные допущения.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import count

import pytest

from agent import config, db
from agent.metrics.data import load_facts
from agent.metrics.engine import Engine, Fact, Period
from agent.metrics.parameters import nominal_profit_tax_rate
from agent.metrics.registry import Status, evaluate

D = Decimal
M = D('1000000')
IDS = count(1)
FY = Period(date(2025, 1, 1), date(2025, 12, 31), 'FY')
H1 = Period(date(2026, 1, 1), date(2026, 6, 30), 'H1')
OLD_H1 = H1.shift_years(-1)
STOCKS = ('equity_total', 'debt_long_term', 'debt_short_term', 'lease_liabilities_long',
          'lease_liabilities_short', 'cash_and_equivalents')


def fact(item, value, p=FY, *, stock=False):
    return Fact(next(IDS), 1, item, D(value) * M, p.end,
                None if stock else p.start, None if stock else p.kind)


def capital(p, *, equity='600', cash='100'):
    # Капитал 600 + долг 300 + аренда 100 − деньги 100 = 900 млн руб.
    values = (equity, '200', '100', '80', '20', cash)
    return [fact(item, value, p, stock=True) for item, value in zip(STOCKS, values)]


def engine(facts, *, standard='IFRS', industry='retail'):
    return Engine(facts, company_id=1, standard=standard,
                  scope='entity' if standard == 'RSBU' else 'group', industry_id=industry,
                  industries=config.industries())


def annual(*, tax='24', pbt='120', op='150', include_start=True):
    facts = capital(FY, equity='800')  # конечный ИК = 1100
    if include_start:
        facts += capital(FY.shift_years(-1))  # начальный ИК = 900; средний = 1000
    facts += [fact('operating_profit', op), fact('profit_before_tax', pbt)]
    if tax is not None:
        facts.append(fact('income_tax', tax))
    return facts


@pytest.mark.parametrize('metric,values,expected', [
    ('invested_capital', dict(equity_total='600', total_debt_incl_leases='400', cash_and_equivalents='100'), '900'),
    ('effective_tax_rate', dict(income_tax='24', profit_before_tax='120'), '20'),
    ('nominal_tax_rate', dict(configured_rate='25'), '25'),
    ('roic', dict(operating_profit='150', tax_rate='20', invested_capital='1000'), '12'),
    ('roic', dict(operating_profit='-150', tax_rate='25', invested_capital='1000'), '-11.25'),
    ('roic', dict(operating_profit='150', tax_rate='100', invested_capital='1000'), '0'),
])
def test_direct_manual_formulas(metric, values, expected):
    result = evaluate(metric, {k: D(v) for k, v in values.items()})
    assert result.status is Status.CALCULATED
    assert result.value == D(expected)


def test_annual_roic_uses_effective_rate_and_average_complete_capital():
    c = engine(annual()).calculate('roic', FY)
    # 150 × (1 − 24/120) / ((900+1100)/2) × 100 = 12%.
    assert c.result.value == D('12')
    assert c.result.inputs_used == {'operating_profit': D('150') * M, 'tax_rate': D('20'),
                                    'invested_capital': D('1000') * M}
    assert not any('Допущение:' in n for n in c.result.notes)
    assert '150,0 × (1 − 20,00 / 100) / 1 000,0' in c.result.explanation
    assert len(c.inputs['invested_capital'].source_ids) == 12
    assert {v.period for v in c.inputs['invested_capital'].calculations} == {FY, FY.shift_years(-1)}


def test_interim_roic_uses_ltm_for_both_profit_and_effective_rate():
    facts = capital(OLD_H1) + capital(H1, equity='800')
    # EBIT LTM = 100+80−30 = 150; PBT LTM = 90+60−30 = 120; налог LTM = 18+12−6 = 24.
    for p, op, pbt, tax in [(FY, '100', '90', '18'), (H1, '80', '60', '12'),
                             (OLD_H1, '30', '30', '6')]:
        facts += [fact('operating_profit', op, p), fact('profit_before_tax', pbt, p), fact('income_tax', tax, p)]
    # Остаток на декабрь не должен подменять начало LTM на июнь прошлого года.
    facts += capital(FY, equity='9999')
    c = engine(facts).calculate('roic', H1)
    assert c.result.value == D('12')
    assert c.period.start == date(2025, 7, 1)
    assert c.result.inputs_used['tax_rate'] == D('20')
    assert len(c.inputs['tax_rate'].calculations) == 1  # ставка LTM не складывается как денежный поток


def test_missing_start_uses_entire_closing_capital_with_note():
    c = engine(annual(include_start=False)).calculate('roic', FY)
    assert c.result.value == D('120') / D('1100') * D('100')
    assert c.result.inputs_used['invested_capital'] == D('1100') * M
    assert any('invested_capital: начало периода отсутствует, использован конец' in n for n in c.result.notes)


def test_partial_opening_capital_does_not_mix_component_averages():
    facts = annual(include_start=False)
    facts.append(fact('equity_total', '600', FY.shift_years(-1), stock=True))
    c = engine(facts).calculate('roic', FY)
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert c.result.value is None
    assert not any('использован конец' in n for n in c.result.notes)


@pytest.mark.parametrize('item', STOCKS)
def test_any_missing_closing_component_blocks_roic(item):
    facts = [f for f in annual() if not (f.item_id == item and f.end == FY.end)]
    c = engine(facts).calculate('roic', FY)
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert c.result.value is None


@pytest.mark.parametrize('day', [FY.end, FY.shift_years(-1).end])
@pytest.mark.parametrize('cash', ['1000', '1200'])
def test_nonpositive_capital_at_either_boundary_is_not_applicable(day, cash):
    facts = annual()
    facts = [replace(f, value=D(cash) * M) if f.item_id == 'cash_and_equivalents' and f.end == day else f
             for f in facts]
    # Для конечного капитала 800+400=1200; для начального — 600+400=1000.
    if day == FY.end and cash == '1000':
        facts = [replace(f, value=D('1200') * M) if f.item_id == 'cash_and_equivalents' and f.end == day else f
                 for f in facts]
    c = engine(facts).calculate('roic', FY)
    assert c.result.status is Status.NOT_APPLICABLE
    assert c.result.value is None
    assert 'границе периода' in c.result.reason or 'капитал ≤ 0' in c.result.reason


def test_explicit_zero_leases_are_valid():
    facts = [replace(f, value=D('0')) if f.item_id.startswith('lease_liabilities_') else f for f in annual()]
    c = engine(facts).calculate('roic', FY)
    assert c.result.value == D('120') / D('900') * D('100')


@pytest.mark.parametrize('tax,pbt', [(None, '120'), ('24', '0'), ('24', '-120'), ('150', '120'), ('-24', '120')])
def test_undefined_effective_rate_uses_nominal_with_explicit_assumption(tax, pbt):
    c = engine(annual(tax=tax, pbt=pbt)).calculate('roic', FY)
    assert c.result.value == D('11.25')
    assert c.result.inputs_used['tax_rate'] == D('25')
    assert any('Допущение: номинальная ставка 25%' in n for n in c.result.notes)
    assert any('резервный показатель — nominal_tax_rate' in n for n in c.result.notes)
    assert any('version=1' in n and 'percent=25' in n for n in c.result.notes)
    assert {calc.result.metric_id for calc in c.inputs['tax_rate'].calculations} == {
        'effective_tax_rate', 'nominal_tax_rate'}


def test_explicit_zero_tax_is_valid_effective_rate_not_missing():
    c = engine(annual(tax='0')).calculate('roic', FY)
    assert c.result.value == D('15')
    assert c.result.inputs_used['tax_rate'] == D('0')
    assert not any('Допущение:' in n for n in c.result.notes)


@pytest.mark.parametrize('item', ['income_tax', 'profit_before_tax'])
@pytest.mark.parametrize('status', ['расхождение', 'проверено_авто'])
def test_disputed_or_conflicting_tax_inputs_never_trigger_nominal_fallback(item, status):
    facts = annual()
    original = next(f for f in facts if f.item_id == item)
    facts.append(replace(original, source_id=next(IDS), value=original.value + M, check_status=status))
    c = engine(facts).calculate('roic', FY)
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert c.blocked
    assert not any('Допущение:' in n for n in c.result.notes)


def test_disputed_prior_ytd_tax_blocks_ltm_fallback():
    facts = capital(OLD_H1) + capital(H1, equity='800')
    for p in [FY, H1, OLD_H1]:
        facts += [fact('operating_profit', '100', p), fact('profit_before_tax', '80', p),
                  fact('income_tax', '16', p)]
    facts = [replace(f, check_status='расхождение') if f.item_id == 'income_tax' and f.end == OLD_H1.end else f
             for f in facts]
    c = engine(facts).calculate('roic', H1)
    assert c.result.value is None
    assert c.blocked
    assert not any('Допущение:' in n for n in c.result.notes)


@pytest.mark.parametrize('on_date,expected', [(date(2024, 12, 31), '20'), (date(2025, 1, 1), '25'),
                                           (date(2026, 6, 30), '25')])
def test_nominal_rate_boundaries_from_config(on_date, expected):
    p = nominal_profit_tax_rate(on_date)
    assert p.value == D(expected)
    assert str(on_date) in p.notes[0]
    assert 'config/methodology.yaml' in p.notes[0]


def test_nominal_rate_missing_historical_range_is_not_guessed():
    assert nominal_profit_tax_rate(date(2019, 12, 31)).value is None
    p = Period(date(2019, 1, 1), date(2019, 12, 31), 'FY')
    facts = capital(p) + [fact('operating_profit', '100', p)]
    assert engine(facts).calculate('roic', p).result.status is Status.INSUFFICIENT_DATA


def test_nominal_rate_at_end_of_ltm_is_explicit_when_rate_changed():
    p = Period(date(2025, 1, 1), date(2025, 6, 30), 'H1')
    facts = capital(p) + capital(p.shift_years(-1))
    for window in [p, p.shift_years(-1), FY.shift_years(-1)]:
        facts.append(fact('operating_profit', '100', window))
    c = engine(facts).calculate('roic', p)
    assert c.result.inputs_used['tax_rate'] == D('25')
    assert any('без усреднения при смене ставки внутри LTM' in n for n in c.result.notes)


@pytest.mark.parametrize('industry', ['banks', 'insurance', 'asset_management', 'brokers', 'exchanges'])
def test_financial_industries_do_not_use_roic(industry):
    assert engine(annual(), industry=industry).calculate('roic', FY).result.status is Status.NOT_APPLICABLE


def test_rsbu_does_not_substitute_profit_from_sales_for_ebit():
    facts = [replace(f, standard='RSBU', scope='entity') for f in annual()]
    assert engine(facts, standard='RSBU').calculate('roic', FY).result.status is Status.NOT_APPLICABLE


@pytest.mark.parametrize('change', [dict(currency='USD'), dict(company_id=2),
                                   dict(scope='segment', segment_name='Другой сегмент')])
def test_foreign_scope_or_currency_does_not_fill_missing_closing_capital(change):
    facts = annual()
    facts = [replace(f, **change) if f.item_id == 'cash_and_equivalents' and f.end == FY.end else f for f in facts]
    assert engine(facts).calculate('roic', FY).result.status is Status.INSUFFICIENT_DATA


@pytest.mark.parametrize('tax', ['24', None])
def test_save_keeps_both_capital_dates_tax_basis_and_fallback_snapshot(tmp_path, tax):
    conn = db.init_db(tmp_path / 'roic.db')
    conn.execute("INSERT INTO companies (id, ticker, name) VALUES (1, 'TEST', 'Синтетический пример')")
    facts = annual(tax=tax)
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
        saved_id = e.save(conn, e.calculate('roic', FY))
    row = conn.execute('SELECT * FROM metric_values WHERE id = ?', (saved_id,)).fetchone()
    assert row['period_type'] == 'LTM'
    assert D(row['value']) == (D('12') if tax else D('11.25'))
    referenced = {row[0] for row in conn.execute('SELECT source_value_id FROM metric_inputs WHERE source_value_id IS NOT NULL')}
    assert referenced == {f.source_id for f in facts}
    capitals = conn.execute("SELECT * FROM metric_values WHERE metric_id = 'invested_capital'").fetchall()
    assert {r['period_end'] for r in capitals} == {'2024-12-31', '2025-12-31'}
    if tax is None:
        nominal = conn.execute("SELECT * FROM metric_values WHERE metric_id = 'nominal_tax_rate'").fetchone()
        assert nominal['value'] == '25'
        assert 'version=1' in nominal['notes'] and 'percent=25' in nominal['notes']
        effective = conn.execute("SELECT * FROM metric_values WHERE metric_id = 'effective_tax_rate'").fetchone()
        assert effective['status'] == Status.INSUFFICIENT_DATA.value
    conn.close()


@pytest.mark.parametrize('percent', ['NaN', 'Infinity', '-1', '101', 25.0])
def test_invalid_nominal_rates_fail_explicitly(monkeypatch, percent):
    monkeypatch.setattr(config, 'load', lambda name: {
        'version': 1,
        'nominal_profit_tax_rates': [{'from': '2025-01-01', 'through': None,
                                     'percent': percent, 'source': 'https://www.nalog.gov.ru/new2025/'}]})
    with pytest.raises(ValueError):
        nominal_profit_tax_rate(FY.end)


def test_overlapping_config_rates_are_not_chosen_arbitrarily(monkeypatch):
    row = {'from': '2025-01-01', 'through': None, 'percent': '25', 'source': 'https://www.nalog.gov.ru/new2025/'}
    monkeypatch.setattr(config, 'load', lambda name: {
        'version': 1, 'nominal_profit_tax_rates': [row, {**row, 'percent': '20'}]})
    with pytest.raises(ValueError, match='Пересекающиеся'):
        nominal_profit_tax_rate(FY.end)
