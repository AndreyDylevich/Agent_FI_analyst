"""Адресная визуальная приёмка OCR разных форм, с сохранением исходных расхождений."""

import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal

from agent import config
from agent.checks.reviewed_tables import compile_reviewed_tables
from agent.extract.ocr_review import reference_sha256
from agent.extract.restatements import original_status
from agent.extract.schemas import _keys, citation, parse_amount
from agent.metrics.data import normalize_money
from agent.metrics.engine import Period


def _apply_source_review(conn, pdf, reference, review, *, recognition, version, meta_prefix):
    """Только явное решение после просмотра PDF; арифметика не запускает эту процедуру."""
    confirmation='visual_review_confirmed' if recognition=='ocr' else 'source_context_reviewed'
    _keys(review, {'version','ticker','reviewer','reason','pdf_sha256','reference_sha256',
                   'reviewed_pages',confirmation,'standard','scope','selected_observations'})
    if (type(review['version']) is not int or review['version'] != version
            or review[confirmation] is not True or review['standard'] != 'IFRS'
            or review['scope'] != 'group'
            or not all(isinstance(review[k],str) and review[k].strip() for k in ('ticker','reviewer','reason'))):
        raise ValueError('Нужно явное решение о визуальной приёмке IFRS группы')
    pages = {p.number:p for p in pdf.pages}
    selected_pages = review['reviewed_pages']
    if (not isinstance(selected_pages,list) or not selected_pages
            or any(type(n) is not int or n not in pages for n in selected_pages)
            or len(set(selected_pages)) != len(selected_pages)):
        raise ValueError('Нужны просмотренные страницы оригинального PDF')
    if (review['pdf_sha256'] != pdf.sha256 or reference['pdf_sha256'] != pdf.sha256
            or reference_sha256(reference) != review['reference_sha256']):
        raise ValueError('PDF/независимый эталон не соответствуют решению')
    documents = conn.execute('SELECT d.* FROM documents d JOIN companies c ON c.id=d.company_id '
                             'WHERE d.sha256=? AND c.ticker=?',(pdf.sha256,review['ticker'])).fetchall()
    if (len(documents) != 1 or documents[0]['url'] != reference['source_url']
            or documents[0]['standard'] != 'IFRS'):
        raise ValueError('Нет единственного выпуска IFRS этой компании')
    document = documents[0]
    period = Period(date.fromisoformat(document['period_start']),date.fromisoformat(document['period_end']),document['period_type'])
    structure = compile_reviewed_tables(pdf,period)
    if not structure or not all(pages[o['evidence']['page']].method==recognition for o in structure.observations.values()):
        raise ValueError(f'Нужна отдельно просмотренная точная структура {recognition}-источника')
    fields = ('item_id','statement','start','end')
    signature = lambda r: tuple(r[k] for k in fields)
    reference_rows = {signature(r):r for r in reference['rows']}
    if len(reference_rows) != len(reference['rows']):
        raise ValueError('Неоднозначный эталон статьи/формы/окна')
    selected = review['selected_observations']
    if not isinstance(selected,list) or not selected:
        raise ValueError('Нужны явно выбранные исходные наблюдения')
    for row in selected:_keys(row,set(fields))
    keys = [signature(r) for r in selected]
    if len(set(keys)) != len(keys):
        raise ValueError('Повтор выбранного наблюдения')
    if any(k not in reference_rows or reference_rows[k]['status'] != 'не_проверено' for k in keys):
        raise ValueError('Расхождение источника нельзя одобрить визуальной приёмкой OCR')
    digest = reference_sha256(review); meta_key = f'{meta_prefix}:{digest}'
    saved = conn.execute('SELECT value FROM meta WHERE key=?',(meta_key,)).fetchone()
    prior = json.loads(saved['value']) if saved else None
    known_ids = {c['source_id'] for c in prior['changes']} if prior else set()
    changes, required_pages = [], set()
    items = config.line_items()
    for key in keys:
        expected = reference_rows[key];item,form,start,end = key
        matches = []
        candidates = conn.execute('SELECT * FROM source_values_current WHERE document_id=? AND item_id=? '
                                  'AND period_start IS ? AND period_end=?',(document['id'],item,start,end)).fetchall()
        for row in candidates:
            location = json.loads(row['location'])
            original_status(conn,row)
            saved_run = conn.execute('SELECT value FROM meta WHERE key=?',
                (f"ifrs_extraction:{document['id']}:{location['extraction_sha256']}",)).fetchone()
            run = json.loads(saved_run['value']);index = location['fact_index'];fact = run['facts'][index]
            if fact['statement'] != form:
                continue
            if (row['company_id'] != document['company_id'] or row['standard'] != 'IFRS'
                    or row['scope'] != 'group' or row['segment_name'] is not None or row['currency'] != 'RUB'
                    or row['superseded_by'] is not None or fact['from_ocr'] != (recognition=='ocr') or not run['contexts']
                    or run['statuses'][index] != 'не_проверено'
                    or row['check_status'] != ('проверено_вручную' if row['id'] in known_ids else 'не_проверено')):
                raise ValueError('Чужое решение, расхождение или неподтверждённый OCR нельзя повысить')
            if (row['unit'] != reference['unit'] or Decimal(row['value']) != Decimal(expected['value'])
                    or Decimal(row['value_norm']) != normalize_money(expected['value'],reference['unit'],items[item]['sign'])
                    or Decimal(expected['normalized_value']) != Decimal(row['value_norm'])
                    or fact['original_name'] != row['original_name'] or fact['raw_value'] != location['raw_value']
                    or fact['number_format'] != location['number_format']
                    or parse_amount(fact['raw_value'],fact['number_format']) != Decimal(expected['value'])
                    or fact['cash_flow_section'] != expected['cash_flow_section']
                    or fact['cash_flow_section'] != row['cash_flow_section']
                    or location['page'] not in expected['pages']):
                raise ValueError('Число/знак/масштаб/форма/состав платежа не совпадают с независимым эталоном')
            if expected.get('evidence') and {k:fact['evidence'][k] for k in ('page','start_line','end_line')}!=expected['evidence']:
                raise ValueError('Строка исходного наблюдения не соответствует независимому адресу')
            for field in ('evidence','unit_evidence'):
                proof = fact[field]
                address = ({k:proof[k] for k in ('page','start_line','end_line')} if proof.get('start_line') is not None
                           else {'page':proof['page'],'quote':proof['quote']})
                if citation(address,pages).quote != proof['quote']:
                    raise ValueError('Цитата не соответствует просмотренному OCR-тексту')
                required_pages.add(proof['page'])
            if fact['evidence'] != {k:location[k] for k in ('page','quote','start_line','end_line')}:
                raise ValueError('Адрес строки изменился после исходного извлечения')
            for context in run['contexts']:
                expected_context={'ticker':review['ticker'],'standard':'IFRS','scope':'group','currency':'RUB',
                    'period_start':document['period_start'],'period_end':document['period_end'],'period_type':document['period_type']}
                if any(context.get(k)!=v for k,v in expected_context.items()):
                    raise ValueError('Метаданные исходного протокола не соответствуют выпуску')
                for field in ('identity_evidence','standard_evidence','scope_evidence','currency_evidence','period_evidence'):
                    proof=context[field]
                    address=({k:proof[k] for k in ('page','start_line','end_line')} if proof.get('start_line') is not None
                             else {'page':proof['page'],'quote':proof['quote']})
                    if citation(address,pages).quote != proof['quote']:
                        raise ValueError('Исходный контекст OCR изменился')
                    required_pages.add(proof['page'])
            matches.append(row['id'])
            changes.append({'source_id':row['id'],'item_id':item,'statement':form,'period_start':start,'period_end':end,
                            'value':row['value'],'value_norm':row['value_norm'],'unit':row['unit'],'page':location['page'],
                            'extraction_sha256':location['extraction_sha256'],
                            'status_before':'не_проверено','status_after':'проверено_вручную'})
        if not matches:
            raise ValueError('Выбранное наблюдение отсутствует в исходном извлечении')
    if not required_pages <= set(selected_pages):
        raise ValueError('Не подтверждён просмотр всех страниц чисел, единиц и контекста')
    if prior:
        if (prior['review'] != review or prior['reference'] != reference
                or known_ids != {c['source_id'] for c in changes} or prior['changes'] != changes):
            raise ValueError('Состав или исходные значения изменились после приёмки')
        return prior
    result={'review_sha256':digest,'review':review,'reference':reference,'changes':changes,
            'document_id':document['id'],'company_id':document['company_id'],
            'period_start':document['period_start'],'period_end':document['period_end'],'period_type':document['period_type'],
            'reviewed_at_utc':datetime.now(timezone.utc).isoformat(),'source_numbers_modified':False,
            'comparability_approved':False,'financial_audit_complete':False,
            **({'source_recognition':'native'} if recognition=='native' else {}),
            'structure_basis':structure.basis,'limitation':f'Подтверждена точность выбранных исходных {"OCR" if recognition=="ocr" else "native"}-значений. Полнота сверок, экономический смысл и сопоставимость этим решением не одобрены.'}
    for change in changes:
        conn.execute("UPDATE source_values SET check_status='проверено_вручную' WHERE id=?",(change['source_id'],))
    conn.execute('INSERT INTO meta(key,value) VALUES (?,?)',(meta_key,json.dumps(result,ensure_ascii=False,sort_keys=True)))
    return result


def apply_ocr_source_review(conn,pdf,reference,review):
    """Версия 2: исходные OCR-значения после просмотра изображений оригинала."""
    return _apply_source_review(conn,pdf,reference,review,recognition='ocr',version=2,meta_prefix='ifrs_ocr_review')


def apply_native_source_review(conn,pdf,reference,review):
    """Версия 3: точность непроверенных native-значений, отдельно от полноты тождеств."""
    return _apply_source_review(conn,pdf,reference,review,recognition='native',version=3,meta_prefix='ifrs_source_review')
