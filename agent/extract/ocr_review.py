"""Явная визуальная приёмка выбранных итогов OCR-баланса с неизменяемым журналом решения."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from agent import config
from agent.checks.ifrs import RULES
from agent.checks.validate import reconcile
from agent.extract.pdf_text import PDFText
from agent.extract.restatements import original_status
from agent.extract.schemas import _keys, parse_amount
from agent.metrics.data import UNIT_MULTIPLIERS, normalize_money

BALANCE_ITEMS = {'total_assets', 'non_current_assets', 'current_assets', 'equity_total',
                 'non_current_liabilities', 'current_liabilities', 'total_equity_and_liabilities'}
BALANCE_RULES = ('assets_components', 'balance', 'balance_totals')


def reference_sha256(reference: dict) -> str:
    """Хеш содержания независимого эталона, без зависимости от отступов JSON."""
    return hashlib.sha256(json.dumps(reference, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def apply_ocr_balance_review(conn: sqlite3.Connection, pdf: PDFText, reference: dict, review: dict) -> dict:
    """Вызывается в транзакции после просмотра изображений. Автоматическая сверка этого не вызывает.

    Первая версия принимает только семь итогов баланса на две даты. Приёмка не подтверждает
    остальные страницы, учётную базу/периметр или сопоставимость и не исправляет исходные числа.
    """
    _keys(review, {'version', 'ticker', 'reviewer', 'reason', 'pdf_sha256', 'reference_sha256',
                   'reviewed_pages', 'visual_review_confirmed'})
    if (type(review['version']) is not int or review['version'] != 1
            or review['visual_review_confirmed'] is not True
            or not all(isinstance(review[key], str) and review[key].strip()
                       for key in ('ticker', 'reviewer', 'reason'))):
        raise ValueError('Нужны явное подтверждение визуальной проверки, автор и обоснование')
    pages = review['reviewed_pages']
    if (not isinstance(pages, list) or not pages or len(set(pages)) != len(pages)
            or any(type(page) is not int or not 1 <= page <= len(pdf.pages) for page in pages)):
        raise ValueError('Нужен непустой список просмотренных физических страниц PDF')
    if (review['pdf_sha256'] != pdf.sha256 or reference['pdf_sha256'] != pdf.sha256
            or review['reference_sha256'] != reference_sha256(reference)):
        raise ValueError('SHA256 PDF или независимого эталона не соответствует решению')
    if reference.get('standard') != 'IFRS' or reference.get('scope') != 'group':
        raise ValueError('Эталон должен относиться к группе по IFRS')
    documents = conn.execute('SELECT d.* FROM documents d JOIN companies c ON c.id=d.company_id '
                             'WHERE d.sha256=? AND c.ticker=?', (pdf.sha256, review['ticker'])).fetchall()
    if len(documents) != 1 or documents[0]['url'] != reference['source_url']:
        raise ValueError('Нет единственной выбранной версии документа этой компании')
    document = documents[0]
    if document['standard'] != 'IFRS' or document['period_type'] not in ('FY', 'H1', 'Q1', '9M'):
        raise ValueError('Документ не относится к выбранной отчётности IFRS')
    ends = {document['period_end'], f"{int(document['period_end'][:4]) - 1}-12-31"}
    expected = reference['rows']
    pairs = [(row['item_id'], row['start'], row['end']) for row in expected]
    if (len(pairs) != 14 or set(pairs) != {(item, None, end) for item in BALANCE_ITEMS for end in ends}
            or any(row['status'] != 'не_проверено' for row in expected)):
        raise ValueError('Для первой приёмки нужен независимый эталон 14 непроверенных итогов баланса')
    digest = reference_sha256(review)
    key = f'ifrs_ocr_review:{digest}'
    previous = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    prior = json.loads(previous['value']) if previous else None
    known_ids = {change['source_id'] for change in prior['changes']} if prior else set()
    changes, selected, contexts = [], {}, {}
    required_pages = set()
    items = config.line_items()
    for expected_row in expected:
        rows = conn.execute('SELECT * FROM source_values WHERE document_id=? AND item_id=? '
                            'AND period_start IS NULL AND period_end=?',
                            (document['id'], expected_row['item_id'], expected_row['end'])).fetchall()
        if not rows:
            raise ValueError('В БД отсутствует явно проверяемый итог баланса')
        for row in rows:
            if (row['company_id'] != document['company_id'] or row['standard'] != 'IFRS'
                    or row['scope'] != 'group' or row['segment_name'] is not None or row['currency'] != 'RUB'
                    or row['cash_flow_section'] is not None or row['superseded_by'] is not None):
                raise ValueError('Неверный охват/валюта или заменённая версия исходного значения')
            allowed_status = 'проверено_вручную' if row['id'] in known_ids else 'не_проверено'
            if row['check_status'] != allowed_status:
                raise ValueError('Исходное расхождение или чужое решение нельзя одобрить приёмкой OCR')
            location = json.loads(row['location'])
            if type(location['fact_index']) is not int or location['fact_index'] < 0:
                raise ValueError('Некорректный адрес строки в исходном протоколе')
            original_status(conn, row)
            source_key = f"ifrs_extraction:{document['id']}:{location['extraction_sha256']}"
            if source_key not in contexts:
                contexts[source_key] = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', (source_key,)).fetchone()['value'])
            run = contexts[source_key]
            fact = run['facts'][location['fact_index']]
            if (run['pdf_sha256'] != pdf.sha256 or not run['contexts'] or not fact['from_ocr'] or fact['statement'] != 'balance'
                    or run['statuses'][location['fact_index']] != 'не_проверено'
                    or fact['evidence']['page'] != location['page'] or fact['evidence']['quote'] != location['quote']
                    or fact['unit_evidence'] != location['unit_evidence']
                    or fact['original_name'] != row['original_name'] or fact['raw_value'] != location['raw_value']
                    or fact['number_format'] != location['number_format'] or fact['column_header'] != location['column_header']
                    or parse_amount(fact['raw_value'], fact['number_format']) != Decimal(row['value'])):
                raise ValueError('Нет неизменённого исходного протокола OCR для выбранной строки')
            if (row['unit'] != reference['unit'] or Decimal(row['value']) != Decimal(expected_row['value'])
                    or Decimal(row['value_norm']) != normalize_money(expected_row['value'], reference['unit'], items[row['item_id']]['sign'])
                    or Decimal(expected_row.get('normalized_value', row['value_norm'])) != Decimal(row['value_norm'])
                    or location['page'] not in expected_row['pages']):
                raise ValueError('Значение, знак, масштаб или страница не совпадают с независимым эталоном')
            required_pages.update((location['page'], fact['unit_evidence']['page']))
            for context in run['contexts']:
                if (context['ticker'] != review['ticker'] or context['standard'] != 'IFRS'
                        or context['scope'] != 'group' or context['currency'] != 'RUB'
                        or context['period_end'] != document['period_end']
                        or context['period_start'] != document['period_start']
                        or context['period_type'] != document['period_type']):
                    raise ValueError('Метаданные протокола не соответствуют документу')
                for field in ('identity_evidence', 'standard_evidence', 'scope_evidence', 'currency_evidence', 'period_evidence'):
                    required_pages.add(context[field]['page'])
            selected[(row['item_id'], row['period_end'])] = Decimal(row['value_norm'])
            changes.append({'source_id': row['id'], 'item_id': row['item_id'], 'period_end': row['period_end'],
                            'value': row['value'], 'value_norm': row['value_norm'], 'unit': row['unit'],
                            'page': location['page'], 'extraction_sha256': location['extraction_sha256'],
                            'status_before': 'не_проверено', 'status_after': 'проверено_вручную'})
    if not required_pages <= set(pages):
        raise ValueError('Не подтверждён просмотр всех страниц чисел, единиц и метаданных')
    # Пропуск не превращается в ноль; исходная ошибка не исправляется ради балансового равенства.
    for end in ends:
        for rule in BALANCE_RULES:
            check = reconcile(rule, {item: selected[(item, end)] for item in RULES[rule]}, RULES[rule],
                              tolerance=UNIT_MULTIPLIERS[reference['unit']])
            saved = [check for run in contexts.values() for check in run['checks']
                     if check['rule'] == rule and check['start'] is None and check['end'] == end]
            if check.status != 'пройдено' or not saved or any(c['result']['status'] != 'пройдено' for c in saved):
                raise ValueError('Бухгалтерская сверка не пройдена: ручная приёмка OCR не устраняет расхождение')
    if prior:
        if (known_ids != {change['source_id'] for change in changes} or prior['review'] != review
                or prior['reference'] != reference or prior['document_id'] != document['id']
                or prior['company_id'] != document['company_id']
                or any(prior[field] != document[field] for field in ('period_start', 'period_end', 'period_type'))):
            raise ValueError('Состав исходных строк изменился после решения')
        return prior
    result = {'review_sha256': digest, 'review': review, 'reference': reference,
              'document_id': document['id'], 'company_id': document['company_id'],
              'period_start': document['period_start'], 'period_end': document['period_end'],
              'period_type': document['period_type'], 'changes': changes,
              'reviewed_at_utc': datetime.now(timezone.utc).isoformat(),
              'source_numbers_modified': False, 'comparability_approved': False,
              'financial_audit_complete': False}
    for change in changes:
        conn.execute("UPDATE source_values SET check_status='проверено_вручную' WHERE id=?", (change['source_id'],))
    conn.execute('INSERT INTO meta(key,value) VALUES (?,?)', (key, json.dumps(result, ensure_ascii=False, sort_keys=True)))
    return result
