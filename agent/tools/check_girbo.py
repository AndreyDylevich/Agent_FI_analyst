"""Диагностика доступности ГИР БО из среды запуска, без изменения списка компаний и основной БД.

python -m agent.tools.check_girbo --output /tmp/girbo-check --query "ПАО МТС"
Сохраняет JSON-протокол и, при доступности, PDF для ручной проверки. Финансовые данные не извлекает.
"""

import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from agent.sources import http
from agent.sources.girbo import SEARCH_URL, parse_search

# Ссылки обнаружены в публичной поисковой выдаче официального ГИР БО.
# Они не подтверждают ИНН в watchlist и не гарантируют доступность файла из конкретной среды.
PDF_REFERENCES = (
    ('mvideo_rsbu_2025', 'https://bo.nalog.gov.ru/download/bfo/pdf/5155604?detailId=57656029&period=2025'),
    ('mts_rsbu_2024', 'https://bo.nalog.gov.ru/download/bfo/pdf/7155648?detailId=52478931&period=2024'),
)
ORGANIZATION_REFERENCES = (('mts', 7155648), ('mvideo', 5155604))
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 60


def validate_pdf_url(url: str) -> None:
    """Принимает только публичный HTTPS-адрес PDF-выгрузки официального ГИР БО."""
    parts = urlparse(url)
    suffix = parts.path.removeprefix('/download/bfo/pdf/')
    params = parse_qs(parts.query, keep_blank_values=True)
    if (parts.scheme != 'https' or parts.netloc != 'bo.nalog.gov.ru' or parts.fragment
            or not parts.path.startswith('/download/bfo/pdf/') or not suffix.isdigit()
            or set(params) != {'detailId', 'period'}
            or any(len(values) != 1 or not values[0].isdigit() for values in params.values())
            or not 2019 <= int(params['period'][0]) <= 9999):
        raise ValueError('Нужна официальная ссылка ГИР БО на PDF с detailId и period')


def check_search(query: str) -> dict:
    """Проверяет реальный ответ поиска. HTML, в том числе страница капчи, не считается успехом."""
    with http.get(SEARCH_URL, params={'query': query, 'page': 0},
                  headers={'Accept': 'application/json'}) as response:
        response.raise_for_status()
        if 'json' not in response.headers.get('Content-Type', '').lower():
            raise ValueError('Поиск вернул не JSON; доступ к данным не подтверждён')
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('content'), list):
            raise ValueError('Неожиданный формат поиска ГИР БО')
        return {'url': response.url, 'http_status': response.status_code,
                'organizations': parse_search(payload), 'identity_confirmed': False,
                'bfo_metadata': [{'organization_id': row.get('id'), 'bfo': row.get('bfo')}
                                 for row in payload['content'][:1]]}


def check_reporting_metadata(organization_id: int, target: Path) -> dict:
    """Исследует публичный ответ ФНС по наборам БФО; формат пока не используется для расчётов."""
    url = f'https://bo.nalog.gov.ru/nbo/organizations/{organization_id}/bfo/'
    with http.get(url, headers={'Accept': 'application/json'}) as response:
        if 'json' not in response.headers.get('Content-Type', '').lower():
            raise ValueError('Метаданные БФО вернули не JSON')
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError('Неожиданный формат метаданных БФО')
        # Сохраняем оригинальные байты JSON: числа из финансовых форм не проходят через float/dumps.
        target.write_bytes(response.content)
        reference = latest_pdf_reference(payload)
        return {'url': response.url, 'http_status': response.status_code,
                'file': target.name, 'sha256': hashlib.sha256(response.content).hexdigest(),
                'financial_values_verified': False, **reference}


def latest_pdf_reference(records: list) -> dict:
    """Ссылка на последний опубликованный годовой отчёт; BFO id не равен organization id."""
    eligible = [r for r in records if r.get('published') is True and str(r.get('period', '')).isdigit()]
    if not eligible:
        raise ValueError('Нет опубликованного годового отчёта')
    year = max(int(r['period']) for r in eligible)
    annual = [r for r in eligible if int(r['period']) == year]
    if len(annual) != 1:
        raise ValueError('Несколько наборов БФО за один год — нужен ручной выбор')
    report = annual[0]
    versions = [r['correction'] for r in report.get('typeCorrections', [])
                if r.get('type') == 12 and isinstance(r.get('correction'), dict)
                and r['correction'].get('correctionVersion') == report.get('publishedCorrectionNumber')]
    if len(versions) != 1:
        raise ValueError('Не определена однозначная опубликованная годовая версия отчёта')
    bfo_id, detail_id = report['id'], versions[0]['id']
    if type(bfo_id) is not int or type(detail_id) is not int:
        raise ValueError('Некорректные идентификаторы отчёта')
    url = f'https://bo.nalog.gov.ru/download/bfo/pdf/{bfo_id}?detailId={detail_id}&period={year}'
    validate_pdf_url(url)
    return {'bfo_id': bfo_id, 'detail_id': detail_id, 'period': year, 'pdf_url': url}


def download_reference(url: str, target: Path, clock: Callable[[], float] = time.monotonic) -> dict:
    """Ограниченная загрузка PDF; не принимает HTML и не обходит защиту.

    При открытии — стандартные 3 попытки для временных сбоев. Дедлайн чтения проверяется между
    чтениями, чтение без данных ограничено таймаутом 10 секунд. Неполный файл удаляется.
    """
    validate_pdf_url(url)
    temporary = target.with_suffix('.part')
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    prefix = bytearray()
    try:
        with http.get(url, timeout=(10, 10), stream=True) as response:
            started = clock()
            response.raise_for_status()
            validate_pdf_url(response.url)  # не сохраняем результат перенаправления на чужой хост/страницу
            content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type not in ('application/pdf', 'application/octet-stream'):
                raise ValueError('Вместо PDF получен другой тип ответа; возможная защита/страница ошибки')
            length = response.headers.get('Content-Length')
            if length is not None and int(length) > MAX_PDF_BYTES:
                raise ValueError('PDF превышает лимит 20 МБ')
            with temporary.open('wb') as stream:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if clock() - started > MAX_SECONDS:
                        raise TimeoutError('Превышено время чтения PDF (60 секунд)')
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise ValueError('PDF превышает лимит 20 МБ')
                    if len(prefix) < 5:
                        prefix.extend(chunk[:5 - len(prefix)])
                    if len(prefix) == 5 and bytes(prefix) != b'%PDF-':
                        raise ValueError('Нет сигнатуры PDF; содержимое не сохраняется как отчётность')
                    digest.update(chunk)
                    stream.write(chunk)
            if bytes(prefix) != b'%PDF-':
                raise ValueError('Пустой/неполный ответ вместо PDF')
            try:
                reader = PdfReader(temporary)
                if reader.is_encrypted and not reader.decrypt(''):
                    raise ValueError('PDF требует пароль')
                pages = len(reader.pages)
                if pages == 0:
                    raise ValueError('PDF не содержит страниц')
            except PyPdfError as error:
                raise ValueError('Ответ не является читаемым PDF') from error
        temporary.replace(target)
        return {'url': url, 'file': target.name, 'bytes': size, 'sha256': digest.hexdigest(),
                'content_type': content_type, 'pages': pages, 'financial_values_verified': False}
    finally:
        temporary.unlink(missing_ok=True)


def inspect_frontend_routes() -> dict:
    """Читает публичные JS-ресурсы сайта ФНС для проверки штатного адреса выгрузки, не исполняя JS."""
    base = 'https://bo.nalog.gov.ru/'
    with http.get(base) as response:
        scripts = re.findall(r'<script[^>]+src=[\"\x27]([^\"\x27]+)', response.text)
    main_scripts = [src for src in scripts if 'main' in src.lower() or 'app.' in src.lower()]
    sources = []
    for src in (main_scripts or scripts[-3:])[:3]:
        url = urljoin(base, src)
        if urlparse(url).netloc != 'bo.nalog.gov.ru' or urlparse(url).scheme != 'https':
            continue
        with http.get(url) as response:
            text = response.text
            snippets = []
            for marker in ('bfo/pdf', '/download', 'detailId', 'downloadBfo'):
                snippets += [text[max(0, m.start()-250):m.end()+350]
                             for m in list(re.finditer(re.escape(marker), text))[:8]]
            sources.append({'url': url, 'snippets': snippets})
    return {'scripts': scripts, 'sources': sources}


def run_checks(output: Path, query: str, inspect_frontend: bool = False) -> dict:
    """Каждый источник проверяется независимо: сбой поиска не мешает проверке известных ссылок."""
    output.mkdir(parents=True, exist_ok=True)
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'purpose': 'Проверка доступности, не подтверждение финансовых значений', 'checks': []}
    tasks = [('organization_search', lambda: check_search(query))]
    tasks += [(f'{name}_bfo_metadata', lambda name=name, org=org: check_reporting_metadata(
        org, output / f'{name}_bfo_metadata.json')) for name, org in ORGANIZATION_REFERENCES]
    def download_discovered(name):
        metadata = next((row for row in report['checks'] if row['name'] == f'{name}_bfo_metadata'), None)
        if metadata is None or metadata['status'] != 'ok':
            raise ValueError('Для загрузки PDF нужны успешно полученные актуальные метаданные БФО')
        return download_reference(metadata['pdf_url'], output / f'{name}_rsbu_latest.pdf')
    tasks += [(f'{name}_pdf', lambda name=name: download_discovered(name))
              for name, org in ORGANIZATION_REFERENCES]
    for name, action in tasks:
        print(f'ГИР БО: проверка {name}', flush=True)
        try:
            details = action()
            report['checks'].append({'name': name, 'status': 'ok', **details})
            print(f'  {name}: ok', flush=True)
        except (requests.RequestException, ValueError, OSError, RuntimeError) as error:
            report['checks'].append({'name': name, 'status': 'error', 'error_type': type(error).__name__,
                                     'message': str(error)})
            print(f'  {name}: {type(error).__name__}', flush=True)
    report['all_accessible'] = all(row['status'] == 'ok' for row in report['checks'])
    if inspect_frontend:
        try:
            details = inspect_frontend_routes()
            (output / 'frontend_pdf_routes.json').write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')
            report['frontend_inspection'] = {'status': 'ok', 'file': 'frontend_pdf_routes.json'}
        except (requests.RequestException, ValueError, OSError, RuntimeError) as error:
            report['frontend_inspection'] = {'status': 'error', 'message': str(error)}
    (output / 'girbo_check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def main(argv: list[str] | None = None) -> int:
    """Код 0 — источники доступны; 1 — есть непроверенные/недоступные источники."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Каталог отдельного протокола и PDF')
    parser.add_argument('--query', default='ПАО МТС')
    parser.add_argument('--inspect-frontend', action='store_true', help='Исследовать публичный код штатной PDF-выгрузки')
    args = parser.parse_args(argv)
    return 0 if run_checks(args.output, args.query, args.inspect_frontend)['all_accessible'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
