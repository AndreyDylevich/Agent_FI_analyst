"""Сохранение МСФО группы с историей ответов, цитатами и блокировкой противоречащих источников."""

import hashlib
import json
import sqlite3
from dataclasses import asdict
from decimal import Decimal

from agent.extract.pipeline import ExtractionRun, PROMPT_VERSION
from agent.extract.schemas import VERSION
from agent.metrics.engine import Period


def store_extraction(conn: sqlite3.Connection, run: ExtractionRun, *, ticker: str, name: str,
                     source_url: str, period: Period, local_path: str) -> dict:
    """Транзакция у вызывающего. Разные ответы не перезаписывают историю и не создают рестейтмент."""
    if len(run.facts) != len(run.statuses):
        raise ValueError('Количество исходных значений и статусов проверки различается')
    conn.execute('INSERT OR IGNORE INTO companies (ticker,name) VALUES (?,?)', (ticker, name))
    company_id = conn.execute('SELECT id FROM companies WHERE ticker=?', (ticker,)).fetchone()['id']
    row = conn.execute('SELECT * FROM documents WHERE url=? AND sha256=?', (source_url, run.pdf_sha256)).fetchone()
    if row and row['company_id'] != company_id:
        raise ValueError('Документ уже связан с другой компанией')
    if row:
        document_id = row['id']
    else:
        cursor = conn.execute('INSERT INTO documents (company_id,url,sha256,title,doc_type,standard,period_start,'
                              'period_end,period_type,status,source,local_path) '
                              "VALUES (?,?,?,?,'ifrs','IFRS',?,?,?,'downloaded','ir_page',?)",
                              (company_id, source_url, run.pdf_sha256, name+' МСФО '+period.kind,
                               str(period.start), str(period.end), period.kind, local_path))
        document_id = cursor.lastrowid
    payload = {'schema_version': VERSION, 'prompt_version': PROMPT_VERSION, **asdict(run)}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    key = f'ifrs_extraction:{document_id}:{digest}'
    existing = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    if existing:
        return {'company_id': company_id, 'document_id': document_id, 'inserted': 0, 'conflicts': [], 'run_sha256': digest}
    conflicts = []
    for index, (fact, check_status) in enumerate(zip(run.facts, run.statuses)):
        previous = conn.execute("SELECT * FROM source_values_current WHERE company_id=? AND item_id=? "
                                "AND standard='IFRS' AND scope='group' AND segment_name IS NULL AND currency='RUB' "
                                'AND period_start IS ? AND period_end=? AND cash_flow_section IS ?',
                                (company_id, fact.item_id, str(fact.start) if fact.start else None,
                                 str(fact.end), fact.cash_flow_section)).fetchall()
        disagreement = [r for r in previous if r['value_norm'] is None or Decimal(r['value_norm']) != fact.normalized]
        if disagreement:
            check_status = 'расхождение'
            ids = [r['id'] for r in previous]
            conn.executemany("UPDATE source_values SET check_status='расхождение' WHERE id=?", [(i,) for i in ids])
            conflicts.append({'item_id': fact.item_id, 'period_end': str(fact.end), 'previous_source_ids': ids})
        location = json.dumps({'page': fact.evidence.page, 'quote': fact.evidence.quote,
                               'column_header': fact.column_header, 'unit_evidence': asdict(fact.unit_evidence),
                               'raw_value': fact.raw_value, 'number_format': fact.number_format,
                               'text_method': 'ocr' if fact.from_ocr else 'native',
                               'extraction_sha256': digest, 'fact_index': index}, ensure_ascii=False)
        conn.execute('INSERT INTO source_values (company_id,item_id,original_name,value,unit,value_norm,currency,'
                     'period_start,period_end,period_type,standard,scope,document_id,source,location,extracted_by,'
                     'check_status,cash_flow_section) VALUES (?,?,?,?,?,?,\'RUB\',?,?,?,?,?,?,?,?,?,?,?)',
                     (company_id, fact.item_id, fact.original_name, str(fact.value), fact.unit, str(fact.normalized),
                      str(fact.start) if fact.start else None, str(fact.end), fact.period_type, 'IFRS', 'group', document_id,
                      'ir_page', location, 'gigachat', check_status, fact.cash_flow_section))
    conn.execute('INSERT INTO meta (key,value) VALUES (?,?)', (key, serialized))
    conn.execute("UPDATE documents SET status='extracted' WHERE id=?", (document_id,))
    return {'company_id': company_id, 'document_id': document_id, 'inserted': len(run.facts),
            'conflicts': conflicts, 'run_sha256': digest}
