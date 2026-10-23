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
results = []


def check_page(name, url):
    """Открываем страницу и смотрим код ответа и признаки антибот-защиты."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        text = r.text.lower()
        blocked = any(w in text for w in ["captcha", "ddos-guard", "access denied"])
        status = f"HTTP {r.status_code}, {len(r.text)} символов"
        if blocked:
            status += " — ПОХОЖЕ НА БЛОКИРОВКУ/КАПЧУ"
        ok = r.status_code == 200 and not blocked
    except Exception as e:
        status, ok = f"ошибка: {e.__class__.__name__}: {e}", False
    results.append((name, ok, status))


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
        results.append(("GigaChat", ok, f"HTTP {r.status_code}"))
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
            data={"chat_id": chat_id, "text": report},
            timeout=30,
        )
        print(f"\nTelegram: HTTP {r.status_code}")
    except Exception as e:
        print(f"\nTelegram: ошибка {e}")


if __name__ == "__main__":
    check_page("e-disclosure (страница ЛУКОЙЛа)", "https://www.e-disclosure.ru/portal/company.aspx?id=17")
    check_page("ГИР БО", "https://bo.nalog.gov.ru/")
    check_gigachat()
    send_report_to_telegram()
