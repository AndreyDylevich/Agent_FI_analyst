"""Физические номера, OCR без замены пустоты нулями, поиск форм и пакеты без обрезки."""

import io

import pytest
from pypdf import PdfWriter

from agent.extract.pdf_text import PDFText, Page, find_sections, page_batches, read_pdf


def blank_pdf(tmp_path, count=2, password=None):
    writer = PdfWriter()
    for _ in range(count): writer.add_blank_page(width=100, height=100)
    if password is not None: writer.encrypt(password, algorithm='AES-256')
    path = tmp_path/'scan.pdf'
    with path.open('wb') as stream: writer.write(stream)
    return path


def test_blank_pages_explicitly_unreadable(tmp_path):
    result = read_pdf(blank_pdf(tmp_path))
    assert [p.number for p in result.pages] == [1, 2]
    assert all(p.method == 'unreadable' for p in result.pages) and len(result.warnings) == 2


def test_ocr_limit_does_not_silently_drop_pages(tmp_path):
    calls = []
    def ocr(path, number):
        calls.append(number)
        return 'Консолидированный отчет о финансовом положении. Итого активы 100.'
    result = read_pdf(blank_pdf(tmp_path), use_ocr=True, max_ocr_pages=1, ocr=ocr)
    assert calls == [1] and result.pages[0].method == 'ocr'
    assert result.pages[1].method == 'unreadable' and result.warnings


def test_ocr_failure_retains_warning(tmp_path):
    def ocr(*a): raise ValueError('Нет OCR')
    result = read_pdf(blank_pdf(tmp_path, 1), use_ocr=True, ocr=ocr)
    assert result.pages[0].method == 'unreadable' and result.warnings


def test_encrypted_pdf_empty_password_supported(tmp_path):
    assert read_pdf(blank_pdf(tmp_path, password='')).pages
    with pytest.raises(ValueError): read_pdf(blank_pdf(tmp_path, password='secret'))


def test_contents_not_treated_as_financial_form():
    result = find_sections(PDFText('sha', (Page(1, 'Содержание. Отчет о финансовом положении'),
        Page(5, 'Консолидированный отчет о финансовом положении'),
        Page(8, 'Консолидированный отчет о движении денежных средств'))))
    assert result.groups['balance'] == (5,) and result.groups['cash_flow'] == (8,)
    assert result.missing_forms == ('income', 'equity')


def test_batches_keep_page_numbers_and_full_text():
    pages = tuple(Page(n, 'a'*40) for n in range(1, 5))
    result = page_batches(pages, max_chars=90, max_pages=3)
    assert [[p.number for p in batch] for batch in result] == [[1,2],[3,4]]
    assert sum(len(p.text) for batch in result for p in batch) == 160
    with pytest.raises(ValueError): page_batches((Page(1, 'a'*100),), max_chars=90)
    with pytest.raises(ValueError): page_batches((Page(1, '', 'unreadable'),))


def test_notes_include_continuations_and_exclude_related_party_subset():
    prefix='Примечания к консолидированной отчетности\n'
    pdf=PDFText('sha',(Page(10,prefix+'9. ДОЛГОВЫЕ ОБЯЗАТЕЛЬСТВА\nКредиты и облигации'),
        Page(11,prefix+'Продолжение таблицы. Облигации и курсовые разницы'),
        Page(12,prefix+'10. ФИНАНСОВЫЕ ДОХОДЫ И РАСХОДЫ\nПроценты'),
        Page(13,prefix+'11. АРЕНДА\nПрочее'),
        Page(14,prefix+'16. СВЯЗАННЫЕ СТОРОНЫ\nКредиты и займы связанным сторонам')))
    selected=find_sections(pdf)
    assert selected.groups['debt']==(10,11,12)
    assert selected.groups['interest']==(12,13)


def test_singular_profit_heading_and_wrapped_policy_note_are_found():
    selected=find_sections(PDFText('sha',(
        Page(6,'Промежуточный консолидированный отчет о прибыли или убытке и прочем совокупном доходе'),
        Page(10,'Примечания к отчетности\n2. ПРИНЦИПЫ ПОДГОТОВКИ И СУЩЕСТВЕННЫЕ ПОЛОЖЕНИЯ УЧЕТНОЙ\nПОЛИТИКИ'),
        Page(11,'Примечания к отчетности\nПродолжение учётной политики'))))
    assert selected.groups['income']==(6,) and selected.groups['basis']==(10,11)
