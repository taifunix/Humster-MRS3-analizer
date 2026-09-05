# Performance v2 — handoff: отбор финалистов

## Цель следующего v2-среза

Довести Performance v2 от готового импорта и ручного A/B-сравнения до
воспроизводимого **отбора финалистов**: фиксированный набор рассчитанных
фактов → упорядоченный набор встроенных фильтров/Pareto-стадий → сохранённый
trace каждого кандидата → таблица панели и один XLSX. Это индивидуальный
отбор стратегий, **не** портфельная симуляция и **не** новый tick-test.

## Что уже есть

- `main` содержит v2 import и ручной A/B с 30-дневным эквивалентом
  (`75277f1`), а предыдущий общий handoff —
  [phase2](2026-08-31-performance-v2-phase2-handoff.md).
- `performance_v2_store.py` хранит одну текущую result-запись на ACTIVE
  стратегию, её actions/equity, order→plateau lineage и cached safe windows.
  В v2 **ещё нет** `selection_runs`, `selection_results`, tags, discard,
  RETEST, filter API или XLSX.
- В `index.html` уже есть статическая карточка «Pareto и фильтры»: она
  показывает предполагаемые stage/group controls, но `app.js` намеренно не
  вызывает endpoint selection. Это preview, а не реализация.
- В v1 была полезная алгоритмическая база для DD5 proxy, holding evidence,
  global/scoped Pareto, детерминированного sequential selection, причин
  исключения и XLSX. Она остаётся только историческим источником проверенных
  правил; v1 schema, pandas-таблицы и DD5 runs нельзя подключать к v2 или
  читать из него.

## Источники истины и приоритет

1. [Unified Performance Analytics v2 spec](../specs/2026-08-28-unified-performance-analytics-v2.md),
   §§ "DD5 proxy" и "Ordered analysis and finalist selection" — целевой
   контракт и границы v2.
2. [ADR-0020](../decisions/0020-unified-performance-analytics-v2.md) —
   архитектурные решения v2.
3. [v1 DD5 finalist selection](../specs/2026-08-14-dd5-finalist-selection.md),
   §§4–9 — переносимые формулы, правила детерминизма и audit-practices.
4. [v1 strategy Performance store](../specs/2026-08-14-strategy-performance-duckdb.md)
   — историческая граница DD5/XLSX; не контракт нового хранилища.
5. [Portfolio Optimizer design](../superpowers/specs/2026-08-30-portfolio-optimizer-design.md),
   §§3–6 — только будущий consumer выбранных индивидуальных стратегий.
   Он не даёт права делать portfolio simulation сейчас.

При конфликте побеждает v2 spec/ADR. Старый DD5 переносится как лучшая
практика, а не буквально как API, таблица или утверждение о тестированном
результате.

## Что перенести из v1

- Рассчитывать признаки один раз **до** стадий; порядок фильтров не меняет
  сами факты.
- Брать только committed DB evidence; не перечитывать HTML/CSV. Отсутствующую
  метрику не подменять нулём — стратегия получает явную причину исключения.
- Сохранить проверенные holding/trade outlier правила по scope
  `(symbol, side, timeframe)`: IQR для `holding_p95_minutes` и `trades`.
- Сохранить Pareto с правилом «не хуже по всем objectives и строго лучше хотя
  бы по одному», deterministic order и trace причин: missing metrics,
  holding/trade filter, stage elimination или SELECTED.
- Полезные v1 варианты: capital (`pnl30` ↑, capital proxy ↓), holding,
  Close MA, first shift, balanced; условная Close-MA стадия только если после
  предыдущей стадии остаётся больше трёх строк; near-tie ranking — только
  presentation, никогда не обходит фильтры/Pareto.
- В output всегда оставлять всех входных кандидатов и причину, а не только
  финалистов. Panel и XLSX должны читать одни сохранённые results.

## Что меняется в v2

- Использовать только `DD5_PROXY`: `risk_scale = target_dd_pct / DD` и
  `dd5_daily_log_return_proxy = daily_log_return * risk_scale`. Масштабирование
  lots/PnL — диагностическое `CALCULATION_ONLY`, не tick-test и не готовый
  результат.
- Добавить в новую v2-спеку/план: typed registry известных stage IDs,
  `enabled` и typed parameters, точный group scope, A/B deterioration
  (`ANNOTATE`/`FILTER`) и его место в order. Никаких пользовательских
  выражений или незафиксированных порогов.
- Зафиксировать snapshot: `selection_runs` хранит requested windows и полную
  pipeline config; `selection_results` — одну строку на **каждую** входную
  стратегию с selected/rank/eliminating stage/reason, window refs, DD5 proxy,
  plateau summary и compact trace. Повторный запуск не должен зависеть от
  текущих настроек панели.
- Любая новая/заменённая result должна инвалидировать зависимые window,
  selection и DD5-proxy facts. Lifecycle/tags/RETEST — отдельные следующие
  куски, не добавлять их в первый selection slice без отдельной спецификации.

## Рекомендуемая последовательность

1. До кода обновить v2 spec и сделать небольшой plan: утвердить registry,
   scopes, exact parameter schema, trace/result schema и XLSX sheets.
2. TDD: pure v2 feature builder + deterministic stage executor на fixtures;
   перенести v1 test-cases, но строить facts из v2 current results/actions.
3. Добавить v2 persistence (`selection_runs/results`) и атомарный runner;
   подтвердить, что filter order меняет survivors, но не source facts.
4. Добавить panel run/status/result table и XLSX из сохранённого run. Сначала
   серверный contract, затем UI — не превращать текущий preview в источник
   истины.
5. Проверить current v2 tests + v1 non-disturbance, `node --check`,
   `git diff --check`, затем commit/review.

## Непереходимые границы

- Не использовать absolute PnL либо сумму individual PnL как portfolio result.
- Не строить Portfolio Optimizer/simulation, не возвращать Fast UI или Runs UI.
- Не удалять v1 и не мигрировать/dual-read его таблицы.
- Не добавлять `point_id`, произвольный filter DSL, DD5 proxy UI как
  «результат теста», discard или RETEST в этот срез без отдельного approved
  contract.
