"""От ответа ИИ до SQLite: источники, один повтор, проверки и сохранение конфликтов."""

import copy
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from agent import config
from agent.checks.ifrs import validate_financials
from agent.db import init_db
from agent.extract.llm import Completion, ProviderError
from agent.extract.pdf_text import PDFText, Page
from agent.extract.pipeline import ExtractionRun, extract_pdf
from agent.extract.store import store_extraction
from tests.test_extraction_schema import PERIOD, TEXT, parse, payload


def facts():
    original = parse(payload()).facts[0]
    values = {'total_assets': '100', 'non_current_assets': '60', 'current_assets': '40',
              'equity_total': '30', 'non_current_liabilities': '50', 'current_liabilities': '20'}
    return tuple(replace(original, item_id=key, value=D(value), normalized=D(value)*1000000,
                         raw_value=value) for key, value in values.items())


def make_run(rows=None):
    rows = rows or facts()
    checks, statuses = validate_financials(rows)
    return ExtractionRun('a'*64, (), ({'scope':'group'},), rows, checks, statuses, ())


def test_complete_balance_passes_and_incomplete_not_assumed_zero():
    checks, statuses = validate_financials(facts())
    assert all(s == 'проверено_авто' for s in statuses)
    assert next(c for c in checks if c.rule == 'balance').result.status == 'пройдено'
    checks, statuses = validate_financials(facts()[:2])
    assert all(c.result.status == 'недостаточно_данных' for c in checks)
    assert set(statuses) == {'не_проверено'}


def test_bad_balance_blocks_involved_values_and_ocr_not_auto_verified():
    rows = list(facts()); rows[0] = replace(rows[0], normalized=D('102000000'))
    checks, statuses = validate_financials(tuple(rows))
    assert 'расхождение' in statuses
    assert next(c for c in checks if c.rule == 'balance').result.delta == 2000000
    rows = list(facts()); rows[2] = replace(rows[2], from_ocr=True)
    _, statuses = validate_financials(tuple(rows))
    assert statuses[2] == 'не_проверено'


def test_conflicting_same_item_not_arbitrarily_selected():
    rows = facts()
    _, statuses = validate_financials((*rows, replace(rows[0], normalized=D('101000000'))))
    assert statuses[0] == statuses[-1] == 'расхождение'


def test_store_is_idempotent_and_keeps_conflicting_history(tmp_path):
    conn = init_db(tmp_path/'test.db')
    options = dict(ticker='TEST', name='Тест', source_url='https://example.test/report.pdf', period=PERIOD, local_path='test.pdf')
    with conn:
        first = store_extraction(conn, make_run(), **options)
        second = store_extraction(conn, make_run(), **options)
    assert first['inserted'] == 6 and second['inserted'] == 0
    assert conn.execute('SELECT count(*) FROM source_values').fetchone()[0] == 6
    row = conn.execute("SELECT * FROM source_values WHERE item_id='current_assets'").fetchone()
    assert row['value_norm'] == '40000000' and row['scope'] == 'group' and row['extracted_by'] == 'gigachat'
    assert json.loads(row['location'])['raw_value'] == '40'
    rows = list(facts());rows[2] = replace(rows[2], normalized=D('42000000'), value=D('42'), raw_value='42')
    with conn:
        revised = store_extraction(conn, make_run(tuple(rows)), **options)
    assert revised['conflicts'] and conn.execute('SELECT count(*) FROM source_values').fetchone()[0] == 12
    assert all(r[0] == 'расхождение' for r in conn.execute("SELECT check_status FROM source_values WHERE item_id='current_assets'"))
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    conn.close()


class Provider:
    def __init__(self, responses, obey_context=True):
        self.responses = responses; self.calls = []; self.schemas = []; self.obey_context=obey_context
    def extract(self, system, text, schema):
        self.schemas.append(schema)
        self.calls.append(json.loads(text.split('\nПовторная проверка')[0]))
        response=copy.deepcopy(self.responses.pop(0))
        if self.obey_context:
            for key,field in schema['properties'].items():
                if 'enum' in field and len(field['enum'])==1 and key in response:
                    response[key]=field['enum'][0]
        return Completion(json.dumps(response, ensure_ascii=False), 'test-model', 10)


def pipeline_pdf():
    return PDFText('a'*64, (Page(1, TEXT), Page(2, 'Консолидированный отчет о финансовом положении\n'+TEXT)))


def pipeline_answer():
    data = payload();data['facts'][0]['evidence']['page'] = 2
    data['facts'][0]['unit_evidence']['page'] = 2
    return data


def test_one_retry_and_original_responses_preserved(tmp_path):
    correct = pipeline_answer();wrong = copy.deepcopy(correct)
    wrong['facts'][0]['evidence']['quote'] = 'Итого активы 999'
    provider = Provider([wrong, correct])
    run = extract_pdf(pipeline_pdf(), provider, ticker='TEST', entity_token='Тест', period=PERIOD,
                      line_items=config.line_items(), output=tmp_path, sections=('balance',))
    assert len(provider.calls) == 2 and len(run.facts) == 1
    assert (tmp_path/'batch_1_attempt_1.json').exists() and (tmp_path/'batch_1_attempt_2.json').exists()
    assert run.batches[0]['accepted'] is False and run.batches[1]['accepted'] is True
    assert provider.calls[0]['target_pages'] == [2]
    assert {p['page'] for p in provider.calls[0]['pages']} == {1,2}
    assert provider.calls[1]['verified_context']['scope_evidence'] == provider.calls[0]['verified_context']['scope_evidence']
    assert provider.schemas[1]['properties']['scope_evidence']['enum'] == [provider.calls[0]['verified_context']['scope_evidence']]
    assert provider.calls[1]['previous_facts'] == wrong['facts']


def test_retry_must_not_change_previously_verified_context(tmp_path):
    wrong = pipeline_answer();wrong['facts'][0]['raw_value'] = '-'
    changed = pipeline_answer();changed['scope_evidence'] = changed['identity_evidence']
    provider = Provider([wrong, changed],obey_context=False)
    with pytest.raises(ValueError):
        extract_pdf(pipeline_pdf(), provider, ticker='TEST', entity_token='Тест', period=PERIOD,
                    line_items=config.line_items(), output=tmp_path, sections=('balance',))
    assert provider.calls[1]['verified_context']['scope_evidence'] == provider.calls[0]['verified_context']['scope_evidence']


def test_missing_context_not_invented():
    from agent.extract.schemas import source_context
    assert source_context((Page(1,'ПАО Тест\nВыручка 100'),),ticker='TEST',period=PERIOD,
                          entity_token='Тест',line_items=config.line_items())=={}


def test_second_invalid_answer_stops_without_creating_financial_values(tmp_path):
    wrong = pipeline_answer();wrong['facts'][0]['raw_value'] = '999'
    provider = Provider([wrong, wrong])
    with pytest.raises(ValueError):
        extract_pdf(pipeline_pdf(), provider, ticker='TEST', entity_token='Тест', period=PERIOD,
                    line_items=config.line_items(), output=tmp_path, sections=('balance',))
    assert len(provider.calls) == 2
    attempts = json.loads((tmp_path/'attempts.json').read_text())
    assert len(attempts) == 2 and all(a['accepted'] is False for a in attempts)


def test_provider_timeout_still_leaves_request_audit(tmp_path):
    class TimedOut:
        def extract(self,*args): raise ProviderError('Извлечение GigaChat: ReadTimeout')
    with pytest.raises(ProviderError):
        extract_pdf(pipeline_pdf(),TimedOut(),ticker='TEST',entity_token='Тест',period=PERIOD,
                    line_items=config.line_items(),output=tmp_path,sections=('balance',))
    attempts=json.loads((tmp_path/'attempts.json').read_text())
    assert len(attempts)==1 and attempts[0]['provider_error'].endswith('ReadTimeout')
    assert attempts[0]['request_sha256'] and not attempts[0]['accepted']
    assert not list(tmp_path.glob('batch*.json'))


def test_equity_reconciles_same_dates_and_profit_without_assuming_movements():
    source = parse(payload()).facts[0]
    rows = tuple(replace(source, item_id=key, normalized=D(value), value=D(value), statement=form,
                         start=PERIOD.start if flow else None) for key,value,form,flow in [
        ('equity_total','34662','balance',False), ('equity_parent','4142','balance',False),
        ('non_controlling_interests','30520','balance',False),
        ('eq_closing_total','34662','equity',False), ('eq_closing_parent','4142','equity',False),
        ('eq_closing_nci','30520','equity',False),
        ('net_profit','75858','income',True), ('eq_profit_total','75858','equity',True),
        ('eq_profit_parent','74030','equity',True), ('eq_profit_nci','1828','equity',True)])
    checks, statuses = validate_financials(rows)
    passed = {c.rule for c in checks if c.result.status=='пройдено'}
    assert {'equity_attribution','eq_closing_attribution','eq_closing_total_vs_balance',
            'eq_closing_parent_vs_balance','eq_closing_nci_vs_balance','profit_vs_equity',
            'eq_profit_attribution'} <= passed
    assert set(statuses)=={'проверено_авто'}
    # Отсутствие движения капитала не превращается в доказательство полного rollforward.
    assert not any('rollforward' in rule for rule in passed)
    changed = (*rows[:3],replace(rows[3], normalized=D('38000000')), *rows[4:])
    _, statuses = validate_financials(changed)
    assert statuses[0]==statuses[3]=='расхождение'


def test_amortization_can_be_requested_from_cash_flow(tmp_path):
    label='Амортизация основных средств, НМА и прав пользования'
    data=pipeline_answer();data['facts'][0].update(item_id='d_and_a', original_name=label,
        raw_value='100',evidence={'page':2,'quote':label+' 100'},statement='cash_flow',
        period_start='2026-01-01',period_type='H1')
    pdf=PDFText('a'*64,(Page(1,TEXT),Page(2,'Консолидированный отчет о движении денежных средств\n'+TEXT+'\n'+label+' 100')))
    provider=Provider([data])
    run=extract_pdf(pdf,provider,ticker='TEST',entity_token='Тест',period=PERIOD,
                    line_items={'d_and_a':config.line_items()['d_and_a']},output=tmp_path,sections=('cash_flow',))
    assert len(provider.calls)==1 and run.facts[0].item_id=='d_and_a'
    assert set(provider.calls[0]['line_items'])=={'d_and_a'}
