"""Мониторинг: нет загрузки архива, новые ссылки/версии, повторы, независимость источников."""

import hashlib
import json
from pathlib import Path

import pytest

from agent.db import init_db
from agent.sources.files import Download
from agent.sources.ir_pages import discover_links, monitor

PAGE = 'https://example.test/reports/'
COMPANY = {'ticker': 'TEST', 'name': 'Компания', 'ir_pages': [{'url': PAGE, 'recognition': 'link_text'}]}


def html(*names):
    return ''.join(f'<a href="{n}.pdf">Бухгалтерская отчетность за 2025 год</a>' for n in names)


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path/'test.db')
    yield c
    c.close()


class Site:
    def __init__(self, tmp_path):
        self.html = html('old')
        self.calls = []
        self.error = False
        self.contents = {}
        self.tmp_path = tmp_path

    def fetch(self, *args):
        if self.error:
            raise ValueError('Недоступно')
        return self.html, PAGE

    def download(self, url, directory, hosts, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise ValueError('PDF недоступен')
        content = self.contents.get(url, url.encode())
        sha = hashlib.sha256(content).hexdigest()
        path = self.tmp_path/f'{sha}.pdf'
        path.write_bytes(content)
        return Download(sha, str(path), 1, 'Бухгалтерская отчетность за 2025 год', etag='v1')

    def run(self, conn, **kwargs):
        return monitor(conn, [COMPANY], self.tmp_path, fetcher=self.fetch, downloader=self.download, **kwargs)


def test_baseline_reopen_and_new_link_only(conn, tmp_path):
    site = Site(tmp_path)
    first = site.run(conn)
    assert first['ok'] and first['pages'][0]['is_baseline'] and not site.calls
    assert conn.execute('SELECT status FROM documents').fetchone()[0] == 'baseline'
    path = conn.execute('PRAGMA database_list').fetchone()['file']
    conn.commit()
    reopened = init_db(path)
    try:
        assert site.run(reopened)['files'] == []
        site.html = html('old', 'new')
        second = site.run(reopened)
        assert second['ok'] and second['pages'][0]['new_links'] == 1
        assert len(site.calls) == 1 and site.calls[0][0].endswith('new.pdf')
        assert second['files'][0]['classification']['standard'] == 'RSBU'
        assert site.run(reopened)['files'] == []
    finally:
        reopened.close()


def test_different_url_same_bytes_is_not_duplicate_document(conn, tmp_path):
    site = Site(tmp_path)
    site.run(conn)
    site.html = html('old', 'a', 'b')
    site.contents = {PAGE+'a.pdf': b'identical', PAGE+'b.pdf': b'identical'}
    result = site.run(conn)
    assert [f['status'] for f in result['files']] == ['downloaded', 'duplicate']
    assert conn.execute('SELECT count(*) FROM documents WHERE sha256 IS NOT NULL').fetchone()[0] == 1
    assert conn.execute('SELECT count(DISTINCT document_id) FROM ir_links WHERE state="ready"').fetchone()[0] == 1


def test_same_url_replacement_keeps_both_versions(conn, tmp_path):
    site = Site(tmp_path)
    site.run(conn)
    site.html = html('old', 'new')
    first = site.run(conn)['files'][0]
    site.contents[PAGE+'new.pdf'] = b'revised-pdf'
    # Прежний URL без изменения подписи проверяется только при явном аудите.
    assert not site.run(conn)['files']
    second = site.run(conn, recheck_known=True)
    assert second['ok']
    revisions = conn.execute('SELECT sha256 FROM documents WHERE url=? AND sha256 IS NOT NULL', (PAGE+'new.pdf',)).fetchall()
    assert len(revisions) == 2 and first['sha256'] in {r[0] for r in revisions}
    assert conn.execute('SELECT count(*) FROM ir_observations o JOIN ir_links l ON l.id=o.link_id WHERE l.url=?',
                        (PAGE+'new.pdf',)).fetchone()[0] == 2


def test_changed_title_queues_known_url(conn, tmp_path):
    site = Site(tmp_path)
    site.run(conn)
    site.html = site.html.replace('Бухгалтерская', 'Исправленная бухгалтерская')
    result = site.run(conn)
    assert result['pages'][0]['changed_titles'] == 1 and len(site.calls) == 1


def test_download_failure_retry_does_not_lose_link(conn, tmp_path):
    site = Site(tmp_path)
    site.run(conn)
    site.html = html('old', 'new')
    def fail(*a, **k):
        raise ValueError('Обрыв PDF')
    failed = monitor(conn, [COMPANY], tmp_path, fetcher=site.fetch, downloader=fail)
    assert not failed['ok'] and failed['files'][0]['status'] == 'error'
    assert conn.execute('SELECT state FROM ir_links WHERE url=?', (PAGE+'new.pdf',)).fetchone()[0] == 'error'
    assert site.run(conn)['ok']
    assert len(site.calls) == 1


def test_failed_initial_page_does_not_establish_baseline(conn, tmp_path):
    site = Site(tmp_path)
    site.error = True
    assert not site.run(conn)['ok']
    assert conn.execute('SELECT initialized_at FROM ir_pages').fetchone()[0] is None
    assert conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 0
    site.error = False
    assert site.run(conn)['pages'][0]['is_baseline']
    assert not site.calls


def test_source_failure_does_not_stop_other_company(conn, tmp_path):
    companies = [COMPANY, {**COMPANY, 'ticker': 'SECOND', 'ir_pages': [{'url': 'https://second.test/', 'recognition': 'pdf_first_page'}]}]
    def fetch(url, hosts):
        if url == PAGE:
            raise ValueError('Нет доступа')
        return '<a href="a.pdf"></a>', url
    result = monitor(conn, companies, tmp_path, fetcher=fetch)
    assert not result['ok'] and [p['status'] for p in result['pages']] == ['error', 'ok']
    assert conn.execute('SELECT count(*) FROM ir_links').fetchone()[0] == 1


def test_download_limit_keeps_pending_and_drains_next_run(conn, tmp_path):
    site = Site(tmp_path)
    site.run(conn)
    site.html = html('old', 'new1', 'new2')
    result = site.run(conn, max_downloads=1)
    assert not result['ok'] and result['pages'][0]['deferred'] == 1
    assert len(site.calls) == 1
    assert site.run(conn, max_downloads=1)['ok']
    assert len(site.calls) == 2


def test_audit_limit_rotates_instead_of_rechecking_same_first_link(conn, tmp_path):
    site = Site(tmp_path)
    site.html = html('a', 'b', 'c')
    site.run(conn)
    for _ in range(3):
        site.run(conn, recheck_known=True, max_downloads=1)
    assert len({u for u, _ in site.calls}) == 3


def test_304_requires_existing_verified_local_file(conn, tmp_path):
    site = Site(tmp_path)
    site.run(conn)
    site.html = html('old', 'new')
    downloaded = site.run(conn)['files'][0]
    calls = []
    def conditional(url, directory, hosts, **kwargs):
        calls.append(kwargs)
        return Download(not_modified=True)
    # Ограничим снимок уже загруженной ссылкой, чтобы не аудировать архив.
    site.html = html('new')
    result = monitor(conn, [COMPANY], tmp_path, recheck_known=True, fetcher=site.fetch, downloader=conditional)
    assert result['ok'] and result['files'][0]['status'] == 'not_modified'
    assert calls[-1]['etag'] == 'v1'
    Path(conn.execute('SELECT local_path FROM documents WHERE id=?', (downloaded['document_id'],)).fetchone()[0]).unlink()
    result = monitor(conn, [COMPANY], tmp_path, recheck_known=True, fetcher=site.fetch, downloader=conditional)
    assert not result['ok'] and calls[-1]['etag'] is None


def test_page_without_pdf_never_erases_history(conn, tmp_path):
    site = Site(tmp_path)
    site.run(conn)
    site.html = '<html><title>Access denied</title></html>'
    assert not site.run(conn)['ok']
    assert conn.execute('SELECT count(*) FROM ir_links').fetchone()[0] == 1


def test_parser_relative_queries_nested_text_duplicates_and_untrusted_hosts():
    source = '''<a href="one.PDF?download=1#page=1"><span>Моя</span> отчётность</a>
    <a href="one.PDF?download=1"></a><a href="//cdn.test/two.pdf">МСФО</a>
    <a href="https://evil.test/no.pdf">Не скачивать</a><a href="javascript:bad.pdf">Нет</a>
    <a href="https://e-disclosure.ru/a.pdf">Капча</a>'''
    links = discover_links(source, PAGE, {'example.test', 'cdn.test', 'e-disclosure.ru'})
    assert len(links) == 2 and links[0].title == 'Моя отчётность'
    assert links[0].url == PAGE+'one.PDF?download=1' and links[1].url == 'https://cdn.test/two.pdf'


def test_empty_or_only_unsafe_pdf_is_not_baseline():
    with pytest.raises(ValueError):
        discover_links('<a href="https://evil.test/a.pdf">Отчёт</a>', PAGE, {'example.test'})


def test_audit_limit_eventually_reaches_other_company(conn, tmp_path):
    site = Site(tmp_path)
    companies = [COMPANY, {**COMPANY, 'ticker': 'SECOND', 'ir_pages': [{'url': 'https://second.test/', 'recognition': 'link_text'}]}]
    def fetch(url, hosts):
        return html('a', 'b'), url
    monitor(conn, companies, tmp_path, fetcher=fetch)
    for _ in range(4):
        monitor(conn, companies, tmp_path, fetcher=fetch, downloader=site.download,
                recheck_known=True, max_downloads=1)
    assert {urlsplit_host(url) for url, _ in site.calls} == {'example.test', 'second.test'}


def urlsplit_host(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname


def test_cli_persists_baseline_and_reports_source_error_without_losing_history(monkeypatch, tmp_path):
    from agent.tools import monitor_ir
    site = Site(tmp_path)
    monkeypatch.setattr(monitor_ir.config, 'load', lambda name: {'companies': [COMPANY]})
    real_monitor = monitor_ir.monitor
    monkeypatch.setattr(monitor_ir, 'monitor', lambda conn, companies, directory, **kw:
                        real_monitor(conn, companies, directory, fetcher=site.fetch, downloader=site.download, **kw))
    args = ['--db', str(tmp_path/'cli.db'), '--files', str(tmp_path/'pdf'), '--output', str(tmp_path/'result.json')]
    assert monitor_ir.main(args) == 0
    result = json.loads((tmp_path/'result.json').read_text())
    assert result['pages'][0]['is_baseline']
    site.error = True
    assert monitor_ir.main(args) == 1
    connection = init_db(tmp_path/'cli.db')
    assert connection.execute('SELECT count(*) FROM ir_links').fetchone()[0] == 1
    connection.close()
