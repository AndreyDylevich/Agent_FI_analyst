"""Общая сверка сохранённых извлечений одного PDF, без записи или повышения статусов."""

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal

from agent import config
from agent.checks.ifrs import RULES, validate_financials
from agent.checks.equity import compile_plans
from agent.checks.cash_sections import CASH_RULES, compile_cash_sections
from agent.checks.debt_bridges import DEBT_RULES, compile_debt_bridges
from agent.checks.source_links import SOURCE_RULES, compile_source_links
from agent.checks.reviewed_tables import compile_reviewed_tables
from agent.extract.pdf_text import PDFText
from agent.extract.ocr_review import apply_ocr_balance_review, reference_sha256
from agent.extract.ocr_source_review import apply_ocr_source_review
from agent.extract.restatements import original_status
from agent.extract.schemas import Citation, ExtractedFact, citation, parse_amount
from agent.metrics.data import normalize_money
from agent.metrics.engine import Period


def _fact(data: dict) -> ExtractedFact:
    """Восстанавливает исходный факт, а не извлекает число из таблицы."""
    return ExtractedFact(**{**data, 'value': Decimal(data['value']), 'normalized': Decimal(data['normalized']),
                            'start': date.fromisoformat(data['start']) if data['start'] else None,
                            'end': date.fromisoformat(data['end']),
                            'evidence': Citation(**data['evidence']), 'unit_evidence': Citation(**data['unit_evidence'])})


def _check_citation(proof: dict, pdf: PDFText) -> None:
    """Повторно проверяет дословную цитату на физических страницах оригинального PDF."""
    pages = {page.number: page for page in pdf.pages}
    address = {key: proof.get(key) for key in ('page', 'start_line', 'end_line')}
    if proof.get('start_line') is None:
        address = {'page': proof['page'], 'quote': proof['quote']}
    if citation(address, pages).quote != proof['quote']:
        raise ValueError('Цитата исходного протокола не совпадает с текстом PDF')


def _verify_ocr_review(conn: sqlite3.Connection, pdf: PDFText, source_id: int, verified_ids: set[int] | None = None) -> None:
    """Ручной статус OCR требует существующего валидного журнала; новых решений здесь нет."""
    if verified_ids is not None and source_id in verified_ids:
        return
    for saved in conn.execute("SELECT key,value FROM meta WHERE key LIKE 'ifrs_ocr_review:%'"):
        review = json.loads(saved['value'])
        if not any(change['source_id'] == source_id for change in review['changes']):
            continue
        if saved['key'] != f"ifrs_ocr_review:{reference_sha256(review['review'])}":
            raise ValueError('Контрольная сумма решения о приёмке OCR не соответствует журналу')
        # Ключ уже существует: процедура только проверяет решение и возвращает его без записи.
        procedure=apply_ocr_source_review if review['review'].get('version') == 2 else apply_ocr_balance_review
        procedure(conn, pdf, review['reference'], review['review'])
        if verified_ids is not None:
            verified_ids.update(change['source_id'] for change in review['changes'])
        return
    raise ValueError('Ручной статус OCR не подтверждён существующим журналом визуальной приёмки')


def load_document(conn: sqlite3.Connection, pdf: PDFText, ticker: str) -> tuple[dict, list[tuple[ExtractedFact, dict]]]:
    """Принимает только точную версию документа, компанию/IFRS/группу/RUB и целый протокол."""
    documents = conn.execute('SELECT d.* FROM documents d JOIN companies c ON c.id=d.company_id '
                             'WHERE d.sha256=? AND c.ticker=?', (pdf.sha256, ticker)).fetchall()
    if len(documents) != 1 or documents[0]['standard'] != 'IFRS':
        raise ValueError('В каждой выбранной БД нужна единственная точная версия PDF этой компании по IFRS')
    document = dict(documents[0])
    rows = conn.execute('SELECT * FROM source_values_current WHERE document_id=? ORDER BY id',
                         (document['id'],)).fetchall()
    if not rows:
        raise ValueError('В выбранном документе нет актуальных извлечённых значений')
    contexts, result, verified_ocr_ids = {}, [], set()
    items = config.line_items()
    for row in rows:
        if (row['company_id'] != document['company_id'] or row['standard'] != 'IFRS' or row['scope'] != 'group'
                or row['segment_name'] is not None or row['currency'] != 'RUB'):
            raise ValueError('Несогласованные компания, стандарт, охват или валюта исходных данных')
        location = json.loads(row['location'])
        if type(location['fact_index']) is not int or location['fact_index'] < 0:
            raise ValueError('Некорректный адрес факта в исходном протоколе')
        original_status(conn, row)
        key = f"ifrs_extraction:{document['id']}:{location['extraction_sha256']}"
        if key not in contexts:
            contexts[key] = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()['value'])
        run = contexts[key]
        fact = _fact(run['facts'][location['fact_index']])
        if (run['pdf_sha256'] != pdf.sha256 or not run['contexts'] or fact.item_id not in items
                or fact.original_name != row['original_name'] or fact.cash_flow_section != row['cash_flow_section']
                or fact.period_type != row['period_type'] or asdict(fact.evidence) != {
                    key: location[key] for key in ('page', 'quote', 'start_line', 'end_line')}
                or asdict(fact.unit_evidence) != location['unit_evidence']
                or fact.raw_value != location['raw_value'] or fact.number_format != location['number_format']
                or fact.column_header != location['column_header']
                or parse_amount(fact.raw_value, fact.number_format) != fact.value
                or normalize_money(str(fact.value), fact.unit, items[fact.item_id]['sign']) != fact.normalized):
            raise ValueError('Сохранённая строка не соответствует исходному факту/нормализации/протоколу')
        for context in run['contexts']:
            expected = {'ticker': ticker, 'standard': 'IFRS', 'scope': 'group', 'currency': 'RUB',
                        'period_start': document['period_start'], 'period_end': document['period_end'],
                        'period_type': document['period_type']}
            if any(context.get(key) != value for key, value in expected.items()):
                raise ValueError('Метаданные исходного протокола не совпадают с выбранным выпуском')
            if not fact.from_ocr:
                for field in ('identity_evidence', 'standard_evidence', 'scope_evidence', 'currency_evidence', 'period_evidence'):
                    _check_citation(context[field], pdf)
        if not fact.from_ocr:
            _check_citation(asdict(fact.evidence), pdf)
            _check_citation(asdict(fact.unit_evidence), pdf)
        reviewed_ocr = fact.from_ocr and row['check_status'] == 'проверено_вручную'
        if reviewed_ocr:
            _verify_ocr_review(conn, pdf, row['id'], verified_ocr_ids)
        # OCR допускается в арифметическую диагностику, но не получает одобрения этим чтением.
        result.append((fact, {'source_id': row['id'], 'extraction_sha256': location['extraction_sha256'],
                              'stored_status': row['check_status'], 'page': fact.evidence.page,
                              'citation_rechecked_from_pdf': not fact.from_ocr,
                              'ocr_review_verified': reviewed_ocr}))
    return document, result


def coverage(checks: list[dict], period: Period, specification: dict, implemented_rules: set[str] = frozenset()) -> list[dict]:
    """Проверяет каждое обязательное окно; успех текущего года не заменяет сравнительный."""
    prior = period.shift_years(-1)
    windows = {'flow': (str(period.start), str(period.end)), 'comparative_flow': (str(prior.start), str(prior.end)),
               'end': (None, str(period.end)), 'comparative_end': (None, str(prior.end)),
               'previous_year_end': (None, f'{period.end.year - 1}-12-31'),
               'opening': (None, str(period.start - timedelta(days=1))),
               'comparative_opening': (None, str(prior.start - timedelta(days=1)))}
    families = specification['families']
    if not families or len({family['id'] for family in families}) != len(families):
        raise ValueError('Нужен непустой каталог различных обязательных сверок')
    result = []
    for family in families:
        requirements = []
        if not family['checks']:
            raise ValueError('Пустой набор обязательных проверок не считается успехом')
        for required in family['checks']:
            if not required['periods']:
                raise ValueError('У обязательной проверки отсутствуют периоды')
            for window in required['periods']:
                start, end = windows[window]
                matching = [check for check in checks if check['rule'] == required['rule']
                            and check['start'] == start and check['end'] == end]
                states = {check['status'] for check in matching}
                status = ('расхождение' if 'расхождение' in states else
                          'пройдено' if states == {'пройдено'} else 'недостаточно_данных')
                missing = sorted({item for check in matching for item in check['result']['missing']})
                requirements.append({'rule': required['rule'], 'start': start, 'end': end, 'status': status,
                                     'missing_inputs': missing if matching else list(RULES.get(required['rule'], {})),
                                     'implemented': required['rule'] in RULES or required['rule']=='complete_equity_rollforward' or required['rule'] in CASH_RULES | DEBT_RULES | SOURCE_RULES.keys() | implemented_rules})
        states = {check['status'] for check in requirements}
        result.append({'id': family['id'], 'name': family['name'], 'requirements': requirements,
                       'limitation': family.get('limitation'),
                       'status': 'расхождение' if 'расхождение' in states else
                                 'пройдено' if states == {'пройдено'} else 'недостаточно_данных'})
    return result


def reconcile_document(sources: list[tuple[str, sqlite3.Connection]], pdf: PDFText, ticker: str) -> dict:
    """Объединяет равные подтверждения, сохраняя все адреса и исходные блокировки, без записи в БД."""
    if not sources:
        raise ValueError('Нужна хотя бы одна БД исходных извлечений')
    facts, origins, by_signature, metadata = [], [], {}, None
    for label, conn in sources:
        document, loaded = load_document(conn, pdf, ticker)
        identity = {key: document[key] for key in ('url', 'standard', 'period_start', 'period_end', 'period_type')}
        if metadata is not None and identity != metadata:
            raise ValueError('Нельзя объединять разные выпуски или окна отчётности')
        metadata = identity
        for fact, source in loaded:
            payload = asdict(fact)
            payload.pop('model_raw_value', None)
            payload.pop('model_observation_key', None)
            signature = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
            if signature not in by_signature:
                by_signature[signature] = len(facts)
                facts.append(fact)
                origins.append([])
            origins[by_signature[signature]].append({'database': label, **source})
    period = Period(date.fromisoformat(metadata['period_start']), date.fromisoformat(metadata['period_end']), metadata['period_type'])
    review = compile_source_links(pdf, period)
    reviewed_document = compile_reviewed_tables(pdf, period)
    checks, statuses = validate_financials(tuple(facts),equity_plans=compile_plans(pdf,period),cash_plans=compile_cash_sections(pdf,period),debt_plans=compile_debt_bridges(pdf,period),source_review=review,reviewed_document=reviewed_document)
    inherited = {index for index, references in enumerate(origins)
                 if any(source['stored_status'] == 'расхождение' for source in references)}
    newly_disputed = {index for index, status in enumerate(statuses) if status == 'расхождение'}
    output = []
    for check in checks:
        blocked = [index for index in check.fact_indices if index in inherited | newly_disputed]
        unreviewed_ocr = [index for index in check.fact_indices if facts[index].from_ocr and
                          any(not source['ocr_review_verified'] for source in origins[index])]
        status = ('расхождение' if check.result.status == 'расхождение' or blocked else
                  'недостаточно_данных' if unreviewed_ocr else check.result.status)
        output.append({**asdict(check), 'status': status, 'arithmetic_status': check.result.status,
                       'inherited_discrepancy_indices': [index for index in blocked if index in inherited],
                       'disputed_input_indices': blocked, 'unreviewed_ocr_indices': unreviewed_ocr,
                       'inputs': [{'fact_index': index, 'item_id': facts[index].item_id,
                                   'normalized_value': str(facts[index].normalized), 'origins': origins[index]}
                                  for index in check.fact_indices]})
    specification = config.load('ifrs_cross_form_checks')
    families = coverage(output, period, specification,
                        {c['rule'] for c in reviewed_document.identities} if reviewed_document else set())
    complete = all(family['status'] == 'пройдено' for family in families)
    discrepancies = any(check['status'] == 'расхождение' for check in output)
    return {'ticker': ticker, 'pdf_sha256': pdf.sha256, **metadata, 'source_integrity_verified': True,
            'source_observations_count': sum(map(len, origins)), 'distinct_facts_count': len(facts),
            'checks': output, 'check_status_counts': dict(Counter(check['status'] for check in output)),
            'cross_form_families': families, 'coverage_version': specification['version'],
            'cross_form_complete': complete, 'has_discrepancies': discrepancies,
            'ok': complete and not discrepancies, 'source_statuses_modified': False,
            'financial_audit_complete': False, 'comparability_approved': False, 'messages_sent': 0,
            'reviewed_disclosure_gaps': list(review.gaps if review else ()) + list(reviewed_document.gaps if reviewed_document else ()),
            'limitation': 'Повторная арифметическая диагностика сохранённых извлечений одного PDF. '
                          'Статусы исходных строк не повышаются и не записываются. '
                          'Данные разных версий PDF не смешиваются. Неполные и ещё не реализованные '
                          'полные мосты показаны отдельно; успешная частичная сверка их не заменяет.'}
