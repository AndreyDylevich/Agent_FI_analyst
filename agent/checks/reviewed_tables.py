"""Независимо проверенная структура строк выпуска. Не извлекает финансовые числа из PDF."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from agent import config
from agent.extract.pdf_text import PDFText
from agent.extract.schemas import citation, normalized_text
from agent.metrics.engine import Period

CATALOG = Path(__file__).resolve().parents[2] / 'config/ifrs_reviewed_tables.json'


@dataclass(frozen=True)
class ReviewedDocument:
    observations: dict[str, dict]
    identities: tuple[dict, ...]
    basis: dict
    gaps: tuple[dict, ...]


def compile_reviewed_tables(pdf: PDFText, period: Period, catalog: dict | None = None) -> ReviewedDocument | None:
    """Адреса/колонки и состав подтверждает отдельный просмотр; значения возвращает модель."""
    catalog = json.loads(CATALOG.read_text()) if catalog is None else catalog
    if type(catalog.get('version')) is not int or catalog['version'] != 1:
        raise ValueError('Неизвестная версия проверенных таблиц')
    matches = [p for p in catalog['documents'] if p['pdf_sha256'] == pdf.sha256]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('Неоднозначная структура проверенных таблиц')
    profile = matches[0]
    if (profile['reviewed'] is not True or not profile['reviewer'].strip() or not profile['reason'].strip()
            or (profile['report_start'], profile['report_end'], profile['report_kind']) !=
               (str(period.start), str(period.end), period.kind)
            or profile['number_format'] not in ('space_grouped_decimal', 'comma_grouped_dot_decimal')):
        raise ValueError('Нужны проверка структуры и точное окно выпуска')
    pages = {p.number: p for p in pdf.pages}; items = config.line_items()
    for number, digest in profile['page_text_sha256'].items():
        if int(number) not in pages or hashlib.sha256(pages[int(number)].text.encode()).hexdigest() != digest:
            raise ValueError('Текст/адреса проверенной страницы изменились; нужна отдельная сверка')
    observations, seen = {}, set()
    windows = (period, period.shift_years(-1))
    stock_days = {p.end for p in windows} | {p.start - timedelta(days=1) for p in windows}
    for alias, source in profile['observations'].items():
        if not re.fullmatch(r'[a-z0-9_]+', alias):
            raise ValueError('Некорректный ключ исходного наблюдения')
        item = source['item_id']; form = source['statement']
        if item not in items or form not in config.source_forms(items[item]):
            raise ValueError('Неизвестная статья/форма проверенного наблюдения')
        start = date.fromisoformat(source['period_start']) if source['period_start'] else None
        end = date.fromisoformat(source['period_end'])
        if config.is_stock(items[item]) != (start is None):
            raise ValueError('Тип остатка/потока проверенного наблюдения не совпадает со статьёй')
        if start is None:
            if end not in stock_days or source['period_type'] != 'AT_DATE':
                raise ValueError('Неизвестная дата остатка проверенной таблицы')
            column_year = end.year + 1 if config.opening_stock(items[item]) else end.year
        else:
            if (start, end, source['period_type']) not in {(p.start, p.end, p.kind) for p in windows}:
                raise ValueError('Проверенное наблюдение относится к другому поточному окну')
            column_year = end.year
        if source['column_header'] != str(column_year):
            raise ValueError('Год колонки не совпадает с датой/ролью исходного наблюдения')
        proof = citation(source['evidence'], pages)
        if str(proof.page) not in profile['page_text_sha256']:
            raise ValueError('Страница наблюдения не имеет проверенного SHA256 текста')
        if pages[proof.page].method != 'native' and not (
                pages[proof.page].method == 'ocr' and profile.get('ocr_structure_reviewed') is True):
            raise ValueError('OCR требует отдельного подтверждения структуры; финансовая приёмка остаётся независимой')
        section = source['cash_flow_section']
        if section is not None and (items[item]['statement'] != 'cash_flow' or start is None
                                    or section not in ('operating', 'investing', 'financing', 'mixed')):
            raise ValueError('Не подтверждён раздел ОДДС исходного наблюдения')
        key = (item, form, start, end, section)
        if key in seen:
            raise ValueError('Одна пара статья/форма/период запрошена дважды')
        seen.add(key)
        if type(source.get('source_absent', False)) is not bool:
            raise ValueError('Отсутствие должно быть явным логическим признаком')
        if source.get('original_name_hints'):
            for name in source['original_name_hints']:
                if (not isinstance(name, str) or
                        (not name.strip() and not items[item].get('allow_unlabelled_source')) or
                        (not name.strip() and re.search(r'[A-Za-zА-Яа-яЁё]', proof.quote)) or
                        normalized_text(name).casefold() not in normalized_text(proof.quote).casefold()):
                    raise ValueError('Подсказка подписи не подтверждена проверенной строкой')
        if source.get('value_column'):
            column = source['value_column']
            if (set(column) != {'line', 'start_char', 'end_char'}
                    or any(type(v) is not int for v in column.values())
                    or not source['evidence']['start_line'] <= column['line'] <= source['evidence']['end_line']
                    or not 1 <= column['start_char'] <= column['end_char'] <= 24000
                    or column['start_char'] > len(pages[proof.page].text.splitlines()[column['line']-1])):
                raise ValueError('Неверный адрес проверенной денежной колонки')
        observations[alias] = {**source, 'start': start, 'end': end}
    if not observations:
        raise ValueError('Пустой выпуск не является проверкой извлечения')
    blocks = profile['complete_blocks']
    if not blocks:
        raise ValueError('Нет проверенных полных блоков источника')
    for block in blocks.values():
        region = block['evidence']; citation(region, pages, max_lines=120, max_chars=24000)
        occupied, previous = set(), region['start_line'] - 1
        for row in block['rows']:
            proof = row['evidence']; quote = citation(proof, pages).quote
            if (proof['page'] != region['page'] or proof['start_line'] <= previous
                    or proof['start_line'] < region['start_line'] or proof['end_line'] > region['end_line']):
                raise ValueError('Нарушен порядок/адрес строк полного блока')
            previous = proof['end_line']
            occupied.update(range(proof['start_line'], proof['end_line'] + 1))
            if row['kind'] == 'heading':
                if row.get('inputs') or re.search(r'\d', quote) or normalized_text(quote) != normalized_text(row['label']):
                    raise ValueError('Денежную строку нельзя скрыть как заголовок')
                continue
            if row['kind'] != 'value' or not row['inputs']:
                raise ValueError('Неизвестная/пустая финансовая строка полного блока')
            for alias in row['inputs']:
                if alias not in observations or observations[alias]['evidence'] != proof:
                    raise ValueError('Наблюдение не относится к своей проверенной строке')
            if not set(row['bridge_inputs']) <= set(row['inputs']):
                raise ValueError('Слагаемое моста отсутствует в строке источника')
        required = {n for n in range(region['start_line'], region['end_line'] + 1)
                    if pages[region['page']].text.splitlines()[n-1].strip()}
        if not required <= occupied:
            raise ValueError('Пропущены непустые строки полного блока')
    checks, keys = [], set()
    for identity in profile['identities']:
        terms = identity['terms']; rule = identity['rule']
        key = (rule, identity['start'], identity['end'])
        if (key in keys or not re.fullmatch(r'[a-z][a-z0-9_]+', rule) or not terms
                or any(k not in observations or type(v) is not int or v == 0 for k, v in terms.items())):
            raise ValueError('Повторная/неполная проверка или неизвестные слагаемые')
        keys.add(key)
        check_start = date.fromisoformat(identity['start']) if identity['start'] else None
        check_end = date.fromisoformat(identity['end'])
        if check_start is None:
            if check_end not in stock_days or any(observations[k]['start'] is not None or observations[k]['end'] != check_end for k in terms):
                raise ValueError('Сверка остатков смешивает разные даты')
        elif (check_start, check_end) not in {(p.start, p.end) for p in windows} or any(
                (observations[k]['start'] is not None and (observations[k]['start'], observations[k]['end']) != (check_start, check_end))
                or (observations[k]['start'] is None and observations[k]['end'] not in (check_start - timedelta(days=1), check_end))
                for k in terms):
            raise ValueError('Потоковая сверка смешивает разные окна/остатки')
        if rule.startswith('complete_') and not identity.get('full_block'):
            raise ValueError('Полная сверка требует проверенного полного блока')
        if identity.get('full_block'):
            block = blocks[identity['full_block']]; year = date.fromisoformat(identity['end']).year
            required = {alias for row in block['rows'] for alias in row.get('bridge_inputs', [])
                        if observations[alias]['column_header'] == str(year)}
            excluded = set(identity.get('excluded_subtotals', []))
            if set(terms) | excluded != required or set(terms) & excluded:
                raise ValueError('Полная сверка пропускает строку или дважды считает подытог')
            for subtotal in excluded:
                if not any(c is not identity and c['start'] == identity['start'] and c['end'] == identity['end']
                           and c['terms'].get(subtotal) == -1 for c in profile['identities']):
                    raise ValueError('Исключённый подытог должен иметь отдельную сверку')
        checks.append(identity)
    digest = hashlib.sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return ReviewedDocument(observations, tuple(checks),
                            {'pdf_sha256': pdf.sha256, 'plan_sha256': digest, 'reviewer': profile['reviewer'],
                             'source_structure_reviewed': True, 'number_format': profile['number_format']},
                            tuple(profile.get('unavailable_checks', [])))


def validate_reviewed_columns(facts, review: ReviewedDocument, pages) -> None:
    """Проверяет возвращённый моделью токен внутри просмотренной колонки; не извлекает суммы."""
    by_page = {p.number:p for p in pages}
    for fact in facts:
        found = [o for o in review.observations.values() if o['item_id'] == fact.item_id
                 and o['statement'] == fact.statement and o['start'] == fact.start and o['end'] == fact.end]
        if len(found) != 1 or not found[0].get('value_column'):
            continue
        source = found[0]; column = source['value_column']
        line = by_page[source['evidence']['page']].text.splitlines()[column['line']-1]
        selected = normalized_text(line[column['start_char']-1:column['end_char']])
        if not re.search(r'(?<![\w.,])' + re.escape(normalized_text(fact.raw_value)) + r'(?![\w.,])', selected):
            raise ValueError(f'{fact.item_id}: число взято из другой колонки; нужна {source.get("owner_column", "проверенная колонка")} '
                             f'в символах {column["start_char"]}–{column["end_char"]} строки {column["line"]}. '
                             'Не меняй число для равенства; выбери исходный токен нужного владельца/всей группы.')
