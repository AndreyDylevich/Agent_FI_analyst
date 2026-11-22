"""Годовая диагностика не отправляет изменённый источник в API и не объявляет историю принятой."""

import json
from types import SimpleNamespace

import pytest

from agent import config, db
from agent.extract.pdf_text import PDFText, Page
from agent.tools import extract_ifrs_history as tool


def command(tmp_path):
    return ['--ticker','SGZH','--year','2025','--db',str(tmp_path/'check.db'),'--output',str(tmp_path/'out')]


def test_history_extraction_refuses_main_database(tmp_path):
    args=command(tmp_path);args[args.index('--db')+1]=str(db.DB_PATH)
    with pytest.raises(SystemExit):tool.main(args)


def test_changed_history_pdf_stops_before_parsing_or_model(monkeypatch,tmp_path):
    monkeypatch.setattr(tool,'download_pdf',lambda *args:SimpleNamespace(sha256='f'*64,path='different.pdf'))
    def no_call(*args,**kwargs):raise AssertionError('Не обрабатывать изменённый источник')
    monkeypatch.setattr(tool,'read_pdf',no_call);monkeypatch.setattr(tool,'extract_main',no_call)
    assert tool.main(command(tmp_path))==1
    report=json.loads((tmp_path/'out/history_extraction_check.json').read_text())
    assert 'Версия' in report['error'] and not (tmp_path/'check.db').exists()


@pytest.mark.parametrize('year',[2024,2025])
def test_year_confirmed_before_model_and_success_is_not_history_acceptance(monkeypatch,tmp_path,year):
    entry=next(row for row in json.loads(tool.MANIFEST.read_text())['documents'] if row['ticker']=='SGZH' and row['expected_year']==2025)
    company=next(c for c in config.load('watchlist')['companies'] if c['ticker']=='SGZH')
    text=(f"ПАО {company['document_identity_token']}. Консолидированная финансовая отчетность по МСФО за {year} год.\n"
          'В миллионах российских рублей')
    monkeypatch.setattr(tool,'download_pdf',lambda *args:SimpleNamespace(sha256=entry['pdf_sha256'],path='source.pdf'))
    monkeypatch.setattr(tool,'read_pdf',lambda *args,**kwargs:PDFText(entry['pdf_sha256'],(Page(1,text),)))
    calls=[]
    def extract(args):
        calls.append(args);folder=tmp_path/'out/extraction';folder.mkdir()
        (folder/'extraction_check.json').write_text(json.dumps({'facts':[{}],'statuses':['проверено_авто']}))
        return 0
    monkeypatch.setattr(tool,'extract_main',extract)
    assert tool.main(command(tmp_path))==(0 if year==2025 else 1)
    assert bool(calls)==(year==2025)
    report=json.loads((tmp_path/'out/history_extraction_check.json').read_text())
    assert not report['financial_history_validated'] and not report['independent_reference_verified']
    assert not report['comparability_approved'] and report['messages_sent']==0
