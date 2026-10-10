# CSV Direct: Analysis DuckDB прямо из CSV тестера

Дата: 2026-10-10. Статус: реализовано в ветке `feat/csv-direct-analysis`, ожидает приёмки пользователем.
Решение: [ADR-0070](../decisions/0070-csv-direct-analysis-from-tester-csv.md).
Исходный пакет поставки (не контракт, только происхождение): локальный каталог
`.worktrees/csv-import-package-2026-10-10` вне git.

## Цель

Заменить цепочку `HTML → Source v6 DB → materialized surface → Analysis DuckDB` одной операцией
`reports_history.csv (+ run_snapshot) → Analysis DuckDB` в существующем формате fresh-анализа.
Shortlist, генератор READY JSON и всё дальше работают без изменений.

Основание: тестер исправил `TotalTrades` — теперь это число завершённых позиций, а не исполнений
ордеров. На реальном прогоне `Input/test1_new2` (16 запусков, частичные исполнения) `TotalTrades`,
`Win`, `Los` из CSV совпали с round trips, которые Source v6 восстанавливает из HTML, во всех 16
запусках. Кроме того, HTML новой версии тестера строгий парсер Source v6 уже не читает: в нём
повторяются метрики и есть таблицы с объединёнными ячейками.

## Non-goals

- Не менять `fresh_shortlist.py`, `fresh_analysis_strategies.py`, `strategy_json.py`, их HTTP-API и
  формат READY JSON.
- Не удалять маршрут Source v6: он остаётся для отката и для старых отчётов.
- Не синтезировать PRETEST A/B: в CSV нет поточечной истории сделок.
- Не склеивать разные окна отчётов: один CSV-анализ — одно окно `StartDate..EndDate`.
- Не принимать отчёты с не-фиксированным лотом (так же, как Source v6).

## Вход

- Один или несколько `reports_history*.csv` (файл, ZIP или каталог); HTML в ZIP не читается.
- `run_snapshot_*.json` — тип/источник MA (их нет в CSV) и `basic.use_fix`, `exchange.use_upnl`.
  Snapshot описывает только CSV своего прогона: той же папки (или ближайшей родительской) на диске
  или внутри ZIP, в пределах выбранного входа.
  Если выбран одиночный CSV, читается snapshot из его папки. Колонки `settings[*].basic.use_fix` /
  `settings[*].exchange.use_upnl`, если перебирались, переопределяют шаблон. Панель разрешает
  применять единственный MA-шаблон прогона к другим символам этого же прогона (оптимизатор
  тестера перебирает символы по одному шаблону), но никогда — к другому прогону.
- Реестр дат листинга (`panel_workflow.listing_dates_path`) и действующий `AlgorithmConfig`.

Обязательные колонки CSV: `settings[*].basic.symbol`, `settings[*].basic.time_frame`,
`StartDate`, `EndDate`, `InitialBalance`, `TotalPnL`, `TotalPnLPercent`, `MaxDrawdownPercent`,
`TotalTrades`, `Win`, `Los`, `WinRate`, `Fills`, `ProfitFactor`, ровно одна активная сторона
`settings[*].mrs2.ma_{long|short}.multiplier` с её `len` и `ma_close_*.len`. `PairsCount`, если есть,
должен быть 1.

## Метрики точки (определения Source v6)

| Поле analysis-row | Источник |
| --- | --- |
| `point_id` | `symbol|SIDE|tf|shift_bp|openType|openSrc|openLen|closeType|closeSrc|closeLen` |
| `pnl_pct` | `TotalPnL / InitialBalance × 100`; сверка с `TotalPnLPercent` (±0.0101) |
| `dd_pct` | `MaxDrawdownPercent` (просадка equity от пика) |
| `trades`, `wins`, `losses` | `TotalTrades`, `Win`, `Los`; допускается `Win+Los ≤ TotalTrades` (сделки в ноль) |
| `win_rate_pct` | `Win / (Win+Los) × 100`; `WinRate` тестера сверяется с этим или с `Win/TotalTrades` |
| `profit_factor` | `ProfitFactor`, пусто/`n/a` → `null` |
| `point_event_count`, `_event_ids`, `event_ids_hash` | `TotalTrades` локальных count-slot идентификаторов точки |
| `events_last_30d` | точный `Decimal`: `TotalTrades × 30 / max(EffectiveDays, 30)` |
| `report_start/end`, `listing_date` | окно CSV и реестр листинга |

`EffectiveDays = (EndDate − max(StartDate, listing_date)) / 86400 > 0`.

Отличие от Source v6 только одно и вынужденное: v6 считал `events_last_30d` как число сделок за
последние 30 дней окна, а у CSV нет времени сделок, поэтому используется средняя частота за 30 дней.
Потолок `TotalTrades` воспроизводит поведение v6 для истории короче 30 дней. Движок суммирует
точные значения по плато и один раз берёт floor (`plateau_event_count`). Для целого порога `T`
это даёт `floor(Σ) ≥ T ⇔ Σ ≥ T`.

## Анализ

Каждый scope `symbol|SIDE|timeframe` проходит тот же `_analyze_points()` в режиме
`real_independent_events` и ту же проекцию `source_v6_analysis_fresh._frames()`. Поэтому действуют
все пороги: `min_point_events`, `min_plateau_points`, `min_plateau_events_per_month`,
многоордерный допуск и ролевой отбор BASE. Единственное изменение движка — `pipeline.py` принимает
`Decimal` в `events_last_30d`. Для целых значений (Source v6) поведение не изменилось.

## Выход

Один неизменяемый `*.analysis-v6.duckdb` (staging → readback → atomic rename через
`source_v6_analysis_fresh._publish`). Девять таблиц, как у Source v6. Manifest:

- `fingerprint=analysis-v6-fresh-compact-v1`, `surface_fingerprint=surface-v6-fresh-compact-v2`
  (контракт v1 неизменённого reader: PRETEST необязателен);
- `event_mode=real_independent_events`, без `build_mode` и `analysis_input_digest`;
- `surface_id=csv-direct-<digest32>` — виртуальная content-addressed метка, файла surface нет;
- `scope_digests` — хэш нормализованных точек каждого scope отдельно;
- `csv_direct` — контракты входа/сделок/активности, `pretest_ab_available=false`, SHA-256 CSV, окно;
- `analysis_id` — SHA-256 канонического manifest без `analysis_id`.

Source v6 DB и surface не создаются.

## Панель

`POST /api/v2/strategies/fresh/analyze-csv`, тело `{"source_path", "target_path"?, "algorithm_version"?}`.
`source_path` — существующий CSV, ZIP или каталог, не symlink. `target_path` — каталог или имя
`*.analysis-v6.duckdb`; относительное значение отсчитывается от `analysis_db_root`, результат
обязан лежать внутри него. Явное имя никогда не перезаписывается. Без `target_path` имя —
`csv-<analysis_id[:24]>.analysis-v6.duckdb`: повторный запуск с тем же CSV, конфигом, версией и
листингом возвращает уже опубликованную базу, а при чужом файле с тем же именем добавляется
суффикс `-2`, `-3`. `algorithm_version`, который разбирается как JSON, отклоняется:
reader manifest декодирует любое значение, разбираемое как JSON (включая строку в кавычках), и хэш `analysis_id` перестал бы сходиться. Ответ:
`{phase:"COMMITTED", analysis_run_id, scope_count, point_count, analysis_path}`. Ошибки — коды
`CSV_DIRECT_*` с русским текстом; ошибки входа сохраняют деталь (файл относительно выбранного
входа, строка, причина) без локальных путей. Анализ регистрируется в сессии панели, дальше работают штатные shortlist,
open, catalog и generate.

Ответ `/api/v2/strategies/fresh/shortlist` дополнительно содержит `pretest_ab_available`. Для
v1/CSV-анализа это `false`, и UI снимает и блокирует галочку A/B с пометкой «недоступен». Если
включить A/B напрямую через API, неизменённый shortlist отказывает (`PRETEST_AB_EVIDENCE_UNAVAILABLE`).

CLI: `python -m mrs3.csv_direct_cli` — проверка входа и сводка; `python -m mrs3.csv_direct_analysis_cli`
— публикация, `--dry-run` без записи.

## Инварианты

1. Для одинаковых метрик точек кадры CSV Direct идентичны кадрам Source v6 (кроме `pretest_ab`
   и признаков, производных от event id).
2. CSV Direct никогда не использует `legacy_trades_proxy` и не смешивает режимы событий.
3. `countslot:*` — порядковые метки внутри точки; их нельзя использовать для дедупликации или
   как время сделки.
4. Ошибки входа (неполная прямоугольная сетка, несколько окон, дубликаты, не фиксированный лот,
   нет листинга, несогласованные числа) не создают файл.

## Acceptance evidence (2026-10-10)

- `tests/test_csv_direct_analysis.py::test_csv_direct_frames_equal_source_v6_analysis_frames` —
  синтетическая каноническая сетка: кадры CSV Direct равны кадрам `_analysis_frame_row` Source v6.
- Реальная эталонная база `MARA_ZM_2026-07-18_2026-09-11` (10 944 точки, 16 scope): точки
  пересобраны в CSV и прогнаны обоими путями с текущим конфигом. Точные метрики дают идентичные
  кадры во всех 16 scope и 6 таблицах. После округления, как у тестера (2 знака), меняются
  внутренние величины в 10 scope, но READY shortlist совпадает 36/36. Код исходного пакета
  (legacy proxy) на тех же данных дал 19 READY.
- Реальный прогон `Input/test1_new2`: 16/16 запусков — `TotalTrades/Win/Los` CSV равны round
  trips Source v6 из HTML; PnL% ±0.0002, DD% в пределах округления CSV.
- Тесты: `test_csv_direct_input`, `test_csv_legacy_compat`, `test_csv_direct_analysis`,
  `test_csv_direct_duckdb_runtime`, `test_panel_csv_direct_e2e`, `test_pipeline`; полный набор —
  в `progress.md`.

## Ограничения

- Точность `MaxDrawdownPercent` и `WinRate` в CSV — 2 знака. На MARA/ZM это не изменило READY, но
  может сдвинуть пограничные случаи. Желательно, чтобы тестер выводил ≥ 6 знаков.
- Нет PRETEST A/B; фильтр A/B для таких баз недоступен.
- Эндпоинт синхронный и однопроцессный; на полном каноническом скане проверить время.
- Не проверено: полный реальный канонический скан нового тестера через панель и клики в браузере.
