"""Оформить проверенное решение о пересчёте и пересчитать затронутые результаты, без удаления истории."""

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

from agent import config, db
from agent.extract.pdf_text import read_pdf
from agent.extract.restatements import apply_restatement_review
from agent.metrics.data import load_facts
from agent.metrics.engine import Engine, Period


def rebuild_affected(conn,result,*,companies=None):
    key=result['recalculation_meta_key']
    saved=conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    if saved:return json.loads(saved['value'])
    companies=companies or {c['ticker']:c for c in config.load('watchlist')['companies']}
    engines={};rebuilt=[];seen=set()
    for old in result['affected_calculations']:
        signature=tuple(old[field] for field in ('company_id','metric_id','period_start','period_end','period_type','standard','scope','segment_name'))
        if signature in seen:continue
        seen.add(signature)
        if old['standard']!='IFRS' or old['scope']!='group' or old['segment_name'] is not None:
            raise ValueError('Зависимый расчёт имеет другой стандарт/охват')
        company_id=old['company_id']
        if company_id not in engines:
            ticker=conn.execute('SELECT ticker FROM companies WHERE id=?',(company_id,)).fetchone()['ticker']
            company=companies[ticker]
            facts=load_facts(conn,company_id=company_id,standard='IFRS',scope='group',segment_name=None,line_items=config.line_items())
            engines[company_id]=Engine([fact for fact in facts if fact.check_status!='не_проверено'],
                company_id=company_id,standard='IFRS',scope='group',industry_id=company['industry']['id'],
                industries=config.industries(),require_comparability=True)
        end=date.fromisoformat(old['period_end']);start=date.fromisoformat(old['period_start']) if old['period_start'] else date(end.year,1,1)
        calculation=engines[company_id].calculate(old['metric_id'],Period(start,end,old['period_type']))
        new_id=engines[company_id].save(conn,calculation)
        rebuilt.append({'metric_id':old['metric_id'],'period_end':old['period_end'],
                        'new_metric_value_id':new_id,'status':calculation.result.status.value})
    conn.execute('INSERT INTO meta(key,value) VALUES (?,?)',(key,json.dumps(rebuilt,ensure_ascii=False,sort_keys=True)))
    return rebuilt


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.db.resolve()==db.DB_PATH.resolve():parser.error('Для приёмки пересчёта нужна отдельная проверочная БД')
    if not args.db.is_file():parser.error('БД источников не существует')
    if args.output.exists():parser.error('Протокол уже существует; история не перезаписывается')
    report={'ok':False,'financial_history_validated':False,'comparability_approved':False,'messages_sent':0}
    conn=sqlite3.connect(args.db);conn.row_factory=sqlite3.Row;conn.execute('PRAGMA foreign_keys=ON')
    try:
        pdf=read_pdf(args.pdf);review=json.loads(args.review.read_text())
        with conn:
            result=apply_restatement_review(conn,pdf,review)
            rebuilt=rebuild_affected(conn,result)
        report.update(ok=True,review=result,recalculated=rebuilt)
    except Exception as error:
        report['error']=f'{type(error).__name__}: {error}'
    finally:conn.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('Пересчёт: '+('оформлен, прежние числа и расчёты сохранены' if report['ok'] else 'требуется проверка'),flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
