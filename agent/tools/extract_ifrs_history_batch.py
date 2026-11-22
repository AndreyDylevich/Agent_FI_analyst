"""Последовательное извлечение годовых балансов без параллельных запросов GigaChat."""

import argparse
import json
import sqlite3
from pathlib import Path
from requests import RequestException

from agent import config, db
from agent.tools.backfill_ifrs import MANIFEST
from agent.tools.extract_ifrs_history import main as extract_one


def run_batch(entries, output: Path, database: Path, *, archive: Path | None = None, runner=extract_one):
    if database.resolve()==db.DB_PATH.resolve():raise ValueError('Нужна отдельная проверочная БД истории')
    if output.exists() and any(output.iterdir()):raise ValueError('Нужен новый/пустой каталог истории')
    output.mkdir(parents=True,exist_ok=True)
    jobs=[]
    for entry in entries:
        folder=output/f"{entry['ticker']}_{entry['expected_year']}"
        command=['--ticker',entry['ticker'],'--year',str(entry['expected_year']),
                 '--db',str(database),'--output',str(folder)]
        if archive:command.extend(['--archive',str(archive)])
        if entry['ticker']=='MVID':command.extend(['--ocr','--max-ocr-pages','12'])
        try:
            code=runner(command)
            result=json.loads((folder/'history_extraction_check.json').read_text())
            jobs.append({'ticker':entry['ticker'],'year':entry['expected_year'],'exit_code':code,**result})
        except (ValueError,TypeError,KeyError,OSError,RuntimeError,RequestException) as error:
            jobs.append({'ticker':entry['ticker'],'year':entry['expected_year'],'exit_code':1,
                         'error':type(error).__name__ if isinstance(error,RequestException) else f'{type(error).__name__}: {error}'})
        # Сохраняем промежуточный прогресс, даже если следующий запрос завершится по таймауту.
        (output/'jobs.json').write_text(json.dumps(jobs,ensure_ascii=False,indent=2)+'\n')
    statuses={};count=0
    if database.is_file():
        conn=sqlite3.connect(f'{database.resolve().as_uri()}?mode=ro',uri=True)
        try:
            statuses=dict(conn.execute('SELECT check_status,count(*) FROM source_values GROUP BY check_status'))
            count=sum(statuses.values())
        finally:conn.close()
    report={'jobs':jobs,'source_values_count':count,'stored_statuses':statuses,
            'jobs_succeeded':sum(job['exit_code']==0 for job in jobs),
            'ok':bool(jobs) and count>0 and all(job['exit_code']==0 for job in jobs),
            'financial_history_validated':False,'comparability_approved':False,'messages_sent':0,
            'limitation':'Только выбранные итоги балансов. Источники и конфликты сохраняются совместно, но независимая сверка, другие формы и сопоставимость истории ещё не приняты.'}
    (output/'history_batch_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    tickers=[company['ticker'] for company in config.load('watchlist')['companies']]
    parser.add_argument('--ticker',choices=tickers,action='append')
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--from-year',type=int,default=2021)
    parser.add_argument('--through-year',type=int,default=2025)
    parser.add_argument('--archive',type=Path)
    args=parser.parse_args(argv)
    if args.from_year>args.through_year:parser.error('Некорректный диапазон лет')
    if args.db.resolve()==db.DB_PATH.resolve():parser.error('Нужна отдельная проверочная БД истории')
    entries=[entry for entry in json.loads(MANIFEST.read_text())['documents']
             if (not args.ticker or entry['ticker'] in args.ticker)
             and args.from_year<=entry['expected_year']<=args.through_year]
    report=run_batch(entries,args.output,args.db,archive=args.archive)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
