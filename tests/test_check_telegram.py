"""Диагностика Telegram не отправляет сообщения и не раскрывает секреты в ошибках."""

import json

import pytest
import requests

from agent.tools import check_telegram as tg


def test_channel_permissions_checked_without_sending(monkeypatch):
    calls = []
    responses = [{'id': 123, 'is_bot': True}, {'type': 'channel'},
                 {'status': 'administrator', 'can_post_messages': True}]
    def call(token, method, data):
        calls.append(method)
        return responses.pop(0)
    monkeypatch.setattr(tg, '_call', call)
    result = tg.check_connection('SECRET', 'PRIVATE_CHAT')
    assert result['ok'] and result['messages_sent'] == 0
    assert calls == ['getMe', 'getChat', 'getChatMember']
    assert 'SECRET' not in json.dumps(result) and 'PRIVATE_CHAT' not in json.dumps(result)


@pytest.mark.parametrize('member', [{'status': 'left'}, {'status': 'member'},
                                    {'status': 'administrator', 'can_post_messages': False}])
def test_no_posting_permission_is_failure(monkeypatch, member):
    responses = iter([{'id': 123, 'is_bot': True}, {'type': 'channel'}, member])
    monkeypatch.setattr(tg, '_call', lambda *a: next(responses))
    assert not tg.check_connection('TOKEN', 'CHAT')['ok']


def test_network_errors_never_contain_secret_url(monkeypatch):
    def broken(*a, **kw):
        raise requests.ConnectionError('https://api.telegram.org/botSECRET/getMe')
    monkeypatch.setattr(tg.requests, 'post', broken)
    result = tg.check_connection('SECRET', 'CHAT')
    assert not result['ok'] and 'SECRET' not in json.dumps(result)


def test_no_credentials_are_reported_as_missing():
    assert tg.check_connection(None, None)['checks'] == []


def test_send_method_forbidden():
    with pytest.raises(tg.TelegramCheckError):
        tg._call('SECRET', 'sendMessage', {})
