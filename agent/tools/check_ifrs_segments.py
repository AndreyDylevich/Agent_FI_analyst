"""Изолированная приёмка исходных сегментных таблиц по независимым значениям."""

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from agent import config, db
from agent.extract.llm import GigaChatProvider
from agent.extract.pdf_text import read_pdf,PDFText,Page
from agent.extract.segments import CATALOG as SEGMENT_CATALOG
from agent.sources.files import checked_url,MAX_PDF_BYTES
from agent.extract.restatements import original_status
from agent.extract.segments import extract_segments, store_segments
from agent.metrics.engine import Period
from agent.tools.check_ifrs_case import main as prepare_case

REFERENCE=Path(__file__).resolve().parents[2]/'tests/fixtures/ifrs/segments_h1_2026_reference.json'


def review_absent_table(archive,prepared_pages,output):
    """A bounded narrative review creates neither a provider nor a numerical fact."""
    profiles=[p for p in json.loads(SEGMENT_CATALOG.read_text())['documents'] if p['ticker']=='MVID']
    if len(profiles)!=1 or profiles[0].get('quantitative_table_not_disclosed') is not True or profiles[0]['segments']:
        raise ValueError('Нужен единственный просмотренный профиль отсутствия отдельной числовой таблицы')
    profile=profiles[0]
    from agent.tools.check_ifrs_case import CATALOG
    cases=json.loads(CATALOG.read_text());reference=json.loads((CATALOG.parent/cases['mvid_balance']['reference']).read_text())
    if reference['pdf_sha256']!=profile['pdf_sha256']:raise ValueError('Эталон PDF и раскрытие относятся к разным версиям')
    company=next(c for c in config.load('watchlist')['companies'] if c['ticker']=='MVID')
    checked_url(reference['source_url'],{h for p in company['ir_pages'] for h in p['allowed_hosts']})
    original=archive/'pdf'/f"{profile['pdf_sha256']}.pdf"
    if not original.is_file() or original.stat().st_size>MAX_PDF_BYTES:
        raise ValueError('Нет допустимого оригинального PDF точного выпуска')
    content=original.read_bytes()
    if not content.startswith(b'%PDF-') or hashlib.sha256(content).hexdigest()!=profile['pdf_sha256']:
        raise ValueError('SHA256/сигнатура оригинального PDF не совпали')
    saved=json.loads(prepared_pages.read_text())
    if saved['sha256']!=profile['pdf_sha256']:raise ValueError('Подготовленные страницы относятся к другой версии PDF')
    pdf=PDFText(saved['sha256'],tuple(Page(**p) for p in saved['pages']),tuple(saved.get('warnings',[])),saved.get('processing',{}))
    source=profile['source_profile'];period=Period(date.fromisoformat(source['report_start']),date.fromisoformat(source['report_end']),source['report_kind'])
    result,finished=extract_segments(pdf,None,ticker='MVID',entity_token=company['document_identity_token'],period=period,output=output/'extraction')
    if finished or result['provider_requests']!=0 or not result['disclosure_reviewed'] or result['financial_values_verified']:
        raise ValueError('Проверка отсутствия таблицы не является извлечением числовых сегментов')
    (output/'pdf').mkdir();shutil.copy2(original,output/'pdf'/original.name)
    shutil.copy2(prepared_pages,output/'prepared_pages.json')
    return {'ok':True,'ticker':'MVID','pdf_sha256':pdf.sha256,'source_url':reference['source_url'],
            'source_pdf_origin':'verified_archive','source_pdf_downloaded_this_run':False,
            'provider_requests':0,'selected_values_count':0,'numeric_segment_acceptance':False,
            'numerical_database_created':False,'disclosure_check':result,'messages_sent':0,
            'financial_audit_complete':False,'comparability_approved':False}


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
    parser.add_argument('--ticker',choices=['MTSS','SGZH','MVID'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source-archive',type=Path)
    parser.add_argument('--prepared-pages',type=Path,help='Для М.Видео: сохранённые страницы точного выпуска с просмотренным OCR')
    args=parser.parse_args(argv)
    if args.ticker=='MVID' and (not args.source_archive or not args.prepared_pages):
        parser.error('Для М.Видео нужны --source-archive и --prepared-pages: отдельная числовая таблица не раскрыта')
    if args.output.exists() and any(args.output.iterdir()):parser.error('Нужен новый каталог приёмки')
    args.output.mkdir(parents=True,exist_ok=True)
    report={'ok':False,'ticker':args.ticker,'messages_sent':0,'financial_audit_complete':False,'comparability_approved':False}
    if args.ticker=='MVID':
        try:report=review_absent_table(args.source_archive,args.prepared_pages,args.output)
        except (ValueError,KeyError,TypeError,OSError) as error:report['error']=f'{type(error).__name__}: {error}'
        (args.output/'case_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print('MVID: раскрытие '+('проверено; отдельные числовые сегменты не созданы' if report['ok'] else 'требует проверки'),flush=True)
        return 0 if report['ok'] else 1
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
