"""Кэш экономит обращения к API, но не пропускает повторную проверку источника."""

import hashlib
import json
from dataclasses import replace

import pytest

from agent import config
from agent.extract.cache import ResponseCache
from agent.extract.pipeline import extract_pdf
from tests.test_extraction_schema import PERIOD
from tests.test_ifrs_pipeline import Provider,pipeline_pdf,pipeline_answer


def cache_run(tmp_path):
    root=tmp_path/'old';(root/'pdf').mkdir(parents=True)
    content=b'%PDF-test-cache';(root/'pdf/original.pdf').write_bytes(content)
    pdf=replace(pipeline_pdf(),sha256=hashlib.sha256(content).hexdigest())
    extract_pdf(pdf,Provider([pipeline_answer()]),ticker='TEST',entity_token='Тест',period=PERIOD,
                line_items={'total_assets':config.line_items()['total_assets']},output=root/'extraction',sections=('balance',))
    return root,pdf


def test_same_source_response_revalidated_without_network(tmp_path):
    root,pdf=cache_run(tmp_path)
    class NoNetwork:
        def extract(self,*args):raise AssertionError('Повторный вызов модели не нужен')
    run=extract_pdf(pdf,NoNetwork(),ticker='TEST',entity_token='Тест',period=PERIOD,
                line_items={'total_assets':config.line_items()['total_assets']},output=tmp_path/'new',sections=('balance',),reuse_from=(root,))
    assert len(run.facts)==1 and run.batches[0]['tokens']==0 and run.batches[0]['original_tokens']==10
    assert run.batches[0]['reused_from']
    assert (tmp_path/'new/batch_1_attempt_1.json').read_bytes()==(root/'extraction/batch_1_attempt_1.json').read_bytes()
    assert run.statuses==('не_проверено',)


def test_different_pdf_or_modified_response_not_accepted(tmp_path):
    root,pdf=cache_run(tmp_path)
    with pytest.raises(ValueError,match='SHA256'):ResponseCache((root,),'b'*64)
    (root/'extraction/batch_1_attempt_1.json').write_text('{}')
    with pytest.raises(ValueError,match='сумма'):ResponseCache((root,),pdf.sha256)


def test_ocr_requires_same_text_hash_and_old_unknown_ocr_not_reused(tmp_path):
    root,pdf=cache_run(tmp_path);cache=ResponseCache((root,),pdf.sha256)
    assert cache.take({2},{'total_assets'},'wrong-hash',True) is None
    records=json.loads((root/'extraction/attempts.json').read_text());records[0].pop('page_text_sha256')
    (root/'extraction/attempts.json').write_text(json.dumps(records))
    cache=ResponseCache((root,),pdf.sha256)
    assert cache.take({2},{'total_assets'},'any-hash',True) is None


def test_changed_source_text_forces_fresh_extraction(tmp_path):
    root,pdf=cache_run(tmp_path)
    changed=replace(pdf,pages=tuple(replace(page,text=page.text+'\nДополнение') for page in pdf.pages))
    provider=Provider([pipeline_answer()])
    run=extract_pdf(changed,provider,ticker='TEST',entity_token='Тест',period=PERIOD,
                line_items={'total_assets':config.line_items()['total_assets']},output=tmp_path/'new',sections=('balance',),reuse_from=(root,))
    assert len(provider.calls)==1 and 'reused_from' not in run.batches[0]


@pytest.mark.parametrize('text_changed',[False,True])
def test_cached_only_cli_never_initializes_provider_or_uses_unmatched_text(monkeypatch,tmp_path,text_changed):
    from agent.tools import extract_ifrs
    root,pdf=cache_run(tmp_path)
    if text_changed:pdf=replace(pdf,pages=tuple(replace(p,text=p.text+'\nИзменён текст') for p in pdf.pages))
    monkeypatch.setattr(extract_ifrs,'read_pdf',lambda *args,**kwargs:pdf)
    original_load=config.load
    company={'ticker':'TEST','name':'Тест','legal_name':'Тест','industry':{'id':'telecom'},
             'ir_pages':[{'allowed_hosts':['example.test']}]}
    monkeypatch.setattr(config,'load',lambda name: {'companies':[company]} if name=='watchlist' else original_load(name))
    def no_provider(*args):raise AssertionError('В режиме cached-only ключ и клиент модели не нужны')
    monkeypatch.setattr(extract_ifrs.GigaChatProvider,'from_env',no_provider)
    output=tmp_path/'cli'
    code=extract_ifrs.main(['original.pdf','--ticker','TEST','--year','2026','--period','H1',
        '--source-url','https://example.test/report.pdf','--db',str(tmp_path/'check.db'),
        '--output',str(output),'--sections','balance','--item','total_assets','--metric','current_ratio',
        '--reuse-from',str(root),'--cached-only'])
    report=json.loads((output/'extraction_check.json').read_text())
    assert report['cached_only'] and code==(1 if text_changed else 0)
    if text_changed:
        assert 'cached-only' in report['error'] and not (tmp_path/'check.db').exists()
    else:
        assert len(report['facts'])==1 and report['batches'][0]['tokens']==0
