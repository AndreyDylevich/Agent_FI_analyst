"""Строгий контракт извлечения: исходные числа/цитаты; статусы, единицы и расчёты проверяет Python."""

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from agent.extract.pdf_text import Page
from agent.config import is_stock
from agent.metrics.data import UNIT_MULTIPLIERS, normalize_money
from agent.metrics.engine import Period

VERSION = 4


def _object(properties: dict) -> dict:
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


STRING = {'type': 'string'}
NULLABLE_STRING = {'type': ['string', 'null']}
CITATION = _object({'page': {'type': 'integer'}, 'quote': STRING})


def extraction_schema(item_ids: list[str], *, stock_only: bool = False, flow_only: bool = False) -> dict:
    """Схема выдачи модели; денежные значения обязательно строки, не JSON float."""
    fact = _object({'item_id': {'type': 'string', 'enum': item_ids}, 'original_name': STRING,
                    'raw_value': STRING,
                    'number_format': {'type': 'string', 'enum': ['space_grouped_decimal', 'comma_grouped_dot_decimal']},
                    'unit': {'type': 'string', 'enum': list(UNIT_MULTIPLIERS)},
                    'unit_evidence': CITATION, 'evidence': CITATION,
                    'column_header': {'type': 'string', 'pattern': '^20[0-9]{2}$'},
                    'period_start': {'type': 'null'} if stock_only else (STRING if flow_only else NULLABLE_STRING), 'period_end': STRING,
                    'period_type': {'type': 'string', 'enum': ['AT_DATE'] if stock_only else (['FY', 'Q1', 'H1', '9M'] if flow_only else ['AT_DATE', 'FY', 'Q1', 'H1', '9M'])},
                    'statement': {'type': 'string', 'enum': ['balance', 'income', 'cash_flow', 'notes', 'equity']},
                    'cash_flow_section': {'type': ['string', 'null'],
                                          'enum': [None, 'operating', 'investing', 'financing', 'mixed']}})
    return _object({'ticker': STRING, 'entity_name': STRING, 'identity_evidence': CITATION,
                    'standard': {'type': 'string', 'enum': ['IFRS']}, 'standard_evidence': CITATION,
                    'scope': {'type': 'string', 'enum': ['group']}, 'scope_evidence': CITATION,
                    'currency': {'type': 'string', 'enum': ['RUB']}, 'currency_evidence': CITATION,
                    'period_start': STRING, 'period_end': STRING,
                    'period_type': {'type': 'string', 'enum': ['FY', 'Q1', 'H1', '9M']},
                    'period_evidence': CITATION, 'facts': {'type': 'array', 'items': fact},
                    'warnings': {'type': 'array', 'items': STRING}})


def normalized_text(text: str) -> str:
    """Нормализуется только пробельное оформление; цифры, знаки и буквы не исправляются."""
    return ' '.join(text.split())


def period_is_cited(quote: str, period: Period) -> bool:
    """Год без даты/длительности не подтверждает окно отчётности."""
    text = normalized_text(quote).lower()
    month = {3: r'март[а-я]*|march', 6: r'июн[а-я]*|june', 9: r'сентябр[а-я]*|september',
             12: r'декабр[а-я]*|december'}[period.end.month]
    end = period.end
    date_pattern = rf'{end.day}\s*(?:{month})\s*{end.year}|{end.day}[./]0?{end.month}[./]{end.year}'
    duration = {'Q1': r'3\s+месяц|три\s+месяц|three.month|первый квартал',
                'H1': r'6\s+месяц|шесть\s+месяц|six.month|полугод',
                '9M': r'9\s+месяц|девять\s+месяц|nine.month',
                'FY': rf'за\s+{end.year}\s*год|годов|год.{{0,15}}закончивш|year ended'}[period.kind]
    return (bool(re.search(date_pattern, text)) or str(end.year) in text) and bool(re.search(duration, text))


def parse_amount(raw: str, number_format: str = 'space_grouped_decimal') -> Decimal:
    """Скобки/минус и пробелы тысяч сохраняют смысл; прочерк не является числом."""
    text = normalized_text(raw).replace('−', '-')
    if text.startswith('(') and text.endswith(')'):
        text = '-'+text[1:-1].strip()
    if number_format == 'comma_grouped_dot_decimal':
        if not re.fullmatch(r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?', text):
            raise ValueError('Число не соответствует заявленному формату с запятыми тысяч')
        return Decimal(text.replace(',', ''))
    if number_format != 'space_grouped_decimal':
        raise ValueError('Неизвестный формат числа')
    if not re.fullmatch(r'[+-]?(?:\d{1,3}(?: \d{3})+|\d+)(?:[.,]\d+)?', text):
        raise ValueError('Исходное значение не является однозначным числом')
    if re.search(r'[.,]\d{3}$', text):
        raise ValueError('Неоднозначный разделитель: три знака могут означать тысячи')
    return Decimal(text.replace(' ', '').replace(',', '.'))


def _keys(data: dict, names: set[str]) -> None:
    if not isinstance(data, dict) or set(data) != names:
        raise ValueError('Ответ не соответствует строгой схеме полей')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Повторяющийся ключ JSON')
        result[key] = value
    return result


@dataclass(frozen=True)
class Citation:
    page: int
    quote: str


def citation(data: dict, pages: dict[int, Page]) -> Citation:
    """Цитата должна действительно находиться на переданной физической странице."""
    _keys(data, {'page', 'quote'})
    number, quote = data['page'], data['quote']
    if type(number) is not int or number not in pages or pages[number].method == 'unreadable':
        raise ValueError('Цитата ссылается на отсутствующую/нечитаемую страницу')
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 4000:
        raise ValueError('Нужна непустая короткая цитата')
    if normalized_text(quote) not in normalized_text(pages[number].text):
        matches = [n for n, page in pages.items() if normalized_text(quote) in normalized_text(page.text)]
        hint = f'; точное совпадение на странице {matches[0]}' if len(matches) == 1 else ''
        raise ValueError(f'Цитата не найдена на странице {number}{hint}')
    # Восстанавливаем исходные пробелы PDF: межколоночный промежуток нельзя принять за разделитель тысяч.
    pattern = r'\s+'.join(re.escape(part) for part in quote.split())
    original = re.search(pattern, pages[number].text)
    if original is None:
        raise ValueError('Не удалось восстановить исходный фрагмент страницы')
    return Citation(number, original.group())


@dataclass(frozen=True)
class ExtractedFact:
    item_id: str
    original_name: str
    raw_value: str
    number_format: str
    value: Decimal
    normalized: Decimal
    unit: str
    start: date | None
    end: date
    period_type: str
    statement: str
    evidence: Citation
    unit_evidence: Citation
    column_header: str
    cash_flow_section: str | None
    from_ocr: bool


@dataclass(frozen=True)
class Extraction:
    context: dict
    facts: tuple[ExtractedFact, ...]
    warnings: tuple[str, ...]


def parse_extraction(content: str, pages: tuple[Page, ...], *, ticker: str, period: Period,
                     entity_token: str, line_items: dict) -> Extraction:
    """Проверяет структуру, источники и периоды. Совпадение цитаты не доказывает выбор колонки."""
    def reject_float(value):
        raise ValueError('JSON float/NaN запрещены')
    data = json.loads(content, object_pairs_hook=_unique_object, parse_float=reject_float, parse_constant=reject_float)
    schema = extraction_schema(list(line_items))
    _keys(data, set(schema['properties']))
    by_number = {p.number: p for p in pages}
    if len(by_number) != len(pages):
        raise ValueError('Повтор физического номера страницы')
    if data['ticker'] != ticker or data['standard'] != 'IFRS' or data['scope'] != 'group' or data['currency'] != 'RUB':
        raise ValueError('Компания/стандарт/охват/валюта не совпадают с ожидаемыми')
    if (data['period_start'], data['period_end'], data['period_type']) != (period.start.isoformat(), period.end.isoformat(), period.kind):
        raise ValueError('Отчётный период не совпадает с ожидаемым')
    contexts = {key: citation(data[key], by_number) for key in ('identity_evidence', 'standard_evidence',
                 'scope_evidence', 'currency_evidence', 'period_evidence')}
    identity = normalized_text(contexts['identity_evidence'].quote).casefold()
    if not isinstance(data['entity_name'], str) or not data['entity_name'].strip():
        raise ValueError('Не извлечено название компании')
    if entity_token.casefold() not in identity or normalized_text(data['entity_name']).casefold() not in identity:
        raise ValueError('Название компании не подтверждено титулом')
    if not re.search(r'мсфо|\bifrs\b|международн.{0,30}стандарт.{0,45}финансов.{0,25}отчет|international financial reporting', contexts['standard_evidence'].quote, re.I|re.S):
        raise ValueError('Нет явного подтверждения МСФО')
    if not re.search(r'консолидир|consolidated|групп|group|и (?:его |ее |её )?дочерни|and.{0,15}subsidiar', contexts['scope_evidence'].quote, re.I):
        raise ValueError('Нет подтверждения группового охвата')
    if not re.search(r'руб|rouble|ruble|RUB', contexts['currency_evidence'].quote, re.I):
        raise ValueError('Нет подтверждения валюты RUB')
    if not period_is_cited(contexts['period_evidence'].quote, period):
        raise ValueError('Окно отчётного периода не подтверждено датой/длительностью')
    if not isinstance(data['facts'], list) or len(data['facts']) > 250:
        raise ValueError('Нужен ограниченный список исходных значений')
    facts, seen = [], set()
    units = {'руб.': r'(?<!тыс\. )(?<!млн )рубл|\broubles\b|\brubles\b',
             'тыс. руб.': r'тыс|thousand', 'млн руб.': r'миллион|млн|million'}
    for row in data['facts']:
        _keys(row, set(schema['properties']['facts']['items']['properties']))
        item_id = row['item_id']
        if item_id not in line_items:
            raise ValueError('Неизвестная каноническая статья')
        item = line_items[item_id]
        evidence, unit_evidence = citation(row['evidence'], by_number), citation(row['unit_evidence'], by_number)
        name, raw, unit = row['original_name'], row['raw_value'], row['unit']
        if not all(isinstance(v, str) and v.strip() for v in (name, raw, row['column_header'])):
            raise ValueError('Нужны подпись строки, число и заголовок колонки')
        quote = normalized_text(evidence.quote)
        if row['number_format'] == 'comma_grouped_dot_decimal':
            body = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
        else:
            body = r'(?:\d{1,3}(?: \d{3})+|\d+)(?:[.,]\d+)?'
        number_pattern = r'(?<![\w.,])(?:\(' + body + r'\)|[+\-−]?' + body + r')(?![\w.,])'
        original_numbers = evidence.quote.replace('\u00a0', ' ').replace('\u202f', ' ')
        tokens = [match.group() for match in re.finditer(number_pattern, original_numbers)]
        if normalized_text(name).casefold() not in quote.casefold() or normalized_text(raw) not in tokens:
            raise ValueError('Подпись/число отсутствуют в цитате строки')
        if normalized_text(row['column_header']).casefold() not in normalized_text(by_number[evidence.page].text).casefold():
            raise ValueError(f"{item_id}: фрагмент заголовка колонки не найден; используй дословный фрагмент с годом, например '2025', не собирай дату из разнесённых строк")
        if unit not in units or not re.search(units[unit], unit_evidence.quote, re.I):
            raise ValueError('Единица не подтверждена цитатой')
        if unit == 'руб.' and re.search(r'тыс|миллион|млн|thousand|million', unit_evidence.quote, re.I):
            raise ValueError('Исходный масштаб не равен рублям')
        value = parse_amount(raw, row['number_format'])
        if item_id == 'income_tax' and value > 0 and not re.search(r'расход|expense|charge', name, re.I):
            raise ValueError('Положительная нейтральная строка налога не подтверждает расход; проверь налоговый доход')
        if item_id == 'income_tax_benefit' and value < 0:
            raise ValueError('Отрицательная строка не подтверждает положительный налоговый доход')
        start = date.fromisoformat(row['period_start']) if row['period_start'] is not None else None
        end = date.fromisoformat(row['period_end'])
        column_year = end.year + 1 if item.get('date_role') == 'cash_opening' else end.year
        if end > period.end or str(column_year) not in row['column_header']:
            raise ValueError(f'{item_id}: дата {end} требует года колонки {column_year}; проверь reporting_windows/cash_stock_dates')
        if is_stock(item) != (start is None):
            raise ValueError('Остаток/поток не соответствует периоду статьи')
        if item.get('date_role'):
            windows = (period, period.shift_years(-1))
            allowed_dates = {w.start - timedelta(days=1) if item['date_role']=='cash_opening' else w.end for w in windows}
            if end not in allowed_dates:
                raise ValueError('Дата остатка ОДДС не соответствует началу/концу отчётного окна')
        if start:
            Period(start, end, row['period_type'])
            kind = row['period_type']
            if (start,end,kind) not in {(w.start,w.end,w.kind) for w in (period,period.shift_years(-1))}:
                raise ValueError('Поток не относится к отчётному или аналогичному сравнительному окну')
        else:
            kind = row['period_type']
            if kind == 'AT_DATE':
                kind = {(3,31):'Q1',(6,30):'H1',(9,30):'9M',(12,31):'FY'}.get((end.month,end.day))
            Period(date(end.year, 1, 1), end, kind)
        allowed = {item['statement'], 'notes'}
        if item_id == 'd_and_a':
            allowed.add('cash_flow')
        if row['statement'] not in allowed:
            raise ValueError('Статья извлечена из несовместимой формы')
        section = row['cash_flow_section']
        if section not in (None, 'operating', 'investing', 'financing', 'mixed') or (section and (item['statement'] != 'cash_flow' or is_stock(item))):
            raise ValueError('Несогласованный раздел ОДДС')
        key = (item_id, start, end, section)
        if key in seen:
            raise ValueError('Повтор статьи/периода: нельзя молча складывать итог и компоненты')
        seen.add(key)
        facts.append(ExtractedFact(item_id, name, raw, row['number_format'], value, normalize_money(str(value), unit, item['sign']), unit,
                                   start, end, kind, row['statement'], evidence, unit_evidence,
                                   row['column_header'], section,
                                   any(by_number[c.page].method == 'ocr' for c in (evidence, unit_evidence, *contexts.values()))))
    if not isinstance(data['warnings'], list) or any(not isinstance(w, str) for w in data['warnings']):
        raise ValueError('Неверный список ограничений извлечения')
    return Extraction({k: v for k, v in data.items() if k not in ('facts', 'warnings')}, tuple(facts), tuple(data['warnings']))
