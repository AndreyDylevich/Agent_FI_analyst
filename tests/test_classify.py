"""Тип не угадывается по каталогу, периоды не путаются с публикацией, конфликты видны."""

import pytest

from agent.classify import classify_document as classify


@pytest.mark.parametrize('text,kind,standard,period', [
    ('Годовая консолидированная финансовая отчетность за 2025 год', 'ifrs', 'IFRS', 'FY'),
    ('Сокращенная промежуточная консолидированная финансовая отчётность по состоянию на 30.06.2026 г.', 'ifrs', 'IFRS', 'H1'),
    ('Бухгалтерская отчетность за 9 месяцев 2026 года', 'rsbu', 'RSBU', '9M'),
    ('Промежуточная бухгалтерская отчетность за 3 месяца 2026 года', 'rsbu', 'RSBU', 'Q1'),
    ('Бухгалтерская отчетность ПАО за 2025 год 31.12.2025', 'rsbu', 'RSBU', 'FY'),
    ('Interim condensed consolidated financial statements for the six months ended 30 June 2026', 'ifrs', 'IFRS', 'H1'),
    ('Пресс-релиз: компания объявляет результаты по МСФО за 2025 год', 'press_release', 'IFRS', 'FY'),
    ('Презентация результатов по МСФО за 2025 год', 'presentation', 'IFRS', 'FY'),
    ('Годовой отчет за 2025 год', 'annual_report', None, 'FY'),
    ('Консолидированная финансовая отчетность US GAAP за 2012 год', 'other', None, 'FY'),
])
def test_types_and_periods(text, kind, standard, period):
    result = classify(text)
    assert (result.doc_type, result.standard, result.period_type) == (kind, standard, period)
    assert result.evidence and result.basis == 'link_text'


def test_bare_link_for_segezha_requires_first_page():
    assert classify('', recognition='pdf_first_page').needs_review
    assert classify('МСФО', recognition='pdf_first_page').standard is None
    result = classify('', 'Консолидированная финансовая отчетность за 2025 год', recognition='pdf_first_page')
    assert result.doc_type == 'ifrs' and result.basis == 'pdf_first_page'


def test_title_conflict_not_silently_resolved():
    result = classify('Бухгалтерская отчетность за 2025 год', 'Консолидированная финансовая отчетность за 2025 год')
    assert result.needs_review and result.standard is None and result.basis == 'conflict'


def test_first_page_press_release_overrides_link_ifrs_label():
    result = classify('МСФО за 2025 год', 'ПРЕСС-РЕЛИЗ. Компания объявляет результаты по МСФО за 2025 год')
    assert result.doc_type == 'press_release'


def test_publication_year_does_not_replace_explicit_report_year():
    result = classify('Бухгалтерская отчетность за 2025 год, опубликовано 01.04.2026')
    assert result.standard == 'RSBU' and result.period_end == '2025-12-31'


def test_ambiguous_period_needs_review():
    assert classify('Бухгалтерская отчетность за 2025 год и за 2024 год').period_end is None


def test_press_release_comparative_table_does_not_replace_heading_period():
    text = 'Компания объявляет результаты по МСФО за 1 полугодие 2026 г. ' + 'Текст. '*100
    text += 'Данные МСФО, 6м 2026, 6м 2025'
    result = classify('', text, recognition='pdf_first_page')
    assert result.period_end == '2026-06-30' and result.doc_type == 'press_release'


def test_three_months_ended_september_are_not_first_quarter():
    assert classify('IFRS financial statements for the three months ended 30 September 2026').period_type is None


def test_simultaneous_standards_not_guessed():
    assert classify('МСФО и РСБУ за 2025 год').standard is None


def test_scanned_or_unrecognized_pdf_not_treated_as_confirmed_title():
    result = classify('Консолидированная финансовая отчетность за 2025 год', 'Нечитаемая таблица')
    assert result.needs_review and result.doc_type == 'other'


def test_explicit_annual_comparative_year_list_is_not_publication_year_guess():
    text='Консолидированная финансовая отчетность на 31 декабря 2021 и 2020 годов и за 2021, 2020 и 2019 годы'
    result=classify('',text,recognition='pdf_first_page')
    assert result.period_start=='2021-01-01' and result.period_end=='2021-12-31' and not result.needs_review
    assert classify(text.replace('на 31 декабря 2021','на 31 декабря 2022')).period_end is None
    assert classify('Консолидированная финансовая отчетность за 2021, 2023 и 2020 годы').period_end is None
