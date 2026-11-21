"""Долг, аренда и проценты: разные определения и даты не сводятся к одному числу."""

from datetime import date
from dataclasses import replace

from agent.checks.ifrs import validate_financials
from tests.test_ifrs_statements import source


def stocks(values, end='2026-06-30'):
    return tuple(source(item,str(value),None,end,'notes') for item,value in values.items())


def test_full_debt_composition_and_maturity_match_balance():
    rows=stocks({'note_debt_total':716341,'note_bonds':352813,'note_loans_and_other_borrowings':363528,
                 'note_debt_current':394470,'note_debt_noncurrent':321871,
                 'debt_short_term':394470,'debt_long_term':321871})
    checks,statuses=validate_financials(rows)
    assert set(statuses)=={'проверено_авто'}
    assert {'note_debt_composition','note_debt_maturity','note_debt_current_vs_balance',
            'note_debt_noncurrent_vs_balance'} <= {c.rule for c in checks if c.result.status=='пройдено'}
    # Банковские кредиты вместе с прочими займами не доказывают отдельную сумму только банков.
    assert not any(c.rule=='debt_components' and c.result.status=='пройдено' for c in checks)


def test_lease_discounting_kept_separate_and_different_dates_not_compared():
    rows=stocks({'note_lease_undiscounted':285078,'note_lease_discount':112843,
                 'note_lease_present_value':172235,'note_lease_current':35425,
                 'note_lease_noncurrent':136810,'lease_liabilities_short':35425,'lease_liabilities_long':136810})
    checks,statuses=validate_financials(rows)
    assert set(statuses)=={'проверено_авто'}
    assert next(c for c in checks if c.rule=='note_lease_discounting').result.delta==0
    # Сравнительный столбец примечания за июнь нельзя сверять с декабрьским балансом.
    prior=stocks({'note_lease_present_value':112535,'note_lease_current':21388,'note_lease_noncurrent':91147},'2025-06-30')
    december=stocks({'lease_liabilities_short':22496,'lease_liabilities_long':92364},'2025-12-31')
    checks,_=validate_financials((*prior,*december))
    cross=[c for c in checks if c.rule.endswith('_vs_balance')]
    assert cross and all(c.result.status=='недостаточно_данных' for c in cross)


def test_missing_lease_discount_not_assumed_zero():
    checks,_=validate_financials(stocks({'note_lease_undiscounted':285078,'note_lease_present_value':172235}))
    check=next(c for c in checks if c.rule=='note_lease_discounting')
    assert check.result.status=='недостаточно_данных' and check.result.missing==('note_lease_discount',)


def test_accrued_interest_and_capitalization_not_treated_as_cash_paid():
    values={'note_interest_borrowings':56180,'note_interest_leases':8289,'note_interest_unwinding':360,
            'note_interest_total':64829,'note_finance_other_expense':3490,'note_finance_expense_gross':68319,
            'note_capitalized_interest_expense':2059,'note_finance_modification_expense':1260,
            'note_finance_expense_net':67520}
    rows=tuple(source(item,str(value),'2026-01-01','2026-06-30','notes') for item,value in values.items())
    checks,statuses=validate_financials(rows)
    assert set(statuses)=={'проверено_авто'}
    assert {'note_interest_composition','note_finance_gross','note_finance_net'} <= {c.rule for c in checks if c.result.status=='пройдено'}
    assert not any(row.item_id=='interest_paid' for row in rows)
    bad=(*rows[:-1],replace(rows[-1],normalized=rows[-2].normalized))
    assert 'расхождение' in validate_financials(bad)[1]
