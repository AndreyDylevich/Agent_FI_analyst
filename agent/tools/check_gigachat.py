"""Обезличенная проверка GigaChat с проверкой TLS и короткой генерацией JSON, без Telegram/основной БД."""

import argparse
import hashlib
import json
from pathlib import Path

import requests
from cryptography import x509

from agent.extract.llm import GigaChatProvider, MODEL, ProviderError

CERT_URL = 'https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt'


def download_certificate(target: Path) -> dict:
    """Официальный публичный сертификат, HTTPS без отключения проверки цепочки сервера."""
    with requests.get(CERT_URL, timeout=(10, 30)) as response:
        response.raise_for_status()
        if len(response.content) > 50000:
            raise ValueError('Недопустимый размер сертификата')
        content = response.content
    certificate = x509.load_pem_x509_certificate(content)
    certificate.verify_directly_issued_by(certificate)
    if not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError('Сертификат не является корневым CA')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {'source': CERT_URL, 'sha256': hashlib.sha256(content).hexdigest()}


def run_check(output: Path) -> dict:
    """Записывает только статусы/модель/счётчик токенов; тела ошибок SDK не раскрываются."""
    output.mkdir(parents=True, exist_ok=True)
    report = {'ok': False, 'tls_verified': True, 'model': MODEL, 'messages_sent': 0}
    try:
        cert = output/'russian_trusted_root_ca.cer'
        report['certificate'] = download_certificate(cert)
        with GigaChatProvider.from_env(cert) as provider:
            models = provider.available_models()
            report['model_available'] = any(m.split(':')[0] == MODEL for m in models)
            if not report['model_available']:
                raise ProviderError('Согласованная модель не найдена; автоматическая замена запрещена')
            completion = provider.extract('Верни только заданный JSON.', 'Верни объект {"status":"ready"}.',
                                          {'type': 'object', 'properties': {'status': {'type': 'string', 'enum': ['ready']}},
                                           'required': ['status'], 'additionalProperties': False})
            report['json_generation_verified'] = json.loads(completion.content) == {'status': 'ready'}
            report['tokens'] = completion.tokens
            report['response_model'] = completion.model
            report['ok'] = report['json_generation_verified']
    except Exception as error:
        report['error'] = str(error) if isinstance(error, ProviderError) else type(error).__name__
    (output/'gigachat_check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    return 0 if run_check(args.output)['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
