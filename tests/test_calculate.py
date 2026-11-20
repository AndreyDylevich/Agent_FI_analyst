"""Команда расчёта: запуск по SQLite без сети, запись только с --save."""

from decimal import Decimal

import pytest

from agent import db
from agent.tools.calculate import main


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'agent.db'
    conn = db.init_db(path)
    conn.execute("INSERT INTO companies (id, ticker, name) VALUES (1, 'TEST', 'Синтетический пример')")
    for item, value in [('current_assets', '1500'), ('current_liabilities', '1200')]:
        conn.execute(
            'INSERT INTO source_values (company_id, item_id, value, unit, currency, period_end, '
            'standard, scope, source, extracted_by, check_status) '
            "VALUES (1, ?, ?, 'млн руб.', 'RUB', '2025-12-31', 'RSBU', 'entity', 'girbo', 'manual', 'проверено_вручную')",
            (item, value))
    conn.commit()
    conn.close()
    return path


def test_read_only_calculation(database, capsys):
    before = database.read_bytes()
    assert main(['TEST', '--db', str(database), '--year', '2025', '--standard', 'RSBU',
                 '--industry', 'retail', '--metric', 'current_ratio']) == 0
    output = capsys.readouterr().out
    assert '1 500,0 / 1 200,0 = 1,25' in output
    assert 'Отчётность отдельного юрлица, не группы' in output
    assert database.read_bytes() == before


def test_explicit_save(database, capsys):
    main(['TEST', '--db', str(database), '--year', '2025', '--standard', 'RSBU',
          '--industry', 'retail', '--metric', 'current_ratio', '--save'])
    conn = db.init_db(database)
    assert conn.execute('SELECT value FROM metric_values').fetchone()[0] == '1.25'
    assert conn.execute('SELECT count(*) FROM metric_inputs').fetchone()[0] == 2
    conn.close()


def test_missing_database_does_not_create_empty_database(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(SystemExit):
        main(['TEST', '--db', str(path), '--year', '2025', '--standard', 'RSBU'])
    assert not path.exists()


def test_yoy_command_uses_historical_revenue_and_saves_both_sources(database, capsys):
    conn = db.init_db(database)
    for year, amount in [(2024, '100'), (2025, '125')]:
        conn.execute(
            'INSERT INTO source_values (company_id, item_id, value, unit, currency, period_start, '
            'period_end, period_type, standard, scope, source, extracted_by, check_status) '
            "VALUES (1, 'revenue', ?, 'млн руб.', 'RUB', ?, ?, 'FY', 'RSBU', 'entity', 'girbo', 'manual', 'проверено_вручную')",
            (amount, f'{year}-01-01', f'{year}-12-31'))
    conn.commit()
    conn.close()
    assert main(['TEST', '--db', str(database), '--year', '2025', '--standard', 'RSBU',
                 '--industry', 'retail', '--metric', 'revenue_yoy', '--save']) == 0
    output = capsys.readouterr().out
    assert '(125,0 / 100,0 − 1) × 100 = 25,00 %' in output
    assert '2024-01-01 — 2024-12-31 (FY)' in output
    conn = db.init_db(database)
    result = conn.execute('SELECT * FROM metric_values').fetchone()
    assert result['metric_id'] == 'revenue_yoy'
    assert result['value'] == '25.00'
    assert conn.execute('SELECT count(*) FROM metric_inputs').fetchone()[0] == 2
    conn.close()


def test_cagr_command_explains_wrong_period(database, capsys):
    main(['TEST', '--db', str(database), '--year', '2025', '--period', 'H1', '--standard', 'RSBU',
          '--industry', 'retail', '--metric', 'revenue_cagr_3y'])
    assert 'Допустимые периоды: FY; получен H1' in capsys.readouterr().out


def test_turnover_command_normalizes_expense_sign_and_saves_ltm(database, capsys):
    conn = db.init_db(database)
    conn.execute(
        'INSERT INTO source_values (company_id, item_id, value, unit, currency, period_start, '
        'period_end, period_type, standard, scope, source, extracted_by, check_status) '
        "VALUES (1, 'cost_of_sales', '-1200', 'млн руб.', 'RUB', '2025-01-01', '2025-12-31', "
        "'FY', 'RSBU', 'entity', 'girbo', 'manual', 'проверено_вручную')")
    for day, value in [('2024-12-31', '100'), ('2025-12-31', '140')]:
        conn.execute(
            'INSERT INTO source_values (company_id, item_id, value, unit, currency, period_end, '
            'standard, scope, source, extracted_by, check_status) '
            "VALUES (1, 'inventories', ?, 'млн руб.', 'RUB', ?, 'RSBU', 'entity', 'girbo', 'manual', 'проверено_вручную')",
            (value, day))
    conn.commit()
    conn.close()
    assert main(['TEST', '--db', str(database), '--year', '2025', '--standard', 'RSBU',
                 '--industry', 'retail', '--metric', 'inventory_days', '--save']) == 0
    output = capsys.readouterr().out
    assert '365 / 10,00 = 36,50 дн.' in output
    assert 'Период расчёта LTM: 2025-01-01 — 2025-12-31' in output
    conn = db.init_db(database)
    assert conn.execute('SELECT count(*) FROM metric_values').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM metric_inputs WHERE source_value_id IS NOT NULL').fetchone()[0] == 3
    conn.close()


@pytest.mark.parametrize('use_effective,expected', [(True, '12'), (False, '11.25')])
def test_roic_command_and_rate_provenance(database, capsys, use_effective, expected):
    conn = db.init_db(database)
    items = ['equity_total', 'debt_long_term', 'debt_short_term', 'lease_liabilities_long',
             'lease_liabilities_short', 'cash_and_equivalents']
    for year, equity in [(2024, '600'), (2025, '800')]:
        for item, value in zip(items, [equity, '200', '100', '80', '20', '100']):
            conn.execute(
                'INSERT INTO source_values (company_id, item_id, value, unit, currency, period_end, '
                'standard, scope, source, extracted_by, check_status) '
                "VALUES (1, ?, ?, 'млн руб.', 'RUB', ?, 'IFRS', 'group', 'ir_page', 'manual', 'проверено_вручную')",
                (item, value, f'{year}-12-31'))
    flows = [('operating_profit', '150')]
    if use_effective:
        flows += [('income_tax', '24'), ('profit_before_tax', '120')]
    for item, value in flows:
        conn.execute(
            'INSERT INTO source_values (company_id, item_id, value, unit, currency, period_start, '
            'period_end, period_type, standard, scope, source, extracted_by, check_status) '
            "VALUES (1, ?, ?, 'млн руб.', 'RUB', '2025-01-01', '2025-12-31', 'FY', 'IFRS', 'group', "
            "'ir_page', 'manual', 'проверено_вручную')", (item, value))
    conn.commit()
    conn.close()
    main(['TEST', '--db', str(database), '--year', '2025', '--standard', 'IFRS',
          '--industry', 'retail', '--metric', 'roic', '--save'])
    output = capsys.readouterr().out
    assert 'Рентабельность инвестированного капитала (ROIC)' in output
    assert ('Допущение: номинальная ставка' in output) == (not use_effective)
    conn = db.init_db(database)
    row = conn.execute("SELECT * FROM metric_values WHERE metric_id='roic'").fetchone()
    assert Decimal(row['value']) == Decimal(expected)
    conn.close()


@pytest.mark.parametrize('section,expected', [('operating', '50000000'), ('financing', '35000000'), (None, None)])
def test_fcf_command_classification_and_persistence(database, capsys, section, expected):
    conn = db.init_db(database)
    for item, value in [('cfo', '100'), ('capex_ppe', '-35'), ('capex_intangibles', '-5'),
                        ('interest_paid', '-15'), ('lease_principal_paid', '-10')]:
        conn.execute(
            'INSERT INTO source_values (company_id, item_id, value, unit, currency, period_start, '
            'period_end, period_type, standard, scope, source, extracted_by, check_status, cash_flow_section) '
            "VALUES (1, ?, ?, 'млн руб.', 'RUB', '2025-01-01', '2025-12-31', 'FY', 'IFRS', 'group', "
            "'ir_page', 'manual', 'проверено_вручную', ?)",
            (item, value, section if item == 'interest_paid' else None))
    conn.commit()
    conn.close()
    main(['TEST', '--db', str(database), '--year', '2025', '--standard', 'IFRS',
          '--industry', 'retail', '--metric', 'fcf_to_equity', '--save'])
    output = capsys.readouterr().out
    conn = db.init_db(database)
    result = conn.execute("SELECT * FROM metric_values WHERE metric_id='fcf_to_equity'").fetchone()
    assert result['value'] == expected
    if expected is None:
        assert result['status'] == 'недостаточно_данных'
        assert 'неизвестна классификация' in output
    else:
        assert result['status'] == 'рассчитан'
        assert 'FCF для акционеров (по методике проекта)' in output
    conn.close()
