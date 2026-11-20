"""Нормализация денежных значений и чтение актуальных исходных строк из SQLite."""

import sqlite3
from datetime import date
from decimal import Decimal

from agent.metrics.engine import Fact, Period

UNIT_MULTIPLIERS = {"руб.": Decimal("1"), "тыс. руб.": Decimal("1000"), "млн руб.": Decimal("1000000")}


def normalize_money(value: str, unit: str, sign: str) -> Decimal:
    """Переводит точную десятичную строку в рубли; расходы/оттоки хранятся положительными.

    Нулевая строка — явный ноль; пустота и неподдерживаемая единица являются ошибками.
    Прибыль/убыток и денежный поток сохраняют исходный знак.
    """
    if unit not in UNIT_MULTIPLIERS:
        raise ValueError(f"Неизвестная денежная единица: {unit}")
    if sign not in ("natural", "outflow"):
        raise ValueError(f"Неизвестное правило знака: {sign}")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("Значение должно быть конечным")
    # Рубли = исходное значение × множитель единицы; оттоки — по модулю.
    return (abs(number) if sign == "outflow" else number) * UNIT_MULTIPLIERS[unit]


def load_facts(
    conn: sqlite3.Connection, *, company_id: int, standard: str,
    scope: str, segment_name: str | None, line_items: dict,
) -> list[Fact]:
    """Читает только актуальные рублёвые статьи нужного стандарта и охвата.

    Пересчитанная история остаётся в БД, но superseded-строки не участвуют в новых расчётах.
    Расхождение сохранённой нормализации с исходной строкой — ошибка, а не молчаливый выбор.
    """
    rows = conn.execute(
        "SELECT * FROM source_values_current WHERE company_id = ? AND standard = ? "
        "AND scope = ? AND segment_name IS ? AND currency = 'RUB' ORDER BY id",
        (company_id, standard, scope, segment_name),
    ).fetchall()
    result = []
    for row in rows:
        if row["item_id"] not in line_items:
            continue  # операционные/натуральные показатели подключаются отдельно, на этапе 5
        item = line_items[row["item_id"]]
        section = row['cash_flow_section'] if 'cash_flow_section' in row.keys() else None
        if section is not None and item['statement'] != 'cash_flow':
            raise ValueError(f"source_values.id={row['id']}: раздел ОДДС задан не для денежного потока")
        normalized = normalize_money(row["value"], row["unit"], item["sign"])
        if row["value_norm"] is not None and Decimal(row["value_norm"]) != normalized:
            raise ValueError(f"source_values.id={row['id']}: нормализованное значение не совпадает с исходным")
        start = date.fromisoformat(row["period_start"]) if row["period_start"] else None
        end = date.fromisoformat(row["period_end"])
        if (item["statement"] == "balance") != (start is None):
            raise ValueError(f"source_values.id={row['id']}: остаток/поток не соответствует периоду")
        if start is not None:
            Period(start, end, row["period_type"])
        result.append(Fact(
            source_id=row["id"], company_id=row["company_id"], item_id=row["item_id"], value=normalized,
            start=start, end=end, period_type=row["period_type"], standard=standard, scope=scope,
            segment_name=segment_name, currency=row["currency"], check_status=row["check_status"],
            cash_flow_section=section,
            document_id=row['document_id'],
        ))
    return result
