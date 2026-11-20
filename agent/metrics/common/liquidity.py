"""Ликвидность: текущая, быстрая, абсолютная и денежные средства / краткосрочный долг."""

from decimal import Decimal

from agent.metrics.registry import RATIO_UNIT, PeriodRule, Values, metric


def _current_liabilities_positive(x: Values) -> str | None:
    if x["current_liabilities"] <= 0:
        return "краткосрочные обязательства ≤ 0 — деление не имеет смысла"
    return None


@metric(
    id="current_ratio",
    name="Текущая ликвидность",
    industry="common",
    group="liquidity",
    formula="Оборотные активы / Краткосрочные обязательства",
    expression="{current_assets} / {current_liabilities}",
    inputs={"current_assets": PeriodRule.AT_DATE, "current_liabilities": PeriodRule.AT_DATE},
    unit=RATIO_UNIT,
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет любой из двух статей — не рассчитывается (недостаточно_данных).",
    meaning="Сколько раз оборотные активы покрывают обязательства со сроком до года.",
    limitations=(
        "Не применяется к банкам и финансовым компаниям. При консолидированном банке (МТС) "
        "банковские активы и обязательства искажают значение. Ниже 1,0 — тревожный сигнал."
    ),
    version=1,
    applicable=_current_liabilities_positive,
)
def current_ratio(x: Values) -> Decimal:
    # Текущая ликвидность = Оборотные активы / Краткосрочные обязательства (РСБУ: 1200 / 1500)
    return x["current_assets"] / x["current_liabilities"]


def _quick_ratio_applicable(x: Values) -> str | None:
    if x['current_liabilities'] <= 0:
        return 'Знаменатель current_liabilities ≤ 0 — показатель неприменим'
    return None


@metric(
    id='quick_ratio',
    name='Быстрая ликвидность',
    industry="common",
    group='liquidity',
    formula='(Дебиторская задолженность + Краткосрочные финансовые вложения + Деньги) / Краткосрочные обязательства',
    expression='({trade_receivables} + {short_term_investments} + {cash_and_equivalents}) / {current_liabilities}',
    inputs={'trade_receivables': PeriodRule.AT_DATE, 'short_term_investments': PeriodRule.AT_DATE, 'cash_and_equivalents': PeriodRule.AT_DATE, 'current_liabilities': PeriodRule.AT_DATE},
    unit='x',
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Покрытие краткосрочных обязательств без реализации запасов.',
    limitations='Для нефинансовых компаний; качество дебиторской задолженности и ликвидность вложений требуют проверки.',
    version=1,
    standards=('IFRS', 'RSBU'),
    applicable=_quick_ratio_applicable,
)
def quick_ratio(x: Values) -> Decimal:
    # (Дебиторская задолженность + Краткосрочные финансовые вложения + Деньги) / Краткосрочные обязательства
    return (x["trade_receivables"] + x["short_term_investments"] + x["cash_and_equivalents"]) / x["current_liabilities"]


def _absolute_ratio_applicable(x: Values) -> str | None:
    if x['current_liabilities'] <= 0:
        return 'Знаменатель current_liabilities ≤ 0 — показатель неприменим'
    return None


@metric(
    id='absolute_ratio',
    name='Абсолютная ликвидность',
    industry="common",
    group='liquidity',
    formula='(Краткосрочные финансовые вложения + Деньги) / Краткосрочные обязательства',
    expression='({short_term_investments} + {cash_and_equivalents}) / {current_liabilities}',
    inputs={'short_term_investments': PeriodRule.AT_DATE, 'cash_and_equivalents': PeriodRule.AT_DATE, 'current_liabilities': PeriodRule.AT_DATE},
    unit='x',
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Покрытие краткосрочных обязательств деньгами и финансовыми вложениями.',
    limitations='Вложения могут быть ограничены в использовании; не применяется к финансовым компаниям.',
    version=1,
    standards=('IFRS', 'RSBU'),
    applicable=_absolute_ratio_applicable,
)
def absolute_ratio(x: Values) -> Decimal:
    # (Краткосрочные финансовые вложения + Деньги) / Краткосрочные обязательства
    return (x["short_term_investments"] + x["cash_and_equivalents"]) / x["current_liabilities"]


def _cash_to_short_term_debt_applicable(x: Values) -> str | None:
    if x['debt_short_term'] <= 0:
        return 'Знаменатель debt_short_term ≤ 0 — показатель неприменим'
    return None


@metric(
    id='cash_to_short_term_debt',
    name='Деньги / краткосрочный долг',
    industry="common",
    group='liquidity',
    formula='Деньги / Краткосрочный долг',
    expression='{cash_and_equivalents} / {debt_short_term}',
    inputs={'cash_and_equivalents': PeriodRule.AT_DATE, 'debt_short_term': PeriodRule.AT_DATE},
    unit='x',
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Способность покрыть краткосрочные заимствования денежными средствами.',
    limitations='Без аренды; при консолидированном банке необходим анализ охвата денежных средств.',
    version=1,
    standards=('IFRS', 'RSBU'),
    applicable=_cash_to_short_term_debt_applicable,
)
def cash_to_short_term_debt(x: Values) -> Decimal:
    # Деньги / Краткосрочный долг
    return x["cash_and_equivalents"] / x["debt_short_term"]
