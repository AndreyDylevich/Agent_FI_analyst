"""Проверка: пускает ли e-disclosure настоящий браузер (Playwright + Chromium)."""

from playwright.sync_api import sync_playwright

URL = "https://www.e-disclosure.ru/portal/company.aspx?id=17"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(locale="ru-RU", user_agent=UA)
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)

    # Даём защите до 30 секунд на проверку и перенаправление
    found = False
    for attempt in range(6):
        page.wait_for_timeout(5000)
        if "ЛУКОЙЛ" in page.content().upper():
            found = True
            break

    print("check_edisclosure v1")
    print(f"Итоговый адрес: {page.url}")
    print(f"Заголовок: {page.title()}")
    print(f"«ЛУКОЙЛ» на странице: {'найдено' if found else 'НЕ найдено'}")
    page.screenshot(path="edisclosure.png")   # снимок, чтобы увидеть, что показал сайт
    browser.close()
