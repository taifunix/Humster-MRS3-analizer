# Humster MRS3 Analyzer — PRD v0.7

## Продукт

Humster MRS3 Analyzer — локальный, детерминированный pipeline для перехода от результатов MRS2 к проверяемым кандидатам MRS3. Он нормализует входные точки, применяет правила устойчивости/пригодности, строит 1ORD и 2–4ORD структуры, выпускает валидированные strategy JSON, запускает batch в Hamster Bot Tester и сравнивает реальные результаты после tick-test.

**Текущий статус:** код v0.6 перенесён в корневой пакет как baseline; продуктовая работа начинается с v0.7. Реальная эффективность MRS3 пока не доказана: до завершения materialization и tick-tests любые source-метрики — только диагностика.

## Пользовательский результат

Для одного сравнимого периода и одной стороны рынка пользователь получает:

1. audit происхождения каждой MRS2-точки и причины каждого исключения;
2. воспроизводимые READY структуры 1ORD/2ORD/3ORD/4ORD с EQUAL и INCOME lot variants;
3. JSON, технически валидные для тестера;
4. результаты реального tick-test, DD5-retest и individual ranking;
5. только после накопления результатов — калиброванный безопасный pre-test potential filter.

## Текущий этап: v0.7 DuckDB analysis storage and importer

Цель ближайшего этапа — сохранить source DuckDB единым пополняемым lossless-хранилищем HTML-отчётов, перенести управление импортом в веб-панель и материализовывать воспроизводимые поверхности для анализа плато в отдельную append-only analysis DuckDB. Анализ напрямую из source DuckDB сначала публикует неизменяемую поверхность, а затем запускает общий plateau pipeline. Контракт зафиксирован в [спецификации DuckDB analysis storage and importer](docs/specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md).

Этап реализован и проверен: source schema v5, управляемый импорт, immutable
analysis surfaces, повторный plateau-анализ, lineage, библиотека результатов и
детерминированные экспорты доступны из общего v0.7 контура. Это завершает
инфраструктуру анализа, но не доказывает доходность готовых MRS3-стратегий.

Соединение CSV с DuckDB не входит в обязательную поставку и не блокирует этот этап. Оно вынесено в отдельное [необязательное ТЗ CSV-DuckDB overlay](docs/specs/2026-08-11-v07-optional-csv-duckdb-overlay.md) со статусом **Optional / Deferred**. Существующие [event source packs](docs/specs/2026-08-10-v07-event-source-packs.md), [ADR-0002](docs/decisions/0002-source-summary-and-window-metrics-verification.md) и [ADR-0003](docs/decisions/0003-source-integrity-action-metrics.md) остаются диагностическими зависимостями.

### Этапы поставки

| № | Результат | Входной критерий | Выходной критерий |
| --- | --- | --- | --- |
| 0 | Репозиторий v0.7 | перенесён baseline | root package, tests, docs и Git готовы |
| 1 | Проверенный v4 import | база и audit доступны | schema v4, manifest, quarantine/checklist проверены |
| 2 | Source packages | CSV и raw payloads v4 | один declared event mode, window и audit на пакет |
| 3 | DuckDB materializer | raw payloads v4 | closed cycles, exclusions и `point_period_metrics` |
| 4 | Selector v0.7 | ровно один source package | event gate, full rebuild, audit и JSON |
| 5 | Реальные MRS3 results | READY JSON | raw + DD5 retests и individual ranking |
| 6 | Source-potential calibration | достаточная пачка results | LOPO-validated optional cap |

## Границы и safety rules

- Не удалять raw HTML до подтверждённого v4 audit и `safe_to_delete=YES`.
- Не использовать `len(raw actions)` как `TotalTrades` без reconciliation.
- Не смешивать `legacy_trades_proxy` и `real_independent_events` в одном run.
- Не фильтровать готовые v0.6 structures задним числом: после event-filter пересобирать весь universe.
- Не объявлять `SourcePnLSum` потолком или прогнозом фактического MRS3 PnL без калибровки.
- Не выдавать individual ranking за portfolio simulation: для портфеля нужны time series equity/drawdown/occupancy/margin.

## Не входит в текущий scope

- портфельная симуляция и смешивание независимых событий с legacy proxy;
- реализация портфельного модуля до появления его обязательных входных данных;
- ML, regression score или per-pair/per-TF production thresholds;
- GitHub push, PR или публикация результатов без отдельного разрешения.

## Hook: Анализатор Портфеля

Отдельная команда может начать модуль по [спецификации Portfolio Analyzer v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md). Статус модуля — **Queued**, он не блокирует текущий v0.7 legacy selection.

Перед началом реализации команда должна подтвердить и записать в `progress.md` модуля: формат individual MRS3 results, журналы входов/выходов с timestamp, договорённости о limiter (позиции или заявки; LONG/SHORT; hedge/one-way), а также доступность/правила L2 и margin data. Без этого разрешён только аналитический Layer A; сетовый симулятор и финальные рекомендации не запускать.

## Реестр активной документации

| Статус | Документ | Назначение | Зависимости |
| --- | --- | --- | --- |
| Accepted | [Repository foundation](docs/specs/2026-08-10-mrs3-v07-repository-foundation.md) | структура репозитория и workflow | — |
| Active prerequisite | [Safe runner smoke-test](docs/specs/2026-08-10-v06-runner-safe-root-json-smoke.md) | безопасная проверка панели и одного реального прогона | локальный tester; до v0.7 implementation |
| Active | [v0.7 legacy selection](docs/specs/2026-08-10-v07-legacy-selection.md) | последовательность import → materializer → unified input → selector | v4 evidence, event-filter spec |
| Active | [v0.7 event source packs](docs/specs/2026-08-10-v07-event-source-packs.md) | CSV/DuckDB пакеты, event modes и closed-cycle audit | v4 evidence, event-filter spec |
| Implemented / Verified | [v0.7 DuckDB analysis storage and importer](docs/specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md) | единый source DuckDB, импорт из панели, analysis DuckDB и plateau lineage | event source packs, event-filter spec |
| Implemented / verified on production archive | [Trusted v4 migration performance](docs/specs/2026-08-11-v07-trusted-v4-migration-performance.md) | bounded v4-to-v5 production migration | DuckDB analysis storage |
| Optional / Deferred | [v0.7 CSV-DuckDB overlay](docs/specs/2026-08-11-v07-optional-csv-duckdb-overlay.md) | необязательное объединение CSV coarse-grid и DuckDB fine-grid | DuckDB analysis storage, event-filter spec |
| Accepted | [ADR-0002](docs/decisions/0002-source-summary-and-window-metrics-verification.md) | раздельная full-horizon/windowed verification для real packages v2 | event source packs |
| Active dependency | [Event filter and shortlist](docs/specs/v07-event-filter-and-shortlist.md) | правила `PointEventCount`, representative и shortlist | unified input |
| Planned | [Source-potential calibration](docs/specs/v07-posttest-calibration-source-potential.md) | empirical cap без leakage | завершённые tick-tests |
| Queued — hook «Анализатор Портфеля» | [Portfolio Analyzer v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md) | отдельный анализ готовых MRS3-стратегий и сетов | individual results, trade timestamps, limiter/L2/margin contract |
| Accepted | [ADR-0001](docs/decisions/0001-repository-and-documentation-model.md) | root v0.7 и модель документации | — |

Полная навигация: [docs/README.md](docs/README.md). Оперативная точка: [progress.md](progress.md).
## Task 4 Status: Pending Production Acceptance

Strategy-performance DuckDB DD5 is implemented as a calculation-only path from committed imports, pending production acceptance. It preserves the legacy CSV posttest workflow and requires complete inbox evidence before panel execution.

| Pending production acceptance | [Strategy performance DuckDB governing spec](docs/specs/2026-08-14-strategy-performance-duckdb.md) | immutable inbox, transactional evidence store and calculation-only DD5 | [ADR-0004](docs/decisions/0004-strategy-performance-evidence-store.md) |
