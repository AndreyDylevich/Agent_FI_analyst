"""Консервативная классификация по подписи/первой странице; URL не является признаком стандарта."""

import re
from dataclasses import dataclass
from datetime import date

VERSION = 2


@dataclass(frozen=True)
class Classification:
    """Гипотеза о документе, а не подтверждение правильности финансовых значений."""

    doc_type: str = 'other'
    standard: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    period_type: str | None = None
    evidence: str = ''
    basis: str = 'unknown'
    needs_review: bool = True


def _period(text: str) -> tuple[str | None, str | None, str | None]:
    """Период берётся из описания отчётности, не из имени файла или даты публикации."""
    # Явный период в заголовке сильнее года публикации и сравнительных колонок ниже.
    heading = text[:500]
    explicit = []
    for kind, pattern in (
        ('H1', r'за\s+(?:(?:1|первое)\s+)?полугодие\s+(20\d{2})'),
        ('9M', r'за\s+(?:9|девять)\s+месяц[а-я]*\s+(20\d{2})'),
        ('Q1', r'за\s+(?:3|три)\s+месяца\s+(20\d{2})'),
        ('FY', r'за\s+(20\d{2})\s*год'),
    ):
        explicit.extend((kind, year) for year in re.findall(pattern, heading))
    if explicit:
        if len(set(explicit)) != 1:
            return None, None, None
        kind, year = explicit[0]
        month, day = {'Q1': (3, 31), 'H1': (6, 30), '9M': (9, 30), 'FY': (12, 31)}[kind]
        return date(int(year), 1, 1).isoformat(), date(int(year), month, day).isoformat(), kind
    if (re.search(r'three.month|(?:3|три)\s+месяц', heading)
            and not re.search(r'six.month|nine.month|(?:6|9|шесть|девять)\s+месяц|полугод', heading)
            and not re.search(r'31\s*(?:март|march)|31[./]03', heading)):
        # Отдельные Q2/Q3/Q4 не являются накопленными H1/9M/FY.
        return None, None, None
    years = set(re.findall(r'\b20\d{2}\b', heading))
    # Несколько лет часто означают сравнительные колонки; титул требует ручного уточнения.
    if len(years) != 1:
        return None, None, None
    year = int(next(iter(years)))
    patterns = [('9M', r'9\s*(?:месяц|months)|девять\s+месяц|nine.month|30\s*(?:сентябр|september)|30[./]09'),
                ('H1', r'полугод|6\s*(?:месяц|months)|шесть\s+месяц|six.month|half.year|1[ -]?h|h1|30\s*(?:июн|june)|30[./]06'),
                ('Q1', r'перв[а-я]*\s+квартал|first.quarter|q1|31\s*(?:март|march)|31[./]03'),
                ('FY', r'годов|annual|за\s+20\d{2}\s*год|for.the.year|31\s*(?:декабр|december)|31[./]12')]
    kinds = {kind for kind, pattern in patterns if re.search(pattern, heading)}
    # Годовой отчёт не становится промежуточным из-за упоминания сопоставимых периодов.
    if len(kinds) != 1:
        return None, None, None
    kind = next(iter(kinds))
    month, day = {'Q1': (3, 31), 'H1': (6, 30), '9M': (9, 30), 'FY': (12, 31)}[kind]
    return date(year, 1, 1).isoformat(), date(year, month, day).isoformat(), kind


def _classify(text: str, basis: str) -> Classification:
    text = re.sub(r'\s+', ' ', text.lower().replace('ё', 'е')).strip()
    if not text:
        return Classification()
    evidence = text[:2500]
    ifrs = bool(re.search(r'мсфо|\bifrs\b|международн[а-я ]+стандарт|international financial reporting|консолидированн[а-я ]+финансов[а-я ]+отчет|consolidated.{0,45}financial statements', text))
    rsbu = bool(re.search(r'рсбу|\bras\b|российск[а-я ]+стандарт|russian accounting|бухгалтерск[а-я ()]+(?:финансов[а-я ()]+)?отчет|бухгалтерский баланс', text))
    usgaap = bool(re.search(r'us\s*gaap', text))
    standard = 'IFRS' if ifrs and not rsbu and not usgaap else ('RSBU' if rsbu and not ifrs and not usgaap else None)
    # Пресс-релиз или презентация с цифрами по МСФО не являются самой отчётностью.
    if re.search(r'пресс[ -]?релиз|press release|объявляет.{0,80}результат|announces.{0,80}results', text):
        kind = 'press_release'
    elif re.search(r'презентаци|presentation', text):
        kind = 'presentation'
    elif re.search(r'годовой отчет|annual report', text):
        kind = 'annual_report'
    elif usgaap:
        kind = 'other'
    elif standard:
        kind = 'ifrs' if standard == 'IFRS' else 'rsbu'
    else:
        kind = 'other'
    start, end, period = _period(text)
    return Classification(kind, standard, start, end, period, evidence, basis,
                          kind == 'other' or (ifrs and rsbu) or period is None)


def classify_document(link_text: str = '', first_page: str = '', *, recognition: str = 'link_text') -> Classification:
    """Предпочитает титул PDF, если он доступен; конфликт с подписью блокирует уверенную классификацию."""
    if recognition not in ('link_text', 'pdf_first_page'):
        raise ValueError('Неизвестный способ распознавания')
    title = _classify(link_text, 'link_text')
    if not first_page.strip():
        return title if recognition == 'link_text' else Classification(evidence=title.evidence)
    content = _classify(first_page, 'pdf_first_page')
    conflict = ((title.standard and content.standard and title.standard != content.standard)
                or (title.period_end and content.period_end and title.period_end != content.period_end))
    if conflict:
        return Classification(evidence=f'Конфликт подписи и PDF: {title.evidence}\n{content.evidence}', basis='conflict')
    # Нельзя выдавать финансовые результаты за отчётность из-за рекламного заголовка ссылки.
    return content if content.doc_type != 'other' else Classification(
        evidence=content.evidence, basis=content.basis)
