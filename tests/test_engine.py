"""Периоды и происхождение расчётов; все примеры синтетические, не данные компаний."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import count

import pytest

from agent import config, db
from agent.metrics.data import load_facts, normalize_money
from agent.metrics.engine import Engine, Fact, Period
from agent.metrics.registry import REGISTRY, PeriodRule, Status, evaluate

D = Decimal
IDS = count(1)
FY = Period(date(2025, 1, 1), date(2025, 12, 31), 'FY')
H1 = Period(date(2026, 1, 1), date(2026, 6, 30), 'H1')
OLD_H1 = Period(date(2025, 1, 1), date(2025, 6, 30), 'H1')


def fact(item, value, period=H1, *, stock=False, **kwargs):
    return Fact(next(IDS), 1, item, D(value), period.end,
                None if stock else period.start, None if stock else period.kind, **kwargs)


def engine(facts, **kwargs):
    args = dict(company_id=1, standard='IFRS', scope='group', industry_id='retail',
                industries=config.industries())
    args.update(kwargs)
    return Engine(facts, **args)


def ebitda_facts():
    # EBITDA FY=120+30=150, H1=90+20=110, H1 прошлого года=50+10=60; LTM=200.
    return [fact(item, value, p) for p, op, da in [(FY, '120', '30'), (H1, '90', '20'),
                                                  (OLD_H1, '50', '10')]
            for item, value in [('operating_profit', op), ('d_and_a', da)]]


def test_ltm_of_derived_ebitda_and_debt_dependencies():
    facts = ebitda_facts() + [fact('debt_long_term', '300', stock=True),
                             fact('debt_short_term', '150', stock=True),
                             fact('cash_and_equivalents', '50', stock=True)]
    c = engine(facts).calculate('net_debt_to_ebitda', H1)
    assert c.result.value == D('2')  # (300 + 150 − 50) / (150 + 110 − 60)
    assert c.result.inputs_used == {'net_debt': D('400'), 'ebitda': D('200')}
    assert len(c.inputs['ebitda'].source_ids) == 6
    assert {p.period for p in c.inputs['ebitda'].calculations} == {FY, H1, OLD_H1}
    assert any('impairment_noncurrent' in n for n in c.result.notes)
    assert any('LTM' in n for n in c.result.notes)


def test_ltm_missing_previous_comparable_period_is_not_annualized():
    c = engine(ebitda_facts()[:4]).calculate('ebitda_interest_coverage', H1)
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert c.result.value is None
    assert any('неполный LTM' in n for n in c.result.notes)


def test_fy_uses_annual_flow_without_prior_year():
    e = engine([fact('net_profit', '20', FY), fact('total_assets', '100', FY, stock=True)])
    c = e.calculate('roa', FY)
    assert c.result.value == D('20')
    assert c.period == FY.ltm()
    assert any('использован конец' in n for n in c.result.notes)


def test_average_for_ltm_uses_last_year_same_date_not_december():
    facts = [fact('net_profit', v, p) for p, v in [(FY, '30'), (H1, '25'), (OLD_H1, '15')]]
    facts += [fact('total_assets', '300', OLD_H1, stock=True),
              fact('total_assets', '500', H1, stock=True), fact('total_assets', '9999', FY, stock=True)]
    c = engine(facts).calculate('roa', H1)
    assert c.result.value == D('10')  # (30+25−15) / ((300+500)/2) × 100
    assert len(c.inputs['total_assets'].source_ids) == 2
    assert not any('использован конец' in n for n in c.result.notes)


def test_negative_equity_at_opening_blocks_roe_even_with_positive_average():
    begin = Period(date(2024, 1, 1), date(2024, 12, 31), 'FY')
    facts = [fact('net_profit_parent', '10', FY), fact('equity_parent', '-10', begin, stock=True),
             fact('equity_parent', '100', FY, stock=True)]
    c = engine(facts).calculate('roe', FY)
    assert c.result.status is Status.NOT_APPLICABLE
    assert c.result.value is None


@pytest.mark.parametrize('change', [dict(standard='RSBU', scope='entity'), dict(company_id=2),
                                   dict(scope='segment', segment_name='Банк'), dict(currency='USD')])
def test_never_mixes_standard_company_scope_or_currency(change):
    wrong = replace(fact('current_liabilities', '100', stock=True), **change)
    c = engine([fact('current_assets', '200', stock=True), wrong]).calculate('current_ratio', H1)
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert c.result.missing == ('current_liabilities',)


def test_ytd_does_not_accept_a_separate_quarter_or_wrong_period_type():
    wrong = replace(fact('revenue', '100'), start=date(2026, 4, 1), period_type='Q1')
    c = engine([wrong, fact('gross_profit', '40')]).calculate('gross_margin', H1)
    assert c.result.status is Status.INSUFFICIENT_DATA


@pytest.mark.parametrize('status', ['проверено_авто', 'расхождение'])
def test_conflicting_or_disputed_sources_block_calculation(status):
    first = fact('current_assets', '100', stock=True)
    second = replace(first, source_id=next(IDS), value=D('200'), check_status=status)
    c = engine([first, second, fact('current_liabilities', '50', stock=True)]).calculate('current_ratio', H1)
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert any('источник' in n or 'расхождение' in n for n in c.result.notes)


def test_equal_sources_keep_both_references_and_unverified_warning():
    first = fact('current_assets', '100', stock=True)
    second = replace(first, source_id=next(IDS), check_status='не_проверено')
    c = engine([first, second, fact('current_liabilities', '50', stock=True)]).calculate('current_ratio', H1)
    assert c.result.value == D('2')
    assert c.inputs['current_assets'].source_ids == (first.source_id, second.source_id)
    assert any('не проверены' in n for n in c.result.notes)


def test_bad_opening_balance_is_not_replaced_with_closing():
    begin = Period(date(2024, 1, 1), date(2024, 12, 31), 'FY')
    facts = [fact('net_profit', '10', FY), fact('total_assets', '100', FY, stock=True),
             fact('total_assets', '80', begin, stock=True, check_status='расхождение')]
    c = engine(facts).calculate('roa', FY)
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert not any('использован конец' in n for n in c.result.notes)


def test_rsbu_blocks_ebitda_and_keeps_entity_warning():
    e = engine([], standard='RSBU', scope='entity')
    assert e.calculate('ebitda', FY).result.status is Status.NOT_APPLICABLE
    assert 'Отчётность отдельного юрлица, не группы' in e.calculate('net_debt', FY).result.notes


def test_bank_does_not_calculate_ebitda_margin():
    e = engine([fact('revenue', '100')], industry_id='banks')
    assert e.calculate('ebitda_margin', H1).result.status is Status.NOT_APPLICABLE


def test_cycle_rejected(monkeypatch):
    original = REGISTRY['current_ratio']
    monkeypatch.setitem(REGISTRY, 'current_ratio', replace(
        original, inputs={'current_ratio': PeriodRule.AT_DATE}))
    with pytest.raises(ValueError, match='Циклическая'):
        engine([]).calculate('current_ratio', H1)


@pytest.mark.parametrize('kind,end', [('FY', date(2026, 6, 30)), ('Q1', date(2026, 12, 31)),
                                     ('unknown', date(2026, 6, 30))])
def test_inconsistent_period_rejected(kind, end):
    with pytest.raises(ValueError):
        Period(date(2026, 1, 1), end, kind)


@pytest.mark.parametrize('value,unit,sign,expected', [
    ('-123.4567', 'млн руб.', 'outflow', '123456700'),
    ('-123.4567', 'тыс. руб.', 'natural', '-123456.7'),
    ('0', 'руб.', 'natural', '0'),
])
def test_normalization_exact(value, unit, sign, expected):
    assert normalize_money(value, unit, sign) == D(expected)


@pytest.mark.parametrize('value,unit', [('NaN', 'руб.'), ('Infinity', 'руб.'), ('1', 'долл.')])
def test_invalid_money_rejected(value, unit):
    with pytest.raises(ValueError):
        normalize_money(value, unit, 'natural')


def insert_fact(conn, f, *, unit='руб.', raw=None, norm=None):
    conn.execute(
        'INSERT INTO source_values (id, company_id, item_id, value, unit, value_norm, currency, '
        'period_start, period_end, period_type, standard, scope, source, extracted_by, check_status) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ir_page', 'manual', ?)",
        (f.source_id, f.company_id, f.item_id, raw or str(f.value), unit, norm,
         f.currency, f.start.isoformat() if f.start else None, f.end.isoformat(), f.period_type,
         f.standard, f.scope, f.check_status))


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / 'test.db')
    c.execute("INSERT INTO companies (id, ticker, name) VALUES (1, 'TEST', 'Синтетический пример')")
    yield c
    c.close()


def test_database_normalization_and_superseded_history(conn):
    old = fact('current_assets', '100', stock=True)
    new = fact('current_assets', '125000', stock=True)
    insert_fact(conn, old)
    insert_fact(conn, new, unit='тыс. руб.', raw='125', norm='125000')
    db.supersede(conn, old.source_id, new.source_id)
    facts = load_facts(conn, company_id=1, standard='IFRS', scope='group', segment_name=None,
                       line_items=config.line_items())
    assert len(facts) == 1
    assert facts[0].value == D('125000')
    assert facts[0].source_id == new.source_id
    assert conn.execute('SELECT count(*) FROM source_values').fetchone()[0] == 2


def test_database_wrong_normalized_value_rejected(conn):
    insert_fact(conn, fact('current_assets', '100', stock=True), norm='200')
    with pytest.raises(ValueError, match='не совпадает'):
        load_facts(conn, company_id=1, standard='IFRS', scope='group', segment_name=None,
                   line_items=config.line_items())


def test_save_records_whole_formula_graph_and_does_not_overwrite(conn):
    facts = ebitda_facts() + [fact('debt_long_term', '300', stock=True),
                             fact('debt_short_term', '150', stock=True),
                             fact('cash_and_equivalents', '50', stock=True)]
    for f in facts:
        insert_fact(conn, f)
    e = engine(load_facts(conn, company_id=1, standard='IFRS', scope='group', segment_name=None,
                          line_items=config.line_items()))
    calculation = e.calculate('net_debt_to_ebitda', H1)
    with conn:
        saved_id = e.save(conn, calculation)
    saved = conn.execute('SELECT * FROM metric_values WHERE id=?', (saved_id,)).fetchone()
    assert D(saved['value']) == D('2')
    assert saved['formula_version'] == 1
    assert saved['period_start'] is None  # остаток / EBITDA LTM, результат на дату
    assert saved['period_end'] == '2026-06-30'
    # Корень, чистый долг и три EBITDA: каждому результату сохранена собственная версия формулы.
    assert conn.execute('SELECT count(*) FROM metric_values').fetchone()[0] == 5
    dependencies = conn.execute('SELECT input_metric_value_id FROM metric_inputs '
                                'WHERE metric_value_id=?', (saved_id,)).fetchall()
    assert len(dependencies) == 4
    assert all(row[0] is not None for row in dependencies)
    referenced = {r[0] for r in conn.execute('SELECT source_value_id FROM metric_inputs '
                                            'WHERE source_value_id IS NOT NULL')}
    assert referenced == {f.source_id for f in facts}
    e.save(conn, calculation)
    assert conn.execute('SELECT count(*) FROM metric_values').fetchone()[0] == 10
