# ИС анализа качества продукции

Информационная система анализа качества продукции промышленного предприятия.
Ведёт справочники продукции, контролируемых параметров и видов дефектов,
регистрирует акты ОТК по партиям и рассчитывает индекс качества, Парето-анализ
дефектов, динамику показателей и контрольные карты SPC. Интерфейс полностью
серверный: вся разметка приходит из Jinja2, графики — инлайновый SVG.

## Стек

* Python 3.13.13.
* FastAPI + Jinja2 (серверный рендеринг), SQLite (stdlib `sqlite3`).
* Графики — серверный инлайновый SVG, клиентский JavaScript не используется.
* pytest.
* Зависимости зафиксированы в `requirements.txt`. `python-multipart` в него
  намеренно не входит (см. [ADR 0001](docs/adr/0001-manual-form-parsing.md)).

## Структура проекта

```
app/
  config.py              константы, пути, resolve_db_path()
  main.py                фабрика FastAPI, middleware, страницы ошибок
  analysis/              B2: quality_index.py, pareto.py, trends.py, spc.py, charts.py
  db/                    B1: connection.py, schema.sql, repositories.py, seed.py
  domain/models.py       доменные датаклассы (сквозной контракт слоёв)
  web/                   B3: context.py, deps.py, pages_*.py, templates/, static/
scripts/
  generate_demo_data.py  генератор демо-данных
data/
  quality.sqlite3        поставляемая база данных
docs/
  contract.md            замороженный контракт (источник истины)
  architecture.md        описание слоёв и швов
  adr/                   архитектурные решения
tests/                   тесты pytest (4 файла)
tests_artifacts/         тестовые БД (создаются и удаляются при тестах)
pytest.ini               конфигурация pytest (testpaths = tests)
requirements.txt         зафиксированные зависимости
```

## Быстрый старт

Из корня проекта (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m scripts.generate_demo_data
.\.venv\Scripts\python.exe -m uvicorn app.main:app
```

Приложение поднимется на `http://127.0.0.1:8000` (значения по умолчанию
uvicorn: `--host 127.0.0.1`, `--port 8000`).

Демо-данные идемпотентны: повторный запуск `generate_demo_data` с тем же
`--seed` не изменяет и не дублирует строки. Полный набор ключей CLI:

```
python -m scripts.generate_demo_data [--products 10] [--batches-per-product 18]
    [--months 24] [--seed 20260101] [--anchor 2026-09-30]
    [--db data/quality.sqlite3] [--force-reference]
```

Тесты (4 файла в `tests/`):

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Последний запуск: `124 passed, 1 warning`. Тестовые базы данных складываются
в `tests_artifacts/` и удаляются при teardown (контракт §9); поставляемая БД
`data/quality.sqlite3` при этом открывается read-only.

## Переменная окружения DIPL2_DB

Путь к файлу базы данных. Относительный путь разрешается от корня проекта
(`PROJECT_DIR`); значение по умолчанию — `PROJECT_DIR / "data" / "quality.sqlite3"`.
Путь вычисляет единственная функция `resolve_db_path()` в `app/config.py`.

```powershell
$env:DIPL2_DB = "data/other.sqlite3"
```

## Маршруты

Все пути корневые, без префиксов роутеров.

| Метод | Путь | Что делает |
|-------|------|------------|
| GET | `/` | Главная: KPI-карточки, топ-5 дефектов, ссылки на разделы |
| GET | `/products` | Продукция: поиск, фильтр категории, пагинация |
| GET | `/products/{id}` | Карточка продукции: показатели периода, индекс качества, последние партии (404 при отсутствии) |
| GET | `/batches` | Партии: фильтры по статусу и продукции, пагинация |
| GET | `/batches/{id}` | Партия: акты ОТК, дефекты, измерения (404 при отсутствии) |
| GET | `/batches/{id}/inspections/new` | Форма регистрации акта ОТК (404 при отсутствии) |
| POST | `/batches/{id}/inspections/new` | Регистрация акта: 303 на карточку партии, 400 при ошибках формы; непустой `param_<code>` — ровно 5 конечных чисел через запятую (не-число или `nan`/`inf` → 400), диапазон не проверяется (§5.1) |
| GET | `/analysis/pareto` | Дефекты (Парето), `months` клампится в 1..36 |
| GET | `/analysis/trends` | Динамика, `months` клампится в 1..36 |
| GET | `/analysis/spc` | Контроль процесса, `param` (по умолчанию первый критичный), `months` |
| GET | `/config/weights` | Настройки индекса качества |
| POST | `/config/weights` | Сохранение настроек: 303 при успехе, 400 при ошибке (в т.ч. не-число, `nan`/`inf`, отрицательные веса или пороги, сумма четырёх весов ≠ 1) |
| GET | `/export/batches.csv` | Выгрузка партий (attachment) |
| GET | `/export/defects.csv` | Выгрузка дефектов (attachment), `months` |
| GET | `/static/...` | Статические файлы |
| — | любая неизвестная | Ошибка 404 |
| — | внутренняя ошибка | Ошибка 500 |

## Модель данных

Схема БД — `SCHEMA_VERSION = 1`, объекты создаются идемпотентно. Подробности —
[`docs/contract.md`](docs/contract.md) §2.

* `products` — справочник продукции (`sku` уникален).
* `parameters_ref` — контролируемые параметры с допусками `target`/`lsl`/`usl`
  (могут быть `NULL`).
* `defects_ref` — виды дефектов, `group_name` — одно из восьми фиксированных
  значений.
* `batches` — партии продукции; `status` (`pending`/`accepted`/`partial`/
  `rejected`) пересчитывается сервером после каждой регистрации акта.
* `inspections` — акты ОТК; гарантировано `accepted_qty + rejected_qty = sample_qty`.
* `defect_events` — дефекты актов; `is_repeat = 1`, если код уже встречался в
  предыдущих актах этой же партии.
* `measurements` — измерения параметров, по 5 значений на пару «акт × параметр»
  (непустой `param_<code>` формы — ровно 5 конечных чисел).
* `quality_index_config` — ровно одна строка (`id = 1`): веса критериев,
  пороги `defect_rate_cap`/`repeat_rate_cap`, `min_sample`.
* `schema_version` — применённые версии схемы.

## Анализ

* **Индекс качества** (`app/analysis/quality_index.py`) — за месяц `as_of`
  назад на `months` (по умолчанию 12). Четыре критерия: дефектность (`defect`),
  соответствие допускам (`spec`), повторяемость (`repeat`), чистые партии
  (`clean`). Веса нормируются только над доступными критериями. Статусы:
  `ok`, `insufficient_data` (выборка меньше `min_sample`), `no_data`.
* **Парето** (`app/analysis/pareto.py`) — дефекты по количеству с накопленной
  долей и классами ABC: накопленная доля ≤ 0.80 → `A`, ≤ 0.95 → `B`, иначе `C`.
* **Динамика** (`app/analysis/trends.py`) — по одной точке на каждый месяц
  периода; месяцы без данных получают `defect_rate = None`.
* **SPC** (`app/analysis/spc.py`) — карты X̄ и R по подгруппам (все измерения
  одного акта по одному параметру). Константы ГОСТ Р ИСО 7870-2 / AIAG SPC для
  n = 2..10, модальная `n` задаёт `d2`/`D3`/`D4`/`A2`. Меньше 10 включённых
  подгрупп — границы не считаются. Нарушения: выход за границы и правило
  «2 из 3 в зоне A».

## Документация

* [`docs/contract.md`](docs/contract.md) — замороженный контракт: схема БД,
  формулы, маршруты, сигнатуры функций.
* [`docs/architecture.md`](docs/architecture.md) — слои B1/B2/B3, швы, поток
  данных.
* ADR: [0001](docs/adr/0001-manual-form-parsing.md) — ручной разбор форм,
  [0002](docs/adr/0002-tolerances-as-normative.md) — допуски как норматив,
  [0003](docs/adr/0003-server-side-svg.md) — серверный SVG,
  [0004](docs/adr/0004-sqlite-schema-no-migrations.md) — схема без миграций.
