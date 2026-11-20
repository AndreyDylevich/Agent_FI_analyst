"""Явные методологические допущения из конфига, без подмены исходных значений отчётности."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

from agent import config


@dataclass(frozen=True)
class Parameter:
    """Число из конфига с воспроизводимым описанием источника и версии."""

    value: Decimal | None
    notes: tuple[str, ...] = ()
    reason: str | None = None


def nominal_profit_tax_rate(on_date: date) -> Parameter:
    """Берёт номинальную ставку на конец периода; не учитывает льготы и иностранные сегменты."""
    data = config.load('methodology')
    if type(data['version']) is not int or data['version'] < 1:
        raise ValueError('Нужна положительная целая версия config/methodology.yaml')
    matches = []
    for row in data['nominal_profit_tax_rates']:
        if not isinstance(row['percent'], str):
            raise ValueError('Номинальная ставка в конфиге должна быть десятичной строкой')
        start = date.fromisoformat(row['from'])
        end = date.fromisoformat(row['through']) if row['through'] else date.max
        value = Decimal(row['percent'])
        if not value.is_finite() or not 0 <= value <= 100 or start > end:
            raise ValueError('Некорректная номинальная ставка в config/methodology.yaml')
        if start <= on_date <= end:
            matches.append(row)
    if len(matches) > 1:
        raise ValueError('Пересекающиеся ставки в config/methodology.yaml')
    if not matches:
        return Parameter(None, reason=f'нет номинальной ставки в конфиге на {on_date}')
    row = matches[0]
    value = Decimal(row['percent'])
    note = (f"Допущение: номинальная ставка {value}% на {on_date}; "
            f"config/methodology.yaml, version={data['version']}, "
            f"nominal_profit_tax_rates[{row['from']}], percent={row['percent']}; источник: {row['source']}. "
            'Ставка на конец периода, без усреднения при смене ставки внутри LTM; '
            'льготы компании и иностранные сегменты не учтены.')
    return Parameter(value, (note,))


PROVIDERS: dict[str, Callable[[date], Parameter]] = {'nominal_profit_tax_rate': nominal_profit_tax_rate}
