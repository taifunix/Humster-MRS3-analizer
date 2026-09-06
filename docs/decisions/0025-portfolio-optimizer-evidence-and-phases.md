# ADR-0025: Portfolio Optimizer — joint tests, evidence и фазовая граница

**Дата:** 2026-09-05

**Статус:** Proposed — пользовательские решения и замечания независимого Opus
review внесены в D4; plan/spec review D4: `PLAN_APPROVED`. D5 фиксирует
стартовую research-only `portfolio_optimizer_research_risk_v1` отдельным
ADR-0029 и ожидает отдельный review. Не
является разрешением реализации или запуска tester.

## Контекст

Прежний Portfolio Analyzer v0.4 предполагал собственную replay-симуляцию и
ограниченный размер сетов. Performance v2 теперь хранит typed стратегии и
current replaceable results; есть отдельный portfolio tick-tester и принятый
Bybit collector. Рабочие заметки и последующие уточнения нужно заменить
каноническими документами без зависимости от исходной подборки.

## Предлагаемое решение

1. Read-only `portfolio_optimizer_input` над Performance v2 предоставляет
   заранее проверенных immutable directional candidates. Новый оптимизатор
   не меняет геометрию/internal lot_x и не пишет в Performance DB.
2. Существующий tester — источник actual joint trading results; optimizer
   рассчитывает liquidity/margin guards отдельно, не обещая точную биржевую
   liquidation simulation. Сумма одиночных PnL не является portfolio PnL.
3. Portfolio DuckDB сохраняет минимальные campaign input snapshots,
   normalized test facts и evaluations. Strategy/Result ID не фиксируют
   изменяемую историю сами по себе. Decision replay отделён от повторного
   запуска, требующего исходных ticks и tester binary. Snapshot получает
   canonical content digest, source schema version и read time; старый Campaign
   воспроизводится только из сохранённых фактов, а изменение source создаёт
   новый Campaign. Все виды digest используют один versioned canonical UTF-8
   contract с typed units/decimals/order; golden vectors фиксируют bytes.
4. Несколько независимых Cross accounts входят в MVP без фиксированного
   продуктового числа пар. Их общая liquidity load проверяется грубо уже
   в MVP; депозиты задаёт пользователь. Capital allocation — Phase 7.
5. Limiter учитывает counted pairs отдельно от priority=0 exemptions;
   margin включает все пары и переходные multi-overflow states до подтверждённых
   отмен/закрытий. Большое leverage не отменяет DD/MM gates.
6. Отдельный `portfolio_optimizer.local.json` содержит versioned scenarios,
   policies и три типа агрессивности. Числа и exact risk rankings пока
   не утверждены. Базовая untouched validation входит в MVP.
7. Existing collector остаётся под ADR-0024: marker-authoritative read,
   без новой обязательной инфраструктуры manifests/hashes для producer.
   Consumer фиксирует только использованное evidence. Для MVP market turnover
   берётся on-demand из публичного Bybit tickers endpoint и замораживается с
   provenance; неподтверждённые monthly/median значения не синтезируются.
   Public ticker читается вне Performance transaction; допустимые skew/staleness
   остаются OPEN POLICY, request failure даёт UNKNOWN. Private endpoint не входит.
8. До реальных runs один target-wide cross-process ownership primitive обязан
   стать общим для panel, RETEST, CLI/common runner и optimizer. Восстановление
   временных settings, transactional import/readback и scope-safe cleanup
   обязательны. Отдельный remote tester допустим без ослабления этих правил.
9. Основной результат joint run — `final_equity - initial_equity`, DD считается
   по той же фактической полной equity series, realised PnL хранится отдельно,
   а `OPEN_AT_END` остаётся явной диагностикой. Deployment recommendation-only.
10. Portfolio DB writers с M1 используют отдельную cross-process lease по
    canonical DB path. DB lease и tester lock хранят PID/start/host/boot identity;
    foreign/unknown owner блокирует действие, reclaim возможен только для
    same-host/same-boot proven-dead owner. Campaign content identity уникальна
    транзакционно. Unverifiable retired-owner lock снимается только явным
    оператором после durable audit attestation; автоматического bypass нет.
11. TradingRun хранит execution Campaign; Evaluation хранит execution и decision
    Campaign. Fresh reference создаёт новую decision Campaign/Evaluation без
    retest, пока executable payload не изменился. `TradingRun` — единственное
    каноническое имя execution entity. Ranking contract и decision-level reasons
    хранятся в decision Campaign и входят в её identity.

## Последствия и границы принятия

- [Спецификация](../specs/2026-09-05-portfolio-optimizer.md) содержит фазовые
  контракты; [план](../superpowers/plans/2026-09-05-portfolio-optimizer.md) —
  последовательность и acceptance gates. PRD индексирует их, progress хранит status.
- ADR-0001/0020/0024 не переписываются. После принятия этого ADR новая spec
  заменит конфликтующие требования queued v0.4 только для нового optimizer;
  existing runtime и историческое evidence сохраняются.
- Неизвестные capabilities и risk thresholds блокируют соответствующие
  final gates, но не требуют запусков для документирования fixtures.
- Не создаётся второй matching engine, копия Performance DB, торговый API
  или автономный capital allocator в MVP.
- `PLAN_APPROVED` относится только к архитектурной документации D4. ADR-0029
  добавляет только research-only DD/free-margin/MM defaults; PnL,
  liquidity/freshness, ranking и implementation authorization остаются
  отдельными gates.
