"""Повторное использование неизменённых ответов той же версии PDF с новой полной проверкой."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from agent.extract.llm import Completion


@dataclass(frozen=True)
class CachedResponse:
    completion: Completion
    record: dict
    source: str
    item_ids: frozenset[str]


class ResponseCache:
    """Кэш не одобряет данные: конвейер заново проверяет цитаты, даты, единицы и тождества."""

    def __init__(self, roots: tuple[Path,...], pdf_sha256: str):
        self.entries=[]
        for root in roots:
            originals=list((root/'pdf').glob('*.pdf'))
            if not any(hashlib.sha256(path.read_bytes()).hexdigest()==pdf_sha256 for path in originals):
                raise ValueError('Кэш ответов не содержит оригинальный PDF с тем же SHA256')
            folder=root/'extraction'
            if not (folder/'attempts.json').is_file():
                raise ValueError('В кэше нет журнала extraction/attempts.json')
            records=json.loads((folder/'attempts.json').read_text())
            if not isinstance(records,list) or len(records)>1000:
                raise ValueError('Некорректный журнал кэша ответов')
            for record in records:
                if not record.get('accepted') or not record.get('file'):
                    continue
                path=(folder/record['file']).resolve()
                if not path.is_relative_to(folder.resolve()) or path.stat().st_size>2000000:
                    raise ValueError('Недопустимый путь или размер ответа кэша')
                content=path.read_text()
                if hashlib.sha256(content.encode()).hexdigest()!=record.get('response_sha256'):
                    raise ValueError('Контрольная сумма сохранённого ответа модели не совпадает')
                data=json.loads(content)
                item_ids=frozenset(record['item_ids']) if record.get('item_ids') else frozenset(row['item_id'] for row in data['facts'])
                self.entries.append(CachedResponse(Completion(content,record['model'],0),record,str(path),item_ids))

    def take(self, target_pages: set[int], item_ids: set[str], page_hash: str, has_ocr: bool):
        """Совпадение целевых страниц/статей; OCR без точного хеша текста повторно не используется."""
        for index,entry in enumerate(self.entries):
            if set(entry.record['target_pages'])!=target_pages or entry.item_ids!=item_ids:
                continue
            previous_hash=entry.record.get('page_text_sha256')
            if previous_hash and previous_hash!=page_hash or has_ocr and not previous_hash:
                continue
            return self.entries.pop(index)
        return None
