"""Проверки извлечённых групповых МСФО: полные тождества, явный допуск, адресная блокировка."""

from dataclasses import dataclass
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


RULES = {
    'assets_components': {'total_assets': 1, 'non_current_assets': -1, 'current_assets': -1},
    'balance': {'total_assets': 1, 'equity_total': -1, 'non_current_liabilities': -1, 'current_liabilities': -1},
    'balance_totals': {'total_assets': 1, 'total_equity_and_liabilities': -1},
    'gross_profit': {'revenue': 1, 'cost_of_sales': -1, 'gross_profit': -1},
    'debt_components': {'debt_long_term': 1, 'debt_short_term': 1, 'bank_loans': -1, 'bonds': -1, 'other_borrowings': -1},
}


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
        disputed = set()
        for item, indices in by_item.items():
            # Разные разделы ОДДС не являются взаимоисключающими версиями одного платежа.
            sections = {}
            for index in indices:
                sections.setdefault(facts[index].cash_flow_section, []).append(index)
            if any(len({facts[i].normalized for i in ids}) > 1 for ids in sections.values()):
                disputed.add(item)
                for index in indices:
                    statuses[index] = 'расхождение'
        for name, terms in RULES.items():
            stock = name != 'gross_profit'
            if stock != (start is None):
                continue
            indices = tuple(i for key in terms for i in by_item.get(key, []))
            if not indices:
                continue
            values = {key: facts[by_item[key][0]].normalized if key in by_item else None for key in terms}
            tolerance = max((UNIT_MULTIPLIERS[facts[i].unit] for i in indices), default=Decimal('1'))
            result = reconcile(name, values, terms, tolerance=tolerance, disputed=tuple(disputed))
            checks.append(FinancialCheck(name, start.isoformat() if start else None, end.isoformat(), result, indices))
            for index in indices:
                if result.status == 'расхождение':
                    statuses[index] = 'расхождение'
                elif result.status == 'пройдено' and statuses[index] != 'расхождение' and not facts[index].from_ocr:
                    statuses[index] = 'проверено_авто'
    return tuple(checks), tuple(statuses)
