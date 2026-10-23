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
    assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"] == "1"


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
