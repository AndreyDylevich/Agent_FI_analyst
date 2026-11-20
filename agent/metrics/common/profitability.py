"""Прибыльность: EBITDA, маржинальность, ROE, ROA и ROIC с явным выбором налоговой ставки."""

from decimal import Decimal

from agent.metrics.registry import MONEY_UNIT, PeriodRule, Values, metric


@metric(
    id="ebitda",
    standards=("IFRS",),
    name="EBITDA",
    industry="common",
    group="profitability",
    formula=(
        "Операционная прибыль + Амортизация (ОС, НМА, права пользования) "
        "+ Обесценение внеоборотных активов (если отражено в операционной прибыли и раскрыто)"
    ),
    expression="{operating_profit} + {d_and_a} + {impairment_noncurrent}",
    inputs={"operating_profit": PeriodRule.FLOW, "d_and_a": PeriodRule.FLOW},
    optional_inputs={"impairment_noncurrent": PeriodRule.FLOW},
    unit=MONEY_UNIT,
    result_period=PeriodRule.FLOW,
    missing_data_rule=(
        "Нет операционной прибыли или амортизации — не рассчитывается. "
        "Обесценение не раскрыто — не учитывается, в результате пометка «не раскрыто»."
    ),
    meaning="Операционный результат до амортизации — приближение операционного денежного потока.",
    limitations=(
        "Только МСФО: по РСБУ операционной прибыли и амортизации в основных формах нет. "
        "«Скорректированная EBITDA» компании хранится отдельно и здесь не используется. "
        "LTM получается преобразованием периода в движке, а не отдельным показателем."
    ),
    version=1,
)
def ebitda(x: Values) -> Decimal:
    # EBITDA = Операционная прибыль + Амортизация + Обесценение внеоборотных активов (если раскрыто)
    result = x["operating_profit"] + x["d_and_a"]
    if "impairment_noncurrent" in x:  # необязательный вход: не раскрыт — не учитываем, пометка в результате
        result += x["impairment_noncurrent"]
    return result


def _gross_margin_applicable(x: Values) -> str | None:
    if x['revenue'] <= 0:
        return 'Знаменатель revenue ≤ 0 — показатель неприменим'
    return None


@metric(
    id='gross_margin',
    name='Валовая рентабельность',
    industry="common",
    group='profitability',
    formula='Валовая прибыль / Выручка × 100',
    expression='{gross_profit} / {revenue} × 100',
    inputs={'gross_profit': PeriodRule.FLOW, 'revenue': PeriodRule.FLOW},
    unit='%',
    result_period=PeriodRule.FLOW,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Доля валовой прибыли в выручке.',
    limitations='При выручке ≤ 0 неприменима; состав себестоимости должен быть сопоставимым.',
    version=1,
    standards=('IFRS', 'RSBU'),
    applicable=_gross_margin_applicable,
)
def gross_margin(x: Values) -> Decimal:
    # Валовая прибыль / Выручка × 100
    return x["gross_profit"] / x["revenue"] * Decimal("100")


def _operating_margin_applicable(x: Values) -> str | None:
    if x['revenue'] <= 0:
        return 'Знаменатель revenue ≤ 0 — показатель неприменим'
    return None


@metric(
    id='operating_margin',
    name='Операционная рентабельность',
    industry="common",
    group='profitability',
    formula='Операционная прибыль / Выручка × 100',
    expression='{operating_profit} / {revenue} × 100',
    inputs={'operating_profit': PeriodRule.FLOW, 'revenue': PeriodRule.FLOW},
    unit='%',
    result_period=PeriodRule.FLOW,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Доля операционной прибыли в выручке.',
    limitations='РСБУ прибыль от продаж не подменяет операционную прибыль МСФО.',
    version=1,
    standards=('IFRS',),
    applicable=_operating_margin_applicable,
)
def operating_margin(x: Values) -> Decimal:
    # Операционная прибыль / Выручка × 100
    return x["operating_profit"] / x["revenue"] * Decimal("100")


def _ebitda_margin_applicable(x: Values) -> str | None:
    if x['revenue'] <= 0:
        return 'Знаменатель revenue ≤ 0 — показатель неприменим'
    return None


@metric(
    id='ebitda_margin',
    name='Рентабельность EBITDA',
    industry="common",
    group='profitability',
    formula='EBITDA / Выручка × 100',
    expression='{ebitda} / {revenue} × 100',
    inputs={'ebitda': PeriodRule.FLOW, 'revenue': PeriodRule.FLOW},
    unit='%',
    result_period=PeriodRule.FLOW,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Доля EBITDA в выручке.',
    limitations='Не применяется к финансовым компаниям; использует расчётную EBITDA без корректировок компании.',
    version=1,
    standards=('IFRS',),
    applicable=_ebitda_margin_applicable,
)
def ebitda_margin(x: Values) -> Decimal:
    # EBITDA / Выручка × 100
    return x["ebitda"] / x["revenue"] * Decimal("100")


def _net_margin_applicable(x: Values) -> str | None:
    if x['revenue'] <= 0:
        return 'Знаменатель revenue ≤ 0 — показатель неприменим'
    return None


@metric(
    id='net_margin',
    name='Чистая рентабельность',
    industry="common",
    group='profitability',
    formula='Чистая прибыль / Выручка × 100',
    expression='{net_profit} / {revenue} × 100',
    inputs={'net_profit': PeriodRule.FLOW, 'revenue': PeriodRule.FLOW},
    unit='%',
    result_period=PeriodRule.FLOW,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Доля чистой прибыли в выручке.',
    limitations='Для финансовых организаций определение выручки требует отраслевого подхода.',
    version=1,
    standards=('IFRS', 'RSBU'),
    applicable=_net_margin_applicable,
)
def net_margin(x: Values) -> Decimal:
    # Чистая прибыль / Выручка × 100
    return x["net_profit"] / x["revenue"] * Decimal("100")


def _roe_applicable(x: Values) -> str | None:
    if x['equity_parent'] <= 0:
        return 'Знаменатель equity_parent ≤ 0 — показатель неприменим'
    return None


@metric(
    id='roe',
    positive_inputs=('equity_parent',),
    name='Рентабельность капитала (ROE)',
    industry="common",
    group='profitability',
    formula='Чистая прибыль акционеров LTM / Средний капитал акционеров × 100',
    expression='{net_profit_parent} / {equity_parent} × 100',
    inputs={'net_profit_parent': PeriodRule.LTM, 'equity_parent': PeriodRule.AVERAGE},
    unit='%',
    result_period=PeriodRule.LTM,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Доходность капитала акционеров материнской компании.',
    limitations='При отрицательном капитале на любой из границ периода неприменима; при отсутствии начала используется конец с пометкой.',
    version=1,
    standards=('IFRS',),
    applicable=_roe_applicable,
)
def roe(x: Values) -> Decimal:
    # Чистая прибыль акционеров LTM / Средний капитал акционеров × 100
    return x["net_profit_parent"] / x["equity_parent"] * Decimal("100")


def _roa_applicable(x: Values) -> str | None:
    if x['total_assets'] <= 0:
        return 'Знаменатель total_assets ≤ 0 — показатель неприменим'
    return None


@metric(
    id='roa',
    name='Рентабельность активов (ROA)',
    industry="common",
    group='profitability',
    formula='Чистая прибыль LTM / Средние активы × 100',
    expression='{net_profit} / {total_assets} × 100',
    inputs={'net_profit': PeriodRule.LTM, 'total_assets': PeriodRule.AVERAGE},
    unit='%',
    result_period=PeriodRule.LTM,
    missing_data_rule="Нет обязательного входа — не рассчитывается; отсутствующее значение не заменяется нулём.",
    meaning='Доходность активов за последние двенадцать месяцев.',
    limitations='При отсутствии начального остатка используется конечный с пометкой; разовые эффекты анализируются отдельно.',
    version=1,
    standards=('IFRS', 'RSBU'),
    applicable=_roa_applicable,
)
def roa(x: Values) -> Decimal:
    # Чистая прибыль LTM / Средние активы × 100
    return x["net_profit"] / x["total_assets"] * Decimal("100")


@metric(
    id='invested_capital',
    name='Инвестированный капитал',
    industry='common',
    group='profitability',
    formula='Итого капитал + Общий долг с арендой − Денежные средства и эквиваленты',
    expression='{equity_total} + {total_debt_incl_leases} − {cash_and_equivalents}',
    inputs={'equity_total': PeriodRule.AT_DATE, 'total_debt_incl_leases': PeriodRule.AT_DATE,
            'cash_and_equivalents': PeriodRule.AT_DATE},
    unit=MONEY_UNIT,
    result_period=PeriodRule.AT_DATE,
    missing_data_rule='Нужен полный состав на дату: капитал, долг, обе части аренды и деньги; отсутствие не заменяется нулём.',
    meaning='Капитал и процентное финансирование бизнеса с арендой, за вычетом денежных средств.',
    limitations=(
        'Нефинансовые компании, МСФО. Для группы с банком состав долга и денежных средств '
        'требует сегментной проверки. Подтверждённая нулевая аренда задаётся явно.'
    ),
    version=1,
    standards=('IFRS',),
)
def invested_capital(x: Values) -> Decimal:
    # Инвестированный капитал = Капитал + Долг с арендой − Денежные средства и эквиваленты.
    return x['equity_total'] + x['total_debt_incl_leases'] - x['cash_and_equivalents']


def _effective_tax_applicable(x: Values) -> str | None:
    if x['profit_before_tax'] <= 0:
        return 'Прибыль до налогообложения LTM ≤ 0 — эффективная ставка для ROIC не определяется'
    if x['income_tax'] < 0 or x['income_tax'] > x['profit_before_tax']:
        return 'Эффективная ставка вне диапазона 0–100% — для ROIC используется номинальная с пометкой'
    return None


@metric(
    id='effective_tax_rate',
    name='Эффективная ставка налога для ROIC',
    industry='common',
    group='profitability',
    formula='Расход по налогу на прибыль LTM / Прибыль до налогообложения LTM × 100',
    expression='{income_tax} / {profit_before_tax} × 100',
    inputs={'income_tax': PeriodRule.LTM, 'profit_before_tax': PeriodRule.LTM},
    unit='%',
    result_period=PeriodRule.LTM,
    missing_data_rule='Нет расхода по налогу или прибыли до налогообложения LTM — эффективная ставка не определяется.',
    meaning='Доля расхода по налогу в прибыли до налогообложения за те же двенадцать месяцев.',
    limitations=(
        'Используется для ROIC только при положительной прибыли до налога и ставке 0–100%. '
        'Нулевой налог должен быть раскрыт явно. Налоговый доход нельзя подменять положительным расходом. '
        'Разовые налоговые эффекты и переоценка отложенных налогов требуют отдельной интерпретации.'
    ),
    version=1,
    standards=('IFRS',),
    applicable=_effective_tax_applicable,
)
def effective_tax_rate(x: Values) -> Decimal:
    # Эффективная ставка (%) = Расход по налогу LTM / Прибыль до налога LTM × 100.
    return x['income_tax'] / x['profit_before_tax'] * Decimal('100')


@metric(
    id='nominal_tax_rate',
    name='Номинальная ставка налога для ROIC (допущение)',
    industry='common',
    group='profitability',
    formula='Номинальная ставка из конфига на дату окончания периода',
    expression='{configured_rate}',
    inputs={'configured_rate': PeriodRule.AT_DATE},
    parameter_inputs={'configured_rate': 'nominal_profit_tax_rate'},
    input_units={'configured_rate': '%'},
    unit='%',
    result_period=PeriodRule.LTM,
    missing_data_rule='Нет ставки на дату в config/methodology.yaml — не рассчитывается.',
    meaning='Резервное допущение для аналитического ROIC, когда эффективная ставка не определяется.',
    limitations=(
        'Не является ставкой конкретной компании. Берётся на конец периода без усреднения по LTM; '
        'льготы и зарубежные сегменты не учитываются. Источник, версия и значение конфига сохраняются в примечании.'
    ),
    version=1,
    standards=('IFRS',),
)
def nominal_tax_rate(x: Values) -> Decimal:
    # Номинальная ставка (%) = явно заданное значение из версионированного конфига.
    return x['configured_rate']


def _roic_applicable(x: Values) -> str | None:
    if x['invested_capital'] <= 0:
        return 'Средний инвестированный капитал ≤ 0 — ROIC неприменим'
    if not 0 <= x['tax_rate'] <= 100:
        return 'Налоговая ставка вне диапазона 0–100% — требуется проверка'
    return None


@metric(
    id='roic',
    name='Рентабельность инвестированного капитала (ROIC)',
    industry='common',
    group='profitability',
    formula='EBIT LTM × (1 − Ставка налога / 100) / Средний инвестированный капитал за LTM × 100',
    expression='{operating_profit} × (1 − {tax_rate} / 100) / {invested_capital} × 100',
    inputs={'operating_profit': PeriodRule.LTM, 'tax_rate': PeriodRule.LTM,
            'invested_capital': PeriodRule.AVERAGE},
    input_items={'tax_rate': 'effective_tax_rate'},
    input_fallbacks={'tax_rate': 'nominal_tax_rate'},
    positive_inputs=('invested_capital',),
    unit='%',
    result_period=PeriodRule.LTM,
    missing_data_rule=(
        'Нужны EBIT LTM и полный состав инвестированного капитала на конец LTM. '
        'Нет начала целиком — конец с пометкой; неполный состав или конфликт на начало блокирует расчёт. '
        'Эффективная ставка не определяется — номинальная из конфига с пометкой; конфликт источников блокирует расчёт.'
    ),
    meaning='Доходность капитала, вложенного в операционную деятельность, после аналитической налоговой корректировки.',
    limitations=(
        'Только нефинансовые компании по МСФО. Капитал включает аренду, деньги вычитаются. '
        'При инвестированном капитале ≤ 0 на любой границе периода неприменим. '
        'Отрицательный EBIT даёт отрицательный ROIC; налоговая корректировка не означает подтверждённого налогового возмещения.'
    ),
    version=1,
    standards=('IFRS',),
    applicable=_roic_applicable,
)
def roic(x: Values) -> Decimal:
    # ROIC (%) = EBIT LTM × (1 − ставка налога (%) / 100) / Средний инвестированный капитал × 100.
    return (x['operating_profit'] * (Decimal('1') - x['tax_rate'] / Decimal('100'))
            / x['invested_capital'] * Decimal('100'))
