"""Проверенный состав движения долга конкретного PDF. Финансовых чисел здесь нет."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from agent import config
from agent.extract.pdf_text import PDFText
from agent.extract.schemas import citation, date_is_cited
from agent.metrics.engine import Period

CATALOG = Path(__file__).resolve().parents[2] / 'config/ifrs_debt_bridges.json'
DEBT_RULES = {'complete_bonds_rollforward', 'complete_loans_rollforward', 'complete_debt_rollforward'}
DEBT_CASH_RULES = {'debt_bonds_issued_vs_cash_flow', 'debt_bonds_repaid_vs_cash_flow',
                   'debt_loans_raised_vs_cash_flow', 'debt_loans_repaid_vs_cash_flow'}


@dataclass(frozen=True)
class DebtBridgePlan:
    start: date
    end: date
    rule: str
    terms: dict[str, int]
    bindings: dict[str, dict]
    basis: dict


def compile_debt_bridges(pdf: PDFText, period: Period, catalog: dict | None = None) -> tuple[DebtBridgePlan, ...]:
    """Полнота охватывает обе раскрытые категории долга; аренда остаётся отдельно."""
    catalog = json.loads(CATALOG.read_text()) if catalog is None else catalog
    if type(catalog.get('version')) is not int or catalog['version'] != 1:
        raise ValueError('Неизвестная версия структуры движения долга')
    profiles = [p for p in catalog['documents'] if p['pdf_sha256'] == pdf.sha256]
    if not profiles:
        return ()
    if len(profiles) != 1:
        raise ValueError('Неоднозначная структура движения долга')
    profile = profiles[0]
    if (profile['complete'] is not True or not profile['reviewer'].strip() or not profile['reason'].strip()
            or (profile['report_start'], profile['report_end'], profile['report_kind']) !=
               (str(period.start), str(period.end), period.kind)
            or profile['number_format'] not in ('comma_grouped_dot_decimal', 'space_grouped_decimal')):
        raise ValueError('Движение долга не подтверждено для точного окна выпуска')
    pages = {p.number: p for p in pdf.pages}
    items = config.line_items()
    components = profile['components']
    if (len(components) != 2 or {p['balance_item'] for p in components} !=
            {'note_bonds', 'note_loans_and_other_borrowings'}):
        raise ValueError('Нужны оба раскрытых компонента полного долга без аренды')
    composition = profile['composition']
    if set(composition) != {'note_bonds', 'note_loans_and_other_borrowings', 'note_debt_total'}:
        raise ValueError('Не подтверждён полный состав общего долга')
    for proof in composition.values():
        source = citation(proof, pages)
        if pages[source.page].method != 'native':
            raise ValueError('Структура движения долга требует нативного текста')
    result, seen_lines, seen_movements = [], set(), set()
    digest = hashlib.sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    basis = {'pdf_sha256': pdf.sha256, 'plan_sha256': digest, 'reviewer': profile['reviewer'],
             'source_structure_reviewed': True, 'number_format': profile['number_format'],
             'debt_excludes_leases': True, 'composition_evidence': composition}
    for component in components:
        item = component['balance_item']
        rule = {'note_bonds': 'complete_bonds_rollforward',
                'note_loans_and_other_borrowings': 'complete_loans_rollforward'}[item]
        if component['rule'] != rule:
            raise ValueError('Неверное имя моста категории долга')
        region = component['evidence']
        citation(region, pages, max_lines=80, max_chars=20000)
        terms, bindings, occupied, kinds = {}, {}, set(), []
        previous = region['start_line'] - 1
        for row in component['rows']:
            proof = row['evidence']; source = citation(proof, pages)
            if (proof['page'] != region['page'] or proof['start_line'] <= previous
                    or proof['start_line'] < region['start_line'] or proof['end_line'] > region['end_line']
                    or pages[source.page].method != 'native'):
                raise ValueError('Нарушен порядок/адрес строк движения долга')
            previous = proof['end_line']
            lines = set(range(proof['start_line'], proof['end_line'] + 1))
            addresses = {(source.page, n) for n in lines}
            if addresses & seen_lines:
                raise ValueError('Повторная строка в структуре движения долга')
            seen_lines |= addresses; occupied |= lines
            kind = row['kind']; kinds.append(kind)
            if kind == 'heading':
                if 'item_id' in row or source.quote.strip() != 'Выпуск новых облигаций':
                    raise ValueError('Денежную строку нельзя скрыть как заголовок движения долга')
                continue
            row_item = row['item_id']
            if row_item not in items or items[row_item]['statement'] != 'notes':
                raise ValueError('В мосте долга нужна известная статья примечаний')
            if kind in ('opening', 'closing'):
                day = period.start - timedelta(days=1) if kind == 'opening' else period.end
                if row_item != item or not config.is_stock(items[row_item]) or not date_is_cited(source.quote, day):
                    raise ValueError('Остаток движения долга не подтверждён на нужную дату')
                key = f'{kind}:{item}'
                start, end = None, day
                coefficient = 1 if kind == 'opening' else -1
            elif kind == 'movement':
                if (row_item in seen_movements or config.is_stock(items[row_item])
                        or items[row_item].get('debt_bridge_role') != 'movement'
                        or items[row_item]['sign'] != 'natural'):
                    raise ValueError('Повторное/неверное движение долга или неподтверждённый знак')
                seen_movements.add(row_item)
                key, start, end, coefficient = row_item, period.start, period.end, 1
            else:
                raise ValueError('Неизвестная роль строки движения долга')
            if key in terms:
                raise ValueError('Повторный остаток в мосте долга')
            terms[key] = coefficient
            bindings[key] = {'item_id': row_item, 'start': start, 'end': end, 'evidence': proof}
        required = {n for n in range(region['start_line'], region['end_line'] + 1)
                    if pages[region['page']].text.splitlines()[n-1].strip()}
        if (not required <= occupied or not kinds or kinds[0] != 'opening' or kinds[-1] != 'closing'
                or kinds.count('opening') != 1 or kinds.count('closing') != 1 or 'movement' not in kinds):
            raise ValueError('Неполный состав строк движения долга')
        result.append(DebtBridgePlan(period.start, period.end, rule, terms, bindings,
                                    {**basis, 'source_evidence': region}))
    # Общий мост складывает два непересекающихся моста. Ошибка одного не скрывается другим.
    result.append(DebtBridgePlan(period.start, period.end, 'complete_debt_rollforward',
                                {k: v for p in result for k, v in p.terms.items()},
                                {k: v for p in result for k, v in p.bindings.items()}, basis))
    movements = result[-1].bindings
    links = profile['cash_links']
    if len(links) != 4 or {p['rule'] for p in links} != DEBT_CASH_RULES:
        raise ValueError('Нужны четыре отдельные связи движения долга с ОДДС')
    linked = set()
    expected_cash_items = {'debt_bonds_issued_vs_cash_flow': 'cf_bonds_issued',
                           'debt_bonds_repaid_vs_cash_flow': 'cf_bonds_repaid',
                           'debt_loans_raised_vs_cash_flow': 'cf_loans_raised',
                           'debt_loans_repaid_vs_cash_flow': 'cf_loans_repaid'}
    for link in links:
        cash = link['cash_item']; note_items = link['note_items']
        if (not note_items or len(set(note_items)) != len(note_items) or linked & set(note_items)
                or cash != expected_cash_items[link['rule']] or items[cash]['sign'] != 'natural'
                or any(item not in movements or movements[item]['start'] is None for item in note_items)):
            raise ValueError('Неверный или повторный состав денежной связи долга')
        linked.update(note_items)
        source = citation(link['evidence'], pages)
        if pages[source.page].method != 'native':
            raise ValueError('Денежная связь долга требует нативного текста')
        bindings = {item: movements[item] for item in note_items}
        bindings[cash] = {'item_id': cash, 'start': period.start, 'end': period.end,
                          'evidence': link['evidence'], 'statement': 'cash_flow', 'cash_flow_section': 'financing'}
        result.append(DebtBridgePlan(period.start, period.end, link['rule'],
                                    {**{item: 1 for item in note_items}, cash: -1}, bindings, basis))
    return tuple(result)
