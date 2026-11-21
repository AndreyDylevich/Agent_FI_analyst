"""Проверка ложных цитат, знаков, масштаба, периодов и недопустимых полей ответа ИИ."""

import copy
import json
from datetime import date
from decimal import Decimal as D

import pytest

from agent import config
from agent.extract.pdf_text import Page
from agent.extract.schemas import parse_amount, parse_extraction
from agent.metrics.engine import Period

TEXT = '''ПАО Тест. Консолидированная финансовая отчетность по МСФО.
За шесть месяцев, закончившихся 30 июня 2026 года. В миллионах российских рублей.
30.06.2026 31.12.2025
Итого активы 1 234 1 000
Оборотные активы 400 350
Обязательства 500 450
Выручка 2026 2025
Выручка 100 90
Себестоимость (50) (40)
'''
PAGES = (Page(1, TEXT),)
PERIOD = Period(date(2026, 1, 1), date(2026, 6, 30), 'H1')


def cite(quote): return {'page': 1, 'quote': quote}


def payload():
    return {'ticker': 'TEST', 'entity_name': 'ПАО Тест', 'identity_evidence': cite('ПАО Тест'),
            'standard': 'IFRS', 'standard_evidence': cite('финансовая отчетность по МСФО'),
            'scope': 'group', 'scope_evidence': cite('Консолидированная финансовая отчетность'),
            'currency': 'RUB', 'currency_evidence': cite('В миллионах российских рублей'),
            'period_start': '2026-01-01', 'period_end': '2026-06-30', 'period_type': 'H1',
            'period_evidence': cite('За шесть месяцев, закончившихся 30 июня 2026 года'),
            'facts': [{'item_id': 'total_assets', 'original_name': 'Итого активы', 'raw_value': '1 234',
                       'number_format': 'space_grouped_decimal',
                       'unit': 'млн руб.', 'unit_evidence': cite('В миллионах российских рублей'),
                       'evidence': cite('Итого активы 1 234 1 000'), 'column_header': '30.06.2026',
                       'period_start': None, 'period_end': '2026-06-30', 'period_type': 'H1',
                       'statement': 'balance', 'cash_flow_section': None}], 'warnings': []}


def parse(data, pages=PAGES):
    return parse_extraction(json.dumps(data, ensure_ascii=False), pages, ticker='TEST', period=PERIOD,
                            entity_token='Тест', line_items=config.line_items())


def test_source_and_normalization():
    result = parse(payload())
    row = result.facts[0]
    assert row.value == D('1234') and row.normalized == D('1234000000')
    assert row.evidence.page == 1 and row.column_header == '30.06.2026'
    assert not row.from_ocr


def test_ocr_source_flag_not_hidden():
    assert parse(payload(), (Page(1, TEXT, 'ocr'),)).facts[0].from_ocr


@pytest.mark.parametrize('change', [
    {'item_id': 'made_up'}, {'raw_value': 1234}, {'raw_value': '234'}, {'raw_value': '0'},
    {'unit': 'руб.'}, {'evidence': cite('Итого активы 9999')}, {'evidence': {'page': 2, 'quote': 'Итого активы'}},
    {'column_header': '2027'}, {'period_end': '2027-06-30'}, {'period_type': 'FY'},
    {'period_start': '2026-01-01'}, {'statement': 'income'}, {'cash_flow_section': 'operating'},
    {'check_status': 'проверено_авто'}, {'normalized': '1234000000'},
])
def test_invalid_facts_rejected(change):
    data = payload(); data['facts'][0].update(change)
    with pytest.raises((ValueError, TypeError)):
        parse(data)


@pytest.mark.parametrize('change', [
    {'ticker': 'OTHER'}, {'standard': 'RSBU'}, {'currency': 'USD'}, {'scope': 'entity'},
    {'entity_name': 'Другая компания'}, {'period_end': '2025-06-30'},
    {'identity_evidence': cite('Обязательства')}, {'standard_evidence': cite('Итого активы')},
    {'scope_evidence': cite('Итого активы')}, {'currency_evidence': cite('Итого активы')},
])
def test_mixed_context_rejected(change):
    data = payload(); data.update(change)
    with pytest.raises(ValueError): parse(data)


def test_duplicates_not_summed():
    data = payload();data['facts'].append(copy.deepcopy(data['facts'][0]))
    with pytest.raises(ValueError): parse(data)


def test_number_substring_and_sign_are_not_evidence():
    for raw in ('234', '123', '1', '50'):
        data = payload();data['facts'][0].update(raw_value=raw)
        if raw == '50':
            data['facts'][0].update(original_name='Себестоимость', evidence=cite('Себестоимость (50) (40)'))
        with pytest.raises(ValueError): parse(data)


@pytest.mark.parametrize('raw,value', [('0', '0'), ('1 234', '1234'), ('(1 234)', '-1234'),
                                      ('−123', '-123'), ('12,50', '12.50'), ('12.5', '12.5')])
def test_exact_amounts(raw, value):
    assert parse_amount(raw) == D(value)


@pytest.mark.parametrize('raw', ['-', '—', '', 'NaN', '1,234', '1.234', '1 23', '(-123)', '(123', '123)'])
def test_ambiguous_or_missing_amount_not_zero(raw):
    with pytest.raises(ValueError): parse_amount(raw)


def test_duplicate_json_keys_rejected():
    with pytest.raises(ValueError):
        parse_extraction('{"ticker":"TEST","ticker":"OTHER"}', PAGES, ticker='TEST', period=PERIOD,
                         entity_token='Тест', line_items=config.line_items())


def test_comma_thousands_must_be_explicit():
    assert parse_amount('376,772', 'comma_grouped_dot_decimal') == D('376772')
    assert parse_amount('(1,234,567)', 'comma_grouped_dot_decimal') == D('-1234567')
    with pytest.raises(ValueError): parse_amount('376,772')
    with pytest.raises(ValueError): parse_amount('12,5', 'comma_grouped_dot_decimal')


def test_ending_date_alone_does_not_prove_accumulated_period():
    from agent.extract.schemas import period_is_cited
    assert not period_is_cited('30 июня 2026 года', PERIOD)
    assert not period_is_cited('За три месяца, закончившихся 30 июня 2026 года', PERIOD)
    assert period_is_cited('За шесть месяцев, закончившихся 30 июня 2026 года', PERIOD)


def test_positive_neutral_tax_not_silently_normalized_as_expense():
    data = payload();row = data['facts'][0]
    row.update(item_id='income_tax', original_name='Налог на прибыль', raw_value='50',
               evidence=cite('Налог на прибыль 50'), column_header='2026',
               period_start='2026-01-01', statement='income')
    pages = (Page(1,TEXT+'\nНалог на прибыль 50'),)
    with pytest.raises(ValueError): parse(data,pages)
    row['item_id'] = 'income_tax_benefit'
    assert parse(data,pages).facts[0].normalized == D('50000000')


def test_balance_point_date_and_group_subsidiaries_are_valid_contexts():
    data = payload();data['facts'][0]['period_type'] = 'AT_DATE'
    data['scope_evidence'] = cite('ПАО Тест и дочерние предприятия')
    result = parse(data,(Page(1,TEXT+'\nПАО Тест и дочерние предприятия'),))
    assert result.facts[0].start is None and result.facts[0].period_type == 'H1'


@pytest.mark.parametrize('left,right,fmt', [('480,620','450,572','comma_grouped_dot_decimal'),
                                          ('200','200','space_grouped_decimal')])
def test_column_gap_not_mistaken_for_thousands(left,right,fmt):
    data = payload();row=data['facts'][0]
    row.update(raw_value=left,number_format=fmt,evidence=cite(f'Итого активы {left} {right}'))
    source=TEXT+f'\nИтого активы {left}             {right}\n'
    result=parse(data,(Page(1,source),))
    assert result.facts[0].value == D(left.replace(',',''))
    assert '             ' in result.facts[0].evidence.quote


def test_line_references_copy_original_text_instead_of_model_quotes():
    data=payload()
    def address(evidence):
        line=next(n for n,text in enumerate(TEXT.splitlines(),1) if evidence['quote'] in text)
        return {'page':1,'start_line':line,'end_line':line}
    for key in ('identity_evidence','standard_evidence','scope_evidence','currency_evidence','period_evidence'):
        data[key]=address(data[key])
    for key in ('evidence','unit_evidence'):
        data['facts'][0][key]=address(data['facts'][0][key])
    result=parse(data)
    ref=result.facts[0].evidence
    assert ref.quote==TEXT.splitlines()[ref.start_line-1]
    assert ref.start_line==ref.end_line
    assert result.context['identity_evidence']['quote']==TEXT.splitlines()[0]


@pytest.mark.parametrize('ref',[{'page':1,'start_line':0,'end_line':1},
                               {'page':1,'start_line':2,'end_line':1},
                               {'page':1,'start_line':1,'end_line':999},
                               {'page':1,'start_line':True,'end_line':1},
                               {'page':2,'start_line':1,'end_line':1},
                               {'page':1,'start_line':1,'end_line':1,'quote':'выдумано'}])
def test_invalid_line_reference_not_accepted(ref):
    data=payload();data['facts'][0]['evidence']=ref
    with pytest.raises(ValueError):parse(data)
