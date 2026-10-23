"""Проверка IR-страниц: открываются ли с GitHub и можно ли распознать МСФО (по названию или по содержимому PDF)."""

import io
import os
import re
import requests
from urllib.parse import urljoin
from pypdf import PdfReader

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PAGES = {
    "МТС": ["https://moskva.mts.ru/about/investoram-i-akcioneram/korporativnoe-upravlenie/raskritie-informacii/godovaya-otchetnost"],
    "Мечел": ["https://mechel.ru/shareholders/disclosure/mechel/"],
    "Сегежа": ["https://segezha-group.com/investors/report/financial-results/"],
    "М.Видео": ["https://www.mvideoeldorado.ru/ru/shareholders-and-investors/regulatory-disclosure/ras-reporting"],
    "ПИК": ["https://pik-group.com/about/news-and-reports/reports/"],
}
KEYWORDS = ["мсфо", "ifrs", "msfo", "консолидир", "konsolid", "consolidated"]
CAPTCHA_SIGNS = ["captcha", "не с ботом", "http-equiv=\"refresh\""]
PROBE_PDFS = 3   # сколько PDF скачать для проверки по содержимому


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


def looks_like_ifrs(url, text):
    """Если у ссылки есть текст — решаем по тексту, иначе по адресу."""
    source = text if text else url
    return any(k in source.lower() for k in KEYWORDS)


def find_pdfs(base_url, html):
    found = {}
    for href, text in re.findall(r'<a\b[^>]*?href=["\']([^"\']+?\.pdf)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
        found.setdefault(urljoin(base_url, href), text)
    for href in re.findall(r'href=["\']([^"\']+?\.pdf)["\']', html, re.I):
        found.setdefault(urljoin(base_url, href), "")
    links = list(found.items())
    ifrs = [(u, t) for u, t in links if looks_like_ifrs(u, t)]
    return links, ifrs


def pdf_first_page(url):
    """Скачиваем PDF и берём начало текста первой страницы."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
        reader = PdfReader(io.BytesIO(r.content))
        text = re.sub(r"\s+", " ", reader.pages[0].extract_text() or "").strip()
        pages = len(reader.pages)
        if not text:
            return f"{pages} стр., текста нет (скан?)", False
        return f"{pages} стр.: {text[:150]}", any(k in text.lower() for k in KEYWORDS)
    except Exception as e:
        return f"не удалось прочитать: {e.__class__.__name__}", False


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
            if ifrs:
                for u, t in ifrs[:3]:
                    note += f"\n    • {t[:120] or '(без названия)'}\n      {u}"
            else:
                note += f"\n    Названий нет, смотрю содержимое первых {PROBE_PDFS} PDF:"
                for u, _ in links[:PROBE_PDFS]:
                    snippet, is_ifrs = pdf_first_page(u)
                    note += f"\n    • {'МСФО' if is_ifrs else 'не МСФО?'} | {snippet}\n      {u}"
            return True, note
        notes.append(note)
    return False, "\n    ".join(notes)


if __name__ == "__main__":
    print("check_ir v3\n")
    lines = []
    for company, urls in PAGES.items():
        for url in urls:
            ok, result = check_url(url)
            lines.append(f"{'OK ' if ok else 'НЕТ'} {company}:\n    {result}")
            if ok:
                break
    report = "Проверка IR-страниц v3\n\n" + "\n\n".join(lines)
    print(report)

    token, chat_id = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if token and chat_id:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": report[:4000],
                                "disable_web_page_preview": "true"}, timeout=30)
        print(f"\nTelegram: HTTP {r.status_code}")
