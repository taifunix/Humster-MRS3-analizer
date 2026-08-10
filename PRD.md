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

## Текущий этап: v0.7 legacy selection

Цель ближайшего этапа — проверяемый `legacy_trades_proxy` run на окне `[2026-07-15T00:00:00, 2026-08-06T00:00:00)`. Fine HTML после materialization и coarse CSV должны иметь одинаковый window contract. Для всех строк этого запуска `point_event_count=TotalTrades`; real independent events в него не входят.

### Этапы поставки

| № | Результат | Входной критерий | Выходной критерий |
| --- | --- | --- | --- |
| 0 | Репозиторий v0.7 | перенесён baseline | root package, tests, docs и Git готовы |
| 1 | Проверенный v4 import | база и audit доступны | schema v4, manifest, quarantine/checklist проверены |
| 2 | Materializer | raw payloads v4 | `point_period_metrics` + reconciliation samples |
| 3 | Unified legacy input | verified fine + compatible coarse | deterministic dedup/shadow/conflict audit |
| 4 | Selector v0.7 | unified input | event gate, full rebuild, audit и JSON |
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

- real event-aware run до достаточного raw HTML coverage всей сетки;
- оптимизация портфеля, L2 capacity и margin simulator;
- ML, regression score или per-pair/per-TF production thresholds;
- GitHub push, PR или публикация результатов без отдельного разрешения.

Будущий портфельный контур описан в [архивной source-спецификации v0.4](docs/archive/sources/MRS3_Portfolio_Selector_v0_4.md). Он не активен, пока не появятся требуемые временные ряды и новая утверждённая спецификация.

## Реестр активной документации

| Статус | Документ | Назначение | Зависимости |
| --- | --- | --- | --- |
| Accepted | [Repository foundation](docs/specs/2026-08-10-mrs3-v07-repository-foundation.md) | структура репозитория и workflow | — |
| Active | [v0.7 legacy selection](docs/specs/2026-08-10-v07-legacy-selection.md) | последовательность import → materializer → unified input → selector | v4 evidence, event-filter spec |
| Active dependency | [Event filter and shortlist](docs/specs/v07-event-filter-and-shortlist.md) | правила `PointEventCount`, representative и shortlist | unified input |
| Planned | [Source-potential calibration](docs/specs/v07-posttest-calibration-source-potential.md) | empirical cap без leakage | завершённые tick-tests |
| Accepted | [ADR-0001](docs/decisions/0001-repository-and-documentation-model.md) | root v0.7 и модель документации | — |

Полная навигация: [docs/README.md](docs/README.md). Оперативная точка: [progress.md](progress.md).
