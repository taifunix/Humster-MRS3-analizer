# Humster MRS3 Analyzer — PRD

## Назначение

Humster MRS3 Analyzer строит воспроизводимые кандидаты MRS3 из MRS2-данных, запускает их через локальный Hamster Bot Tester и сохраняет проверяемый audit. Текущая продуктовая линия — v0.7; код v0.6 был перенесён как стартовая база и требует доработки.

## Текущая цель

Получить проверяемый v0.7 legacy-run на едином окне `2026-07-15T00:00:00` — `2026-08-06T00:00:00` (правая граница исключается): materialized fine HTML + совместимый coarse CSV, `event_mode=legacy_trades_proxy`, затем кандидаты для реальных MRS3 tick-тестов.

## Не входит в текущий scope

- смешивание real events fine-данных с `TotalTrades` coarse-данных;
- удаление HTML до подтверждённого импорта v4;
- объявление суммы source PnL фактическим PnL MRS3;
- портфельная симуляция без equity/drawdown/margin time series;
- ML или отдельные production-thresholds по pair/TF без подтверждённой калибровки.

## Реестр спецификаций

| Статус | Спецификация | Роль | Зависимости |
| --- | --- | --- | --- |
| Approved | [Repository foundation](docs/specs/2026-08-10-mrs3-v07-repository-foundation.md) | правила репозитория, документации и миграции | — |
| Active | [v0.7 legacy selection](docs/specs/2026-08-10-v07-legacy-selection.md) | импорт evidence → materializer → единый input → event-filter | master handoff, event-filter spec |
| Planned | [Source-potential calibration](docs/specs/v07-posttest-calibration-source-potential.md) | post-test empirical cap без leakage | завершённые MRS3 tick-tests |

## Принятые решения

- [ADR-0001](docs/decisions/0001-repository-and-documentation-model.md): v0.7 ведётся из корня; v0.6 — архивный источник.
- Тестер на этой машине настраивается только через ignored local config.
- Результаты каждого run хранят входные hashes, algorithm version, окно, event mode и source/dedup/conflict counts.

## Порядок поставки

1. Проверить результаты v4 DuckDB импорта.
2. Реализовать и сверить materializer общего периода.
3. Собрать и аудировать единый legacy input.
4. Доработать selector до v0.7 event-filter и полного rebuild.
5. Запустить MRS3 candidates и реальные DD5 retests.
6. Калибровать optional Source-PnL potential filter только по фактическим результатам.

Подробности каждой работы живут в отдельной спецификации, а не в этом PRD.
