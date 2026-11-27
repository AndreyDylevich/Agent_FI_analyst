"""Сохранить просмотренные раскрытия учётной базы в отдельной БД, не меняя финансовые строки."""

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

from agent import db
from agent.extract.document_policy import store_document_policy
from agent.extract.pdf_text import read_pdf,PDFText,Page
from agent.metrics.engine import Period


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True);parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--ticker',required=True);parser.add_argument('--year',type=int,required=True)
    parser.add_argument('--period',choices=['FY','Q1','H1','9M'],required=True)
    parser.add_argument('--prepared-pages',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.db.resolve()==db.DB_PATH.resolve():parser.error('Нужна отдельная проверочная БД')
    if not args.db.is_file():parser.error('БД исходных значений не существует')
    if args.output.exists():parser.error('Нужно новое имя протокола')
    conn=sqlite3.connect(args.db);conn.row_factory=sqlite3.Row;conn.execute('PRAGMA foreign_keys=ON')
    report={'ok':False,'source_numbers_modified':False,'source_financial_statuses_modified':False,'comparability_approved':False,'messages_sent':0}
    try:
        pdf=read_pdf(args.pdf)
        if args.prepared_pages:
            saved=json.loads(args.prepared_pages.read_text());sha=saved.get('sha256',saved.get('pdf_sha256'))
            if sha!=pdf.sha256:raise ValueError('Сохранённые страницы относятся к другому PDF')
            pdf=PDFText(sha,tuple(Page(**p) for p in saved['pages']),tuple(saved.get('warnings',[])),saved.get('processing',{}))
        month,day={'FY':(12,31),'Q1':(3,31),'H1':(6,30),'9M':(9,30)}[args.period]
        period=Period(date(args.year,1,1),date(args.year,month,day),args.period)
        before=[tuple(r) for r in conn.execute('SELECT * FROM source_values ORDER BY id')]
        with conn:
            result=store_document_policy(conn,pdf,args.ticker,period)
            if before!=[tuple(r) for r in conn.execute('SELECT * FROM source_values ORDER BY id')]:
                raise ValueError('Политика не должна менять исходные финансовые строки')
        report.update(ok=True,review=result,source_financial_rows_unchanged=True)
    except Exception as error:report['error']=f'{type(error).__name__}: {error}'
    finally:conn.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:stream.write(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('Раскрытия учётной базы '+('сохранены' if report['ok'] else 'требуют проверки'),flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
