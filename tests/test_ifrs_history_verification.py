"""Приёмка балансов не допускает подмены года/источника и сохраняет блокировку OCR."""

import copy
import hashlib
import json
from dataclasses import replace
from datetime import date

import pytest

from agent import db
from agent.extract.store import store_extraction
from agent.metrics.engine import Period
from agent.tools import verify_ifrs_history as tool
from tests.test_ifrs_pipeline import facts, make_run


def sample(tmp_path, *, ocr=False):
    conn = db.init_db(tmp_path / 'check.db')
    rows = facts()
    rows = (*rows, replace(rows[0], item_id='total_equity_and_liabilities'))
    rows = tuple(replace(row, end=date(year, 12, 31), period_type='FY', from_ocr=ocr)
                 for year in (2025, 2024) for row in rows)
    url = 'https://example.test/annual.pdf'
    with conn:
        store_extraction(conn, make_run(rows), ticker='MTSS', name='Тест', source_url=url,
                         period=Period(date(2025, 1, 1), date(2025, 12, 31), 'FY'), local_path='annual.pdf')
    entry = {'ticker': 'MTSS', 'expected_year': 2025, 'pdf_sha256': 'a' * 64, 'source_url': url}
    reference = {'source_specific': True, 'pdf_sha256': 'a' * 64, 'source_url': url,
                 'unit': 'млн руб.', 'multiplier': '1000000', 'checks_required': list(tool.BALANCE_RULES),
                 'rows': [{'item_id': row.item_id, 'start': None, 'end': str(row.end),
                           'value': str(row.value), 'pages': [1],
                           'status': 'не_проверено' if ocr else 'проверено_авто'} for row in rows]}
    return conn, entry, reference


@pytest.mark.parametrize('ocr', [False, True])
def test_complete_balances_verified_read_only_without_promoting_ocr(tmp_path, ocr):
    conn, entry, reference = sample(tmp_path, ocr=ocr)
    before = '\n'.join(conn.iterdump())
    result = tool.verify_history(conn, 'MTSS', [entry], {2025: reference})
    assert result['ok'] and result['source_values_count'] == 14
    assert len(result['years'][0]['period_checks']) == 6
    assert not result['financial_history_validated'] and not result['comparability_approved']
    assert result['unavailable_report_years'] == [2021, 2022, 2023, 2024]
    assert not result['requested_window_complete']
    assert result['years'][0]['stored_statuses'] == {'не_проверено' if ocr else 'проверено_авто': 14}
    assert '\n'.join(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize('change', ['missing_row', 'duplicate_row', 'sha', 'url', 'no_source_scope'])
def test_incomplete_or_different_reference_cannot_pass(tmp_path, change):
    conn, entry, reference = sample(tmp_path)
    if change == 'missing_row': reference['rows'].pop()
    if change == 'duplicate_row': reference['rows'][-1] = reference['rows'][0]
    if change == 'sha': reference['pdf_sha256'] = 'f' * 64
    if change == 'url': reference['source_url'] = 'https://example.test/other.pdf'
    if change == 'no_source_scope': reference['source_specific'] = False
    result = tool.verify_history(conn, 'MTSS', [entry], {2025: reference})
    assert not result['ok'] and result['years'][0]['error']
    conn.close()


def rewrite_context(conn, transform):
    """Новая целостная версия протокола позволяет проверить отдельное условие приёмки."""
    saved = conn.execute("SELECT * FROM meta WHERE key LIKE 'ifrs_extraction:%'").fetchone()
    payload = json.loads(saved['value'])
    transform(payload)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    document = conn.execute('SELECT id FROM documents').fetchone()[0]
    with conn:
        conn.execute('DELETE FROM meta WHERE key=?', (saved['key'],))
        conn.execute('INSERT INTO meta(key,value) VALUES (?,?)',
                     (f'ifrs_extraction:{document}:{digest}', serialized))
        for row in conn.execute('SELECT id,location FROM source_values').fetchall():
            location = json.loads(row['location'])
            location['extraction_sha256'] = digest
            conn.execute('UPDATE source_values SET location=? WHERE id=?', (json.dumps(location), row['id']))


def test_passed_identity_in_other_year_cannot_replace_comparative_check(tmp_path):
    conn, entry, reference = sample(tmp_path)
    rewrite_context(conn, lambda payload: payload.update(checks=[check for check in payload['checks']
                    if not (check['rule'] == 'balance' and check['end'] == '2024-12-31')]))
    result = tool.verify_history(conn, 'MTSS', [entry], {2025: reference})
    assert not result['ok']
    failed = [check for check in result['years'][0]['period_checks'] if not check['passed']]
    assert [(check['rule'], check['period_end']) for check in failed] == [('balance', '2024-12-31')]
    conn.close()


def test_unchanged_numbers_do_not_hide_corrupt_extraction_protocol(tmp_path):
    conn, entry, reference = sample(tmp_path)
    with conn:
        conn.execute("UPDATE meta SET value=value||' ' WHERE key LIKE 'ifrs_extraction:%'")
    result = tool.verify_history(conn, 'MTSS', [entry], {2025: reference})
    assert not result['ok'] and 'Контрольная сумма' in result['years'][0]['error']
    conn.close()


@pytest.mark.parametrize('change', ['company', 'currency', 'report_year', 'missing_reference'])
def test_other_company_currency_or_report_year_rejected(tmp_path, change):
    conn, entry, reference = sample(tmp_path)
    if change == 'company': entry['ticker'] = 'SGZH'
    if change == 'currency': conn.execute("UPDATE source_values SET currency='USD'")
    if change == 'report_year': conn.execute("UPDATE documents SET period_end='2024-12-31'")
    result = tool.verify_history(conn, entry['ticker'], [entry], {} if change == 'missing_reference' else {2025: reference})
    assert not result['ok']
    conn.close()


def test_no_documents_is_not_success_and_duplicate_manifest_year_is_rejected(tmp_path):
    conn, entry, reference = sample(tmp_path)
    assert not tool.verify_history(conn, 'MVID', [entry], {})['ok']
    with pytest.raises(ValueError, match='неоднозначный'):
        tool.verify_history(conn, 'MTSS', [entry, copy.deepcopy(entry)], {2025: reference})
    conn.close()


def test_cli_opens_database_read_only_and_preserves_old_output(tmp_path, monkeypatch):
    conn, entry, reference = sample(tmp_path)
    conn.close()
    manifest = tmp_path / 'sources.json'
    manifest.write_text(json.dumps({'documents': [entry]}))
    monkeypatch.setattr(tool, 'MANIFEST', manifest)
    monkeypatch.setattr(tool, 'REFERENCES', tmp_path)
    (tmp_path / 'mts_fy2025_balance_reference.json').write_text(json.dumps(reference))
    database = tmp_path / 'check.db'
    before = database.read_bytes()
    output = tmp_path / 'result.json'
    command = ['--ticker', 'MTSS', '--db', str(database), '--output', str(output)]
    assert tool.main(command) == 0
    assert database.read_bytes() == before
    original = output.read_bytes()
    with pytest.raises(SystemExit): tool.main(command)
    assert output.read_bytes() == original
