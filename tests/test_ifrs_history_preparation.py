"""Годовые файлы проверяются до API; готовность текста не одобряет историю и сопоставимость."""

import hashlib
import json

import pytest

from agent.extract.pdf_text import PDFText, Page
from agent.tools.prepare_ifrs_history import prepare_archive

COMPANY={'TEST':{'name':'Тест','legal_name':'ПАО Тест','document_identity_token':'Тест',
    'ir_pages':[{'url':'https://example.test/reports','allowed_hosts':['example.test']}]}}
ENTRY={'ticker':'TEST','expected_year':2025,'source_url':'https://example.test/2025.pdf',
       'source_page':'https://example.test/reports','discovery_hint':'МСФО за 2025 год'}


def archive(tmp_path):
    root=tmp_path/'archive';(root/'pdf').mkdir(parents=True)
    raw=b'%PDF-fixture';sha=hashlib.sha256(raw).hexdigest()
    (root/'pdf'/f'{sha}.pdf').write_bytes(raw)
    row={'ticker':'TEST','expected_year':2025,'url':ENTRY['source_url'],'sha256':sha}
    (root/'history_check.json').write_text(json.dumps({'files':[row]}))
    return root,sha,row


def text(year=2025):
    return (f'ПАО Тест. Консолидированная финансовая отчетность по МСФО за {year} год.\n'
        'В миллионах российских рублей\nКонсолидированный отчет о финансовом положении\n'
        'Консолидированный отчет о прибылях и убытках\n'
        'Консолидированный отчет о движении денежных средств\n'
        'Консолидированный отчет об изменениях капитала\n')


def test_verified_pdf_produces_plan_without_approving_financial_history(tmp_path):
    root,sha,_=archive(tmp_path)
    result=prepare_archive(root,[ENTRY],COMPANY,tmp_path/'out',
        reader=lambda *args,**kwargs:PDFText(sha,(Page(1,text()),)))
    assert result['ok'] and result['ready_count']==1
    assert not result['financial_history_validated'] and not result['comparability_approved']
    assert not result['documents'][0]['financial_values_verified'] and result['messages_sent']==0
    assert (tmp_path/'out/TEST_2025/pages.json').exists()


@pytest.mark.parametrize('change',['sha','duplicate','wrong_url'])
def test_invalid_archive_rejected_before_pdf_processing(tmp_path,change):
    root,sha,row=archive(tmp_path)
    if change=='sha':(root/'pdf'/f'{sha}.pdf').write_bytes(b'%PDF-modified')
    if change=='duplicate':(root/'history_check.json').write_text(json.dumps({'files':[row,row]}))
    if change=='wrong_url':(root/'history_check.json').write_text(json.dumps({'files':[{**row,'url':'https://example.test/another.pdf'}]}))
    def no_processing(*args,**kwargs):raise AssertionError('Невалидный архив не передаётся даже парсеру PDF')
    result=prepare_archive(root,[ENTRY],COMPANY,tmp_path/'out',reader=no_processing)
    assert not result['ok'] and result['documents'][0]['error']


@pytest.mark.parametrize('content',[text(2024),''])
def test_link_caption_does_not_replace_wrong_or_unreadable_title(tmp_path,content):
    root,sha,_=archive(tmp_path)
    result=prepare_archive(root,[ENTRY],COMPANY,tmp_path/'out',
        reader=lambda *args,**kwargs:PDFText(sha,(Page(1,content,'native' if content else 'unreadable'),)))
    assert not result['ok'] and not result['documents'][0]['document_metadata_verified']


def test_preparation_preserves_previous_outputs_and_empty_plan_is_not_success(tmp_path):
    root,_,_=archive(tmp_path);out=tmp_path/'out';out.mkdir();(out/'old.json').write_text('original')
    with pytest.raises(ValueError):prepare_archive(root,[ENTRY],COMPANY,out)
    assert (out/'old.json').read_text()=='original'
    assert not prepare_archive(root,[],COMPANY,tmp_path/'empty')['ok']
