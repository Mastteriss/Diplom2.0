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
packaging/
  build.ps1              сборка PyInstaller onedir
  build_installer.ps1    сборка Inno Setup 6 установщика
  diplom.spec            спецификация PyInstaller
  installer.iss          скрипт Inno Setup 6
  run_diplom.py          лаунчер замороженной сборки
  install.ps1            fallback-установщик (PowerShell)
  uninstall.ps1          fallback-деинсталлятор (PowerShell)
  README.md              заметки по упаковке (инструменты)
  DiplomQuality-Setup-*.exe  собранный установщик (при наличии)
tests/                   тесты pytest (5 файлов)
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

Тесты (5 файлов в `tests/`):

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Последний запуск: `134 passed, 1 warning`. Тестовые базы данных складываются
в `tests_artifacts/` и удаляются при teardown (контракт §9); поставляемая БД
`data/quality.sqlite3` при этом открывается read-only.

## Сборка и установка (Windows)

### Сборка бандла (PyInstaller onedir)

Из корня проекта:

```powershell
.\packaging\build.ps1
```

Скрипт собирает замороженный бандл в `packaging\dist\DiplomQuality\DiplomQuality.exe`.
При необходимости устанавливает PyInstaller в `.venv`.

### Сборка установщика (Inno Setup 6)

Требуется [Inno Setup 6](https://jrsoftware.org/isinfo.php). Скрипт читает `APP_VERSION`
из `app.config` и автоматически запускает сборку бандла при его отсутствии.

```powershell
.\packaging\build_installer.ps1
```

Результат: `packaging\DiplomQuality-Setup-<версия>.exe` (например, `DiplomQuality-Setup-1.0.0.exe`).

### Установка

Запустите собранный установщик (`DiplomQuality-Setup-*.exe`). Установка **персональная**
(без прав администратора), по умолчанию в `%LOCALAPPDATA%\Programs\DiplomQuality`.
Создаётся ярлык в меню «Пуск» («ИС анализа качества продукции»), при желании — на рабочем столе
(задача `desktopicon` не отмечена по умолчанию). После установки можно запустить приложение
из меню «Пуск» или с рабочего стола.

### Где живут пользовательские данные

Пользовательские данные **никогда** не кладутся в каталог установки. Они находятся в
`%LOCALAPPDATA%\DiplomQuality\data\quality.sqlite3`. При первом запуске `packaging\run_diplom.py`
(он же используется в замороженной сборке) при отсутствии файла БД копирует из ресурсов
встроенный демо-файл `data/quality.sqlite3` в этот каталог.

### Обновление

Обновление выполняется механически: увеличьте `APP_VERSION` в `app/config.py` (единственный
источник версии), пересоберите бандл и установщик, запустите **новый** `DiplomQuality-Setup-*.exe`
поверх старого. Установщик использует фиксированный `AppId`, поэтому файлы программы заменяются,
а каталог `%LOCALAPPDATA%\DiplomQuality\data` остаётся нетронутым.

### Удаление

При удалении через установщик (Inno Setup) предлагается вопрос: удалить ли пользовательские данные.
По умолчанию — **Нет** (данные сохраняются). Если ответить «Да», каталог
`%LOCALAPPDATA%\DiplomQuality\data` будет удалён.

### Fallback: PowerShell-установщик без Inno Setup

Если Inno Setup 6 недоступен:

```powershell
.\packaging\install.ps1 [-InstallDir "путь"]
```

Копирует `packaging\dist\DiplomQuality\*` в `%LOCALAPPDATA%\Programs\DiplomQuality` (по умолчанию)
и создаёт ярлык в меню «Пуск». Повторный запуск — обновление (перезаписывает файлы программы),
пользовательские данные в `%LOCALAPPDATA%\DiplomQuality\data` не трогает.

Удаление:

```powershell
.\packaging\uninstall.ps1 [-PurgeData] [-InstallDir "путь"]
```

По умолчанию данные сохраняются; с `-PurgeData` — удаляются.

### Лаунчер замороженной сборки

`packaging\run_diplom.py` принимает флаги:

- `--host` (по умолчанию `127.0.0.1`)
- `--port` (по умолчанию `8000`)
- `--no-browser` (не открывать браузер автоматически)
- `--version` (напечатать версию и выйти)
- `--data-dir DIR` (задать каталог файла БД; файл будет `DIR\quality.sqlite3`)
- `--remote` (защищённый режим: включает вход по ключу; если ключ не задан,
  генерирует его через `secrets.token_urlsafe(24)` и печатает)
- `--access-key KEY` (явный ключ доступа)
- `--tunnel` (запустить быстрый туннель Cloudflare; требует ключ доступа)

## Удалённый доступ (тоннель Cloudflare)

По умолчанию приложение слушает `127.0.0.1` и доступно только на этом
компьютере. Чтобы открыть его с другой машины или из интернета (демонстрация,
филиал), лаунчер умеет поднимать быстрый туннель Cloudflare и защищать вход
ключом доступа.

Запуск из корня проекта (PowerShell):

```powershell
.\.venv\Scripts\python.exe packaging\run_diplom.py --remote --tunnel
```

В замороженной сборке — тот же набор флагов у `DiplomQuality.exe`:

```powershell
& "$env:LOCALAPPDATA\Programs\DiplomQuality\DiplomQuality.exe" --remote --tunnel
```

Порядок действий:

1. `--remote` включает защищённый режим и, если ключ не задан, генерирует его.
   Лаунчер печатает строку `Ключ доступа: ...` — скопируйте её.
2. `--tunnel` запускает `cloudflared tunnel --url http://127.0.0.1:<port>` и
   печатает строку `Публичный адрес: https://<...>.trycloudflare.com` — откройте
   этот адрес.
3. На странице `/login` введите ключ доступа. Сессия хранится в cookie
   `dipl2_session` (30 суток).

Учётная запись Cloudflare не нужна: быстрый туннель анонимный. Требуется
`cloudflared` в `PATH`; если его нет, лаунчер напечатает инструкцию по установке
и продолжит работу без туннеля.

> **Не поднимайте туннель без ключа.** `--tunnel` без ключа доступа (`--remote`,
> `--access-key` или `DIPL2_ACCESS_KEY`) отклоняется с кодом выхода 2: публичный
> адрес без защиты открыл бы систему всем.

Ключ можно задать заранее переменной окружения `DIPL2_ACCESS_KEY` — тогда
достаточно `--tunnel` (см. ниже).

## Переменная окружения DIPL2_DB

Путь к файлу базы данных. Относительный путь разрешается от корня проекта
(`PROJECT_DIR`) вне замороженной сборки; в замороженной сборке относительный путь
разрешается от родителя каталога данных (`%LOCALAPPDATA%\DiplomQuality`).
Значение по умолчанию: вне сборки — `PROJECT_DIR / "data" / "quality.sqlite3"`,
в замороженной сборке — `%LOCALAPPDATA%\DiplomQuality\data\quality.sqlite3`.
Путь вычисляет функция `resolve_db_path()` в `app/config.py`.

```powershell
$env:DIPL2_DB = "data/other.sqlite3"
```

## Переменная окружения DIPL2_ACCESS_KEY

Ключ доступа к защищённому режиму. Если переменная задана и непуста, приложение
требует вход по ключу; если не задана или пуста — защита выключена и поведение
прежнее. Значение читает функция `get_access_key()` в `app/config.py`
(константа `ACCESS_KEY_ENV`).

```powershell
$env:DIPL2_ACCESS_KEY = "пример-сгенерированного-ключа"
```

Лаунчер выставляет эту переменную сам при `--remote`/`--access-key`; ключ,
унаследованный из окружения, в баннере не печатается.

## Маршруты

Все пути корневые, без префиксов роутеров.

| Метод | Путь | Что делает |
|-------|------|------------|
| GET | `/` | Главная: KPI-карточки, топ-5 дефектов, ссылки на разделы |
| GET | `/login` | Форма входа (в защищённом режиме; иначе 303 на `/`) |
| POST | `/login` | Проверка ключа: верный → 303 на `/` и cookie `dipl2_session`, неверный → 401 |
| GET | `/logout` | Удаляет cookie сессии, 303 на `/login` |
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
  [0004](docs/adr/0004-sqlite-schema-no-migrations.md) — схема без миграций,
  [0005](docs/adr/0005-windows-packaging-and-updates.md) — Windows-упаковка и обновления,
  [0006](docs/adr/0006-remote-access-and-access-key.md) — удалённый доступ и ключ доступа.
