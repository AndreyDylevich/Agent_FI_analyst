"""ГИР БО (bo.nalog.gov.ru): поиск организации и годовая РСБУ отдельного юрлица.

Адреса неофициальные и могут измениться. JSON БФО проверен с GitHub;
преобразование сохранённого ответа в исходные значения — agent.sources.rsbu.
"""

import re
from typing import Any

from agent.sources import http

SEARCH_URL = "https://bo.nalog.gov.ru/advanced-search/organizations/search"


def clean(text: Any) -> str | None:
    """Убирает HTML-теги (в ответе поиска совпадения обёрнуты в <strong>) и лишние пробелы."""
    if text is None:
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(text))).strip()


def parse_search(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Ответ поиска → список организаций с очищенными полями.

    Ответ: {"content": [{"id", "inn", "shortName", "ogrn", "okved2", "region", "statusCode",
    "hasKfo", "bfo": {"period", ...}, ...}], "totalElements", ...}.
    hasKfo — у организации есть консолидированная отчётность в ГИР БО.
    """
    found = []
    for org in data.get("content") or []:
        found.append({
            "id": org.get("id"),
            "inn": clean(org.get("inn")),
            "ogrn": clean(org.get("ogrn")),
            "short_name": clean(org.get("shortName")),
            "okved": clean(org.get("okved2")),
            "region": clean(org.get("region")),
            "status": org.get("statusCode"),
            "last_bfo_period": (org.get("bfo") or {}).get("period"),
            "has_kfo": bool(org.get("hasKfo")),
        })
    return found


def search_organizations(query: str, page: int = 0) -> list[dict[str, Any]]:
    """Ищет организации по ИНН или названию (одна страница результатов)."""
    response = http.get(SEARCH_URL, params={"query": query, "page": page}, headers={"Accept": "application/json"})
    return parse_search(response.json())
