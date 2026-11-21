"""Текст страниц МСФО и выбор форм/примечаний; финансовые таблицы Python не разбирает."""

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

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


def ocr_page(path: Path, number: int) -> str:
    """Локальный OCR Poppler + Tesseract rus/eng, без загрузки изображений во внешнюю модель."""
    if not shutil.which('pdftoppm') or not shutil.which('tesseract'):
        raise ValueError('Для OCR нужны pdftoppm и tesseract с языками rus+eng')
    with tempfile.TemporaryDirectory(prefix='fi-ocr-') as directory:
        prefix = Path(directory)/'page'
        subprocess.run(['pdftoppm', '-f', str(number), '-singlefile', '-r', '200', '-png', str(path), str(prefix)],
                       check=True, capture_output=True, timeout=45)
        result = subprocess.run(['tesseract', str(prefix.with_suffix('.png')), 'stdout', '-l', 'rus+eng', '--psm', '6'],
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
    return PDFText(hashlib.sha256(raw).hexdigest(), tuple(pages), tuple(warnings))


HEADINGS = {
    'balance': r'отчет.{0,80}финансовом положении|statement.{0,80}financial position',
    'income': r'отчет.{0,80}прибылях|statement.{0,80}(?:profit|income)',
    'cash_flow': r'отчет.{0,80}движении денежных средств|statement.{0,80}cash flows',
    'equity': r'отчет.{0,80}изменени.{0,15}капитал|statement.{0,80}changes in equity',
    'debt': r'кредиты и займы|заемные средства|loans and borrowings|lease liabilities|обязательства по аренде',
    'segments': r'сегментная информация|операционные сегменты|segment information|operating segments',
    'basis': r'основа подготовки|основы подготовки|международн.{0,60}стандарт|international financial reporting|basis of preparation',
}


@dataclass(frozen=True)
class Selection:
    groups: dict[str, tuple[int, ...]]
    missing_forms: tuple[str, ...]


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
            if re.search(pattern, candidate):
                groups[key].append(page.number)
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
