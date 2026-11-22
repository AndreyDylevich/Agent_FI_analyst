"""Отдельная загрузка годового архива МСФО с SHA256/историей, без извлечения цифр и Telegram."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from agent import config, db
from agent.sources.files import checked_url
from agent.sources.ir_pages import process_link

MANIFEST=Path(__file__).resolve().parents[2]/'config/history_sources.json'


def download_history(conn, companies, entries, directory: Path, *, first_year: int, last_year: int,
                     max_downloads: int, refresh: bool=False, processor=process_link) -> dict:
    """Подсказки IR служат только выбору кандидатов; метаданные проверяются по содержимому PDF."""
    now=datetime.now(timezone.utc).isoformat()
    selected=[e for e in entries if e['ticker'] in companies and first_year<=e['expected_year']<=last_year]
    files=[];downloads=0
    for entry in selected:
        company=companies[entry['ticker']]
        source=next((p for p in company['ir_pages'] if p['url']==entry['source_page']),None)
        if source is None:raise ValueError('IR-страница истории отсутствует в watchlist')
        hosts=set(source['allowed_hosts']);url=checked_url(entry['source_url'],hosts)
        with conn:
            conn.execute('INSERT OR IGNORE INTO companies(ticker,name) VALUES (?,?)',(entry['ticker'],company['name']))
            company_id=conn.execute('SELECT id FROM companies WHERE ticker=?',(entry['ticker'],)).fetchone()['id']
            conn.execute('INSERT OR IGNORE INTO ir_pages(company_id,url) VALUES (?,?)',(company_id,source['url']))
            page_id=conn.execute('SELECT id FROM ir_pages WHERE company_id=? AND url=?',(company_id,source['url'])).fetchone()['id']
            conn.execute("INSERT OR IGNORE INTO ir_links(page_id,url,title,first_seen_at,last_seen_at,state) VALUES (?,?,?,?,?,'pending')",
                         (page_id,url,entry['discovery_hint'],now,now))
        row=conn.execute('SELECT * FROM ir_links WHERE page_id=? AND url=?',(page_id,url)).fetchone()
        old=conn.execute('SELECT * FROM documents WHERE id=?',(row['document_id'],)).fetchone()
        cached=bool(old and old['sha256'] and old['local_path'] and Path(old['local_path']).is_file()
                    and hashlib.sha256(Path(old['local_path']).read_bytes()).hexdigest()==old['sha256'])
        base={'ticker':entry['ticker'],'expected_year':entry['expected_year'],'discovery_hint':entry['discovery_hint'],
              'source_page':entry['source_page'],'financial_values_verified':False}
        if cached and not refresh:
            evidence=conn.execute('SELECT evidence FROM document_classifications WHERE document_id=?',(old['id'],)).fetchone()
            classification=json.loads(evidence['evidence']) if evidence else {'basis':'unknown','needs_review':True}
            result={'url':url,'status':'cached','document_id':old['id'],'sha256':old['sha256'],'classification':classification}
        elif downloads>=max_downloads:
            files.append({**base,'url':url,'status':'deferred','document_metadata_verified':False});continue
        else:
            downloads+=1
            result=processor(conn,row,company_id=company_id,recognition=source['recognition'],directory=directory,
                             allowed_hosts=hosts,now=now)
        if result['status']=='not_modified' and old:
            evidence=conn.execute('SELECT evidence FROM document_classifications WHERE document_id=?',(old['id'],)).fetchone()
            result.update(sha256=old['sha256'],classification=json.loads(evidence['evidence']) if evidence else {})
        classification=result.get('classification',{})
        verified=(classification.get('basis')=='pdf_first_page' and classification.get('doc_type')=='ifrs'
                  and classification.get('standard')=='IFRS' and classification.get('period_type')=='FY'
                  and classification.get('period_end')==f"{entry['expected_year']}-12-31" and not classification.get('needs_review',True))
        files.append({**base,**result,'document_metadata_verified':verified})
        print(f"{entry['ticker']} FY {entry['expected_year']}: {result['status']}; метаданные PDF: {verified}",flush=True)
    report={'checked_at_utc':now,'files':files,'downloads':downloads,
            'archive_download_complete':not any(f['status'] in ('error','deferred') for f in files),
            'financial_history_validated':False,'messages_sent':0,
            'limitation':'Загрузка PDF не является извлечением/сверкой финансовой истории. Не подтверждённые по PDF метаданные и специальные основы подготовки требуют отдельного разбора.'}
    report['ok']=bool(selected) and report['archive_download_complete']
    serialized=json.dumps(report,ensure_ascii=False,sort_keys=True)
    with conn:
        conn.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)',
                     (f'ifrs_backfill:{hashlib.sha256(serialized.encode()).hexdigest()}',serialized))
    return report


def main(argv=None) -> int:
    companies={c['ticker']:c for c in config.load('watchlist')['companies']}
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--files',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--ticker',choices=sorted(companies),action='append')
    parser.add_argument('--from-year',type=int,default=2021)
    parser.add_argument('--through-year',type=int,default=2025)
    parser.add_argument('--max-downloads',type=int,default=20)
    parser.add_argument('--refresh',action='store_true')
    args=parser.parse_args(argv)
    if args.db.resolve()==db.DB_PATH.resolve():parser.error('Для первичной загрузки истории нужна отдельная БД проверки')
    if args.output.exists():parser.error('Протокол уже существует; задайте новое имя, история не перезаписывается')
    if args.from_year>args.through_year or args.max_downloads<1:parser.error('Нужен корректный диапазон лет и положительный лимит')
    if args.ticker:companies={key:companies[key] for key in args.ticker}
    entries=json.loads(MANIFEST.read_text())['documents']
    conn=db.init_db(args.db)
    try:
        report=download_history(conn,companies,entries,args.files,first_year=args.from_year,last_year=args.through_year,
                                max_downloads=args.max_downloads,refresh=args.refresh)
    finally:conn.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f"Архив: {len(report['files'])} кандидатов, {report['downloads']} загрузок; финансовые значения не сверялись",flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
