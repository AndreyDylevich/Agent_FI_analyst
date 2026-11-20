"""Расчёт показателей по уже сохранённым исходным данным SQLite (без сети).

Пример:
    python -m agent.tools.calculate MTSS --year 2026 --period H1 --standard IFRS \
        --metric net_debt_to_ebitda

Без --save выводит результаты, не меняя БД. Загрузка отчётности подключается отдельно.
"""

import argparse
import sqlite3
from datetime import date
from pathlib import Path

from agent import config
from agent.db import DB_PATH
from agent.metrics.data import load_facts
from agent.metrics.engine import Engine, Period
from agent.metrics.registry import REGISTRY, get


def main(argv: list[str] | None = None) -> int:
    """Разбирает параметры, готовит расчётный контекст и выводит объяснения формул."""
    industries = config.industries()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('ticker', help='Тикер компании в companies')
    parser.add_argument('--db', type=Path, default=DB_PATH, help='Существующая база исходных значений')
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--period', choices=['FY', 'Q1', 'H1', '9M'], default='FY')
    parser.add_argument('--standard', choices=['IFRS', 'RSBU'], required=True)
    parser.add_argument('--industry', choices=sorted(industries), help='По умолчанию — из watchlist.yaml')
    parser.add_argument('--segment', help='Название сегмента; без параметра — группа МСФО или юрлицо РСБУ')
    parser.add_argument('--metric', choices=sorted(REGISTRY), action='append', help='Можно указать несколько раз')
    parser.add_argument('--save', action='store_true', help='Сохранить результаты и цепочку источников в БД')
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error(f'База данных не найдена: {args.db}')
    if args.standard == 'RSBU' and args.segment:
        parser.error('РСБУ анализируется в охвате отдельного юрлица, без --segment')
    if not 2 <= args.year <= 9999:
        parser.error('Год должен быть от 2 до 9999')
    watchlist = config.load('watchlist')['companies']
    company_config = next((c for c in watchlist if c['ticker'] == args.ticker.upper()), None)
    industry = args.industry or (company_config['industry']['id'] if company_config else None)
    if industry is None:
        parser.error('Компания отсутствует в watchlist.yaml: укажите --industry')
    month, day = {'FY': (12, 31), 'Q1': (3, 31), 'H1': (6, 30), '9M': (9, 30)}[args.period]
    period = Period(date(args.year, 1, 1), date(args.year, month, day), args.period)
    scope = 'entity' if args.standard == 'RSBU' else ('segment' if args.segment else 'group')
    mode = 'rw' if args.save else 'ro'
    conn = sqlite3.connect(f'{args.db.resolve().as_uri()}?mode={mode}', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        company = conn.execute('SELECT * FROM companies WHERE ticker = ?', (args.ticker.upper(),)).fetchone()
        if company is None:
            parser.error(f'Компания {args.ticker.upper()} отсутствует в таблице companies')
        facts = load_facts(conn, company_id=company['id'], standard=args.standard, scope=scope,
                           segment_name=args.segment, line_items=config.line_items())
        engine = Engine(facts, company_id=company['id'], standard=args.standard, scope=scope,
                        industry_id=industry, industries=industries, segment_name=args.segment)
        print(f"{company['name']} | {args.standard} | {period.start} — {period.end} | {scope}", flush=True)
        if company_config and not company_config['industry']['confirmed'] and not args.industry:
            print('Отрасль из watchlist.yaml ещё не подтверждена пользователем.', flush=True)
        metrics = args.metric or [m for m in REGISTRY if m in engine.allowed and args.standard in get(m).standards]
        with conn:
            for metric_id in metrics:
                calculation = engine.calculate(metric_id, period)
                r = calculation.result
                print(r.explanation or f'{get(metric_id).name}: {r.status.value} — {r.reason}', flush=True)
                if calculation.period.kind == 'LTM':
                    print(f'  Период расчёта LTM: {calculation.period.start} — {calculation.period.end}', flush=True)
                for note in r.notes:
                    print(f'  Примечание: {note}', flush=True)
                if args.save:
                    saved_id = engine.save(conn, calculation)
                    print(f'  Сохранено: metric_values.id={saved_id}', flush=True)
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
