"""Оборачиваемость запасов, дебиторской и кредиторской задолженности.

Числитель — поток LTM, знаменатель — средний остаток за те же двенадцать месяцев.
Дни = 365 / число оборотов; 365 фиксировано методологией, в том числе в високосный год.
"""

from decimal import Decimal

from agent.metrics.registry import PeriodRule, Values, metric

DAYS_IN_YEAR = Decimal('365')


def _ratio_applicable(x: Values, stock: str, flow: str) -> str | None:
    if x[stock] <= 0:
        return f'Средний остаток {stock} ≤ 0 — оборачиваемость неприменима'
    if x[flow] < 0:
        return f'Поток {flow} LTM < 0 — требуется проверка исходных данных'
    return None


def _days_applicable(x: Values, turnover: str) -> str | None:
    if x[turnover] <= 0:
        return 'Оборачиваемость ≤ 0 — срок в днях не рассчитывается'
    return None


@metric(
    id='inventory_turnover',
    name='Оборачиваемость запасов',
    industry='common',
    group='turnover',
    formula='Себестоимость продаж LTM / Средние запасы за LTM',
    expression='{cost_of_sales} / {inventories}',
    inputs={'cost_of_sales': PeriodRule.LTM, 'inventories': PeriodRule.AVERAGE},
    nonnegative_inputs=('inventories',),
    unit='x',
    result_period=PeriodRule.LTM,
    missing_data_rule='Нет себестоимости LTM или конечных запасов — не рассчитывается; нет начала — конец с пометкой.',
    meaning='Сколько раз за год запасы проходят через себестоимость продаж.',
    limitations=(
        'Нефинансовые компании применимых отраслей. Себестоимость нормализована положительной величиной. '
        'Среднее двух остатков не описывает сезонные колебания; при нулевой себестоимости число оборотов равно нулю.'
    ),
    version=1,
    applicable=lambda x: _ratio_applicable(x, 'inventories', 'cost_of_sales'),
)
def inventory_turnover(x: Values) -> Decimal:
    # Оборачиваемость запасов = Себестоимость LTM / ((Запасы в начале LTM + Запасы в конце LTM) / 2).
    return x['cost_of_sales'] / x['inventories']


@metric(
    id='inventory_days',
    name='Период оборота запасов',
    industry='common',
    group='turnover',
    formula='365 / Оборачиваемость запасов LTM',
    expression='365 / {inventory_turnover}',
    inputs={'inventory_turnover': PeriodRule.LTM},
    unit='дн.',
    result_period=PeriodRule.LTM,
    missing_data_rule='Нет оборачиваемости запасов LTM — срок не рассчитывается; ограничения исходного расчёта сохраняются.',
    meaning='Приблизительный срок нахождения запасов в обороте.',
    limitations='Используется 365 дней; при нулевой оборачиваемости срок не определён. Учитываются ограничения расчёта оборотов.',
    version=1,
    applicable=lambda x: _days_applicable(x, 'inventory_turnover'),
)
def inventory_days(x: Values) -> Decimal:
    # Период оборота запасов = 365 / Оборачиваемость запасов LTM.
    return DAYS_IN_YEAR / x['inventory_turnover']


@metric(
    id='receivables_turnover',
    name='Оборачиваемость дебиторской задолженности',
    industry='common',
    group='turnover',
    formula='Выручка LTM / Средняя дебиторская задолженность за LTM',
    expression='{revenue} / {trade_receivables}',
    inputs={'revenue': PeriodRule.LTM, 'trade_receivables': PeriodRule.AVERAGE},
    nonnegative_inputs=('trade_receivables',),
    unit='x',
    result_period=PeriodRule.LTM,
    missing_data_rule='Нет выручки LTM или конечной дебиторской задолженности — не рассчитывается; нет начала — конец с пометкой.',
    meaning='Сколько раз за год дебиторская задолженность покрывается выручкой.',
    limitations=(
        'Нефинансовые компании применимых отраслей. Общая дебиторская задолженность может включать '
        'статьи, не связанные с продажами; состав и качество задолженности проверяются отдельно.'
    ),
    version=1,
    applicable=lambda x: _ratio_applicable(x, 'trade_receivables', 'revenue'),
)
def receivables_turnover(x: Values) -> Decimal:
    # Оборачиваемость дебиторской задолженности = Выручка LTM / Средняя дебиторская задолженность за LTM.
    return x['revenue'] / x['trade_receivables']


@metric(
    id='receivables_days',
    name='Период погашения дебиторской задолженности',
    industry='common',
    group='turnover',
    formula='365 / Оборачиваемость дебиторской задолженности LTM',
    expression='365 / {receivables_turnover}',
    inputs={'receivables_turnover': PeriodRule.LTM},
    unit='дн.',
    result_period=PeriodRule.LTM,
    missing_data_rule='Нет оборачиваемости дебиторской задолженности LTM — срок не рассчитывается.',
    meaning='Приблизительный срок погашения дебиторской задолженности.',
    limitations='Используется 365 дней; при нулевой оборачиваемости срок не определён. Это оценка по отчётности, не договорный срок.',
    version=1,
    applicable=lambda x: _days_applicable(x, 'receivables_turnover'),
)
def receivables_days(x: Values) -> Decimal:
    # Период погашения дебиторской задолженности = 365 / Оборачиваемость дебиторской задолженности LTM.
    return DAYS_IN_YEAR / x['receivables_turnover']


@metric(
    id='payables_turnover',
    name='Оборачиваемость кредиторской задолженности',
    industry='common',
    group='turnover',
    formula='Себестоимость продаж LTM / Средняя кредиторская задолженность за LTM',
    expression='{cost_of_sales} / {trade_payables}',
    inputs={'cost_of_sales': PeriodRule.LTM, 'trade_payables': PeriodRule.AVERAGE},
    nonnegative_inputs=('trade_payables',),
    unit='x',
    result_period=PeriodRule.LTM,
    missing_data_rule='Нет себестоимости LTM или конечной кредиторской задолженности — не рассчитывается; нет начала — конец с пометкой.',
    meaning='Сколько раз за год кредиторская задолженность покрывается себестоимостью.',
    limitations=(
        'Нефинансовые компании применимых отраслей. Себестоимость используется как приближение закупок; '
        'состав кредиторской задолженности может включать прочие статьи, что ограничивает интерпретацию.'
    ),
    version=1,
    applicable=lambda x: _ratio_applicable(x, 'trade_payables', 'cost_of_sales'),
)
def payables_turnover(x: Values) -> Decimal:
    # Оборачиваемость кредиторской задолженности = Себестоимость LTM / Средняя кредиторская задолженность за LTM.
    return x['cost_of_sales'] / x['trade_payables']


@metric(
    id='payables_days',
    name='Период погашения кредиторской задолженности',
    industry='common',
    group='turnover',
    formula='365 / Оборачиваемость кредиторской задолженности LTM',
    expression='365 / {payables_turnover}',
    inputs={'payables_turnover': PeriodRule.LTM},
    unit='дн.',
    result_period=PeriodRule.LTM,
    missing_data_rule='Нет оборачиваемости кредиторской задолженности LTM — срок не рассчитывается.',
    meaning='Приблизительный срок погашения кредиторской задолженности.',
    limitations='Используется 365 дней; это оценка по себестоимости, не договорный срок оплаты поставщикам.',
    version=1,
    applicable=lambda x: _days_applicable(x, 'payables_turnover'),
)
def payables_days(x: Values) -> Decimal:
    # Период погашения кредиторской задолженности = 365 / Оборачиваемость кредиторской задолженности LTM.
    return DAYS_IN_YEAR / x['payables_turnover']
