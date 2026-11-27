"""Визуальная приёмка OCR адресна, воспроизводима и не исправляет ошибки исходной отчётности."""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from agent import config, db
from agent.extract.ocr_review import apply_ocr_balance_review, reference_sha256
from agent.extract.pdf_text import PDFText, Page
from agent.extract.schemas import Citation
from agent.extract.store import store_extraction
from agent.metrics.engine import Engine
from agent.tools import review_ifrs_ocr as tool
from tests.test_extraction_schema import PERIOD
from tests.test_ifrs_pipeline import facts, make_run


def setup(tmp_path):
    conn = db.init_db(tmp_path / 'ocr.db')
    base = facts()
    base = (*base, replace(base[0], item_id='total_equity_and_liabilities'))
    rows = tuple(replace(row, end=day, period_type=kind, from_ocr=True,
                         evidence=Citation(2, f'{row.item_id} {row.raw_value}'),
                         unit_evidence=Citation(3, 'В миллионах российских рублей'))
                 for day, kind in ((date(2026, 6, 30), 'H1'), (date(2025, 12, 31), 'FY')) for row in base)
    context = {'ticker': 'TEST', 'standard': 'IFRS', 'scope': 'group', 'currency': 'RUB',
               'period_start': '2026-01-01', 'period_end': '2026-06-30', 'period_type': 'H1'}
    for field in ('identity_evidence', 'standard_evidence', 'scope_evidence', 'currency_evidence', 'period_evidence'):
        context[field] = {'page': 1, 'quote': 'Контекст отчётности'}
    run = replace(make_run(rows), contexts=(context,))
    with conn:
        stored = store_extraction(conn, run, ticker='TEST', name='Тест', source_url='https://example.test/a.pdf',
                                  period=PERIOD, local_path='a.pdf')
        engine = Engine([], company_id=stored['company_id'], standard='IFRS', scope='group',
                        industry_id='retail', industries=config.industries(), require_comparability=True)
        engine.save(conn, engine.calculate('current_ratio', PERIOD))
    reference = {'pdf_sha256': 'a'*64, 'source_url': 'https://example.test/a.pdf', 'source_specific': True,
                 'standard': 'IFRS', 'scope': 'group',
                 'unit': 'млн руб.', 'multiplier': '1000000', 'checks_required': [],
                 'rows': [{'item_id': row.item_id, 'start': None, 'end': str(row.end), 'value': str(row.value),
                           'pages': [2], 'status': 'не_проверено'} for row in rows]}
    review = {'version': 1, 'ticker': 'TEST', 'reviewer': 'Независимая визуальная сверка',
              'reason': 'Просмотрены титул, стандарт, обе колонки, единица и знаки итогов',
              'pdf_sha256': 'a'*64, 'reference_sha256': reference_sha256(reference),
              'reviewed_pages': [1, 2, 3], 'visual_review_confirmed': True}
    pdf = PDFText('a'*64, tuple(Page(page, 'Изображение источника') for page in (1, 2, 3)))
    return conn, pdf, reference, review


def source_snapshot(conn):
    return [tuple(row) for row in conn.execute('SELECT id,value,value_norm,unit,location,superseded_by,is_restated '
                                              'FROM source_values ORDER BY id')]


def test_explicit_review_preserves_values_and_old_calculations_and_is_idempotent(tmp_path):
    conn, pdf, reference, review = setup(tmp_path)
    before = source_snapshot(conn)
    with conn:
        result = apply_ocr_balance_review(conn, pdf, reference, review)
        calculations = tool.recalculate_reviewed(conn, result, ['current_ratio'],
                                                  companies={'TEST': {'industry': {'id': 'retail'}}})
    assert before == source_snapshot(conn)
    assert len(result['changes']) == 14 and not result['source_numbers_modified']
    assert not result['comparability_approved'] and not result['financial_audit_complete']
    assert {row[0] for row in conn.execute('SELECT check_status FROM source_values')} == {'проверено_вручную'}
    assert calculations[0]['value'] == '2' and calculations[0]['status'] == 'рассчитан'
    assert [tuple(row) for row in conn.execute('SELECT status,value FROM metric_values ORDER BY id')] == [
        ('недостаточно_данных', None), ('рассчитан', '2')]
    with conn:
        assert apply_ocr_balance_review(conn, pdf, reference, review) == result
        assert tool.recalculate_reviewed(conn, result, ['current_ratio']) == calculations
    assert conn.execute('SELECT count(*) FROM metric_values').fetchone()[0] == 2
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    conn.close()


@pytest.mark.parametrize('change', ['pdf', 'reference_hash', 'value', 'sign', 'page', 'date', 'no_visual',
                                   'no_author', 'missing_unit_page', 'missing_metadata_page', 'duplicate',
                                   'source_conflict', 'tampered_protocol', 'unit', 'currency', 'superseded'])
def test_review_does_not_bypass_source_reference_or_original_conflicts(tmp_path, change):
    conn, pdf, reference, review = setup(tmp_path)
    if change == 'pdf': pdf = replace(pdf, sha256='f'*64)
    if change == 'reference_hash': review['reference_sha256'] = 'f'*64
    if change == 'value': reference['rows'][0]['value'] = '101'
    if change == 'sign': reference['rows'][0]['value'] = '-100'
    if change == 'page': reference['rows'][0]['pages'] = [1]
    if change == 'date': reference['rows'][0]['end'] = '2024-12-31'
    if change == 'unit': reference['unit'] = 'тыс. руб.'
    if change in ('value', 'sign', 'page', 'date', 'unit'):
        review['reference_sha256'] = reference_sha256(reference)
    if change == 'no_visual': review['visual_review_confirmed'] = False
    if change == 'no_author': review['reviewer'] = ''
    if change == 'missing_unit_page': review['reviewed_pages'] = [1, 2]
    if change == 'missing_metadata_page': review['reviewed_pages'] = [2, 3]
    if change == 'duplicate':
        reference['rows'][-1] = reference['rows'][0]
        review['reference_sha256'] = reference_sha256(reference)
    with conn:
        if change == 'source_conflict': conn.execute("UPDATE source_values SET check_status='расхождение' WHERE id=1")
        if change == 'tampered_protocol': conn.execute("UPDATE meta SET value=value||' ' WHERE key LIKE 'ifrs_extraction:%'")
        if change == 'currency': conn.execute("UPDATE source_values SET currency='USD' WHERE id=1")
        if change == 'superseded': conn.execute('UPDATE source_values SET superseded_by=2 WHERE id=1')
    before = list(conn.iterdump())
    with pytest.raises((ValueError, KeyError)):
        with conn: apply_ocr_balance_review(conn, pdf, reference, review)
    assert list(conn.iterdump()) == before
    conn.close()


def test_unreviewed_ocr_facts_remain_blocked_and_comparability_not_inferred(tmp_path):
    from agent.metrics.data import load_facts
    conn, pdf, reference, review = setup(tmp_path)
    original = make_run().facts[0]
    extra = replace(original, item_id='cash_and_equivalents', value=Decimal('5'), normalized=Decimal('5000000'),
                    raw_value='5', from_ocr=True)
    with conn:
        store_extraction(conn, make_run((extra,)), ticker='TEST', name='Тест', source_url='https://example.test/a.pdf',
                         period=PERIOD, local_path='a.pdf')
        result = apply_ocr_balance_review(conn, pdf, reference, review)
        calculations = tool.recalculate_reviewed(conn, result, ['current_ratio', 'roa'],
                                                  companies={'TEST': {'industry': {'id': 'retail'}}})
    assert conn.execute("SELECT check_status FROM source_values WHERE item_id='cash_and_equivalents'").fetchone()[0] == 'не_проверено'
    assert {item['metric_id']: item['status'] for item in calculations} == {
        'current_ratio': 'рассчитан', 'roa': 'недостаточно_данных'}
    conn.close()


def test_recalculation_failure_rolls_back_statuses_and_journal(tmp_path):
    conn, pdf, reference, review = setup(tmp_path)
    before = list(conn.iterdump())
    with pytest.raises(KeyError):
        with conn:
            result = apply_ocr_balance_review(conn, pdf, reference, review)
            tool.recalculate_reviewed(conn, result, ['current_ratio'], companies={'OTHER': {}})
    assert list(conn.iterdump()) == before
    conn.close()


def test_repeat_review_does_not_hide_later_conflict(tmp_path):
    conn, pdf, reference, review = setup(tmp_path)
    with conn: apply_ocr_balance_review(conn, pdf, reference, review)
    with conn: conn.execute("UPDATE source_values SET check_status='расхождение' WHERE id=1")
    with pytest.raises(ValueError):
        with conn: apply_ocr_balance_review(conn, pdf, reference, review)
    assert conn.execute('SELECT check_status FROM source_values WHERE id=1').fetchone()[0] == 'расхождение'
    conn.close()


def test_old_review_cannot_reuse_cached_calculation_after_source_conflict(tmp_path):
    conn, pdf, reference, review = setup(tmp_path)
    with conn:
        result = apply_ocr_balance_review(conn, pdf, reference, review)
        tool.recalculate_reviewed(conn, result, ['current_ratio'], companies={'TEST': {'industry': {'id': 'retail'}}})
    with conn: conn.execute("UPDATE source_values SET check_status='расхождение' WHERE id=1")
    with pytest.raises(ValueError, match='после приёмки'):
        tool.recalculate_reviewed(conn, result, ['current_ratio'])
    conn.close()


def test_cli_refuses_main_database_missing_database_and_old_output(tmp_path):
    command = ['--pdf', 'report.pdf', '--reference', 'ref.json', '--review', 'decision.json',
               '--metric', 'current_ratio', '--output', str(tmp_path / 'out.json')]
    for path in (db.DB_PATH, tmp_path / 'missing.db'):
        with pytest.raises(SystemExit): tool.main(['--db', str(path), *command])
    conn, *_ = setup(tmp_path)
    conn.close()
    (tmp_path / 'out.json').write_text('прежний протокол')
    with pytest.raises(SystemExit): tool.main(['--db', str(tmp_path/'ocr.db'), *command])
    assert (tmp_path/'out.json').read_text() == 'прежний протокол'


def test_cli_applies_explicit_decision_without_ocr_rerun_or_network(tmp_path, monkeypatch):
    conn, pdf, reference, review = setup(tmp_path)
    conn.close()
    reference_path, review_path = tmp_path/'ref.json', tmp_path/'decision.json'
    reference_path.write_text(json.dumps(reference))
    review_path.write_text(json.dumps(review))
    monkeypatch.setattr(tool, 'read_pdf', lambda path: pdf)
    load_config = tool.config.load
    monkeypatch.setattr(tool.config, 'load', lambda name: {'companies': [{'ticker': 'TEST', 'industry': {'id': 'retail'}}]}
                        if name == 'watchlist' else load_config(name))
    monkeypatch.delenv('GIGACHAT_KEY', raising=False)
    output = tmp_path/'result.json'
    assert tool.main(['--db', str(tmp_path/'ocr.db'), '--pdf', 'original.pdf', '--reference', str(reference_path),
                      '--review', str(review_path), '--metric', 'current_ratio', '--output', str(output)]) == 0
    result = json.loads(output.read_text())
    assert result['ok'] and result['messages_sent'] == 0 and not result['comparability_approved']
    assert result['recalculated'][0]['value'] == '2'
