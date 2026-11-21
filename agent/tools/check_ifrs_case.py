"""Изолированная приёмка реальной МСФО по независимому эталону, без основной БД и Telegram."""

import argparse
import json
import sqlite3
from pathlib import Path

from agent import config
from agent.sources.files import download_pdf
from agent.tools.extract_ifrs import main as extract_main
from agent.tools.verify_ifrs_reference import verify

ROOT=Path(__file__).resolve().parents[2]
CATALOG=ROOT/'tests/fixtures/ifrs/validation_cases.json'


def main(argv=None) -> int:
    cases=json.loads(CATALOG.read_text())
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=sorted(cases),required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('Нужен новый/пустой каталог проверки')
    args.output.mkdir(parents=True,exist_ok=True)
    case=cases[args.case]
    reference=json.loads((CATALOG.parent/case['reference']).read_text())
    company=next(c for c in config.load('watchlist')['companies'] if c['ticker']==case['ticker'])
    hosts={host for page in company['ir_pages'] for host in page['allowed_hosts']}
    report={'case':args.case,'ok':False,'messages_sent':0,'financial_audit_complete':False}
    try:
        pdf=download_pdf(reference['source_url'],args.output/'pdf',hosts)
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
        if case.get('ocr'):command.extend(['--ocr','--max-ocr-pages',str(case.get('max_ocr_pages',50))])
        code=extract_main(command)
        if code:
            raise ValueError('Извлечение не прошло проверку; исходные ответы сохранены')
        conn=sqlite3.connect(f'{db_path.resolve().as_uri()}?mode=ro',uri=True);conn.row_factory=sqlite3.Row
        try:result=verify(conn,reference)
        finally:conn.close()
        (args.output/'reference_check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        report.update(ok=result['ok'],pdf_sha256=pdf.sha256,reference=case['reference'])
    except Exception as error:
        report['error']=f'{type(error).__name__}: {error}'
    (args.output/'case_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f"МСФО {args.case}: {'эталон подтверждён' if report['ok'] else 'требуется проверка'}",flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
