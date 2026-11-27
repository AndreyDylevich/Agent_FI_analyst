"""Сохранить адресное решение о сопоставимости в отдельной проверочной БД; без изменения сумм."""

import argparse
import json
import sqlite3
from pathlib import Path

from agent import db
from agent.extract.comparability_review import apply_comparability_review
from agent.extract.ocr_transcript import apply_reviewed_ocr_punctuation
from agent.extract.pdf_text import read_pdf


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--pdf',type=Path,action='append',required=True)
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--ocr',action='store_true')
    args=parser.parse_args(argv)
    if args.db.resolve()==db.DB_PATH.resolve():parser.error('Нужна отдельная проверочная БД')
    if not args.db.is_file():parser.error('БД исходных значений не существует')
    if args.output.exists():parser.error('Протокол уже существует; история не перезаписывается')
    report={'ok':False,'source_numbers_modified':False,'source_financial_statuses_modified':False,'messages_sent':0}
    conn=sqlite3.connect(args.db);conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        pdfs={}
        for path in args.pdf:
            pdf=read_pdf(path,use_ocr=args.ocr,max_ocr_pages=50)
            if args.ocr:pdf=apply_reviewed_ocr_punctuation(pdf)
            if pdf.sha256 in pdfs:raise ValueError('Одна версия PDF указана дважды')
            pdfs[pdf.sha256]=pdf
        review=json.loads(args.review.read_text())
        with conn:result=apply_comparability_review(conn,pdfs,review)
        report.update(ok=True,decision=result['decision'],review=result)
    except Exception as error:
        report['error']=f'{type(error).__name__}: {error}'
    finally:conn.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:stream.write(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('Решение о сопоставимости '+('сохранено' if report['ok'] else 'требует проверки'),flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
