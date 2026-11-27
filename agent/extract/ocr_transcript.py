"""Явный журнал визуальных исправлений OCR; исходный PDF и исходный OCR сохраняются."""

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

from agent.extract.pdf_text import PDFText

CATALOG = Path(__file__).resolve().parents[2] / 'config/ifrs_ocr_transcripts.json'


def apply_reviewed_ocr_punctuation(pdf: PDFText, catalog: dict | None = None) -> PDFText:
    catalog = json.loads(CATALOG.read_text()) if catalog is None else catalog
    if catalog.get('version') != 1:
        raise ValueError('Неизвестная версия визуальной правки OCR')
    profiles = [p for p in catalog['documents'] if p['pdf_sha256'] == pdf.sha256]
    if not profiles:
        return pdf
    if len(profiles) != 1:
        raise ValueError('Неоднозначный журнал OCR')
    profile = profiles[0]
    if (profile.get('visual_review_confirmed') is not True or not profile.get('reviewer', '').strip()
            or not profile.get('reason', '').strip()):
        raise ValueError('Исправление OCR требует визуального подтверждения')
    pages = {p.number: p for p in pdf.pages}
    changed, audit, unavailable = {}, [], []
    for address, review in profile['pages'].items():
        number = int(address)
        source = pages.get(number)
        if source is not None and source.method == 'unreadable':
            unavailable.append(number)
            continue
        if source is None or source.method != 'ocr':
            raise ValueError('Нет исходной OCR-страницы')
        original_hash = hashlib.sha256(source.text.encode()).hexdigest()
        if original_hash != review['original_text_sha256']:
            raise ValueError('OCR изменился; прежняя визуальная правка не применима')
        lines = source.text.splitlines(keepends=True)
        for line_number in review['bracket_lines']:
            if type(line_number) is not int or not 1 <= line_number <= len(lines):
                raise ValueError('Неверный адрес правки OCR')
            original = lines[line_number-1]
            corrected = original.translate(str.maketrans({'{':'(', '}':')'}))
            if corrected == original:
                raise ValueError('Правка OCR не меняет скобок')
            lines[line_number-1] = corrected
        transcriptions = review.get('visual_transcriptions', [])
        for correction in transcriptions:
            line_number, before, after = correction['line'], correction['original'], correction['reviewed']
            if (type(line_number) is not int or not 1 <= line_number <= len(lines)
                    or not re.fullmatch(r'[0-9][0-9 ]*', before)
                    or not re.fullmatch(r'[0-9][0-9 ]*', after) or before == after
                    or not re.fullmatch(r'[a-f0-9]{64}', correction.get('reviewed_image_sha256', ''))
                    or not correction.get('reason', '').strip()):
                raise ValueError('Числовая транскрипция требует точного адреса, изображения и причины')
            # Не ищем финансовые строки и не рассчитываем замену: только точная запись журнала.
            if len(re.findall(r'(?<![0-9])' + re.escape(before) + r'(?![0-9])', lines[line_number-1])) != 1:
                raise ValueError('Исходный токен OCR отсутствует или неоднозначен')
            lines[line_number-1] = re.sub(r'(?<![0-9])' + re.escape(before) + r'(?![0-9])',
                                        lambda _: after, lines[line_number-1])
        corrected = ''.join(lines)
        changed[number] = replace(source, text=corrected)
        audit.append({'page':number,'original_text_sha256':original_hash,
                      'reviewed_text_sha256':hashlib.sha256(corrected.encode()).hexdigest(),
                      'bracket_lines':review['bracket_lines'], 'financial_digits_modified':bool(transcriptions),
                      'visual_transcriptions':transcriptions})
    digest = hashlib.sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return replace(pdf, pages=tuple(changed.get(p.number,p) for p in pdf.pages),
                   processing={**pdf.processing, 'visual_ocr_punctuation':{
                       'reviewer':profile['reviewer'],'profile_sha256':digest,'pages':audit,
                       'unavailable_pages':unavailable,
                       'source_pdf_modified':False,'financial_values_approved':False}})
