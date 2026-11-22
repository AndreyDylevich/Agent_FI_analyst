"""Проверки извлечённых групповых МСФО: полные тождества, явный допуск, адресная блокировка."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from agent.checks.validate import CheckResult, reconcile
from agent.extract.schemas import ExtractedFact
from agent.metrics.data import UNIT_MULTIPLIERS


@dataclass(frozen=True)
class FinancialCheck:
    rule: str
    start: str | None
    end: str
    result: CheckResult
    fact_indices: tuple[int, ...]
    version: int = 5


RULES = {
    'assets_components': {'total_assets': 1, 'non_current_assets': -1, 'current_assets': -1},
    'balance': {'total_assets': 1, 'equity_total': -1, 'non_current_liabilities': -1, 'current_liabilities': -1},
    'balance_totals': {'total_assets': 1, 'total_equity_and_liabilities': -1},
    'gross_profit': {'revenue': 1, 'cost_of_sales': -1, 'gross_profit': -1},
    'profit_attribution': {'net_profit':1, 'net_profit_parent':-1, 'net_profit_nci':-1},
    'profit_vs_cash_flow_base': {'net_profit':1, 'cf_starting_net_profit':-1},
    'equity_attribution': {'equity_total':1, 'equity_parent':-1, 'non_controlling_interests':-1},
    'eq_opening_attribution': {'eq_opening_total':1, 'eq_opening_parent':-1, 'eq_opening_nci':-1},
    'eq_closing_attribution': {'eq_closing_total':1, 'eq_closing_parent':-1, 'eq_closing_nci':-1},
    'eq_profit_attribution': {'eq_profit_total':1, 'eq_profit_parent':-1, 'eq_profit_nci':-1},
    'profit_vs_equity': {'net_profit':1, 'eq_profit_total':-1},
    'profit_parent_vs_equity': {'net_profit_parent':1, 'eq_profit_parent':-1},
    'profit_nci_vs_equity': {'net_profit_nci':1, 'eq_profit_nci':-1},
    'cash_flow_summary': {'cfo':1,'cfi':1,'cff':1,'cf_fx_effect':1,'cf_cash_change':-1},
    'cash_held_for_sale_bridge': {'cf_cash_closing_gross':1, 'cf_cash_held_for_sale':-1, 'cf_cash_closing_after_hfs':-1},
    'debt_components': {'debt_long_term': 1, 'debt_short_term': 1, 'bank_loans': -1, 'bonds': -1, 'other_borrowings': -1},
    'note_debt_composition': {'note_debt_total':1,'note_bonds':-1,'note_loans_and_other_borrowings':-1},
    'note_debt_maturity': {'note_debt_total':1,'note_debt_current':-1,'note_debt_noncurrent':-1},
    'note_debt_current_vs_balance': {'debt_short_term':1,'note_debt_current':-1},
    'note_debt_noncurrent_vs_balance': {'debt_long_term':1,'note_debt_noncurrent':-1},
    'note_lease_discounting': {'note_lease_undiscounted':1,'note_lease_discount':-1,'note_lease_present_value':-1},
    'note_lease_maturity': {'note_lease_present_value':1,'note_lease_current':-1,'note_lease_noncurrent':-1},
    'note_lease_current_vs_balance': {'lease_liabilities_short':1,'note_lease_current':-1},
    'note_lease_noncurrent_vs_balance': {'lease_liabilities_long':1,'note_lease_noncurrent':-1},
    'note_interest_composition': {'note_interest_total':1,'note_interest_borrowings':-1,'note_interest_leases':-1,'note_interest_unwinding':-1},
    'note_finance_gross': {'note_finance_expense_gross':1,'note_interest_total':-1,'note_finance_other_expense':-1},
    'note_finance_net': {'note_finance_expense_gross':1,'note_capitalized_interest_expense':-1,'note_finance_modification_expense':1,'note_finance_expense_net':-1},
}
FLOW_RULES = {'gross_profit','profit_attribution','profit_vs_cash_flow_base','eq_profit_attribution',
              'profit_vs_equity','profit_parent_vs_equity','profit_nci_vs_equity'}
FLOW_RULES.update(('note_interest_composition','note_finance_gross','note_finance_net'))
FLOW_RULES.add('cash_flow_summary')
# Начало/конец отчёта об изменениях капитала сверяются с балансом НА ТУ ЖЕ дату.
for _role in ('opening', 'closing'):
    for _component, _balance_id in (('total','equity_total'),('parent','equity_parent'),('nci','non_controlling_interests')):
        RULES[f'eq_{_role}_{_component}_vs_balance'] = {_balance_id:1, f'eq_{_role}_{_component}':-1}


def _apply_check(name, start, end, terms, by_item, facts, statuses):
    """Полный набор слагаемых проверяется точно, неполный не дополняется нулями."""
    indices = tuple(i for key in terms for i in by_item.get(key, []))
    if not indices:
        return None
    disputed = tuple(key for key in terms if key in by_item and (
        len({facts[i].normalized for i in by_item[key]}) > 1
        or len({facts[i].cash_flow_section for i in by_item[key]}) > 1))
    values = {key:facts[by_item[key][0]].normalized if key in by_item else None for key in terms}
    tolerance = max(UNIT_MULTIPLIERS[facts[i].unit] for i in indices)
    result = reconcile(name,values,terms,tolerance=tolerance,disputed=disputed)
    for index in indices:
        if result.status == 'расхождение':
            statuses[index] = 'расхождение'
        elif result.status == 'пройдено' and statuses[index] != 'расхождение' and not facts[index].from_ocr:
            statuses[index] = 'проверено_авто'
    return FinancialCheck(name,str(start) if start else None,str(end),result,indices)


def validate_financials(facts: tuple[ExtractedFact, ...]) -> tuple[tuple[FinancialCheck, ...], tuple[str, ...]]:
    """Одинаковое число в разных цитатах подтверждает источник; разные числа блокируются оба.

    «Проверено авто» здесь означает совпадение цитаты и участие в пройденном тождестве,
    а не аудит всей статьи/отчётности. OCR до независимой сверки остаётся непроверенным.
    """
    statuses = ['не_проверено'] * len(facts)
    groups = {}
    for index, fact in enumerate(facts):
        groups.setdefault((fact.start, fact.end), {}).setdefault(fact.item_id, []).append(index)
    checks = []
    for (start, end), by_item in groups.items():
        for item, indices in by_item.items():
            # Разные разделы ОДДС не являются взаимоисключающими версиями одного платежа.
            sections = {}
            for index in indices:
                sections.setdefault(facts[index].cash_flow_section, []).append(index)
            if any(len({facts[i].normalized for i in ids}) > 1 for ids in sections.values()):
                for index in indices:
                    statuses[index] = 'расхождение'
        for name, terms in RULES.items():
            stock = name not in FLOW_RULES
            if stock != (start is None):
                continue
            result = _apply_check(name,start,end,terms,by_item,facts,statuses)
            if result:
                checks.append(result)
    for (start,end),by_item in groups.items():
        if start is None or 'cf_cash_change' not in by_item:
            continue
        bridge = {'cf_cash_change':by_item['cf_cash_change']}
        for key,day in (('cf_cash_opening',start-timedelta(days=1)),('cf_cash_closing_gross',end)):
            if key in groups.get((None,day),{}):
                bridge[key] = groups[(None,day)][key]
        result = _apply_check('cash_rollforward',start,end,
                              {'cf_cash_opening':1,'cf_cash_change':1,'cf_cash_closing_gross':-1},bridge,facts,statuses)
        if result:
            checks.append(result)
    return tuple(checks), tuple(statuses)
