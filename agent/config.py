"""Загрузка конфигов из каталога config/ (watchlist, industries, line_items, thresholds)."""

from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load(name: str) -> dict[str, Any]:
    """Читает config/<name>.yaml и возвращает словарь."""
    with open(CONFIG_DIR / f"{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def line_items() -> dict[str, dict[str, Any]]:
    """Канонические статьи: id → описание статьи."""
    return {item["id"]: item for item in load("line_items")["items"]}


def industries() -> dict[str, dict[str, Any]]:
    """Справочник отраслей: id → описание отрасли."""
    return {ind["id"]: ind for ind in load("industries")["industries"]}


def is_stock(item: dict[str, Any]) -> bool:
    """Остаток определяется природой величины, а не только названием формы (деньги начала ОДДС)."""
    return item.get('value_kind', 'stock' if item['statement'] == 'balance' else 'flow') == 'stock'
