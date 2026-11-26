"""Независимая приёмка выбранных значений по вручную сверенной МСФО МТС 1П2026."""

import argparse
import json
import sqlite3
from decimal import Decimal
from fractions import Fraction
from pathlib import Path


def _metric_row(conn, reference: dict, metric_id: str, end: str):
    """Расчёт другого юрлица/стандарта/сегмента не подтверждает эталон этого PDF."""
    companies = conn.execute('SELECT DISTINCT company_id FROM documents WHERE sha256=?',
                             (reference['pdf_sha256'],)).fetchall()
    if len(companies) != 1:
        return None
    return conn.execute(
        "SELECT value,status FROM metric_values WHERE company_id=? AND metric_id=? AND period_end=? "
        "AND standard='IFRS' AND scope='group' AND segment_name IS NULL ORDER BY id DESC LIMIT 1",
        (companies[0]['company_id'], metric_id, end)).fetchone()


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
    ratio = _metric_row(conn, reference, 'current_ratio', '2026-06-30')
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
        source_filter=' AND d.sha256=?' if reference.get('source_specific') else ''
        parameters=(expected['item_id'],expected['start'],expected['end'])
        if source_filter:parameters=(*parameters,reference['pdf_sha256'])
        rows=conn.execute("SELECT s.*,d.sha256 FROM source_values s JOIN documents d ON d.id=s.document_id "
                          "WHERE s.item_id=? AND s.period_start IS ? AND s.period_end=? AND s.standard='IFRS' AND s.scope='group'"+source_filter,
                          parameters).fetchall()
        passed=bool(rows) and all(row['sha256']==reference['pdf_sha256'] and row['unit']==reference['unit']
                                and row['check_status']==expected['status'] and Decimal(row['value'])==Decimal(expected['value'])
                                and Decimal(row['value_norm'])==Decimal(expected.get('normalized_value',str(Decimal(expected['value'])*Decimal(reference['multiplier']))))
                                and json.loads(row['location'])['page'] in expected['pages']
                                and ('cash_flow_section' not in expected or row['cash_flow_section']==expected['cash_flow_section']) for row in rows)
        results.append({'item_id':expected['item_id'],'period_end':expected['end'],'passed':passed})
    contexts=[json.loads(row[0]) for row in conn.execute("SELECT value FROM meta WHERE key LIKE 'ifrs_extraction:%'")]
    if reference.get('source_specific'):
        contexts=[context for context in contexts if context['pdf_sha256']==reference['pdf_sha256']]
    checks=[check for context in contexts for check in context['checks']]
    check_results={name:any(check['rule']==name and check['result']['status']=='пройдено' for check in checks)
                   for name in reference['checks_required']}
    expected_results=[]
    for expected in reference.get('checks_expected',[]):
        matches=[check for check in checks if check['rule']==expected['rule'] and check['start']==expected.get('start') and check['end']==expected['end']]
        passed=bool(matches) and all(check['result']['status']==expected['status'] and
            ('delta' not in expected or Decimal(check['result']['delta'])==Decimal(expected['delta'])) for check in matches)
        expected_results.append({**expected,'passed':passed})
    metric_results={}
    for expected in reference.get('metrics',[]):
        row=_metric_row(conn,reference,expected['metric_id'],expected['period_end'])
        fraction=Fraction(expected['numerator'],expected['denominator'])
        metric_results[expected['metric_id']]=bool(row and row['status']=='рассчитан' and row['value'] is not None
            and abs(Fraction(row['value'])-fraction)<=Fraction(1,10**27))
    for expected in reference.get('metric_states',[]):
        row=_metric_row(conn,reference,expected['metric_id'],expected['period_end'])
        metric_results[expected['metric_id']]=bool(row and row['status']==expected['status'] and row['value'] is None)
    return {'all_selected_values_verified':all(r['passed'] for r in results),'selected_values':results,
            'checks':check_results,'expected_checks':expected_results,'metrics':metric_results,'financial_audit_complete':False,
            'ok':all(r['passed'] for r in results) and all(check_results.values()) and all(metric_results.values()) and all(r['passed'] for r in expected_results)}


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
