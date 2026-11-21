"""Годовая история: явный лимит/возобновление, разные версии и отсутствие ложной проверки цифр."""

import hashlib
from pathlib import Path

import pytest

from agent.db import init_db
from agent.sources.files import Download
from agent.sources.ir_pages import process_link
from agent.tools.backfill_ifrs import download_history, main

COMPANIES={'TEST':{'name':'Тест','ir_pages':[{'url':'https://example.test/reports','recognition':'pdf_first_page','allowed_hosts':['example.test']}]}}
ENTRIES=[{'ticker':'TEST','expected_year':year,'source_url':f'https://example.test/{year}.pdf',
         'source_page':'https://example.test/reports','discovery_hint':f'МСФО {year}'} for year in (2024,2025)]


def downloader(directory,revision='v1'):
    def fetch(url,out,hosts,**kwargs):
        year={'https://example.test/2024.pdf':2024,'https://example.test/2025.pdf':2025}[url]
        title=f'ПАО Тест. Консолидированная финансовая отчетность по МСФО за {year} год'
        data=('%PDF-'+title+revision).encode();sha=hashlib.sha256(data).hexdigest()
        path=directory/(sha+'.pdf');path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        return Download(path=str(path),sha256=sha,pages=1,first_page=title)
    return fetch


def options(tmp_path,fetch):
    return dict(first_year=2024,last_year=2025,max_downloads=1,
        processor=lambda conn,row,**kw:process_link(conn,row,downloader=fetch,**kw))


def test_history_resumes_limited_downloads_and_does_not_claim_financial_validation(tmp_path):
    conn=init_db(tmp_path/'archive.db');files=tmp_path/'pdf';fetch=downloader(files)
    first=download_history(conn,COMPANIES,ENTRIES,files,**options(tmp_path,fetch))
    assert not first['ok'] and [row['status'] for row in first['files']]==['downloaded','deferred']
    assert first['files'][0]['document_metadata_verified']
    second=download_history(conn,COMPANIES,ENTRIES,files,**options(tmp_path,fetch))
    assert second['ok'] and [row['status'] for row in second['files']]==['cached','downloaded']
    assert not second['financial_history_validated'] and second['messages_sent']==0
    assert conn.execute('SELECT count(*) FROM documents').fetchone()[0]==2
    assert conn.execute('SELECT count(*) FROM source_values').fetchone()[0]==0
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    conn.close()


def test_same_url_new_pdf_preserves_both_versions(tmp_path):
    conn=init_db(tmp_path/'archive.db');files=tmp_path/'pdf'
    for revision in ('v1','v2'):
        report=download_history(conn,COMPANIES,ENTRIES[:1],files,refresh=True,**options(tmp_path,downloader(files,revision)))
        assert report['ok']
    assert conn.execute('SELECT count(*) FROM documents').fetchone()[0]==2
    assert conn.execute('SELECT count(*) FROM ir_observations').fetchone()[0]==2
    assert len(list(files.glob('*.pdf')))==2
    conn.close()


def test_link_hint_never_confirms_document_metadata(tmp_path):
    conn=init_db(tmp_path/'archive.db')
    def guessed(*args,**kwargs):
        return {'status':'downloaded','classification':{'basis':'link_text','doc_type':'ifrs','standard':'IFRS',
            'period_type':'FY','period_end':'2024-12-31','needs_review':False}}
    report=download_history(conn,COMPANIES,ENTRIES[:1],tmp_path/'pdf',first_year=2024,last_year=2024,
                             max_downloads=1,processor=guessed)
    assert report['ok'] and not report['files'][0]['document_metadata_verified']
    conn.close()


def test_cli_refuses_main_database(monkeypatch,tmp_path):
    import agent.tools.backfill_ifrs as tool
    target=tmp_path/'main.db';target.write_bytes(b'original')
    monkeypatch.setattr(tool.db,'DB_PATH',target)
    with pytest.raises(SystemExit):main(['--db',str(target),'--files',str(tmp_path/'pdf'),'--output',str(tmp_path/'out.json')])
    assert target.read_bytes()==b'original'
