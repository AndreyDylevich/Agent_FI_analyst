-- Снимок исходной схемы v1 до добавления cash_flow_section; миграционный эталон.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    id         INTEGER PRIMARY KEY,
    ticker     TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    inn        TEXT,
    ogrn       TEXT,
    girbo_id   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Документы: каждый найденный файл. Первый запуск по компании — status = 'baseline' (без скачивания).
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id),
    url           TEXT NOT NULL,
    sha256        TEXT,
    title         TEXT,
    doc_type      TEXT CHECK (doc_type IN
                    ('ifrs', 'rsbu', 'press_release', 'presentation', 'annual_report', 'other')),
    standard      TEXT CHECK (standard IN ('IFRS', 'RSBU')),
    period_start  TEXT,
    period_end    TEXT,
    period_type   TEXT CHECK (period_type IN ('FY', 'Q1', 'H1', '9M')),
    published_at  TEXT,
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    status        TEXT NOT NULL DEFAULT 'new' CHECK (status IN
                    ('baseline', 'new', 'downloaded', 'extracted', 'error')),
    source        TEXT CHECK (source IN ('girbo', 'ir_page', 'press_release', 'moex')),
    local_path    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (url, sha256)
);
CREATE INDEX IF NOT EXISTS ix_documents_company_url ON documents(company_id, url);

-- Отрасль компании: история предложений и подтверждений, не перезапись.
CREATE TABLE IF NOT EXISTS company_industry (
    id             INTEGER PRIMARY KEY,
    company_id     INTEGER NOT NULL REFERENCES companies(id),
    industry_id    TEXT NOT NULL,
    segments       TEXT,                 -- JSON-список сегментов
    business_model TEXT,
    confidence     TEXT,
    rationale      TEXT,
    document_id    INTEGER REFERENCES documents(id),
    location       TEXT,                 -- страница или раздел документа
    origin         TEXT NOT NULL CHECK (origin IN ('auto', 'user')),
    valid_from     TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Исходные значения из отчётности и операционных раскрытий.
CREATE TABLE IF NOT EXISTS source_values (
    id             INTEGER PRIMARY KEY,
    company_id     INTEGER NOT NULL REFERENCES companies(id),
    item_id        TEXT NOT NULL,        -- id из config/line_items.yaml или операционного показателя
    original_name  TEXT,                 -- наименование статьи как в отчёте
    value          TEXT NOT NULL,        -- как в отчёте, точно
    unit           TEXT NOT NULL,        -- руб. / тыс. руб. / млн руб. / тыс. т / млн абонентов / шт.
    value_norm     TEXT,                 -- нормализовано (руб. для денежных)
    currency       TEXT,
    period_start   TEXT,                 -- NULL для балансовых (на дату)
    period_end     TEXT NOT NULL,
    period_type    TEXT CHECK (period_type IN ('FY', 'Q1', 'H1', '9M')),
    standard       TEXT NOT NULL CHECK (standard IN ('IFRS', 'RSBU', 'operational')),
    scope          TEXT NOT NULL CHECK (scope IN ('group', 'entity', 'segment')),
    segment_name   TEXT,
    document_id    INTEGER REFERENCES documents(id),
    source         TEXT NOT NULL CHECK (source IN ('girbo', 'ir_page', 'press_release', 'moex')),
    location       TEXT,                 -- страница или раздел
    extracted_by   TEXT NOT NULL CHECK (extracted_by IN ('girbo', 'gigachat', 'manual')),
    check_status   TEXT NOT NULL DEFAULT 'не_проверено' CHECK (check_status IN
                     ('не_проверено', 'проверено_авто', 'расхождение', 'проверено_вручную')),
    is_restated    INTEGER NOT NULL DEFAULT 0,   -- сравнительные данные пересчитаны компанией
    is_one_off     INTEGER NOT NULL DEFAULT 0,   -- разовая статья
    one_off_note   TEXT,                         -- обоснование из отчёта
    superseded_by  INTEGER REFERENCES source_values(id),  -- NULL = актуальная строка
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_source_values_lookup ON source_values(company_id, item_id, period_end);

CREATE VIEW IF NOT EXISTS source_values_current AS
    SELECT * FROM source_values WHERE superseded_by IS NULL;

-- Рассчитанные показатели. Ряды разных стандартов не смешиваются: standard обязателен.
CREATE TABLE IF NOT EXISTS metric_values (
    id              INTEGER PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    metric_id       TEXT NOT NULL,
    formula_version INTEGER NOT NULL,
    standard        TEXT NOT NULL CHECK (standard IN ('IFRS', 'RSBU')),
    period_start    TEXT,
    period_end      TEXT NOT NULL,
    period_type     TEXT CHECK (period_type IN ('FY', 'Q1', 'H1', '9M', 'LTM')),
    scope           TEXT NOT NULL DEFAULT 'group' CHECK (scope IN ('group', 'entity', 'segment')),
    segment_name    TEXT,
    value           TEXT,                -- NULL, если не рассчитан
    unit            TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('рассчитан', 'неприменим', 'недостаточно_данных')),
    status_reason   TEXT,
    notes           TEXT,                -- пометки, например «не раскрыто: impairment_noncurrent»
    explanation     TEXT,                -- формула → числа → результат
    calculated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_metric_values_lookup
    ON metric_values(company_id, metric_id, standard, period_end);

-- Какие исходные значения или другие показатели вошли в расчёт.
CREATE TABLE IF NOT EXISTS metric_inputs (
    id                    INTEGER PRIMARY KEY,
    metric_value_id       INTEGER NOT NULL REFERENCES metric_values(id),
    input_name            TEXT NOT NULL,
    period_rule           TEXT NOT NULL,          -- AT_DATE / AVERAGE / FLOW / LTM
    source_value_id       INTEGER REFERENCES source_values(id),
    input_metric_value_id INTEGER REFERENCES metric_values(id),
    CHECK ((source_value_id IS NULL) <> (input_metric_value_id IS NULL))
);

CREATE TABLE IF NOT EXISTS market_data (
    id                 INTEGER PRIMARY KEY,
    company_id         INTEGER NOT NULL REFERENCES companies(id),
    date               TEXT NOT NULL,
    share_class        TEXT NOT NULL DEFAULT 'common',
    price              TEXT,
    shares_outstanding TEXT,
    market_cap         TEXT,
    source             TEXT NOT NULL,
    fetched_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, date, share_class, source)
);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    signal_id       TEXT NOT NULL,       -- id из config/thresholds.yaml
    kind            TEXT NOT NULL CHECK (kind IN ('quantitative', 'qualitative')),
    severity        TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
    period_end      TEXT,
    value           TEXT,
    threshold       TEXT,
    metric_value_id INTEGER REFERENCES metric_values(id),
    document_id     INTEGER REFERENCES documents(id),
    location        TEXT,
    message         TEXT NOT NULL,
    sent_at         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
