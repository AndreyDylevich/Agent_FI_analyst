"""Реестр показателей: значения на числах, сверенных вручную; статусы; объяснение; правила периода.

Суммы в рублях; в комментариях — те же числа в млн руб. для ручной сверки.
"""

from decimal import Decimal

import pytest

from agent import config
from agent.metrics.registry import (
    REGISTRY, MetricDef, PeriodRule, Status, evaluate, for_industry, format_number, get, register,
    validate_registry,
)

M = Decimal("1000000")   # 1 млн руб.


def mln(x: str) -> Decimal:
    return Decimal(x) * M


# ---------- Текущая ликвидность ----------

def test_current_ratio_value_and_explanation():
    # 1 500 / 1 200 = 1,25
    r = evaluate("current_ratio", {"current_assets": mln("1500"), "current_liabilities": mln("1200")})
    assert r.status is Status.CALCULATED
    assert r.value == Decimal("1.25")
    assert r.explanation == "Текущая ликвидность = 1 500,0 / 1 200,0 = 1,25 (суммы в млн руб.)"


def test_current_ratio_zero_liabilities_not_applicable():
    r = evaluate("current_ratio", {"current_assets": mln("10"), "current_liabilities": Decimal("0")})
    assert r.status is Status.NOT_APPLICABLE
    assert r.value is None
    assert "≤ 0" in r.reason


# ---------- Чистый долг ----------

def test_net_debt_value_and_explanation():
    # 300 000 + 150 000 − 37 700 = 412 300 млн руб.
    r = evaluate("net_debt", {
        "debt_long_term": mln("300000"), "debt_short_term": mln("150000"), "cash_and_equivalents": mln("37700"),
    })
    assert r.value == mln("412300")
    assert r.explanation == "Чистый долг = 300 000,0 + 150 000,0 − 37 700,0 = 412 300,0 млн руб."


def test_net_debt_incl_leases_value():
    # 300 000 + 150 000 + 80 000 + 20 000 − 37 700 = 512 300 млн руб.
    r = evaluate("net_debt_incl_leases", {
        "debt_long_term": mln("300000"), "debt_short_term": mln("150000"),
        "lease_liabilities_long": mln("80000"), "lease_liabilities_short": mln("20000"),
        "cash_and_equivalents": mln("37700"),
    })
    assert r.value == mln("512300")


def test_net_debt_missing_input_is_not_zero():
    r = evaluate("net_debt", {"debt_long_term": mln("300000"), "debt_short_term": None})
    assert r.status is Status.INSUFFICIENT_DATA
    assert r.value is None
    assert r.missing == ("debt_short_term", "cash_and_equivalents")
    assert r.reason == "нет данных: debt_short_term, cash_and_equivalents"


def test_net_debt_incl_leases_requires_leases():
    r = evaluate("net_debt_incl_leases", {
        "debt_long_term": mln("1"), "debt_short_term": mln("1"), "cash_and_equivalents": mln("1"),
    })
    assert r.status is Status.INSUFFICIENT_DATA
    assert r.missing == ("lease_liabilities_long", "lease_liabilities_short")


def test_negative_net_debt_formatting():
    # 100 + 50 − 200 = −50 млн руб. (чистая денежная позиция)
    r = evaluate("net_debt", {
        "debt_long_term": mln("100"), "debt_short_term": mln("50"), "cash_and_equivalents": mln("200"),
    })
    assert r.value == mln("-50")
    assert r.explanation.endswith("= −50,0 млн руб.")


# ---------- EBITDA и необязательный вход ----------

def test_ebitda_with_impairment():
    # 100 000 + 50 000 + 1 800 = 151 800 млн руб.
    r = evaluate("ebitda", {
        "operating_profit": mln("100000"), "d_and_a": mln("50000"), "impairment_noncurrent": mln("1800"),
    })
    assert r.value == mln("151800")
    assert r.notes == ()
    assert r.explanation == "EBITDA = 100 000,0 + 50 000,0 + 1 800,0 = 151 800,0 млн руб."


def test_ebitda_optional_input_not_disclosed():
    # обесценение не раскрыто → не учитывается: 100 000 + 50 000 = 150 000 млн руб.
    r = evaluate("ebitda", {"operating_profit": mln("100000"), "d_and_a": mln("50000")})
    assert r.status is Status.CALCULATED
    assert r.value == mln("150000")
    assert r.notes == ("не раскрыто: impairment_noncurrent",)
    assert "impairment_noncurrent" not in r.inputs_used
    assert r.explanation == "EBITDA = 100 000,0 + 50 000,0 + н/д = 150 000,0 млн руб."


def test_ebitda_required_input_missing():
    r = evaluate("ebitda", {"operating_profit": mln("100000"), "impairment_noncurrent": mln("1800")})
    assert r.status is Status.INSUFFICIENT_DATA
    assert r.missing == ("d_and_a",)


# ---------- Чистый долг / EBITDA ----------

def test_net_debt_to_ebitda_value_and_explanation():
    # 412 300 / 151 800 = 2,7160… → 2,72; значение хранится без округления
    r = evaluate("net_debt_to_ebitda", {"net_debt": mln("412300"), "ebitda": mln("151800")})
    assert r.value == Decimal("412300") / Decimal("151800")
    assert r.value != Decimal("2.72")
    assert r.explanation == "Чистый долг / EBITDA = 412 300,0 / 151 800,0 = 2,72 (суммы в млн руб.)"


@pytest.mark.parametrize("ebitda", ["0", "-5000"])
def test_net_debt_to_ebitda_not_applicable_when_ebitda_not_positive(ebitda):
    r = evaluate("net_debt_to_ebitda", {"net_debt": mln("1000"), "ebitda": mln(ebitda)})
    assert r.status is Status.NOT_APPLICABLE
    assert r.value is None
    assert "EBITDA ≤ 0" in r.reason


# ---------- Правила периода по входам ----------

def test_period_rules_are_per_input():
    d = get("net_debt_to_ebitda")
    assert d.inputs == {"net_debt": PeriodRule.AT_DATE, "ebitda": PeriodRule.LTM}
    assert d.result_period is PeriodRule.AT_DATE
    assert get("ebitda").result_period is PeriodRule.FLOW
    assert get("ebitda").optional_inputs == {"impairment_noncurrent": PeriodRule.FLOW}


def test_registry_consistent_with_line_items():
    assert validate_registry(config.line_items()) == []


def _dummy(**overrides) -> MetricDef:
    fields = dict(
        id="dummy", name="Тест", industry="common", group="growth", formula="—", expression="{revenue}",
        inputs={"revenue": PeriodRule.FLOW}, unit="x", result_period=PeriodRule.FLOW,
        missing_data_rule="—", meaning="—", limitations="—", version=1, calculate=lambda x: Decimal("1"),
    )
    fields.update(overrides)
    return MetricDef(**fields)


def test_validate_registry_detects_wrong_period_rule(monkeypatch):
    # выручка — поток, «на дату» для неё не подходит; капитал — остаток, LTM не подходит
    monkeypatch.setitem(REGISTRY, "dummy", _dummy(
        inputs={"revenue": PeriodRule.AT_DATE, "equity_total": PeriodRule.LTM, "ebitda": PeriodRule.AVERAGE},
    ))
    errors = validate_registry(config.line_items())
    assert len(errors) == 3
    assert any("'revenue' — поток" in e for e in errors)
    assert any("'equity_total' — остаток" in e for e in errors)
    assert any("'ebitda' — поток" in e for e in errors)


def test_validate_registry_detects_unknown_input(monkeypatch):
    monkeypatch.setitem(REGISTRY, "dummy", _dummy(inputs={"no_such_item": PeriodRule.FLOW}))
    assert validate_registry(config.line_items()) == [
        "dummy: вход 'no_such_item' не найден ни в line_items.yaml, ни в реестре"
    ]


# ---------- Регистрация и прочее ----------

def test_duplicate_id_rejected():
    before = dict(REGISTRY)
    with pytest.raises(ValueError):
        register(get("current_ratio"))
    assert REGISTRY == before


def test_input_both_required_and_optional_rejected():
    with pytest.raises(ValueError):
        register(_dummy(id="dummy2", optional_inputs={"revenue": PeriodRule.FLOW}))
    assert "dummy2" not in REGISTRY


def test_float_input_rejected():
    with pytest.raises(TypeError):
        evaluate("current_ratio", {"current_assets": 1.5, "current_liabilities": Decimal("1")})


def test_all_metrics_fully_described():
    for d in REGISTRY.values():
        for name in ("name", "formula", "expression", "unit", "missing_data_rule", "meaning", "limitations"):
            assert getattr(d, name), f"{d.id}: пустое поле {name}"


@pytest.mark.parametrize("value, unit, expected", [
    (Decimal("1234567890"), "руб.", "1 234,6"),
    (Decimal("50000"), "руб.", "0,1"),
    (Decimal("-2.345"), "x", "−2,35"),
    (Decimal("999.995"), "x", "1 000,00"),
    (Decimal("0"), "x", "0,00"),
])
def test_format_number(value, unit, expected):
    assert format_number(value, unit) == expected


def test_for_industry_uses_groups_and_exclusions():
    industries = config.industries()
    bank_ids = {d.id for d in for_industry("banks", industries)}
    assert not bank_ids & {"current_ratio", "net_debt_to_ebitda", "ebitda", "net_debt"}
    telecom_ids = {d.id for d in for_industry("telecom", industries)}
    assert {"current_ratio", "net_debt", "net_debt_incl_leases", "ebitda", "net_debt_to_ebitda"} <= telecom_ids
