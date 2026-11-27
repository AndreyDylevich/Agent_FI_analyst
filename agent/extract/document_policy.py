"""Просмотренные раскрытия учётной базы/периметра/представления, отдельно от финансовых чисел."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from agent import config
from agent.extract.schemas import citation,normalized_text
from agent.metrics.engine import Period

CATALOG=Path(__file__).resolve().parents[2]/'config/ifrs_document_policies.json'
PREFIX='ifrs_document_policy:'
TOPICS={'accounting_basis','accounting_policy','perimeter','restatement','ifrs16','ifrs18','cash_flow','definition'}


def compile_document_policy(pdf,ticker,period:Period,catalog=None):
    """Цитаты копируются с проверенных адресов. Числа и сопоставимость не извлекаются/не одобряются."""
    catalog=json.loads(CATALOG.read_text()) if catalog is None else catalog
    if type(catalog.get('version')) is not int or catalog['version']!=1:raise ValueError('Неизвестная версия раскрытий учётной базы')
    matching=[d for d in catalog['documents'] if d['pdf_sha256']==pdf.sha256]
    if not matching:return None
    if len(matching)!=1:raise ValueError('Неоднозначный просмотренный выпуск')
    profile=matching[0]
    if (profile.get('reviewed') is not True or profile.get('ticker')!=ticker
            or (profile['report_start'],profile['report_end'],profile['report_kind'])!=(str(period.start),str(period.end),period.kind)
            or not all(isinstance(profile.get(k),str) and profile[k].strip() for k in ('reviewer','reason'))):
        raise ValueError('Нужны точный период/компания и явный просмотр раскрытий')
    pages={p.number:p for p in pdf.pages}
    if not profile.get('page_text_sha256'):raise ValueError('Нет проверенных версий страниц раскрытий')
    for n,digest in profile['page_text_sha256'].items():
        if int(n) not in pages or hashlib.sha256(pages[int(n)].text.encode()).hexdigest()!=digest:
            raise ValueError('Текст/адреса раскрытий изменились; нужна новая проверка')
        if pages[int(n)].method not in ('native','ocr') or pages[int(n)].method=='ocr' and profile.get('ocr_visual_review_confirmed') is not True:
            raise ValueError('OCR раскрытий требует отдельного просмотра оригинала')
    evidence={}
    for key,entry in profile['evidence'].items():
        if entry['topic'] not in TOPICS:raise ValueError('Неизвестная тема раскрытия')
        proof=citation(entry['address'],pages,max_lines=80,max_chars=12000)
        if str(proof.page) not in profile['page_text_sha256']:raise ValueError('Цитата вне просмотренных страниц')
        if not entry.get('contains') or any(normalized_text(hint).casefold() not in normalized_text(proof.quote).casefold() for hint in entry['contains']):
            raise ValueError(f'{key}: содержание просмотренного раскрытия не подтверждено цитатой')
        evidence[key]={'topic':entry['topic'],**asdict(proof),'text_method':pages[proof.page].method}
    def required(ids,topics):
        if not isinstance(ids,list) or not ids or any(k not in evidence for k in ids) or not topics<={evidence[k]['topic'] for k in ids}:
            raise ValueError('Не подтверждены отдельные основания вывода о представлении')
    basis=profile['financial_reporting_basis']
    if basis['kind'] not in ('ias34','ifrs_annual','special_purpose_ifrs_based'):raise ValueError('Неизвестная основа подготовки')
    required(basis['evidence_ids'],{'accounting_basis'})
    for name in ('policy_changes','perimeter_events','comparative_presentation'):
        for change in profile[name]:
            if not change.get('description','').strip():raise ValueError('Нужно описание ограничения/изменения')
            required(change['evidence_ids'],{'accounting_policy'} if name=='policy_changes' else {'perimeter'} if name=='perimeter_events' else {'restatement'})
    lease=profile['ifrs16']
    if lease['lease_payment_definition'] not in ('principal_explicit','lease_payment_unspecified','not_disclosed'):raise ValueError('Неизвестный состав арендного платежа')
    required(lease['evidence_ids'],{'ifrs16'})
    adoption=profile['ifrs18']
    if adoption['adoption'] not in ('not_early_adopted','adopted','not_disclosed_in_reviewed_notes'):raise ValueError('Неизвестное состояние IFRS 18')
    if type(adoption['transition_bridge_reviewed']) is not bool:raise ValueError('Нужно явное состояние переходной сверки IFRS 18')
    if adoption['adoption']!='not_disclosed_in_reviewed_notes':required(adoption['evidence_ids'],{'ifrs18'})
    elif adoption['evidence_ids'] or adoption['transition_bridge_reviewed']:
        raise ValueError('Отсутствие раскрытия не подтверждает применение или переходный мост')
    if adoption['adoption']=='adopted' and adoption['transition_bridge_reviewed']:
        required(adoption['evidence_ids'],{'ifrs18','restatement','cash_flow'})
    items=config.line_items()
    for row in profile['cash_flow_classifications']:
        if row['item_id'] not in items or items[row['item_id']]['statement']!='cash_flow' or row['section'] not in ('operating','investing','financing'):
            raise ValueError('Неизвестная статья/классификация ОДДС')
        required(row['evidence_ids'],{'cash_flow'})
    digest=hashlib.sha256(json.dumps(profile,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return {'version':1,'pdf_sha256':pdf.sha256,'ticker':ticker,'period_start':str(period.start),'period_end':str(period.end),'period_type':period.kind,
            'profile_sha256':digest,'reviewer':profile['reviewer'],'reason':profile['reason'],'evidence':evidence,
            **{name:profile[name] for name in ('financial_reporting_basis','policy_changes','perimeter_events','comparative_presentation','ifrs16','ifrs18','cash_flow_classifications')},
            'source_numbers_modified':False,'source_financial_statuses_modified':False,'comparability_approved':False,
            'limitation':'Просмотренные раскрытия подтверждают только явно описанные свойства конкретного выпуска. Не подтверждают постоянный периметр всех сравнительных периодов, полноту финансовых проверок или автоматическую сопоставимость.'}


def store_document_policy(conn,pdf,ticker,period,catalog=None):
    policy=compile_document_policy(pdf,ticker,period,catalog)
    if policy is None:raise ValueError('Нет просмотренных раскрытий выбранной версии PDF')
    docs=conn.execute('SELECT d.* FROM documents d JOIN companies c ON c.id=d.company_id WHERE c.ticker=? AND d.sha256=?',(ticker,pdf.sha256)).fetchall()
    if len(docs)!=1 or any(docs[0][k]!=v for k,v in {'standard':'IFRS','period_start':str(period.start),'period_end':str(period.end),'period_type':period.kind}.items()):
        raise ValueError('Нет единственного исходного документа нужной компании/окна')
    payload={**policy,'document_id':docs[0]['id']};serialized=json.dumps(payload,ensure_ascii=False,sort_keys=True);digest=hashlib.sha256(serialized.encode()).hexdigest();key=PREFIX+str(docs[0]['id'])+':'+digest
    existing=conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    if existing:
        if existing['value']!=serialized:raise ValueError('Журнал раскрытий изменился')
    else:conn.execute('INSERT INTO meta(key,value) VALUES (?,?)',(key,serialized))
    return {'meta_key':key,'policy':payload,'policy_sha256':digest,'source_numbers_modified':False,'source_financial_statuses_modified':False,'comparability_approved':False}


def load_document_policies(conn,document_id):
    """Читает неизменяемые журналы; повреждённое раскрытие не может поддерживать сравнение."""
    result=[]
    for row in conn.execute('SELECT key,value FROM meta WHERE key LIKE ?',(PREFIX+str(document_id)+':%',)):
        payload=json.loads(row['value']);digest=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if row['key']!=PREFIX+str(document_id)+':'+digest or payload.get('document_id')!=document_id:
            raise ValueError('Контрольная сумма журнала учётной базы изменилась')
        doc=conn.execute('SELECT d.*,c.ticker FROM documents d JOIN companies c ON c.id=d.company_id WHERE d.id=?',(document_id,)).fetchone()
        if not doc or (doc['sha256'],doc['ticker'],doc['period_start'],doc['period_end'],doc['period_type'])!=(payload['pdf_sha256'],payload['ticker'],payload['period_start'],payload['period_end'],payload['period_type']):
            raise ValueError('Журнал учётной базы относится к другой версии/компании/окну')
        result.append({'meta_key':row['key'],'policy':payload})
    return result
