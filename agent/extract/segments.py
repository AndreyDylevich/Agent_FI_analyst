"""Сегментные исходные суммы через общий LLM-контракт, с раздельным охватом и полными сверками."""

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from agent import config
from agent.checks.ifrs import FinancialCheck
from agent.checks.reviewed_tables import compile_reviewed_tables
from agent.checks.validate import reconcile
from agent.extract.pipeline import extract_pdf
from agent.extract.schemas import citation, _keys
from agent.extract.store import store_extraction
from agent.metrics.data import UNIT_MULTIPLIERS

CATALOG = Path(__file__).resolve().parents[2] / 'config/ifrs_segments.json'


def segment_items(item_ids):
    """Охват определяется просмотренной колонкой сегмента; документ остаётся отчётностью группы."""
    items = config.line_items()
    return {k:{**items[k], 'reviewed_table':True,
               'source_forms':sorted(set(config.source_forms(items[k])) | {'notes'}),
               'source_topics':['segments']} for k in sorted(item_ids)}


def reviewed_segment_profile(document, segment):
    _keys(segment['source_profile'], {'observations','complete_blocks','identities'})
    return {'version':1,'documents':[{**document['source_profile'], **segment['source_profile']} ]}


def extract_segments(pdf, provider, *, ticker, entity_token, period, output, catalog=None):
    catalog=json.loads(CATALOG.read_text()) if catalog is None else catalog
    if catalog.get('version') != 1:
        raise ValueError('Неизвестная версия сегментной структуры')
    matching=[p for p in catalog['documents'] if p['pdf_sha256']==pdf.sha256 and p['ticker']==ticker]
    if len(matching)!=1:
        raise ValueError('Нет единственной просмотренной сегментной структуры выпуска')
    document=matching[0]
    if output.exists() and any(output.iterdir()):
        raise ValueError('Нужен новый каталог: исходные ответы сегментов не перезаписываются')
    output.mkdir(parents=True,exist_ok=True)
    if not document.get('reviewed') or not document.get('reviewer','').strip():
        raise ValueError('Сегментная структура требует отдельного просмотра')
    source_profile=document['source_profile']
    if (source_profile['pdf_sha256']!=pdf.sha256 or
            (source_profile['report_start'],source_profile['report_end'],source_profile['report_kind'])!=
            (str(period.start),str(period.end),period.kind)):
        raise ValueError('Сегментное раскрытие относится к другой версии/периоду')
    by_page={p.number:p for p in pdf.pages}
    if not source_profile.get('page_text_sha256'):
        raise ValueError('Нет просмотренных версий страниц сегментного раскрытия')
    for number,digest in source_profile['page_text_sha256'].items():
        if int(number) not in by_page or hashlib.sha256(by_page[int(number)].text.encode()).hexdigest()!=digest:
            raise ValueError('Текст/адреса сегментного раскрытия изменились')
    for evidence in document['disclosure_evidence']:
        citation(evidence,{p.number:p for p in pdf.pages},max_lines=80,max_chars=12000)
    if not document['segments']:
        if document.get('quantitative_table_not_disclosed') is not True or document['identities']:
            raise ValueError('Пустой сегментный выпуск не является успешным извлечением')
        report = {'pdf_sha256':pdf.sha256,'ticker':ticker,'runs':[],'checks':[],
                'quantitative_table_not_disclosed':True,'single_operating_segment_reported':document.get('single_operating_segment_reported',False),
                'source_evidence':[asdict(citation(e,{p.number:p for p in pdf.pages},max_lines=80,max_chars=12000)) for e in document['disclosure_evidence']],
                'disclosure_reviewed':True,'financial_values_verified':False,'provider_requests':0,'selected_values_count':0,
                'absence_means_zero':False,'comparative_basis':document['comparative_basis'],
                'disclosure_review_sha256':hashlib.sha256(json.dumps(document,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
                'financial_audit_complete':False,'comparability_approved':False}
        (output/'segments_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        return report,[]
    keys=[s['key'] for s in document['segments']]
    if (len(set(keys))!=len(keys)
            or len({(s['scope'],s['segment_name']) for s in document['segments']}) != len(keys)):
        raise ValueError('Повтор сегмента в плане')
    runs=[]
    known_items=config.line_items()
    for segment in document['segments']:
        if (segment['scope'] not in ('group','segment')
                or segment['scope']=='group' and segment['segment_name'] is not None
                or segment['scope']=='segment' and not segment['segment_name']):
            raise ValueError('Не согласованы сегмент и групповой итог')
        profile=reviewed_segment_profile(document,segment)
        items=segment_items({o['item_id'] for o in segment['source_profile']['observations'].values()})
        if not compile_reviewed_tables(pdf,period,profile,line_items={**known_items,**items}):
            raise ValueError('Сегментная структура относится к другой версии PDF')
        run=extract_pdf(pdf,provider,ticker=ticker,entity_token=entity_token,period=period,
                        line_items=items,sections=('segments',),output=output/segment['key'],
                        compact=True,reviewed_catalog=profile)
        runs.append((segment,run))
    # Каждая сверка использует явно перечисленные колонки: подытог и его дети не суммируются вместе.
    proof={'pdf_sha256':pdf.sha256,'segments':[
        {'key':s['key'],'scope':s['scope'],'segment_name':s['segment_name'],'facts':[asdict(f) for f in run.facts]}
        for s,run in runs]}
    digest=hashlib.sha256(json.dumps(proof,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
    status={s['key']:list(run.statuses) for s,run in runs};extra={s['key']:[] for s,run in runs};checks=[]
    for identity in document['identities']:
        year=identity['year'];start=f'{year}-01-01';end=f'{year}-06-30'
        terms={};values={};involved={};units=[];sources=[];disputed=[]
        for alias,term in identity['terms'].items():
            if (term['segment'] not in keys or term['item_id'] not in known_items
                    or type(term['coefficient']) is not int or not term['coefficient']):
                raise ValueError('Неизвестная колонка/коэффициент сегментной сверки')
            segment,run=next((s,r) for s,r in runs if s['key']==term['segment'])
            found=[(i,f) for i,f in enumerate(run.facts) if f.item_id==term['item_id'] and str(f.start)==start and str(f.end)==end]
            if len(found)>1:
                raise ValueError('Неоднозначная статья сегмента в одном окне')
            terms[alias]=term['coefficient'];values[alias]=found[0][1].normalized if found else None
            if found:
                index,fact=found[0];involved.setdefault(segment['key'],[]).append(index);units.append(UNIT_MULTIPLIERS[fact.unit])
                if status[segment['key']][index]=='расхождение':disputed.append(alias)
                sources.append({'segment':segment['key'],'item_id':fact.item_id,'evidence':asdict(fact.evidence)})
        result=reconcile(identity['rule'],values,terms,tolerance=max(units) if units else 0,disputed=tuple(disputed))
        check={'rule':identity['rule'],'start':start,'end':end,'result':asdict(result),'sources':sources}
        checks.append(check)
        for segment,run in runs:
            indices=tuple(involved.get(segment['key'],[]))
            for index in indices:
                if result.status=='расхождение':status[segment['key']][index]='расхождение'
                elif result.status=='пройдено' and status[segment['key']][index]!='расхождение' and not run.facts[index].from_ocr:
                    status[segment['key']][index]='проверено_авто'
            if indices:
                extra[segment['key']].append(FinancialCheck(identity['rule'],start,end,result,indices,
                    basis={'segment_bundle_sha256':digest,'sources':sources,'terms':terms}))
    finished=[(s,replace(r,statuses=tuple(status[s['key']]),checks=(*r.checks,*extra[s['key']]))) for s,r in runs]
    report={'pdf_sha256':pdf.sha256,'ticker':ticker,'segment_bundle_sha256':digest,
            'reviewer':document['reviewer'],'disclosure_evidence':document['disclosure_evidence'],
            'comparative_basis':document['comparative_basis'],'checks':checks,
            'runs':[{'segment':s,'extraction':asdict(r)} for s,r in finished],
            'financial_audit_complete':False,'comparability_approved':False,
            'limitation':'Раскрытая OIBDA хранится отдельно от расчётной EBITDA. Прочие, элиминации и подытоги не являются самостоятельными отчётными бизнес-сегментами. Пересчитанные сравнения не заменяют старые источники автоматически.'}
    (output/'segments_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n')
    return report,finished


def store_segments(conn, result, finished, *, ticker,name,source_url,period,local_path):
    saved=[]
    for segment,run in finished:
        saved.append({'segment_key':segment['key'],**store_extraction(conn,run,ticker=ticker,name=name,
            source_url=source_url,period=period,local_path=local_path,scope=segment['scope'],segment_name=segment['segment_name'])})
    serialized=json.dumps({'result':result,'stored':saved},ensure_ascii=False,sort_keys=True,default=str)
    digest=hashlib.sha256(serialized.encode()).hexdigest()
    conn.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)',(f'ifrs_segments:{ticker}:{digest}',serialized))
    return saved
