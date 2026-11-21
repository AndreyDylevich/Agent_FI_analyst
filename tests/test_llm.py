"""Контракт SDK: TLS обязателен, ошибки обезличены, обрезанный JSON не принимается."""

from types import SimpleNamespace as NS

import pytest

from agent.extract import llm


class Client:
    def __init__(self, **kwargs):
        self.settings = kwargs
        self.finish = 'stop'
        self.closed = False

    def close(self): self.closed = True
    def get_models(self): return NS(data=[NS(id_='GigaChat-2-Max')])
    def chat(self, payload):
        self.payload = payload
        return NS(choices=[NS(finish_reason=self.finish, message=NS(content='{"status":"ready"}'))],
                  model='GigaChat-2-Max:version', usage=NS(total_tokens=25))


@pytest.fixture
def provider(monkeypatch, tmp_path):
    class Context:
        def load_verify_locations(self, **kw): pass
    monkeypatch.setattr(llm.ssl, 'create_default_context', lambda: Context())
    ca = tmp_path/'ca.pem'; ca.write_text('test')
    return llm.GigaChatProvider('SECRET', ca, client_factory=Client)


def test_tls_and_structured_json(provider):
    result = provider.extract('Инструкция', 'Текст', {'type': 'object'})
    assert result.tokens == 25 and result.content == '{"status":"ready"}'
    assert provider.client.settings['verify_ssl_certs'] is True
    assert provider.client.settings['base_url'] == 'https://api.giga.chat/v1'
    assert provider.client.payload.response_format.type == 'json_schema'
    assert provider.client.payload.model == 'GigaChat-2-Max'


def test_truncated_response_rejected(provider):
    provider.client.finish = 'length'
    with pytest.raises(llm.ProviderError):
        provider.extract('Инструкция', 'Текст', {})


def test_incomplete_response_reason_and_model_text_retained_without_http_headers(provider):
    provider.client.finish='blacklist'
    with pytest.raises(llm.IncompleteResponse) as error:
        provider.extract('Инструкция','Текст',{})
    assert error.value.reason=='blacklist'
    assert error.value.completion.content=='{"status":"ready"}'
    assert 'SECRET' not in str(error.value)


def test_sdk_exception_does_not_expose_key(provider):
    def bad(*a): raise RuntimeError('Authorization: SECRET')
    provider.client.chat = bad
    with pytest.raises(llm.ProviderError) as exc:
        provider.extract('Инструкция', 'Текст', {})
    assert 'SECRET' not in str(exc.value)


def test_missing_key_and_certificate(tmp_path):
    with pytest.raises(llm.ProviderError): llm.GigaChatProvider('', tmp_path/'missing')
    with pytest.raises(llm.ProviderError): llm.GigaChatProvider('SECRET', tmp_path/'missing')


def test_close_and_model_list(provider):
    with provider:
        assert provider.available_models() == ['GigaChat-2-Max']
    assert provider.client.closed
