"""Загрузка сохранённого JSON РСБУ в явно указанную SQLite, без сети и изменения watchlist."""

import argparse
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from agent import config, db
from agent.metrics.data import load_facts
from agent.metrics.engine import Engine, Period
from agent.metrics.registry import REGISTRY, get
from agent.sources.rsbu import ADAPTER_VERSION, UnitEvidence, parse_snapshot, store_report


def main(argv: list[str] | None = None) -> int:
    """Импортирует выбранный год/юрлицо и сохраняет проверяемые результаты расчётов."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--ticker', required=True)
    parser.add_argument('--expected-inn', required=True)
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--sha256')
    parser.add_argument('--unit', choices=['руб.', 'тыс. руб.', 'млн руб.'], required=True)
    parser.add_argument('--unit-source', required=True, help='Документ и страница подтверждения единицы')
    parser.add_argument('--confirm-unit', action='store_true')
    parser.add_argument('--industry', choices=sorted(config.industries()), required=True)
    parser.add_argument('--metric', choices=sorted(REGISTRY), action='append')
    parser.add_argument('--output', type=Path, help='Отдельный JSON-протокол проверки')
    args = parser.parse_args(argv)
    try:
        unit = UnitEvidence(args.unit, args.unit_source, args.confirm_unit)
        report = parse_snapshot(args.snapshot.read_bytes(), year=args.year, expected_inn=args.expected_inn,
                                source_url=args.source_url, unit=unit, line_items=config.line_items(),
                                expected_sha256=args.sha256)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    conn = db.init_db(args.db)
    try:
        with conn:
            imported = store_report(conn, report, args.ticker.upper())
            facts = load_facts(conn, company_id=imported['company_id'], standard='RSBU', scope='entity',
                               segment_name=None, line_items=config.line_items())
            engine = Engine(facts, company_id=imported['company_id'], standard='RSBU', scope='entity',
                            industry_id=args.industry, industries=config.industries())
            metrics = args.metric or [m for m in REGISTRY if m in engine.allowed and 'RSBU' in get(m).standards]
            period = Period(date(args.year, 1, 1), date(args.year, 12, 31), 'FY')
            results = []
            print(f'{report.name}: РСБУ {report.year}, отдельное юрлицо, не группа', flush=True)
            for metric in metrics:
                calculation = engine.calculate(metric, period)
                result = calculation.result
                saved_id = engine.save(conn, calculation)
                print(result.explanation or f'{get(metric).name}: {result.status.value} — {result.reason}', flush=True)
                results.append({'metric_id': metric, 'status': result.status.value,
                                'value': str(result.value) if result.value is not None else None,
                                'unit': get(metric).unit, 'reason': result.reason, 'notes': result.notes,
                                'explanation': result.explanation, 'saved_id': saved_id})
            protocol = {'name': report.name, 'inn': report.inn, 'standard': 'RSBU', 'scope': 'entity',
                        'adapter_version': ADAPTER_VERSION,
                        'year': report.year, 'source_url': report.source_url, 'sha256': report.sha256,
                        'unit_context': asdict(report.unit), 'import': imported,
                        'checks': [asdict(c) for c in report.checks], 'metrics': results,
                        'limitation': 'Проверена арифметика JSON; это не аудит всех статей и не анализ группы.'}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(protocol, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    finally:
        conn.close()
    return 1 if imported['conflicts'] or any(c.status == 'расхождение' for c in report.checks) else 0


if __name__ == '__main__':
    raise SystemExit(main())
