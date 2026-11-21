"""Диагностика реальных IR-источников и воспроизведение обнаружения нового PDF в отдельной БД."""

import argparse
import hashlib
import html
import json
from pathlib import Path
from urllib.parse import urlsplit

from agent import config, db
from agent.classify import classify_document
from agent.sources.files import fetch_html
from agent.sources.ir_pages import discover_links, monitor


def run_check(output: Path, companies: list[dict]) -> dict:
    """Скрывает 1–2 реальные ссылки в первом снимке и возвращает их во втором.

    Это контролируемое воспроизведение, а не утверждение, что эмитент опубликовал документ во время
    проверки. В первом прогоне PDF не скачиваются; во втором скачиваются реальные документы;
    третий проверяет отсутствие повторной загрузки. История основной БД и watchlist не меняются.
    """
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'ir_check.db'
    if path.exists():
        raise ValueError('Для диагностики нужен новый каталог; история существующей БД не удаляется')
    conn = db.init_db(path)
    cache, withheld, errors = {}, {}, {}
    def get(url, hosts):
        if url in errors:
            raise ValueError(errors[url])
        if url not in cache:
            print(f'IR: получение {url}', flush=True)
            try:
                cache[url] = fetch_html(url, hosts)
                slug = hashlib.sha256(url.encode()).hexdigest()[:12]
                (output / f'page_{slug}.html').write_text(cache[url][0], encoding='utf-8')
            except Exception as error:
                errors[url] = f'{type(error).__name__}: {error}'
                raise
        return cache[url]
    def first_snapshot(url, hosts):
        content, final_url = get(url, hosts)
        links = discover_links(content, final_url, hosts)
        if len(links) < 2:
            raise ValueError('Для воспроизведения нужны хотя бы две PDF-ссылки')
        selected = []
        for standard in ('IFRS', 'RSBU'):
            selected += [link for link in links if classify_document(link.title).standard == standard][:1]
        if not selected:
            selected = links[:2]
        selected = selected[:len(links)-1]
        withheld[url] = [link.url for link in selected]
        filtered = [link for link in links if link not in selected]
        return ''.join(f'<a href="{html.escape(link.url, quote=True)}">{html.escape(link.title)}</a>'
                       for link in filtered), final_url
    try:
        first = monitor(conn, companies, output/'pdf', fetcher=first_snapshot)
        second = monitor(conn, companies, output/'pdf', fetcher=get)
        third = monitor(conn, companies, output/'pdf', fetcher=get)
        counts = {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                  for table in ('ir_pages', 'ir_links', 'documents', 'ir_observations')}
        result = {'purpose': 'Контролируемое воспроизведение по реальным снимкам IR; не событие публикации',
                  'baseline': first, 'new_documents_replay': second, 'unchanged_replay': third,
                  'withheld_real_urls': withheld, 'counts': counts,
                  'ok': first['ok'] and second['ok'] and third['ok'] and not first['files'] and not third['files']
                        and len(second['files']) == sum(map(len, withheld.values()))}
        (output/'ir_check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(f"IR: результат {'ok' if result['ok'] else 'есть ошибки'}, PDF загружено: {len(second['files'])}", flush=True)
        return result
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """Секреты и Telegram не используются; результаты и PDF предназначены для артефакта Actions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    return 0 if run_check(args.output, config.load('watchlist')['companies'])['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
