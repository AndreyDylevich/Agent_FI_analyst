"""Общая сверка разных извлечений одного PDF не меняет источники и не скрывает пробелы."""

import json
from dataclasses import replace

import pytest

from agent import db
from agent.checks.document import coverage, reconcile_document
from agent.extract.pdf_text import PDFText, Page
from agent.extract.schemas import Citation
from agent.extract.store import store_extraction
from tests.test_extraction_schema import PERIOD
from tests.test_ifrs_pipeline import facts, make_run
from tests.test_ifrs_statements import source


def setup(tmp_path, partitions, *, ocr=False):
    header = 'ТЕСТ. Консолидированная отчётность IFRS. RUB. В миллионах российских рублей. За шесть месяцев 30 июня 2026 года.'
    proof = {'page': 1, 'quote': header, 'start_line': 1, 'end_line': 1}
    context = {'ticker': 'TEST', 'standard': 'IFRS', 'scope': 'group', 'currency': 'RUB',
               'period_start': '2026-01-01', 'period_end': '2026-06-30', 'period_type': 'H1',
               **{field: proof for field in ('identity_evidence', 'standard_evidence', 'scope_evidence',
                                             'currency_evidence', 'period_evidence')}}
    text, sources = [], []
    for index, partition in enumerate(partitions):
        rows = []
        for fact in partition:
            quote = f'{fact.item_id} {fact.raw_value}'
            text.append(quote)
            rows.append(replace(fact, from_ocr=ocr, evidence=Citation(2, quote),
                                unit_evidence=Citation(1, header, 1, 1)))
        conn = db.init_db(tmp_path / f'{index}.db')
        with conn:
            store_extraction(conn, replace(make_run(tuple(rows)), contexts=(context,)), ticker='TEST', name='Тест',
                             source_url='https://example.test/report.pdf', period=PERIOD, local_path='report.pdf')
        sources.append((f'db_{index}', conn))
    pdf = PDFText('a'*64, (Page(1, header), Page(2, '\n'.join(text))))
    return sources, pdf


def close(sources):
    for _, conn in sources:
        conn.close()


def test_different_runs_of_same_pdf_can_complete_cross_form_check_without_database_writes(tmp_path):
    rows = [source('net_profit', '10', '2026-01-01', '2026-06-30', 'income'),
            source('cf_starting_net_profit', '10', '2026-01-01', '2026-06-30')]
    sources, pdf = setup(tmp_path, [[rows[0]], [rows[1]]])
    before = [list(conn.iterdump()) for _, conn in sources]
    result = reconcile_document(sources, pdf, 'TEST')
    check = next(check for check in result['checks'] if check['rule'] == 'profit_vs_cash_flow_base')
    assert check['status'] == 'пройдено' and check['result']['delta'] == 0
    assert {origin['database'] for value in check['inputs'] for origin in value['origins']} == {'db_0', 'db_1'}
    assert result['source_observations_count'] == result['distinct_facts_count'] == 2
    family = next(family for family in result['cross_form_families'] if family['id'] == 'profit_cash_flow')
    assert family['status'] == 'недостаточно_данных'
    assert family['requirements'][0]['status'] == 'пройдено' and family['requirements'][1]['status'] == 'недостаточно_данных'
    assert not result['ok'] and not result['cross_form_complete']
    assert not result['source_statuses_modified'] and not result['comparability_approved']
    assert before == [list(conn.iterdump()) for _, conn in sources]
    close(sources)


def test_equal_confirmations_are_not_added_twice_and_keep_both_origins(tmp_path):
    sources, pdf = setup(tmp_path, [facts(), facts()])
    result = reconcile_document(sources, pdf, 'TEST')
    assert result['source_observations_count'] == 12 and result['distinct_facts_count'] == 6
    check = next(check for check in result['checks'] if check['rule'] == 'balance')
    assert check['result']['delta'] == 0 and check['status'] == 'пройдено'
    assert all(len(value['origins']) == 2 for value in check['inputs'])
    close(sources)


def test_new_conflict_blocks_other_identities_using_the_disputed_input(tmp_path):
    attribution = (source('equity_parent', '20', None, '2026-06-30', 'balance'),
                   source('non_controlling_interests', '10', None, '2026-06-30', 'balance'))
    other = source('total_assets', '103', None, '2026-06-30', 'balance')
    sources, pdf = setup(tmp_path, [(*facts(), *attribution), [other]])
    result = reconcile_document(sources, pdf, 'TEST')
    check = next(check for check in result['checks'] if check['rule'] == 'equity_attribution')
    assert check['arithmetic_status'] == 'пройдено' and check['status'] == 'расхождение'
    assert check['disputed_input_indices'] and result['has_discrepancies']
    close(sources)


def test_inherited_discrepancy_is_not_cleared_by_passing_new_arithmetic(tmp_path):
    sources, pdf = setup(tmp_path, [facts()])
    with sources[0][1]: sources[0][1].execute("UPDATE source_values SET check_status='расхождение' WHERE item_id='total_assets'")
    result = reconcile_document(sources, pdf, 'TEST')
    check = next(check for check in result['checks'] if check['rule'] == 'balance')
    assert check['arithmetic_status'] == 'пройдено' and check['status'] == 'расхождение'
    assert check['inherited_discrepancy_indices']
    close(sources)


@pytest.mark.parametrize('change', ['sha', 'currency', 'company', 'window', 'protocol', 'location', 'pdf_text'])
def test_different_contexts_tampered_protocols_and_false_citations_cannot_be_combined(tmp_path, change):
    sources, pdf = setup(tmp_path, [facts()])
    conn = sources[0][1]
    with conn:
        if change == 'sha': conn.execute("UPDATE documents SET sha256=?", ('f'*64,))
        if change == 'currency': conn.execute("UPDATE source_values SET currency='USD'")
        if change == 'company': conn.execute("UPDATE companies SET ticker='OTHER'")
        if change == 'window': conn.execute("UPDATE documents SET period_end='2025-06-30'")
        if change == 'protocol': conn.execute("UPDATE meta SET value=value||' ' WHERE key LIKE 'ifrs_extraction:%'")
        if change == 'location':
            row = conn.execute('SELECT id,location FROM source_values LIMIT 1').fetchone()
            location = json.loads(row['location']); location['quote'] = 'Другая строка'
            conn.execute('UPDATE source_values SET location=? WHERE id=?', (json.dumps(location), row['id']))
    if change == 'pdf_text': pdf = replace(pdf, pages=(pdf.pages[0], Page(2, 'Другие числа')))
    with pytest.raises(ValueError): reconcile_document(sources, pdf, 'TEST')
    close(sources)


def test_pending_ocr_is_not_approved_by_document_arithmetic(tmp_path):
    sources, pdf = setup(tmp_path, [facts()], ocr=True)
    result = reconcile_document(sources, pdf, 'TEST')
    check = next(check for check in result['checks'] if check['rule'] == 'balance')
    assert check['arithmetic_status'] == 'пройдено' and check['status'] == 'недостаточно_данных'
    assert check['unreviewed_ocr_indices'] and not result['ok']
    close(sources)


def test_manual_ocr_status_without_journal_is_not_trusted(tmp_path):
    sources, pdf = setup(tmp_path, [facts()], ocr=True)
    with sources[0][1]: sources[0][1].execute("UPDATE source_values SET check_status='проверено_вручную'")
    with pytest.raises(ValueError, match='журналом'):
        reconcile_document(sources, pdf, 'TEST')
    close(sources)


def test_previously_reviewed_ocr_is_rechecked_without_new_decision_or_writes(tmp_path):
    from tests.test_ifrs_ocr_review import setup as setup_ocr
    from agent.extract.ocr_review import apply_ocr_balance_review
    conn, pdf, reference, review = setup_ocr(tmp_path)
    with conn: apply_ocr_balance_review(conn, pdf, reference, review)
    before = list(conn.iterdump())
    result = reconcile_document([('reviewed', conn)], pdf, 'TEST')
    check = next(check for check in result['checks'] if check['rule'] == 'balance')
    assert check['status'] == 'пройдено' and not check['unreviewed_ocr_indices']
    assert list(conn.iterdump()) == before
    conn.close()


def test_coverage_cannot_succeed_with_empty_requirements_or_missing_rule():
    with pytest.raises(ValueError): coverage([], PERIOD, {'families': []})
    with pytest.raises(ValueError): coverage([], PERIOD, {'families': [{'id': 'empty', 'name': 'Пусто', 'checks': []}]})
    result = coverage([], PERIOD, {'families': [{'id': 'missing', 'name': 'Нет сверки',
                       'checks': [{'rule': 'not_implemented', 'periods': ['flow']}]}]})
    assert result[0]['status'] == 'недостаточно_данных'
    assert not result[0]['requirements'][0]['implemented']


def test_no_input_is_not_success():
    with pytest.raises(ValueError): reconcile_document([], PDFText('a'*64, ()), 'TEST')


def test_cli_reads_databases_without_changes_and_preserves_incomplete_report(tmp_path, monkeypatch):
    from agent.tools import reconcile_ifrs_document as tool
    sources, pdf = setup(tmp_path, [facts()])
    close(sources)
    database = tmp_path/'0.db'; original = database.read_bytes()
    monkeypatch.setattr(tool, 'read_pdf', lambda path: pdf)
    monkeypatch.delenv('GIGACHAT_KEY', raising=False)
    output = tmp_path/'report.json'
    command = ['--db', str(database), '--pdf', 'report.pdf', '--ticker', 'TEST', '--output', str(output)]
    assert tool.main(command) == 1
    report = json.loads(output.read_text())
    assert report['source_integrity_verified'] and not report['cross_form_complete']
    assert report['source_databases'][0]['unchanged'] and database.read_bytes() == original
    saved_output = output.read_bytes()
    with pytest.raises(SystemExit): tool.main(command)
    assert output.read_bytes() == saved_output


def test_cli_failure_is_recorded_and_main_database_rejected(tmp_path, monkeypatch):
    from agent.tools import reconcile_ifrs_document as tool
    command = ['--db', str(tmp_path/'missing.db'), '--pdf', 'missing.pdf', '--ticker', 'TEST',
               '--output', str(tmp_path/'report.json')]
    monkeypatch.setattr(tool, 'read_pdf', lambda path: PDFText('a'*64, (Page(1, 'Текст'),)))
    assert tool.main(command) == 1
    report = json.loads((tmp_path/'report.json').read_text())
    assert report['error'] and not report['source_integrity_verified'] and report['messages_sent'] == 0
    assert not (tmp_path/'missing.db').exists()
    command[1] = str(db.DB_PATH)
    with pytest.raises(SystemExit): tool.main(command)
