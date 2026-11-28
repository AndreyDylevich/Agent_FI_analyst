"""Missing, disputed and duplicated sources must remain distinguishable in coverage."""
from dataclasses import replace
import pytest

from agent.checks.source_coverage import reviewed_position_coverage
from agent.checks.document import reconcile_document
from tests.test_mts_primary_forms import inputs
from tests.test_extraction_schema import PERIOD


def test_complete_position_coverage_does_not_approve_a_discrepancy_or_zero_absence():
    _,_,review,facts=inputs()
    origins=[[{'stored_status':'проверено_авто'}] for f in facts]
    origins[0]=[{'stored_status':'расхождение'}]
    result=reviewed_position_coverage(review,facts,origins)
    assert result['all_reviewed_numeric_positions_present']
    assert result['position_state_counts']['absent_in_source']==2
    assert result['position_state_counts']['source_discrepancy']==1
    assert not result['financial_values_approved']
    assert all(p['source_count']==0 for p in result['positions'] if p['state']=='absent_in_source')


def test_equal_duplicate_confirmations_and_conflicts_have_different_coverage():
    _,_,review,facts=inputs();origins=[[{'stored_status':'проверено_авто'}] for f in facts]
    equal=reviewed_position_coverage(review,(*facts,facts[0]),[*origins,[{'stored_status':'проверено_авто'}]])
    assert equal['all_reviewed_numeric_positions_present']
    row=next(p for p in equal['positions'] if p['source_count']==2)
    assert row['state']=='extracted_verified'
    different=replace(facts[0],value=facts[0].value+1,normalized=facts[0].normalized+1000000)
    conflict=reviewed_position_coverage(review,(*facts,different),[*origins,[{'stored_status':'проверено_авто'}]])
    assert not conflict['all_reviewed_numeric_positions_present']
    assert conflict['position_state_counts']['conflicting_values']==1
    missing=reviewed_position_coverage(review,facts[1:],origins[1:])
    assert not missing['all_reviewed_numeric_positions_present']
    assert missing['position_state_counts']['not_extracted']==1


def test_cross_form_recheck_rejects_a_real_token_from_the_other_year_column(monkeypatch):
    import agent.checks.document as module
    pdf,_,review,facts=inputs()
    current=next(f for f in facts if f.item_id=='revenue' and f.end==PERIOD.end)
    prior=next(f for f in facts if f.item_id=='revenue' and f.end!=PERIOD.end)
    wrong=replace(current,value=prior.value,normalized=prior.normalized,raw_value=prior.raw_value)
    assert prior.raw_value in current.evidence.quote
    metadata={'url':'https://example.test/report.pdf','standard':'IFRS','period_start':str(PERIOD.start),'period_end':str(PERIOD.end),'period_type':'H1'}
    monkeypatch.setattr(module,'load_document',lambda *a:(metadata,[(wrong,{'stored_status':'проверено_авто'})]))
    with pytest.raises(ValueError,match='другой колонки'):
        reconcile_document([('wrong_column',None)],pdf,'MTSS')
