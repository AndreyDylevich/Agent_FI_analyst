"""FCF по методике проекта: учёт процентов, аренды и классификации в каждом периоде LTM."""

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


def fact(item, value, p=FY, section=None, document=None):
    return Fact(next(IDS), 1, item, D(value) * M, p.end, p.start, p.kind,
                cash_flow_section=section, document_id=document)


def sample(p=FY, section='operating'):
    return [fact('cfo', '100', p), fact('capex_ppe', '35', p), fact('capex_intangibles', '5', p),
            fact('interest_paid', '15', p, section), fact('lease_principal_paid', '10', p)]


def engine(facts, *, industry='retail', standard='IFRS'):
    return Engine(facts, company_id=1, standard=standard,
                  scope='entity' if standard == 'RSBU' else 'group', industry_id=industry,
                  industries=config.industries())


@pytest.mark.parametrize('section,expected', [('operating', '50'), ('financing', '35'), ('investing', '35')])
def test_manual_example_classifies_interest_and_keeps_original_amount(section, expected):
    facts = sample(section=section)
    r = engine(facts).calculate('fcf_to_equity', FY)
    assert r.result.value == D(expected) * M
    assert r.result.inputs_used['interest_paid_outside_cfo'] == (D('0') if section == 'operating' else D('15') * M)
    assert r.inputs['interest_paid_outside_cfo'].source_ids == (facts[3].source_id,)
    assert any(('повторно не вычитается' if section == 'operating' else 'вне CFO учитывается отдельно') in n
               for n in r.result.notes)


@pytest.mark.parametrize('section', [None, 'mixed'])
def test_unknown_or_mixed_total_without_detail_is_not_assumed_to_be_cfo(section):
    r = engine(sample(section=section)).calculate('fcf_to_equity', FY)
    assert r.result.status is Status.INSUFFICIENT_DATA
    assert r.result.missing == ('interest_paid_outside_cfo',)
    assert any('классификация' in n for n in r.result.notes)


@pytest.mark.parametrize('section', [None, 'mixed', 'operating', 'financing'])
def test_explicit_zero_interest_is_not_missing(section):
    facts = sample(section=section)
    facts[3] = replace(facts[3], value=D('0'))
    assert engine(facts).calculate('fcf_to_equity', FY).result.value == D('50') * M


@pytest.mark.parametrize('item', ['cfo', 'capex_ppe', 'capex_intangibles', 'interest_paid', 'lease_principal_paid'])
def test_every_missing_component_blocks_calculation(item):
    facts = [f for f in sample() if f.item_id != item]
    r = engine(facts).calculate('fcf_to_equity', FY).result
    assert r.status is Status.INSUFFICIENT_DATA
    assert r.value is None


def test_explicit_zero_lease_principal_is_valid():
    facts = [replace(f, value=D('0')) if f.item_id == 'lease_principal_paid' else f for f in sample()]
    assert engine(facts).calculate('fcf_to_equity', FY).result.value == D('60') * M


def test_negative_cfo_and_negative_fcf_are_preserved():
    facts = sample(section='financing')
    facts[0] = replace(facts[0], value=D('-20') * M)
    assert engine(facts).calculate('fcf_to_equity', FY).result.value == D('-85') * M


@pytest.mark.parametrize('name', ['capex', 'interest_paid_outside_cfo', 'lease_principal_paid'])
def test_negative_prepared_outflows_are_not_applicable(name):
    values = {'cfo': D('100'), 'capex': D('40'), 'interest_paid_outside_cfo': D('15'),
              'lease_principal_paid': D('10')}
    values[name] = D('-1')
    assert evaluate('fcf_to_equity', values).status is Status.NOT_APPLICABLE


def test_negative_raw_interest_in_cfo_is_not_hidden_by_zero_correction():
    facts = sample()
    facts[3] = replace(facts[3], value=D('-15') * M)
    assert engine(facts).calculate('fcf_to_equity', FY).result.status is Status.NOT_APPLICABLE


def test_split_interest_in_same_document_is_applied_by_section_and_not_double_counted():
    facts = [f for f in sample() if f.item_id != 'interest_paid']
    interest = [fact('interest_paid', '10', section='operating', document=1),
                fact('interest_paid', '5', section='financing', document=1)]
    facts += interest
    e = engine(facts)
    r = e.calculate('fcf_to_equity', FY)
    assert r.result.value == D('45') * M  # 100−40−5−10
    assert r.result.inputs_used['interest_paid_outside_cfo'] == D('5') * M
    assert set(r.inputs['interest_paid_outside_cfo'].source_ids) == {f.source_id for f in interest}
    # Покрытие денежных процентов использует полный платёж 10+5, а не только часть вне CFO.
    facts += [fact('operating_profit', '120'), fact('d_and_a', '30')]
    assert engine(facts).calculate('ebitda_cash_interest_coverage', FY).result.value == D('10')


def test_confirming_documents_with_same_partition_do_not_duplicate_payments():
    facts = [f for f in sample() if f.item_id != 'interest_paid']
    for document in [1, 2]:
        facts += [fact('interest_paid', '10', section='operating', document=document),
                  fact('interest_paid', '5', section='financing', document=document)]
    assert engine(facts).calculate('fcf_to_equity', FY).result.value == D('45') * M


@pytest.mark.parametrize('same_amount', [True, False])
def test_conflicting_section_assignments_across_documents_are_not_summed(same_amount):
    facts = sample()
    facts[3] = replace(facts[3], document_id=1)
    facts.append(fact('interest_paid', '15' if same_amount else '5', section='financing', document=2))
    c = engine(facts).calculate('fcf_to_equity', FY)
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert c.blocked
    assert any('классификация' in n for n in c.result.notes)


def test_total_and_components_are_not_added_together():
    facts = sample(section='mixed')
    facts[3] = replace(facts[3], document_id=1)
    facts += [fact('interest_paid', '10', section='operating', document=1),
              fact('interest_paid', '5', section='financing', document=1)]
    assert engine(facts).calculate('fcf_to_equity', FY).result.status is Status.INSUFFICIENT_DATA


def test_disputed_source_blocks_calculation_even_when_interest_is_in_cfo():
    facts = sample()
    facts[3] = replace(facts[3], check_status='расхождение')
    c = engine(facts).calculate('fcf_to_equity', FY)
    assert c.result.status is Status.INSUFFICIENT_DATA and c.blocked


def test_ltm_applies_classification_before_adding_flow_periods():
    facts = []
    # Денежные потоки: CFO 100+70−40=130; CapEx 40+25−15=50; аренда 10+8−4=14.
    # Проценты FY2025: 30 вне CFO, H1 2026: 20 внутри CFO, H1 2025: 12 вне CFO.
    # Проценты вне CFO LTM = 30+0−12=18; FCF = 130−50−18−14 = 48.
    for p, cfo, ppe, nma, lease, interest, section in [
        (FY, '100', '35', '5', '10', '30', 'financing'),
        (H1, '70', '20', '5', '8', '20', 'operating'),
        (OLD_H1, '40', '10', '5', '4', '12', 'financing'),
    ]:
        facts += [fact('cfo', cfo, p), fact('capex_ppe', ppe, p), fact('capex_intangibles', nma, p),
                  fact('lease_principal_paid', lease, p), fact('interest_paid', interest, p, section)]
    c = engine(facts).calculate('fcf_to_equity', H1.ltm())
    assert c.result.value == D('48') * M
    assert c.result.inputs_used['interest_paid_outside_cfo'] == D('18') * M
    assert len(c.inputs['interest_paid_outside_cfo'].source_ids) == 3
    assert any('повторно не вычитается' in n for n in c.result.notes)


def test_incomplete_ltm_classification_is_not_guessed():
    facts = sample(FY, 'financing') + sample(H1, 'operating') + sample(OLD_H1, None)
    c = engine(facts).calculate('fcf_to_equity', H1.ltm())
    assert c.result.status is Status.INSUFFICIENT_DATA
    assert any('классификация' in n for n in c.result.notes)


@pytest.mark.parametrize('industry', ['banks', 'insurance', 'asset_management', 'brokers', 'exchanges'])
def test_financial_industries_do_not_use_generic_fcf(industry):
    assert engine(sample(), industry=industry).calculate('fcf_to_equity', FY).result.status is Status.NOT_APPLICABLE


def test_rsbu_does_not_infer_missing_lease_principal():
    facts = [replace(f, standard='RSBU', scope='entity') for f in sample()]
    assert engine(facts, standard='RSBU').calculate('fcf_to_equity', FY).result.status is Status.NOT_APPLICABLE


@pytest.mark.parametrize('change', [dict(currency='USD'), dict(company_id=2),
                                   dict(scope='segment', segment_name='Другой сегмент')])
def test_foreign_scope_does_not_fill_missing_interest(change):
    facts = sample()
    facts[3] = replace(facts[3], **change)
    assert engine(facts).calculate('fcf_to_equity', FY).result.status is Status.INSUFFICIENT_DATA


def test_save_keeps_interest_source_even_when_correction_is_zero(tmp_path):
    conn = db.init_db(tmp_path / 'fcf.db')
    conn.execute("INSERT INTO companies (id, ticker, name) VALUES (1, 'TEST', 'Синтетический пример')")
    facts = sample()
    for f in facts:
        conn.execute(
            'INSERT INTO source_values (id, company_id, item_id, value, unit, currency, period_start, '
            'period_end, period_type, standard, scope, source, extracted_by, check_status, cash_flow_section) '
            "VALUES (?, 1, ?, ?, 'руб.', 'RUB', ?, ?, ?, 'IFRS', 'group', 'ir_page', 'manual', 'проверено_вручную', ?)",
            (f.source_id, f.item_id, str(f.value), f.start.isoformat(), f.end.isoformat(), f.period_type, f.cash_flow_section))
    loaded = load_facts(conn, company_id=1, standard='IFRS', scope='group', segment_name=None,
                        line_items=config.line_items())
    assert next(f for f in loaded if f.item_id == 'interest_paid').cash_flow_section == 'operating'
    e = engine(loaded)
    with conn:
        saved_id = e.save(conn, e.calculate('fcf_to_equity', FY))
    row = conn.execute('SELECT * FROM metric_values WHERE id=?', (saved_id,)).fetchone()
    assert D(row['value']) == D('50') * M
    assert 'повторно не вычитается' in row['notes']
    interest_source = conn.execute("SELECT source_value_id FROM metric_inputs WHERE metric_value_id=? "
                                   "AND input_name='interest_paid_outside_cfo'", (saved_id,)).fetchone()[0]
    assert interest_source == facts[3].source_id
    assert conn.execute('SELECT value FROM source_values WHERE id=?', (interest_source,)).fetchone()[0] == '15000000'
    conn.close()


def test_old_schema_can_be_loaded_without_guessing_classification(tmp_path):
    import sqlite3
    from pathlib import Path

    path = tmp_path / 'legacy.db'
    old = sqlite3.connect(path)
    old.row_factory = sqlite3.Row
    old.executescript((Path(__file__).parent / 'fixtures' / 'schema_v1.sql').read_text())
    old.execute("INSERT INTO companies (id, ticker, name) VALUES (1, 'TEST', 'Тест')")
    for f in sample():
        old.execute(
            'INSERT INTO source_values (id, company_id, item_id, value, unit, currency, period_start, '
            'period_end, period_type, standard, scope, source, extracted_by) '
            "VALUES (?, 1, ?, ?, 'руб.', 'RUB', ?, ?, ?, 'IFRS', 'group', 'ir_page', 'manual')",
            (f.source_id, f.item_id, str(f.value), f.start.isoformat(), f.end.isoformat(), f.period_type))
    loaded = load_facts(old, company_id=1, standard='IFRS', scope='group', segment_name=None,
                        line_items=config.line_items())
    assert all(f.cash_flow_section is None for f in loaded)
    assert engine(loaded).calculate('fcf_to_equity', FY).result.status is Status.INSUFFICIENT_DATA
    # Чтение старой базы само по себе не запускает миграцию и не создаёт выдуманную классификацию.
    assert 'cash_flow_section' not in {r['name'] for r in old.execute('PRAGMA table_info(source_values)')}
    old.close()


def test_cash_flow_section_cannot_be_attached_to_balance_article(tmp_path):
    conn = db.init_db(tmp_path / 'invalid.db')
    conn.execute("INSERT INTO companies (id, ticker, name) VALUES (1, 'TEST', 'Тест')")
    conn.execute("INSERT INTO source_values (company_id, item_id, value, unit, currency, period_end, "
                 "standard, scope, source, extracted_by, cash_flow_section) VALUES "
                 "(1, 'current_assets', '100', 'руб.', 'RUB', '2025-12-31', 'IFRS', 'group', 'ir_page', 'manual', 'operating')")
    with pytest.raises(ValueError, match='не для денежного потока'):
        load_facts(conn, company_id=1, standard='IFRS', scope='group', segment_name=None,
                   line_items=config.line_items())
    conn.close()
