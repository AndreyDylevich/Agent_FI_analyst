"""Реестр показателей (CLAUDE.md, раздел 6; docs/requirements.md, раздел 14).

Каждый показатель — запись MetricDef: описание (формула, входы, единица, правила периода и отсутствия
данных, смысл, ограничения) + функция расчёта. Показатели не зашиваются в условия: добавление
показателя = новая функция с декоратором @metric в agent/metrics/common/ или agent/metrics/industries/.

Реестр сам ничего не достаёт из БД и не преобразует периоды. Движок (этап 2) готовит входы по правилам
периода из MetricDef.inputs (на дату / поток / LTM / средний остаток) и передаёт их в evaluate().
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Callable, Mapping


class PeriodRule(Enum):
    """Как брать значение входа (или результата) по периоду."""

    AT_DATE = "на дату"            # балансовый остаток на конец периода
    AVERAGE = "средний остаток"    # (начало периода + конец периода) / 2
    FLOW = "поток за период"       # значение за отчётный период как есть
    LTM = "LTM"                    # год(t−1) + с начала года(t) − с начала года(t−1)


# Остатки (балансовые величины) и потоки — для проверки, что правило периода подходит входу.
STOCK_RULES = {PeriodRule.AT_DATE, PeriodRule.AVERAGE}
FLOW_RULES = {PeriodRule.FLOW, PeriodRule.LTM}


class Status(Enum):
    """Статус расчёта показателя (как хранится в metric_values.status)."""

    CALCULATED = "рассчитан"
    NOT_APPLICABLE = "неприменим"
    INSUFFICIENT_DATA = "недостаточно_данных"


MONEY_UNIT = "руб."   # денежные значения хранятся в рублях; при выводе — млн руб.
RATIO_UNIT = "x"

Values = Mapping[str, Decimal]


@dataclass(frozen=True)
class MetricDef:
    """Описание показателя и функция его расчёта."""

    id: str                                    # уникальный id, например "current_ratio"
    name: str                                  # название для отчёта
    industry: str                              # "common" или id отрасли из config/industries.yaml
    group: str                                 # группа: liquidity, leverage, profitability, ...
    formula: str                               # формула текстом (названия статей)
    expression: str                            # шаблон подстановки чисел: "{net_debt} / {ebitda}"
    inputs: Mapping[str, PeriodRule]           # обязательные входы: id статьи/показателя → правило периода
    unit: str                                  # единица результата: "руб.", "x", ...
    result_period: PeriodRule                  # к какому периоду относится результат
    missing_data_rule: str                     # что происходит, если входа нет
    meaning: str                               # экономический смысл
    limitations: str                           # ограничения применимости
    version: int                               # версия формулы; изменение определения = новая версия
    calculate: Callable[[Values], Decimal]     # расчёт по готовым входам, без округления
    optional_inputs: Mapping[str, PeriodRule] = field(default_factory=dict)  # нет — не учитываются, пометка
    applicable: Callable[[Values], str | None] | None = None  # причина неприменимости или None


@dataclass(frozen=True)
class MetricResult:
    """Результат расчёта: значение без округления + всё, что нужно для проверки."""

    metric_id: str
    version: int
    status: Status
    value: Decimal | None                      # None, если не рассчитан; никогда не 0 «вместо»
    reason: str | None                         # причина неприменимости / каких данных нет
    inputs_used: dict[str, Decimal]            # фактически использованные входы
    missing: tuple[str, ...] = ()              # отсутствующие обязательные входы
    notes: tuple[str, ...] = ()                # пометки, например «не раскрыто: impairment_noncurrent»
    explanation: str | None = None             # «формула = подставленные числа = результат», округлено


REGISTRY: dict[str, MetricDef] = {}


def register(definition: MetricDef) -> MetricDef:
    """Добавляет показатель в реестр. Повтор id — ошибка (иначе одна формула молча заменит другую)."""
    if definition.id in REGISTRY:
        raise ValueError(f"Показатель {definition.id!r} уже зарегистрирован")
    overlap = set(definition.inputs) & set(definition.optional_inputs)
    if overlap:
        raise ValueError(f"{definition.id}: вход одновременно обязательный и необязательный: {sorted(overlap)}")
    REGISTRY[definition.id] = definition
    return definition


def metric(**fields: Any) -> Callable[[Callable[[Values], Decimal]], Callable[[Values], Decimal]]:
    """Декоратор: функция расчёта + описание показателя → запись в реестре.

    Пример:
        @metric(id="current_ratio", name="Текущая ликвидность", ...)
        def current_ratio(x):
            return x["current_assets"] / x["current_liabilities"]
    """

    def decorator(func: Callable[[Values], Decimal]) -> Callable[[Values], Decimal]:
        register(MetricDef(calculate=func, **fields))
        return func

    return decorator


def get(metric_id: str) -> MetricDef:
    """Описание показателя по id."""
    return REGISTRY[metric_id]


def evaluate(metric_id: str, values: Mapping[str, Decimal | None]) -> MetricResult:
    """Считает показатель по готовым входам.

    1. Нет обязательного входа (None или отсутствует) → «недостаточно_данных» со списком; 0 не подставляется.
    2. Нет необязательного входа → он не учитывается, в notes — «не раскрыто: …».
    3. Условие применимости вернуло причину → «неприменим».
    4. Иначе — расчёт без округления + текстовое объяснение (округление только в нём).
    """
    d = get(metric_id)
    for name, value in values.items():
        if value is not None and not isinstance(value, Decimal):
            raise TypeError(f"{metric_id}: вход {name} должен быть Decimal, получен {type(value).__name__}")

    missing = tuple(name for name in d.inputs if values.get(name) is None)
    if missing:
        return MetricResult(
            metric_id=d.id, version=d.version, status=Status.INSUFFICIENT_DATA, value=None,
            reason=f"нет данных: {', '.join(missing)}", inputs_used={}, missing=missing,
        )

    used = {name: values[name] for name in d.inputs}
    not_disclosed = []
    for name in d.optional_inputs:
        if values.get(name) is None:
            not_disclosed.append(name)
        else:
            used[name] = values[name]
    notes = (f"не раскрыто: {', '.join(not_disclosed)}",) if not_disclosed else ()

    if d.applicable is not None:
        reason = d.applicable(used)
        if reason:
            return MetricResult(
                metric_id=d.id, version=d.version, status=Status.NOT_APPLICABLE, value=None,
                reason=reason, inputs_used=used, notes=notes,
            )

    value = d.calculate(used)
    return MetricResult(
        metric_id=d.id, version=d.version, status=Status.CALCULATED, value=value, reason=None,
        inputs_used=used, notes=notes, explanation=explain(d, used, value),
    )


# ---------- Объяснение расчёта ----------

MILLION = Decimal("1000000")


def input_unit(name: str) -> str:
    """Единица входа: у показателя — его единица; статьи отчётности сейчас все денежные."""
    if name in REGISTRY:
        return REGISTRY[name].unit
    return MONEY_UNIT


def format_number(value: Decimal, unit: str) -> str:
    """Округление только для вывода: суммы — млн руб. с 1 знаком, коэффициенты — 2 знака.

    Разделитель тысяч — пробел, дробная часть — запятая, минус — «−».
    """
    if unit == MONEY_UNIT:
        rounded = (value / MILLION).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    else:
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "−" if rounded < 0 else ""
    integer, fraction = f"{abs(rounded):f}".split(".")
    groups = []
    while integer:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    return f"{sign}{' '.join(groups)},{fraction}"


def explain(d: MetricDef, used: Mapping[str, Decimal], value: Decimal) -> str:
    """«Название = подставленные числа = результат», например
    «Чистый долг / EBITDA = 412 300,0 / 151 800,0 = 2,72 (суммы в млн руб.)».
    Нераскрытый необязательный вход показывается как «н/д».
    """
    shown = {name: format_number(v, input_unit(name)) for name, v in used.items()}
    shown.update({name: "н/д" for name in d.optional_inputs if name not in used})
    text = f"{d.name} = {d.expression.format_map(shown)} = {format_number(value, d.unit)}"
    if d.unit == MONEY_UNIT:
        return f"{text} млн руб."
    if any(input_unit(name) == MONEY_UNIT for name in used):
        return f"{text} (суммы в млн руб.)"
    return text


# ---------- Выбор показателей и проверка реестра ----------

def for_industry(industry_id: str, industries: Mapping[str, Mapping[str, Any]]) -> list[MetricDef]:
    """Показатели для отрасли: общие из применимых групп (без исключений) + отраслевые."""
    ind = industries[industry_id]
    groups = set(ind.get("common_groups", []))
    excluded = set(ind.get("exclude_metrics", []))
    return [
        d for d in REGISTRY.values()
        if (d.industry == "common" and d.group in groups and d.id not in excluded)
        or d.industry == industry_id
    ]


def validate_registry(line_items: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Проверяет реестр против config/line_items.yaml. Возвращает список ошибок (пустой — всё в порядке).

    - каждый вход — существующая статья или зарегистрированный показатель;
    - правило периода подходит входу: остатку (баланс, показатель «на дату») — AT_DATE / AVERAGE,
      потоку (ОПУ, ОДДС, показатель «за период») — FLOW / LTM.
    """
    errors = []
    for d in REGISTRY.values():
        for name, rule in {**d.inputs, **d.optional_inputs}.items():
            if name in line_items:
                is_stock = line_items[name]["statement"] == "balance"
            elif name in REGISTRY:
                is_stock = REGISTRY[name].result_period in STOCK_RULES
            else:
                errors.append(f"{d.id}: вход {name!r} не найден ни в line_items.yaml, ни в реестре")
                continue
            allowed = STOCK_RULES if is_stock else FLOW_RULES
            if rule not in allowed:
                kind = "остаток" if is_stock else "поток"
                errors.append(f"{d.id}: вход {name!r} — {kind}, правило {rule.name} не подходит")
    return errors
