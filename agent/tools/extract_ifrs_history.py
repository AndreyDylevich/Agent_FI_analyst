"""Извлечь один год из проверенного манифеста в отдельную БД; готовность истории не объявляется."""

import argparse
import json
import hashlib
import re
import shutil
from types import SimpleNamespace
from datetime import date
from dataclasses import asdict
from pathlib import Path
from requests import RequestException

from agent import config, db
from agent.classify import classify_document
from agent.checks.reviewed_tables import compile_reviewed_tables
from agent.extract.pdf_text import HEADINGS, read_pdf, find_sections
from agent.extract.schemas import source_context
from agent.extract.ocr_transcript import apply_reviewed_ocr_punctuation
from agent.metrics.engine import Period
from agent.sources.files import download_pdf, MAX_PDF_BYTES, checked_url
from agent.tools.backfill_ifrs import MANIFEST
from agent.tools.extract_ifrs import main as extract_main

BALANCE_ITEMS=('total_assets','non_current_assets','current_assets','equity_total',
               'non_current_liabilities','current_liabilities','total_equity_and_liabilities')
INCOME_CASH_ITEMS=('revenue','operating_profit','net_profit','profit_before_tax','income_tax','income_tax_benefit',
                   'd_and_a','cfo','cfi','cff','capex_ppe','capex_intangibles','capex_total_ppe_intangibles',
                   'cf_cash_opening','cf_cash_closing_gross','cf_cash_change','cf_cash_change_before_fx',
                   'cf_fx_effect','cf_starting_net_profit','cf_starting_operating_profit','cf_starting_profit_before_tax')
CORE_FLOW_ITEMS=('revenue','net_profit','cfo','cfi','cff')
FINANCING_ITEMS=('cash_and_equivalents','debt_long_term','debt_short_term',
                 'lease_liabilities_long','lease_liabilities_short','right_of_use_assets')
FINANCING_CATALOG=Path(__file__).resolve().parents[2]/'config/ifrs_history_financing_tables.json'


def main(argv=None):
    companies={company['ticker']:company for company in config.load('watchlist')['companies']}
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker',choices=sorted(companies),required=True)
    parser.add_argument('--year',type=int,required=True)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--sections',choices=list(HEADINGS),nargs='+',default=['balance'])
    parser.add_argument('--item',choices=sorted(config.line_items()),action='append')
    parser.add_argument('--ocr',action='store_true')
    parser.add_argument('--max-ocr-pages',type=int,default=12)
    parser.add_argument('--ocr-dpi',type=int,choices=[320,400],default=400)
    parser.add_argument('--reuse-from',type=Path,action='append')
    parser.add_argument('--cached-only',action='store_true')
    parser.add_argument('--archive',type=Path,help='Уже скачанный архив с pdf/<sha256>.pdf; файл обязательно сверяется')
    parser.add_argument('--forms-profile',choices=['balance','income_cash','balance_financing'])
    parser.add_argument('--prepare-text-only',action='store_true')
    args=parser.parse_args(argv)
    if args.forms_profile=='income_cash':
        if args.item or args.sections!=['balance']:parser.error('Профиль форм нельзя смешивать с --item/--sections')
        args.sections=['income','cash_flow'];args.item=list(INCOME_CASH_ITEMS)
        if args.ocr and args.max_ocr_pages==12:args.max_ocr_pages=20
    elif args.forms_profile=='balance_financing':
        if args.item or args.sections!=['balance']:parser.error('Профиль форм нельзя смешивать с --item/--sections')
        args.item=list(FINANCING_ITEMS)
    if args.db.resolve()==db.DB_PATH.resolve():parser.error('Для первичной истории нужна отдельная проверочная БД')
    if args.output.exists() and any(args.output.iterdir()):parser.error('Нужен новый/пустой каталог ответов')
    if args.cached_only and not args.reuse_from:parser.error('--cached-only требует --reuse-from')
    entries=[row for row in json.loads(MANIFEST.read_text())['documents']
             if row['ticker']==args.ticker and row['expected_year']==args.year]
    if len(entries)!=1:parser.error('В манифесте нет единственного отчёта для выбранной компании/года')
    args.output.mkdir(parents=True,exist_ok=True)
    entry=entries[0];company=companies[args.ticker]
    report={'ticker':args.ticker,'year':args.year,'ok':False,'messages_sent':0,
            'source_url':entry['source_url'],'financial_history_validated':False,
            'comparability_approved':False,'independent_reference_verified':False,
            'forms_profile':args.forms_profile or ('balance' if not args.item and args.sections==['balance'] else 'custom')}
    try:
        expected_sha=entry.get('pdf_sha256')
        if not isinstance(expected_sha,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha):
            raise ValueError('Для первой приёмки истории нужен закреплённый SHA256 PDF')
        source=next(page for page in company['ir_pages'] if page['url']==entry['source_page'])
        checked_url(entry['source_url'],set(source['allowed_hosts']))
        if args.archive:
            original=args.archive/'pdf'/f'{expected_sha}.pdf'
            if original.stat().st_size>MAX_PDF_BYTES or hashlib.sha256(original.read_bytes()).hexdigest()!=expected_sha:
                raise ValueError('SHA256/размер архивного PDF не соответствует закреплённой версии')
            target=args.output/'pdf'/original.name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(original,target)
            downloaded=SimpleNamespace(path=str(target),sha256=expected_sha)
            report['pdf_origin']='verified_archive'
        else:
            downloaded=download_pdf(entry['source_url'],args.output/'pdf',set(source['allowed_hosts']))
            report['pdf_origin']='official_download'
        if downloaded.sha256!=expected_sha:
            raise ValueError('Версия годового PDF изменилась; требуется отдельная сверка, новый источник не отправлен в модель')
        pdf=read_pdf(Path(downloaded.path),use_ocr=args.ocr,max_ocr_pages=args.max_ocr_pages,ocr_dpi=args.ocr_dpi)
        if pdf.sha256!=expected_sha:raise ValueError('SHA256 изменился при чтении PDF')
        title=classify_document(entry['discovery_hint'],pdf.pages[0].text,recognition='pdf_first_page')
        period=Period(date(args.year,1,1),date(args.year,12,31),'FY')
        if not (title.basis=='pdf_first_page' and title.doc_type=='ifrs' and title.standard=='IFRS'
                and title.period_type=='FY' and title.period_end==str(period.end) and not title.needs_review):
            raise ValueError('Годовой период/тип не подтверждён по PDF')
        context=source_context(pdf.pages,ticker=args.ticker,period=period,
            entity_token=company.get('document_identity_token',company['legal_name']),line_items=config.line_items())
        if not context:raise ValueError('Контекст компании/МСФО/группы/RUB/периода не подтверждён')
        report.update(pdf_sha256=pdf.sha256,source_context=context)
        if args.forms_profile in ('income_cash','balance_financing') and not args.prepare_text_only:
            if args.ocr:pdf=apply_reviewed_ocr_punctuation(pdf)
            reviewed=compile_reviewed_tables(pdf,period,json.loads(FINANCING_CATALOG.read_text())) if args.forms_profile=='balance_financing' else compile_reviewed_tables(pdf,period)
            if reviewed is None:
                raise ValueError('Для выбранного исторического профиля нужна просмотренная структура точной версии PDF')
            args.item=sorted({o['item_id'] for o in reviewed.observations.values() if o['statement'] in args.sections})
            if not set(FINANCING_ITEMS if args.forms_profile=='balance_financing' else CORE_FLOW_ITEMS)<=set(args.item):
                raise ValueError('Просмотренный профиль не содержит обязательных исходных статей')
            report['reviewed_structure_basis']=reviewed.basis
        if args.prepare_text_only:
            (args.output/'pages.json').write_text(json.dumps(asdict(pdf),ensure_ascii=False,indent=2)+'\n')
            (args.output/'selection.json').write_text(json.dumps(asdict(find_sections(pdf)),ensure_ascii=False,indent=2)+'\n')
            report.update(text_preparation_ok=True,provider_requests=0,financial_values_verified=False)
            (args.output/'history_extraction_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            print(f'{args.ticker} FY{args.year}: подготовлен текст; финансовая приёмка не выполнялась',flush=True)
            return 0
        command=[downloaded.path,'--ticker',args.ticker,'--year',str(args.year),'--period','FY',
                 '--source-url',entry['source_url'],'--db',str(args.db),'--output',str(args.output/'extraction'),
                 '--sections',*args.sections,'--metric','current_ratio','--compact-contract']
        if args.forms_profile=='income_cash':command.append('--reviewed-tables')
        elif args.forms_profile=='balance_financing':command.extend(['--reviewed-catalog',str(FINANCING_CATALOG)])
        for item in args.item or BALANCE_ITEMS:command.extend(['--item',item])
        if args.ocr:command.extend(['--ocr','--max-ocr-pages',str(args.max_ocr_pages),'--ocr-dpi',str(args.ocr_dpi)])
        for path in args.reuse_from or []:command.extend(['--reuse-from',str(path)])
        if args.cached_only:command.append('--cached-only')
        code=extract_main(command)
        extraction=json.loads((args.output/'extraction/extraction_check.json').read_text())
        mandatory_balance=not args.item and args.sections==['balance']
        missing_balance=[]
        if mandatory_balance:
            actual={(fact['item_id'],fact['end']) for fact in extraction.get('facts',[])}
            missing_balance=[{'item_id':item,'period_end':f'{year}-12-31'} for item in BALANCE_ITEMS
                             for year in (args.year,args.year-1) if (item,f'{year}-12-31') not in actual]
        report.update(ok=code==0,selected_values_count=len(extraction.get('facts',[])),
                      statuses=extraction.get('statuses',[]),extraction_error=extraction.get('error'),
                      financial_audit_complete=False,
                      limitation='Извлечение и тождества не подтверждают независимый эталон, полную историю и сопоставимость. Специальные основы подготовки, пересчёты и изменения периметра требуют отдельной проверки.')
        report['missing_balance_observations']=missing_balance
        if missing_balance:report['ok']=False
        if args.forms_profile=='income_cash':
            actual={(f['item_id'],f.get('start'),f['end']) for f in extraction.get('facts',[])}
            required=[{'item_id':item,'period_start':f'{year}-01-01','period_end':f'{year}-12-31'}
                      for item in CORE_FLOW_ITEMS for year in (args.year,args.year-1)
                      if (item,f'{year}-01-01',f'{year}-12-31') not in actual]
            report.update(missing_core_flow_observations=required,
                          missing_requested_observations=extraction.get('missing_observations',[]),
                          requested_observations_count=extraction.get('requested_observations_count'))
            if required:report['ok']=False
        elif args.forms_profile=='balance_financing':
            actual={(f['item_id'],f.get('start'),f['end']) for f in extraction.get('facts',[])}
            required=[{'item_id':o['item_id'],'period_start':o['period_start'],'period_end':o['period_end']}
                      for o in reviewed.observations.values() if not o.get('source_absent')
                      and (o['item_id'],o['period_start'],o['period_end']) not in actual]
            report.update(missing_financing_observations=required,
                          missing_requested_observations=extraction.get('missing_observations',[]),
                          requested_observations_count=extraction.get('requested_observations_count'))
            if required:report['ok']=False
    except (ValueError, TypeError, KeyError, OSError, StopIteration, RuntimeError, RequestException) as error:
        report['error']=type(error).__name__ if isinstance(error,RequestException) else f'{type(error).__name__}: {error}'
    (args.output/'history_extraction_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f"{args.ticker} FY{args.year}: извлечение {'выполнено' if report['ok'] else 'требует проверки'}; история ещё не принята",flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
