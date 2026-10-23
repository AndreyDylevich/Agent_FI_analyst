"""Поиск кандидатов на ИНН по названию через ГИР БО.

Запуск:
    python -m agent.tools.find_inn "Мобильные ТелеСистемы" "Сегежа Групп"

ИНН в config/watchlist.yaml вписывается только после подтверждения пользователем.
"""

import sys

from agent.sources.girbo import search_organizations

MAX_SHOWN = 10


def main(queries: list[str]) -> None:
    for query in queries:
        print(f"\n=== «{query}» ===", flush=True)
        try:
            found = search_organizations(query)
        except Exception as e:
            print(f"  ошибка: {e}", flush=True)
            continue
        if not found:
            print("  ничего не найдено", flush=True)
        for org in found[:MAX_SHOWN]:
            print(
                f"  ИНН {org['inn']} | ОГРН {org['ogrn']} | {org['short_name']} | {org['region']}"
                f" | ОКВЭД {org['okved']} | {org['status']} | БФО {org['last_bfo_period']}"
                f" | КФО {'есть' if org['has_kfo'] else 'нет'} | id ГИР БО {org['id']}",
                flush=True,
            )
        if len(found) > MAX_SHOWN:
            print(f"  … ещё {len(found) - MAX_SHOWN}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
