"""Эталон относится к конкретной версии PDF; изменённый источник не отправляется в модель."""

import json
from types import SimpleNamespace

import pytest

from agent.tools import check_ifrs_case


def test_changed_pdf_stops_before_model_call(monkeypatch,tmp_path):
    monkeypatch.setattr(check_ifrs_case,'download_pdf',lambda *args:SimpleNamespace(sha256='f'*64,path='different.pdf'))
    def no_model(*args):raise AssertionError('Не обращаться к LLM при другой версии источника')
    monkeypatch.setattr(check_ifrs_case,'extract_main',no_model)
    out=tmp_path/'check'
    assert check_ifrs_case.main(['--case','sgzh_balance','--output',str(out)])==1
    report=json.loads((out/'case_check.json').read_text())
    assert not report['ok'] and 'SHA256' in report['error']
    assert report['messages_sent']==0 and not (out/'check.db').exists()


def test_nonempty_case_output_not_overwritten(tmp_path):
    out=tmp_path/'check';out.mkdir();(out/'original.json').write_text('исходный ответ')
    with pytest.raises(SystemExit):check_ifrs_case.main(['--case','sgzh_balance','--output',str(out)])
    assert (out/'original.json').read_text()=='исходный ответ'


def test_empty_reference_cannot_pass_without_validation():
    from agent.tools.verify_ifrs_reference import verify_statements
    with pytest.raises(ValueError,match='пустой'):
        verify_statements(None,{'rows':[]})


def test_selected_metric_checked_against_independent_fraction(tmp_path):
    from agent import config
    from agent.db import init_db
    from agent.extract.store import store_extraction
    from agent.metrics.data import load_facts
    from agent.metrics.engine import Engine
    from agent.tools.verify_ifrs_reference import verify_statements
    from tests.test_extraction_schema import PERIOD
    from tests.test_ifrs_pipeline import make_run
    conn=init_db(tmp_path/'ref.db')
    with conn:
        saved=store_extraction(conn,make_run(),ticker='TEST',name='Тест',source_url='https://example.test/a.pdf',
                               period=PERIOD,local_path='a.pdf')
        facts=load_facts(conn,company_id=saved['company_id'],standard='IFRS',scope='group',segment_name=None,
                         line_items=config.line_items())
        engine=Engine(facts,company_id=saved['company_id'],standard='IFRS',scope='group',industry_id='telecom',industries=config.industries())
        engine.save(conn,engine.calculate('current_ratio',PERIOD))
    reference={'pdf_sha256':'a'*64,'unit':'млн руб.','multiplier':'1000000','checks_required':[],
        'rows':[{'item_id':'current_assets','start':None,'end':'2026-06-30','value':'40','pages':[1],'status':'проверено_авто'}],
        'metrics':[{'metric_id':'current_ratio','period_end':'2026-06-30','numerator':2,'denominator':1}]}
    assert verify_statements(conn,reference)['ok']
    reference['metrics'][0]['numerator']=3
    assert not verify_statements(conn,reference)['ok']
    conn.close()
