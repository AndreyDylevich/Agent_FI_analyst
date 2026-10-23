"""Разбор ответа поиска ГИР БО (без сети, фикстура в tests/fixtures)."""

import json
from pathlib import Path

from agent.sources.girbo import clean, parse_search

FIXTURE = Path(__file__).parent / "fixtures" / "girbo_search.json"


def test_parse_search_strips_tags():
    found = parse_search(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert len(found) == 2
    first = found[0]
    assert first["inn"] == "7700000001"
    assert first["short_name"] == 'ПАО "ТЕСТ ГРУПП"'
    assert first["okved"] == "70.22"
    assert first["last_bfo_period"] == "2025"
    assert first["has_kfo"] is True


def test_parse_search_missing_fields():
    second = parse_search(json.loads(FIXTURE.read_text(encoding="utf-8")))[1]
    assert second["last_bfo_period"] is None
    assert second["has_kfo"] is False
    assert parse_search({"content": []}) == []
    assert parse_search({}) == []


def test_clean():
    assert clean(None) is None
    assert clean("  <strong>МТС</strong>\n ПАО ") == "МТС ПАО"
