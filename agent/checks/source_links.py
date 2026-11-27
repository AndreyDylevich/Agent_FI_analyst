"""Проверенные связи форм и состава показателей точного PDF; суммы извлекает провайдер."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from agent import config
from agent.extract.pdf_text import PDFText
from agent.extract.schemas import citation, normalized_text
from agent.metrics.engine import Period

CATALOG = Path(__file__).resolve().parents[2] / 'config/ifrs_source_links.json'
SOURCE_RULES = {
    'depreciation_composition_vs_cash_flow': {'income_da': 1, 'cf_da': -1},
    'rou_depreciation_components': {'note_rou_network_da': 1, 'note_rou_buildings_da': 1,
                                    'note_rou_other_da': 1, 'note_rou_total_da': -1},
    'finance_expense_income_vs_note': {'income_finance_expense': 1, 'note_finance_expense_net': -1},
    'finance_expense_cash_adjustment_vs_note': {'cf_finance_expense_adjustment': 1, 'note_finance_expense_net': -1},
    'finance_income_income_vs_note': {'income_finance_income': 1, 'note_finance_income_total': -1},
    'finance_income_cash_adjustment_vs_note': {'cf_finance_income_adjustment': 1, 'note_finance_income_total': 1},
    'finance_income_components': {'note_finance_income_deposits': 1, 'note_finance_income_loans': 1,
                                  'note_finance_income_other': 1, 'note_finance_income_total': -1},
    'net_finance_expense_components': {'note_finance_expense_net': 1, 'note_finance_income_total': -1,
                                      'note_net_finance_expense': -1},
}


@dataclass(frozen=True)
class SourceReview:
    period: Period
    inputs: dict[str, dict]
    checks: dict[str, dict[str, int]]
    gaps: tuple[dict, ...]
    basis: dict


def compile_source_links(pdf: PDFText, period: Period, catalog: dict | None = None) -> SourceReview | None:
    """Равенство чисел не заменяет явное подтверждение состава ОС/НМА/прав пользования."""
    catalog = json.loads(CATALOG.read_text()) if catalog is None else catalog
    if type(catalog.get('version')) is not int or catalog['version'] != 1:
        raise ValueError('Неизвестная версия проверенных связей форм')
    profiles = [p for p in catalog['documents'] if p['pdf_sha256'] == pdf.sha256]
    if not profiles:
        return None
    if len(profiles) != 1:
        raise ValueError('Неоднозначные проверенные связи форм')
    profile = profiles[0]
    if (profile['reviewed'] is not True or not profile['reviewer'].strip() or not profile['reason'].strip()
            or (profile['report_start'], profile['report_end'], profile['report_kind']) !=
               (str(period.start), str(period.end), period.kind)
            or profile['number_format'] not in ('comma_grouped_dot_decimal', 'space_grouped_decimal')
            or profile['checks'] != SOURCE_RULES):
        raise ValueError('Связи форм/формулы не подтверждены для точного окна выпуска')
    pages = {p.number: p for p in pdf.pages}; items = config.line_items()
    bindings = profile['inputs']
    if not {key for terms in SOURCE_RULES.values() for key in terms} <= set(bindings):
        raise ValueError('В структуре связей форм пропущены обязательные входы')
    for alias, binding in bindings.items():
        item, statement = binding['item_id'], binding['statement']
        expected_form = 'income' if alias.startswith('income_') else 'cash_flow' if alias.startswith('cf_') else 'notes'
        if (item not in items or config.is_stock(items[item]) or statement != expected_form
                or statement not in config.source_forms(items[item])
                or item != ('d_and_a' if alias in ('income_da', 'cf_da') else alias)):
            raise ValueError('Неверная статья/форма проверенной связи')
        expected_section = ('investing' if alias.startswith('cf_capitalized_') else 'operating') if statement == 'cash_flow' and item != 'd_and_a' else None
        if binding['cash_flow_section'] != expected_section:
            raise ValueError('Неверный раздел ОДДС проверенной связи')
        proof = citation(binding['evidence'], pages)
        if 'raw_sign' in binding and (binding['raw_sign'] != 'nonnegative' or not alias.startswith('cf_capitalized_interest_')):
            raise ValueError('Неизвестное ограничение знака вложенной суммы')
        if 'original_name_hints' in binding and (not binding['original_name_hints'] or any(
                not isinstance(name, str) or not name.strip()
                or normalized_text(name).casefold() not in normalized_text(proof.quote).casefold()
                for name in binding['original_name_hints'])):
            raise ValueError('Подсказка подписи отсутствует в исходной строке')
        if pages[proof.page].method != 'native':
            raise ValueError('Проверенные связи требуют нативного текста; OCR принимается отдельно')
    for field in ('income_scope', 'cash_flow_scope'):
        quote = citation(profile['depreciation_scope'][field], pages).quote.lower()
        if not all(token in quote for token in ('основных средств', 'права', 'нематериальных активов')):
            raise ValueError('Не подтверждён одинаковый полный состав амортизации')
    rou = citation(profile['depreciation_scope']['rou_included'], pages).quote.lower()
    if not all(token in rou for token in ('включенная', 'амортизации', 'прибылях и убытках')):
        raise ValueError('Не подтверждено включение амортизации прав пользования в общий расход')
    expected_blocks = [{key for key in bindings if key.startswith('note_rou_')},
                       {'note_interest_borrowings', 'note_interest_leases', 'note_interest_unwinding',
                        'note_interest_total', 'note_finance_other_expense', 'note_finance_expense_gross',
                        'note_capitalized_interest_expense', 'note_finance_modification_expense', 'note_finance_expense_net'},
                       {'note_finance_income_deposits', 'note_finance_income_loans', 'note_finance_income_other',
                        'note_finance_income_total', 'note_net_finance_expense'}]
    if (len(profile['complete_blocks']) != 3 or
            {frozenset(b['inputs']) for b in profile['complete_blocks']} != {frozenset(b) for b in expected_blocks}):
        raise ValueError('Нужны три полных непересекающихся состава примечаний')
    for block in profile['complete_blocks']:
        region = block['evidence']; citation(region, pages, max_lines=80, max_chars=20000)
        if any(key not in bindings for key in block['inputs']):
            raise ValueError('В полном составе примечания отсутствует описание входа')
        occupied = set()
        proofs = [bindings[key]['evidence'] for key in block['inputs']]
        for heading in block.get('headings', []):
            quote = citation(heading, pages).quote.strip()
            if quote not in ('Процентный расход:', 'Финансовые доходы:'):
                raise ValueError('Денежную строку нельзя скрыть как заголовок примечания')
            proofs.append(heading)
        for proof in sorted(proofs, key=lambda p: p['start_line']):
            lines = set(range(proof['start_line'], proof['end_line'] + 1))
            if (proof['page'] != region['page'] or proof['start_line'] < region['start_line']
                    or proof['end_line'] > region['end_line'] or lines & occupied):
                raise ValueError('Пересечение/неверный адрес полного состава примечания')
            occupied |= lines
        required = {n for n in range(region['start_line'], region['end_line'] + 1)
                    if pages[region['page']].text.splitlines()[n-1].strip()}
        if not required <= occupied:
            raise ValueError('В составе примечания пропущены непустые строки')
    for gap in profile['unavailable_checks']:
        if gap['rule'] not in ('cash_composition_vs_balance', 'interest_expense_to_payment_bridge') or not gap['reason'].strip():
            raise ValueError('Неизвестная/необоснованная граница раскрытия')
        for proof in gap['evidence']:
            citation(proof, pages, max_lines=80, max_chars=20000)
    digest = hashlib.sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return SourceReview(period, bindings, profile['checks'], tuple(profile['unavailable_checks']),
                        {'pdf_sha256': pdf.sha256, 'plan_sha256': digest, 'reviewer': profile['reviewer'],
                         'source_structure_reviewed': True, 'number_format': profile['number_format'],
                         'depreciation_scope': profile['depreciation_scope'],
                         'interest_accrual_is_not_payment': True})
