"""Пакет истории сохраняет прогресс и продолжает независимые годы после ошибки."""

import json

import pytest

from agent import db
from agent.extract.store import store_extraction
from agent.tools.extract_ifrs_history_batch import run_batch
from tests.test_extraction_schema import PERIOD
from tests.test_ifrs_pipeline import make_run


@pytest.mark.parametrize('failure_type',[ValueError,RuntimeError])
def test_failed_year_does_not_drop_later_independent_report(tmp_path,failure_type):
    database=tmp_path/'check.db';called=[]
    entries=[{'ticker':'MTSS','expected_year':year} for year in [2024,2025]]
    def runner(command):
        year=int(command[command.index('--year')+1]);called.append(year)
        if year==2024:raise failure_type('Источник временно недоступен')
        folder=tmp_path/'out/MTSS_2025';folder.mkdir()
        (folder/'history_extraction_check.json').write_text(json.dumps({'ok':True,'selected_values_count':6}))
        conn=db.init_db(database)
        with conn:store_extraction(conn,make_run(),ticker='TEST',name='Тест',source_url='https://example.test/a.pdf',period=PERIOD,local_path='a.pdf')
        conn.close();return 0
    result=run_batch(entries,tmp_path/'out',database,runner=runner)
    assert called==[2024,2025] and result['jobs_succeeded']==1 and result['source_values_count']==6
    assert not result['ok'] and not result['financial_history_validated']
    assert len(json.loads((tmp_path/'out/jobs.json').read_text()))==2


def test_batch_refuses_main_database_old_output_and_empty_success(tmp_path):
    with pytest.raises(ValueError):run_batch([],tmp_path/'out',db.DB_PATH)
    out=tmp_path/'out';out.mkdir();(out/'old.json').write_text('original')
    with pytest.raises(ValueError):run_batch([],out,tmp_path/'check.db')
    assert (out/'old.json').read_text()=='original'
    assert not run_batch([],tmp_path/'empty',tmp_path/'check.db')['ok']


def test_preparation_batch_preserves_profile_without_claiming_financial_success(tmp_path):
 calls=[]
 def runner(command):
  calls.append(command);year=command[command.index('--year')+1];folder=tmp_path/'out'/('SGZH_'+year);folder.mkdir()
  (folder/'history_extraction_check.json').write_text(json.dumps({'ok':False,'text_preparation_ok':True,'provider_requests':0}))
  return 0
 report=run_batch([{'ticker':'SGZH','expected_year':2025}],tmp_path/'out',tmp_path/'check.db',runner=runner,forms_profile='income_cash',prepare_text_only=True)
 assert '--prepare-text-only' in calls[0] and calls[0][calls[0].index('--forms-profile')+1]=='income_cash'
 assert report['text_preparation_ok'] and not report['ok'] and not report['financial_history_validated']
