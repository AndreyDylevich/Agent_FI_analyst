import hashlib
from dataclasses import replace
import pytest
from agent.extract.ocr_transcript import apply_reviewed_ocr_punctuation
from agent.extract.pdf_text import PDFText,Page


def setup():
 text='Расходы {123)\nДоход 8\n'
 pdf=PDFText('a'*64,(Page(1,text,'ocr'),))
 profile={'pdf_sha256':pdf.sha256,'visual_review_confirmed':True,'reviewer':'Тест','reason':'Скобка сверена с изображением','pages':{'1':{'original_text_sha256':hashlib.sha256(text.encode()).hexdigest(),'bracket_lines':[1]}}}
 return pdf,{'version':1,'documents':[profile]}


def test_only_reviewed_brackets_change_and_provenance_remains_ocr():
 pdf,catalog=setup();result=apply_reviewed_ocr_punctuation(pdf,catalog)
 assert result.pages[0].text=='Расходы (123)\nДоход 8\n' and result.pages[0].method=='ocr'
 assert pdf.pages[0].text=='Расходы {123)\nДоход 8\n' and result.sha256==pdf.sha256
 assert not result.processing['visual_ocr_punctuation']['financial_values_approved']
 assert not result.processing['visual_ocr_punctuation']['pages'][0]['financial_digits_modified']


@pytest.mark.parametrize('problem',['text','native','review','line','no_bracket','duplicate'])
def test_changed_or_unreviewed_transcript_is_rejected(problem):
 pdf,catalog=setup();profile=catalog['documents'][0]
 if problem=='text':pdf=replace(pdf,pages=(Page(1,'Расходы {124)','ocr'),))
 if problem=='native':pdf=replace(pdf,pages=(replace(pdf.pages[0],method='native'),))
 if problem=='review':profile['visual_review_confirmed']=False
 if problem=='line':profile['pages']['1']['bracket_lines']=[0]
 if problem=='no_bracket':profile['pages']['1']['bracket_lines']=[2]
 if problem=='duplicate':catalog['documents'].append(profile)
 with pytest.raises(ValueError):apply_reviewed_ocr_punctuation(pdf,catalog)


def test_visual_digit_transcription_is_explicit_and_does_not_change_source():
 pdf,catalog=setup();p=catalog['documents'][0]['pages']['1']
 p['visual_transcriptions']=[{'line':2,'original':'8','reviewed':'9','reviewed_image_sha256':'b'*64,'reason':'Просмотрена цифра на исходном изображении'}]
 result=apply_reviewed_ocr_punctuation(pdf,catalog)
 assert result.pages[0].text.endswith('Доход 9\n') and pdf.pages[0].text.endswith('Доход 8\n')
 audit=result.processing['visual_ocr_punctuation']['pages'][0]
 assert audit['financial_digits_modified'] and audit['visual_transcriptions']==p['visual_transcriptions']
 p['visual_transcriptions'][0]['reviewed_image_sha256']=''
 with pytest.raises(ValueError):apply_reviewed_ocr_punctuation(pdf,catalog)
