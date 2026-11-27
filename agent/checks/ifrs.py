"""Проверки извлечённых групповых МСФО: полные тождества, явный допуск, адресная блокировка."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from agent.checks.validate import CheckResult, reconcile
from agent.extract.schemas import ExtractedFact
from agent.metrics.data import UNIT_MULTIPLIERS
from agent.checks.equity import EquityBridgePlan
from agent.checks.cash_sections import CashSectionPlan
from agent.checks.debt_bridges import DebtBridgePlan
from agent.checks.source_links import SourceReview


@dataclass(frozen=True)
class FinancialCheck:
    rule: str
    start: str | None
    end: str
    result: CheckResult
    fact_indices: tuple[int, ...]
    version: int = 11
    basis: dict | None = None


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
    'cash_flow_summary_before_fx': {'cfo':1,'cfi':1,'cff':1,'cf_cash_change_before_fx':-1},
    'cash_fx_bridge': {'cf_cash_change_before_fx':1,'cf_fx_effect':1,'cf_cash_change':-1},
    'cash_held_for_sale_bridge': {'cf_cash_closing_gross':1, 'cf_cash_held_for_sale':-1, 'cf_cash_closing_after_hfs':-1},
    'debt_components': {'debt_long_term': 1, 'debt_short_term': 1, 'bank_loans': -1, 'bonds': -1, 'other_borrowings': -1},
    'note_debt_composition': {'note_debt_total':1,'note_bonds':-1,'note_loans_and_other_borrowings':-1},
    'note_debt_maturity': {'note_debt_total':1,'note_debt_current':-1,'note_debt_noncurrent':-1},
    'note_debt_current_vs_balance': {'debt_short_term':1,'note_debt_current':-1},
    'note_debt_noncurrent_vs_balance': {'debt_long_term':1,'note_debt_noncurrent':-1},
    'note_debt_total_vs_balance': {'debt_long_term':1,'debt_short_term':1,'note_debt_total':-1},
    'note_lease_discounting': {'note_lease_undiscounted':1,'note_lease_discount':-1,'note_lease_present_value':-1},
    'note_lease_maturity': {'note_lease_present_value':1,'note_lease_current':-1,'note_lease_noncurrent':-1},
    'note_lease_current_vs_balance': {'lease_liabilities_short':1,'note_lease_current':-1},
    'note_lease_noncurrent_vs_balance': {'lease_liabilities_long':1,'note_lease_noncurrent':-1},
    'note_lease_total_vs_balance': {'lease_liabilities_long':1,'lease_liabilities_short':1,'note_lease_present_value':-1},
    'note_interest_composition': {'note_interest_total':1,'note_interest_borrowings':-1,'note_interest_leases':-1,'note_interest_unwinding':-1},
    'note_finance_gross': {'note_finance_expense_gross':1,'note_interest_total':-1,'note_finance_other_expense':-1},
    'note_finance_net': {'note_finance_expense_gross':1,'note_capitalized_interest_expense':-1,'note_finance_modification_expense':1,'note_finance_expense_net':-1},
}
FLOW_RULES = {'gross_profit','profit_attribution','profit_vs_cash_flow_base','eq_profit_attribution',
              'profit_vs_equity','profit_parent_vs_equity','profit_nci_vs_equity'}
FLOW_RULES.update(('note_interest_composition','note_finance_gross','note_finance_net'))
FLOW_RULES.add('cash_flow_summary')
FLOW_RULES.update(('cash_flow_summary_before_fx','cash_fx_bridge'))
# Начало/конец отчёта об изменениях капитала сверяются с балансом НА ТУ ЖЕ дату.
for _role in ('opening', 'closing'):
    for _component, _balance_id in (('total','equity_total'),('parent','equity_parent'),('nci','non_controlling_interests')):
        RULES[f'eq_{_role}_{_component}_vs_balance'] = {_balance_id:1, f'eq_{_role}_{_component}':-1}


def _apply_check(name, start, end, terms, by_item, facts, statuses, *, extra_disputed=(), basis=None):
    """Полный набор слагаемых проверяется точно, неполный не дополняется нулями."""
    indices = tuple(i for key in terms for i in by_item.get(key, []))
    if not indices:
        return None
    disputed = tuple(key for key in terms if key in extra_disputed or key in by_item and (
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
    return FinancialCheck(name,str(start) if start else None,str(end),result,indices,basis=basis)


def validate_financials(facts: tuple[ExtractedFact, ...], *, equity_plans: tuple[EquityBridgePlan, ...] = (), cash_plans: tuple[CashSectionPlan, ...] = (), debt_plans: tuple[DebtBridgePlan, ...] = (), source_review: SourceReview | None = None) -> tuple[tuple[FinancialCheck, ...], tuple[str, ...]]:
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
        if start is None:
            continue
        for change in ('cf_cash_change','cf_cash_change_before_fx'):
            if change not in by_item:continue
            bridge = {change:by_item[change]}
            terms={'cf_cash_opening':1,change:1,'cf_cash_closing_gross':-1}
            if change=='cf_cash_change_before_fx':
                terms['cf_fx_effect']=1
                if 'cf_fx_effect' in by_item:bridge['cf_fx_effect']=by_item['cf_fx_effect']
            for key,day in (('cf_cash_opening',start-timedelta(days=1)),('cf_cash_closing_gross',end)):
                if key in groups.get((None,day),{}):bridge[key]=groups[(None,day)][key]
            rule='cash_rollforward' if change=='cf_cash_change' else 'cash_rollforward_before_fx'
            result=_apply_check(rule,start,end,terms,bridge,facts,statuses)
            if result:checks.append(result)
    for plan in equity_plans:
        by_item = dict(groups.get((plan.start, plan.end), {}))
        for item, day in (('eq_opening_total', plan.start-timedelta(days=1)), ('eq_closing_total', plan.end)):
            if item in groups.get((None, day), {}):
                by_item[item] = groups[(None, day)][item]
        # Число принимается только из исходной строки, включённой в проверенную схему этого окна.
        for item, indices in by_item.items():
            if item not in plan.item_evidence:
                continue
            proof = plan.item_evidence[item]
            for index in indices:
                evidence = facts[index].evidence
                if (evidence.page != proof['page'] or evidence.start_line is None or evidence.end_line is None
                        or evidence.start_line < proof['start_line'] or evidence.end_line > proof['end_line']):
                    raise ValueError('Факт движения капитала взят не из строки проверенного полного моста')
        disputed = []
        for index, members in enumerate(plan.neutral_groups, 1):
            neutral = _apply_check(f'equity_neutral_transfer_{index}', plan.start, plan.end,
                                   dict.fromkeys(members, 1), by_item, facts, statuses, basis=plan.basis)
            if neutral:
                checks.append(neutral)
                if neutral.result.status == 'расхождение':
                    disputed.extend(members)
        # Начальный капитал + все раскрытые движения (со знаком) − конечный капитал = 0.
        terms = {'eq_opening_total': 1, **dict.fromkeys(plan.movements, 1), 'eq_closing_total': -1}
        bridge = _apply_check('complete_equity_rollforward', plan.start, plan.end, terms, by_item, facts,
                              statuses, extra_disputed=tuple(disputed), basis=plan.basis)
        if bridge:
            checks.append(bridge)
    for plan in cash_plans:
        by_item = {item:[index for index in indices if facts[index].statement=='cash_flow']
                   for item,indices in groups.get((plan.start,plan.end),{}).items()}
        by_item = {item:indices for item,indices in by_item.items() if indices}
        for item in plan.terms:
            proof = plan.item_evidence[item]
            for index in by_item.get(item, []):
                if item in plan.unavailable_items:
                    raise ValueError('Прочерк этой колонки ОДДС не является числом и не заменяется соседним годом/нулём')
                fact = facts[index]; evidence = fact.evidence
                if (fact.statement != 'cash_flow' or fact.cash_flow_section != plan.expected_sections[item]
                        or evidence.page != proof['page'] or evidence.start_line is None or evidence.end_line is None
                        or evidence.start_line < proof['start_line'] or evidence.end_line > proof['end_line']):
                    raise ValueError('Статья ОДДС взята из другого раздела/строки проверенного состава')
        check = _apply_check(f'complete_{plan.total_item}_components',plan.start,plan.end,plan.terms,
                             by_item,facts,statuses,basis=plan.basis)
        if check:
            checks.append(check)
    for plan in debt_plans:
        by_item = {}
        for key, binding in plan.bindings.items():
            indices = [i for i, f in enumerate(facts) if f.item_id == binding['item_id']
                       and f.start == binding['start'] and f.end == binding['end']]
            for index in indices:
                fact = facts[index]; evidence = fact.evidence
                proofs = [binding['evidence']]
                # Тот же датированный остаток также раскрыт в сводной таблице примечания.
                if fact.start is None:
                    proofs.append(plan.basis['composition_evidence'][fact.item_id])
                if (fact.statement != binding.get('statement', 'notes')
                        or fact.cash_flow_section != binding.get('cash_flow_section') or not any(
                        evidence.page == p['page'] and evidence.start_line is not None and evidence.end_line is not None
                        and evidence.start_line >= p['start_line'] and evidence.end_line <= p['end_line'] for p in proofs)):
                    raise ValueError('Статья движения долга взята из другого источника/строки')
            if indices:
                by_item[key] = indices
            if binding['start'] is not None and binding.get('statement', 'notes') == 'notes' and any(f.item_id == binding['item_id'] and
                    (f.start, f.end) != (binding['start'], binding['end']) for f in facts):
                raise ValueError('В проверенном источнике нет движения долга за другой период')
        # Две компенсирующие ошибки компонентных мостов не разрешают общий мост.
        disputed = tuple(key for key, indices in by_item.items()
                         if any(statuses[i] == 'расхождение' for i in indices))
        check = _apply_check(plan.rule, plan.start, plan.end, plan.terms, by_item, facts,
                             statuses, extra_disputed=disputed, basis=plan.basis)
        if check:
            checks.append(check)
    if source_review:
        for window in (source_review.period, source_review.period.shift_years(-1)):
            by_alias = {}
            for alias, binding in source_review.inputs.items():
                indices = [i for i, f in enumerate(facts) if f.item_id == binding['item_id']
                           and f.statement == binding['statement'] and (f.start, f.end) == (window.start, window.end)]
                proof = binding['evidence']
                for i in indices:
                    evidence = facts[i].evidence
                    if binding.get('raw_sign') == 'nonnegative' and facts[i].value < 0:
                        raise ValueError('Нужна положительная вложенная сумма капитализированных процентов, не отрицательная сумма приобретения ОС/НМА')
                    if (facts[i].cash_flow_section != binding['cash_flow_section'] or evidence.page != proof['page']
                            or evidence.start_line is None or evidence.end_line is None
                            or evidence.start_line < proof['start_line'] or evidence.end_line > proof['end_line']):
                        raise ValueError('Вход проверенной связи форм взят из другой строки/раздела')
                if indices:
                    by_alias[alias] = indices
            for rule, terms in source_review.checks.items():
                disputed = tuple(key for key in terms if any(statuses[i] == 'расхождение' for i in by_alias.get(key, [])))
                check = _apply_check(rule, window.start, window.end, terms, by_alias, facts,
                                     statuses, extra_disputed=disputed, basis=source_review.basis)
                if check:
                    checks.append(check)
    return tuple(checks), tuple(statuses)
