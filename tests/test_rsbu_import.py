"""Реальный JSON ГИР БО → SQLite → реестр: единицы, знаки, версии, пропуски и конфликты.

Эталон М.Видео: официальный текст PDF https://bo.nalog.gov.ru/download/bfo/pdf/57656029,
стр. 1 — тыс. руб.; 2–3 — баланс; 4 — финансовые результаты. Это отдельное юрлицо, не группа.
В изменённых копиях снимка ниже проверяются искусственно заданные ошибки и разовые ситуации.
"""

import copy
import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest

from agent import config, db
from agent.metrics.data import load_facts
from agent.metrics.engine import Engine, Period
from agent.metrics.registry import Status
from agent.sources.rsbu import UnitEvidence, parse_snapshot, store_report
from agent.tools.import_girbo import main

FOLDER = Path(__file__).parent / 'fixtures' / 'girbo'
CONTENT = (FOLDER / 'mvideo_bfo_metadata.json').read_bytes()
MANIFEST = json.loads((FOLDER / 'manifest.json').read_text())
SOURCE = next(x for x in MANIFEST['sources'] if x['file'] == 'mvideo_bfo_metadata.json')
UNIT_SOURCE = 'https://bo.nalog.gov.ru/download/bfo/pdf/57656029#page=1 (единица подтверждена по тексту PDF)'
UNIT = UnitEvidence('тыс. руб.', UNIT_SOURCE, True)
FY = Period(date(2025, 1, 1), date(2025, 12, 31), 'FY')
D = Decimal


def parse(content=CONTENT, **kwargs):
    args = dict(year=2025, expected_inn='7707602010', source_url=SOURCE['url'], unit=UNIT,
                line_items=config.line_items())
    args.update(kwargs)
    return parse_snapshot(content, **args)


def altered(change):
    data = json.loads(CONTENT)
    record = next(x for x in data if x['period'] == '2025')
    correction = record['typeCorrections'][0]['correction']
    change(data, record, correction)
    return json.dumps(data).encode()


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / 'check.db')
    yield connection
    connection.close()


def calculate(conn, imported, metric, period=FY):
    facts = load_facts(conn, company_id=imported['company_id'], standard='RSBU', scope='entity',
                       segment_name=None, line_items=config.line_items())
    return Engine(facts, company_id=imported['company_id'], standard='RSBU', scope='entity',
                  industry_id='retail', industries=config.industries()).calculate(metric, period)


def test_real_report_uses_published_year_and_keeps_provenance():
    r = parse(expected_sha256=SOURCE['sha256'])
    assert r.year == 2025 and r.bfo_id == 30236354 and r.correction_id == 57656029
    assert r.inn == '7707602010'
    assert r.sha256 == hashlib.sha256(CONTENT).hexdigest()
    item = next(x for x in r.rows if x.item_id == 'current_assets' and x.column == 'current')
    assert item.value == D('33217459')  # стр. 2, строка 1200, тыс. руб.
    assert item.normalized == D('33217459000')
    assert item.field == 'current1200'
    assert 'balance.current1200' in item.location
    assert item.period_start is None and item.period_end == '2025-12-31'


def test_real_balance_and_flow_checks_have_no_discrepancies():
    r = parse()
    assert not any(x.status == 'расхождение' for x in r.checks)
    balance = [x for x in r.checks if x.form == 'balance']
    assert len(balance) == 9 and all(x.status == 'пройдено' for x in balance)
    assert all(D(x.delta) == 0 for x in balance)
    assert next(x for x in r.checks if x.name == 'cff' and x.column == 'current').status == 'недостаточно_данных'


@pytest.mark.parametrize('metric,expected', [
    ('current_ratio', '2.62'), ('quick_ratio', '2.62'), ('absolute_ratio', '0.00'),
    ('debt_to_equity', '2.59'), ('equity_ratio', '0.28'), ('short_term_debt_share', '38.27'),
    ('revenue_yoy', '-18.10'), ('net_margin', '19.99'), ('roa', '0.13'),
])
def test_real_reference_values_against_manual_baseline(conn, metric, expected):
    imported = store_report(conn, parse(), 'MVID')
    calculation = calculate(conn, imported, metric)
    assert calculation.result.status is Status.CALCULATED
    assert calculation.result.value.quantize(D('0.01'), rounding=ROUND_HALF_UP) == D(expected)
    assert 'Отчётность отдельного юрлица, не группы' in calculation.result.notes
    if metric == 'absolute_ratio':
        assert calculation.result.value > 0  # вывод 0,00 не означает сохранённый ноль


@pytest.mark.parametrize('metric,expected', [('total_debt', '32858688000'), ('net_debt', '32857497000')])
def test_real_monetary_baseline_is_in_rubles_without_storage_rounding(conn, metric, expected):
    imported = store_report(conn, parse(), 'MVID')
    assert calculate(conn, imported, metric).result.value == D(expected)


def test_null_is_not_zero_and_explicit_zero_remains_zero(conn):
    report = parse()
    assert not any(r.item_id == 'debt_long_term' and r.column == 'previous' for r in report.rows)
    investment = next(r for r in report.rows if r.item_id == 'short_term_investments' and r.column == 'current')
    assert investment.value == 0 and investment.normalized == 0
    imported = store_report(conn, report, 'MVID')
    assert calculate(conn, imported, 'net_debt', FY.shift_years(-1)).result.status is Status.INSUFFICIENT_DATA
    assert calculate(conn, imported, 'fcf_simple').result.status is Status.INSUFFICIENT_DATA  # строка 4221 не подменяет ОС+НМА
    assert calculate(conn, imported, 'ebitda').result.status is Status.NOT_APPLICABLE


def test_negative_tax_expense_normalized_but_source_sign_preserved():
    row = next(r for r in parse().rows if r.item_id == 'income_tax' and r.column == 'current')
    assert row.value == D('-35066') and row.normalized == D('35066000')


def test_positive_tax_benefit_does_not_become_expense():
    content = altered(lambda data, record, c: c['financialResult'].update(current2410=35066))
    report = parse(content)
    assert not any(r.item_id == 'income_tax' and r.column == 'current' for r in report.rows)
    benefit = next(r for r in report.rows if r.item_id == 'income_tax_benefit' and r.column == 'current')
    assert benefit.normalized == D('35066000')


def test_tax_semantics_change_between_sources_is_a_conflict(conn):
    store_report(conn, parse(), 'MVID')
    content = altered(lambda data, record, c: c['financialResult'].update(current2410=35066))
    result = store_report(conn, parse(content), 'MVID')
    assert any(x['item_id'] == 'income_tax_benefit' for x in result['conflicts'])
    rows = conn.execute("SELECT check_status FROM source_values WHERE item_id IN ('income_tax','income_tax_benefit') "
                        "AND period_end='2025-12-31'").fetchall()
    assert len(rows) == 2 and all(r[0] == 'расхождение' for r in rows)


def test_arithmetic_failure_marks_source_values_and_blocks_metrics(conn):
    content = altered(lambda data, record, c: c['balance'].update(current1600=45607238))
    report = parse(content)
    assert any(c.name == 'balance' and c.column == 'current' and D(c.delta) == 1 for c in report.checks)
    imported = store_report(conn, report, 'MVID')
    assert calculate(conn, imported, 'current_ratio').result.status is Status.INSUFFICIENT_DATA


def test_reimport_same_document_is_idempotent_and_records_unit_evidence(conn):
    report = parse()
    first = store_report(conn, report, 'MVID')
    second = store_report(conn, report, 'MVID')
    assert second['inserted'] == 0
    assert conn.execute('SELECT count(*) FROM source_values').fetchone()[0] == first['inserted']
    assert conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 1
    context = json.loads(conn.execute('SELECT value FROM meta WHERE key=?',
                                      (f'girbo_context:{first["document_id"]}',)).fetchone()[0])
    assert context['unit']['source'] == UNIT_SOURCE
    assert context['unit']['confirmed'] is True
    assert conn.execute('SELECT sha256 FROM documents').fetchone()[0] == SOURCE['sha256']


def test_incompatible_unit_of_existing_document_is_not_accepted(conn):
    report = parse()
    store_report(conn, report, 'MVID')
    with pytest.raises(ValueError, match='Единица'):
        store_report(conn, replace(report, unit=UnitEvidence('руб.', 'Другой контекст', True)), 'MVID')


def test_revised_value_preserves_both_sources_and_marks_conflict(conn):
    first = store_report(conn, parse(), 'MVID')
    content = altered(lambda data, record, c: c['balance'].update(current1200=33217460))
    second = store_report(conn, parse(content), 'MVID')
    assert second['document_id'] != first['document_id']
    assert any(x['item_id'] == 'current_assets' for x in second['conflicts'])
    rows = conn.execute("SELECT * FROM source_values WHERE item_id='current_assets' AND period_end='2025-12-31'").fetchall()
    assert len(rows) == 2
    assert {D(r['value_norm']) for r in rows} == {D('33217459000'), D('33217460000')}
    assert all(r['superseded_by'] is None and r['check_status'] == 'расхождение' for r in rows)
    assert calculate(conn, second, 'current_ratio').result.value is None


def test_same_values_in_different_exports_do_not_double_count(conn):
    store_report(conn, parse(), 'MVID')
    data = json.loads(CONTENT)
    data[0]['extra_export_note'] = 'новый экспорт, те же цифры'
    imported = store_report(conn, parse(json.dumps(data).encode()), 'MVID')
    assert not imported['conflicts']
    assert calculate(conn, imported, 'total_debt').result.value == D('32858688000')


def test_decimal_reading_does_not_use_float():
    # Изменённая копия: проверяем точный разбор числа, не достоверность этой подставленной суммы.
    content = altered(lambda data, record, c: c['balance'].update(current1250='PRECISE'))
    content = content.replace(b'"PRECISE"', b'1191.1234567890123456789')
    row = next(r for r in parse(content).rows if r.item_id == 'cash_and_equivalents' and r.column == 'current')
    assert row.value == D('1191.1234567890123456789')
    assert row.normalized == D('1191123.4567890123456789')


@pytest.mark.parametrize('kwargs', [dict(expected_inn='0000000000'), dict(expected_sha256='0'*64),
                                  dict(source_url='https://example.com/data'), dict(year=2026)])
def test_identity_checksum_and_year_errors_are_not_silently_accepted(kwargs):
    with pytest.raises(ValueError):
        parse(**kwargs)


def test_ambiguous_version_is_not_chosen_by_order():
    def change(data, record, c):
        record['typeCorrections'].append(copy.deepcopy(record['typeCorrections'][0]))
    with pytest.raises(ValueError, match='версия'):
        parse(altered(change))


def test_unpublished_newer_version_is_not_selected():
    def change(data, record, c):
        record['actualCorrectionNumber'] = 1
        newer = copy.deepcopy(record['typeCorrections'][0])
        newer['correction']['correctionVersion'] = 1
        newer['correction']['id'] = 999
        record['typeCorrections'].insert(0, newer)
    assert parse(altered(change)).correction_id == 57656029


@pytest.mark.parametrize('unit,source,confirmed', [('тыс. руб.', UNIT_SOURCE, False), ('тыс. руб.', '', True),
                                               ('долл.', UNIT_SOURCE, True)])
def test_units_are_never_guessed(unit, source, confirmed):
    with pytest.raises(ValueError):
        UnitEvidence(unit, source, confirmed)


def test_cli_import_and_calculation_in_isolated_db(tmp_path):
    path = tmp_path / 'verify.db'
    output = tmp_path / 'result.json'
    result = main([str(FOLDER/'mvideo_bfo_metadata.json'), '--db', str(path), '--ticker', 'MVID',
                   '--expected-inn', '7707602010', '--year', '2025', '--source-url', SOURCE['url'],
                   '--sha256', SOURCE['sha256'], '--unit', 'тыс. руб.', '--unit-source', UNIT_SOURCE,
                   '--confirm-unit', '--industry', 'retail', '--metric', 'net_debt', '--output', str(output)])
    assert result == 0
    protocol = json.loads(output.read_text())
    assert protocol['standard'] == 'RSBU' and protocol['scope'] == 'entity'
    assert D(protocol['metrics'][0]['value']) == D('32857497000')
    conn = db.init_db(path)
    assert conn.execute('SELECT count(*) FROM metric_inputs').fetchone()[0] >= 3
    conn.close()


@pytest.mark.parametrize('form,code', [('financialResult', '2120'), ('fundsMovement', '4120')])
def test_accounting_sign_of_expense_does_not_break_arithmetic_checks(form, code):
    def change(data, record, correction):
        correction[form]['current'+code] = -abs(correction[form]['current'+code])
    report = parse(altered(change))
    assert not any(c.status == 'расхождение' for c in report.checks)


def test_wrong_existing_company_identity_rolls_back_import(conn):
    conn.execute("INSERT INTO companies (ticker, name, inn) VALUES ('MVID', 'Другое юрлицо', '0000000000')")
    conn.commit()
    with pytest.raises(ValueError, match='ИНН'):
        with conn:
            store_report(conn, parse(), 'MVID')
    assert conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM source_values').fetchone()[0] == 0


def test_real_mts_signed_tax_reconciles_profit_without_unit_assumptions():
    from agent.sources.rsbu import _checks

    # Проверяем тождество в исходном масштабе JSON; единица МТС здесь не назначается и в БД не импортируется.
    reports = json.loads((FOLDER/'mts_bfo_metadata.json').read_bytes(), parse_float=D)
    record = next(r for r in reports if r['period'] == '2025')
    correction = record['typeCorrections'][0]['correction']
    assert correction['financialResult']['current2410'] > 0  # доход по налогу
    checks = [c for c in _checks(correction) if c.name == 'net_profit' and c.column in ('current','previous')]
    assert len(checks) == 2 and all(c.status == 'пройдено' for c in checks)


def test_old_adapter_version_is_not_silently_reused(conn):
    report = parse()
    imported = store_report(conn, report, 'MVID')
    key = f'girbo_context:{imported["document_id"]}'
    context = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0])
    context['adapter_version'] = 999
    conn.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(context), key))
    with pytest.raises(ValueError, match='Версия адаптера'):
        store_report(conn, report, 'MVID')
    assert conn.execute('SELECT count(*) FROM source_values').fetchone()[0] == imported['inserted']
