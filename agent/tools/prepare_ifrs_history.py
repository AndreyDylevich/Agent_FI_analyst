"""Проверка локального годового архива перед извлечением: SHA256, титул, контекст и формы."""

import argparse
import hashlib
import json
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path

from agent import config
from agent.classify import classify_document
from agent.extract.pdf_text import read_pdf, find_sections
from agent.extract.schemas import source_context
from agent.metrics.engine import Period
from agent.sources.files import checked_url, MAX_PDF_BYTES
from agent.tools.backfill_ifrs import MANIFEST


def prepare_archive(archive: Path, entries: list[dict], companies: dict, output: Path,
                    *, use_ocr=False, max_ocr_pages=20, reader=read_pdf) -> dict:
    """Архив не задаёт доверенные URL/периоды: они сверяются с выбранным манифестом и PDF.

    Финансовые числа не извлекаются и не одобряются. Старые протоколы/страницы не меняются.
    """
    if output.exists() and any(output.iterdir()):
        raise ValueError('Нужен новый/пустой каталог подготовки истории')
    if max_ocr_pages<0:raise ValueError('Лимит OCR не может быть отрицательным')
    output.mkdir(parents=True,exist_ok=True)
    protocol=json.loads((archive/'history_check.json').read_text())
    documents=[]
    for entry in entries:
        row={key:entry[key] for key in ('ticker','expected_year','source_url')}
        row.update(ready_for_extraction=False,financial_values_verified=False,
                   comparability_approved=False)
        try:
            company=companies[entry['ticker']]
            source=next(page for page in company['ir_pages'] if page['url']==entry['source_page'])
            checked_url(entry['source_url'],set(source['allowed_hosts']))
            matches=[item for item in protocol['files'] if item['ticker']==entry['ticker']
                     and item['expected_year']==entry['expected_year'] and item.get('url')==entry['source_url']]
            if len(matches)!=1:raise ValueError('Нужен ровно один архивный документ выбранной компании/года/URL')
            sha=matches[0].get('sha256','')
            if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):
                raise ValueError('В архиве нет корректного SHA256 документа')
            path=archive/'pdf'/f'{sha}.pdf'
            if not path.resolve().is_relative_to((archive/'pdf').resolve()):
                raise ValueError('PDF ссылается за пределы архива')
            if path.stat().st_size>MAX_PDF_BYTES:raise ValueError('PDF превышает лимит 20 МБ')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=sha:
                raise ValueError('SHA256 локального PDF не совпадает с архивным протоколом')
            pdf=reader(path,use_ocr=use_ocr,max_ocr_pages=max_ocr_pages)
            if pdf.sha256!=sha:raise ValueError('При чтении PDF изменился SHA256')
            title=classify_document(entry['discovery_hint'],pdf.pages[0].text,recognition='pdf_first_page')
            year=entry['expected_year']; period=Period(date(year,1,1),date(year,12,31),'FY')
            metadata=(title.basis=='pdf_first_page' and title.doc_type=='ifrs' and title.standard=='IFRS'
                      and title.period_end==str(period.end) and title.period_type=='FY' and not title.needs_review)
            context=source_context(pdf.pages,ticker=entry['ticker'],period=period,
                entity_token=company.get('document_identity_token',company['legal_name']),line_items=config.line_items())
            sections=find_sections(pdf)
            forms={form:list(sections.groups[form]) for form in ('balance','income','cash_flow','equity')}
            complete_forms=all(forms.values())
            folder=output/f"{entry['ticker']}_{year}";folder.mkdir()
            (folder/'pages.json').write_text(json.dumps(asdict(pdf),ensure_ascii=False,indent=2)+'\n')
            row.update(pdf_sha256=sha,local_path=str(path),classification=asdict(title),
                       document_metadata_verified=metadata,source_context=context,forms=forms,
                       all_main_forms_located=complete_forms,pages=len(pdf.pages),pdf_warnings=pdf.warnings,
                       text_processing=pdf.processing,
                       special_purpose_title=bool(re.search(r'специальн.{0,20}назначени|special.purpose',pdf.pages[0].text,re.I|re.S)),
                       ready_for_extraction=bool(metadata and context and complete_forms))
            if not row['ready_for_extraction']:
                row['review_reason']='Не подтверждены метаданные, контекст или расположение всех основных форм'
        except (ValueError, TypeError, KeyError, OSError, StopIteration) as error:
            row['error']=f'{type(error).__name__}: {error}'
        documents.append(row)
        print(f"{entry['ticker']} FY{entry['expected_year']}: готов к извлечению={row['ready_for_extraction']}",flush=True)
    result={'documents':documents,'ready_count':sum(row['ready_for_extraction'] for row in documents),
            'financial_history_validated':False,'comparability_approved':False,'messages_sent':0,
            'ok':bool(documents) and all(row['ready_for_extraction'] for row in documents),
            'limitation':'Готовность текста не подтверждает финансовые значения и сопоставимость. Специальная основа подготовки требует проверки исключений. OCR не повышает статусы чисел.'}
    (output/'history_preparation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--ticker',action='append')
    parser.add_argument('--from-year',type=int,default=2021)
    parser.add_argument('--through-year',type=int,default=2025)
    parser.add_argument('--ocr',action='store_true')
    parser.add_argument('--max-ocr-pages',type=int,default=20)
    args=parser.parse_args(argv)
    if args.from_year>args.through_year or args.max_ocr_pages<0:parser.error('Некорректный диапазон лет/лимит OCR')
    companies={c['ticker']:c for c in config.load('watchlist')['companies']}
    if args.ticker and set(args.ticker)-set(companies):parser.error('Тикер отсутствует в списке наблюдения')
    entries=[entry for entry in json.loads(MANIFEST.read_text())['documents']
             if (not args.ticker or entry['ticker'] in args.ticker)
             and args.from_year<=entry['expected_year']<=args.through_year]
    report=prepare_archive(args.archive,entries,companies,args.output,use_ocr=args.ocr,max_ocr_pages=args.max_ocr_pages)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
