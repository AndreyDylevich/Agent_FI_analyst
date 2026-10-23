"""Ликвидность: текущая ликвидность. Быстрая, абсолютная и денежные средства / краткосрочный долг — этап 2."""

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
