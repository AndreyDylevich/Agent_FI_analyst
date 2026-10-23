"""
Проверка доступа: можно ли с этой машины (сервер GitHub или твой мак)
достучаться до всех сервисов, которые нужны агенту.

Запуск локально:  python3 check_access.py
Нужные переменные окружения (на GitHub задаются через Secrets):
  GIGACHAT_KEY  — ключ авторизации из Studio (developers.sber.ru)
  TG_TOKEN      — токен бота от @BotFather
  TG_CHAT_ID    — id канала, куда бот пишет (вида -100...)
"""

import os
import uuid
import requests
import urllib3

# Для теста отключаем проверку сертификата GigaChat (сертификат Минцифры).
# В рабочем агенте вместо этого подключим сам сертификат.
urllib3.disable_warnings()

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
INN_LUKOIL = "7708004767"
results = []


def check_secrets():
    """Показываем, какие секреты скрипт видит. Сами значения не печатаем."""
    for name in ["GIGACHAT_KEY", "TG_TOKEN", "TG_CHAT_ID"]:
        value = os.environ.get(name, "")
        state = f"задан ({len(value)} символов)" if value else "НЕ ЗАДАН"
        print(f"Секрет {name}: {state}")
    print()


def check_page(name, url, must_contain):
    """Страница считается доступной, если в ней есть ожидаемый текст (а не заглушка)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        found = must_contain.lower() in r.text.lower()
        status = f"HTTP {r.status_code}, {len(r.text)} символов, «{must_contain}» {'найдено' if found else 'НЕ найдено'}"
        ok = r.status_code == 200 and found
        if not ok:
            status += f"\n    начало ответа: {r.text[:200]!r}"
    except Exception as e:
        status, ok = f"ошибка: {e.__class__.__name__}: {e}", False
    results.append((name, ok, status))


def check_girbo_data():
    """
    Главная страница ГИР БО — пустая оболочка, данные сайт подгружает отдельными запросами.
    Пробуем найти ЛУКОЙЛ по ИНН через внутренние адреса сайта (неофициальные,
    поэтому проверяем несколько вариантов и печатаем, что вернулось).
    """
    candidates = [
        f"https://bo.nalog.gov.ru/advanced-search/organizations/search?query={INN_LUKOIL}&page=0",
        f"https://bo.nalog.gov.ru/nbo/organizations/search?query={INN_LUKOIL}&page=0",
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=30)
            found = INN_LUKOIL in r.text
            status = f"HTTP {r.status_code}, ИНН в ответе {'найден' if found else 'НЕ найден'}\n    {url}\n    начало ответа: {r.text[:200]!r}"
            ok = r.status_code == 200 and found
        except Exception as e:
            status, ok = f"ошибка: {e.__class__.__name__}: {e}\n    {url}", False
        results.append(("ГИР БО (поиск по ИНН)", ok, status))
        if ok:
            break


def check_gigachat():
    """Получаем токен доступа GigaChat — это доказывает, что API доступен и ключ рабочий."""
    key = os.environ.get("GIGACHAT_KEY")
    if not key:
        results.append(("GigaChat", False, "нет переменной GIGACHAT_KEY"))
        return
    try:
        r = requests.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={
                "Authorization": f"Basic {key}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"scope": "GIGACHAT_API_PERS"},
            verify=False,
            timeout=30,
        )
        ok = r.status_code == 200 and "access_token" in r.text
        status = f"HTTP {r.status_code}"
        if not ok:
            status += f", ответ: {r.text[:200]!r}"
        results.append(("GigaChat", ok, status))
    except Exception as e:
        results.append(("GigaChat", False, f"ошибка: {e.__class__.__name__}: {e}"))


def send_report_to_telegram():
    """Отправляем итог проверки в канал — заодно проверяем бота."""
    token, chat_id = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    lines = [f"{'OK ' if ok else 'НЕТ'} {name}: {status}" for name, ok, status in results]
    report = "Проверка доступа\n" + "\n".join(lines)
    print(report)
    if not token or not chat_id:
        print("\nTelegram: нет TG_TOKEN или TG_CHAT_ID, сообщение не отправлено")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": report[:4000]},
            timeout=30,
        )
        print(f"\nTelegram: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"\nTelegram: ошибка {e}")


if __name__ == "__main__":
    check_secrets()
    check_page("e-disclosure (страница ЛУКОЙЛа)",
               "https://www.e-disclosure.ru/portal/company.aspx?id=17", "ЛУКОЙЛ")
    check_girbo_data()
    check_gigachat()
    send_report_to_telegram()
