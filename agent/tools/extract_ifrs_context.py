"""Извлечь раскрытия МСФО в отдельный протокол/SQLite; без одобрения сопоставимости и Telegram."""

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

from agent import config, db
from agent.extract.disclosures import extract_disclosures
from agent.extract.llm import GigaChatProvider
from agent.extract.pdf_text import read_pdf
from agent.extract.schemas import source_context
from agent.metrics.engine import Period
from agent.sources.files import checked_url


def main(argv=None) -> int:
    companies={c['ticker']:c for c in config.load('watchlist')['companies']}
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf',type=Path)
    parser.add_argument('--ticker',choices=sorted(companies),required=True)
    parser.add_argument('--year',type=int,required=True)
    parser.add_argument('--period',choices=['FY','Q1','H1','9M'],required=True)
    parser.add_argument('--source-url',required=True)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--ocr',action='store_true')
    parser.add_argument('--max-ocr-pages',type=int,default=20)
    args=parser.parse_args(argv)
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('Нужен новый/пустой каталог: прежние ответы не перезаписываются')
    args.output.mkdir(parents=True,exist_ok=True)
    company=companies[args.ticker]
    hosts={host for page in company['ir_pages'] for host in page.get('allowed_hosts',[])}
    source=checked_url(args.source_url,hosts)
    month,day={'FY':(12,31),'Q1':(3,31),'H1':(6,30),'9M':(9,30)}[args.period]
    period=Period(date(args.year,1,1),date(args.year,month,day),args.period)
    report={'ok':False,'ticker':args.ticker,'source_url':source,'messages_sent':0,
            'comparability_approved':False,'financial_audit_complete':False}
    try:
        pdf=read_pdf(args.pdf,use_ocr=args.ocr,max_ocr_pages=args.max_ocr_pages)
        context=source_context(pdf.pages,ticker=args.ticker,period=period,
            entity_token=company.get('document_identity_token',company['legal_name']),line_items=config.line_items())
        if not context:
            raise ValueError('Не подтверждены компания, МСФО, групповой охват, RUB и период документа')
        with GigaChatProvider.from_env() as provider:
            disclosures=extract_disclosures(pdf,provider,output=args.output/'responses')
        report.update(ok=True,source_context=context,pdf_warnings=pdf.warnings,**disclosures)
        serialized=json.dumps(report,ensure_ascii=False,sort_keys=True)
        digest=hashlib.sha256(serialized.encode()).hexdigest()
        conn=db.init_db(args.db)
        try:
            with conn:
                conn.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)',
                             (f'ifrs_context:{args.ticker}:{pdf.sha256}:{digest}',serialized))
        finally:
            conn.close()
        report['context_sha256']=digest
    except Exception as error:
        report['error']=f'{type(error).__name__}: {error}'
    (args.output/'context_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f"{args.ticker}: раскрытия {'извлечены' if report['ok'] else 'требуют проверки'}; сопоставимость автоматически не одобрена",flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
