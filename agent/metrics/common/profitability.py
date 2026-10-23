"""Прибыльность: EBITDA. Валовая и операционная рентабельность, EBITDA margin, ROE, ROA, ROIC — этап 2."""

from decimal import Decimal

from agent.metrics.registry import MONEY_UNIT, PeriodRule, Values, metric


@metric(
    id="ebitda",
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
