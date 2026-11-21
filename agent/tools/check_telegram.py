"""Проверка подключения и права публикации в канале без отправки сообщений."""

import argparse
import json
import os
from pathlib import Path

import requests


class TelegramCheckError(Exception):
    """Ошибка без URL, токена, содержимого ответа и идентификаторов канала."""


def _call(token: str, method: str, data: dict) -> dict:
    """Вызывает только три читающих метода; исключения requests не выходят наружу."""
    if method not in ('getMe', 'getChat', 'getChatMember'):
        raise TelegramCheckError('Метод не разрешён диагностикой')
    try:
        with requests.post(f'https://api.telegram.org/bot{token}/{method}',
                           data=data, timeout=(10, 30)) as response:
            if response.status_code != 200:
                raise TelegramCheckError(f'{method}: HTTP {response.status_code}')
            payload = response.json()
    except (requests.RequestException, ValueError):
        raise TelegramCheckError(f'{method}: сбой соединения или формата ответа') from None
    if not isinstance(payload, dict) or payload.get('ok') is not True or not isinstance(payload.get('result'), dict):
        raise TelegramCheckError(f'{method}: API не подтвердил результат')
    return payload['result']


def check_connection(token: str | None, chat_id: str | None) -> dict:
    """Возвращает обезличенный протокол, пригодный для артефакта публичного репозитория."""
    result = {'ok': False, 'messages_sent': 0, 'checks': []}
    if not token or not chat_id:
        result['error'] = 'Не заданы TG_TOKEN или TG_CHAT_ID'
        return result
    try:
        bot = _call(token, 'getMe', {})
        if bot.get('is_bot') is not True or type(bot.get('id')) is not int:
            raise TelegramCheckError('getMe: ответ не подтверждает бота')
        result['checks'].append('bot_authenticated')
        chat = _call(token, 'getChat', {'chat_id': chat_id})
        if chat.get('type') != 'channel':
            raise TelegramCheckError('Цель не является каналом Telegram')
        result['checks'].append('channel_accessible')
        member = _call(token, 'getChatMember', {'chat_id': chat_id, 'user_id': bot['id']})
        if member.get('status') == 'creator' or (
                member.get('status') == 'administrator' and member.get('can_post_messages') is True):
            result['checks'].append('posting_permission_confirmed')
            result['ok'] = True
        else:
            raise TelegramCheckError('У бота не подтверждено право публикации в канале')
    except TelegramCheckError as error:
        result['error'] = str(error)
    return result


def main(argv: list[str] | None = None) -> int:
    """Проверяет секреты окружения; не читает и не изменяет основную БД."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = check_connection(os.environ.get('TG_TOKEN'), os.environ.get('TG_CHAT_ID'))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
