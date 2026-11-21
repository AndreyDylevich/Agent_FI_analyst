"""HTTP-запросы по правилам проекта (CLAUDE.md, раздел 11): таймауты, 3 попытки с паузой, User-Agent браузера.

Повторяются только временные сбои: обрыв соединения, таймаут, ответ 5xx. Ошибка 4xx — сразу исключение.
Потоковое скачивание PDF с лимитами 20 МБ / 60 с — этап 3.
"""

import time
from typing import Any

import requests

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = (10, 30)      # (соединение, чтение), секунды
ATTEMPTS = 3
PAUSE_SECONDS = 5       # пауза растёт: 5, 10 секунд


def get(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
        *, stream: bool = False, timeout: tuple[int, int] = TIMEOUT,
        allow_redirects: bool = True) -> requests.Response:
    """GET с повторами. Возвращает ответ с кодом 2xx/3xx или выбрасывает исключение."""
    last_error: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = requests.get(
                url, params=params, headers={"User-Agent": USER_AGENT, **(headers or {})},
                timeout=timeout, stream=stream, allow_redirects=allow_redirects,
            )
            if response.status_code < 500:
                try:
                    response.raise_for_status()   # 4xx — повтор не поможет
                except requests.HTTPError:
                    response.close()
                    raise
                return response
            last_error = requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.close()
        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = e
        print(f"  попытка {attempt}/{ATTEMPTS} не удалась: {last_error.__class__.__name__}", flush=True)
        if attempt < ATTEMPTS:
            time.sleep(PAUSE_SECONDS * attempt)
    raise RuntimeError(f"Не удалось получить {url} за {ATTEMPTS} попытки") from last_error
