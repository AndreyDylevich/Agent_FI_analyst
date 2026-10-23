"""Долговая нагрузка: чистый долг (без аренды и с арендой), чистый долг / EBITDA.

Определения — CLAUDE.md, раздел 7: долг = кредиты и займы + облигации (долгосрочная и краткосрочная части);
аренда (МСФО 16) — отдельно, вариант с арендой — отдельный показатель; краткосрочные депозиты в денежные
средства не входят. Покрытие процентов, долг/капитал и доля краткосрочного долга — этап 2.
"""

from decimal import Decimal

from agent.metrics.registry import MONEY_UNIT, RATIO_UNIT, PeriodRule, Values, metric

_DEBT_LIMITATIONS = (
    "При консолидированном банке (МТС) банковские обязательства в долг не включаются, но денежные "
    "средства группы могут включать средства банка — требуется пометка или расчёт по сегментам. "
    "Отрицательное значение означает чистую денежную позицию."
)


@metric(
    id="net_debt",
    name="Чистый долг",
    industry="common",
    group="leverage",
    formula="Долгосрочные кредиты и займы + Краткосрочные кредиты и займы − Денежные средства и эквиваленты",
    expression="{debt_long_term} + {debt_short_term} − {cash_and_equivalents}",
    inputs={
        "debt_long_term": PeriodRule.AT_DATE,
        "debt_short_term": PeriodRule.AT_DATE,
        "cash_and_equivalents": PeriodRule.AT_DATE,
    },
    unit=MONEY_UNIT,
    result_period=PeriodRule.AT_DATE,
    missing_data_rule=(
        "Нет любой из трёх статей — не рассчитывается. Если статьи в отчёте нет, а не «не извлечено», "
        "это фиксируется при извлечении явно; 0 молча не подставляется."
    ),
    meaning="Долг, который останется после погашения его имеющимися денежными средствами. Без аренды.",
    limitations=_DEBT_LIMITATIONS,
    version=1,
)
def net_debt(x: Values) -> Decimal:
    # Чистый долг = Долг долгосрочный + Долг краткосрочный − Денежные средства (РСБУ: 1410 + 1510 − 1250)
    return x["debt_long_term"] + x["debt_short_term"] - x["cash_and_equivalents"]


@metric(
    id="net_debt_incl_leases",
    name="Чистый долг с арендой",
    industry="common",
    group="leverage",
    formula=(
        "Долгосрочные кредиты и займы + Краткосрочные кредиты и займы + Обязательства по аренде "
        "(долгосрочные и краткосрочные) − Денежные средства и эквиваленты"
    ),
    expression=(
        "{debt_long_term} + {debt_short_term} + {lease_liabilities_long} + {lease_liabilities_short}"
        " − {cash_and_equivalents}"
    ),
    inputs={
        "debt_long_term": PeriodRule.AT_DATE,
        "debt_short_term": PeriodRule.AT_DATE,
        "lease_liabilities_long": PeriodRule.AT_DATE,
        "lease_liabilities_short": PeriodRule.AT_DATE,
        "cash_and_equivalents": PeriodRule.AT_DATE,
    },
    unit=MONEY_UNIT,
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет любой из пяти статей (включая аренду) — не рассчитывается.",
    meaning="Чистый долг с учётом обязательств по аренде (МСФО 16); важен для ритейла и транспорта.",
    limitations=_DEBT_LIMITATIONS + " По РСБУ аренда в балансе отдельно не показывается — не рассчитывается.",
    version=1,
)
def net_debt_incl_leases(x: Values) -> Decimal:
    # Чистый долг с арендой = Долг дс + Долг кс + Аренда дс + Аренда кс − Денежные средства
    return (
        x["debt_long_term"] + x["debt_short_term"]
        + x["lease_liabilities_long"] + x["lease_liabilities_short"]
        - x["cash_and_equivalents"]
    )


def _ebitda_positive(x: Values) -> str | None:
    if x["ebitda"] <= 0:
        return "EBITDA ≤ 0 — коэффициент не имеет экономического смысла"
    return None


@metric(
    id="net_debt_to_ebitda",
    name="Чистый долг / EBITDA",
    industry="common",
    group="leverage",
    formula="Чистый долг (на дату) / EBITDA (LTM)",
    expression="{net_debt} / {ebitda}",
    inputs={"net_debt": PeriodRule.AT_DATE, "ebitda": PeriodRule.LTM},
    unit=RATIO_UNIT,
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет чистого долга или EBITDA LTM — не рассчитывается.",
    meaning="За сколько лет компания может погасить чистый долг из EBITDA при неизменных результатах.",
    limitations=(
        "Неприменим к банкам и финансовым компаниям. При EBITDA ≤ 0 не считается (пометка). "
        "Без аренды; сравнивать только с показателями той же методики. Выше 3,0x — тревожный сигнал."
    ),
    version=1,
    applicable=_ebitda_positive,
)
def net_debt_to_ebitda(x: Values) -> Decimal:
    # Чистый долг / EBITDA = Чистый долг на конец периода / EBITDA за последние 12 месяцев
    return x["net_debt"] / x["ebitda"]
