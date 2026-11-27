"""Неизменяемые адресные решения о сопоставимости; финансовые числа/статусы не меняются."""

import json
from datetime import datetime, timezone

from agent.extract.ocr_review import reference_sha256
from agent.extract.restatements import original_status
from agent.extract.schemas import _keys, citation

PREFIX='ifrs_comparability_review:'
TOPICS={'accounting_policy','perimeter','definition','presentation','restatement','ifrs16','ifrs18'}
FIELDS=('id','company_id','item_id','value','value_norm','unit','currency','period_start','period_end',
        'period_type','standard','scope','segment_name','document_id','check_status','cash_flow_section',
        'location','sha256','url')


def _source(conn,source_id):
    row=conn.execute('SELECT s.*,d.sha256,d.url FROM source_values_current s '
                     'JOIN documents d ON d.id=s.document_id WHERE s.id=?',(source_id,)).fetchone()
    if not row:
        raise ValueError('Источник отсутствует или заменён пересчитанной версией')
    original_status(conn,row)
    return {k:row[k] for k in FIELDS}


def apply_comparability_review(conn,pdfs,review):
    """Вызывается явно после анализа раскрытий, никогда не вызывается загрузкой/арифметикой."""
    _keys(review,{'version','ticker','reviewer','reason','decision','scope','segment_name','item_id',
                  'accounting_basis','perimeter_basis','methodology_version','source_context_reviewed',
                  'observations','evidence'})
    if (type(review['version']) is not int or review['version']!=1
            or review['source_context_reviewed'] is not True
            or review['decision'] not in ('approved','not_comparable','insufficient_data')
            or not all(isinstance(review[k],str) and review[k].strip() for k in ('ticker','reviewer','reason','item_id'))
            or review['scope'] not in ('group','segment')
            or (review['scope']=='segment') != bool(review['segment_name'])):
        raise ValueError('Нужно адресное решение об учётной базе, периметре и определении статьи')
    if review['decision']=='approved' and not all(isinstance(review[k],str) and review[k].strip()
            for k in ('accounting_basis','perimeter_basis','methodology_version')):
        raise ValueError('Одобрение требует подтверждённых трёх оснований сравнения')
    selected=review['observations']
    if not isinstance(selected,list) or not 2<=len(selected)<=100:
        raise ValueError('Нужно от двух до ста явно выбранных исходных наблюдений')
    company=conn.execute('SELECT id FROM companies WHERE ticker=?',(review['ticker'],)).fetchone()
    if not company:raise ValueError('Компания отсутствует в БД')
    snapshots=[];seen=set()
    for chosen in selected:
        _keys(chosen,{'source_id','pdf_sha256','normalized_value'})
        if type(chosen['source_id']) is not int or chosen['source_id'] in seen:
            raise ValueError('Неверный или повторный идентификатор источника')
        seen.add(chosen['source_id']);source=_source(conn,chosen['source_id'])
        if (source['company_id']!=company['id'] or source['item_id']!=review['item_id']
                or source['standard']!='IFRS' or source['currency']!='RUB'
                or source['scope']!=review['scope'] or source['segment_name']!=review['segment_name']
                or source['sha256']!=chosen['pdf_sha256'] or source['value_norm']!=chosen['normalized_value']):
            raise ValueError('Решение относится к другой статье/версии/компании/охвату или числу')
        if review['decision']=='approved' and source['check_status'] not in ('проверено_авто','проверено_вручную'):
            raise ValueError('Сопоставимость не одобряет финансовое расхождение или непроверенную сумму')
        snapshots.append(source)
    if len({(s['period_start'],s['period_end']) for s in snapshots})<2:
        raise ValueError('Подтверждение одной даты не является сопоставимостью периодов')
    if len({s['cash_flow_section'] for s in snapshots})!=1:
        raise ValueError('Сравнение смешивает разные классификации денежных потоков')
    evidence=[];topics=set();covered=set()
    if not isinstance(review['evidence'],list) or not review['evidence']:
        raise ValueError('Нужны исходные раскрытия для решения')
    for entry in review['evidence']:
        _keys(entry,{'pdf_sha256','topic','evidence'})
        digest=entry['pdf_sha256'];topic=entry['topic']
        if digest not in pdfs or pdfs[digest].sha256!=digest or topic not in TOPICS:
            raise ValueError('Неизвестные PDF или тема подтверждения')
        proof=citation(entry['evidence'],{p.number:p for p in pdfs[digest].pages},max_lines=80,max_chars=12000)
        evidence.append({'pdf_sha256':digest,'topic':topic,'page':proof.page,'quote':proof.quote,
                         'start_line':proof.start_line,'end_line':proof.end_line})
        topics.add(topic);covered.add(digest)
    if not {s['sha256'] for s in snapshots} <= covered:
        raise ValueError('Не просмотрены раскрытия каждой выбранной версии PDF')
    if review['decision']=='approved' and not {'accounting_policy','perimeter','definition'} <= topics:
        raise ValueError('Нужны отдельные подтверждения политики, периметра и определения статьи')
    payload={'review':review,'snapshots':snapshots,'evidence':evidence}
    digest=reference_sha256(payload);key=PREFIX+digest
    old=conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    if old:
        result=json.loads(old['value'])
        if result['payload']!=payload:raise ValueError('Журнал решения изменился')
        return result
    result={'review_sha256':digest,'payload':payload,'decision':review['decision'],
            'reviewed_at_utc':datetime.now(timezone.utc).isoformat(),'source_numbers_modified':False,
            'source_financial_statuses_modified':False}
    conn.execute('INSERT INTO meta(key,value) VALUES (?,?)',(key,json.dumps(result,ensure_ascii=False,sort_keys=True)))
    return result


def comparability_for_sources(conn,source_ids):
    """Только читает журнал. Устаревшее/противоречивое решение блокирует весь выбранный набор."""
    wanted=set(source_ids);choices={}
    for saved in conn.execute('SELECT key,value FROM meta WHERE key LIKE ?',(PREFIX+'%',)):
        record=json.loads(saved['value']);payload=record['payload'];digest=reference_sha256(payload)
        if (saved['key']!=PREFIX+digest or record['review_sha256']!=digest
                or record['decision']!=payload['review']['decision']):
            raise ValueError('Контрольная сумма решения о сопоставимости изменилась')
        snapshots=payload['snapshots'];affected=wanted & {s['id'] for s in snapshots}
        if not affected:continue
        valid=True
        try:
            valid=all(_source(conn,s['id'])==s for s in snapshots)
        except ValueError:
            valid=False
        review=payload['review']
        if valid and record['decision']=='approved':
            basis={k:review[k] for k in ('accounting_basis','perimeter_basis','methodology_version')}
            basis.update(comparison_evidence=PREFIX+digest,comparison_issue=None)
        else:
            issue=review['reason'] if valid else 'Решение о сопоставимости устарело: исходный набор/статус изменился'
            basis={'accounting_basis':None,'perimeter_basis':None,'methodology_version':None,
                   'comparison_evidence':PREFIX+digest,'comparison_issue':issue}
        for source_id in affected:choices.setdefault(source_id,[]).append(basis)
    result={}
    for source_id,decisions in choices.items():
        meanings={(d['accounting_basis'],d['perimeter_basis'],d['methodology_version'],d['comparison_issue']) for d in decisions}
        if len(meanings)>1:
            result[source_id]={'accounting_basis':None,'perimeter_basis':None,'methodology_version':None,
                               'comparison_evidence':None,'comparison_issue':'Противоречивые решения о сопоставимости: требуется отдельный разбор'}
        else:result[source_id]=decisions[0]
    return result
