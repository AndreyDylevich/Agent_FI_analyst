"""Общие показатели денежных потоков по принятой методологии."""

from decimal import Decimal

from agent.metrics.registry import PeriodRule, Values, metric


@metric(
    id='capex',
    name='Капитальные затраты',
    industry="common",
    group='cash_flow',
    formula='Приобретение ОС + Приобретение НМА',
    expression='{capex_ppe} + {capex_intangibles}',
    inputs={'capex_ppe': PeriodRule.FLOW, 'capex_intangibles': PeriodRule.FLOW},
    unit='руб.',
    result_period=PeriodRule.FLOW,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Денежные инвестиции в основные средства и нематериальные активы.',
    limitations='Оттоки нормализованы положительными величинами; строка 4221 РСБУ не подменяет сумму без проверки состава.',
    version=1,
    standards=('IFRS', 'RSBU'),
)
def capex(x: Values) -> Decimal:
    # Приобретение ОС + Приобретение НМА
    return x["capex_ppe"] + x["capex_intangibles"]


@metric(
    id='fcf_simple',
    name='Свободный денежный поток (простой)',
    industry="common",
    group='cash_flow',
    formula='CFO − CapEx',
    expression='{cfo} − {capex}',
    inputs={'cfo': PeriodRule.FLOW, 'capex': PeriodRule.FLOW},
    unit='руб.',
    result_period=PeriodRule.FLOW,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Средства после операционной деятельности и капитальных затрат.',
    limitations='Не вычитает отдельно аренду и проценты вне CFO; не равен FCF для акционеров.',
    version=1,
    standards=('IFRS', 'RSBU'),
)
def fcf_simple(x: Values) -> Decimal:
    # CFO − CapEx
    return x["cfo"] - x["capex"]


def _cash_conversion_applicable(x: Values) -> str | None:
    if x['ebitda'] <= 0:
        return 'Знаменатель ebitda ≤ 0 — показатель неприменим'
    return None


@metric(
    id='cash_conversion',
    name='Конверсия EBITDA в денежный поток',
    industry="common",
    group='cash_flow',
    formula='CFO / EBITDA',
    expression='{cfo} / {ebitda}',
    inputs={'cfo': PeriodRule.FLOW, 'ebitda': PeriodRule.FLOW},
    unit='x',
    result_period=PeriodRule.FLOW,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Насколько EBITDA подтверждается операционным денежным потоком.',
    limitations='При EBITDA ≤ 0 не рассчитывается; классификация процентов влияет на CFO.',
    version=1,
    standards=('IFRS',),
    applicable=_cash_conversion_applicable,
)
def cash_conversion(x: Values) -> Decimal:
    # CFO / EBITDA
    return x["cfo"] / x["ebitda"]


def _fcf_to_equity_applicable(x: Values) -> str | None:
    for name in ('capex', 'interest_paid_outside_cfo', 'lease_principal_paid'):
        if x[name] < 0:
            return f'{name} < 0 — расход/погашение должны быть нормализованы положительной величиной'
    return None


@metric(
    id='fcf_to_equity',
    name='FCF для акционеров (по методике проекта)',
    industry='common',
    group='cash_flow',
    formula='CFO − CapEx − Проценты уплаченные вне CFO − Погашение основной суммы аренды',
    expression='{cfo} − {capex} − {interest_paid_outside_cfo} − {lease_principal_paid}',
    inputs={'cfo': PeriodRule.FLOW, 'capex': PeriodRule.FLOW,
            'interest_paid_outside_cfo': PeriodRule.FLOW, 'lease_principal_paid': PeriodRule.FLOW},
    input_items={'interest_paid_outside_cfo': 'interest_paid'},
    input_cash_flow_sections={'interest_paid_outside_cfo': ('investing', 'financing')},
    nonnegative_inputs=('capex', 'interest_paid_outside_cfo', 'lease_principal_paid'),
    unit='руб.',
    result_period=PeriodRule.FLOW,
    missing_data_rule=(
        'Нужны CFO, обе составляющие CapEx, проценты с классификацией ОДДС и основная сумма аренды. '
        'Неизвестный раздел или смешанный платёж без разбивки блокирует расчёт; явный ноль допустим. '
        'Нулевые платежи по аренде должны быть раскрыты явно, отсутствие не означает ноль.'
    ),
    meaning='Денежный поток после капитальных вложений, процентов и погашения основной суммы аренды по выбранной методике.',
    limitations=(
        'Нефинансовые компании по МСФО. Проценты внутри CFO повторно не вычитаются; '
        'процентная часть аренды входит в interest_paid, основная сумма хранится отдельно. '
        'Для LTM классификация применяется к каждому исходному периоду до преобразования. '
        'Основная сумма аренды вычитается отдельно по принятой методике. '
        'Не учитывает привлечение/погашение кредитов и не определяет допустимый объём дивидендов.'
    ),
    version=1,
    standards=('IFRS',),
    applicable=_fcf_to_equity_applicable,
)
def fcf_to_equity(x: Values) -> Decimal:
    # FCF для акционеров = CFO − CapEx − Проценты вне CFO − Основная сумма аренды.
    return x['cfo'] - x['capex'] - x['interest_paid_outside_cfo'] - x['lease_principal_paid']
