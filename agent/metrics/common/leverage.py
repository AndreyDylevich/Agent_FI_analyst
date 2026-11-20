"""Долговая нагрузка: чистый долг (без аренды и с арендой), чистый долг / EBITDA.

Определения — CLAUDE.md, раздел 7: долг = кредиты и займы + облигации (долгосрочная и краткосрочная части);
аренда (МСФО 16) — отдельно, вариант с арендой — отдельный показатель; краткосрочные депозиты в денежные
средства не входят. Покрытие процентов по начисленным расходам и денежным выплатам — отдельно.
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
    standards=("IFRS",),
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
    standards=("IFRS",),
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


@metric(
    id='total_debt',
    name='Общий долг без аренды',
    industry="common",
    group='leverage',
    formula='Долгосрочный долг + Краткосрочный долг',
    expression='{debt_long_term} + {debt_short_term}',
    inputs={'debt_long_term': PeriodRule.AT_DATE, 'debt_short_term': PeriodRule.AT_DATE},
    unit='руб.',
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Объём процентных заимствований без аренды.',
    limitations='Кредиты, займы и облигации; банковские средства клиентов сюда не входят.',
    version=1,
    standards=('IFRS', 'RSBU'),
)
def total_debt(x: Values) -> Decimal:
    # Долгосрочный долг + Краткосрочный долг
    return x["debt_long_term"] + x["debt_short_term"]


@metric(
    id='total_debt_incl_leases',
    name='Общий долг с арендой',
    industry="common",
    group='leverage',
    formula='Общий долг + Долгосрочная аренда + Краткосрочная аренда',
    expression='{total_debt} + {lease_liabilities_long} + {lease_liabilities_short}',
    inputs={'total_debt': PeriodRule.AT_DATE, 'lease_liabilities_long': PeriodRule.AT_DATE, 'lease_liabilities_short': PeriodRule.AT_DATE},
    unit='руб.',
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Заимствования и обязательства по аренде вместе.',
    limitations='МСФО 16; отсутствие раскрытия аренды не означает нулевую аренду.',
    version=1,
    standards=('IFRS',),
)
def total_debt_incl_leases(x: Values) -> Decimal:
    # Общий долг + Долгосрочная аренда + Краткосрочная аренда
    return x["total_debt"] + x["lease_liabilities_long"] + x["lease_liabilities_short"]


def _debt_to_equity_applicable(x: Values) -> str | None:
    if x['equity_total'] <= 0:
        return 'Знаменатель equity_total ≤ 0 — показатель неприменим'
    return None


@metric(
    id='debt_to_equity',
    name='Долг / капитал',
    industry="common",
    group='leverage',
    formula='Общий долг без аренды / Итого капитал',
    expression='{total_debt} / {equity_total}',
    inputs={'total_debt': PeriodRule.AT_DATE, 'equity_total': PeriodRule.AT_DATE},
    unit='x',
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Соотношение процентного долга и собственного капитала.',
    limitations='При капитале ≤ 0 не рассчитывается; нефинансовые компании.',
    version=1,
    standards=('IFRS', 'RSBU'),
    applicable=_debt_to_equity_applicable,
)
def debt_to_equity(x: Values) -> Decimal:
    # Общий долг без аренды / Итого капитал
    return x["total_debt"] / x["equity_total"]


def _short_term_debt_share_applicable(x: Values) -> str | None:
    if x['total_debt'] <= 0:
        return 'Знаменатель total_debt ≤ 0 — показатель неприменим'
    return None


@metric(
    id='short_term_debt_share',
    name='Доля краткосрочного долга',
    industry="common",
    group='leverage',
    formula='Краткосрочный долг / Общий долг × 100',
    expression='{debt_short_term} / {total_debt} × 100',
    inputs={'debt_short_term': PeriodRule.AT_DATE, 'total_debt': PeriodRule.AT_DATE},
    unit='%',
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Доля заимствований, которую нужно погасить в течение года.',
    limitations='Без аренды; сама доля не определяет риск без графика погашения и ликвидности.',
    version=1,
    standards=('IFRS', 'RSBU'),
    applicable=_short_term_debt_share_applicable,
)
def short_term_debt_share(x: Values) -> Decimal:
    # Краткосрочный долг / Общий долг × 100
    return x["debt_short_term"] / x["total_debt"] * Decimal("100")


def _equity_ratio_applicable(x: Values) -> str | None:
    if x['total_assets'] <= 0:
        return 'Знаменатель total_assets ≤ 0 — показатель неприменим'
    return None


@metric(
    id='equity_ratio',
    name='Коэффициент автономии',
    industry="common",
    group='leverage',
    formula='Итого капитал / Итого активы',
    expression='{equity_total} / {total_assets}',
    inputs={'equity_total': PeriodRule.AT_DATE, 'total_assets': PeriodRule.AT_DATE},
    unit='x',
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Доля активов, профинансированная собственным капиталом.',
    limitations='Отрицательное значение сохраняется как признак отрицательного капитала.',
    version=1,
    standards=('IFRS', 'RSBU'),
    applicable=_equity_ratio_applicable,
)
def equity_ratio(x: Values) -> Decimal:
    # Итого капитал / Итого активы
    return x["equity_total"] / x["total_assets"]


def _net_debt_incl_leases_to_ebitda_applicable(x: Values) -> str | None:
    if x['ebitda'] <= 0:
        return 'Знаменатель ebitda ≤ 0 — показатель неприменим'
    return None


@metric(
    id='net_debt_incl_leases_to_ebitda',
    name='Чистый долг с арендой / EBITDA',
    industry="common",
    group='leverage',
    formula='Чистый долг с арендой на дату / EBITDA LTM',
    expression='{net_debt_incl_leases} / {ebitda}',
    inputs={'net_debt_incl_leases': PeriodRule.AT_DATE, 'ebitda': PeriodRule.LTM},
    unit='x',
    result_period=PeriodRule.AT_DATE,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Долговая нагрузка с учётом аренды.',
    limitations='Нефинансовые компании; при EBITDA ≤ 0 не рассчитывается.',
    version=1,
    standards=('IFRS',),
    applicable=_net_debt_incl_leases_to_ebitda_applicable,
)
def net_debt_incl_leases_to_ebitda(x: Values) -> Decimal:
    # Чистый долг с арендой на дату / EBITDA LTM
    return x["net_debt_incl_leases"] / x["ebitda"]


def _ebit_interest_coverage_applicable(x: Values) -> str | None:
    if x['interest_expense'] <= 0:
        return 'Знаменатель interest_expense ≤ 0 — показатель неприменим'
    return None


@metric(
    id='ebit_interest_coverage',
    name='EBIT / процентные расходы',
    industry="common",
    group='leverage',
    formula='Операционная прибыль LTM / Процентные расходы LTM',
    expression='{operating_profit} / {interest_expense}',
    inputs={'operating_profit': PeriodRule.LTM, 'interest_expense': PeriodRule.LTM},
    unit='x',
    result_period=PeriodRule.LTM,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Покрытие начисленных процентов операционной прибылью.',
    limitations='Только МСФО; процентные расходы нормализованы положительной величиной.',
    version=1,
    standards=('IFRS',),
    applicable=_ebit_interest_coverage_applicable,
)
def ebit_interest_coverage(x: Values) -> Decimal:
    # Операционная прибыль LTM / Процентные расходы LTM
    return x["operating_profit"] / x["interest_expense"]


def _ebitda_interest_coverage_applicable(x: Values) -> str | None:
    if x['interest_expense'] <= 0:
        return 'Знаменатель interest_expense ≤ 0 — показатель неприменим'
    return None


@metric(
    id='ebitda_interest_coverage',
    name='EBITDA / процентные расходы',
    industry="common",
    group='leverage',
    formula='EBITDA LTM / Процентные расходы LTM',
    expression='{ebitda} / {interest_expense}',
    inputs={'ebitda': PeriodRule.LTM, 'interest_expense': PeriodRule.LTM},
    unit='x',
    result_period=PeriodRule.LTM,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Покрытие начисленных процентов результатом до амортизации.',
    limitations='Отрицательное покрытие сохраняется; проценты уплаченные анализируются отдельно.',
    version=1,
    standards=('IFRS',),
    applicable=_ebitda_interest_coverage_applicable,
)
def ebitda_interest_coverage(x: Values) -> Decimal:
    # EBITDA LTM / Процентные расходы LTM
    return x["ebitda"] / x["interest_expense"]


def _ebitda_cash_interest_coverage_applicable(x: Values) -> str | None:
    if x['interest_paid'] <= 0:
        return 'Знаменатель interest_paid ≤ 0 — показатель неприменим'
    return None


@metric(
    id='ebitda_cash_interest_coverage',
    name='EBITDA / проценты уплаченные',
    industry="common",
    group='leverage',
    formula='EBITDA LTM / Проценты уплаченные LTM',
    expression='{ebitda} / {interest_paid}',
    inputs={'ebitda': PeriodRule.LTM, 'interest_paid': PeriodRule.LTM},
    unit='x',
    result_period=PeriodRule.LTM,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Покрытие денежных процентных платежей результатом до амортизации.',
    limitations='Уплаченные проценты могут отличаться от начисленных из-за капитализации и сроков оплаты.',
    version=1,
    standards=('IFRS',),
    applicable=_ebitda_cash_interest_coverage_applicable,
)
def ebitda_cash_interest_coverage(x: Values) -> Decimal:
    # EBITDA LTM / Проценты уплаченные LTM
    return x["ebitda"] / x["interest_paid"]


def _rsbu_interest_coverage_applicable(x: Values) -> str | None:
    if x['interest_expense'] <= 0:
        return 'Знаменатель interest_expense ≤ 0 — показатель неприменим'
    return None


@metric(
    id='rsbu_interest_coverage',
    name='Покрытие процентов по РСБУ',
    industry="common",
    group='leverage',
    formula='(Прибыль до налогообложения + Процентные расходы) / Процентные расходы',
    expression='({profit_before_tax} + {interest_expense}) / {interest_expense}',
    inputs={'profit_before_tax': PeriodRule.FLOW, 'interest_expense': PeriodRule.FLOW},
    unit='x',
    result_period=PeriodRule.FLOW,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Покрытие процентов результатом юрлица до налога и процентных расходов.',
    limitations='РСБУ отдельного юрлица, не группы; не подменяет операционное покрытие процентов по МСФО.',
    version=1,
    standards=('RSBU',),
    applicable=_rsbu_interest_coverage_applicable,
)
def rsbu_interest_coverage(x: Values) -> Decimal:
    # (Прибыль до налогообложения + Процентные расходы) / Процентные расходы
    return (x["profit_before_tax"] + x["interest_expense"]) / x["interest_expense"]
