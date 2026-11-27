"""Приёмка выбранных годовых балансов по независимым эталонам, без сети и изменения БД."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from decimal import Decimal, DecimalException
from pathlib import Path
from zoneinfo import ZoneInfo

from agent import db
from agent.checks.ifrs import RULES
from agent.checks.validate import reconcile
from agent.extract.restatements import original_status
from agent.metrics.data import UNIT_MULTIPLIERS
from agent.tools.backfill_ifrs import MANIFEST
from agent.tools.extract_ifrs_history import BALANCE_ITEMS
from agent.tools.verify_ifrs_reference import verify_statements

REFERENCES = Path(__file__).resolve().parents[2] / 'tests/fixtures/ifrs'
PREFIXES = {'MTSS': 'mts', 'SGZH': 'sgzh', 'MVID': 'mvid'}
BALANCE_RULES = ('assets_components', 'balance', 'balance_totals')


def verify_year(conn: sqlite3.Connection, entry: dict, reference: dict) -> dict:
    """Каждый итог и каждое тождество привязаны к своей версии PDF и обеим датам."""
    year = entry['expected_year']
    if (not reference.get('source_specific') or reference['pdf_sha256'] != entry['pdf_sha256']
            or reference['source_url'] != entry['source_url']):
        raise ValueError('Эталон не закреплён за выбранной версией годового PDF')
    expected_pairs = {(item, None, f'{end}-12-31') for item in BALANCE_ITEMS for end in (year, year - 1)}
    pairs = [(row['item_id'], row['start'], row['end']) for row in reference['rows']]
    if len(pairs) != 14 or set(pairs) != expected_pairs:
        raise ValueError('Нужны ровно 14 различных итогов баланса на две годовые даты')
    documents = conn.execute(
        'SELECT d.* FROM documents d JOIN companies c ON c.id=d.company_id '
        'WHERE c.ticker=? AND d.sha256=?', (entry['ticker'], entry['pdf_sha256'])).fetchall()
    if not documents or any(
            row['url'] != entry['source_url'] or row['standard'] != 'IFRS'
            or row['period_type'] != 'FY' or row['period_start'] != f'{year}-01-01'
            or row['period_end'] != f'{year}-12-31' for row in documents):
        raise ValueError('В БД нет нужного годового документа компании с подтверждённой версией')
    rows = conn.execute(
        'SELECT s.* FROM source_values s JOIN documents d ON d.id=s.document_id '
        'WHERE d.company_id=? AND d.sha256=?',
        (documents[0]['company_id'], entry['pdf_sha256'])).fetchall()
    selected = [row for row in rows if (row['item_id'], row['period_start'], row['period_end']) in expected_pairs]
    contexts = {}
    for row in selected:
        if (row['company_id'] != documents[0]['company_id'] or row['standard'] != 'IFRS'
                or row['scope'] != 'group' or row['segment_name'] is not None or row['currency'] != 'RUB'
                or row['cash_flow_section'] is not None):
            raise ValueError('Итог относится к другой компании, валюте или охвату')
        # Проверяем SHA256 протокола и исходные суммы, не повышая статусы OCR/пересчёта.
        original_status(conn, row)
        location = json.loads(row['location'])
        key = f"ifrs_extraction:{row['document_id']}:{location['extraction_sha256']}"
        if key not in contexts:
            payload = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()['value']
            contexts[key] = json.loads(payload)
        context = contexts[key]
        fact = context['facts'][location['fact_index']]
        if (context['pdf_sha256'] != entry['pdf_sha256'] or fact['statement'] != 'balance'
                or fact['evidence']['page'] != location['page']
                or fact['evidence']['quote'] != location['quote']):
            raise ValueError('Источник строки не совпадает с исходным протоколом извлечения')
    result = verify_statements(conn, reference)
    period_checks = []
    for end in (f'{year}-12-31', f'{year - 1}-12-31'):
        dated = [row for row in selected if row['period_end'] == end]
        for rule in BALANCE_RULES:
            terms = RULES[rule]
            values = {item: {Decimal(row['value_norm']) for row in dated if row['item_id'] == item}
                      for item in terms}
            tolerance = max((UNIT_MULTIPLIERS[row['unit']] for row in dated), default=Decimal(0))
            arithmetic = reconcile(rule, {item: next(iter(numbers)) if len(numbers) == 1 else None
                                          for item, numbers in values.items()}, terms, tolerance=tolerance)
            # Другой год, другая версия PDF или неполное тождество не заменяют эту проверку.
            saved = [check for context in contexts.values() for check in context['checks']
                     if check['rule'] == rule and check['start'] is None and check['end'] == end]
            passed = bool(saved) and arithmetic.status == 'пройдено' and all(
                check['result']['status'] == 'пройдено'
                and Decimal(check['result']['delta']) == arithmetic.delta
                and Decimal(check['result']['tolerance']) == tolerance for check in saved)
            period_checks.append({'rule': rule, 'period_end': end, 'passed': passed,
                                  'delta': str(arithmetic.delta), 'tolerance': str(tolerance)})
    return {**result, 'year': year, 'pdf_sha256': entry['pdf_sha256'],
            'reference_sha256': hashlib.sha256(json.dumps(reference, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            'period_checks': period_checks, 'source_values_count': len(selected),
            'stored_statuses': dict(Counter(row['check_status'] for row in selected)),
            'superseded_values_count': sum(row['superseded_by'] is not None for row in selected),
            'restated_values_count': sum(bool(row['is_restated']) for row in selected),
            'ok': result['ok'] and all(check['passed'] for check in period_checks)}


def verify_history(conn: sqlite3.Connection, ticker: str, entries: list[dict], references: dict[int, dict], *,
                   first_year: int = 2021, last_year: int = 2025, years: list[int] | None = None) -> dict:
    """Полнота выбранных балансов отделена от полноты архива и приёмки всей истории."""
    requested=sorted(set(years)) if years is not None else list(range(first_year,last_year+1))
    if not requested or any(y<first_year or y>last_year for y in requested):
        raise ValueError('Некорректный список выбранных лет')
    selected = [entry for entry in entries if entry['ticker'] == ticker
                and entry['expected_year'] in requested]
    counts = Counter(entry['expected_year'] for entry in selected)
    if first_year > last_year or any(count != 1 for count in counts.values()):
        raise ValueError('Некорректный диапазон либо неоднозначный год в манифесте')
    results = []
    for entry in sorted(selected, key=lambda item: item['expected_year']):
        year = entry['expected_year']
        try:
            if year not in references:
                raise ValueError('Нет независимого эталона выбранного года')
            result = verify_year(conn, entry, references[year])
        except (ValueError, KeyError, TypeError, IndexError, DecimalException, sqlite3.Error) as error:
            result = {'year': year, 'ok': False, 'error': f'{type(error).__name__}: {error}'}
        results.append(result)
    ok = bool(results) and all(result['ok'] for result in results)
    missing = sorted(set(requested) - set(counts))
    return {'checked_date_moscow': datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat(),
            'ticker': ticker, 'years': results, 'unavailable_report_years': missing,
            'requested_report_years': requested,
            'requested_window_complete': bool(results) and not missing,
            'selected_balance_history_verified': ok, 'ok': ok,
            'source_values_count': sum(result.get('source_values_count', 0) for result in results),
            'financial_history_validated': False, 'comparability_approved': False,
            'financial_audit_complete': False, 'messages_sent': 0,
            'limitation': 'Проверены только семь выбранных итогов на две даты каждого доступного годового PDF. '
                          'Отсутствующие выпуски не восстановлены из сравнительных колонок. '
                          'Статусы OCR, исходные конфликты и история пересчётов сохранены. '
                          'Другие формы, учётная база/периметр и сопоставимость требуют отдельной приёмки.'}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker', choices=sorted(PREFIXES), required=True)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--from-year', type=int, default=2021)
    parser.add_argument('--through-year', type=int, default=2025)
    parser.add_argument('--years',help='Явно выбранные годы через запятую')
    args = parser.parse_args(argv)
    try: years=sorted({int(s.strip()) for s in args.years.split(',')}) if args.years else None
    except ValueError: parser.error('Нужны целые годы через запятую')
    if years is not None and (not years or any(y<args.from_year or y>args.through_year for y in years)):
        parser.error('Выбранные годы вне диапазона')
    if args.db.resolve() == db.DB_PATH.resolve():
        parser.error('Для приёмки истории нужна отдельная проверочная БД')
    if args.output.exists():
        parser.error('Протокол уже существует; выберите новое имя')
    if args.from_year > args.through_year:
        parser.error('Некорректный диапазон лет')
    entries = json.loads(MANIFEST.read_text())['documents']
    references = {}
    for year in range(args.from_year, args.through_year + 1):
        path = REFERENCES / f'{PREFIXES[args.ticker]}_fy{year}_balance_reference.json'
        if path.is_file():
            references[year] = json.loads(path.read_text())
    conn = sqlite3.connect(f'{args.db.resolve().as_uri()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = verify_history(conn, args.ticker, entries, references,
                                first_year=args.from_year, last_year=args.through_year, years=years)
    finally:
        conn.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"{args.ticker}: выбранные балансы {'подтверждены' if report['ok'] else 'требуют проверки'}", flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
