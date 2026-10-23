# Agent_FI_analyst

ИИ-агент финансового анализа российских публичных компаний: находит официальную отчётность,
извлекает данные, считает показатели по реестру, выявляет тревожные сигналы и отправляет отчёт в Telegram.

- Архитектура, методология, источники и статус этапов — [CLAUDE.md](CLAUDE.md).
- Требования к содержанию анализа — [docs/requirements.md](docs/requirements.md).

## Установка

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # заполнить ключи
```

Для GigaChat нужен корневой сертификат Минцифры: скачать с https://www.gosuslugi.ru/crt
и положить в `certs/russian_trusted_root_ca.cer`.

## Тесты

```bash
.venv/bin/pytest -q
```

## Служебные команды

```bash
# Поиск организации в ГИР БО (кандидаты на ИНН)
.venv/bin/python -m agent.tools.find_inn "Сегежа Групп"
```
