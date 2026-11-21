"""Формальные проверки Python: тождества и сопоставимость данных.

Успех арифметики не подтверждает правильность извлечения или экономического вывода.
Правила конкретных форм, полноту сверки и происхождение входов задаёт вызывающий адаптер.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from typing import Mapping


@dataclass(frozen=True)
class CheckResult:
    """Протокол проверки; delta — сумма слагаемых, которая должна равняться нулю."""

    name: str
    status: str
    delta: Decimal | None = None
    tolerance: Decimal = Decimal('0')
    missing: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def reconcile(
    name: str, values: Mapping[str, Decimal | None], terms: Mapping[str, int], *,
    tolerance: Decimal = Decimal('0'), disputed: tuple[str, ...] = (),
) -> CheckResult:
    """Проверяет Σ(значение × коэффициент) = 0 в одной согласованной единице.

    Допуск задаётся явно в единице входов, не как процент суммы. Пропуск не равен нулю;
    конфликтующий вход запрещает успешный результат даже при формальном совпадении сумм.
    Для сверки между формами terms должен включать все раскрытые корректировки.
    """
    if not isinstance(tolerance, Decimal) or not tolerance.is_finite() or tolerance < 0:
        raise ValueError('Допуск должен быть конечным неотрицательным Decimal')
    if not terms or any(type(c) is not int or c == 0 for c in terms.values()):
        raise ValueError('Нужны непустые слагаемые с ненулевыми целыми коэффициентами')
    for key in terms:
        value = values.get(key)
        if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
            raise ValueError(f'{key}: нужен конечный Decimal либо None')
    conflicts = tuple(key for key in terms if key in disputed)
    missing = tuple(key for key in terms if values.get(key) is None)
    if conflicts:
        return CheckResult(name, 'расхождение', tolerance=tolerance, missing=missing,
                           reasons=(f"Конфликт исходных данных: {', '.join(conflicts)}",))
    if missing:
        return CheckResult(name, 'недостаточно_данных', tolerance=tolerance, missing=missing)
    # Запас точности сохраняет все разряды даже при сложении величин разного масштаба.
    numbers = [values[key] for key in terms]
    min_exp = min(n.as_tuple().exponent for n in numbers)
    max_place = max(n.adjusted() + len(str(abs(terms[key])))
                    for key, n in zip(terms, numbers))
    with localcontext() as ctx:
        ctx.prec = max(ctx.prec, max_place - min_exp + len(str(len(terms))) + 2)
        delta = sum((values[key] * coefficient for key, coefficient in terms.items()), Decimal('0'))
    return CheckResult(name, 'пройдено' if delta.copy_abs() <= tolerance else 'расхождение',
                       delta=delta, tolerance=tolerance)


@dataclass(frozen=True)
class ComparisonContext:
    """Контекст одного значения для сравнения год к году; единица уже нормализована.

    accounting_basis — сопоставимая учётная политика/представление, perimeter_basis —
    сопоставимый состав бизнеса. Их нельзя подтверждать только совпадением стандарта IFRS.
    Для пересчитанного прошлого периода задаётся база из текущего отчёта с его источником.
    """

    company: str
    item_id: str
    standard: str
    scope: str
    currency: str
    unit: str
    end: date
    kind: str
    source: str
    start: date | None = None
    segment: str | None = None
    accounting_basis: str | None = None
    perimeter_basis: str | None = None
    methodology_version: str | None = None

    def __post_init__(self) -> None:
        if self.standard not in ('IFRS', 'RSBU') or self.scope not in ('group', 'entity', 'segment'):
            raise ValueError('Нужны поддерживаемые стандарт и охват')
        if (self.scope == 'segment') != bool(self.segment):
            raise ValueError('Название сегмента задаётся только для сегментного охвата')
        if any(not isinstance(v, str) or not v.strip() for v in
               (self.company, self.item_id, self.currency, self.unit, self.source)):
            raise ValueError('Компания, статья, валюта, единица и источник обязательны')
        if self.standard == 'RSBU' and self.scope != 'entity':
            raise ValueError('РСБУ относится к отдельному юрлицу')
        if self.kind == 'AT_DATE':
            if self.start is not None:
                raise ValueError('Остаток должен относиться к дате')
        else:
            from agent.metrics.engine import Period
            Period(self.start, self.end, self.kind)


def compare_periods(current: ComparisonContext, previous: ComparisonContext) -> CheckResult:
    """Разрешает YoY только для одинакового состава, методики и окна годом ранее.

    Проверка не восстанавливает сопоставимые данные и не одобряет переход IFRS 17/18 по дате.
    Подтверждения учётной базы и периметра должны быть получены из раскрытий отдельно.
    """
    fields = ('company', 'item_id', 'standard', 'scope', 'segment', 'currency', 'unit', 'kind')
    reasons = [f'Различается {key}' for key in fields if getattr(current, key) != getattr(previous, key)]
    missing = []
    for key in ('accounting_basis', 'perimeter_basis', 'methodology_version'):
        a, b = getattr(current, key), getattr(previous, key)
        if not a or not a.strip() or not b or not b.strip():
            missing.append(key)
        elif a != b:
            reasons.append(f'Различается {key}')
    # Сравниваются календарные окна, а не число дней: високосный FY остаётся сопоставимым.
    def year_before(a: date | None, b: date | None) -> bool:
        if a is None or b is None:
            return a is None and b is None
        return (a.year - 1, a.month, a.day) == (b.year, b.month, b.day)

    if not year_before(current.end, previous.end) or not year_before(current.start, previous.start):
        reasons.append('Нужен аналогичный период предыдущего года')
    status = 'несопоставимо' if reasons else ('недостаточно_данных' if missing else 'пройдено')
    return CheckResult('period_comparability', status, missing=tuple(missing), reasons=tuple(reasons))
