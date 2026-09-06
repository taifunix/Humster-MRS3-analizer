# ADR-0029: Стартовые research risk-профили Portfolio Optimizer

**Дата:** 2026-09-06

**Статус:** Accepted — независимый Opus D5 review: `PLAN_APPROVED`.

**D5 review:** `PLAN_APPROVED`.

## Контекст

ADR-0025 и спецификация D4 описывают evidence и фазовую архитектуру
оптимизатора, но не задавали численные risk limits. Для исследования и
калибровки нужны исходные профильные bounds, без переноса их в разрешение
тестера, торговли или финальной рекомендации.

Этот ADR дополняет ADR-0025; он не supersede, не изменяет и не сужает ADR-0025
и не закрывает ни один из его D4 open-policy items.

## Решение

Предлагается принять `portfolio_optimizer_research_risk_v1`:

| Профиль | Максимальная actual equity DD | Минимальный расчётный запас свободной маржи | Максимальная расчётная account MM load |
| --- | ---: | ---: | ---: |
| `AGGRESSIVE` | 20% | 20% | 50% |
| `BALANCED` | 10% | 40% | 35% |
| `CONSERVATIVE` | 5% | 60% | 20% |

Профиль проходит research risk gate только при одновременном прохождении трёх
ограничений. DD — положительный maximum peak-to-trough percentage от running
peak по полной actual joint equity series без windowing, resampling или
synthetic/per-symbol substitute. Free-margin reserve и account MM load —
calculated guards по полному margin race envelope; каждый state использует
согласованный timestamp/currency snapshot. Missing, truncated, stale, invalid,
currency-mismatched или unevaluable input даёт `UNKNOWN`, не PASS.

Thresholds fixed per named profile: они не интерполируются, не ослабляются
автоматически, не смешиваются, не получают intermediate profile и не заменяются
другим/default profile или per-run override. Любое отклонение — policy violation,
не passing result. Изменение threshold, formula, verdict rule или profile set
создаёт новый policy ID и ADR; у исторической Campaign/Evaluation остаётся
исходный policy ID, который обязан быть записан в результате.

Прохождение research thresholds не разрешает implementation, tester run,
`RECOMMENDATION_READY`, trading admission или live use; все remaining gates
(PnL floor, liquidity/freshness limits, profile ranking) остаются open blockers.

## Последствия

Research output может однозначно указывать применённый risk policy ID и
результат трёх guards. Прохождение policy необходимо, но недостаточно для
рекомендации или торговли; остальные evidence, capabilities и отдельные
разрешения сохраняются.
