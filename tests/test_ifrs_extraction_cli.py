import json
from contextlib import nullcontext
import pytest
from agent.extract.pdf_text import PDFText,Page
from agent.tools import extract_ifrs as tool


def test_raw_ocr_archive_does_not_make_pipeline_output_nonempty(monkeypatch,tmp_path):
 pdf=PDFText('a'*64,(Page(1,'Original OCR transcript','ocr'),))
 monkeypatch.setattr(tool,'read_pdf',lambda *a,**kw:pdf)
 monkeypatch.setattr(tool,'apply_reviewed_ocr_punctuation',lambda p:p)
 monkeypatch.setattr(tool.GigaChatProvider,'from_env',lambda *a,**kw:nullcontext(object()))
 called=[]
 def pipeline(*a,**kw):
  called.append(True)
  assert list(kw['output'].iterdir())==[]
  raise ValueError('intentional pipeline stop')
 monkeypatch.setattr(tool,'extract_pdf',pipeline)
 out=tmp_path/'out'
 assert tool.main(['scan.pdf','--ticker','MVID','--source-url',
  'https://www.mvideoeldorado.ru/fileadmin/user_upload/IFRS/IRFS_2026/pao-mvideo-msfo-6-mes-2026.pdf',
  '--year','2026','--period','H1','--db',str(tmp_path/'case.db'),'--output',str(out),'--ocr'])==1
 assert called and json.loads((out/'ocr_original_pages.json').read_text())['pages'][0]['text']=='Original OCR transcript'
 assert 'intentional pipeline stop' in json.loads((out/'extraction_check.json').read_text())['error']
 assert not (tmp_path/'case.db').exists()
