"""Адаптер сохранённого JSON ГИР БО: кодированные формы РСБУ → исходные значения SQLite.

Сети и распознавания PDF здесь нет. Единица задаётся явно вместе с основанием подтверждения.
JSON читается через Decimal, отсутствующие статьи не превращаются в нули.
"""

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from agent.metrics.data import UNIT_MULTIPLIERS, normalize_money

FORMS = {'balance': ('balance', '0710001'), 'income': ('financialResult', '0710002'),
         'cash_flow': ('fundsMovement', '0710005')}
COLUMNS = {'current': 0, 'previous': -1, 'beforePrevious': -2}
ADAPTER_VERSION = 1  # изменение нормализации/правил проверок требует новой версии импорта


@dataclass(frozen=True)
class UnitEvidence:
    """Подтверждённая единица денежной отчётности и источник подтверждения (страница/раздел)."""

    unit: str
    source: str
    confirmed: bool = False
    currency: str = 'RUB'

    def __post_init__(self) -> None:
        if self.unit not in UNIT_MULTIPLIERS or self.currency != 'RUB':
            raise ValueError('Нужна поддерживаемая денежная единица и валюта RUB')
        if self.confirmed is not True or not self.source.strip():
            raise ValueError('Единица должна быть явно подтверждена с указанием источника')


@dataclass(frozen=True)
class Check:
    """Результат точной арифметической проверки исходной формы в её единице измерения."""

    name: str
    form: str
    column: str
    status: str
    delta: str | None = None
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class Row:
    """Строка с исходным знаком, нормализованным значением и точным путём к JSON-полю."""

    item_id: str
    field: str
    form: str
    column: str
    value: Decimal
    normalized: Decimal
    period_start: str | None
    period_end: str
    location: str
    check_status: str
    comparison_items: tuple[str, ...]
    cash_flow_section: str | None = None


@dataclass(frozen=True)
class Report:
    """Одна опубликованная годовая версия юрлица со сравнительными значениями из той же версии."""

    inn: str
    name: str
    year: int
    bfo_id: int
    correction_id: int
    source_url: str
    sha256: str
    published_at: str | None
    unit: UnitEvidence
    rows: tuple[Row, ...]
    checks: tuple[Check, ...]


def _number(value: Any) -> Decimal:
    """Отбрасывает float и bool; принимает только точные JSON-числа и десятичные строки."""
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        raise ValueError('Финансовое значение должно быть точным числом, не float/bool')
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError('Некорректное десятичное значение') from error
    if not result.is_finite():
        raise ValueError('Финансовое значение должно быть конечным')
    return result


def _checks(correction: dict) -> tuple[Check, ...]:
    """Проверяет тождества только при наличии каждого входа, без подстановки нулей."""
    equations = [
        ('balance', 'balance', {'1600': 1, '1700': -1}),
        ('assets', 'balance', {'1100': 1, '1200': 1, '1600': -1}),
        ('liabilities', 'balance', {'1300': 1, '1400': 1, '1500': 1, '1700': -1}),
        ('gross_profit', 'financialResult', {'2110': 1, '2120': -1, '2100': -1}),
        ('net_profit', 'financialResult', {'2300': 1, '2410': 1, '2460': 1, '2400': -1}),
        ('cfo', 'fundsMovement', {'4110': 1, '4120': -1, '4100': -1}),
        ('cfi', 'fundsMovement', {'4210': 1, '4220': -1, '4200': -1}),
        ('cff', 'fundsMovement', {'4310': 1, '4320': -1, '4300': -1}),
        ('cash_rollforward', 'fundsMovement', {'4450': 1, '4400': 1, '4490': 1, '4500': -1}),
    ]
    result = []
    for column in COLUMNS:
        for name, form, terms in equations:
            fields = correction.get(form) or {}
            missing = tuple(column + code for code in terms if fields.get(column + code) is None)
            if missing:
                result.append(Check(name, form, column, 'недостаточно_данных', missing=missing))
                continue
            # Проверка: сумма слагаемых с указанными знаками = 0, в единице отчётности.
            outflows = {'2120', '4120', '4220', '4320'}
            delta = sum(((abs(_number(fields[column + code])) if code in outflows
                          else _number(fields[column + code])) * sign
                         for code, sign in terms.items()), Decimal('0'))
            result.append(Check(name, form, column, 'пройдено' if delta == 0 else 'расхождение', str(delta)))
    return tuple(result)


def parse_snapshot(
    content: bytes, *, year: int, expected_inn: str, source_url: str,
    unit: UnitEvidence, line_items: dict, expected_sha256: str | None = None,
) -> Report:
    """Выбирает подтверждённое юрлицо и опубликованную годовую версию, преобразует известные коды."""
    parsed_url = urlparse(source_url)
    if (parsed_url.scheme != 'https' or parsed_url.netloc != 'bo.nalog.gov.ru'
            or not re.fullmatch(r'/nbo/organizations/\d+/bfo/', parsed_url.path)
            or parsed_url.query or parsed_url.fragment):
        raise ValueError('Нужен официальный URL исходного JSON ГИР БО')
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError('Контрольная сумма JSON не совпадает с источником')
    if not re.fullmatch(r'\d{10}', expected_inn):
        raise ValueError('Нужен явно выбранный десятизначный ИНН юрлица')
    data = json.loads(content, parse_float=Decimal)
    if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
        raise ValueError('Нужен список наборов БФО')
    records = [r for r in data if str(r.get('period')) == str(year) and r.get('published') is True]
    if len(records) != 1:
        raise ValueError('Опубликованный набор БФО за год не определён однозначно')
    record = records[0]
    identity = record.get('organizationInfo') or {}
    if identity.get('inn') != expected_inn:
        raise ValueError('ИНН в отчётности не совпадает с выбранным юрлицом')
    if record.get('isCb') or record.get('knd') != '0710099':
        raise ValueError('Адаптер поддерживает полную годовую РСБУ нефинансового юрлица (КНД 0710099)')
    version = record.get('publishedCorrectionNumber')
    versions = [x['correction'] for x in record.get('typeCorrections', [])
                if x.get('type') == 12 and isinstance(x.get('correction'), dict)
                and version is not None and x['correction'].get('correctionVersion') == version]
    if len(versions) != 1:
        raise ValueError('Опубликованная годовая версия БФО не определена однозначно')
    correction = versions[0]
    if ((correction.get('bfoOrganizationInfo') or {}).get('inn') != expected_inn
            or correction.get('periodType') != 12):
        raise ValueError('Юрлицо или период внутри версии БФО не совпадают')
    if type(record.get('id')) is not int or type(correction.get('id')) is not int:
        raise ValueError('Неверные идентификаторы набора/версии БФО')
    for form, okud in FORMS.values():
        if correction.get(form) is not None and correction[form].get('okud') != okud:
            raise ValueError(f'Неизвестный вид формы: {form}')
    checks = _checks(correction)
    rows = []
    for item_id, item in line_items.items():
        form, _ = FORMS[item['statement']]
        fields = correction.get(form) or {}
        for code in item.get('rsbu_codes', []):
            for column, offset in COLUMNS.items():
                field = column + code
                if fields.get(field) is None:
                    continue
                value = _number(fields[field])
                canonical_id = item.get('rsbu_positive_item', item_id) if value > 0 else item_id
                definition = line_items[canonical_id]
                applicable_checks = [c for c in checks if c.form == form and c.column == column]
                status = ('расхождение' if any(c.status == 'расхождение' for c in applicable_checks)
                          else 'проверено_авто' if any(c.status == 'пройдено' for c in applicable_checks)
                          else 'не_проверено')
                end = date(year + offset, 12, 31).isoformat()
                location = f'bfo={record["id"]}; correction={correction["id"]}; {form}.{field}'
                rows.append(Row(
                    canonical_id, field, form, column, value,
                    normalize_money(str(value), unit.unit, definition['sign']),
                    None if item['statement'] == 'balance' else date(year + offset, 1, 1).isoformat(),
                    end, location, status,
                    tuple(dict.fromkeys((item_id, item.get('rsbu_positive_item', item_id)))),
                    item.get('rsbu_cash_flow_section'),
                ))
    return Report(expected_inn, identity.get('fullName') or expected_inn, year, record['id'], correction['id'],
                  source_url, digest, record.get('publishedCorrectionDate'), unit, tuple(rows), checks)


def store_report(conn: sqlite3.Connection, report: Report, ticker: str) -> dict:
    """Добавляет источники, не изменяя watchlist. Транзакцией управляет вызывающий код.

    Повтор того же импорта не дублирует строки. Расхождения с другими документами сохраняются,
    оба источника помечаются; автоматической подмены прошлых значений нет.
    """
    company = conn.execute('SELECT * FROM companies WHERE ticker=?', (ticker,)).fetchone()
    if company is None:
        cursor = conn.execute('INSERT INTO companies (ticker, name, inn) VALUES (?, ?, ?)',
                              (ticker, report.name, report.inn))
        company_id = cursor.lastrowid
    else:
        if company['inn'] != report.inn:
            raise ValueError('Компания в БД не имеет подтверждённого совпадающего ИНН')
        company_id = company['id']
    url = f'{report.source_url}#bfo={report.bfo_id}&correction={report.correction_id}'
    document = conn.execute('SELECT * FROM documents WHERE url=? AND sha256=?', (url, report.sha256)).fetchone()
    context = {'adapter_version': ADAPTER_VERSION, 'unit': asdict(report.unit), 'checks': [asdict(c) for c in report.checks],
               'note': 'Проверена доступная арифметика форм; это не аудит всех статей и не анализ группы.'}
    if document:
        if document['company_id'] != company_id:
            raise ValueError('Документ уже связан с другой компанией в БД')
        stored = conn.execute('SELECT value FROM meta WHERE key=?', (f'girbo_context:{document["id"]}',)).fetchone()
        if stored is None or json.loads(stored['value'])['unit'] != context['unit']:
            raise ValueError('Единица/основание импорта отличаются от ранее сохранённых')
        if json.loads(stored['value']).get('adapter_version') != ADAPTER_VERSION:
            raise ValueError('Версия адаптера отличается; требуется отдельная повторная проверка источника')
        return {'company_id': company_id, 'document_id': document['id'], 'inserted': 0, 'conflicts': []}
    cursor = conn.execute(
        'INSERT INTO documents (company_id, url, sha256, title, doc_type, standard, period_start, period_end, '
        "period_type, published_at, status, source) VALUES (?, ?, ?, ?, 'rsbu', 'RSBU', ?, ?, 'FY', ?, 'extracted', 'girbo')",
        (company_id, url, report.sha256, f'{report.name}: РСБУ {report.year}', f'{report.year}-01-01',
         f'{report.year}-12-31', report.published_at))
    document_id = cursor.lastrowid
    conn.execute('INSERT INTO meta (key, value) VALUES (?, ?)',
                 (f'girbo_context:{document_id}', json.dumps(context, ensure_ascii=False)))
    conflicts = []
    for row in report.rows:
        placeholders = ','.join('?' for _ in row.comparison_items)
        previous = conn.execute(
            f"SELECT * FROM source_values_current WHERE company_id=? AND item_id IN ({placeholders}) AND standard='RSBU' "
            "AND scope='entity' AND segment_name IS NULL AND currency='RUB' AND period_start IS ? AND period_end=?",
            (company_id, *row.comparison_items, row.period_start, row.period_end)).fetchall()
        disagree = [r for r in previous if r['item_id'] != row.item_id or r['value_norm'] is None
                    or Decimal(r['value_norm']) != row.normalized]
        status = row.check_status
        if disagree:
            status = 'расхождение'
            conflicts.append({'item_id': row.item_id, 'period_end': row.period_end,
                              'previous_source_ids': [r['id'] for r in disagree]})
            conn.executemany("UPDATE source_values SET check_status='расхождение' WHERE id=?",
                             [(r['id'],) for r in previous])
        conn.execute(
            'INSERT INTO source_values (company_id, item_id, original_name, value, unit, value_norm, currency, '
            'period_start, period_end, period_type, standard, scope, document_id, source, location, '
            'extracted_by, check_status, cash_flow_section) '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FY', 'RSBU', 'entity', ?, 'girbo', ?, 'girbo', ?, ?)",
            (company_id, row.item_id, row.field, str(row.value), report.unit.unit, str(row.normalized),
             report.unit.currency, row.period_start, row.period_end, document_id, row.location, status,
             row.cash_flow_section))
    return {'company_id': company_id, 'document_id': document_id, 'inserted': len(report.rows), 'conflicts': conflicts}
