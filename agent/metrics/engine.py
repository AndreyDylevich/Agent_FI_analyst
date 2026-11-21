"""Подготовка входов реестра: периоды, LTM, средние остатки и происхождение данных.

Движок использует рублёвые данные одной компании, стандарта и охвата. Между противоречащими
источниками автоматически не выбирает. Сети и LLM в расчётах нет.
"""

import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from agent.metrics.parameters import PROVIDERS
from agent.metrics.registry import (
    REGISTRY, MetricDef, MetricResult, PeriodRule, Status, evaluate, for_industry, get,
)


@dataclass(frozen=True)
class Period:
    """Календарный период с начала года либо скользящий год (LTM)."""

    start: date
    end: date
    kind: str

    def __post_init__(self) -> None:
        ends = {"Q1": (3, 31), "H1": (6, 30), "9M": (9, 30), "FY": (12, 31)}
        if self.kind == "LTM":
            valid = ((self.end.month, self.end.day) in ends.values()
                     and self.start == self.end.replace(year=self.end.year - 1) + timedelta(days=1))
        else:
            valid = (self.kind in ends and self.start == date(self.end.year, 1, 1)
                     and (self.end.month, self.end.day) == ends[self.kind])
        if not valid:
            raise ValueError(f"Несогласованный календарный период: {self.start} — {self.end} ({self.kind})")

    def ltm(self) -> 'Period':
        """Последние двенадцать месяцев на дату окончания отчёта."""
        return Period(self.end.replace(year=self.end.year - 1) + timedelta(days=1), self.end, "LTM")

    def shift_years(self, offset: int) -> 'Period':
        """Сдвигает обе границы, сохраняя длительность и тип сопоставимого периода."""
        return Period(self.start.replace(year=self.start.year + offset),
                      self.end.replace(year=self.end.year + offset), self.kind)


@dataclass(frozen=True)
class Fact:
    """Нормализованное значение; source_id — строка source_values с документом и страницей."""

    source_id: int
    company_id: int
    item_id: str
    value: Decimal
    end: date
    start: date | None = None
    period_type: str | None = None
    standard: str = "IFRS"
    scope: str = "group"
    segment_name: str | None = None
    currency: str = "RUB"
    check_status: str = "проверено_авто"
    cash_flow_section: str | None = None
    document_id: int | None = None
    accounting_basis: str | None = None
    perimeter_basis: str | None = None
    methodology_version: str | None = None
    comparison_evidence: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            raise ValueError("Исходное значение должно быть конечным Decimal")
        if self.cash_flow_section not in (None, 'operating', 'investing', 'financing', 'mixed'):
            raise ValueError('Неизвестный раздел ОДДС')


@dataclass(frozen=True)
class Prepared:
    """Вход, исходные строки, ограничения и наблюдения на границах периода."""

    value: Decimal | None
    source_ids: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()
    observations: tuple[Decimal, ...] = ()
    reason: str | None = None
    calculations: tuple['Calculation', ...] = ()  # версии и периоды расчётных зависимостей
    absent: bool = False  # исходных данных совсем нет (отличается от неполного состава/конфликта)
    blocked: bool = False  # расхождение источников; резервное допущение запрещено


@dataclass(frozen=True)
class Calculation:
    """Результат с периодом, подготовленными входами и сквозными ссылками на исходные строки."""

    result: MetricResult
    period: Period
    inputs: dict[str, Prepared] = field(default_factory=dict)
    absent: bool = False
    blocked: bool = False


def _unique(items: Iterable) -> tuple:
    """Убирает повторы, сохраняя порядок происхождения."""
    return tuple(dict.fromkeys(items))


class Engine:
    """Подготавливает входы и вызывает реестр; сетевых обращений не делает."""

    def __init__(
        self, facts: Iterable[Fact], *, company_id: int, standard: str,
        scope: str, industry_id: str, industries: dict, segment_name: str | None = None,
        require_comparability: bool = False,
    ) -> None:
        if standard not in ("IFRS", "RSBU"):
            raise ValueError("Нужен стандарт IFRS или RSBU")
        if scope not in ("group", "entity", "segment") or (scope == "segment") != bool(segment_name):
            raise ValueError("Для сегмента нужно название; для группы и юрлица оно не задаётся")
        if standard == "RSBU" and scope != "entity":
            raise ValueError("РСБУ анализируется в охвате отдельного юрлица (entity)")
        self.company_id, self.standard, self.scope = company_id, standard, scope
        self.segment_name = segment_name
        self.require_comparability = require_comparability
        self.allowed = {d.id for d in for_industry(industry_id, industries)}
        note = industries[industry_id].get("notes")
        self.company_notes = (note,) if note else ()
        self.facts = tuple(
            f for f in facts if f.company_id == company_id and f.standard == standard
            and f.scope == scope and f.segment_name == segment_name and f.currency == "RUB"
        )
        self._cache: dict[tuple[str, Period], Calculation] = {}
        self._active: set[tuple[str, Period]] = set()

    def calculate(self, metric_id: str, period: Period) -> Calculation:
        """Вычисляет показатель и зависимости по правилам периода каждого входа."""
        d = get(metric_id)
        target = period.ltm() if d.result_period is PeriodRule.LTM else period
        key = (metric_id, target)
        if key in self._cache:
            return self._cache[key]
        if key in self._active:
            raise ValueError(f"Циклическая зависимость показателей: {metric_id}")
        if metric_id not in self.allowed or self.standard not in d.standards:
            reason = "Показатель неприменим к выбранной отрасли или стандарту отчётности"
            return Calculation(MetricResult(d.id, d.version, Status.NOT_APPLICABLE, None, reason, {}), target)
        if target.kind not in d.period_kinds:
            reason = f"Допустимые периоды: {', '.join(d.period_kinds)}; получен {target.kind}"
            return Calculation(MetricResult(d.id, d.version, Status.NOT_APPLICABLE, None, reason, {}), target)
        self._active.add(key)
        try:
            prepared = {name: self._prepare_input(d, name, rule, target)
                        for name, rule in {**d.inputs, **d.optional_inputs}.items()}
            result = evaluate(metric_id, {name: p.value for name, p in prepared.items()})
            comparison_problem = self._comparison_problem(prepared)
            if comparison_problem:
                result = replace(result,status=Status.INSUFFICIENT_DATA,value=None,explanation=None,
                                 reason=comparison_problem)
            for name in d.positive_inputs:
                if any(v <= 0 for v in prepared[name].observations):
                    result = replace(result, status=Status.NOT_APPLICABLE, value=None, explanation=None,
                                     reason=f"{name} ≤ 0 на границе периода — показатель неприменим")
            for name in d.nonnegative_inputs:
                if any(v < 0 for v in prepared[name].observations):
                    result = replace(result, status=Status.NOT_APPLICABLE, value=None, explanation=None,
                                     reason=f"{name} < 0 на границе периода — требуется проверка данных")
            notes = _unique((*self.company_notes, *result.notes,
                             *(n for p in prepared.values() for n in p.notes),
                             *(f"{name}: {p.reason}" for name, p in prepared.items() if p.reason)))
            if self.standard == "RSBU":
                notes += ("Отчётность отдельного юрлица, не группы",)
            mandatory = [prepared[name] for name in d.inputs]
            calculation = Calculation(
                replace(result, notes=notes), target, prepared,
                absent=bool(mandatory) and all(p.absent for p in mandatory),
                blocked=bool(comparison_problem) or any(p.blocked for p in prepared.values()),
            )
            self._cache[key] = calculation
            return calculation
        finally:
            self._active.remove(key)

    def _comparison_problem(self, prepared: dict[str, Prepared]) -> str | None:
        """Автоматический путь МСФО требует раскрытий для YoY/CAGR/LTM и средних остатков.

        Одни даты/равные числа не подтверждают одинаковые политику, периметр и определение статьи.
        Старый диагностический режим расчётов остаётся явно отключаемым от этого строгого пути.
        """
        if not self.require_comparability or self.standard!='IFRS':
            return None
        used = {source for part in prepared.values() for source in part.source_ids}
        groups: dict[str,list[Fact]] = {}
        for fact in self.facts:
            if fact.source_id in used:
                groups.setdefault(fact.item_id,[]).append(fact)
        reasons = []
        for item,observations in groups.items():
            if len({(fact.start,fact.end) for fact in observations})<2:
                continue
            for field in ('accounting_basis','perimeter_basis','methodology_version'):
                values = {getattr(fact,field) for fact in observations}
                if any(not value or not value.strip() for value in values):
                    reasons.append(f'{item}: не подтверждена {field}')
                elif len(values)>1:
                    reasons.append(f'{item}: различается {field}')
            if any(not fact.comparison_evidence or not fact.comparison_evidence.strip() for fact in observations):
                reasons.append(f'{item}: нет источника подтверждения сопоставимости')
        return ('Сопоставимость периодов МСФО не подтверждена: '+'; '.join(reasons)) if reasons else None

    def _prepare_input(self, definition: MetricDef, name: str, rule: PeriodRule, period: Period) -> Prepared:
        """Разрешает псевдоним и сдвиг года из реестра до применения правила периода."""
        item_id = definition.input_items.get(name, name)
        offset = definition.input_year_offsets.get(name, 0)
        try:
            shifted = period.shift_years(offset)
        except ValueError:
            return Prepared(None, reason=f"период со сдвигом {offset} лет выходит за календарный диапазон")
        if name in definition.parameter_inputs:
            parameter = PROVIDERS[definition.parameter_inputs[name]](shifted.end)
            prepared = Prepared(parameter.value, notes=parameter.notes, reason=parameter.reason,
                                absent=parameter.value is None)
        else:
            prepared = self._resolve(item_id, rule, shifted, definition.input_cash_flow_sections.get(name))
        fallback_id = definition.input_fallbacks.get(name)
        if fallback_id and prepared.value is None and not prepared.blocked:
            fallback = self._resolve(fallback_id, rule, shifted)
            note = f'{name}: {item_id} не определена ({prepared.reason}); резервный показатель — {fallback_id}'
            prepared = replace(
                fallback, notes=_unique((*prepared.notes, note, *fallback.notes)),
                source_ids=_unique((*prepared.source_ids, *fallback.source_ids)),
                calculations=(*prepared.calculations, *fallback.calculations),
            )
        if item_id != name or offset:
            note = f"{name}: {item_id}, {shifted.start} — {shifted.end} ({shifted.kind})"
            prepared = replace(prepared, notes=prepared.notes + (note,))
        return prepared

    def _resolve(self, name: str, rule: PeriodRule, period: Period,
                 sections: tuple[str, ...] | None = None) -> Prepared:
        """Применяет правило периода к исходной статье либо расчётному показателю."""
        if rule is PeriodRule.AT_DATE:
            return self._stock(name, period.end)
        if rule is PeriodRule.AVERAGE:
            end = self._stock(name, period.end)
            begin = self._stock(name, period.start - timedelta(days=1))
            if end.value is None:
                return end
            if begin.value is None:
                # Замена концом допустима при отсутствии начала, но не при конфликте/расхождении.
                if not begin.absent:
                    return begin
                return replace(end, notes=end.notes + (f"{name}: начало периода отсутствует, использован конец",))
            # Средний остаток = (начало периода + конец периода) / 2.
            return Prepared((begin.value + end.value) / Decimal("2"),
                            _unique((*begin.source_ids, *end.source_ids)),
                            _unique((*begin.notes, *end.notes)), (begin.value, end.value),
                            calculations=(*begin.calculations, *end.calculations))
        if rule is PeriodRule.LTM or period.kind == "LTM":
            target = period.ltm()
            if name in REGISTRY and get(name).result_period is PeriodRule.LTM:
                # Показатель уже определён на LTM: коэффициенты нельзя складывать как потоки FY + YTD − YTD.
                return self._derived(name, target)
            year = target.end.year
            if target.end.month == 12:
                return self._flow(name, Period(date(year, 1, 1), target.end, "FY"), sections)
            kind = {(3, 31): "Q1", (6, 30): "H1", (9, 30): "9M"}[(target.end.month, target.end.day)]
            parts = [self._flow(name, Period(date(year - 1, 1, 1), date(year - 1, 12, 31), "FY"), sections),
                     self._flow(name, Period(date(year, 1, 1), target.end, kind), sections),
                     self._flow(name, Period(date(year - 1, 1, 1), target.end.replace(year=year - 1), kind), sections)]
            sources = _unique(s for p in parts for s in p.source_ids)
            notes = _unique(n for p in parts for n in p.notes)
            dependencies = tuple(c for p in parts for c in p.calculations)
            if any(p.value is None for p in parts):
                reasons = "; ".join(p.reason or "нет данных" for p in parts if p.value is None)
                return Prepared(None, sources, notes, reason=f"неполный LTM: {reasons}",
                                absent=all(p.absent for p in parts), blocked=any(p.blocked for p in parts))
            # LTM = год(t−1) + YTD(t) − YTD(t−1).
            value = parts[0].value + parts[1].value - parts[2].value
            return Prepared(value, sources, notes + (f"{name}: LTM = FY(t−1) + YTD(t) − YTD(t−1)",),
                            calculations=dependencies)
        return self._flow(name, period, sections)

    def _stock(self, name: str, day: date) -> Prepared:
        if name in REGISTRY:
            kind = {(3, 31): "Q1", (6, 30): "H1", (9, 30): "9M", (12, 31): "FY"}.get((day.month, day.day))
            if kind is None:
                return Prepared(None, reason="неподдерживаемая дата остатка")
            return self._derived(name, Period(date(day.year, 1, 1), day, kind))
        return self._select(name, day, None, None)

    def _flow(self, name: str, period: Period, sections: tuple[str, ...] | None = None) -> Prepared:
        if name in REGISTRY:
            return self._derived(name, period)
        return self._select(name, period.end, period.start, period.kind, sections)

    def _derived(self, name: str, period: Period) -> Prepared:
        calculation = self.calculate(name, period)
        r = calculation.result
        return Prepared(r.value, _unique(s for p in calculation.inputs.values() for s in p.source_ids),
                        r.notes, observations=(r.value,) if r.value is not None else (),
                        reason=r.reason, calculations=(calculation,),
                        absent=calculation.absent, blocked=calculation.blocked)

    def _select(self, name: str, end: date, start: date | None, kind: str | None,
                sections: tuple[str, ...] | None = None) -> Prepared:
        candidates = [f for f in self.facts if f.item_id == name and f.end == end
                      and f.start == start and (start is None or f.period_type == kind)]
        if not candidates:
            return Prepared(None, reason="нет исходных данных", absent=True)
        if any(f.check_status == "расхождение" for f in candidates):
            return Prepared(None, source_ids=_unique(f.source_id for f in candidates),
                            reason=f"расхождение в исходных данных: {name}, {end}", blocked=True)
        groups: dict[str | None, list[Fact]] = {}
        for f in candidates:
            groups.setdefault(f.cash_flow_section, []).append(f)
        if any(len({f.value for f in group}) != 1 for group in groups.values()):
            return Prepared(None, source_ids=_unique(f.source_id for f in candidates),
                            reason=f"противоречащие источники: {name}, {end}", blocked=True)
        if len(groups) > 1:
            # Детали по разделам суммируются только при полном одинаковом раскрытии в каждом документе.
            # Один документ с итогом без раздела и другой с частью нельзя автоматически складывать.
            documents = {f.document_id for f in candidates}
            complete = all({f.cash_flow_section for f in candidates if f.document_id == doc} == set(groups)
                           for doc in documents)
            if None in documents or None in groups or 'mixed' in groups or not complete:
                return Prepared(None, source_ids=_unique(f.source_id for f in candidates),
                                reason=f'несогласованная классификация по разделам ОДДС: {name}, {end}', blocked=True)
        notes = (f"{name}, {end}: данные не проверены",) if any(
            f.check_status == "не_проверено" for f in candidates) else ()
        observations = tuple(group[0].value for group in groups.values())
        selected = []
        labels = {'operating': 'операционная деятельность (CFO)',
                  'investing': 'инвестиционная деятельность (CFI)',
                  'financing': 'финансовая деятельность (CFF)'}
        for section, group in groups.items():
            value = group[0].value
            if sections is not None:
                if section in (None, 'mixed'):
                    if value != 0:
                        return Prepared(None, source_ids=_unique(f.source_id for f in candidates), notes=notes,
                                        reason=f'неизвестна классификация платежа {name}, {end}; нужна разбивка по разделам ОДДС')
                    notes += (f'{name}, {end}: раскрыт нулевой платёж, раздел ОДДС не влияет на корректировку',)
                elif section not in sections:
                    notes += (f'{name}, {end}: {labels[section]}; платёж уже учтён в CFO, повторно не вычитается',)
                    value = Decimal('0')
                else:
                    notes += (f'{name}, {end}: {labels[section]}; платёж вне CFO учитывается отдельно',)
            selected.append(value)
        # Один платёж на раздел; совпадающие подтверждения из других документов не удваивают сумму.
        value = sum(selected, Decimal('0'))
        return Prepared(value, _unique(f.source_id for f in candidates), notes, observations)

    def save(self, conn: sqlite3.Connection, calculation: Calculation) -> int:
        """Сохраняет новый результат и исходные строки, не перезаписывая историю.

        Для LTM и средних сохраняются все строки; роли видны по датам источников и period_rule.
        Расчётные зависимости сохраняются со своей версией формулы и связью metric_inputs.
        Транзакцией управляет вызывающий код (with conn: ...).
        """
        return self._save(conn, calculation, {})

    def _save(self, conn: sqlite3.Connection, calculation: Calculation, saved: dict) -> int:
        """Сохраняет граф зависимостей без повторов в рамках одного результата."""
        key = (calculation.result.metric_id, calculation.period)
        if key in saved:
            return saved[key]
        d = get(calculation.result.metric_id)
        r, p = calculation.result, calculation.period
        cursor = conn.execute(
            "INSERT INTO metric_values (company_id, metric_id, formula_version, standard, "
            "period_start, period_end, period_type, scope, segment_name, value, unit, status, "
            "status_reason, notes, explanation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.company_id, r.metric_id, r.version, self.standard,
             None if d.result_period is PeriodRule.AT_DATE else p.start.isoformat(),
             p.end.isoformat(), p.kind, self.scope, self.segment_name,
             str(r.value) if r.value is not None else None, d.unit, r.status.value, r.reason,
             json.dumps(r.notes, ensure_ascii=False), r.explanation),
        )
        metric_value_id = cursor.lastrowid
        saved[key] = metric_value_id
        for name, prepared in calculation.inputs.items():
            rule = {**d.inputs, **d.optional_inputs}[name]
            if prepared.calculations:
                for dependency in prepared.calculations:
                    dependency_id = self._save(conn, dependency, saved)
                    conn.execute("INSERT INTO metric_inputs "
                                 "(metric_value_id, input_name, period_rule, input_metric_value_id) "
                                 "VALUES (?, ?, ?, ?)", (metric_value_id, name, rule.name, dependency_id))
            else:
                for source_id in prepared.source_ids:
                    conn.execute("INSERT INTO metric_inputs "
                                 "(metric_value_id, input_name, period_rule, source_value_id) VALUES (?, ?, ?, ?)",
                                 (metric_value_id, name, rule.name, source_id))
        return metric_value_id
