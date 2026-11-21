"""Раскрытия привязаны к источнику и не становятся одобрением сопоставимости по одному слову."""

import json

import pytest

from agent.extract.disclosures import parse_disclosures
from agent.extract.pdf_text import Page


def answer(topic='ifrs16', first=1,last=2):
    return json.dumps({'findings':[{'topic':topic,'evidence':{'page':14,'start_line':first,'end_line':last}}]})


def test_preserves_negation_and_full_source_does_not_assert_comparability():
    pages=(Page(14,'Применён МСФО (IFRS) 16 к продаже с обратной арендой.\nВыбытие не классифицировано как прекращённая деятельность.'),)
    row=parse_disclosures(answer(),pages)[0]
    assert 'не классифицировано' in row.evidence.quote
    assert row.status=='источник_подтверждён_интерпретация_не_проверена'
    assert not row.from_ocr


def test_ifrs18_cannot_be_inferred_from_note_18():
    with pytest.raises(ValueError):
        parse_disclosures(answer('ifrs18',1,1),(Page(14,'18. Прочие обязательства'),))


def test_ocr_and_missing_topics_do_not_imply_verified_interpretation():
    row=parse_disclosures(answer('leases',1,1),(Page(14,'Обязательства по аренде','ocr'),))[0]
    assert row.from_ocr
    assert parse_disclosures('{"findings":[]}',(Page(14,'Прочее'),))==()


def test_unknown_fields_and_fabricated_line_ranges_rejected():
    pages=(Page(14,'МСФО (IFRS) 16'),)
    for data in [answer(last=3),'{"findings":[],"comparability_approved":true}']:
        with pytest.raises(ValueError):parse_disclosures(data,pages)
