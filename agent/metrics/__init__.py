"""Показатели: реестр и модули с расчётами.

Регистрация явная: модуль попадает в реестр, когда импортирован здесь.
Новый модуль показателей — добавить строку импорта ниже.
"""

from agent.metrics import registry  # noqa: F401  — сначала реестр, затем модули с показателями
from agent.metrics.common import leverage, liquidity, profitability  # noqa: F401
