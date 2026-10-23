"""Проверка IR-страниц: открываются ли с GitHub и видно ли на них МСФО по названию ссылки."""

import os
import re
import requests
from urllib.parse import urljoin

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PAGES = {
    "МТС": ["https://moskva.mts.ru/about/investoram-i-akcioneram/korporativnoe-upravlenie/raskritie-informacii/godovaya-otchetnost"],
    "Мечел": ["https://www.mechel.ru/shareholders/disclosure/mechel/", "https://mechel.ru/"],
    "Сегежа": ["https://segezha-group.com/investors/report/financial-results/"],
    "М.Видео": ["https://www.mvideoeldorado.ru/ru/shareholders-and-investors/regulatory-disclosure/ras-reporting"],
    "ПИК": ["https://pik-group.com/about/news-and-reports/reports/",
            "https://pik-group.com/about/news-and-reports/news/"],
}
# Признаки МСФО в адресе или в тексте ссылки
KEYWORDS = ["мсфо", "ifrs", "msfo", "консолидир", "konsolid", "consolidated"]
CAPTCHA_SIGNS = ["captcha", "не с ботом", "http-equiv=\"refresh\""]


def fetch_simple(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    return r.status_code, r.text


def fetch_browser(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="ru-RU", user_agent=UA)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        html = page.content()
        browser.close()
    return 200, html


def find_pdfs(base_url, html):
    """Возвращает пары (ссылка, текст ссылки) и отдельно те, что похожи на МСФО."""
    found = {}
    for href, text in re.findall(r'<a\b[^>]*?href=["\']([^"\']+?\.pdf)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
        found.setdefault(urljoin(base_url, href), text)
    for href in re.findall(r'href=["\']([^"\']+?\.pdf)["\']', html, re.I):
        found.setdefault(urljoin(base_url, href), "")
    links = list(found.items())
    ifrs = [(u, t) for u, t in links if any(k in (u + " " + t).lower() for k in KEYWORDS)]
    return links, ifrs


def check_url(url):
    notes = []
    for method, fetch in [("requests", fetch_simple), ("браузер", fetch_browser)]:
        try:
            status, html = fetch(url)
        except Exception as e:
            notes.append(f"{method}: ошибка {e.__class__.__name__}: {str(e)[:150]}")
            continue
        captcha = any(s in html.lower() for s in CAPTCHA_SIGNS) and len(html) < 20000
        links, ifrs = find_pdfs(url, html)
        note = (f"{method}: HTTP {status}, PDF: {len(links)}, МСФО по названию: {len(ifrs)}"
                + (", ПОХОЖЕ НА КАПЧУ" if captcha else ""))
        if status == 200 and links and not captcha:
            for u, t in (ifrs[:3] or links[:2]):
                note += f"\n    • {(t or '(без названия)')[:120]}\n      {u}"
            return True, note
        notes.append(note)
    return False, "\n    ".join(notes)


if __name__ == "__main__":
    print("check_ir v2\n")
    lines = []
    for company, urls in PAGES.items():
        for url in urls:
            ok, result = check_url(url)
            lines.append(f"{'OK ' if ok else 'НЕТ'} {company} ({url}):\n    {result}")
            if ok:
                break
    report = "Проверка IR-страниц v2\n\n" + "\n\n".join(lines)
    print(report)

    token, chat_id = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if token and chat_id:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": report[:4000],
                                "disable_web_page_preview": "true"}, timeout=30)
        print(f"\nTelegram: HTTP {r.status_code}")
