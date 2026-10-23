"""Проверка IR-страниц: открываются ли с GitHub и есть ли на них PDF с отчётностью."""

import os
import re
import requests
from urllib.parse import urljoin

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PAGES = {
    "МТС": ["https://moskva.mts.ru/about/investoram-i-akcioneram/korporativnoe-upravlenie/raskritie-informacii/godovaya-otchetnost"],
    "Мечел": ["https://www.mechel.ru/shareholders/disclosure/mechel/"],
    "Сегежа": ["https://segezha-group.com/investors/report/financial-results/"],
    "М.Видео": ["https://mvideoeldorado.ru/en/shareholders-and-investors/news-and-events/investor-news"],
    "ПИК": ["https://pik-group.com/about/news-and-reports/reports/",
            "https://pik-group.com/about/news-and-reports/news/"],
}
KEYWORDS = ["msfo", "ifrs", "мсфо", "konsolid", "консолид", "fs"]
CAPTCHA_SIGNS = ["captcha", "не с ботом", "http-equiv=\"refresh\""]


def fetch_simple(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    return r.status_code, r.text


def fetch_browser(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="ru-RU", user_agent=UA)
        page.goto(url, wait_until="networkidle", timeout=60000)
        html = page.content()
        browser.close()
    return 200, html


def find_pdfs(base_url, html):
    links = re.findall(r'href=["\']([^"\']+?\.pdf)["\']', html, re.I)
    links = list(dict.fromkeys(urljoin(base_url, l) for l in links))  # без дублей
    ifrs = [l for l in links if any(k in l.lower() for k in KEYWORDS)]
    return links, ifrs


def check_url(url):
    for method, fetch in [("requests", fetch_simple), ("браузер", fetch_browser)]:
        try:
            status, html = fetch(url)
        except Exception as e:
            result = f"{method}: ошибка {e.__class__.__name__}"
            continue
        captcha = any(s in html.lower() for s in CAPTCHA_SIGNS) and len(html) < 20000
        links, ifrs = find_pdfs(url, html)
        result = (f"{method}: HTTP {status}, PDF: {len(links)}, похожих на МСФО: {len(ifrs)}"
                  + (", ПОХОЖЕ НА КАПЧУ" if captcha else ""))
        examples = (ifrs or links)[:2]
        if examples:
            result += "\n    " + "\n    ".join(examples)
        if status == 200 and links and not captcha:
            return True, result
    return False, result


if __name__ == "__main__":
    print("check_ir v1\n")
    lines = []
    for company, urls in PAGES.items():
        for url in urls:
            ok, result = check_url(url)
            lines.append(f"{'OK ' if ok else 'НЕТ'} {company}: {result}")
            if ok:
                break
    report = "Проверка IR-страниц\n\n" + "\n\n".join(lines)
    print(report)

    token, chat_id = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if token and chat_id:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": report[:4000],
                                "disable_web_page_preview": "true"}, timeout=30)
        print(f"\nTelegram: HTTP {r.status_code}")
