"""Независимая приёмка выбранных значений по вручную сверенной МСФО МТС 1П2026."""

import argparse
import json
import sqlite3
from decimal import Decimal
from fractions import Fraction
from pathlib import Path


def verify(conn: sqlite3.Connection, reference: dict) -> dict:
    """Проверяет конкретный эталон; не означает проверки всех данных отчётности."""
    if 'rows' in reference:
        return verify_statements(conn,reference)
    results = []
    for end, values in reference['raw_values_decimal'].items():
        for item, expected in values.items():
            rows = conn.execute("SELECT s.*,d.sha256 FROM source_values s JOIN documents d ON d.id=s.document_id "
                                "WHERE s.item_id=? AND s.period_end=? AND s.standard='IFRS' AND s.scope='group'",
                                (item, end)).fetchall()
            expected_page = 5 if item in ('total_assets','non_current_assets','current_assets') else 6
            passed = bool(rows) and all(
                row['sha256'] == reference['pdf_sha256'] and row['unit'] == 'млн руб.'
                and row['check_status'] == 'проверено_авто' and row['period_start'] is None
                and Decimal(row['value']) == Decimal(expected)
                and Decimal(row['value_norm']) == Decimal(expected) * Decimal('1000000')
                and json.loads(row['location'])['page'] == expected_page for row in rows)
            results.append({'item_id': item, 'period_end': end, 'passed': passed})
    ratio = conn.execute("SELECT value,status FROM metric_values WHERE metric_id='current_ratio' AND period_end='2026-06-30' ORDER BY id DESC LIMIT 1").fetchone()
    expected_ratio = Fraction(reference['current_ratio_fraction']['numerator'], reference['current_ratio_fraction']['denominator'])
    ratio_passed = bool(ratio and ratio['status'] == 'рассчитан' and ratio['value'] is not None
                        and abs(Fraction(ratio['value']) - expected_ratio) <= Fraction(1,10**27))
    return {'all_selected_values_verified': all(r['passed'] for r in results), 'selected_values': results,
            'current_ratio_verified': ratio_passed, 'financial_audit_complete': False,
            'ok': all(r['passed'] for r in results) and ratio_passed}


def verify_statements(conn: sqlite3.Connection, reference: dict) -> dict:
    """Сверка выбранных строк/статусов нескольких форм; не изменяет статусы в БД."""
    if not reference.get('rows'):
        raise ValueError('Эталон должен содержать исходные значения; пустой набор не считается сверкой')
    results=[]
    for expected in reference['rows']:
        rows=conn.execute("SELECT s.*,d.sha256 FROM source_values s JOIN documents d ON d.id=s.document_id "
                          "WHERE s.item_id=? AND s.period_start IS ? AND s.period_end=? AND s.standard='IFRS' AND s.scope='group'",
                          (expected['item_id'],expected['start'],expected['end'])).fetchall()
        passed=bool(rows) and all(row['sha256']==reference['pdf_sha256'] and row['unit']==reference['unit']
                                and row['check_status']==expected['status'] and Decimal(row['value'])==Decimal(expected['value'])
                                and Decimal(row['value_norm'])==Decimal(expected.get('normalized_value',str(Decimal(expected['value'])*Decimal(reference['multiplier']))))
                                and json.loads(row['location'])['page'] in expected['pages'] for row in rows)
        results.append({'item_id':expected['item_id'],'period_end':expected['end'],'passed':passed})
    contexts=[json.loads(row[0]) for row in conn.execute("SELECT value FROM meta WHERE key LIKE 'ifrs_extraction:%'")]
    checks=[check for context in contexts for check in context['checks']]
    check_results={name:any(check['rule']==name and check['result']['status']=='пройдено' for check in checks)
                   for name in reference['checks_required']}
    metric_results={}
    for expected in reference.get('metrics',[]):
        row=conn.execute('SELECT value,status FROM metric_values WHERE metric_id=? AND period_end=? ORDER BY id DESC LIMIT 1',
                         (expected['metric_id'],expected['period_end'])).fetchone()
        fraction=Fraction(expected['numerator'],expected['denominator'])
        metric_results[expected['metric_id']]=bool(row and row['status']=='рассчитан' and row['value'] is not None
            and abs(Fraction(row['value'])-fraction)<=Fraction(1,10**27))
    return {'all_selected_values_verified':all(r['passed'] for r in results),'selected_values':results,
            'checks':check_results,'metrics':metric_results,'financial_audit_complete':False,
            'ok':all(r['passed'] for r in results) and all(check_results.values()) and all(metric_results.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    conn = sqlite3.connect(f'{args.db.resolve().as_uri()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = verify(conn, json.loads(args.reference.read_text()))
    finally:
        conn.close()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(f"Эталон МСФО: {'совпадает' if result['ok'] else 'есть несовпадения/пропуски'}", flush=True)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
