"""Addressed extraction coverage; neither arithmetic nor source absences imply financial approval."""

from collections import Counter


def reviewed_position_coverage(review, facts, origins):
    """Compare explicitly reviewed positions with stored facts without reading monetary tables."""
    if review is None:
        return {'reviewed_profile_available':False,'positions':[],
                'all_reviewed_numeric_positions_present':False,'financial_values_approved':False}
    positions=[]
    for alias, source in review.observations.items():
        matching=[i for i,fact in enumerate(facts)
                  if (fact.item_id,fact.statement,fact.start,fact.end)==
                  (source['item_id'],source['statement'],source['start'],source['end'])]
        references=[origin for i in matching for origin in origins[i]]
        if source.get('source_absent'):
            # Numerical source absences are rejected by the financial validator;
            # keeping this separate also makes the matrix safe to use directly.
            state='unexpected_numeric_value' if matching else 'absent_in_source'
        elif not matching:
            state='not_extracted'
        elif len({(facts[i].value,facts[i].normalized,facts[i].unit) for i in matching})>1:
            state='conflicting_values'
        elif any(r['stored_status']=='расхождение' for r in references):
            state='source_discrepancy'
        elif any(r['stored_status']=='не_проверено' for r in references):
            state='extracted_unreviewed'
        else:
            state='extracted_verified'
        positions.append({'observation_key':alias,'item_id':source['item_id'],
            'statement':source['statement'],'start':str(source['start']) if source['start'] else None,
            'end':str(source['end']),'source_evidence':source['evidence'],
            'value_column':source.get('value_column'),'state':state,
            'stored_statuses':dict(Counter(r['stored_status'] for r in references)),
            'fact_indices':matching,'source_count':len(references)})
    return {'reviewed_profile_available':True,'structure_basis':review.basis,
            'positions':positions,'position_state_counts':dict(Counter(p['state'] for p in positions)),
            'all_reviewed_numeric_positions_present':not any(p['state'] in
                ('not_extracted','unexpected_numeric_value','conflicting_values') for p in positions),
            'financial_values_approved':False,
            'scope_limitation':'Матрица покрывает только позиции точного просмотренного профиля. Наличие суммы не одобряет тождество, полноту всех форм, исходное расхождение или сопоставимость.'}
