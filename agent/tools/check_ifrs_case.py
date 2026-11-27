"""Изолированная приёмка реальной МСФО по независимому эталону, без основной БД и Telegram."""

import argparse
import json
import sqlite3
import hashlib
import shutil
from pathlib import Path

from agent import config
from agent.sources.files import download_pdf, checked_url, MAX_PDF_BYTES, Download
from agent.tools.extract_ifrs import main as extract_main
from agent.tools.verify_ifrs_reference import verify

ROOT=Path(__file__).resolve().parents[2]
CATALOG=ROOT/'tests/fixtures/ifrs/validation_cases.json'


def main(argv=None) -> int:
    cases=json.loads(CATALOG.read_text())
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=sorted(cases),required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reuse-from',type=Path,action='append')
    parser.add_argument('--cached-only',action='store_true')
    parser.add_argument('--source-archive',type=Path,help='Артефакт предыдущей загрузки: используется только PDF с точным SHA256 эталона')
    args=parser.parse_args(argv)
    if args.cached_only and not args.reuse_from:parser.error('--cached-only требует --reuse-from')
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('Нужен новый/пустой каталог проверки')
    args.output.mkdir(parents=True,exist_ok=True)
    case=cases[args.case]
    reference=json.loads((CATALOG.parent/case['reference']).read_text())
    company=next(c for c in config.load('watchlist')['companies'] if c['ticker']==case['ticker'])
    hosts={host for page in company['ir_pages'] for host in page['allowed_hosts']}
    report={'case':args.case,'ok':False,'messages_sent':0,'financial_audit_complete':False}
    try:
        checked_url(reference['source_url'],hosts)
        if args.source_archive:
            original=args.source_archive/'pdf'/f"{reference['pdf_sha256']}.pdf"
            if not original.is_file() or original.stat().st_size>MAX_PDF_BYTES:
                raise ValueError('В архиве нет допустимого закреплённого PDF')
            content=original.read_bytes()
            if not content.startswith(b'%PDF-') or hashlib.sha256(content).hexdigest()!=reference['pdf_sha256']:
                raise ValueError('SHA256/сигнатура архивного PDF не совпадают с эталоном')
            target=args.output/'pdf'/original.name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(original,target)
            pdf=Download(path=str(target),sha256=reference['pdf_sha256'])
            report.update(pdf_origin='previously_verified_archive',source_pdf_downloaded_this_run=False)
        else:
            pdf=download_pdf(reference['source_url'],args.output/'pdf',hosts)
            report.update(pdf_origin='official_download',source_pdf_downloaded_this_run=True)
        if pdf.sha256!=reference['pdf_sha256']:
            raise ValueError('SHA256 отчёта изменился: эталон относится к другой версии, требуется отдельная сверка')
        items=sorted({row['item_id'] for row in reference['rows']}) if 'rows' in reference else sorted(
            {item for values in reference['raw_values_decimal'].values() for item in values})
        db_path=args.output/'check.db'
        command=[pdf.path,'--ticker',case['ticker'],'--year',str(case['year']),'--period',case['period'],
                 '--source-url',reference['source_url'],'--db',str(db_path),'--output',str(args.output/'extraction'),
                 '--sections',*case['sections'],'--metric','current_ratio']
        for item in items:command.extend(['--item',item])
        if case.get('compact',True):command.append('--compact-contract')
        for path in args.reuse_from or []:command.extend(['--reuse-from',str(path)])
        if args.cached_only:command.append('--cached-only')
        if case.get('ocr'):command.extend(['--ocr','--max-ocr-pages',str(case.get('max_ocr_pages',50))])
        code=extract_main(command)
        extraction=json.loads((args.output/'extraction/extraction_check.json').read_text())
        expected_discrepancy=any(check['status']=='расхождение' for check in reference.get('checks_expected',[]))
        if code and not (expected_discrepancy and not extraction.get('error') and extraction.get('facts') and db_path.is_file()):
            raise ValueError('Извлечение не прошло проверку; исходные ответы сохранены')
        conn=sqlite3.connect(f'{db_path.resolve().as_uri()}?mode=ro',uri=True);conn.row_factory=sqlite3.Row
        try:result=verify(conn,reference)
        finally:conn.close()
        (args.output/'reference_check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        report.update(ok=result['ok'],pdf_sha256=pdf.sha256,reference=case['reference'],
                      extraction_ok=extraction['ok'],source_discrepancies_present='расхождение' in extraction['statuses'],
                      all_requested_observations_present=extraction.get('all_requested_observations_present'),
                      missing_observations=extraction.get('missing_observations',[]))
    except Exception as error:
        report['error']=f'{type(error).__name__}: {error}'
    (args.output/'case_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f"МСФО {args.case}: {'эталон подтверждён' if report['ok'] else 'требуется проверка'}",flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
