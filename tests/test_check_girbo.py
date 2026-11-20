"""Диагностика ГИР БО: фиктивная сеть, проверка PDF/JSON и протокол частичных ошибок."""

import hashlib
import io
import json

import pytest
import requests
from pypdf import PdfWriter

from agent.sources import http
from agent.tools import check_girbo as check

URL = check.PDF_REFERENCES[0][1]


class Response:
    def __init__(self, data=b'', *, content_type='application/pdf', payload=None, status=200, headers=None):
        self.data = data
        self.content = data
        self.headers = {'Content-Type': content_type, **(headers or {})}
        self.payload = payload
        self.status_code = status
        self.url = URL
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'HTTP {self.status_code}', response=self)

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        # Маленькие куски проверяют, что сигнатура PDF собирается через несколько чтений.
        for offset in range(0, len(self.data), 3):
            yield self.data[offset:offset + 3]


def pdf_bytes():
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(stream)
    return stream.getvalue()


def test_pdf_download_keeps_exact_original_bytes_and_provenance(tmp_path, monkeypatch):
    body = pdf_bytes()
    response = Response(body)
    monkeypatch.setattr(http, 'get', lambda *args, **kwargs: response)
    target = tmp_path / 'report.pdf'
    result = check.download_reference(URL, target)
    assert target.read_bytes() == body
    assert result['sha256'] == hashlib.sha256(body).hexdigest()
    assert result['pages'] == 1
    assert result['bytes'] == len(body)
    assert result['financial_values_verified'] is False
    assert response.closed
    assert not target.with_suffix('.part').exists()


@pytest.mark.parametrize('response', [
    Response(b'<html>captcha</html>', content_type='text/html'),
    Response(b'<html>captcha</html>'),
    Response(b'%PDF-not-a-real-document'),
    Response(b''),
    Response(b'%PDF', headers={'Content-Length': str(check.MAX_PDF_BYTES + 1)}),
])
def test_non_pdf_empty_or_oversized_response_not_saved(tmp_path, monkeypatch, response):
    monkeypatch.setattr(http, 'get', lambda *args, **kwargs: response)
    target = tmp_path / 'report.pdf'
    with pytest.raises(ValueError):
        check.download_reference(URL, target)
    assert not target.exists()
    assert not target.with_suffix('.part').exists()
    assert response.closed


def test_stream_size_limit_enforced_without_content_length(tmp_path, monkeypatch):
    monkeypatch.setattr(http, 'get', lambda *args, **kwargs: Response(pdf_bytes()))
    monkeypatch.setattr(check, 'MAX_PDF_BYTES', 10)
    with pytest.raises(ValueError, match='20 МБ'):
        check.download_reference(URL, tmp_path / 'report.pdf')
    assert list(tmp_path.iterdir()) == []


def test_deadline_enforced_and_partial_file_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(http, 'get', lambda *args, **kwargs: Response(pdf_bytes()))
    times = iter([0, check.MAX_SECONDS + 1])
    with pytest.raises(TimeoutError):
        check.download_reference(URL, tmp_path / 'report.pdf', clock=lambda: next(times))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('url', [
    'http://bo.nalog.gov.ru/download/bfo/pdf/1?detailId=2&period=2025',
    'https://example.com/download/bfo/pdf/1?detailId=2&period=2025',
    'https://bo.nalog.gov.ru@evil.example/download/bfo/pdf/1?detailId=2&period=2025',
    'https://bo.nalog.gov.ru/download/bfo/pdf/1?detailId=2&period=2025&token=x',
    'https://bo.nalog.gov.ru/download/bfo/pdf/1?detailId=2&period=2025&period=2024',
    'https://bo.nalog.gov.ru/download/bfo/pdf/1?period=2025',
])
def test_only_observed_official_pdf_url_shape_allowed(url):
    with pytest.raises(ValueError):
        check.validate_pdf_url(url)


def test_redirect_to_other_service_is_not_downloaded_as_statement(tmp_path, monkeypatch):
    response = Response(pdf_bytes())
    response.url = 'https://example.com/report.pdf'
    monkeypatch.setattr(http, 'get', lambda *args, **kwargs: response)
    with pytest.raises(ValueError):
        check.download_reference(URL, tmp_path / 'report.pdf')
    assert list(tmp_path.iterdir()) == []


def test_search_returns_candidates_without_confirming_identity(monkeypatch):
    response = Response(content_type='application/json', payload={'content': [
        {'id': 1, 'inn': '<strong>7700000001</strong>', 'shortName': 'Тест'}]})
    monkeypatch.setattr(http, 'get', lambda *args, **kwargs: response)
    result = check.check_search('Тест')
    assert result['organizations'][0]['inn'] == '7700000001'
    assert result['identity_confirmed'] is False


@pytest.mark.parametrize('response', [Response(content_type='text/html', payload={'content': []}),
                                      Response(content_type='application/json', payload={}),
                                      Response(content_type='application/json', payload=[])])
def test_search_schema_or_html_not_treated_as_success(monkeypatch, response):
    monkeypatch.setattr(http, 'get', lambda *args, **kwargs: response)
    with pytest.raises(ValueError):
        check.check_search('Тест')


def test_failure_of_search_does_not_skip_pdf_checks_and_protocol(tmp_path, monkeypatch):
    def failed_search(query):
        raise requests.ConnectionError('connection refused')
    monkeypatch.setattr(check, 'check_search', failed_search)
    monkeypatch.setattr(check, 'check_reporting_metadata', lambda org, target: {'pdf_url': URL})
    monkeypatch.setattr(check, 'download_reference', lambda url, target: {'url': url, 'file': target.name})
    result = check.run_checks(tmp_path, 'Тест')
    assert len(result['checks']) == 5
    assert result['checks'][0]['status'] == 'error'
    assert all(r['status'] == 'ok' for r in result['checks'][1:])
    assert result['all_accessible'] is False
    assert json.loads((tmp_path / 'girbo_check.json').read_text()) == result


def test_missing_sources_produce_nonzero_exit_code_and_machine_readable_report(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('source unavailable')
    monkeypatch.setattr(check, 'check_search', fail)
    monkeypatch.setattr(check, 'check_reporting_metadata', fail)
    monkeypatch.setattr(check, 'download_reference', fail)
    assert check.main(['--output', str(tmp_path)]) == 1
    report = json.loads((tmp_path / 'girbo_check.json').read_text())
    assert len(report['checks']) == 5
    assert not report['all_accessible']


def test_http_retries_transient_errors_and_keeps_stream_options(monkeypatch):
    calls = []
    temporary_error = Response(status=503)
    success = Response(pdf_bytes())
    def request(url, **kwargs):
        calls.append(kwargs)
        return temporary_error if len(calls) < 3 else success
    monkeypatch.setattr(http.requests, 'get', request)
    monkeypatch.setattr(http.time, 'sleep', lambda seconds: None)
    assert http.get(URL, stream=True, timeout=(10, 10)) is success
    assert len(calls) == 3
    assert all(call['stream'] and call['timeout'] == (10, 10) for call in calls)
    assert temporary_error.closed


def test_http_does_not_retry_403(monkeypatch):
    calls = []
    response = Response(status=403)
    def request(url, **kwargs):
        calls.append(kwargs)
        return response
    monkeypatch.setattr(http.requests, 'get', request)
    with pytest.raises(requests.HTTPError):
        http.get(URL)
    assert len(calls) == 1
    assert response.closed


def test_pdf_reference_uses_bfo_id_and_published_version():
    data = [{'id': 100, 'period': '2025', 'published': True, 'publishedCorrectionNumber': 1,
             'typeCorrections': [{'type': 12, 'correction': {'id': 201, 'correctionVersion': 0}},
                                 {'type': 12, 'correction': {'id': 202, 'correctionVersion': 1}}]}]
    result = check.latest_pdf_reference(data)
    assert result['pdf_url'] == 'https://bo.nalog.gov.ru/download/bfo/pdf/202'


def test_ambiguous_report_is_not_selected_by_list_order():
    row = {'id': 100, 'period': '2025', 'published': True}
    with pytest.raises(ValueError, match='Несколько'):
        check.latest_pdf_reference([row, row])


def test_frontend_inspection_reads_only_official_main_bundle(monkeypatch):
    class TextResponse(Response):
        @property
        def text(self):
            return self.data.decode()
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if url.endswith('/'):
            return TextResponse(b'<script src="/static/js/main.abc.js"></script><script src="https://third.example/main.js"></script>')
        return TextResponse(b'var route="/download/bfo/pdf/";var parameter="detailId";')
    monkeypatch.setattr(http, 'get', get)
    result = check.inspect_frontend_routes()
    assert calls == ['https://bo.nalog.gov.ru/', 'https://bo.nalog.gov.ru/static/js/main.abc.js']
    assert result['sources'][0]['snippets']


def test_metadata_keeps_exact_raw_response_numbers(tmp_path, monkeypatch):
    body = b'[{"id":100,"period":"2025","published":true,"publishedCorrectionNumber":0,"amount":123.1234567890123456789,"typeCorrections":[{"type":12,"correction":{"id":200,"correctionVersion":0}}]}]'
    response = Response(body, content_type='application/json', payload=json.loads(body))
    monkeypatch.setattr(http, 'get', lambda *args, **kwargs: response)
    target = tmp_path / 'metadata.json'
    result = check.check_reporting_metadata(1, target)
    assert target.read_bytes() == body
    assert result['sha256'] == hashlib.sha256(body).hexdigest()
    assert result['pdf_url'] == 'https://bo.nalog.gov.ru/download/bfo/pdf/200'


def test_pdf_reference_ignores_newer_unpublished_reporting():
    known = {'id': 100, 'period': '2025', 'published': True, 'publishedCorrectionNumber': 0,
             'typeCorrections': [{'type': 12, 'correction': {'id': 200, 'correctionVersion': 0}}]}
    result = check.latest_pdf_reference([dict(known, id=101, period='2026', published=False), known])
    assert result['period'] == 2025
