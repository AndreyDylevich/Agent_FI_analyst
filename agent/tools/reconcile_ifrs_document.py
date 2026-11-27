"""Повторная общая сверка форм/примечаний одного PDF по сохранённым проверочным БД, без сети."""

import argparse
import hashlib
import json
import sqlite3
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent import db
from agent.checks.document import reconcile_document
from agent.extract.pdf_text import read_pdf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, action='append', required=True)
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--ticker', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if any(path.resolve() == db.DB_PATH.resolve() for path in args.db):
        parser.error('Для приёмки нужны отдельные проверочные БД')
    if len({path.resolve() for path in args.db}) != len(args.db):
        parser.error('Одна БД не должна повторяться в списке')
    if args.output.exists():
        parser.error('Протокол уже существует; история не перезаписывается')
    report = {'ok': False, 'source_integrity_verified': False, 'cross_form_complete': False, 'messages_sent': 0}
    try:
        pdf = read_pdf(args.pdf)
        with ExitStack() as stack:
            sources, hashes = [], []
            for index, path in enumerate(args.db, 1):
                conn = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)
                stack.callback(conn.close)
                conn.row_factory = sqlite3.Row
                conn.execute('BEGIN')
                label = f'database_{index}'
                sources.append((label, conn))
                hashes.append({'database': label, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            report = reconcile_document(sources, pdf, args.ticker)
            for record, path in zip(hashes, args.db):
                record['unchanged'] = record['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
            if not all(record['unchanged'] for record in hashes):
                raise ValueError('Проверочная БД изменилась внешним процессом во время сверки')
            report['source_databases'] = hashes
    except Exception as error:
        report.update(ok=False, source_integrity_verified=False, cross_form_complete=False,
                      error=f'{type(error).__name__}: {error}')
    report['checked_date_moscow'] = datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    print(f"{args.ticker}: полные сверки {'пройдены' if report['ok'] else 'требуют проверки/дополнения'}", flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
