"""Независимая приёмка выбранных значений по вручную сверенной МСФО МТС 1П2026."""

import argparse
import json
import sqlite3
from decimal import Decimal
from fractions import Fraction
from pathlib import Path


def verify(conn: sqlite3.Connection, reference: dict) -> dict:
    """Проверяет конкретный эталон; не означает проверки всех данных отчётности."""
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
