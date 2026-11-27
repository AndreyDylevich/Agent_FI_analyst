"""Извлечение МСФО в явно указанную БД. Без Telegram, расписания и изменения watchlist."""

import argparse
import json
from contextlib import nullcontext
from dataclasses import asdict
from datetime import date
from pathlib import Path

from agent import config, db
from agent.extract.llm import CA_PATH, GigaChatProvider, ProviderError
from agent.extract.pdf_text import HEADINGS, read_pdf
from agent.extract.ocr_transcript import apply_reviewed_ocr_punctuation
from agent.extract.pipeline import extract_pdf
from agent.extract.store import store_extraction
from agent.metrics.data import load_facts
from agent.metrics.engine import Engine, Period
from agent.metrics.registry import REGISTRY
from agent.sources.files import checked_url


class CachedOnlyProvider:
    def extract(self, *_args):
        raise ProviderError('cached-only: нет подходящего проверяемого ответа; сетевой запрос к модели запрещён')


def main(argv: list[str] | None = None) -> int:
    """Первый путь PDF → LLM → проверки → SQLite → только проверенные входы расчётов."""
    companies = {c['ticker']: c for c in config.load('watchlist')['companies']}
    items = config.line_items()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--ticker', choices=sorted(companies), required=True)
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--period', choices=['FY','Q1','H1','9M'], required=True)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='Новый каталог ответов и протокола')
    parser.add_argument('--certificate', type=Path, default=CA_PATH)
    parser.add_argument('--sections', choices=list(HEADINGS), nargs='+')
    parser.add_argument('--item', choices=sorted(items), action='append')
    parser.add_argument('--metric', choices=sorted(REGISTRY), action='append')
    parser.add_argument('--ocr', action='store_true')
    parser.add_argument('--max-ocr-pages', type=int, default=20)
    parser.add_argument('--ocr-dpi',type=int,choices=[320,400],default=320)
    parser.add_argument('--reuse-from',type=Path,action='append',help='Артефакт прежнего прогона с pdf/ и extraction/; ответы проверяются заново')
    parser.add_argument('--compact-contract',action='store_true',help='ИИ возвращает значения по фиксированным ключам статья/дата')
    parser.add_argument('--cached-only',action='store_true',help='Повторная проверка только сохранённых ответов, без ключа/API модели')
    args = parser.parse_args(argv)
    if args.cached_only and not args.reuse_from:
        parser.error('--cached-only требует --reuse-from')
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('Каталог вывода должен быть новым/пустым: история ответов не перезаписывается')
    args.output.mkdir(parents=True, exist_ok=True)
    company = companies[args.ticker]
    hosts = {h for page in company['ir_pages'] for h in page.get('allowed_hosts', [])}
    source_url = checked_url(args.source_url, hosts)
    month, day = {'FY': (12,31), 'Q1': (3,31), 'H1': (6,30), '9M': (9,30)}[args.period]
    period = Period(date(args.year,1,1), date(args.year,month,day), args.period)
    entity_token = company.get('document_identity_token', company['legal_name'])
    if args.item:
        items = {key: items[key] for key in args.item}
    report = {'ok': False, 'ticker': args.ticker, 'financial_audit_complete': False, 'messages_sent': 0,
              'cached_only':args.cached_only}
    original_ocr = None
    try:
        text = read_pdf(args.pdf, use_ocr=args.ocr, max_ocr_pages=args.max_ocr_pages,ocr_dpi=args.ocr_dpi)
        if args.ocr:
            original_ocr = asdict(text)
            text = apply_reviewed_ocr_punctuation(text)
        report.update(pdf_sha256=text.sha256,text_processing=text.processing,pdf_warnings=text.warnings)
        manager=nullcontext(CachedOnlyProvider()) if args.cached_only else GigaChatProvider.from_env(args.certificate)
        with manager as provider:
            options = {'sections': tuple(args.sections)} if args.sections else {}
            if args.reuse_from:options['reuse_from']=tuple(args.reuse_from)
            if args.compact_contract:options['compact']=True
            run = extract_pdf(text, provider, ticker=args.ticker, entity_token=entity_token, period=period,
                              line_items=items, output=args.output, **options)
        report.update(pdf_sha256=run.pdf_sha256, batches=run.batches, contexts=run.contexts,
                      facts=[asdict(f) for f in run.facts], checks=[asdict(c) for c in run.checks],
                      statuses=run.statuses, warnings=run.warnings,
                      requested_observations_count=run.requested_observations_count,
                      missing_observations=run.missing_observations,
                      all_requested_observations_present=not run.missing_observations)
        report['reviewed_disclosure_gaps'] = run.reviewed_disclosure_gaps
        conn = db.init_db(args.db)
        try:
            with conn:
                imported = store_extraction(conn, run, ticker=args.ticker, name=company['name'],
                                            source_url=source_url, period=period, local_path=str(args.pdf))
                facts = load_facts(conn, company_id=imported['company_id'], standard='IFRS', scope='group',
                                   segment_name=None, line_items=config.line_items())
                # Неисследованные/OCR-строки сохраняются, но в автоматический расчёт не допускаются.
                checked = [f for f in facts if f.check_status != 'не_проверено']
                engine = Engine(checked, company_id=imported['company_id'], standard='IFRS', scope='group',
                                industry_id=company['industry']['id'], industries=config.industries(),
                                require_comparability=True)
                calculations = []
                for metric in args.metric or ['current_ratio', 'net_debt', 'revenue_yoy', 'fcf_simple']:
                    result = engine.calculate(metric, period)
                    saved_id = engine.save(conn, result)
                    calculations.append({**asdict(result.result), 'status': result.result.status.value, 'saved_id': saved_id})
                report.update(import_result=imported, metrics=calculations)
            report['ok'] = not imported['conflicts'] and 'расхождение' not in run.statuses
        finally:
            conn.close()
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
    if original_ocr is not None:
        (args.output/'ocr_original_pages.json').write_text(json.dumps(original_ocr,ensure_ascii=False,indent=2)+'\n')
    (args.output/'extraction_check.json').write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2)+'\n', encoding='utf-8')
    print(f"{args.ticker}: {'извлечение выполнено' if report['ok'] else 'требуется проверка'}; "
          f"строк: {len(report.get('facts', []))}; протокол сохранён", flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
