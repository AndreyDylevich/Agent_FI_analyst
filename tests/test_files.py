"""Ограничения HTTP/PDF: HTML вместо отчёта, размер, время, редиректы, AES и уборка файлов."""

import hashlib
import io

import pytest
import requests
from pypdf import PdfWriter

from agent.sources import files

URL = 'https://example.test/a.pdf'
HOSTS = {'example.test'}


def pdf(password=None):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    if password is not None:
        writer.encrypt(password, algorithm='AES-256')
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class Response:
    def __init__(self, data=b'', *, status=200, headers=None, chunks=None):
        self.data, self.status_code = data, status
        self.headers = {'Content-Type': 'application/pdf', **(headers or {})}
        self.url = URL
        self.chunks = chunks
        self.closed = False

    def __enter__(self): return self
    def __exit__(self, *a): self.close()
    def close(self): self.closed = True
    def iter_content(self, *a):
        yield from self.chunks if self.chunks is not None else [self.data]


def test_valid_pdf_sha_and_blank_text(monkeypatch, tmp_path):
    data = pdf()
    response = Response(data, headers={'ETag': 'v1'})
    monkeypatch.setattr(files, 'open_url', lambda *a: response)
    result = files.download_pdf(URL, tmp_path, HOSTS)
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.pages == 1 and result.first_page == '' and result.etag == 'v1'
    assert (tmp_path/(result.sha256+'.pdf')).read_bytes() == data
    assert response.closed and not list(tmp_path.glob('*.part'))


def test_encrypted_pdf_empty_password_supported(monkeypatch, tmp_path):
    monkeypatch.setattr(files, 'open_url', lambda *a: Response(pdf('')))
    assert files.download_pdf(URL, tmp_path, HOSTS).pages == 1


@pytest.mark.parametrize('response', [
    Response(b'<html>captcha</html>', headers={'Content-Type': 'text/html'}),
    Response(b'<html>captcha</html>'), Response(b'%PDF-broken'), Response(b''),
    Response(pdf('required-password')),
    Response(pdf(), headers={'Content-Length': str(files.MAX_PDF_BYTES+1)}),
])
def test_invalid_pdf_is_not_saved(monkeypatch, tmp_path, response):
    monkeypatch.setattr(files, 'open_url', lambda *a: response)
    with pytest.raises(ValueError):
        files.download_pdf(URL, tmp_path, HOSTS)
    assert not list(tmp_path.iterdir()) and response.closed


def test_content_length_is_not_required_and_stream_limit_still_applies(monkeypatch, tmp_path):
    monkeypatch.setattr(files, 'MAX_PDF_BYTES', 10)
    monkeypatch.setattr(files, 'open_url', lambda *a: Response(chunks=[b'%PDF-', b'x'*6]))
    with pytest.raises(ValueError):
        files.download_pdf(URL, tmp_path, HOSTS)
    assert not list(tmp_path.iterdir())


def test_stream_timeout_removes_partial_file(monkeypatch, tmp_path):
    def chunks():
        yield b'%PDF-'
        raise requests.Timeout('read timeout')
    monkeypatch.setattr(files, 'open_url', lambda *a: Response(chunks=chunks()))
    with pytest.raises(requests.Timeout):
        files.download_pdf(URL, tmp_path, HOSTS)
    assert not list(tmp_path.iterdir())


def test_elapsed_time_limit_applies_even_to_drip_feed():
    times = iter([0, 10, 61])
    response = Response(chunks=[b'a', b'b'])
    with pytest.raises(TimeoutError):
        list(files._chunks(response, 100, clock=lambda: next(times)))


def test_conditional_request_and_304(monkeypatch, tmp_path):
    captured = []
    def open_url(url, hosts, headers):
        captured.append(headers)
        return Response(status=304)
    monkeypatch.setattr(files, 'open_url', open_url)
    assert files.download_pdf(URL, tmp_path, HOSTS, etag='v1').not_modified
    assert captured[0]['If-None-Match'] == 'v1'
    with pytest.raises(ValueError):
        files.download_pdf(URL, tmp_path, HOSTS)


def test_redirect_is_validated_before_request(monkeypatch):
    calls = []
    response = Response(status=302, headers={'Location': 'https://evil.test/secret.pdf'})
    def get(url, **kw):
        calls.append(url)
        assert kw['allow_redirects'] is False
        return response
    monkeypatch.setattr(files.http, 'get', get)
    with pytest.raises(ValueError):
        files.open_url(URL, HOSTS)
    assert calls == [URL] and response.closed


@pytest.mark.parametrize('url', ['http://example.test/a.pdf', 'https://user:pass@example.test/a.pdf',
                                 'https://example.test:8080/a.pdf', 'https://e-disclosure.ru/a.pdf'])
def test_unsafe_sources_rejected(url):
    with pytest.raises(ValueError):
        files.checked_url(url, HOSTS | {'e-disclosure.ru'})
