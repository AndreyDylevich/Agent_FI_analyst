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


def test_expected_discrepancy_requires_exact_rule_date_and_delta(tmp_path):
    from dataclasses import replace
    from decimal import Decimal
    from agent.db import init_db
    from agent.extract.store import store_extraction
    from agent.tools.verify_ifrs_reference import verify_statements
    from tests.test_extraction_schema import PERIOD
    from tests.test_ifrs_pipeline import facts,make_run
    rows=list(facts());rows[0]=replace(rows[0],value=Decimal('102'),normalized=Decimal('102000000'),raw_value='102')
    conn=init_db(tmp_path/'disputed.db')
    with conn:store_extraction(conn,make_run(tuple(rows)),ticker='TEST',name='Тест',
        source_url='https://example.test/a.pdf',period=PERIOD,local_path='a.pdf')
    reference={'pdf_sha256':'a'*64,'unit':'млн руб.','multiplier':'1000000','checks_required':[],
        'rows':[{'item_id':'total_assets','start':None,'end':'2026-06-30','value':'102','pages':[1],'status':'расхождение'}],
        'checks_expected':[{'rule':'balance','start':None,'end':'2026-06-30','status':'расхождение','delta':'2000000'}]}
    assert verify_statements(conn,reference)['ok']
    for change in ({'delta':'1000000'},{'end':'2025-06-30'},{'status':'пройдено'},{'rule':'absent'}):
        altered={**reference,'checks_expected':[{**reference['checks_expected'][0],**change}]}
        assert not verify_statements(conn,altered)['ok']
    conn.close()


def test_history_reference_checks_requested_pdf_with_other_confirming_sources_present(tmp_path):
    from dataclasses import replace
    from agent.db import init_db
    from agent.extract.store import store_extraction
    from agent.tools.verify_ifrs_reference import verify_statements
    from tests.test_extraction_schema import PERIOD
    from tests.test_ifrs_pipeline import make_run
    conn=init_db(tmp_path/'history.db')
    with conn:
        for sha in ('a'*64,'b'*64):
            store_extraction(conn,replace(make_run(),pdf_sha256=sha),ticker='TEST',name='Тест',
                source_url=f'https://example.test/{sha}.pdf',period=PERIOD,local_path='a.pdf')
    reference={'pdf_sha256':'a'*64,'unit':'млн руб.','multiplier':'1000000','source_specific':True,
        'checks_required':['assets_components'],
        'rows':[{'item_id':'current_assets','start':None,'end':'2026-06-30','value':'40','pages':[1],'status':'проверено_авто'}]}
    assert verify_statements(conn,reference)['ok']
    assert not verify_statements(conn,{**reference,'pdf_sha256':'f'*64})['ok']
    assert not verify_statements(conn,{**reference,'source_specific':False})['ok']
    conn.close()


@pytest.mark.parametrize('foreign_context', ['company', 'standard', 'segment'])
def test_reference_metric_cannot_come_from_other_company_standard_or_segment(tmp_path, foreign_context):
    from agent import config
    from agent.db import init_db
    from agent.extract.store import store_extraction
    from agent.metrics.data import load_facts
    from agent.metrics.engine import Engine
    from agent.tools.verify_ifrs_reference import verify_statements
    from tests.test_extraction_schema import PERIOD
    from tests.test_ifrs_pipeline import make_run
    conn = init_db(tmp_path/'metric-context.db')
    with conn:
        saved = store_extraction(conn, make_run(), ticker='TEST', name='Тест',
                                source_url='https://example.test/a.pdf', period=PERIOD, local_path='a.pdf')
        facts = load_facts(conn, company_id=saved['company_id'], standard='IFRS', scope='group',
                           segment_name=None, line_items=config.line_items())
        engine = Engine(facts, company_id=saved['company_id'], standard='IFRS', scope='group',
                        industry_id='telecom', industries=config.industries())
        engine.save(conn, engine.calculate('current_ratio', PERIOD))
        if foreign_context == 'company':
            other = conn.execute("INSERT INTO companies(ticker,name) VALUES ('OTHER','Другая')").lastrowid
            conn.execute('UPDATE metric_values SET company_id=?', (other,))
        elif foreign_context == 'standard':
            conn.execute("UPDATE metric_values SET standard='RSBU'")
        else:
            conn.execute("UPDATE metric_values SET scope='segment',segment_name='Телеком'")
    reference = {'pdf_sha256':'a'*64, 'unit':'млн руб.', 'multiplier':'1000000', 'checks_required':[],
        'rows':[{'item_id':'current_assets','start':None,'end':'2026-06-30','value':'40',
                 'pages':[1],'status':'проверено_авто'}],
        'metrics':[{'metric_id':'current_ratio','period_end':'2026-06-30','numerator':2,'denominator':1}]}
    result = verify_statements(conn, reference)
    assert result['all_selected_values_verified'] and not result['ok']
    assert not result['metrics']['current_ratio']
    conn.close()


@pytest.mark.parametrize('declared',[True,False])
def test_case_proceeds_to_reference_only_for_declared_source_discrepancy(monkeypatch,tmp_path,declared):
    from agent.db import init_db
    original=json.loads((check_ifrs_case.CATALOG.parent/'sgzh_h1_2026_balance_reference.json').read_text())
    if declared:original['checks_expected']=[{'rule':'balance','end':'2026-06-30','status':'расхождение'}]
    catalog=tmp_path/'cases.json'
    catalog.write_text(json.dumps({'sgzh_balance':{'ticker':'SGZH','year':2026,'period':'H1',
                                                  'sections':['balance'],'reference':'ref.json'}}))
    (tmp_path/'ref.json').write_text(json.dumps(original))
    monkeypatch.setattr(check_ifrs_case,'CATALOG',catalog)
    monkeypatch.setattr(check_ifrs_case,'download_pdf',lambda *args:SimpleNamespace(sha256=original['pdf_sha256'],path='source.pdf'))
    verified=[]
    def extraction(command):
        out=tmp_path/'out';(out/'extraction').mkdir()
        init_db(out/'check.db').close()
        (out/'extraction/extraction_check.json').write_text(json.dumps(
            {'ok':False,'facts':[{'item_id':'total_assets'}],'statuses':['расхождение']}))
        return 1
    monkeypatch.setattr(check_ifrs_case,'extract_main',extraction)
    def verify(*args): verified.append(True);return {'ok':True}
    monkeypatch.setattr(check_ifrs_case,'verify',verify)
    code=check_ifrs_case.main(['--case','sgzh_balance','--output',str(tmp_path/'out')])
    report=json.loads((tmp_path/'out/case_check.json').read_text())
    assert code==(0 if declared else 1) and bool(verified)==declared
    if declared:assert not report['extraction_ok'] and report['source_discrepancies_present']


@pytest.mark.parametrize('problem',['missing','changed','not_pdf'])
def test_archive_source_stops_before_model_if_pinned_pdf_is_not_exact(monkeypatch,tmp_path,problem):
    cases=json.loads(check_ifrs_case.CATALOG.read_text());reference=json.loads((check_ifrs_case.CATALOG.parent/cases['sgzh_balance']['reference']).read_text())
    archive=tmp_path/'archive';(archive/'pdf').mkdir(parents=True)
    if problem!='missing':(archive/'pdf'/f"{reference['pdf_sha256']}.pdf").write_bytes(b'%PDF- changed' if problem=='changed' else b'not a PDF')
    def no_external(*args):raise AssertionError('Не обращаться к модели/сети при неверном архивном PDF')
    monkeypatch.setattr(check_ifrs_case,'download_pdf',no_external);monkeypatch.setattr(check_ifrs_case,'extract_main',no_external)
    out=tmp_path/'out';assert check_ifrs_case.main(['--case','sgzh_balance','--source-archive',str(archive),'--output',str(out)])==1
    report=json.loads((out/'case_check.json').read_text());assert not report['ok'] and report['error']
    assert not (out/'check.db').exists()


def test_exact_archive_reuses_pdf_but_does_not_reuse_model_responses(monkeypatch,tmp_path):
    import hashlib
    from agent.db import init_db
    cases=json.loads(check_ifrs_case.CATALOG.read_text());case=cases['sgzh_balance'];ref=json.loads((check_ifrs_case.CATALOG.parent/case['reference']).read_text())
    content=b'%PDF- synthetic source for CLI-only test';digest=hashlib.sha256(content).hexdigest();ref['pdf_sha256']=digest
    catalog=tmp_path/'cases.json';catalog.write_text(json.dumps({'sgzh_balance':{**case,'reference':'reference.json'}}));(tmp_path/'reference.json').write_text(json.dumps(ref))
    monkeypatch.setattr(check_ifrs_case,'CATALOG',catalog)
    archive=tmp_path/'archive';(archive/'pdf').mkdir(parents=True);(archive/'pdf'/f'{digest}.pdf').write_bytes(content)
    def no_download(*args):raise AssertionError('Не загружать документ при закреплённом архиве')
    monkeypatch.setattr(check_ifrs_case,'download_pdf',no_download)
    called=[]
    def extract(command):
        called.append(command);assert '--reuse-from' not in command and '--cached-only' not in command
        out=tmp_path/'out';(out/'extraction').mkdir();init_db(out/'check.db').close()
        (out/'extraction/extraction_check.json').write_text(json.dumps({'ok':True,'facts':[{}],'statuses':['проверено_авто']}));return 0
    monkeypatch.setattr(check_ifrs_case,'extract_main',extract);monkeypatch.setattr(check_ifrs_case,'verify',lambda *args:{'ok':True})
    out=tmp_path/'out';assert check_ifrs_case.main(['--case','sgzh_balance','--source-archive',str(archive),'--output',str(out)])==0
    r=json.loads((out/'case_check.json').read_text());assert called and r['pdf_origin']=='previously_verified_archive' and r['source_pdf_downloaded_this_run'] is False
    assert (out/'pdf'/f'{digest}.pdf').read_bytes()==content
