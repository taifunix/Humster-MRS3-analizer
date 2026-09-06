# ADR-0030: admission, leverage, liquidity and individual-DD sizing contract

**Дата:** 2026-09-06

**Статус:** Accepted — independent Opus D6 review: `PLAN_APPROVED`.

## Контекст

M0 и fixture-only M1 уже приняты. Перед M2 необходимо убрать неоднозначности
между PerformanceDB, collector, exchange reference и последующим M4 search.
Эта запись не разрешает runtime, tester run, trading admission или live use.

## Решение

1. Единственный MVP-universe — замороженные строки с точным `User Status =
   FINALIST`. `RESERVE`, manual candidates, missing и любой иной status не имеют
   fallback.
2. Historical leverage individual PerformanceDB result — только provenance.
   Planned leverage всегда maximum valid current exchange value, полученный после
   определения notional/exposure варианта и его risk tier. Оно не является осью
   размера лота. Tier/граница, которую нельзя определить устойчиво, блокирует
   variant. Для одного symbol применяется одно symbol-level planned leverage;
   conflicting requirements не разрешаются молча. Если applied leverage joint
   TradingRun не прочитан для любого symbol либо не совпал с manifest, весь run
   получает `NEEDS_RETEST` и не используется в scheduling, ranking или
   recommendation.
3. M2 вычисляет и сохраняет per Strategy ID/direction
   `liquidity_scalar_pct_max`: minimum ceiling всех position-opening grid,
   averaging и DCA orders; closing/reduce-only orders туда не входят. Evidence
   включает facts/window/digest/source/timestamp/expiry. Stale, changed filters,
   tier или geometry и absent ceiling означают `UNKNOWN`, не last-known-good.
   Ceiling использует published minute depth distribution за последние семь
   полностью завершённых UTC-суток, а не мгновенный order-book snapshot; обычные
   weekend/off-session провалы ликвидности тем самым входят в статистику.
4. Profile получает отдельное OPEN-POLICY поле `each_strategy_max_dd_pct`. Для
   `scalar_pct=100` равного historical individual test lot, positive historical
   maximum DD amount `D100`, positive current planning/live portfolio equity `E`
   и cap `c` в percentage points:

   ```text
   estimated_individual_dd_amount = D100 * scalar_pct / 100
   estimated_individual_dd_pct = estimated_individual_dd_amount / E * 100
   dd_scalar_pct_max = c * E / D100
   ```

   `D100` и `E` обязаны иметь одну settlement currency; implicit FX conversion
   запрещён. Missing, non-positive, currency mismatch или stale `D100`/`E` дают
   `UNKNOWN`; missing `c` даёт `OPEN_POLICY`. Это линейная fixed-lot,
   non-compounding planning assumption, не actual joint DD. Gate независим для
   каждой стратегии, не суммируется и ограничивает только new/upsized orders.
   `E` snapshot, timestamp, provenance, cap и вычисленный ceiling сохраняются;
   fresh `E` требуется при каждом new/upsized order. `max_balance` ограничивает
   только lot-sizing base, не `E`.
5. M4 выбирает scalar как minimum всех applicable liquidity/DD/margin/exchange
   ceilings, округляет quantity вниз и повторно проверяет geometry. Below minimum
   отклоняется, никогда не увеличивается до minimum. До tester проходят только
   finite variants, прошедшие structural → liquidity → margin → individual-DD.
   Они упорядочиваются только для расходования test budget по
   `estimated_individual_net_pnl_at_selected_scalar / worst_calculated_initial_margin_requirement`:
   denominator должен быть positive/known, negative numerator допустим, tie-break
   — canonical identity. Это scheduling key, а не portfolio result/ranking/
   recommendation; grid, max variant count и порядок versioned и frozen.
6. `portfolio_reason_v1` неизменяем и читаем. Каждая причина хранит
   `reason_enum_version`. Будущий `portfolio_reason_v2` добавляет
   `INDIVIDUAL_DD_UNAVAILABLE`, `INDIVIDUAL_DD_LIMIT`, `LEVERAGE_UNVERIFIED`, не
   меняя значения v1. До отдельной M4-authorized writer implementation новые
   codes не выпускаются; readers принимают v1.

## Последствия

- Нельзя выдать individual historical PnL/DD или leverage за результат joint
  portfolio test.
- В D6 не вводятся численные `each_strategy_max_dd_pct`, PnL, liquidity,
  freshness или profile-ranking policies: они остаются open blockers.
- ADR-0030 дополняет ADR-0025 и ADR-0029; для перечисленных admission/sizing
  вопросов current spec D6 является нормативным контрактом. Старые ADR не
  переписываются задним числом.

## Проверка D6

Только docs-only diff; cross-links spec/plan/PRD/progress/AGENTS; отсутствие
нового численного DD cap; отсутствие runtime/tester/live изменений. M2/M3/M4
tests в плане — будущие acceptance descriptions, не evidence реализации.
