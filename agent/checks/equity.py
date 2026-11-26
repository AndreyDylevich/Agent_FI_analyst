"""Проверенная по источнику структура полного моста капитала, без разбора числовых таблиц."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from agent import config
from agent.extract.pdf_text import PDFText
from agent.extract.schemas import citation, date_is_cited
from agent.metrics.engine import Period

CATALOG = Path(__file__).resolve().parents[2] / 'config/ifrs_equity_bridges.json'


@dataclass(frozen=True)
class EquityBridgePlan:
    start: date
    end: date
    movements: tuple[str, ...]
    neutral_groups: tuple[tuple[str, ...], ...]
    item_evidence: dict[str, dict]
    basis: dict


def compile_plans(pdf: PDFText, period: Period, catalog: dict | None = None) -> tuple[EquityBridgePlan, ...]:
    """Полнота относится к точным строкам проверенного выпуска. Пропущенная строка запрещена.

    Числа извлекает провайдер; здесь проверяются адреса, даты и состав строк. Подытог и его
    компоненты нельзя складывать вместе. Внутренний перенос требует всех числовых компонентов.
    """
    catalog = catalog if catalog is not None else json.loads(CATALOG.read_text())
    if type(catalog.get('version')) is not int or catalog['version'] != 1:
        raise ValueError('Неизвестная версия каталога структур капитала')
    profiles = [profile for profile in catalog['documents'] if profile['pdf_sha256'] == pdf.sha256]
    if not profiles:
        return ()
    if len(profiles) != 1:
        raise ValueError('Неоднозначная структура моста капитала для этой версии PDF')
    profile = profiles[0]
    if (profile['report_start'] != str(period.start) or profile['report_end'] != str(period.end)
            or profile['report_kind'] != period.kind or profile['complete'] is not True
            or not profile['reviewer'].strip() or not profile['reason'].strip()):
        raise ValueError('Структура капитала требует явной проверки состава и точного окна выпуска')
    windows = {(str(window.start), str(window.end)) for window in (period, period.shift_years(-1))}
    if {(plan['start'], plan['end']) for plan in profile['plans']} != windows or len(profile['plans']) != 2:
        raise ValueError('Нужны разные полные структуры текущего и сравнительного окон')
    pages = {page.number: page for page in pdf.pages}
    items = config.line_items()
    result = []
    for plan in profile['plans']:
        region = plan['evidence']
        proof = citation(region, pages, max_lines=80, max_chars=20000)
        if pages[proof.page].method != 'native':
            raise ValueError('Эта структура моста требует исходный нативный текст; OCR принимается отдельно')
        start, end = date.fromisoformat(plan['start']), date.fromisoformat(plan['end'])
        occupied, item_evidence, movements, neutral = set(), {}, [], []
        kinds = []
        previous_line = region['start_line']-1
        comprehensive_seen = False
        for row in plan['rows']:
            address = row['evidence']
            evidence = citation(address, pages)
            if (address['page'] != proof.page or address['start_line'] < region['start_line']
                    or address['end_line'] > region['end_line']):
                raise ValueError('Строка движения выходит за проверенный блок периода')
            if address['start_line'] <= previous_line:
                raise ValueError('Порядок строк полного моста не соответствует источнику')
            previous_line = address['end_line']
            lines = set(range(address['start_line'], address['end_line'] + 1))
            if occupied & lines:
                raise ValueError('Повторная строка/двойной счёт в составе моста капитала')
            occupied |= lines
            kind = row['kind']
            kinds.append(kind)
            ids = row['items']
            if kind == 'comprehensive_component':
                if ids or comprehensive_seen:
                    raise ValueError('Компонент уже включён в совокупный доход и не складывается повторно')
                continue
            if kind not in ('opening', 'closing', 'comprehensive', 'movement', 'neutral') or not ids:
                raise ValueError('Неизвестная или пустая строка полного моста')
            if kind == 'neutral' and len(ids) < 2:
                raise ValueError('Нейтральный перенос требует как минимум двух исходных компонентов')
            if kind != 'neutral' and len(ids) != 1:
                raise ValueError('Итог строки не складывается из неизвестного состава')
            if kind == 'opening' and (ids != ['eq_opening_total'] or not date_is_cited(evidence.quote, start - timedelta(days=1))):
                raise ValueError('Начальное сальдо не подтверждает дату начала моста')
            if kind == 'closing' and (ids != ['eq_closing_total'] or not date_is_cited(evidence.quote, end)):
                raise ValueError('Конечное сальдо не подтверждает дату окончания моста')
            if kind == 'comprehensive' and ids != ['eq_comprehensive_total']:
                raise ValueError('Нужен раскрытый итог совокупного дохода, не прибыль или сумма компонентов')
            if kind == 'comprehensive':
                comprehensive_seen = True
            if kind in ('movement', 'neutral') and not comprehensive_seen:
                raise ValueError('Операция с владельцами/перенос не относится к компонентам совокупного дохода')
            for item in ids:
                if item in item_evidence or item not in items or items[item]['statement'] != 'equity':
                    raise ValueError('Двойной счёт или неизвестная статья капитала')
                if kind in ('movement', 'neutral') and items[item].get('equity_bridge_role') != kind:
                    raise ValueError('Подытог/остаток нельзя подменить произвольным движением капитала')
                item_evidence[item] = address
            if kind not in ('opening', 'closing'):
                movements.extend(ids)
            if kind == 'neutral':
                neutral.append(tuple(ids))
        if (kinds.count('opening') != 1 or kinds.count('closing') != 1 or kinds.count('comprehensive') != 1
                or kinds[0] != 'opening' or kinds[-1] != 'closing'):
            raise ValueError('Нужны начальное сальдо, один совокупный доход и конечное сальдо')
        all_lines = pages[proof.page].text.splitlines()
        required = {line for line in range(region['start_line'], region['end_line'] + 1) if all_lines[line-1].strip()}
        if not required <= occupied:
            raise ValueError('В структуре моста пропущены непустые строки исходного блока капитала')
        digest = hashlib.sha256(json.dumps({'profile': profile, 'plan': plan}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        result.append(EquityBridgePlan(start, end, tuple(movements), tuple(neutral), item_evidence,
                      {'pdf_sha256': pdf.sha256, 'plan_sha256': digest, 'reviewer': profile['reviewer'],
                       'source_evidence': region, 'source_structure_reviewed': True}))
    return tuple(result)
