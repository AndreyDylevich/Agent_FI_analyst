"""Оформить независимую приёмку точности native-строк, не одобряя полноту и сопоставимость."""

import argparse
import json
import sqlite3
from pathlib import Path

from agent import db
from agent.extract.pdf_text import read_pdf
from agent.extract.ocr_source_review import apply_native_source_review
from agent.metrics.registry import REGISTRY
from agent.tools.review_ifrs_ocr import recalculate_reviewed


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True);parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--reference',type=Path,required=True);parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--metric',choices=sorted(REGISTRY),action='append',required=True)
    args=parser.parse_args(argv)
    if args.db.resolve()==db.DB_PATH.resolve():parser.error('Нужна отдельная проверочная БД')
    if not args.db.is_file():parser.error('БД исходных значений не существует')
    if args.output.exists():parser.error('Нужно новое имя протокола')
    report={'ok':False,'source_numbers_modified':False,'comparability_approved':False,'financial_audit_complete':False,'messages_sent':0}
    conn=sqlite3.connect(args.db);conn.row_factory=sqlite3.Row;conn.execute('PRAGMA foreign_keys=ON')
    try:
        pdf=read_pdf(args.pdf);reference=json.loads(args.reference.read_text());decision=json.loads(args.review.read_text())
        with conn:
            review=apply_native_source_review(conn,pdf,reference,decision)
            metrics=recalculate_reviewed(conn,review,args.metric)
        report.update(ok=True,review=review,recalculated=metrics)
    except Exception as error:report['error']=f'{type(error).__name__}: {error}'
    finally:conn.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:stream.write(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('Native-источники '+('приняты по независимому эталону' if report['ok'] else 'требуют проверки'),flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
