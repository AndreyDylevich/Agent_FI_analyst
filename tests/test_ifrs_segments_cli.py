"""Qualitative segment absence cannot create a provider request or numerical database."""
import json
import pytest
from agent.tools import check_ifrs_segments as tool


def test_mvid_requires_both_original_pdf_archive_and_prepared_reviewed_pages(tmp_path):
    with pytest.raises(SystemExit) as error:
        tool.main(['--ticker','MVID','--output',str(tmp_path/'out')])
    assert error.value.code==2 and not (tmp_path/'out').exists()


@pytest.mark.parametrize('wrong_pdf',[False,True])
def test_missing_or_changed_original_blocks_narrative_acceptance_before_any_provider(monkeypatch,tmp_path,wrong_pdf):
    calls=[]
    def forbidden(*a,**k):calls.append(True);raise AssertionError('A narrative absence must never create a provider')
    monkeypatch.setattr(tool.GigaChatProvider,'from_env',forbidden)
    profile=next(p for p in json.loads(tool.SEGMENT_CATALOG.read_text())['documents'] if p['ticker']=='MVID')
    archive=tmp_path/'archive';(archive/'pdf').mkdir(parents=True)
    if wrong_pdf:(archive/'pdf'/f"{profile['pdf_sha256']}.pdf").write_bytes(b'%PDF-1.4\nchanged source')
    out=tmp_path/'result'
    assert tool.main(['--ticker','MVID','--source-archive',str(archive),'--prepared-pages',str(tmp_path/'pages.json'),'--output',str(out)])==1
    report=json.loads((out/'case_check.json').read_text())
    assert not report['ok'] and not calls and not list(out.rglob('*.db'))
    assert ('SHA256' in report['error'])==wrong_pdf
