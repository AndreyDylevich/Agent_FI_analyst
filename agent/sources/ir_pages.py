"""Мониторинг IR: базовая линия без PDF, очередь новых ссылок, версии и дедупликация SHA256."""

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from agent.classify import VERSION, classify_document
from agent.sources.files import checked_url, download_pdf, fetch_html


@dataclass(frozen=True)
class Link:
    """Подпись только из ссылки: каталог /IFRS/ не является доказательством МСФО."""

    url: str
    title: str


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.ignored += 1
        if tag == 'a' and not self.ignored:
            self.current = [attrs.get('href', ''), []]

    def handle_data(self, data):
        if self.current is not None and not self.ignored:
            self.current[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.links.append(self.current)
            self.current = None
        if tag in ('script', 'style'):
            self.ignored = max(0, self.ignored-1)


def discover_links(html: str, base_url: str, allowed_hosts: set[str]) -> list[Link]:
    """Относительные URL, query-string, вложенные подписи, дубли и пустые ссылки."""
    if re.search(r'<title[^>]*>[^<]*(?:captcha|access denied|доступ ограничен|проверка браузера)', html, re.I):
        raise ValueError('Страница защиты не является IR-страницей')
    parser = _Links()
    parser.feed(html)
    found = {}
    for href, parts in parser.links:
        url = urljoin(base_url, href)
        if not urlsplit(url).path.lower().endswith('.pdf'):
            continue
        try:
            url = checked_url(url, allowed_hosts)
        except ValueError:
            continue
        title = ' '.join(' '.join(parts).split())
        # Одинаковая ссылка в мобильной и полной версии страницы встречается один раз.
        if url not in found or len(title) > len(found[url]):
            found[url] = title
    if not found:
        raise ValueError('Не найдено разрешённых PDF: возможна смена страницы или защита')
    return [Link(url, title) for url, title in found.items()]


def _document(conn, company_id, link: Link, classification, now: str, *, download=None) -> int:
    """Повтор байтов под новым адресом не создаёт повторный документ одной компании."""
    sha = download.sha256 if download else None
    if sha:
        row = conn.execute('SELECT id FROM documents WHERE company_id=? AND sha256=? ORDER BY id LIMIT 1',
                           (company_id, sha)).fetchone()
    else:
        row = conn.execute('SELECT id FROM documents WHERE company_id=? AND url=? AND sha256 IS NULL LIMIT 1',
                           (company_id, link.url)).fetchone()
    if row:
        if download:
            conn.execute('UPDATE documents SET local_path=? WHERE id=?', (download.path, row['id']))
        return row['id']
    cur = conn.execute(
        'INSERT INTO documents (company_id,url,sha256,title,doc_type,standard,period_start,period_end,'
        'period_type,discovered_at,status,source,local_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (company_id, link.url, sha, link.title or None, classification.doc_type, classification.standard,
         classification.period_start, classification.period_end, classification.period_type, now,
         'downloaded' if download else 'baseline', 'ir_page', download.path if download else None))
    document_id = cur.lastrowid
    conn.execute('INSERT INTO document_classifications VALUES (?,?,?)',
                 (document_id, VERSION, json.dumps(asdict(classification), ensure_ascii=False)))
    return document_id


def apply_snapshot(conn: sqlite3.Connection, page_id: int, links: list[Link], *,
                   recognition: str, now: str) -> dict:
    """Атомарно фиксирует снимок. Ошибка/пустой список не инициализирует базовую линию."""
    if not links:
        raise ValueError('Пустой снимок не является базовой линией')
    page = conn.execute('SELECT * FROM ir_pages WHERE id=?', (page_id,)).fetchone()
    if page is None:
        raise ValueError('Источник не зарегистрирован')
    baseline = page['initialized_at'] is None
    counts = {'baseline': 0, 'new_links': 0, 'changed_titles': 0}
    for link in links:
        row = conn.execute('SELECT * FROM ir_links WHERE page_id=? AND url=?', (page_id, link.url)).fetchone()
        if row is None:
            document_id = None
            if baseline:
                classification = classify_document(link.title, recognition=recognition)
                document_id = _document(conn, page['company_id'], link, classification, now)
            cur = conn.execute('INSERT INTO ir_links (page_id,url,title,first_seen_at,last_seen_at,state,document_id) '
                               'VALUES (?,?,?,?,?,?,?)', (page_id, link.url, link.title, now, now,
                                                       'baseline' if baseline else 'pending', document_id))
            if document_id:
                conn.execute('INSERT INTO ir_observations (link_id,document_id,observed_at) VALUES (?,?,?)',
                             (cur.lastrowid, document_id, now))
            counts['baseline' if baseline else 'new_links'] += 1
        else:
            changed = row['title'] != link.title
            conn.execute('UPDATE ir_links SET title=?,last_seen_at=?,state=? WHERE id=?',
                         (link.title, now, 'pending' if changed else row['state'], row['id']))
            counts['changed_titles'] += int(changed)
    conn.execute('UPDATE ir_pages SET initialized_at=COALESCE(initialized_at,?),last_checked_at=?,last_error=NULL WHERE id=?',
                 (now, now, page_id))
    return {'is_baseline': baseline, **counts}


def process_link(conn, row, *, company_id: int, recognition: str, directory: Path,
                 allowed_hosts: set[str], now: str, downloader=download_pdf) -> dict:
    """Сбой оставляет ссылку в очереди; новая версия не перезаписывает старый документ."""
    result = {'url': row['url'], 'status': 'error'}
    try:
        previous = conn.execute('SELECT * FROM documents WHERE id=?', (row['document_id'],)).fetchone()
        # 304 допустим только когда проверенный локальный файл действительно сохранился.
        local_ok = bool(previous and previous['sha256'] and previous['local_path']
                        and Path(previous['local_path']).is_file()
                        and hashlib.sha256(Path(previous['local_path']).read_bytes()).hexdigest() == previous['sha256'])
        download = downloader(row['url'], directory, allowed_hosts,
                              etag=row['etag'] if local_ok else None,
                              last_modified=row['last_modified'] if local_ok else None)
        if download.not_modified:
            if not local_ok:
                raise ValueError('304 без сохранённого PDF')
            with conn:
                conn.execute("UPDATE ir_links SET state='ready',last_error=NULL,last_fetched_at=? WHERE id=?", (now, row['id']))
            return {**result, 'status': 'not_modified', 'document_id': row['document_id']}
        classification = classify_document(row['title'], download.first_page, recognition=recognition)
        with conn:
            existing = conn.execute('SELECT id FROM documents WHERE company_id=? AND sha256=?',
                                    (company_id, download.sha256)).fetchone()
            document_id = _document(conn, company_id, Link(row['url'], row['title']), classification, now,
                                    download=download)
            conn.execute('INSERT OR IGNORE INTO ir_observations (link_id,document_id,observed_at) VALUES (?,?,?)',
                         (row['id'], document_id, now))
            conn.execute("UPDATE ir_links SET state='ready',document_id=?,etag=?,last_modified=?,last_error=NULL,last_fetched_at=? WHERE id=?",
                         (document_id, download.etag, download.last_modified, now, row['id']))
        return {**result, 'status': 'duplicate' if existing else 'downloaded', 'document_id': document_id,
                'sha256': download.sha256, 'classification': asdict(classification)}
    except (requests.RequestException, ValueError, OSError, RuntimeError) as error:
        message = f'{type(error).__name__}: {error}'
        with conn:
            conn.execute("UPDATE ir_links SET state='error',last_error=?,last_fetched_at=? WHERE id=?", (message, now, row['id']))
        return {**result, 'error': message}


def monitor(conn: sqlite3.Connection, companies: list[dict], directory: Path, *,
            recheck_known: bool = False, max_downloads: int = 20,
            fetcher=fetch_html, downloader=download_pdf) -> dict:
    """Проверяет все источники независимо; рабочий режим не загружает архив базовой линии.

    --recheck-known выполняет явный аудит прежних URL, включая замену файла без смены адреса.
    Без аудита новые версии известного URL обнаруживаются по изменению подписи ссылки.
    """
    if max_downloads < 1:
        raise ValueError('Лимит загрузок должен быть положительным')
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'pages': [], 'files': []}
    queue = []
    for company in companies:
        with conn:
            conn.execute('INSERT OR IGNORE INTO companies (ticker,name) VALUES (?,?)', (company['ticker'], company['name']))
        company_id = conn.execute('SELECT id FROM companies WHERE ticker=?', (company['ticker'],)).fetchone()['id']
        for page in company['ir_pages']:
            now = datetime.now(timezone.utc).isoformat()
            hosts = set(page.get('allowed_hosts', [])) | {urlsplit(page['url']).hostname}
            with conn:
                conn.execute('INSERT OR IGNORE INTO ir_pages (company_id,url) VALUES (?,?)', (company_id, page['url']))
            page_id = conn.execute('SELECT id FROM ir_pages WHERE company_id=? AND url=?',
                                   (company_id, page['url'])).fetchone()['id']
            item = {'ticker': company['ticker'], 'url': page['url'], 'status': 'error'}
            try:
                html, final_url = fetcher(page['url'], hosts)
                links = discover_links(html, final_url, hosts)
                with conn:
                    snapshot = apply_snapshot(conn, page_id, links, recognition=page['recognition'], now=now)
                item.update(status='ok', links=len(links), **snapshot)
                report['pages'].append(item)
                if snapshot['is_baseline']:
                    continue
                rows = conn.execute('SELECT * FROM ir_links WHERE page_id=? AND last_seen_at=? '
                                    "AND (state IN ('pending','error') OR ?)",
                                    (page_id, now, recheck_known)).fetchall()
                item['deferred'] = len(rows)
                queue.extend((row, company_id, company['ticker'], page['recognition'], hosts, now, item) for row in rows)
            except (requests.RequestException, ValueError, OSError, RuntimeError) as error:
                item['error'] = f'{type(error).__name__}: {error}'
                with conn:
                    conn.execute('UPDATE ir_pages SET last_checked_at=?,last_error=? WHERE id=?', (now, item['error'], page_id))
                report['pages'].append(item)
    # Общая очередь не даёт первой большой странице постоянно вытеснять остальные при аудите.
    queue.sort(key=lambda task: (task[0]['state'] != 'pending', task[0]['last_fetched_at'] or '', task[0]['id']))
    for row, company_id, ticker, recognition, hosts, now, item in queue[:max_downloads]:
        report['files'].append({'ticker': ticker, **process_link(
            conn, row, company_id=company_id, recognition=recognition, directory=directory,
            allowed_hosts=hosts, now=now, downloader=downloader)})
        item['deferred'] -= 1
    report['ok'] = (all(p['status'] == 'ok' and not p.get('deferred') for p in report['pages'])
                    and all(f['status'] != 'error' for f in report['files']))
    return report
