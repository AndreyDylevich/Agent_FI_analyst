"""Провайдер извлечения GigaChat: только текст выбранных страниц, строгий JSON, проверенный TLS."""

import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole
from dotenv import load_dotenv

MODEL = 'GigaChat-2-Max'
BASE_URL = 'https://api.giga.chat/v1'
CA_PATH = Path(__file__).resolve().parents[2] / 'certs' / 'russian_trusted_root_ca.cer'


class ProviderError(Exception):
    """Обезличенная ошибка; не содержит ключей, заголовков или HTTP-тела SDK."""


@dataclass(frozen=True)
class Completion:
    """Ответ и происхождение; финансовые значения здесь не интерпретируются."""

    content: str
    model: str
    tokens: int


class ExtractionProvider(Protocol):
    def extract(self, system: str, text: str, schema: dict) -> Completion:
        """Возвращает JSON-строку; статус проверки никогда не задаётся моделью."""
        ...


class GigaChatProvider:
    """SDK изолирован от конвейера; ошибки внешнего клиента не печатаются целиком."""

    def __init__(self, credentials: str, ca_path: Path = CA_PATH, *, client_factory=GigaChat):
        if not credentials:
            raise ProviderError('Не задан GIGACHAT_KEY')
        if not ca_path.is_file():
            raise ProviderError('Не найден корневой сертификат Минцифры')
        try:
            context = ssl.create_default_context()
            context.load_verify_locations(cafile=str(ca_path))
            self.client = client_factory(credentials=credentials, scope='GIGACHAT_API_PERS',
                                         base_url=BASE_URL, model=MODEL, ssl_context=context,
                                         verify_ssl_certs=True, timeout=90, max_retries=2)
        except Exception as error:
            raise ProviderError(f'Инициализация GigaChat: {type(error).__name__}') from None

    @classmethod
    def from_env(cls, ca_path: Path = CA_PATH):
        """Ключ остаётся внутри процесса; публичные протоколы не содержат его даже при сбоях."""
        load_dotenv(Path(__file__).resolve().parents[2]/'.env', override=False)
        return cls(os.environ.get('GIGACHAT_KEY', ''), ca_path)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def available_models(self) -> list[str]:
        """Проверка авторизации без генерации."""
        try:
            return [model.id_ for model in self.client.get_models().data]
        except Exception as error:
            raise ProviderError(f'Список моделей: {type(error).__name__}') from None

    def extract(self, system: str, text: str, schema: dict) -> Completion:
        """Не исполняет функции или инструкции из PDF; вывод принимается только как JSON-данные."""
        if len(system) + len(text) > 65000:
            raise ProviderError('Превышен лимит текста одного запроса; разбейте выбранные страницы')
        try:
            result = self.client.chat(Chat(
                model=MODEL, messages=[Messages(role=MessagesRole.SYSTEM, content=system),
                                       Messages(role=MessagesRole.USER, content=text)],
                temperature=0, max_tokens=12000, response_format={'type': 'json_schema', 'schema': schema, 'strict': True}))
            if len(result.choices) != 1 or result.choices[0].finish_reason != 'stop':
                raise ProviderError('Модель не завершила JSON-ответ полностью')
            content = result.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise ProviderError('Модель не вернула текст JSON')
            return Completion(content, result.model, result.usage.total_tokens)
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(f'Извлечение GigaChat: {type(error).__name__}') from None
