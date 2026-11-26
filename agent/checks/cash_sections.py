"""Проверенный состав CFO/CFI/CFF для точной версии PDF; числа извлекает только провайдер."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from agent import config
from agent.extract.pdf_text import PDFText
from agent.extract.schemas import citation
from agent.metrics.engine import Period

CATALOG = Path(__file__).resolve().parents[2]/'config/ifrs_cash_sections.json'
CASH_RULES = {'complete_cfo_components', 'complete_cfi_components', 'complete_cff_components'}


@dataclass(frozen=True)
class CashSectionPlan:
    start: date
    end: date
    section: str
    total_item: str
    terms: dict[str, int]
    item_evidence: dict[str, dict]
    expected_sections: dict[str, str | None]
    unavailable_items: tuple[str, ...]
    basis: dict


def compile_cash_sections(pdf: PDFText, period: Period, catalog: dict | None = None) -> tuple[CashSectionPlan, ...]:
    """Покрытие всех строк не разрешает подменять прочерки нулями или смешивать разделы."""
    catalog = json.loads(CATALOG.read_text()) if catalog is None else catalog
    if type(catalog.get('version')) is not int or catalog['version'] != 1:
        raise ValueError('Неизвестная версия структуры разделов ОДДС')
    profiles = [profile for profile in catalog['documents'] if profile['pdf_sha256'] == pdf.sha256]
    if not profiles:
        return ()
    if len(profiles) != 1:
        raise ValueError('Неоднозначный состав разделов ОДДС')
    profile = profiles[0]
    if profile.get('number_format') not in ('comma_grouped_dot_decimal','space_grouped_decimal'):
        raise ValueError('Не подтверждён формат исходных чисел разделов ОДДС')
    if (profile['complete'] is not True or not profile['reviewer'].strip() or not profile['reason'].strip()
            or (profile['report_start'], profile['report_end'], profile['report_kind']) !=
               (str(period.start), str(period.end), period.kind)):
        raise ValueError('Состав ОДДС не подтверждён для точного окна выпуска')
    sections = profile['sections']
    if len(sections) != 3 or {section['section'] for section in sections} != {'operating', 'investing', 'financing'}:
        raise ValueError('Нужны три разных раздела CFO/CFI/CFF')
    pages = {page.number: page for page in pdf.pages}
    items = config.line_items()
    seen_items, seen_lines, result = set(), set(), []
    for section in sections:
        name = section['section']
        total = {'operating':'cfo', 'investing':'cfi', 'financing':'cff'}[name]
        if section['total_item'] != total:
            raise ValueError('Итог относится к другому разделу ОДДС')
        region = section['evidence']
        proof = citation(region, pages, max_lines=80, max_chars=20000)
        if pages[proof.page].method != 'native':
            raise ValueError('Для этой структуры нужен нативный текст; OCR требует отдельной приёмки')
        occupied, terms, evidence, expected_sections, kinds = set(), {}, {}, {}, []
        previous_line = region['start_line']-1
        for row in section['rows']:
            address = row['evidence']
            source = citation(address, pages)
            if (address['page'] != proof.page or address['start_line'] <= previous_line
                    or address['start_line'] < region['start_line'] or address['end_line'] > region['end_line']):
                raise ValueError('Нарушен порядок/диапазон строк состава ОДДС')
            previous_line = address['end_line']
            lines = set(range(address['start_line'], address['end_line']+1))
            occupied |= lines
            if {(proof.page, line) for line in lines} & seen_lines:
                raise ValueError('Одна исходная строка включена в несколько разделов ОДДС')
            seen_lines.update((proof.page, line) for line in lines)
            kind = row['kind']; kinds.append(kind)
            if kind == 'heading':
                if 'item_id' in row or not re.match(r'\s*(?:ДВИЖЕНИЕ ДЕНЕЖНЫХ|Корректировки:|Изменение операционных)', source.quote):
                    raise ValueError('Денежную строку нельзя скрыть как заголовок ОДДС')
                continue
            item = row['item_id']
            years=row.get('dash_years',[])
            if (not isinstance(years,list) or len(years)!=len(set(years))
                    or any(type(year) is not int or year not in (period.end.year,period.end.year-1) for year in years)):
                raise ValueError('Некорректные годы прочерков в составе ОДДС')
            if kind not in ('component', 'total') or item not in items or item in seen_items:
                raise ValueError('Неизвестная или повторная статья в составе ОДДС')
            if 'cash_flow' not in config.source_forms(items[item]) or config.is_stock(items[item]):
                raise ValueError('В состав потока нельзя включать остаток или несовместимую форму')
            # Амортизация хранится как положительный расход, но в косвенном CFO прибавляется.
            # Денежный отток с нормализованным модулем, напротив, вычитается из потока.
            coefficient = -1 if kind=='total' or (items[item]['sign']=='outflow' and items[item]['statement']=='cash_flow') else 1
            if (type(row['coefficient']) is not int or row['coefficient'] != coefficient
                    or (kind == 'total') != (item == total)):
                raise ValueError('Неверный знак/итог компонента ОДДС')
            terms[item] = coefficient
            evidence[item] = address
            expected_sections[item] = name if items[item]['statement']=='cash_flow' else None
            seen_items.add(item)
        if not kinds or kinds[0] != 'heading' or kinds[-1] != 'total' or kinds.count('total') != 1 or len(terms)<2:
            raise ValueError('Нужны заголовок, компоненты и один конечный итог раздела')
        lines = pages[proof.page].text.splitlines()
        required = {line for line in range(region['start_line'], region['end_line']+1) if lines[line-1].strip()}
        if not required <= occupied:
            raise ValueError('В составе ОДДС пропущены непустые строки источника')
        digest = hashlib.sha256(json.dumps({'profile':profile,'section':section}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        for window in (period, period.shift_years(-1)):
            unavailable=tuple(row['item_id'] for row in section['rows'] if window.end.year in row.get('dash_years',[]))
            result.append(CashSectionPlan(window.start, window.end, name, total, terms, evidence, expected_sections,unavailable,
                          {'pdf_sha256':pdf.sha256,'plan_sha256':digest,'reviewer':profile['reviewer'],
                           'source_evidence':region,'source_structure_reviewed':True,'dash_is_zero':False,
                           'number_format':profile['number_format']}))
    return tuple(result)
