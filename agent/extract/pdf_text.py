"""Текст страниц МСФО и выбор форм/примечаний; финансовые таблицы Python не разбирает."""

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader
from importlib.metadata import version

from agent.sources.files import MAX_PDF_BYTES


@dataclass(frozen=True)
class Page:
    """Физический номер PDF с единицы; печатный номер в колонтитуле может отличаться."""

    number: int
    text: str
    method: str = 'native'


@dataclass(frozen=True)
class PDFText:
    sha256: str
    pages: tuple[Page, ...]
    warnings: tuple[str, ...] = ()
    processing: dict = field(default_factory=dict)


def ocr_page(path: Path, number: int) -> str:
    """Локальный OCR Poppler + Tesseract rus/eng, без загрузки изображений во внешнюю модель."""
    if not shutil.which('pdftoppm') or not shutil.which('tesseract'):
        raise ValueError('Для OCR нужны pdftoppm и tesseract с языками rus+eng')
    with tempfile.TemporaryDirectory(prefix='fi-ocr-') as directory:
        prefix = Path(directory)/'page'
        subprocess.run(['pdftoppm', '-f', str(number), '-singlefile', '-r', '320', '-png', str(path), str(prefix)],
                       check=True, capture_output=True, timeout=45)
        result = subprocess.run(['tesseract', str(prefix.with_suffix('.png')), 'stdout', '-l', 'rus+eng', '--psm', '6',
                                 '-c', 'preserve_interword_spaces=1'],
                                check=True, capture_output=True, text=True, timeout=60)
        return result.stdout


def read_pdf(path: Path, *, use_ocr: bool = False, max_ocr_pages: int = 20, ocr=ocr_page) -> PDFText:
    """Читает PDF локально. Непрочитанные страницы остаются явно отмеченными, не пустыми фактами."""
    if path.stat().st_size > MAX_PDF_BYTES:
        raise ValueError('PDF превышает лимит 20 МБ')
    if max_ocr_pages < 0:
        raise ValueError('Лимит OCR не может быть отрицательным')
    raw = path.read_bytes()
    if not raw.startswith(b'%PDF-'):
        raise ValueError('Нет сигнатуры PDF')
    pages, warnings, used = [], [], 0
    with path.open('rb') as stream:
        reader = PdfReader(stream)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('PDF требует пароль')
        if not 1 <= len(reader.pages) <= 200:
            raise ValueError('Поддерживается PDF от 1 до 200 страниц')
        for number, source in enumerate(reader.pages, 1):
            text = source.extract_text(extraction_mode='layout') or ''
            method = 'native'
            if len(re.sub(r'\s', '', text)) < 30:
                method = 'unreadable'
                if use_ocr and used < max_ocr_pages:
                    used += 1
                    try:
                        text = ocr(path, number)
                        method = 'ocr' if len(re.sub(r'\s', '', text)) >= 30 else 'unreadable'
                    except (ValueError, OSError, subprocess.SubprocessError) as error:
                        warnings.append(f'Страница {number}: OCR недоступен ({type(error).__name__})')
                if method == 'unreadable':
                    warnings.append(f'Страница {number}: недостаточно читаемого текста; требуется OCR/ручная проверка')
            pages.append(Page(number, text, method))
    processing={'pypdf':version('pypdf'),'native_mode':'layout','ocr_requested':use_ocr}
    if use_ocr:
        processing['ocr']={'version':2,'dpi':320,'languages':'rus+eng','psm':6,'preserve_interword_spaces':True,
                           'max_pages':max_ocr_pages,'attempted_pages':used}
    return PDFText(hashlib.sha256(raw).hexdigest(), tuple(pages), tuple(warnings),processing)


HEADINGS = {
    'balance': r'отчет.{0,80}финансовом положении|statement.{0,80}financial position',
    'income': r'отчет.{0,80}(?:прибылях|прибыли)|statement.{0,80}(?:profit|income)',
    'cash_flow': r'отчет.{0,80}движении денежных средств|statement.{0,80}cash flows',
    'equity': r'отчет.{0,80}изменени.{0,15}капитал|statement.{0,80}changes in equity',
    'debt': r'долговые обязательства|кредиты и займы|кредиты и прочие финансовые обязательства|заемные средства|loans and borrowings',
    'leases': r'активы в форме права пользования|обязательства по аренде|lease liabilities|leases',
    'interest': r'финансовые доходы и расходы|finance income and costs|finance costs|interest expense',
    'perimeter': r'выбытие компаний|приобретение компаний|объединени.{0,10}бизнес|disposal of subsidiar|business combinations|acquisitions',
    'segments': r'сегментная информация|информация по сегментам|операционные сегменты|segment information|operating segments',
    'basis': r'основа подготовки|основы подготовки|принципы подготовки|принцип.{0,20}учетной политик|положени.{0,20}учетной политик|международн.{0,60}стандарт|international financial reporting|basis of preparation|accounting policies',
}


def _note_topics(pdf: PDFText) -> dict[str, set[int]]:
    """Находит нумерованные заголовки примечаний и их продолжения, без разбора финансовых таблиц."""
    topics = {key: set() for key in HEADINGS if key not in ('balance','income','cash_flow','equity')}
    active: set[str] = set()
    for page in pdf.pages:
        if page.method == 'unreadable':
            active = set()
            continue
        if not re.search(r'примечания к|notes to',page.text[:700],re.I):
            continue
        # Начало страницы может продолжать прежнее примечание до нового заголовка.
        encountered = set(active)
        lines=page.text.splitlines()
        for index,line in enumerate(lines):
            title = re.match(r'^\s*\d{1,2}\.\s+([^\d].{3,180})$', line)
            if title:
                parts=[title[1]]
                for following in lines[index+1:index+3]:
                    following=following.strip()
                    if following and following.isupper() and not re.search(r'\d',following):
                        parts.append(following)
                    else:
                        break
                name = ' '.join(' '.join(parts).lower().replace('ё','е').split())
                active = {key for key in topics if re.search(HEADINGS[key],name)}
                encountered.update(active)
        for key in encountered:
            topics[key].add(page.number)
    return topics


@dataclass(frozen=True)
class Selection:
    groups: dict[str, tuple[int, ...]]
    missing_forms: tuple[str, ...]


def heading_matches(pattern: str, text: str) -> bool:
    """Пробелы между глифами заголовка не меняют исходный текст/адреса финансовых цитат."""
    return bool(re.search(pattern,text) or re.search(pattern.replace(' ',''),re.sub(r'\s+','',text)))


def find_sections(pdf: PDFText) -> Selection:
    """Ищет заголовки форм и тем примечаний, не извлекает числа и не трактует таблицы."""
    groups = {key: [] for key in HEADINGS}
    for page in pdf.pages:
        if page.method == 'unreadable':
            continue
        full = ' '.join(page.text.lower().replace('ё', 'е').split())
        heading = full[:1100]
        if re.search(r'\bсодержание\b|table of contents', heading[:150]):
            continue
        for key, pattern in HEADINGS.items():
            if key in ('balance', 'income', 'cash_flow', 'equity'):
                if re.search(r'примечания к|notes to|заключение|обзорной проверки', heading[:250]):
                    continue
                candidate = heading[:450]
            else:
                candidate = full if key == 'basis' else heading
            if heading_matches(pattern, candidate):
                groups[key].append(page.number)
    # При явных заголовках примечаний не считаем каждое упоминание аренды/долга полным раскрытием.
    # В частности, таблица связанных сторон не заменяет долговое примечание всей группы.
    for key,numbers in _note_topics(pdf).items():
        if numbers:
            groups[key] = sorted(numbers)
    return Selection({k: tuple(v) for k, v in groups.items()},
                     tuple(k for k in ('balance', 'income', 'cash_flow', 'equity') if not groups[k]))


def page_batches(pages: tuple[Page, ...], *, max_chars: int = 18000, max_pages: int = 3) -> list[tuple[Page, ...]]:
    """Ни одна выбранная страница не обрезается молча: длинную нужно подготовить отдельно."""
    batches, current, size = [], [], 0
    if max_chars < 1 or max_pages < 1:
        raise ValueError('Лимиты пакета должны быть положительными')
    for page in pages:
        if page.method == 'unreadable':
            raise ValueError(f'Нельзя отправлять нечитаемую страницу {page.number}')
        if len(page.text) > max_chars:
            raise ValueError(f'Страница {page.number} превышает лимит текста; требуется ручная подготовка')
        if current and (size + len(page.text) > max_chars or len(current) >= max_pages):
            batches.append(tuple(current)); current, size = [], 0
        current.append(page); size += len(page.text)
    if current:
        batches.append(tuple(current))
    return batches
