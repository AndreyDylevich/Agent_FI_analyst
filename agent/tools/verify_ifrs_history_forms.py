"""Независимая приёмка выбранных годовых строк ОПУ/ОДДС; сопоставимость не одобряется."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from decimal import Decimal, DecimalException
from pathlib import Path

from agent import db
from agent.checks.reviewed_tables import CATALOG
from agent.extract.restatements import original_status
from agent.extract.schemas import parse_amount
from agent.tools.backfill_ifrs import MANIFEST
from agent.tools.extract_ifrs_history import CORE_FLOW_ITEMS
from agent.tools.verify_ifrs_history import PREFIXES, REFERENCES
from agent.tools.verify_ifrs_reference import verify_statements


def verify_year(conn, entry, reference):
    year=entry['expected_year']
    if (reference.get('source_specific') is not True or reference.get('ticker') != entry['ticker']
            or reference.get('year') != year or reference['pdf_sha256'] != entry['pdf_sha256']
            or reference['source_url'] != entry['source_url'] or not reference.get('rows')):
        raise ValueError('Нужен независимый непустой эталон точного выпуска')
    signature=lambda r:(r['item_id'],r['statement'],r['start'],r['end'])
    expected={signature(r):r for r in reference['rows']}
    if len(expected)!=len(reference['rows']):raise ValueError('Повтор исходной строки в эталоне')
    core={(item,'income' if item in ('revenue','net_profit') else 'cash_flow',f'{y}-01-01',f'{y}-12-31')
          for y in (year,year-1) for item in CORE_FLOW_ITEMS}
    if not core <= set(expected):raise ValueError('Нет обязательных годовых потоков на оба периода')
    profiles=[p for p in json.loads(CATALOG.read_text())['documents'] if p['pdf_sha256']==entry['pdf_sha256']]
    if len(profiles)!=1 or profiles[0].get('selection_profile')!='income_cash':
        raise ValueError('Нет единственной просмотренной структуры годовых ОПУ/ОДДС')
    profile=profiles[0];plan_digest=hashlib.sha256(json.dumps(profile,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    documents=conn.execute('SELECT d.* FROM documents d JOIN companies c ON c.id=d.company_id '
                           'WHERE c.ticker=? AND d.sha256=?',(entry['ticker'],entry['pdf_sha256'])).fetchall()
    if len(documents)!=1:raise ValueError('Нет единственного исходного годового документа')
    document=documents[0]
    if (document['url']!=entry['source_url'] or document['standard']!='IFRS'
            or document['period_type']!='FY' or document['period_start']!=f'{year}-01-01'
            or document['period_end']!=f'{year}-12-31'):
        raise ValueError('Период/стандарт/URL документа не совпадают')
    selected=[];seen=set();contexts={};ocr_count=0;unreviewed_ocr_count=0
    for row in conn.execute('SELECT * FROM source_values WHERE document_id=?',(document['id'],)):
        location=json.loads(row['location'])
        original_status(conn,row)
        key=f"ifrs_extraction:{document['id']}:{location['extraction_sha256']}"
        if key not in contexts:contexts[key]=json.loads(conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()['value'])
        run=contexts[key];fact=run['facts'][location['fact_index']]
        identity=(row['item_id'],fact['statement'],row['period_start'],row['period_end'])
        if identity not in expected:continue
        if identity in seen:raise ValueError('Неоднозначное исходное наблюдение')
        seen.add(identity);target=expected[identity]
        proof=fact['evidence'];address={k:proof[k] for k in ('page','start_line','end_line')}
        if (row['company_id']!=document['company_id'] or row['standard']!='IFRS' or row['scope']!='group'
                or row['segment_name'] is not None or row['currency']!='RUB' or row['superseded_by'] is not None
                or run['pdf_sha256']!=entry['pdf_sha256'] or address!=target['evidence']
                or fact['raw_value']!=location['raw_value'] or fact['raw_value']!=target['raw_value']
                or parse_amount(fact['raw_value'],fact['number_format'])!=Decimal(target['value'])
                or row['cash_flow_section']!=target['cash_flow_section']):
            raise ValueError('Строка/колонка/дата/знак/охват не совпадают с просмотренным оригиналом')
        ocr_count+=bool(fact['from_ocr'])
        unreviewed_ocr_count+=bool(fact['from_ocr'] and row['check_status']!='проверено_вручную')
        selected.append(row)
    if seen!=set(expected):raise ValueError('Не все независимые исходные наблюдения найдены')
    result=verify_statements(conn,reference)
    checked=[]
    for target in reference['checks_expected']:
        matches=[c for run in contexts.values() for c in run['checks'] if
                 (c['rule'],c['start'],c['end'])==(target['rule'],target['start'],target['end'])
                 and (c.get('basis') or {}).get('source_structure_reviewed')]
        passed=bool(matches) and all(c['basis'].get('plan_sha256')==plan_digest
                   and c['result']['status']==target['status']
                   and ('delta' not in target or Decimal(c['result']['delta'])==Decimal(target['delta'])) for c in matches)
        checked.append({**target,'passed':passed})
    if not checked:raise ValueError('Пустой набор сверок не подтверждает годовые формы')
    return {'year':year,'pdf_sha256':entry['pdf_sha256'],'source_values_count':len(selected),
            'stored_statuses':dict(Counter(r['check_status'] for r in selected)),
            'ocr_observations_count':ocr_count,'unreviewed_ocr_observations_count':unreviewed_ocr_count,
            'ocr_financial_acceptance_complete':ocr_count>0 and unreviewed_ocr_count==0,
            'reference_check':result,'reviewed_checks':checked,'structure_plan_sha256':plan_digest,
            'reference_sha256':hashlib.sha256(json.dumps(reference,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
            'ok':result['ok'] and all(c['passed'] for c in checked)}


def verify_history(conn,ticker,entries,references,years):
    if not years or len(set(years))!=len(years):raise ValueError('Нужны различные выбранные годы')
    results=[]
    for year in sorted(years):
        matches=[e for e in entries if e['ticker']==ticker and e['expected_year']==year]
        try:
            if len(matches)!=1 or year not in references:raise ValueError('Нет выпуска/независимого эталона выбранного года')
            result=verify_year(conn,matches[0],references[year])
        except (ValueError,KeyError,TypeError,IndexError,DecimalException,sqlite3.Error) as error:
            result={'year':year,'ok':False,'error':f'{type(error).__name__}: {error}'}
        results.append(result)
    covered=sorted({y for r in results if r['ok'] for y in (r['year'],r['year']-1)})
    return {'ticker':ticker,'years':results,'requested_report_years':sorted(years),'reported_flow_years_verified':covered,
            'source_values_count':sum(r.get('source_values_count',0) for r in results),
            'ok':bool(results) and all(r['ok'] for r in results),'financial_history_validated':False,
            'comparability_approved':False,'financial_audit_complete':False,'messages_sent':0,
            'limitation':'Проверены только выбранные исходные строки и адресные тождества. Сравнительные колонки могут иметь другой периметр/пересчёт; покрытие календарных лет не означает полной одинаково подготовленной истории или приёмки всех форм.'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker',choices=sorted(PREFIXES),required=True)
    parser.add_argument('--db',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--from-year',type=int,default=2021);parser.add_argument('--through-year',type=int,default=2025)
    parser.add_argument('--years')
    args=parser.parse_args(argv)
    if args.db.resolve()==db.DB_PATH.resolve():parser.error('Нужна отдельная проверочная БД')
    if args.output.exists():parser.error('Нужно новое имя протокола')
    try:years=sorted({int(s.strip()) for s in args.years.split(',')}) if args.years else list(range(args.from_year,args.through_year+1))
    except ValueError:parser.error('Нужны целые годы через запятую')
    if not years or any(y<2021 or y>2025 for y in years):parser.error('Поддерживаются закреплённые годы 2021–2025')
    references={}
    for year in years:
        path=REFERENCES/f'{PREFIXES[args.ticker]}_fy{year}_income_cash_reference.json'
        if path.is_file():references[year]=json.loads(path.read_text())
    before=hashlib.sha256(args.db.read_bytes()).hexdigest()
    conn=sqlite3.connect(f'{args.db.resolve().as_uri()}?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    try:
        report=verify_history(conn,args.ticker,json.loads(MANIFEST.read_text())['documents'],references,years)
        report['sqlite_integrity_verified']=conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:conn.close()
    report.update(database_sha256=before,database_unchanged=before==hashlib.sha256(args.db.read_bytes()).hexdigest())
    report['ok']=report['ok'] and report['sqlite_integrity_verified'] and report['database_unchanged']
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:stream.write(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f"{args.ticker}: выбранные годовые потоки {'подтверждены' if report['ok'] else 'требуют проверки'}",flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
