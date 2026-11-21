"""Ограниченные загрузки официальных HTML/PDF, с проверкой каждого перенаправления."""

import hashlib
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from agent.sources import http

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_SECONDS = 60


def checked_url(url: str, allowed_hosts: set[str]) -> str:
    """HTTPS, явный список официальных хостов; капча e-disclosure не используется."""
    url = urldefrag(url.strip())[0]
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in allowed_hosts or parsed.username
            or parsed.password or parsed.port not in (None, 443)
            or parsed.hostname == 'e-disclosure.ru' or parsed.hostname.endswith('.e-disclosure.ru')):
        raise ValueError('URL вне разрешённых официальных HTTPS-источников')
    return url


def open_url(url: str, allowed_hosts: set[str], headers: dict | None = None):
    """Не следует редиректу до проверки его назначения."""
    for _ in range(6):
        url = checked_url(url, allowed_hosts)
        response = http.get(url, headers=headers, stream=True, timeout=(10, 10), allow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get('Location')
        response.close()
        if not location:
            raise ValueError('Редирект без адреса')
        url = urljoin(url, location)
    raise ValueError('Слишком много перенаправлений')


def _chunks(response, limit: int, clock=time.monotonic):
    """Лимит действует на распакованное тело; ожидание отдельного чтения ограничено 10 с."""
    started, size = clock(), 0
    length = response.headers.get('Content-Length')
    if length is not None and (not length.isdigit() or int(length) > limit):
        raise ValueError('Недопустимый размер ответа')
    for chunk in response.iter_content(64 * 1024):
        if clock() - started > MAX_SECONDS:
            raise TimeoutError('Превышено время чтения (60 секунд)')
        size += len(chunk)
        if size > limit:
            raise ValueError('Превышен лимит размера ответа')
        if chunk:
            yield chunk


def fetch_html(url: str, allowed_hosts: set[str]) -> tuple[str, str]:
    """HTML ограничен по размеру; JS не исполняется и защита не обходится."""
    with open_url(url, allowed_hosts) as response:
        if response.status_code != 200 or 'html' not in response.headers.get('Content-Type', '').lower():
            raise ValueError('Страница не вернула HTML с HTTP 200')
        content = b''.join(_chunks(response, MAX_HTML_BYTES))
        # Все текущие IR-источники используют UTF-8; не принимаем молча повреждённую кодировку.
        return content.decode('utf-8-sig'), response.url


@dataclass(frozen=True)
class Download:
    """Результат загрузки; 304 возвращается без файла, SHA и текста."""

    sha256: str | None = None
    path: str | None = None
    pages: int | None = None
    first_page: str = ''
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


def download_pdf(url: str, directory: Path, allowed_hosts: set[str], *,
                 etag: str | None = None, last_modified: str | None = None) -> Download:
    """PDF потоково, ≤20 МБ, временный файл удаляется при любом сбое. Имя — SHA256.

    Разрешена AES-шифровка с пустым паролем. Извлекается только первая страница для классификации;
    отсутствие текста остаётся неопределённостью, а не успехом распознавания.
    """
    directory.mkdir(parents=True, exist_ok=True)
    headers = {'Accept': 'application/pdf'}
    if etag:
        headers['If-None-Match'] = etag
    if last_modified:
        headers['If-Modified-Since'] = last_modified
    with open_url(url, allowed_hosts, headers) as response:
        if response.status_code == 304:
            if not etag and not last_modified:
                raise ValueError('Неожиданный 304 без условного запроса')
            return Download(not_modified=True)
        if response.status_code != 200:
            raise ValueError('Загрузка PDF не вернула HTTP 200')
        content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if content_type not in ('application/pdf', 'application/octet-stream'):
            raise ValueError('Ответ не является PDF; возможная страница защиты')
        with tempfile.NamedTemporaryFile(dir=directory, suffix='.part', delete=False) as temporary:
            path = Path(temporary.name)
            try:
                digest, prefix = hashlib.sha256(), bytearray()
                for chunk in _chunks(response, MAX_PDF_BYTES):
                    prefix.extend(chunk[:max(0, 5-len(prefix))])
                    if len(prefix) == 5 and prefix != b'%PDF-':
                        raise ValueError('Нет сигнатуры PDF')
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
                if prefix != b'%PDF-':
                    raise ValueError('Пустой/неполный PDF')
                try:
                    reader = PdfReader(path)
                    if reader.is_encrypted and not reader.decrypt(''):
                        raise ValueError('PDF требует пароль')
                    pages = len(reader.pages)
                    if not pages:
                        raise ValueError('В PDF нет страниц')
                    first_page = reader.pages[0].extract_text() or ''
                except PyPdfError as error:
                    raise ValueError('PDF не читается') from error
                sha = digest.hexdigest()
                target = directory / f'{sha}.pdf'
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                        raise ValueError('Существующий файл с таким именем повреждён')
                else:
                    path.replace(target)
                return Download(sha, str(target), pages, first_page,
                                response.headers.get('ETag'), response.headers.get('Last-Modified'))
            finally:
                path.unlink(missing_ok=True)
