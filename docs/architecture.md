# Архитектура

Система разделена на три слоя (B1/B2/B3) по `docs/contract.md` §10. Слои
общаются через небольшое число явных швов; всё остальное — детали реализации.

## Слои

### B1 — данные

Отвечает за SQLite, справочники и генерацию демо-данных.

* `app/db/connection.py` — соединение и инициализация схемы (`connect()`,
  `get_connection()`, `init_db()`, `init_default_db()`, `SCHEMA_VERSION = 1`).
* `app/db/schema.sql` — идемпотентная схема (все объекты через `IF NOT EXISTS`).
* `app/db/repositories.py` — все запросы к БД: справочники, партии, акты,
  агрегаты для анализа.
* `app/db/seed.py` — посадка справочников (`seed_reference_data`).
* `scripts/generate_demo_data.py` — генератор демо-данных.
* `data/quality.sqlite3` — поставляемая база.

B1 не импортирует `app.analysis`; зависимости — только `app.config` и
`app.domain.models`.

### B2 — анализ

Чистые вычисления над доменными датаклассами, без обращений к БД.

* `app/analysis/quality_index.py` — индекс качества (`compute_one`,
  `compute_all`).
* `app/analysis/pareto.py` — Парето и ABC (`compute_pareto`).
* `app/analysis/trends.py` — динамика по месяцам (`compute_trends`).
* `app/analysis/spc.py` — контрольные карты X̄ и R (`compute_spc`).
* `app/analysis/charts.py` — построение SVG-строк (`pareto_chart`,
  `trend_chart`, `spc_chart`, `weight_bar`).

### B3 — веб

FastAPI-приложение, маршруты, шаблоны и статика.

* `app/main.py` — фабрика `create_app()`, middleware, обработчики ошибок,
  подключение роутеров.
* `app/web/context.py` — шов рендеринга, базовый контекст, пагинация, период,
  разбор формы.
* `app/web/deps.py` — подключение слоёв B1 и B2.
* `app/web/pages_*.py` — маршруты: `pages_index`, `pages_batches`,
  `pages_analysis`, `pages_config`, `pages_export`.
* `app/web/templates/` — Jinja2-шаблоны; `app/web/static/app.css` — стили.

### Общие модули контракта

`app/config.py`, `app/domain/models.py`, `app/db/schema.sql` и
`app/db/connection.py` используются всеми слоями и считаются частью контракта.

## Швы

### `render()` — шов рендеринга (B3)

`app/web/context.py::render(request, name, *, conn, active, page_title,
status_code=200, **context)` — единственная точка отдачи HTML. Каждый
обработчик страницы и оба обработчика ошибок в `app/main.py` вызывают его.
`render()` собирает `base_context()` (заголовок, меню, версия схемы, версия
весов, путь к БД) и передаёт управление `Jinja2Templates`. В `context.py`
зарегистрированы глобальные функции шаблонов `pareto_chart`, `trend_chart`,
`spc_chart`, `weight_bar` — графики вызываются прямо из разметки.

### `resolve_db_path()` — шов пути к БД (общий)

`app/config.py::resolve_db_path()` — единственная точка вычисления пути к БД:
читает `DIPL2_DB`, относительный путь разрешает от `PROJECT_DIR`, иначе
возвращает `DEFAULT_DB_PATH`. Его используют `app/db/connection.py`
(`get_connection`, `init_default_db`) и `app/web/context.py`
(`base_context`).

### `app/web/deps.py` — шов B1/B2 (B3)

Единственный модуль, через который веб-слой видит данные и анализ. Импортирует
`app.db.repositories` (B1) и модули `app.analysis.quality_index`, `pareto`,
`trends`, `spc`, `charts` (B2). Обработчики обращаются к ним как
`deps.repositories.*`, `deps.pareto.*` и т.д.

## Поток данных

Общая схема запроса:

```
HTTP-запрос
  → роутер (app/web/pages_*.py), первая строка conn = db_conn()
  → репозиторий (app/db/repositories.py) читает SQLite
  → анализ (app/analysis/*.py) считает результат
  → charts.py возвращает SVG-строку
  → render() (app/web/context.py) отдаёт Jinja2-шаблон
  → в шаблоне SVG вставляется через |safe
```

Пример для `GET /analysis/pareto`:

1. `app/web/pages_analysis.py::pareto_page` получает соединение `db_conn()`,
   разбирает `months` через `parse_months()` и вычисляет период
   `period_range()`.
2. `deps.repositories.load_pareto_counts(conn, since, until)` возвращает
   последовательность `DefectCount`.
3. `deps.pareto.compute_pareto(counts, months=months)` возвращает
   `ParetoResult`.
4. `render(..., "analysis_pareto.html", result=result, ...)` рендерит шаблон.
5. Шаблон вызывает `pareto_chart(result, width=980, height=430)|safe`; функция
   `app/analysis/charts.py::pareto_chart` возвращает готовую SVG-строку либо
   `<p class="chart-empty">Нет данных для графика</p>` при пустом входе.

Аналогично устроены `/analysis/trends` (`trends.compute_trends` +
`trend_chart`), `/analysis/spc` (`spc.compute_spc` + `spc_chart`) и блок весов
на `/config/weights` (`weight_bar`).
