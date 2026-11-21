"""Схема БД: таблицы создаются, superseded_by выбирает актуальную строку."""

import sqlite3

import pytest

from agent.db import TABLES, current_source_values, init_db, supersede


@pytest.fixture
def conn(tmp_path):
    connection = init_db(tmp_path / "test.db")
    connection.execute("INSERT INTO companies (ticker, name) VALUES ('TEST', 'Тест')")
    yield connection
    connection.close()


def add_value(conn, value: str, item_id: str = "revenue", period_end: str = "2025-12-31") -> int:
    cur = conn.execute(
        "INSERT INTO source_values (company_id, item_id, value, unit, value_norm, period_start, period_end,"
        " period_type, standard, scope, source, extracted_by)"
        " VALUES (1, ?, ?, 'млн руб.', ?, '2025-01-01', ?, 'FY', 'IFRS', 'group', 'ir_page', 'manual')",
        (item_id, value, value + "000000", period_end),
    )
    return cur.lastrowid


def test_all_tables_created(tmp_path):
    conn = init_db(tmp_path / "test.db")
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(TABLES) <= names
    views = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert "source_values_current" in views
    assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"] == "3"


def test_init_db_is_idempotent(tmp_path):
    init_db(tmp_path / "test.db").close()
    conn = init_db(tmp_path / "test.db")
    assert conn.execute("SELECT count(*) FROM meta").fetchone()[0] == 1


def test_superseded_by_selects_current_row(conn):
    old_id = add_value(conn, "1000")
    new_id = add_value(conn, "1050")   # компания пересчитала сравнительные данные
    supersede(conn, old_id, new_id)

    rows = current_source_values(conn, 1, "revenue", "IFRS")
    assert [r["id"] for r in rows] == [new_id]
    assert rows[0]["value"] == "1050"
    # старая строка не удалена и ссылается на новую
    old = conn.execute("SELECT * FROM source_values WHERE id = ?", (old_id,)).fetchone()
    assert old["superseded_by"] == new_id


def test_supersede_rejects_different_values(conn):
    revenue_id = add_value(conn, "1000")
    other_id = add_value(conn, "1000", item_id="net_profit")
    with pytest.raises(ValueError):
        supersede(conn, revenue_id, other_id)


def test_supersede_twice_rejected(conn):
    a, b, c = add_value(conn, "1"), add_value(conn, "2"), add_value(conn, "3")
    supersede(conn, a, b)
    with pytest.raises(ValueError):
        supersede(conn, a, c)


def test_metric_values_requires_standard(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO metric_values (company_id, metric_id, formula_version, period_end, unit, status)"
            " VALUES (1, 'current_ratio', 1, '2025-12-31', 'x', 'рассчитан')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO metric_values (company_id, metric_id, formula_version, standard, period_end, unit, status)"
            " VALUES (1, 'current_ratio', 1, 'МСФО', '2025-12-31', 'x', 'рассчитан')"
        )


def test_check_constraints(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_values (company_id, item_id, value, unit, period_end, standard, scope, source,"
            " extracted_by, check_status) VALUES (1, 'revenue', '1', 'руб.', '2025-12-31', 'IFRS', 'group',"
            " 'ir_page', 'manual', 'ок')"
        )


def test_schema_v1_migration_preserves_sources_history_and_results(tmp_path):
    from pathlib import Path

    path = tmp_path / 'legacy.db'
    old = sqlite3.connect(path)
    old.row_factory = sqlite3.Row
    old.executescript((Path(__file__).parent / 'fixtures' / 'schema_v1.sql').read_text())
    old.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
    old.execute("INSERT INTO companies (id, ticker, name) VALUES (1, 'TEST', 'Тест миграции')")
    a = add_value(old, '15', item_id='interest_paid')
    b = add_value(old, '16', item_id='interest_paid')
    supersede(old, a, b)
    old.execute("INSERT INTO metric_values (company_id, metric_id, formula_version, standard, period_end, "
                "unit, value, status) VALUES (1, 'fcf_simple', 1, 'IFRS', '2025-12-31', 'руб.', '60000000', 'рассчитан')")
    old.execute("INSERT INTO metric_inputs (metric_value_id, input_name, period_rule, source_value_id) "
                "VALUES (1, 'interest_paid', 'FLOW', ?)", (b,))
    old.commit()
    original_sources = [dict(row) for row in old.execute('SELECT * FROM source_values ORDER BY id')]
    old.close()
    migrated = init_db(path)
    assert migrated.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '3'
    rows = migrated.execute('SELECT * FROM source_values ORDER BY id').fetchall()
    assert [dict(row) for row in rows] == [dict(row, cash_flow_section=None) for row in original_sources]
    assert rows[0]['superseded_by'] == b
    assert migrated.execute('SELECT value FROM metric_values').fetchone()[0] == '60000000'
    assert migrated.execute('SELECT source_value_id FROM metric_inputs').fetchone()[0] == b
    assert migrated.execute('SELECT id FROM source_values_current').fetchone()[0] == b
    migrated.close()
    again = init_db(path)
    assert [r['name'] for r in again.execute('PRAGMA table_info(source_values)')].count('cash_flow_section') == 1
    again.execute("UPDATE source_values SET cash_flow_section='operating' WHERE id=?", (b,))
    with pytest.raises(sqlite3.IntegrityError):
        again.execute("UPDATE source_values SET cash_flow_section='unknown' WHERE id=?", (b,))
    again.close()


def test_future_schema_version_is_not_overwritten(tmp_path):
    path = tmp_path / 'future.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
    conn.execute("INSERT INTO meta VALUES ('schema_version', '999')")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match='Неподдерживаемая версия'):
        init_db(path)
    assert path.read_bytes() == before
