"""Независимая приёмка выбранных годовых денег/долга/аренды/ROU; без одобрения сопоставимости."""
import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from agent import db
from agent.extract.restatements import original_status
from agent.tools.backfill_ifrs import MANIFEST
from agent.tools.extract_ifrs_history import FINANCING_CATALOG,FINANCING_ITEMS
from agent.tools.verify_ifrs_history import PREFIXES,REFERENCES
from agent.tools.verify_ifrs_reference import verify_statements


def verify_year(conn,entry,reference):
    year=entry['expected_year'];digest=entry['pdf_sha256']
    if (reference.get('source_specific') is not True or reference.get('ticker')!=entry['ticker']
            or reference.get('year')!=year or reference['pdf_sha256']!=digest
            or reference['source_url']!=entry['source_url']):
        raise ValueError('Нужен независимый эталон точного выпуска и компании')
    profiles=[p for p in json.loads(FINANCING_CATALOG.read_text())['documents'] if p['pdf_sha256']==digest]
    if len(profiles)!=1 or profiles[0].get('selection_profile')!='balance_financing':
        raise ValueError('Нет единственного просмотренного профиля денег/долга/аренды')
    profile=profiles[0]
    plan_digest=hashlib.sha256(json.dumps(profile,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    identity=lambda r:(r['item_id'],r['statement'],r.get('start',r.get('period_start')),r.get('end',r.get('period_end')))
    expected={identity(r):r for r in reference['rows']}
    planned={identity(o):o for o in profile['observations'].values() if not o.get('source_absent')}
    core={(item,'balance',None,f'{y}-12-31') for item in FINANCING_ITEMS for y in (year,year-1)}
    if len(expected)!=len(reference['rows']) or set(expected)!=set(planned) or not core<=set(expected):
        raise ValueError('Эталон не покрывает все выбранные строки и обязательные остатки на обе даты')
    documents=conn.execute('SELECT d.* FROM documents d JOIN companies c ON c.id=d.company_id WHERE c.ticker=? AND d.sha256=?',(entry['ticker'],digest)).fetchall()
    if len(documents)!=1:raise ValueError('Нет единственного исходного выпуска')
    document=documents[0]
    if (document['url']!=entry['source_url'] or document['standard']!='IFRS' or document['period_type']!='FY'
            or document['period_start']!=f'{year}-01-01' or document['period_end']!=f'{year}-12-31'):
        raise ValueError('Не совпадает год/стандарт/URL исходного документа')
    selected=[];seen=set()
    for row in conn.execute('SELECT * FROM source_values WHERE document_id=?',(document['id'],)):
        original_status(conn,row);location=json.loads(row['location'])
        saved=conn.execute('SELECT value FROM meta WHERE key=?',(f"ifrs_extraction:{document['id']}:{location['extraction_sha256']}",)).fetchone()
        run=json.loads(saved['value']);fact=run['facts'][location['fact_index']]
        key=(row['item_id'],fact['statement'],row['period_start'],row['period_end'])
        if key not in expected:continue
        if key in seen:raise ValueError('Неоднозначное выбранное наблюдение')
        seen.add(key);target=expected[key]
        if (row['company_id']!=document['company_id'] or row['scope']!='group' or row['segment_name'] is not None
                or row['standard']!='IFRS' or row['currency']!='RUB' or row['superseded_by'] is not None
                or row['cash_flow_section'] is not None or run['pdf_sha256']!=digest
                or run.get('text_processing',{}).get('reviewed_source_structure',{}).get('plan_sha256')!=plan_digest
                or fact['raw_value']!=target['raw_value'] or fact['raw_value']!=location['raw_value']
                or fact['column_header']!=planned[key]['column_header']
                or {k:fact['evidence'][k] for k in ('page','start_line','end_line')}!=target['evidence']):
            raise ValueError('Число/дата/колонка/адрес/охват не совпали с оригинальным балансом')
        selected.append(row)
    if seen!=set(expected):raise ValueError('Не все выбранные исходные остатки сохранены')
    checked=verify_statements(conn,reference)
    return {'year':year,'pdf_sha256':digest,'ok':checked['ok'],'source_values_count':len(selected),
            'independent_reference_check':checked,'stored_statuses':dict(Counter(r['check_status'] for r in selected)),
            'source_accuracy_accepted':False,'comparability_approved':False,
            'structure_plan_sha256':plan_digest}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ticker',choices=sorted(PREFIXES),required=True);p.add_argument('--db',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--years')
    p.add_argument('--from-year',type=int,default=2021);p.add_argument('--through-year',type=int,default=2025)
    args=p.parse_args(argv)
    if args.db.resolve()==db.DB_PATH.resolve():p.error('Нужна отдельная проверочная БД')
    if args.output.exists():p.error('Протокол уже существует')
    try:years=sorted({int(y.strip()) for y in args.years.split(',')}) if args.years else list(range(args.from_year,args.through_year+1))
    except ValueError:p.error('Нужны целые годы через запятую')
    if not years or any(y<2021 or y>2025 for y in years):p.error('Нужны закреплённые годы 2021–2025')
    entries=json.loads(MANIFEST.read_text())['documents'];before=hashlib.sha256(args.db.read_bytes()).hexdigest();results=[]
    conn=sqlite3.connect(f'{args.db.resolve().as_uri()}?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    try:
        for year in years:
            try:
                entry=next(e for e in entries if e['ticker']==args.ticker and e['expected_year']==year)
                reference=json.loads((REFERENCES/f'{PREFIXES[args.ticker]}_fy{year}_financing_reference.json').read_text())
                result=verify_year(conn,entry,reference)
            except (ValueError,KeyError,TypeError,IndexError,OSError,StopIteration,sqlite3.Error) as error:
                result={'year':year,'ok':False,'error':f'{type(error).__name__}: {error}'}
            results.append(result)
        integrity=conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:conn.close()
    unchanged=hashlib.sha256(args.db.read_bytes()).hexdigest()==before
    report={'ticker':args.ticker,'years':results,'ok':all(r['ok'] for r in results) and integrity and unchanged,
        'source_values_count':sum(r.get('source_values_count',0) for r in results),'database_sha256':before,
        'database_unchanged':unchanged,'sqlite_integrity_verified':integrity,'financial_history_validated':False,
        'source_accuracy_accepted':False,'comparability_approved':False,'financial_audit_complete':False,'messages_sent':0,
        'limitation':'Подтверждены выбранные исходные балансовые суммы, не полные балансы/примечания. Независимый эталон не меняет непроверенные статусы и не заменяет отдельную ручную приёмку точности.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:stream.write(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f"{args.ticker}: выбранные деньги/долг/аренда {'сверены' if report['ok'] else 'требуют проверки'}",flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
