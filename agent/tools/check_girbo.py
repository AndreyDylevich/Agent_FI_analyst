"""Диагностика доступности ГИР БО из среды запуска, без изменения списка компаний и основной БД.

python -m agent.tools.check_girbo --output /tmp/girbo-check --query "ПАО МТС"
Сохраняет JSON-протокол и, при доступности, PDF для ручной проверки. Финансовые данные не извлекает.
"""

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

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
        if not isinstance(payload, (list, dict)):
            raise ValueError('Неожиданный формат метаданных БФО')
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return {'url': response.url, 'http_status': response.status_code,
                'file': target.name, 'financial_values_verified': False}


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


def run_checks(output: Path, query: str) -> dict:
    """Каждый источник проверяется независимо: сбой поиска не мешает проверке известных ссылок."""
    output.mkdir(parents=True, exist_ok=True)
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'purpose': 'Проверка доступности, не подтверждение финансовых значений', 'checks': []}
    tasks = [('organization_search', lambda: check_search(query))]
    tasks += [(f'{name}_bfo_metadata', lambda name=name, org=org: check_reporting_metadata(
        org, output / f'{name}_bfo_metadata.json')) for name, org in ORGANIZATION_REFERENCES]
    tasks += [(name, lambda url=url, name=name: download_reference(url, output / f'{name}.pdf'))
              for name, url in PDF_REFERENCES]
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
    (output / 'girbo_check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def main(argv: list[str] | None = None) -> int:
    """Код 0 — источники доступны; 1 — есть непроверенные/недоступные источники."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Каталог отдельного протокола и PDF')
    parser.add_argument('--query', default='ПАО МТС')
    args = parser.parse_args(argv)
    return 0 if run_checks(args.output, args.query)['all_accessible'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
