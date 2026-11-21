"""Ручной мониторинг IR-источников без LLM, расчётов, Telegram и включения расписания."""

import argparse
import json
from pathlib import Path

from agent import config, db
from agent.sources.ir_pages import monitor


def main(argv: list[str] | None = None) -> int:
    """Состояние сохраняется в SQLite даже при частичном сбое; ошибки повторятся в следующем прогоне."""
    companies = config.load('watchlist')['companies']
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=db.DB_PATH)
    parser.add_argument('--files', type=Path, default=Path('data/documents'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ticker', choices=[c['ticker'] for c in companies], action='append')
    parser.add_argument('--recheck-known', action='store_true', help='Аудит замен PDF по прежним URL, включая базовую линию')
    parser.add_argument('--max-downloads', type=int, default=20)
    args = parser.parse_args(argv)
    if args.max_downloads < 1:
        parser.error('--max-downloads должен быть положительным')
    if args.ticker:
        companies = [c for c in companies if c['ticker'] in args.ticker]
    conn = db.init_db(args.db)
    try:
        report = monitor(conn, companies, args.files, recheck_known=args.recheck_known, max_downloads=args.max_downloads)
    finally:
        conn.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    for page in report['pages']:
        print(f"{page['ticker']}: {page['status']}, PDF: {page.get('links', '?')}, "
              f"базовая линия: {page.get('is_baseline', False)}, отложено: {page.get('deferred', 0)}", flush=True)
    print(f"Обработано файлов: {len(report['files'])}; результат: {'ok' if report['ok'] else 'требует внимания'}", flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
