# Humster MRS3 Analyzer

Инструменты для воспроизводимого отбора, генерации, тестирования и аудита кандидатов MRS3 mean-reversion strategy.

> Статус: идёт переход к v0.7. Код уже запускается и покрыт тестами, но production legacy-run ещё ожидает подтверждённые результаты v4 DuckDB-импорта.

## Что делает проект

```text
MRS2 reports → нормализованные точки → plateaus / structures → JSON кандидатов
           → Hamster Bot Tester → raw results → DD5 retest → individual comparison
```

Pipeline сохраняет traceability от каждого JSON до MRS2-точек и не выдаёт source-метрики за фактическую доходность MRS3 до реального теста.

## Быстрый старт

Нужен Python 3.11+ и локальный Hamster Bot Tester.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config.local.json.example config.local.json
.\.venv\Scripts\python.exe -m pytest -q
```

В `config.local.json` задайте локальный `tester_runner.bot_root` и
`trade_store.root`. Для импорта результатов тик-теста в PerformanceDB добавьте
ваш private listing-date workbook в `input/dates.xlsx` (или укажите другой путь в
`panel_workflow.listing_dates_path`). Файл игнорируется Git и не должен
публиковаться. Без этого входа импорт не начнётся и панель явно сообщит причину.

Панель состоит из трёх экранов: «Пары и стратегии», «Портфели» и
«Настройки». Общее число рабочих процессов в настройках влияет на скорость
импорта, но не на результаты.

На экране «Пары и стратегии» есть:
- переключатель биржи;
- действия «Синхронизировать», «Пересчитать историю», «Пересобрать READY»,
  «Проверить тестером», «Тик-тест MRS3» и «Ретест финалистов»; у каждого,
  кроме синхронизации, есть диалог плана с отметкой строк;
- область задачи: прогресс, оставшееся время, ошибки, кнопка «Стоп»;
- виды реестра пар (Пары, Справочник, Ликвидность, Изменения): сортировка,
  фильтр, выбор столбцов и выгрузка в Excel;
- карточка пары, где записываются решения и личные пороги;
- вид «Стратегии»: структуры READY с метриками тик-теста, жёсткие фильтры,
  отметки финалистов (хранятся в реестре пар) и Excel с загрузкой отметок;
- ниже — тик-тест с импортом в PerformanceDB и две карточки ретеста;
  обслуживание PerformanceDB — на экране «Настройки».

## Основные команды

```powershell
# Справка по доступным командам
.\.venv\Scripts\python.exe -m mrs3.cli --help

# Локальная панель управления
.\scripts\start_panel.bat

# Та же панель на порту 8766
.\scripts\start_new_panel.bat
.\scripts\stop_new_panel.bat

# Построение кандидатов
.\.venv\Scripts\python.exe -m mrs3.cli select --help

# Read-only DuckDB source-pack с опциональной HTML-верификацией (3–5 образцов)
.\.venv\Scripts\python.exe -m mrs3.cli source-duckdb --database <reports.duckdb> --start <UTC_START> --end <UTC_END> --output-dir <source-package> --verify-html-root <html-root> --verification-sample-count 3 --config config.local.json

# Проверка batch без изменения тестера / запуск batch
.\.venv\Scripts\python.exe -m mrs3.cli tester-plan --help
.\.venv\Scripts\python.exe -m mrs3.cli tester-run --help

# Bybit base-lot values in the Actual sheet of the liquidity workbook (from the pair registry;
# --source minute-files keeps the old seven-calendar-day model). A sheet never mixes the two models:
# switching a sheet needs past registry windows and one full rebuild
.\.venv\Scripts\python.exe -m mrs3.pair_registry liquidity --days 14
.\scripts\calculate_bybit_base_lots.cmd --rebuild-history
.\scripts\calculate_bybit_base_lots.cmd
# One-time rebuild of all ten dated columns after interrupted/partial initialization
.\scripts\calculate_bybit_base_lots.cmd --rebuild-history

# Pair registry: refresh, liquidity, pipeline and the listing workbook (sheets «Пары», «Пары_BingX»)
.\.venv\Scripts\python.exe -m mrs3.pair_registry sync
# Git sync between machines: once per machine; --remote is a private repository you create yourself
# (with GitHub CLI installed and logged in, the sync repository uses its credentials)
.\.venv\Scripts\python.exe -m mrs3.pair_registry sync-init --remote <URL>
.\.venv\Scripts\python.exe -m mrs3.pair_registry workbook --out <path.xlsx>

# MRS2 history in the trade store (trade_store.root in config.local.json)
.\.venv\Scripts\python.exe scripts\simulate_history.py --pairs XOMUSDT,SNOWUSDT
.\.venv\Scripts\python.exe scripts\tester_jobs.py plan --pairs XOMUSDT
.\.venv\Scripts\python.exe scripts\tester_jobs.py run --limit 1
.\.venv\Scripts\python.exe scripts\tester_jobs.py import --pairs XOMUSDT
.\.venv\Scripts\python.exe scripts\ready_from_store.py --out <new folder> --latest-source simulator
```

Хранилище MRS2 ([спецификация](docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md))
держит историю пар по 20-дневным периодам: сделки, часовой маржин-баланс и
точные метрики каждой точки сетки, посчитанные симулятором на минутных свечах
тестера (недостающие дни докачиваются). Последний период можно проверить
настоящим тестером на отобранных точках: исполнитель подменяет конфиг и
стратегию тестера только на время прогона и восстанавливает их побайтно.
В панели эти действия запускаются с экрана «Пары и стратегии». READY сам
берёт результаты тестера для последнего периода, если они импортированы.
Пороги READY v4 из хранилища предварительные: их нужно перекалибровать на
данных хранилища до использования в продакшене.

В панели источники MRS2 разделены на CSV и DuckDB. На вкладке «Кандидаты
стратегий» выберите ровно один вход: проверенный `source-pack` или
совместимый raw CSV. Портфельный раздел пока информационный: симулятор и
рекомендации недоступны до подтверждения входных контрактов.

Во вкладке DuckDB HTML-каталог верификации необязателен: без него пакет
остаётся audit-only и не может быть передан селектору. При указанном каталоге
выберите от 3 до 5 HTML-образцов; путь используется только во время локального
запуска и не сохраняется в package manifest.

Проверенный workflow импорта DuckDB в панели: в Settings задайте
`source_duckdb_path`, `analysis_duckdb_path`, каталоги HTML/audit, число workers
и batch size, затем выполните preflight перед Start. Задание можно отменить;
панель показывает шесть счётчиков `parsed`, `inserted`, `replaced`, `identical`,
`ambiguous` и `quarantined`. Миграция создаёт и проверяет новый файл вне места
активного источника и активирует его только после успешной валидации, сохраняя
параллельные изменения Settings. `safe_to_delete=YES` появляется лишь после
проверки обоих артефактов; панель сама HTML не удаляет.

Перед запуском на production данных прочитайте активную спецификацию: [v0.7 legacy selection](docs/specs/2026-08-10-v07-legacy-selection.md).

Экраны A/B, Pareto и фильтров и карточка экспорта PerformanceDB удалены
(этап 4c переделки панели). Финалистов отмечают в виде «Стратегии», выгрузка —
его кнопка «Excel»; модули фильтров остаются как основа жёстких фильтров вида.

## Важные ограничения

- Не удаляйте HTML до успешного v4 import audit.
- Не смешивайте `legacy_trades_proxy` и real independent events.
- Сумма PnL исходных MRS2-ордеров не равна PnL готовой MRS3-стратегии.
- Реальное сравнение финалистов требует tick-test и DD5 retest.
- Портфельная симуляция пока вне scope: ей нужны временные ряды equity, drawdown, occupancy и margin.

## Документация и участие

- [PRD и реестр фич](PRD.md)
- [Текущая проверенная точка](progress.md)
- [Правила для агентов и Claude Code](AGENTS.md), [CLAUDE.md](CLAUDE.md)
- [Навигация по документации](docs/README.md)

Внутренние правила требуют отдельного agent-review перед каждым коммитом. Не добавляйте raw HTML, DuckDB, локальную конфигурацию или результаты тестера в Git.
