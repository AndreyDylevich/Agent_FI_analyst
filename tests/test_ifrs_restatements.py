"""Пересчёт — отдельное проверяемое решение, не повод исправить числа или одобрить OCR."""

import copy
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from agent import config
from agent.db import init_db
from agent.extract.pdf_text import PDFText,Page
from agent.extract.restatements import apply_restatement_review
from agent.extract.store import store_extraction
from agent.metrics.data import load_facts
from agent.metrics.engine import Engine,Period
from agent.tools.review_ifrs_restatement import rebuild_affected
from tests.test_ifrs_pipeline import facts,make_run

DAY=date(2021,12,31)


def setup(tmp_path,*,ocr=False,bad=False,other_company=False):
    conn=init_db(tmp_path/'review.db')
    old=tuple(replace(f,end=DAY,period_type='FY') for f in facts())
    period=Period(date(2021,1,1),DAY,'FY')
    with conn:
        first=store_extraction(conn,make_run(old),ticker='TEST',name='Тест',source_url='https://example.test/old.pdf',period=period,local_path='old.pdf')
        engine=Engine(load_facts(conn,company_id=first['company_id'],standard='IFRS',scope='group',segment_name=None,line_items=config.line_items()),
            company_id=first['company_id'],standard='IFRS',scope='group',industry_id='telecom',industries=config.industries())
        engine.save(conn,engine.calculate('current_ratio',period))
    values={'total_assets':'105' if bad else '102','current_assets':'42','non_current_liabilities':'52'}
    new=tuple(replace(f,value=D(values.get(f.item_id,str(f.value))),normalized=D(values.get(f.item_id,str(f.value)))*1000000,
                      raw_value=values.get(f.item_id,f.raw_value),from_ocr=ocr) for f in old)
    with conn:
        store_extraction(conn,replace(make_run(new),pdf_sha256='b'*64),ticker='OTHER' if other_company else 'TEST',name='Тест',
            source_url='https://example.test/new.pdf',period=Period(date(2022,1,1),date(2022,12,31),'FY'),local_path='new.pdf')
    proof=PDFText('b'*64,(Page(1,'Сравнительная информация по состоянию на 31 декабря 2021 года ретроспективно пересмотрена.'),))
    review={'version':1,'reviewer':'Тестовая проверка источника','reason':'Проверены исходные версии и раскрытие пересчёта',
        'source_evidence':{'page':1,'start_line':1,'end_line':1},'replacements':[
            {'item_id':item,'period_start':None,'period_end':str(DAY),'old_pdf_sha256':'a'*64,'new_pdf_sha256':'b'*64,
             'old_normalized_value':str(next(f for f in old if f.item_id==item).normalized),
             'new_normalized_value':str(next(f for f in new if f.item_id==item).normalized)} for item in values]}
    return conn,proof,review


def test_restatement_keeps_original_numbers_and_rebuilds_dependent_calculation(tmp_path):
    conn,pdf,review=setup(tmp_path)
    before=[tuple(r) for r in conn.execute('SELECT id,value,value_norm,unit FROM source_values ORDER BY id')]
    with conn:
        result=apply_restatement_review(conn,pdf,review)
        rebuilt=rebuild_affected(conn,result,companies={'TEST':{'industry':{'id':'telecom'}}})
    assert before==[tuple(r) for r in conn.execute('SELECT id,value,value_norm,unit FROM source_values ORDER BY id')]
    assert conn.execute('SELECT count(*) FROM source_values WHERE superseded_by IS NOT NULL').fetchone()[0]==3
    assert conn.execute('SELECT count(*) FROM source_values WHERE is_restated=1').fetchone()[0]==3
    ratios=[row[0] for row in conn.execute("SELECT value FROM metric_values WHERE metric_id='current_ratio' ORDER BY id")]
    assert ratios==['2','2.1'] and len(rebuilt)==1
    assert not result['comparability_approved'] and not result['source_numbers_modified']
    with conn:
        assert apply_restatement_review(conn,pdf,review)==result
        assert rebuild_affected(conn,result,companies={'TEST':{'industry':{'id':'telecom'}}})==rebuilt
    assert conn.execute('SELECT count(*) FROM metric_values').fetchone()[0]==2
    assert not conn.execute('PRAGMA foreign_key_check').fetchall();conn.close()


@pytest.mark.parametrize('change',['missing_disclosure','wrong_date','wrong_value','wrong_pdf','other_company','flow_scope','tampered_protocol'])
def test_restatement_cannot_be_inferred_from_difference_alone(tmp_path,change):
    conn,pdf,review=setup(tmp_path,other_company=change=='other_company')
    if change=='missing_disclosure':pdf=replace(pdf,pages=(Page(1,'Значения на 31 декабря 2021 года.'),))
    if change=='wrong_date':pdf=replace(pdf,pages=(Page(1,'Данные на 31 декабря 2020 года ретроспективно пересмотрены.'),))
    if change=='wrong_value':review['replacements'][0]['new_normalized_value']='999'
    if change=='wrong_pdf':pdf=replace(pdf,sha256='f'*64)
    if change=='flow_scope':review['replacements'][0]['period_start']='2021-01-01'
    if change=='tampered_protocol':
        with conn:conn.execute("UPDATE meta SET value=value||' ' WHERE key LIKE 'ifrs_extraction:%'")
    with pytest.raises(ValueError):
        with conn:apply_restatement_review(conn,pdf,review)
    assert not conn.execute('SELECT id FROM source_values WHERE superseded_by IS NOT NULL').fetchall()
    conn.close()


def test_another_conflicting_version_is_not_silently_hidden(tmp_path):
    conn,pdf,review=setup(tmp_path)
    values={'total_assets':'103','current_assets':'43','non_current_liabilities':'53'}
    rows=tuple(replace(f,end=DAY,period_type='FY',value=D(values.get(f.item_id,str(f.value))),
        normalized=D(values.get(f.item_id,str(f.value)))*1000000,raw_value=values.get(f.item_id,f.raw_value)) for f in facts())
    with conn:store_extraction(conn,replace(make_run(rows),pdf_sha256='c'*64),ticker='TEST',name='Тест',
        source_url='https://example.test/third.pdf',period=Period(date(2022,1,1),date(2022,6,30),'H1'),local_path='third.pdf')
    with pytest.raises(ValueError,match='ещё не разобранная'):
        with conn:apply_restatement_review(conn,pdf,review)
    assert not conn.execute('SELECT id FROM source_values WHERE superseded_by IS NOT NULL').fetchall()
    conn.close()


@pytest.mark.parametrize('ocr,bad',[(True,False),(False,True)])
def test_source_restatement_does_not_approve_ocr_or_bad_arithmetic(tmp_path,ocr,bad):
    conn,pdf,review=setup(tmp_path,ocr=ocr,bad=bad)
    with conn:result=apply_restatement_review(conn,pdf,review)
    states={row[0] for row in conn.execute("SELECT s.check_status FROM source_values s JOIN documents d ON d.id=s.document_id WHERE d.sha256=? AND s.item_id='current_assets'",('b'*64,))}
    assert states==({'не_проверено'} if ocr else {'расхождение'})
    assert not result['comparability_approved'];conn.close()
