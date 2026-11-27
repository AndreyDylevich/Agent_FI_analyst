"""Изолированная приёмка исходных сегментных таблиц по независимым значениям."""

import argparse
import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from agent import config, db
from agent.extract.llm import GigaChatProvider
from agent.extract.pdf_text import read_pdf
from agent.extract.restatements import original_status
from agent.extract.segments import extract_segments, store_segments
from agent.metrics.engine import Period
from agent.tools.check_ifrs_case import main as prepare_case

REFERENCE=Path(__file__).resolve().parents[2]/'tests/fixtures/ifrs/segments_h1_2026_reference.json'


def verify_segments(conn,ticker,reference,pdf_sha256):
    expected=[r for r in reference['references'] if r['ticker']==ticker]
    if not expected:
        raise ValueError('Нет независимого эталона выбранных сегментных чисел')
    rows=conn.execute('SELECT s.*,d.sha256 FROM source_values_current s JOIN documents d ON d.id=s.document_id '
                     'JOIN companies c ON c.id=s.company_id WHERE c.ticker=? AND d.sha256=?',
                     (ticker,pdf_sha256)).fetchall()
    result=[]
    for r in expected:
        found=[s for s in rows if (s['item_id'],s['scope'],s['segment_name'],s['period_start'],s['period_end'])==
               (r['item_id'],r['scope'],r['segment_name'],r['start'],r['end'])]
        valid=len(found)==1
        if valid:
            s=found[0];original=original_status(conn,s);location=json.loads(s['location'])
            valid=(s['standard']=='IFRS' and s['currency']=='RUB' and s['unit']=='млн руб.'
                   and Decimal(s['value'])==Decimal(r['value']) and Decimal(s['value_norm'])==Decimal(r['normalized_value'])
                   and s['check_status']==r['status'] and original==r['status']
                   and s['original_name']==r['original_name']
                   and all(location[k]==r['evidence'][k] for k in ('page','start_line','end_line')))
        result.append({'segment_key':r['segment_key'],'item_id':r['item_id'],'end':r['end'],'ok':valid})
    return {'ok':len(rows)==len(expected) and all(r['ok'] for r in result),
            'selected_values_count':len(expected),'actual_values_count':len(rows),'observations':result}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker',choices=['MTSS','SGZH'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source-archive',type=Path)
    args=parser.parse_args(argv)
    if args.output.exists() and any(args.output.iterdir()):parser.error('Нужен новый каталог приёмки')
    args.output.mkdir(parents=True,exist_ok=True)
    report={'ok':False,'ticker':args.ticker,'messages_sent':0,'financial_audit_complete':False,'comparability_approved':False}
    try:
        case='mts_balance' if args.ticker=='MTSS' else 'sgzh_balance'
        command=['--case',case,'--prepare-text-only','--output',str(args.output/'source')]
        if args.source_archive:command.extend(['--source-archive',str(args.source_archive)])
        if prepare_case(command):raise ValueError('Закреплённый источник не прошёл проверку')
        files=list((args.output/'source/pdf').glob('*.pdf'))
        if len(files)!=1:raise ValueError('Не найден единственный исходный PDF')
        pdf=read_pdf(files[0]);company=next(c for c in config.load('watchlist')['companies'] if c['ticker']==args.ticker)
        from agent.tools.check_ifrs_case import CATALOG
        case_meta=json.loads(CATALOG.read_text())[case];source=json.loads((CATALOG.parent/case_meta['reference']).read_text())['source_url']
        period=Period(date(2026,1,1),date(2026,6,30),'H1')
        with GigaChatProvider.from_env() as provider:
            result,finished=extract_segments(pdf,provider,ticker=args.ticker,
                entity_token=company.get('document_identity_token',company['legal_name']),period=period,output=args.output/'extraction')
        conn=db.init_db(args.output/'check.db')
        try:
            with conn:
                imported=store_segments(conn,result,finished,ticker=args.ticker,name=company['name'],source_url=source,period=period,local_path=str(files[0]))
            verified=verify_segments(conn,args.ticker,json.loads(REFERENCE.read_text()),pdf.sha256)
        finally:conn.close()
        report.update(ok=verified['ok'] and all(not r['conflicts'] for r in imported),pdf_sha256=pdf.sha256,
                      independent_reference_check=verified,checks=result['checks'],imported=imported)
    except Exception as error:
        report['error']=f'{type(error).__name__}: {error}'
    (args.output/'case_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n')
    print(f"{args.ticker}: сегментный эталон {'подтверждён' if report['ok'] else 'требует проверки'}",flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
