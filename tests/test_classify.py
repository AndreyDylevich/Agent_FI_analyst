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


def test_unknown_or_ambiguous_period_needs_review():
    result = classify('Бухгалтерская отчетность за 2025 год, опубликовано 01.04.2026')
    assert result.standard == 'RSBU' and result.period_end is None and result.needs_review


def test_simultaneous_standards_not_guessed():
    assert classify('МСФО и РСБУ за 2025 год').standard is None


def test_scanned_or_unrecognized_pdf_not_treated_as_confirmed_title():
    result = classify('Консолидированная финансовая отчетность за 2025 год', 'Нечитаемая таблица')
    assert result.needs_review and result.doc_type == 'other'
