"""Конфиги читаются и согласованы между собой."""

from decimal import Decimal

from agent import config

GROUPS = {"growth", "profitability", "cash_flow", "leverage", "liquidity", "turnover", "market"}


def test_industries():
    data = config.load("industries")
    assert set(data["all_common_groups"]) == GROUPS
    items = data["industries"]
    assert len(items) == 21
    ids = [i["id"] for i in items]
    assert len(set(ids)) == len(ids)
    for ind in items:
        assert ind["name"] and ind["detect"]["okved"] and ind["detect"]["keywords"], ind["id"]
        assert set(ind["common_groups"]) <= GROUPS, ind["id"]
        assert isinstance(ind["exclude_metrics"], list)
        assert isinstance(ind["metrics"], list)
    by_id = config.industries()
    for ind_id in ("telecom", "retail", "forestry"):
        assert by_id[ind_id]["metrics"], ind_id
    assert by_id["forestry"]["name"] == "Лесная и целлюлозно-бумажная промышленность"


def test_watchlist():
    companies = config.load("watchlist")["companies"]
    assert [c["ticker"] for c in companies] == ["MTSS", "MVID", "SGZH"]
    industries = config.industries()
    for c in companies:
        assert c["inn"] is None, "ИНН вписывается только после подтверждения"
        assert c["industry"]["id"] in industries
        assert c["industry"]["confirmed"] is False
        for page in c["ir_pages"]:
            assert page["url"].startswith("https://")
            assert page["recognition"] in ("link_text", "pdf_first_page")


def test_line_items():
    items = config.load("line_items")["items"]
    ids = [i["id"] for i in items]
    assert len(set(ids)) == len(ids)
    for item in items:
        assert item["statement"] in ("balance", "income", "cash_flow"), item["id"]
        assert item["sign"] in ("natural", "outflow"), item["id"]
        assert isinstance(item["rsbu_codes"], list), item["id"]
    codes = [c for i in items for c in i["rsbu_codes"]]
    assert len(set(codes)) == len(codes), "код РСБУ не должен принадлежать двум статьям"


def test_debt_components():
    items = config.line_items()
    components = {i for i, v in items.items() if v.get("component_of") == "debt"}
    assert components == {"bank_loans", "bonds", "other_borrowings"}
    for c in components:
        assert items[c]["optional"] is True
        assert items[c]["statement"] == "balance"
    assert {"debt_long_term", "debt_short_term"} <= set(items)


def test_thresholds():
    data = config.load("thresholds")
    for t in data["quantitative"]:
        assert isinstance(t["threshold"], str), "пороги — строками, чтобы читать как Decimal"
        Decimal(t["threshold"])
        assert t["severity"] in ("warning", "critical")
    by_id = {t["id"]: t for t in data["quantitative"]}
    assert Decimal(by_id["net_debt_to_ebitda_high"]["threshold"]) == Decimal("3.0")
    assert Decimal(by_id["interest_coverage_critical"]["threshold"]) == Decimal("1.0")
    assert Decimal(by_id["short_term_debt_share_high"]["threshold"]) == Decimal("0.5")
    assert {q["id"] for q in data["qualitative"]} == {
        "going_concern", "covenant_breach_reclassification", "modified_audit_opinion", "auditor_change",
    }


def test_methodology_tax_rates():
    from datetime import date

    data = config.load('methodology')
    assert type(data['version']) is int and data['version'] >= 1
    rows = data['nominal_profit_tax_rates']
    previous_end = None
    for row in rows:
        assert isinstance(row['percent'], str)
        assert 0 <= Decimal(row['percent']) <= 100
        start = date.fromisoformat(row['from'])
        end = date.fromisoformat(row['through']) if row['through'] else date.max
        assert start <= end
        if previous_end is not None:
            assert start > previous_end
        previous_end = end
        assert row['source'].startswith('https://www.nalog.gov.ru/')
