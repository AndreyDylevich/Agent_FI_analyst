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
