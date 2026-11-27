"""Явный разбор пересчётов: источник/значения/дата проверяются, исходные числа не меняются."""

import hashlib
import json
import re
from datetime import date
from decimal import Decimal

from agent import config
from agent.extract.schemas import citation, date_is_cited, _keys


def original_status(conn,row):
    """Возвращает статус проверок исходного извлечения, не повышая OCR/ошибочные тождества."""
    location=json.loads(row['location'])
    digest=location['extraction_sha256'];index=location['fact_index']
    saved=conn.execute('SELECT value FROM meta WHERE key=?',
                       (f"ifrs_extraction:{row['document_id']}:{digest}",)).fetchone()
    if not saved:raise ValueError('Нет исходного протокола проверок значения')
    if hashlib.sha256(saved['value'].encode()).hexdigest()!=digest:
        raise ValueError('Контрольная сумма протокола извлечения изменилась')
    run=json.loads(saved['value']);fact=run['facts'][index]
    if row['scope'] != run.get('data_scope','group') or row['segment_name'] != run.get('segment_name'):
        raise ValueError('Охват/сегмент в БД не совпадает с исходным протоколом')
    if (fact['item_id']!=row['item_id'] or fact['normalized']!=row['value_norm']
            or fact['unit']!=row['unit'] or fact['end']!=row['period_end'] or fact['start']!=row['period_start']
            or Decimal(fact['value'])!=Decimal(row['value'])):
        raise ValueError('Значение в БД не совпадает с исходным протоколом')
    status=run['statuses'][index]
    if fact['from_ocr'] and status=='проверено_авто':raise ValueError('OCR не может быть одобрен арифметикой')
    return row['check_status'] if row['check_status']=='проверено_вручную' else status


def apply_restatement_review(conn,pdf,review):
    """Вызывается в транзакции. Решение относится к точным версиям и исходным значениям.

    Пересчёт не устанавливает сопоставимость политики/периметра и не исправляет арифметику PDF.
    """
    _keys(review,{'version','reviewer','reason','source_evidence','replacements'})
    if review['version']!=1 or not all(isinstance(review[key],str) and review[key].strip()
                                     for key in ('reviewer','reason')):
        raise ValueError('Нужны версия решения, автор проверки и обоснование')
    if not isinstance(review['replacements'],list) or not review['replacements']:
        raise ValueError('Нужен непустой список заменяемых значений')
    proof=citation(review['source_evidence'],{p.number:p for p in pdf.pages})
    if not re.search(r'пересмотр|пересчит|ретроспектив|restat|retrospect',proof.quote,re.I):
        raise ValueError('Цитата не подтверждает пересчёт сравнительных данных')
    digest=hashlib.sha256(json.dumps({'review':review,'pdf_sha256':pdf.sha256},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    key=f'ifrs_restatement_review:{digest}'
    existing=conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    if existing:return json.loads(existing['value'])
    prepared=[];seen=set();companies=set()
    for replacement in review['replacements']:
        _keys(replacement,{'item_id','period_start','period_end','old_pdf_sha256','new_pdf_sha256',
                           'old_normalized_value','new_normalized_value'})
        if replacement['period_start'] is not None or config.line_items().get(replacement['item_id'],{}).get('statement')!='balance':
            raise ValueError('Это решение поддерживает только остатки баланса на дату; пересчёт потоков/других форм требует отдельного подтверждения')
        if replacement['new_pdf_sha256']!=pdf.sha256 or replacement['old_pdf_sha256']==pdf.sha256:
            raise ValueError('Цитата должна происходить из новой, отдельной версии PDF')
        if not date_is_cited(proof.quote,date.fromisoformat(replacement['period_end'])):
            raise ValueError('Раскрытие не подтверждает дату пересчитанного значения')
        signature=(replacement['item_id'],replacement['period_start'],replacement['period_end'])
        if signature in seen:raise ValueError('Повтор значения в решении о пересчёте')
        seen.add(signature)
        versions=[]
        for role in ('old','new'):
            rows=conn.execute("SELECT s.*,d.period_end report_end FROM source_values s JOIN documents d ON d.id=s.document_id "
                "WHERE d.sha256=? AND s.item_id=? AND s.period_start IS ? AND s.period_end=? "
                "AND s.standard='IFRS' AND s.scope='group' AND s.segment_name IS NULL",
                (replacement[f'{role}_pdf_sha256'],*signature)).fetchall()
            if not rows or any(Decimal(row['value_norm'])!=Decimal(replacement[f'{role}_normalized_value']) for row in rows):
                raise ValueError('Ожидаемые значения не совпадают с выбранными версиями источников')
            versions.append(rows)
        old,new=versions;target=new[0]
        identity=('company_id','item_id','period_start','period_end','standard','scope','segment_name','currency','cash_flow_section')
        if any(any(row[field]!=target[field] for field in identity) for row in (*old,*new)):
            raise ValueError('Нельзя связать разные компании/периоды/определения платежа')
        if any(row['report_end']>=target['report_end'] for row in old) or target['report_end']<=target['period_end']:
            raise ValueError('Новая версия должна содержать сравнительный период более раннего выпуска')
        if any(row['superseded_by'] is not None for row in (*old,*new)):
            raise ValueError('Версия уже заменена; требуется отдельное решение для текущих источников')
        active=conn.execute("SELECT * FROM source_values_current WHERE company_id=? AND item_id=? AND period_start IS ? "
            "AND period_end=? AND standard='IFRS' AND scope='group' AND segment_name IS NULL AND currency=? AND cash_flow_section IS ?",
            (target['company_id'],*signature,target['currency'],target['cash_flow_section'])).fetchall()
        conflicting={row['id'] for row in active if Decimal(row['value_norm'])!=Decimal(target['value_norm'])}
        if not conflicting<={row['id'] for row in old}:
            raise ValueError('Есть ещё не разобранная конфликтующая версия; её нельзя скрыть этим решением')
        restores=[(row,original_status(conn,row)) for row in active
                  if row['id'] in {item['id'] for item in old} or Decimal(row['value_norm'])==Decimal(target['value_norm'])]
        prepared.append((old,new,restores));companies.add(target['company_id'])
    old_ids=[row['id'] for old,_,_ in prepared for row in old]
    placeholders=','.join('?' for _ in old_ids)
    affected=[dict(row) for row in conn.execute(f"WITH RECURSIVE affected(id) AS ("
        f"SELECT metric_value_id FROM metric_inputs WHERE source_value_id IN ({placeholders}) UNION "
        "SELECT mi.metric_value_id FROM metric_inputs mi JOIN affected a ON mi.input_metric_value_id=a.id) "
        "SELECT DISTINCT m.* FROM metric_values m JOIN affected a ON a.id=m.id",old_ids)]
    changes=[]
    for old,new,restores in prepared:
        for row in old:
            conn.execute('UPDATE source_values SET superseded_by=? WHERE id=?',(new[0]['id'],row['id']))
        for row in new:conn.execute('UPDATE source_values SET is_restated=1 WHERE id=?',(row['id'],))
        for row,status in restores:conn.execute('UPDATE source_values SET check_status=? WHERE id=?',(status,row['id']))
        changes.append({'item_id':new[0]['item_id'],'period_end':new[0]['period_end'],
                        'old_source_ids':[row['id'] for row in old],'new_source_ids':[row['id'] for row in new],
                        'old_normalized_value':old[0]['value_norm'],'new_normalized_value':new[0]['value_norm'],
                        'statuses_before':{str(row['id']):row['check_status'] for row in (*old,*new)},
                        'statuses_after':{str(row['id']):status for row,status in restores}})
    result={'review_sha256':digest,'review':review,'pdf_sha256':pdf.sha256,
            'source_evidence':{'page':proof.page,'quote':proof.quote,'start_line':proof.start_line,'end_line':proof.end_line},
            'changes':changes,'company_ids':sorted(companies),'affected_calculations':affected,
            'comparability_approved':False,'source_numbers_modified':False,
            'recalculation_meta_key':f'ifrs_restatement_recalculation:{digest}'}
    conn.execute('INSERT INTO meta(key,value) VALUES (?,?)',(key,json.dumps(result,ensure_ascii=False,sort_keys=True)))
    return result
