# Контракт v2.0 — информационная система анализа качества продукции

Этот документ — замороженный контракт между слоями и параллельными агентами.
Он имеет приоритет над соображениями удобства: расхождение с ним — сигнал
ОСТАНОВИТЬСЯ и сообщить, а не «поправить по-тихому». Все имена файлов, таблиц,
полей, маршрутов и функций обязательны.

---

## 1. Стек и запуск

* Python 3.13.13, FastAPI + Jinja2 (серверный рендеринг), SQLite (stdlib
  `sqlite3`), pytest. В браузере JavaScript НЕ используется: все графики —
  серверный инлайновый SVG.
* Зависимости зафиксированы в `requirements.txt` (полный разрешённый набор).
  `python-multipart` отсутствует намеренно.
* Виртуальное окружение и команды:

```
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m scripts.generate_demo_data
```

* Переменная окружения `DIPL2_DB` задаёт путь к файлу БД. Относительный путь
  разрешается от `PROJECT_DIR`; значение по умолчанию —
  `PROJECT_DIR / "data" / "quality.sqlite3"`.
* `resolve_db_path()` — единственная точка вычисления пути к БД.

---

## 2. Схема БД (SCHEMA_VERSION = 1)

Полный текст `app/db/schema.sql`:

```sql
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
```

### 2.1 Перечисления

* `batches.status`: `pending`, `accepted`, `partial`, `rejected`.
* `inspections.result`: `accepted`, `partial`, `rejected`.
* `defects_ref.group_name` (ровно эти строки): `Внешний вид`, `Размеры`,
  `Прочность`, `Материал`, `Функциональные`, `Упаковка`,
  `Сопроводительные документы`, `Прочее`.
* Линии: `Линия-1`, `Линия-2`, `Линия-3`. Сущности «смена» НЕТ.

### 2.2 Правила согласованности

* `accepted_qty + rejected_qty = sample_qty` — гарантируется `CHECK`.
* `defect_events.qty` НЕ обязано совпадать с `rejected_qty`: у одной единицы
  может быть несколько кодов дефектов, а дефекты фиксируются и на принятых
  единицах.
* `batches.status` пересчитывается сервером после каждой регистрации акта:
  * нет актов → `pending`;
  * Σ`rejected_qty` = 0 и Σ`accepted_qty` > 0 → `accepted`;
  * Σ`accepted_qty` = 0 и Σ`rejected_qty` > 0 → `rejected`;
  * иначе → `partial`.
* KPI «доля принятых партий» =
  `count(status='accepted') / count(status != 'pending')`; при пустом
  знаменателе — `0`.
* `measurements`: на пару (`inspection_id`, `parameter_id`) приходится n
  значений (обычно ровно 5); уникальность по паре НЕ накладывается.

---

## 3. Индекс качества

Период расчёта — месяц, в котором лежит `as_of`, назад на `months` месяцев
(по умолчанию `DEFAULT_MONTHS = 12`). Для каждой позиции продукции:

* `defect_rate = rejected_qty / sample_qty`.
* `C1 = clamp(1 − defect_rate / defect_rate_cap, 0, 1)`; n/a при `sample_qty = 0`.
* `C2 = clamp(in_spec / with_specs, 0, 1)`; n/a при `with_specs = 0`.
* `C3 = clamp(1 − (repeat_qty / defect_qty) / repeat_rate_cap, 0, 1)`; n/a при
  `defect_qty = 0`.
* `C4 = clamp(batches_clean / batches_total, 0, 1)`; n/a при
  `batches_total = 0`.
* Веса нормируются ТОЛЬКО над доступными критериями:
  `w'_i = w_i / Σ w_available`.
* `raw_score = Σ w'_i · f_i`, округление до `ROUND_DP = 6`.
* Статус:
  * `sample_qty < min_sample` → `insufficient_data`, `score = None`,
    `raw_score` заполнен;
  * ни одного доступного критерия → `no_data`, `score = None`;
  * иначе → `ok`.

Ключи критериев: `defect`, `spec`, `repeat`, `clean` (им соответствуют веса
`w_defect`, `w_spec`, `w_repeat`, `w_clean`).

Норматив дефектности в системе НЕ хранится. «Норматив» — это допуски параметра
(`lsl`/`usl`/`target` справочника `parameters_ref`). Это вынесенное в ADR
решение; на вопрос «а где ПДу?» отвечать устно.

---

## 4. SPC (статистическое управление процессом)

* Подгруппа = все `measurements` одного акта ОТК по одному параметру
  (`group by inspection_id, parameter_id`), порядок по `inspected_at, id`.
* Метка подгруппы (`label`) = `batch_no`.
* Если `n` вне диапазона `SPC_N_MIN..SPC_N_MAX` (2..10), подгруппа
  исключается и учитывается в `excluded_subgroups`.

### 4.1 Константы (ГОСТ Р ИСО 7870-2 / AIAG SPC), n = 2..10

| n  | d2    | D3    | D4    | A2    |
|----|-------|-------|-------|-------|
| 2  | 1.128 | 0     | 3.267 | 1.880 |
| 3  | 1.693 | 0     | 2.574 | 1.023 |
| 4  | 2.059 | 0     | 2.282 | 0.729 |
| 5  | 2.326 | 0     | 2.114 | 0.577 |
| 6  | 2.534 | 0     | 2.004 | 0.483 |
| 7  | 2.704 | 0.076 | 1.924 | 0.419 |
| 8  | 2.847 | 0.136 | 1.864 | 0.373 |
| 9  | 2.970 | 0.184 | 1.816 | 0.337 |
| 10 | 3.078 | 0.223 | 1.777 | 0.308 |

### 4.2 Формулы

* `R̄` — среднее `R` по включённым подгруппам.
* Модальная `n` (`modal_n`; при равенстве частот — меньшая) задаёт `A2`, `D3`,
  `D4`, `d2` для ВСЕХ подгрупп.
* `X̿` — среднее `x̄` по включённым подгруппам.
* Карта `X̄`: `CL = X̿`, `UCL = X̿ + A2·R̄`, `LCL = X̿ − A2·R̄`.
* Карта `R`: `CL_r = R̄`, `UCL_r = D4·R̄`, `LCL_r = D3·R̄` (при `D3 = 0`
  `LCL_r` рисуется как 0).
* `σ̂ = R̄ / d2`.
* Меньше `SPC_MIN_SUBGROUPS` (10) включённых подгрупп → `enough = False`,
  границы не считаются, в `warnings` попадает сообщение
  «Недостаточно подгрупп для расчётных границ (N < 10)».
* `Cp = (usl − lsl) / (6·σ̂)` — при известных обоих допусках.
* `Cpk = min((usl − X̿)/(3·σ̂), (X̿ − lsl)/(3·σ̂))` — при обоих допусках,
  `cpk_side = "two"`.
* При одном допуске — `Cpu`/`Cpl` (`cpk_side = "upper"` / `"lower"`).
* Нет допусков → `None` (отображается «—», НИКОГДА не 0).

### 4.3 Нарушения (только карта X̄)

* `beyond_limit` — `x̄` выше `UCL` или ниже `LCL`.
* `zone_a_2_of_3` — среди трёх последовательных подгрупп не менее двух на одной
  стороне от `CL` с `|x̄ − CL| ≥ 2·σ̂`; нарушение относится к индексу третьей
  подгруппы тройки.

Тексты нарушений — по-русски (`violation_texts`). Округление: значения карт —
`SPC_VALUE_DP = 4`, `Cp`/`Cpk` — `SPC_CPDP = 3`. Окно расчёта — подгруппы за
период `months` до `as_of`.

---

## 5. Таблица маршрутов

| Метод | Путь | Query-параметры | active | Заголовок | Статусы |
|-------|------|-----------------|--------|-----------|---------|
| GET | `/` | — | `home` | Главная | 200 |
| GET | `/products` | `q` (поиск по названию/SKU), `category`, `page` (≥1, клампится) | `products` | Продукция | 200 |
| GET | `/products/{id}` | — | `products` | Карточка продукции | 200 / 404 |
| GET | `/batches` | `status` (один из 4), `product_id`, `page` | `batches` | Партии | 200 |
| GET | `/batches/{id}` | — | `batches` | Партия | 200 / 404 |
| GET | `/batches/{id}/inspections/new` | — | `batches` | Регистрация акта ОТК | 200 / 404 |
| POST | `/batches/{id}/inspections/new` | — | `batches` | — | 303 / 400 / 404 |
| GET | `/analysis/pareto` | `months` (clamp 1..36) | `pareto` | Дефекты (Парето) | 200 |
| GET | `/analysis/trends` | `months` (clamp 1..36) | `trends` | Динамика | 200 |
| GET | `/analysis/spc` | `param` (код; по умолчанию первый критичный), `months` | `spc` | Контроль процесса | 200 |
| GET | `/config/weights` | — | `config` | Настройки | 200 |
| POST | `/config/weights` | — | `config` | — | 303 / 400 |
| GET | `/export/batches.csv` | — | — | — | 200 (attachment) |
| GET | `/export/defects.csv` | `months` | — | — | 200 (attachment) |
| GET | `/static/...` | — | — | — | 200 |
| — | любая неизвестная | — | — | Ошибка 404 | 404 |
| — | внутренняя ошибка | — | — | Ошибка 500 | 500 |

Контекст поведения:

* `GET /` — сводка: KPI-карточки, топ-5 дефектов, ссылки на разделы.
* `GET /products` — список с поиском, фильтром категории и пагинацией.
* `GET /products/{id}` — карточка: показатели периода, индекс качества,
  последние партии; 404 при отсутствии.
* `GET /batches` — список с фильтрами; `page` клампится до SQL-запроса.
* `GET /batches/{id}` — акты ОТК, дефекты, измерения; кнопка «Регистрация акта
  ОТК»; 404 при отсутствии.
* `GET /batches/{id}/inspections/new` — форма; `POST` того же пути: успех →
  `303` на `/batches/{id}`, ошибка валидации → `400` с тем же шаблоном и
  списком ошибок.
* `GET /analysis/pareto`, `/analysis/trends`, `/analysis/spc` — аналитические
  экраны; `months` клампится в `MIN_MONTHS..MAX_MONTHS`.
* `GET|POST /config/weights` — POST принимает 4 веса + `defect_rate_cap` +
  `repeat_rate_cap` + `min_sample` + `author` (обязателен, ≥1 символа); успех →
  `303` на `/config/weights`, ошибка → `400`.
* `GET /export/batches.csv`, `/export/defects.csv` — выгрузки с заголовком
  `Content-Disposition: attachment`.

### 5.1 Поля формы акта ОТК

* `inspector` — непустой, ≤100 символов.
* `inspected_at` — ISO-дата, действительная и не позже завтра.
* `sample_qty`, `accepted_qty`, `rejected_qty` — целые, `sample_qty > 0`,
  `accepted_qty + rejected_qty = sample_qty`.
* `defect_<code>` — количество дефекта (необязательно).
* `param_<code>` — ровно 5 чисел через запятую (необязательно). Если поле
  непустое и число значений ≠ 5 — ошибка. Диапазон значений НЕ проверяется:
  значения вне допуска — реальные данные, их и ищет SPC.

Пустые `defect_*`/`param_*` игнорируются. Тело формы разбирается вручную через
`urllib.parse.parse_qsl` (без `python-multipart`).

---

## 6. CSV-выгрузки

Разделитель `CSV_DELIMITER = ";"`, кодировка `CSV_ENCODING = "utf-8-sig"`,
`csv.writer` (dialect `excel` → CRLF), заголовок ровно одной строкой.

* `partii.csv` (константа `CSV_BATCHES_FILENAME`), колонки:
  `ID;SKU;Продукция;Линия;Статус;Произведено;Дата;Актов ОТК` — все партии без
  фильтра.
* `defekty.csv` (константа `CSV_DEFECTS_FILENAME`), колонки:
  `Код;Дефект;Группа;Критичность;Количество;Доля` — за `months`; доля от общего
  количества с `SHARE_DP = 4` знаками (например, `0.4123`).

---

## 7. Контракт функций

Реализаторы могут добавлять приватные помощники, но публичные сигнатуры ниже
менять нельзя.

### 7.1 Репозитории (B1) — `app/db/repositories.py`

```python
list_products(conn, *, q=None, category=None, active_only=False,
              limit=PAGE_SIZE, offset=0) -> list[dict]
count_products(conn, *, q=None, category=None, active_only=False) -> int
get_product(conn, product_id) -> dict | None
list_categories(conn) -> list[str]
list_defects(conn, *, active_only=True) -> list[dict]
get_defect_by_code(conn, code) -> dict | None
list_parameters(conn, *, critical_first=True) -> list[dict]
get_parameter_by_code(conn, code) -> dict | None

list_batches(conn, *, product_id=None, status=None,
             limit=PAGE_SIZE, offset=0) -> list[dict]
    # строки содержат product_sku, product_name, inspections_count
count_batches(conn, *, product_id=None, status=None) -> int
get_batch(conn, batch_id) -> dict | None          # с полями продукта
recompute_batch_status(conn, batch_id) -> str

list_inspections(conn, batch_id) -> list[dict]
defects_for_inspections(conn, inspection_ids) -> dict[int, list[dict]]
measurements_for_inspections(conn, inspection_ids) -> dict[int, list[dict]]

create_inspection(conn, *, batch_id: int, draft: InspectionDraft,
                  defects_ref: dict[str, int],
                  params_ref: dict[str, int]) -> dict
load_period_facts(conn, *, since: str, until: str) -> dict[int, ProductFacts]
load_monthly_facts(conn, *, since: str, until: str) -> list[MonthlyFacts]
load_pareto_counts(conn, *, since: str, until: str) -> list[DefectCount]
load_spc_subgroups(conn, *, parameter_id: int, since: str, until: str) -> list[SpcSubgroup]
dashboard_stats(conn, *, since: str, until: str) -> dict
get_weights(conn) -> IndexWeights
update_weights(conn, *, author: str, **fields) -> IndexWeights
```

* `create_inspection` — одна транзакция: вставляет акт, вычисляет `result`,
  вставляет `defect_events` (правило `is_repeat`: `1`, если тот же `defect_id`
  уже встречался в ПРЕДЫДУЩИХ актах ЭТОЙ ЖЕ партии, иначе `0`), вставляет
  `measurements` (по 5 значений на параметр), пересчитывает статус партии,
  возвращает строку акта.
* `dashboard_stats` возвращает ключи: `batches_total`, `batches_pending`,
  `accepted_share`, `defect_rate`, `index_overall`, `top_defect_code`,
  `top_defect_name`, `top_defect_qty`, `parameters_critical`, `products_active`,
  `inspections_count`.
* `get_weights` при отсутствии строки вставляет базовую (`INSERT OR IGNORE`).
* `update_weights` валидирует: веса ≥ 0 и их сумма ≈ 1, `cap > 0`,
  `min_sample ≥ 0`, `author` непустой; нарушение → `ValueError`; `version + 1`.

### 7.2 Анализ (B2) — `app/analysis/*.py`

```python
quality_index.compute_all(facts: Mapping[int, ProductFacts], weights: IndexWeights,
                          *, as_of: date, months: int) -> list[IndexResult]
quality_index.compute_one(f, weights, *, as_of, months, ...) -> IndexResult
# сортировка compute_all: score DESC, None в конец, затем sku

pareto.compute_pareto(counts: Sequence[DefectCount], *, months: int) -> ParetoResult
# ABC: накопленная доля ≤ 0.80 → A, ≤ 0.95 → B, иначе C
# top_share = доля первой строки; share/cumulative — доли 0..1 c ROUND_DP

trends.compute_trends(months_seq: Sequence[str],
                      facts: Sequence[MonthlyFacts]) -> list[TrendPoint]
# месяцы без данных → точка с defect_rate = None

spc.compute_spc(subgroups: Sequence[SpcSubgroup], parameter: ParameterSpec,
                *, period: str) -> SpcResult

charts.pareto_chart(result: ParetoResult, *, width=980, height=430) -> str
charts.trend_chart(points: Sequence[TrendPoint], *, width=980, height=360) -> str
charts.spc_chart(result: SpcResult, *, width=980, height=430) -> str
charts.weight_bar(weights: IndexWeights, *, width=520) -> str
```

Требования к SVG:

* весь динамический текст проходит через `html.escape`;
* `role="img"` и `<title>` с русским описанием;
* палитра — из `config.CHART_SERIES`, оси/сетка — `CHART_AXIS_TEXT` /
  `CHART_GRID`;
* при пустом входе возврат
  `f'<p class="chart-empty">{CHART_EMPTY_TEXT}</p>'`;
* никаких внешних шрифтов и скриптов;
* идентификаторы элементов уникальны внутри одного вызова (через переданный
  префикс, без глобального счётчика).

---

## 8. Демо-данные (инварианты для генератора B1)

CLI:

```
python -m scripts.generate_demo_data [--products 10] [--batches-per-product 18]
    [--months 24] [--seed 20260101] [--anchor 2026-09-30]
    [--db data/quality.sqlite3] [--force-reference]
```

Правила:

* только добавление (natural keys: продукт `sku`, параметр `code`, дефект
  `code`, партия `(product_id, batch_no)`); ничего не удаляется;
* чистый `random.Random(seed)`; итерирование по `set`/`dict`, влияющее на id,
  запрещено — только списки и сортировка. Повторный запуск с тем же seed даёт
  одинаковые числовые значения;
* обязательные посадки:
  1. у продукта `SKU-1004` параметр `BORE` — две последовательные подгруппы со
     смещением `target + 6σ` (σ из внутренней таблицы генератора, σ ≈ 0.05 →
     гарантирован `beyond_limit`);
  2. один дефект-код даёт ≥ 35 % количества за период (для Парето);
  3. есть партии всех четырёх статусов (в т.ч. одна `pending` — без актов);
  4. ≥ 30 записей `defect_events.is_repeat = 1`;
  5. 5 измерений на (акт, параметр) для ВСЕХ параметров;
  6. референс: 10 продуктов, 6 параметров, 12 дефектов в 8 группах, линии
     «Линия-1..3».

Генератор НЕ импортирует `app.analysis`. Перед завершением:
`PRAGMA wal_checkpoint(TRUNCATE)`, закрыть соединение, удалить `-wal`/`-shm`
при наличии.

---

## 9. Соглашения

* Обработчики — синхронные; первая строка — `conn = db_conn()`.
* Все внутренние ссылки в шаблонах — корневые (`/...`), без префиксов роутеров.
* Все строковые литералы интерфейса — по-русски.
* Рендер страниц идёт через шов `render()` (владелец — B3).
* Тестовая политика: тестовые БД складываются в `tests_artifacts/` и
  УДАЛЯЮТСЯ при teardown (в v2 это разрешено). Поставляемая БД
  (`data/quality.sqlite3`) открывается read-only с `PRAGMA integrity_check`.

---

## 10. Роли агентов и файлы

| Роль | Файлы |
|------|-------|
| B1 (данные) | `app/db/repositories.py`, `app/db/seed.py`, `scripts/generate_demo_data.py`, `data/quality.sqlite3` |
| B2 (анализ) | `app/analysis/*.py` |
| B3 (веб) | `app/main.py`, `app/web/*` (включая шаблоны и статику) |
| тестер | `tests/*` |
| докер | `README.md`, `docs/*` (кроме `contract.md`, который сверяется) |

* `app/config.py`, `app/domain/models.py`, `app/db/schema.sql`,
  `app/db/connection.py` уже готовы и являются частью контракта.
* Ни один агент не изменяет файлы вне своего списка.
* Любое расхождение с `contract.md` → агент обязан ОСТАНОВИТЬСЯ и сообщить,
  а не «поправить».
