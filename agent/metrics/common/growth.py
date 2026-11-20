"""Рост выручки: сопоставимые периоды год к году и фиксированные окна CAGR 3/5 лет.

CAGR считается только по годовым данным. Для контроля непрерывности истории требуем
каждое годовое наблюдение окна; автоматически сокращать окно при пропусках нельзя.
"""

from decimal import Decimal

from agent.metrics.registry import PeriodRule, Values, metric


def _yoy_applicable(x: Values) -> str | None:
    if x['revenue_previous'] <= 0:
        return 'Выручка предыдущего сопоставимого периода ≤ 0 — процентный рост неприменим'
    if x['revenue_current'] < 0:
        return 'Выручка текущего периода < 0 — требуется проверка исходных данных'
    return None


@metric(
    id='revenue_yoy',
    name='Рост выручки год к году',
    industry='common',
    group='growth',
    formula='(Выручка текущего периода / Выручка аналогичного периода прошлого года − 1) × 100',
    expression='({revenue_current} / {revenue_previous} − 1) × 100',
    inputs={'revenue_current': PeriodRule.FLOW, 'revenue_previous': PeriodRule.FLOW},
    input_items={'revenue_current': 'revenue', 'revenue_previous': 'revenue'},
    input_year_offsets={'revenue_previous': -1},
    period_kinds=('FY', 'Q1', 'H1', '9M'),
    unit='%',
    result_period=PeriodRule.FLOW,
    missing_data_rule='Нет текущей выручки или выручки аналогичного периода год назад — не рассчитывается.',
    meaning='Изменение выручки относительно сопоставимого периода прошлого года.',
    limitations=(
        'FY сравнивается с FY, промежуточный период — с тем же периодом год назад. '
        'При базе ≤ 0 неприменим; текущий ноль при положительной базе означает снижение на 100%. '
        'Изменение состава группы и определения выручки требует отдельной проверки раскрытий.'
    ),
    version=1,
    applicable=_yoy_applicable,
)
def revenue_yoy(x: Values) -> Decimal:
    # Рост выручки год к году = (Выручка(t) / Выручка(t−1) − 1) × 100.
    return (x['revenue_current'] / x['revenue_previous'] - Decimal('1')) * Decimal('100')


def _cagr_applicable(x: Values) -> str | None:
    if any(value <= 0 for value in x.values()):
        return 'Выручка в годовой истории ≤ 0 — CAGR неприменим'
    return None


def _cagr(x: Values, years: int) -> Decimal:
    """Степень вычисляется Decimal с точностью его контекста; float и quantize не используются."""
    # CAGR = ((Выручка в конце / Выручка в начале)^(1 / число годовых интервалов) − 1) × 100.
    return ((x['revenue_end'] / x['revenue_start']) ** (Decimal('1') / Decimal(years))
            - Decimal('1')) * Decimal('100')


@metric(
    id='revenue_cagr_3y',
    name='CAGR выручки за 3 года',
    industry='common',
    group='growth',
    formula='((Годовая выручка(t) / Годовая выручка(t−3))^(1/3) − 1) × 100',
    expression='(({revenue_end} / {revenue_start})^(1/3) − 1) × 100',
    inputs={name: PeriodRule.FLOW for name in
            ('revenue_start', 'revenue_year_1', 'revenue_year_2', 'revenue_end')},
    input_items={name: 'revenue' for name in
                 ('revenue_start', 'revenue_year_1', 'revenue_year_2', 'revenue_end')},
    input_year_offsets={'revenue_start': -3, 'revenue_year_1': -2, 'revenue_year_2': -1},
    period_kinds=('FY',),
    unit='%',
    result_period=PeriodRule.FLOW,
    missing_data_rule='Нужны четыре последовательных годовых наблюдения (три интервала); пропуск блокирует расчёт.',
    meaning='Среднегодовой темп изменения выручки за три полных годовых интервала.',
    limitations=(
        'Только годовая история одного стандарта, валюты и охвата с положительными значениями. '
        'Промежуточные годы подтверждают полноту окна, формула использует начальную и конечную выручку. '
        'Не отражает колебания внутри окна; изменения состава бизнеса требуют проверки раскрытий.'
    ),
    version=1,
    applicable=_cagr_applicable,
)
def revenue_cagr_3y(x: Values) -> Decimal:
    # CAGR за 3 года = ((Выручка(t) / Выручка(t−3))^(1/3) − 1) × 100.
    return _cagr(x, 3)


@metric(
    id='revenue_cagr_5y',
    name='CAGR выручки за 5 лет',
    industry='common',
    group='growth',
    formula='((Годовая выручка(t) / Годовая выручка(t−5))^(1/5) − 1) × 100',
    expression='(({revenue_end} / {revenue_start})^(1/5) − 1) × 100',
    inputs={name: PeriodRule.FLOW for name in
            ('revenue_start', 'revenue_year_1', 'revenue_year_2', 'revenue_year_3', 'revenue_year_4', 'revenue_end')},
    input_items={name: 'revenue' for name in
                 ('revenue_start', 'revenue_year_1', 'revenue_year_2', 'revenue_year_3', 'revenue_year_4', 'revenue_end')},
    input_year_offsets={'revenue_start': -5, 'revenue_year_1': -4, 'revenue_year_2': -3,
                        'revenue_year_3': -2, 'revenue_year_4': -1},
    period_kinds=('FY',),
    unit='%',
    result_period=PeriodRule.FLOW,
    missing_data_rule='Нужны шесть последовательных годовых наблюдений (пять интервалов); окно не сокращается.',
    meaning='Среднегодовой темп изменения выручки за пять полных годовых интервалов.',
    limitations=(
        'Только годовая история одного стандарта, валюты и охвата с положительными значениями. '
        'Промежуточные годы подтверждают полноту окна, формула использует начальную и конечную выручку. '
        'Не отражает колебания внутри окна; изменения состава бизнеса требуют проверки раскрытий.'
    ),
    version=1,
    applicable=_cagr_applicable,
)
def revenue_cagr_5y(x: Values) -> Decimal:
    # CAGR за 5 лет = ((Выручка(t) / Выручка(t−5))^(1/5) − 1) × 100.
    return _cagr(x, 5)
