-- Схема БД v2.0 (SCHEMA_VERSION = 1). Все объекты создаются идемпотентно
-- через IF NOT EXISTS; механизма миграций нет. Русские комментарии поясняют
-- назначение таблиц и правила согласованности.

-- Версия схемы: по одной строке на применённую версию (строку 1 пишет init_db).
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL
);

-- Справочник продукции предприятия.
CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sku         TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    category    TEXT    NOT NULL DEFAULT '',
    unit        TEXT    NOT NULL DEFAULT 'шт.',
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_products_category ON products(category);

-- Справочник контролируемых параметров. Допуски заданы для параметра
-- глобально и могут отсутствовать (target/lsl/usl допускают NULL).
CREATE TABLE IF NOT EXISTS parameters_ref (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    unit        TEXT    NOT NULL DEFAULT '',
    target      REAL,
    lsl         REAL,
    usl         REAL,
    is_critical INTEGER NOT NULL DEFAULT 0 CHECK (is_critical IN (0,1)),
    CHECK (lsl IS NULL OR usl IS NULL OR lsl < usl)
);

-- Справочник видов дефектов. group_name — одно из фиксированных значений
-- перечисления (см. docs/contract.md).
CREATE TABLE IF NOT EXISTS defects_ref (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    group_name  TEXT    NOT NULL,
    severity    INTEGER NOT NULL DEFAULT 3 CHECK (severity BETWEEN 1 AND 5),
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1))
);

-- Партии продукции, выпущенные на линиях. Номер партии уникален в рамках
-- продукции; status пересчитывается сервером после каждой регистрации акта.
CREATE TABLE IF NOT EXISTS batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    batch_no     TEXT    NOT NULL,
    line         TEXT    NOT NULL DEFAULT '',
    produced_qty INTEGER NOT NULL CHECK (produced_qty > 0),
    produced_at  TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','partial','rejected')),
    UNIQUE (product_id, batch_no)
);
CREATE INDEX IF NOT EXISTS ix_batches_product ON batches(product_id, produced_at);
CREATE INDEX IF NOT EXISTS ix_batches_status ON batches(status);

-- Акты ОТК по партиям. Для каждого акта accepted_qty + rejected_qty = sample_qty.
CREATE TABLE IF NOT EXISTS inspections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    inspected_at TEXT    NOT NULL,
    inspector    TEXT    NOT NULL,
    sample_qty   INTEGER NOT NULL CHECK (sample_qty > 0),
    accepted_qty INTEGER NOT NULL CHECK (accepted_qty >= 0),
    rejected_qty INTEGER NOT NULL CHECK (rejected_qty >= 0),
    result       TEXT    NOT NULL CHECK (result IN ('accepted','partial','rejected')),
    CHECK (accepted_qty + rejected_qty = sample_qty)
);
CREATE INDEX IF NOT EXISTS ix_inspections_batch ON inspections(batch_id, inspected_at);

-- Дефекты, зафиксированные в актах ОТК. Сумма qty не обязана совпадать
-- с rejected_qty: у единицы может быть несколько кодов, а дефекты бывают
-- и на принятых единицах. is_repeat=1, если код уже встречался в предыдущих
-- актах этой же партии.
CREATE TABLE IF NOT EXISTS defect_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    defect_id     INTEGER NOT NULL REFERENCES defects_ref(id) ON DELETE RESTRICT,
    qty           INTEGER NOT NULL CHECK (qty > 0),
    is_repeat     INTEGER NOT NULL DEFAULT 0 CHECK (is_repeat IN (0,1)),
    note          TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_defect_events_inspection ON defect_events(inspection_id);
CREATE INDEX IF NOT EXISTS ix_defect_events_defect ON defect_events(defect_id);

-- Измерения параметров в акте ОТК. На одну пару (акт, параметр) приходится
-- n замеров (обычно 5), поэтому уникальность по ним НЕ накладывается.
CREATE TABLE IF NOT EXISTS measurements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    parameter_id  INTEGER NOT NULL REFERENCES parameters_ref(id) ON DELETE RESTRICT,
    value         REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_measurements_inspection ON measurements(inspection_id);
CREATE INDEX IF NOT EXISTS ix_measurements_parameter ON measurements(parameter_id);

-- Конфигурация индекса качества: ровно одна строка (id = 1).
CREATE TABLE IF NOT EXISTS quality_index_config (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    version            INTEGER NOT NULL DEFAULT 1,
    w_defect           REAL    NOT NULL DEFAULT 0.40,
    w_spec             REAL    NOT NULL DEFAULT 0.30,
    w_repeat           REAL    NOT NULL DEFAULT 0.15,
    w_clean            REAL    NOT NULL DEFAULT 0.15,
    defect_rate_cap    REAL    NOT NULL DEFAULT 0.10,
    repeat_rate_cap    REAL    NOT NULL DEFAULT 0.20,
    min_sample         INTEGER NOT NULL DEFAULT 10,
    notes              TEXT    NOT NULL DEFAULT '',
    updated_at         TEXT    NOT NULL,
    updated_by         TEXT    NOT NULL DEFAULT 'system',
    CHECK (abs(w_defect + w_spec + w_repeat + w_clean - 1.0) < 0.000001),
    CHECK (defect_rate_cap > 0),
    CHECK (repeat_rate_cap > 0),
    CHECK (min_sample >= 0)
);
