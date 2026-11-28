"""Оформить визуальную приёмку OCR-баланса в отдельной БД и пересчитать выбранные показатели."""

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

from agent import config, db
from agent.extract.ocr_review import apply_ocr_balance_review, reference_sha256
from agent.extract.ocr_source_review import apply_ocr_source_review
from agent.extract.pdf_text import read_pdf, PDFText, Page
from agent.extract.restatements import original_status
from agent.metrics.data import load_facts
from agent.metrics.engine import Engine, Period
from agent.metrics.registry import REGISTRY


def recalculate_reviewed(conn: sqlite3.Connection, review: dict, metric_ids: list[str], *, companies=None) -> list[dict]:
    """Создаёт новые результаты в строгом режиме; прежние, включая блокированные, сохраняются."""
    metrics = sorted(set(metric_ids))
    if not metrics or any(metric not in REGISTRY for metric in metrics):
        raise ValueError('Нужен непустой список известных показателей для пересчёта')
    saved_review = conn.execute('SELECT value FROM meta WHERE key=?',
                                (f"{'ifrs_source_review' if review['review']['version']==3 else 'ifrs_ocr_review'}:{review['review_sha256']}",)).fetchone()
    if not saved_review or json.loads(saved_review['value']) != review:
        raise ValueError('Нет неизменённого журнала ручной приёмки OCR')
    for change in review['changes']:
        row = conn.execute('SELECT * FROM source_values WHERE id=?', (change['source_id'],)).fetchone()
        if (not row or row['check_status'] != 'проверено_вручную' or row['superseded_by'] is not None
                or row['value'] != change['value'] or row['value_norm'] != change['value_norm']
                or row['unit'] != change['unit']):
            raise ValueError('Проверенный источник изменился или получил расхождение после приёмки')
        original_status(conn, row)
        location = json.loads(row['location'])
        if location['extraction_sha256'] != change['extraction_sha256'] or location['page'] != change['page']:
            raise ValueError('Привязка проверенной строки к исходному протоколу изменилась')
    # Новые источники/статусы требуют нового расчёта; прежние результаты не перезаписываются.
    snapshot = [dict(row) for row in conn.execute('SELECT * FROM source_values_current WHERE company_id=? ORDER BY id',
                                                 (review['company_id'],))]
    calculation_basis = {'metrics': {metric: REGISTRY[metric].version for metric in metrics}, 'sources': snapshot}
    key = f"{'ifrs_source_calculations' if review['review']['version']==3 else 'ifrs_ocr_calculations'}:{review['review_sha256']}:{reference_sha256(calculation_basis)}"
    previous = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    if previous:
        return json.loads(previous['value'])
    companies = companies or {company['ticker']: company for company in config.load('watchlist')['companies']}
    company = companies[review['review']['ticker']]
    facts = load_facts(conn, company_id=review['company_id'], standard='IFRS', scope='group',
                       segment_name=None, line_items=config.line_items())
    engine = Engine([fact for fact in facts if fact.check_status != 'не_проверено'],
                    company_id=review['company_id'], standard='IFRS', scope='group',
                    industry_id=company['industry']['id'], industries=config.industries(),
                    require_comparability=True)
    period = Period(date.fromisoformat(review['period_start']), date.fromisoformat(review['period_end']), review['period_type'])
    result = []
    for metric in metrics:
        calculated = engine.calculate(metric, period)
        saved_id = engine.save(conn, calculated)
        result.append({'metric_id': metric, 'period_end': str(period.end), 'saved_id': saved_id,
                       'status': calculated.result.status.value,
                       'value': str(calculated.result.value) if calculated.result.value is not None else None,
                       'explanation': calculated.result.explanation})
    conn.execute('INSERT INTO meta(key,value) VALUES (?,?)', (key, json.dumps(result, ensure_ascii=False, sort_keys=True)))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--metric', choices=sorted(REGISTRY), action='append', required=True)
    parser.add_argument('--prepared-pages',type=Path,help='Для решения v2: сохранённые OCR-страницы точного выпуска; проверяются по просмотренной структуре')
    args = parser.parse_args(argv)
    if args.db.resolve() == db.DB_PATH.resolve():
        parser.error('Для приёмки OCR нужна отдельная проверочная БД')
    if not args.db.is_file():
        parser.error('БД источников не существует')
    if args.output.exists():
        parser.error('Протокол уже существует; история не перезаписывается')
    report = {'ok': False, 'messages_sent': 0, 'comparability_approved': False, 'financial_audit_complete': False}
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        pdf = read_pdf(args.pdf)
        reference = json.loads(args.reference.read_text())
        decision = json.loads(args.review.read_text())
        if decision.get('version') == 2:
            if not args.prepared_pages:
                raise ValueError('Для многоформенной приёмки нужны исходные OCR-страницы')
            saved=json.loads(args.prepared_pages.read_text())
            if saved['sha256'] != pdf.sha256:
                raise ValueError('Сохранённые страницы относятся к другому PDF')
            pdf=PDFText(saved['sha256'],tuple(Page(**p) for p in saved['pages']),
                        tuple(saved.get('warnings',[])),saved.get('processing',{}))
        with conn:
            procedure=apply_ocr_source_review if decision.get('version') == 2 else apply_ocr_balance_review
            review = procedure(conn, pdf, reference, decision)
            metrics = recalculate_reviewed(conn, review, args.metric)
        report.update(ok=True, review=review, recalculated=metrics)
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        conn.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print('OCR-баланс: ' + ('визуальная приёмка оформлена' if report['ok'] else 'требуется проверка'), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
