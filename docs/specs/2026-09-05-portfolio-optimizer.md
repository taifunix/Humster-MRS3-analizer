# Portfolio Optimizer — спецификация по фазам

**Дата:** 2026-09-05

**Статус:** Draft D7 — независимый Opus plan/spec review D4–D7:
`PLAN_APPROVED`. D5 фиксирует стартовую
`portfolio_optimizer_research_risk_v1`; D6 уточняет admission/sizing contract;
D7 добавляет отдельный контракт минимального интерфейса Panel.

**D5 review:** `PLAN_APPROVED`. **D6 review:** `PLAN_APPROVED`.
**D6 contract:** [ADR-0030](../decisions/0030-portfolio-optimizer-m2-admission-and-sizing-contract.md).
**D7 UI contract:** [спецификация Panel](2026-09-06-portfolio-optimizer-panel-ui.md),
[ADR-0031](../decisions/0031-portfolio-optimizer-panel-ui-and-campaign-boundary.md).
**D7 UI docs review:** `CODE_REVIEW_PASS` — Claude Opus 5 high, two rounds.
Пользователь авторизовал M0 read-only inventory в новой сессии; real tester/bot
runs остаются запрещены до M5 и отдельного разрешения.

**Реализация:** M0 read-only inventory accepted after independent Opus
`CODE_REVIEW_PASS`; fixture-only M1 implementation accepted after independent
Opus `CODE_REVIEW_PASS`; fixture-only M2 liquidity/reference implementation accepted
after independent Opus `CODE_REVIEW_PASS`. M3–M4 fixture-only implementations are
accepted after independent Opus `CODE_REVIEW_PASS`; M5 выполняется только на
fixtures/fakes, M6–M8 не начаты. Запуск tester/bot не разрешён. Evidence:
[M0 capability inventory](../superpowers/plans/2026-09-06-portfolio-optimizer-m0-evidence.md),
[M1 implementation evidence](../superpowers/plans/2026-09-06-portfolio-optimizer-m1-evidence.md),
[M2 implementation evidence](../superpowers/plans/2026-09-06-portfolio-optimizer-m2-evidence.md),
[M3 evidence](../superpowers/plans/2026-09-06-portfolio-optimizer-m3-evidence.md),
[M4 evidence](../superpowers/plans/2026-09-06-portfolio-optimizer-m4-evidence.md).

**Численные политики:** стартовые DD/free-margin/MM limits для исследования
зафиксированы как `portfolio_optimizer_research_risk_v1` в §10.1. Минимальный PnL, liquidity/freshness и точный
Balanced/Conservative ranking требуют последующего согласования.

## 1. Назначение и управление документом

Из заранее протестированных MRS3-стратегий система формирует несколько
независимых портфелей, подбирает состав, размеры, limiter и приоритеты,
проверяет совместную торговлю существующим tick-tester и выдаёт рекомендации.
Сумма одиночных PnL никогда не считается результатом портфеля.

Этот файл содержит требования, контракты и DoD по фазам. Порядок реализации
находится в [плане](../superpowers/plans/2026-09-05-portfolio-optimizer.md),
архитектурная граница — в [ADR-0025](../decisions/0025-portfolio-optimizer-evidence-and-phases.md).
Оперативное состояние хранится только в [progress](../../progress.md),
продуктовый scope — в [PRD](../../PRD.md).

Это самодостаточный новый пакет дизайна, а не приложение к прежней подборке
заметок. Для его применения не требуется сохранять исходную рабочую папку.
Он ещё не supersede-ит утверждённые runtime-контракты: принятие новой
спецификации/ADR и независимый review остаются отдельным gate. Старый
[Portfolio Analyzer v0.4](2026-08-09-portfolio-analyzer-v04.md) сохраняет
происхождение прежнего дизайна; его собственная replay-симуляция и фиксированное
число пар не переносятся в новый оптимизатор.

### Явные зависимости

- [Модель документации, ADR-0001](../decisions/0001-repository-and-documentation-model.md).
- [Unified Performance v2](2026-08-28-unified-performance-analytics-v2.md),
  [ADR-0020](../decisions/0020-unified-performance-analytics-v2.md),
  [CHECK & RETEST](2026-09-03-performance-v2-retest-workflow.md),
  [typed-config identity](2026-09-04-performance-v2-config-dedup.md).
- [Selection review](2026-09-02-performance-v2-selection-review-import.md),
  [ADR-0021](../decisions/0021-performance-v2-persisted-selection-snapshots.md),
  [ADR-0022](../decisions/0022-performance-v2-selection-review-ledger.md).
- [Bybit collector Revision 2](2026-09-05-bybit-market-data-collector.md) и
  [ADR-0024](../decisions/0024-bybit-market-data-collector-archive.md).
- [Минимальный интерфейс Panel](2026-09-06-portfolio-optimizer-panel-ui.md) и
  [ADR-0031](../decisions/0031-portfolio-optimizer-panel-ui-and-campaign-boundary.md).
- Исторический handoff: только
  [разделы 9–10](../archive/sources/MRS3_v07_MASTER_HANDOFF_LEGACY_DUCKDB_2026-08-10.md#9-что-появится-только-после-реальных-mrs3-tick-тестов).

## 2. Согласованная предметная модель

### 2.1. Рынок и счета

- Начальный рынок — Bybit linear контракты на американские акции;
  сырьевые контракты допускаются после проверки конкретного инструмента.
  Это биржевой дериватив, не ликвидность базовой акции на другой площадке.
- На каждый портфель — отдельный Unified Cross account или несвязанный
  subaccount, свой баланс, свой limiter. Балансы и маржа счетов не суммируются.
- Пользователь задаёт депозит каждого сценария. Рекомендация распределения
  общего капитала между типами портфеля отложена до Phase 7.
- Фиксированного продуктового ограничения «6–8» или «не более 10 пар» нет.
  Реальная граница — допустимый universe, биржевые ограничения и бюджет поиска.
- Одна пара может входить в несколько портфелей. Разные глубины shift
  выбираются только из уже протестированных кандидатов. Отличие shifts
  само по себе не доказывает независимость риска или исполнения.

### 2.2. Стратегия

Стратегия mean-reversion, без стратегических стопов. Dedicated closing order
закрывает позицию. Принудительное закрытие лишней позиции по limiter — отдельная
штатная механика, не добавление стопа оптимизатором.

LONG и SHORT выбираются независимо при достаточном индивидуальном evidence.
Целевой контракт — не более одной направленной позиции по symbol на счёте.
При открытой позиции opposite opening orders могут оставаться или сниматься
согласно явной настройке бота. Оставленная встречная заявка не объявляется
closing order; эффект её исполнения должен быть подтверждён adapter fixtures.

Оптимизатор не меняет MA, sources, shifts, число/геометрию ордеров, внутренние
`lot_x`, CloseMA и правила выхода. Разрешено масштабировать все opening orders
одного directional candidate единым scalar, сохраняя внутренние пропорции.
Режим BOTH требует совместного retest даже после успешных раздельных тестов.

### 2.3. Термины

| Сущность | Содержание |
| --- | --- |
| DirectionalCandidate | Strategy ID, канонические настройки, symbol/side/TF, индивидуальные факты и их период |
| PairSlot | один symbol и необязательные LONG/SHORT candidates; хотя бы одна сторона обязательна |
| PortfolioCandidate | PairSlots, скаляры, limiter, priority, opposite-order policy, депозит/max_balance и исполняемые настройки |
| Campaign | замороженный universe, input/config snapshots, periods, budget, список experiments |
| TradingRun | конкретный совместный tick-test с точными входами, immutable `execution_campaign_id` и результатами |
| Evaluation | расчёт gates/ranking фиксированной политикой над сохранённым TradingRun |
| PortfolioSet | одновременно рассматриваемые независимые портфели и совместная проверка их liquidity load |
| Тип портфеля / профиль риска | AGGRESSIVE, BALANCED, CONSERVATIVE; не биржевой margin mode |

## 3. Фазы и границы MVP

| Фаза | Результат | Граница |
| --- | --- | --- |
| 0 | готовность внешних данных и adapters | переиспользовать collector и Performance v2 |
| 1 — MVP | offline optimizer, portfolio tests, три типа рекомендаций | несколько счетов, пользовательские депозиты, грубая общая liquidity-проверка |
| 2A | empirical capacity и уточнённая margin/order модель | дополнительные факты об исполнении |
| 2B | read-only Live Account Monitor | наблюдение и alerts, без торговых команд |
| 3 | time/session analysis | только evidence-backed entry policies и joint retest |
| 4 | dependency и robustness | совместные неблагоприятные состояния и устойчивость выбора |
| 5 | degradation analytics | сравнение live и test, рекомендации |
| 6 | controlled rotation | проверяемая замена стратегий, без автоматической торговли по умолчанию |
| 7 | advanced validation, multi-account allocation и profit policies | распределение капитала — будущая идея, не MVP |

В MVP входят liquidity ceilings, актуальные leverage/risk tiers, limiter-aware
IM/MM precheck, оптимизация priorities, portfolio report import, replay,
минимальная защита от подгонки, экспорт точных конфигураций.

Не входят: собственный matching engine, точная копия Bybit liquidation engine,
live monitor/alerts, оптимизация времени торговли, автоматические переводы,
автоторговля/ротация, генерация новых shifts, точная empirical fill capacity,
полный order-lifecycle recorder и отдельная funding/fee аналитическая система.
Комиссии и funding в фактическом PnL при этом не исключаются.

## 4. Phase 0 — источники и готовность

### 4.1. PerformanceDB и `portfolio_optimizer_input`

Единственный источник индивидуальных кандидатов — текущая Performance v2 DB
через read-only adapter `portfolio_optimizer_input`. Это имя выходного
контракта, не утверждение о существующем готовом API. Source DB/raw HTML не
становятся альтернативными входами оптимизатора.

Adapter проверяет текущую схему и читает typed strategy/orders, current result,
report/effective/research windows, metrics, tags и актуальный selection review.
Единственный MVP-universe — строки замороженного source snapshot с точным
`User Status = FINALIST`. `RESERVE`, ручной набор, отсутствующий или иной status
исключаются без fallback. Нет данных о текущем статусе/периоде — причина отказа,
а не молчаливый fallback.

В коде уже есть `selection_runs`/`selection_results`, но это не доказательство
полного immutable snapshot использованных фактов. `strategy_results` может
обновляться на месте с прежним Result ID при расширении истории.
Достаточность existing snapshots проверяется, недостающие входы сохраняются
в Campaign (§5.2), без полной копии Performance DB.

Нельзя вызывать cache-writing selection/windows функции через read-only adapter.
Всё source-чтение выполняется одной согласованной read-only транзакцией;
существующий selection path вызывается только с `cache_only=True`. Cache miss
рассчитывается из уже прочитанных typed facts и сохраняется только в Portfolio
DB. Оптимизатор не останавливает и не ставит на паузу чужой writer: если
согласованный snapshot открыть нельзя, операция завершается fail-closed и может
быть повторена оператором. Копирование открытого файла не заменяет snapshot.

### 4.2. Collector — существующая независимая подсистема

Его действующая спецификация и ADR-0024 определяют формат; заново реализовывать
сборщик из прежнего проекта нельзя. Он не знает стратегий, балансов и позиций,
не использует private API и не выдаёт capacity-рекомендаций.

Контракт чтения оптимизатора:

- `liquidity_1m`, видимые bid/ask depth 10/25/50/100 bps, minute p05/median,
  spread, coverage, active targets и complete ratios;
- текущая `schema_version=2`, nullable FLOAT64 и combined/side-specific
  bid/ask completeness ratios по утверждённой схеме;
- `published_hours` SQLite index определяет опубликованные liquidity-файлы:
  сырой glob не является источником истины, незарегистрированный final не читается;
- instruments/risk-limit snapshots и symbol events читаются по проверенному
  текущему reference-контракту; архив и spool не изменяются потребителем;
- недоступный/неполный файл, неподдерживаемая схема, низкое качество, пропуски
  наблюдения и смена состава symbols отражаются явно.

Collector не публикует per-file cryptographic manifests и не обязан это делать.
Digest фактов/файлов, использованных кампанией, при необходимости вычисляет
сам read-only consumer для своей воспроизводимости. Marker удостоверяет
структурную публикацию, не достаточность рыночной ликвидности.

Для чтения индекса использовать проверенный read-only путь, например текущий
`SQLiteSpool.open_read_only(...).published_hours()`. Обычный `SQLiteSpool(...)`
берёт writer lock и создаёт/настраивает schema, поэтому не подходит consumer.
Открытый reader закрывается после чтения; отсутствие spool не запускает collector.

### 4.3. Gate фазы 0

Есть зафиксированное сопоставление реальных полей DB/report/config, поддержанных
tester capabilities, collector schema и данных instrument tiers. Отсутствующая
возможность блокирует только зависящий путь; mocks не подтверждают реальную
совместимость. Live/soak status collector берётся из progress, не из этой спеки.

## 5. Phase 1 — MVP: конфигурация, данные и воспроизводимость

### 5.1. Отдельный config

Рабочий файл: `portfolio_optimizer.local.json`. Будущий отслеживаемый образец:
`portfolio_optimizer.local.json.example`, без реальных путей и secrets.
Это не конфиг collector и не неявное расширение `config.local.json` панели.

Обязательные группы:

| Группа | Содержание |
| --- | --- |
| Identity | schema version, policy version, algorithm versions |
| Inputs/storage | Performance DB, Portfolio DB, collector root, approved templates |
| Scenarios | явно заданный депозит каждого портфеля, currency/collateral assumptions, `current_portfolio_equity` с timestamp/source/currency и maximum age, `max_balance`, sizing mode и верхняя граница sizing balance для проверяемого горизонта |
| Search | exact `FINALIST` universe, exhaustive composition candidates bounded by `max_enumerated_combinations`, sizing/limiter/priority candidates, finite sizing grid, seed, rounds, total test budget, and a post-joint-test selection limit |
| Research | development/validation windows, warm-up и boundary rules, evidence minimums |
| Liquidity | lookback, band, quantile, permitted share, freshness, valid-minute/coverage/completeness minima, global screen |
| Margin model | venue/account mode, fees, tier и denominator semantics, overflow envelope, missing-data policy |
| Profiles | отдельные DD/free-margin/MM bounds, `each_strategy_max_dd_pct`, PnL minimum, exact ranking ID/parameters и top_n |
| Runner | локальный либо уже предоставленный удалённый tester target, paths, timeouts/retries и ownership |

Все денежные единицы, проценты/fractions и временные единицы задаются явно.
Неверное значение или неизвестный ranking ID — ошибка. Versioned стартовые
DD/free-margin/MM defaults `portfolio_optimizer_research_risk_v1` определены
в §10.1; никаких иных risk/PnL/ranking чисел не подразумевается.
Общие margin-поля определяют модель, профильные — лимиты: конфликтующие
численные значения в двух местах запрещены.

Config и overrides разрешаются один раз при старте Campaign. Новая редакция
файла не меняет уже идущую кампанию. Неутверждённые thresholds/ranking допускают
разработку parser/storage на fixtures, но не `RECOMMENDATION_READY`.

Поля config делятся на три класса. Исполняемые входят в TradingRun identity;
risk/ranking/reference policy — в Evaluation identity; timeout/retry и другие
чисто операционные параметры могут меняться между attempts, но не исполняемый
payload. Класс каждого поля фиксирует schema, неизвестное поле не принимается.
Campaign, TradingRun, Evaluation и export имеют типизированные disposition/reason;
минимальный набор использует `RESEARCH_ONLY`, `RECOMMENDATION_READY`,
`NEEDS_RETEST`, `NEEDS_RESCREEN` и причины `OPEN_POLICY`,
`INSUFFICIENT_EVIDENCE`/`UNKNOWN`. Это не отдельный workflow-engine.

### 5.2. Что замораживается

Согласованное read-only чтение сохраняет минимальный набор, достаточный для
повтора решения, а не всю исходную DB:

- все рассмотренные Strategy IDs, точные канонические настройки и их hashes;
- идентичность source DB/result, report/effective/selection periods;
- использованные метрики/признаки, их версии, причины допуска/исключения и
  selection/review provenance, включая данные upstream-периода отбора;
- разрешённые настройки кампании и random seed, если алгоритм использует RNG;
- использованные exchange/liquidity facts, качество, периоды и content digests;
- для каждого run — strategy JSON, tester/account runtime settings без secrets,
  binary version/hash и доступная идентичность tick dataset;
- normalized results, required series, parser/metric versions, report digest.

Если поиск использует derived dependency features, сохраняются эти входы,
а не только ссылка на mutable таблицу. При замене/расширении Performance-истории
старое решение остаётся объяснимым, новая кампания читает новое состояние.

Для каждого набора source-строк, окон, метрик и derived facts сохраняются
канонический content digest, время согласованного чтения и версия source schema.
Result ID без digest не является identity. Decision replay читает сохранённые
факты и не сверяется с изменившейся Performance DB; несовпадение нового чтения
создаёт новую Campaign и не инвалидирует replay старой.

### 5.3. Три идентичности вместо смешивания кэшей

1. Campaign/input identity фиксирует universe, periods, facts и policy.
2. Trading-run identity фиксирует точный исполняемый portfolio package,
   период, депозит/cap, fees, limiter/priority, opposite policy, версии
   binary/templates и tick data, а также immutable `execution_campaign_id`.
   Любое влияющее на торговлю изменение — новый run.
3. Evaluation identity фиксирует Run ID, ranking/margin/liquidity policy,
   `execution_campaign_id` из TradingRun и отдельный `decision_campaign_id`
   snapshot, факты которого использованы для текущего решения. Decision replay
   читает decision Campaign, executable verification — execution Campaign.
   Повторная оценка старого run допустима, но не переписывает старую оценку.

Хеш только timestamp обновления reference не должен создавать фиктивное
изменение торгового run. Изменившееся фактическое leverage/quantity — должно.
При неизвестной идентичности binary/ticks нельзя заявлять exact rerun cache hit.

К TradingRun относятся все фактически переданные tester параметры: quantities,
leverage, geometry, deposit/cap и sizing, limiter/priority/opposite policy,
tester fees, период/ticks, binary и templates. Текущие status/filters, max
leverage, tiers/MMR и liquidity facts относятся к Evaluation, пока не меняют
payload. Если обновление делает payload недопустимым, READY блокируется; если
для исправления меняется payload, создаётся новый TradingRun и нужен retest.
Например, fresh turnover создаёт новый decision Campaign и Evaluation, но
сохраняет TradingRun и его execution Campaign, если executable payload остался
валидным и неизменным.

**Decision replay** воспроизводит выбор из сохранённых входов/results без
текущей Performance DB и HTML. **Tick-test replay** дополнительно требует
доступности тех же ticks и executable; один hash не восстанавливает удалённые
файлы. Ограничения replay показываются отдельно, все ticks в DB не копируются.

### 5.4. Канонический digest

Все content, ticker/reference, semantic-result и PortfolioSet digests используют
один versioned `canonical_digest_v1`: SHA-256 от compact UTF-8 JSON с
детерминированной сортировкой ключей. Envelope включает schema ID/version,
версию digest contract и явные type/unit tags. Timestamps нормализуются в UTC
RFC 3339 с `Z` и schema-declared precision. Decimal quantities, prices и equity
кодируются по объявленным для поля scale/rounding rules без промежуточного
binary-float rendering и без молчаливого отбрасывания лишней точности.

Missing field, explicit `null` и `UNKNOWN` имеют разные представления; UNKNOWN
включает stable reason. Presentation-only fields исключаются только versioned
списком схемы. Candidates/facts/members сортируются по canonical typed identity,
actions и series — по `(timestamp_utc, source_ordinal)`, loads — по
`(symbol, direction, member_identity)`. `source_ordinal` сохраняет экономически
значимый порядок при одинаковом времени. Изменение canonical schema создаёт
новую identity, не меняя воспроизводимость старой версии.

Golden fixtures фиксируют literal expected digest как минимум для Campaign
snapshot, semantic result/actions/series и PortfolioSet; перестановка ключей
не меняет digest, а изменение type/unit/UNKNOWN reason/decimal/source ordinal
меняет. Expected bytes нельзя получать в тесте второй копией той же реализации.

### 5.5. Portfolio DuckDB

Это отдельное хранилище портфельных экспериментов, не конкурирующая Performance
DB. В MVP численные portfolio series хранятся в child-таблицах DuckDB;
вынос в Parquet — отдельное изменение по измеренному объёму, не второй
одновременно поддерживаемый способ хранения.

Минимальные логические наборы (физическое объединение таблиц допустимо):

| Набор | Обязательные факты |
| --- | --- |
| campaigns / candidate snapshots | config, universe, input facts, provenance, status |
| portfolio candidates / members | состав, directions, scalars, limiter, priorities, scenario |
| reference used / prechecks | exchange/liq facts, качество, envelope, diagnostics, решения gates |
| generated strategies / test runs | точные JSON/settings, hashes, execution identity, attempts/status |
| transactions / position cycles | все actions, stable order, реконструированные циклы и availability |
| portfolio / symbol metrics / series | фактические joint results, numeric paths и версии |
| evaluations / profile rankings | policy, причины отказа, diagnostics, ordering |
| portfolio sets / deployments | выбранные независимые accounts, shared-liquidity assessment, manifest |

До первой записи M1 все writers M1–M8 используют DB-scoped cross-process lease,
ключом которой служит canonical resolved Portfolio DB path. Owner содержит PID,
process-start identity, host/machine identity и boot/container-instance identity.
Foreign/unknown host или boot считается live/unverifiable и блокирует запись;
reclaim разрешён только на том же host/boot для доказанно мёртвого PID с
проверенной start identity. Optimizer не останавливает и не завершает процессы.
Read-only подготовка не держит lease; publication выполняется короткой transaction.

Непроверяемый owner даёт `LOCK_OWNER_UNVERIFIABLE`: write прекращается без
retry loop, bypass и partial publication. Для retired host/container разрешён
только явный manual clear оператором. До очистки versioned append-only ownership
audit sidecar рядом с lock сохраняет lock kind/path/target, полный stale owner
PID/start/host/boot, operator identity, UTC time и причину. Запись attestation
выполняется атомарно вне защищаемой DB до удаления lock; автоматический процесс
не может создать attestation или применить clear. Clear не завершает process.

Campaign canonical content identity имеет transactional UNIQUE constraint.
Concurrent duplicate возвращает ту же exact row либо после deterministic
uniqueness conflict перечитывает её; mismatch/collision завершается fail-closed,
а не создаёт вторую Campaign. Уникальность run/attempt/action keys и idempotent
import также проверяются транзакцией.
Raw report digest хранится как provenance. Для одинакового executable manifest,
binary и ticks дополнительно сравнивается канонический semantic digest
нормализованных results/actions/series: расхождение получает
`NONDETERMINISTIC_RESULT`, не схлопывается в cache hit и блокирует READY до
явного disposition. Отличающийся только raw HTML digest этого не доказывает.
Один DB writer; независимая подготовка может быть параллельной, стандартный
бюджет тяжёлой обработки — 16 workers с явным ограничением ресурсов.
Число workers импорта не задаёт число одновременно запущенных tester instances.

### 5.6. Нормативные dispositions и причины

Отдельная state machine не создаётся. Каждый condition применяется к указанному
объекту; candidate-local `FAIL` не делает всю Campaign неуспешной, если поиск
может продолжаться. Все применимые reasons сохраняются.

| Condition | Scope | Результат | Stable reason |
| --- | --- | --- | --- |
| owner unverifiable / operator manual clear | requested protected action / ownership audit | stop без write / durable audit event | `LOCK_OWNER_UNVERIFIABLE` / `LOCK_MANUAL_CLEAR` |
| turnover отсутствует/stale либо public request failed | Evaluation / PortfolioSet | `INSUFFICIENT_EVIDENCE` | `TURNOVER_MISSING` / `TURNOVER_STALE` / `TURNOVER_REQUEST_FAILED` |
| liquidity отсутствует/stale/ниже quality policy | candidate Evaluation | `INSUFFICIENT_EVIDENCE` | `LIQUIDITY_MISSING` / `LIQUIDITY_STALE` / `LIQUIDITY_QUALITY_INSUFFICIENT` |
| fee unknown без approved bound | Evaluation | `INSUFFICIENT_EVIDENCE` | `FEE_RATE_UNKNOWN` |
| нет finite sizing upper bound | candidate Evaluation | `INSUFFICIENT_EVIDENCE` | `SIZING_ENVELOPE_UNBOUNDED` |
| equity denominator/path/coverage недостаточны | TradingRun и dependent Evaluation | `INSUFFICIENT_EVIDENCE` | `EQUITY_DENOMINATOR_INVALID` / `EQUITY_PATH_MISSING` / `EQUITY_COVERAGE_INSUFFICIENT` |
| baseline individual DD/current equity/profile cap недоступны или invalid | PortfolioCandidate | `INSUFFICIENT_EVIDENCE` / `RESEARCH_ONLY` | `INDIVIDUAL_DD_UNAVAILABLE` / `OPEN_POLICY` |
| оценённый DD одной стратегии выше `each_strategy_max_dd_pct` | PortfolioCandidate | `FAIL` | `INDIVIDUAL_DD_LIMIT` |
| фактический leverage не равен manifest либо не прочитан | TradingRun | `NEEDS_RETEST` | `LEVERAGE_MISMATCH` / `LEVERAGE_UNVERIFIED` |
| round-down ниже minimum/нарушил geometry | PortfolioCandidate | `FAIL` | `POST_ROUNDING_MINIMUM` / `POST_ROUNDING_GEOMETRY` |
| enumeration limit превышен, approved bound посчитан | guard evidence | не автоматический отказ; `CONSERVATIVE_BOUND` | `ENUMERATION_FALLBACK_USED` |
| margin bound нарушен/недоступен | candidate / Evaluation | `FAIL` / `INSUFFICIENT_EVIDENCE` | `MARGIN_BOUND_FAILED` / `MARGIN_BOUND_UNAVAILABLE` |
| frozen finalist не прошёл validation | finalist Evaluation | `FAIL` | `VALIDATION_FAILED` |
| semantic divergence при exact execution identity | TradingRun | `NONDETERMINISTIC_RESULT` | `SEMANTIC_RESULT_DIVERGENCE` |
| executable payload изменился | Evaluation / export | `NEEDS_RETEST` | `EXECUTABLE_PAYLOAD_CHANGED` |
| PortfolioSet member/load изменился | PortfolioSet Evaluation / export | `NEEDS_RESCREEN` | `PORTFOLIO_SET_CHANGED` |
| обязательная PnL/liquidity/freshness/ranking policy открыта | Evaluation / export | `RESEARCH_ONLY` | `OPEN_POLICY` |
| ни один frozen finalist не прошёл validation | profile/decision Campaign | `INSUFFICIENT_EVIDENCE` | `NO_VALIDATION_PASS` |

`RECOMMENDATION_READY` допустим только без blocking rows. Если отсутствие
validation pass совпало с invalid TradingRun, сохраняются обе причины, а более
конкретный TradingRun disposition не заменяется `NO_VALIDATION_PASS`.
`NEEDS_RETEST` и `NONDETERMINISTIC_RESULT` относятся к execution evidence этого
TradingRun и блокируют зависящие Evaluations; decision Campaign facts они не изменяют.

Нормативные enum версии 1 разделены по смыслу:

- `portfolio_disposition_v1`: `RESEARCH_ONLY`, `RECOMMENDATION_READY`, `NEEDS_RETEST`,
  `NEEDS_RESCREEN`, `INSUFFICIENT_EVIDENCE`, `NONDETERMINISTIC_RESULT`;
- `portfolio_gate_result_v1`: `PASS`, `FAIL`, `UNKNOWN`;
- `portfolio_evidence_class_v1`: `OBSERVED`, `CALCULATED`, `CONSERVATIVE_BOUND`,
  `COARSE_ESTIMATE`, `UNKNOWN`;
- `portfolio_capability_result_v1`: `CONFIRMED_CAPABILITY`, `APPROVED_CONSERVATIVE_BOUND`,
  `BLOCKING_UNKNOWN`;
- `portfolio_reason_v1`: `TURNOVER_MISSING`, `TURNOVER_STALE`, `TURNOVER_REQUEST_FAILED`,
  `LIQUIDITY_MISSING`, `LIQUIDITY_STALE`, `LIQUIDITY_QUALITY_INSUFFICIENT`,
  `FEE_RATE_UNKNOWN`, `SIZING_ENVELOPE_UNBOUNDED`,
  `EQUITY_DENOMINATOR_INVALID`, `EQUITY_PATH_MISSING`,
  `EQUITY_COVERAGE_INSUFFICIENT`, `LEVERAGE_MISMATCH`,
  `POST_ROUNDING_MINIMUM`, `POST_ROUNDING_GEOMETRY`,
  `ENUMERATION_FALLBACK_USED`, `MARGIN_BOUND_FAILED`,
  `MARGIN_BOUND_UNAVAILABLE`, `VALIDATION_FAILED`, `NO_VALIDATION_PASS`,
  `SEMANTIC_RESULT_DIVERGENCE`, `EXECUTABLE_PAYLOAD_CHANGED`,
   `PORTFOLIO_SET_CHANGED`, `OPEN_POLICY`, `LOCK_OWNER_UNVERIFIABLE`,
   `LOCK_MANUAL_CLEAR`.

Каждая запись причины несёт явное поле `reason_enum_version`. `portfolio_reason_v1`
никогда не переопределяется и остаётся читаемым всеми последующими readers.
`portfolio_reason_v2` содержит все коды v1 с тем же значением и добавляет
`INDIVIDUAL_DD_UNAVAILABLE`, `INDIVIDUAL_DD_LIMIT`, `LEVERAGE_UNVERIFIED`.
До отдельной авторизованной M4-реализации writer продолжает выпускать только v1;
M2/M4 readers обязаны принимать v1. Golden canonical vector фиксирует exact строки
каждой версии enum. Добавление или переименование значения требует новой
enum/canonical schema version; disposition
gate, evidence class и capability result нельзя сохранять в поле reason code.

## 6. Phase 1 — liquidity и sizing

### 6.1. Рыночный ceiling

Для symbol/side policy фиксирует lookback, band, history quantile, долю depth,
freshness и минимальное качество. Базовый horizon — последние семь полностью
завершённых UTC-суток; изменение horizon требует versioned policy. Ceiling
строится по опубликованному распределению minute p05 depth всего horizon, а не
по мгновенному стакану или последней минуте: выбранный history quantile следует
интерполяции collector. Поэтому обычное падение ликвидности фондов на выходных и
вне основной сессии участвует в расчёте, а не зависит от времени запуска.
Пропуски, неполная depth, stale data и отсутствие достаточной истории не
заменяются нулями.
`band` выбирается только из опубликованных collector bands 10/25/50/100 bps;
другое значение — ошибка config, а не интерполяция стакана.

```text
depth_reference = quantile(eligible minute depth_p05, policy.quantile)
single_order_cap = depth_reference * policy.allowed_depth_share
```

LONG/bid и SHORT/ask в этой proxy-политике описывают сторону размещения входной
лимитной заявки; это не модель очереди и не доказательство её fill probability.
В opening geometry входят все position-opening уровни выбранной стратегии и
направления (включая grid/averaging/DCA), но не closing, reduce-only, TP/SL и
прочие закрывающие заявки. Для каждого Strategy ID и направления M2 сохраняет
`liquidity_scalar_pct_max`, использованные facts/window/content digest, источник и
timestamp liquidity snapshot, а также policy-defined срок его годности. Изменение
filters/tier/geometry или stale/missing snapshot инвалидирует ceiling: M4 считает
его `UNKNOWN`, а не last-known-good; отсутствие сохранённого ceiling не означает
отсутствие лимита.
Риск закрытия LONG относится к bid, закрытия SHORT — к ask; весь закрываемый
размер и аварийные market closes нельзя считать исполненными только потому,
что каждый отдельный opening order прошёл ceiling. В MVP это отдельная
conservative position/exit diagnostic с явной availability, empirical calibration
отложена до Phase 2A. Неизвестный обязательный liquidity guard не получает
финальный PASS. Отсутствие точной empirical capacity само по себе не добавляет
в MVP Phase 2A: применяются явно утверждённые coarse/proxy ограничения с
указанием их уровня доказательности, а не утверждение о точной исполнимости.

Недостаток истории не препятствует исследованию доступных фактов, но приводит
к `LIQUIDITY_QUALITY_INSUFFICIENT` для зависящей финальной рекомендации. Сегодняшняя
ликвидность поверх старого tick-test — текущая feasibility-оценка, не измерение
ликвидности того исторического периода. Оба периода фиксируются отдельно.

### 6.2. Масштабирование и `max_balance`

После подтверждения bot/tester sizing contract, для balance-percentage mode
`scalar_pct` измеряется в процентах исторического размера (100 = размер
индивидуального теста):

```text
B = actual sizing balance at the bot-defined recalculation event
B_cap = B if max_balance is disabled else min(B, max_balance)
s = scalar_pct / 100
opening_order_notional[i] = B_cap * s * lot_x[i]
full_side_notional = B_cap * s * sum(lot_x)
liquidity_scalar_pct_max = 100 * min_i(single_order_cap / (B_cap * lot_x[i]))
```

`lot_x` не нормализуются до суммы 1. Начальный `scenario_balance` — старт теста,
не постоянная база будущих заявок при динамическом sizing. Момент перерасчёта,
wallet/equity basis и изменение resting quantities подтверждаются в Phase 0.
Режим `risk_*` не объявляется эквивалентным без доказанного mapping.

Liquidity/capacity gate использует верхнюю базу объявленного sizing envelope:
при включённом cap — `min(B_envelope_max, max_balance)`, без cap — явный
конечный `B_envelope_max` и порог обязательной переоценки. Текущий balance
пригоден только для диагностики; без конечной границы READY невозможен.

Размер ограничивается exchange `qtyStep`, minQty/minNotional, maxLimit/maxMarket
quantity и precision. Quantity округляется вниз до `qtyStep`; округление вверх
ради minimum запрещено. Нулевой/меньший minimum или нарушающий immutable
геометрию уровень делает вариант FAIL с причиной, а не удаляется молча.
После округления заново проверяются сумма размера, capacity, tiers и margin.

### 6.2.1. Ceiling просадки одной стратегии

Отдельно от actual joint DD профиля применяется planning estimate одной
стратегии. Пусть `D100` — maximum drawdown amount из individual PerformanceDB
result при ровно `scalar_pct=100`, в account settlement currency этого result;
`E` — положительный `current_portfolio_equity` из одного planning snapshot в той
же currency; `c` — profile `each_strategy_max_dd_pct` в процентных пунктах.
Никакой implicit FX conversion не допускается:

```text
estimated_individual_dd_amount = D100 * scalar_pct / 100
estimated_individual_dd_pct = estimated_individual_dd_amount / E * 100
dd_scalar_pct_max = c * E / D100
```

`D100` отсутствует, не положителен или имеет другую currency; `E` отсутствует,
не положителен, stale либо имеет другую currency — `UNKNOWN` и candidate не
проходит. Отсутствующий численный `c` — `OPEN_POLICY`, не бесконечный ceiling.
Это линейная planning-модель fixed-lot, non-compounding historical result без
заявления о path-dependent margin/liquidation effects; она не является actual
joint DD и не заменяет joint test. Ограничение применяется независимо к каждой
стратегии, не суммируется между ними и ограничивает только новый или увеличенный
order, не принудительное закрытие уже открытой позиции.

`E`, `D100`, `c`, `dd_scalar_pct_max`, их currency/provenance и timestamp
сохраняются в Campaign/TradingRun evidence. `E` снимается один раз на planning
run и имеет policy-defined maximum age; при каждом новом/увеличенном order его
нужно переоценить по свежему `E`. `max_balance` ограничивает только sizing base
`B_cap` и никогда не заменяет знаменатель `E`.

Для варианта берётся minimum всех применимых ceiling, включая
`liquidity_scalar_pct_max`, `dd_scalar_pct_max`, margin и exchange limits, затем
quantity округляется вниз. Если после округления любой opening order ниже биржевого
minimum, candidate отклоняется с stable reason; увеличивать его до minimum нельзя.
Для растущей базы/изменения mark стоимость не считается навсегда равной
стартовому notional: envelope и post-test diagnostics учитывают sizing path.

Cap ограничивает базу лота, а не реальный Cross balance. Прибыль, оставленная
на счёте, учитывается в equity; её будущая защитная роль не гарантируется.
Изъятие/каскад и оптимизация распределения капитала — Phase 7.

### 6.3. Общая ёмкость нескольких портфелей

MVP выдаёт грубый допуск совместного использования symbol: `PASS`, `FAIL` или
`UNKNOWN`, с пометкой `COARSE_ESTIMATE`. Уже выбранные accounts и рассматриваемый
новый учитываются совместно, независимо от размера их маржинальных балансов.

Входы: суммарный потенциальный opening/position notional по symbol/direction,
размер отдельных заявок и `turnover24h` именно этого Bybit linear-контракта;
доступная depth даёт дополнительное ограничение. Turnover получает Phase 0/M2
read-only adapter из публичного Bybit `/v5/market/tickers` по exact
category/symbol и сохраняет server/capture time, единицы, freshness и content
digest в Portfolio DB. Этот network snapshot выполняется вне Performance DB
transaction; Decision Campaign отдельно сохраняет source snapshot time и ticker
server/capture times. Допустимые source↔ticker skew и staleness — именованные
OPEN POLICY без default. Timeout, rate limit или request failure дают
`TURNOVER_REQUEST_FAILED`/UNKNOWN, не ноль и не unlimited capacity.

Это единственный outbound endpoint Phase 1; он public/read-only, private API
запрещён. Текущая документационная работа и fixture development его не вызывают;
фактический request требует разрешения implementation. Это snapshot текущей feasibility, а не второй постоянно
работающий collector. Missing/stale значение даёт `UNKNOWN` и блокирует
зависящий READY. Месячные/средние/медианные оценки требуют накопленной истории
и не выводятся из одного snapshot. Порог и агрегация утверждаются в policy.

Отношение размера заявки к суточному turnover — грубый static screen, не
прогноз дневного оборота стратегии и не доказанная возможность выхода.
Нетто LONG минус SHORT между счетами не освобождает market capacity. Если
используется оценка оборота стратегии, отдельно учитываются частота и обе
стороны исполнения. Missing/stale turnover не означает достаточную ликвидность.

Нельзя каждому счёту выдать весь один и тот же capacity budget. Изменение
состава PortfolioSet пересчитывает shared screen перед выдачей deployment;
эта проверка не распределяет депозиты и не перемещает средства.

## 7. Phase 1 — margin и limiter

### 7.1. Биржевые facts и leverage

Из instruments/risk-limit используются status, settlement/contract type,
quantity/price filters и полный набор tiers: value bounds, max leverage,
initial/maintenance rates, mm deduction. Проверяются pagination, единицы,
freshness и применимость symbol. Значение leverage из одиночного source test
сохраняется только как provenance и никогда не является gate допуска или отказа.

Для уже выбранного scalar сначала определяется notional варианта и его
position+active-order exposure, затем применимый tier; leverage не меняет этот
notional и не является свободной осью поиска. Берётся maximum valid leverage
этого tier, округлённый вниз до `leverageStep`. Если exposure пересекает границу
tier, tier/maximum пересчитываются по новому exposure; неизвестный tier, конфликт
границ или невозможность получить устойчивый результат дают `UNKNOWN`, не выбор
удобного значения. Тир включает combined position и active-order exposure, не
только открытую позицию.

Для одного symbol в one-way portfolio существует одно planned leverage. Несколько
стратегий одного symbol не могут требовать разные значения: такой variant blocked
до единого symbol-level значения, а не разрешается молча выбором max/min. Полученное
planned leverage используется в renderer, joint tester, guards и export; оно влияет
на IM reservation, но не на размер лота. Direction/one-way semantics проверяются
fixtures. Если actual applied leverage нельзя прочитать обратно хотя бы для одного
symbol joint run, либо он не равен manifest, весь TradingRun получает
`NEEDS_RETEST` и исключается из ranking, scheduling и recommendation surfaces.

Обновление reference перед тестом и финальным экспортом обязательно по policy.
Современный tier не выдаётся за историческое состояние биржи в прошлом тесте.

### 7.2. Расчётная модель, не биржевой liquidation engine

Минимально различаются initial margin (позиции и оставшиеся opening orders)
и maintenance requirement. Для линейного контракта исходные формулы:

```text
Position IM = position value / leverage + estimated close fee
Order IM = order value / leverage + estimated open fee + estimated close fee
Position MM = position value * tier MMR - applicable MM deduction + close fee
```

Ставки fees всегда имеют versioned source. Historical TradingRun использует
ровно MakerFee/TakerFee своего tester contract. Deployment evaluation может
использовать вручную заданные актуальные account rates с provenance либо
будущий read-only private snapshot; private API не является требованием MVP.
Численного default нет: неизвестная ставка не равна нулю, а assumed upper
bound допустим только как явно утверждённая policy.

Order MM, deduction, netting и однонаправленные/встречные резервы проверяются
venue adapter; нельзя повторно вычитать position deduction из каждой заявки.
Исполненная часть заявки не учитывается одновременно как позиция и pending.
Close/reduce-only orders классифицируются отдельно; произвольная встречная
opening order не получает освобождение маржи как reduce-only.

Account IM/MM ratio требует корректного знаменателя: Margin Balance с
применимыми Haircut/Order Loss поправками. Equity DD, IM load, MM load и gross
notional/equity — разные показатели. Низкая historical DD не доказывает
достаточность маржи; `Position avg/max %` отчёта не переименовывается в IM.

Для каждого guard сохраняются numerator/denominator, источники, времена,
model version и класс `OBSERVED` / `CALCULATED` / `CONSERVATIVE_BOUND` / `UNKNOWN`.
Equity proxy не называется фактическим Bybit Available Balance или расстоянием
до ликвидации. Нет данных для обязательного gate — он не пройден. Проверенный
conservative bound допускается только явной утверждённой моделью, не подстановкой
неизвестных поправок нулём. Чистый USDT collateral, отсутствие borrow и
применимость one-way netting проверяются, не предполагаются для любого UTA.

До READY Q11/M3 фиксирует закрытую классификацию. Всегда блокируют неизвестные
или неверные equity denominator/path, instrument/tier/quantity/price identity,
sizing/limiter semantics, требуемое качество liquidity и collateral/borrow
state, когда оно влияет на знаменатель. Approved bound может покрывать сумму
всех ещё исполнимых orders/positions, multi-overflow, sizing envelope и
зафиксированные fee/price-shock assumptions только с формулой, значением и
provenance. Haircut принимается нулевым только для подтверждённого eligible
collateral без borrow; иначе это `UNKNOWN`. Phase 2A уточняет эти bounds, но не
является обязательным условием READY, если MVP-классификация полностью закрыта.

Funding не отдельный margin reservation, но его уплата влияет на wallet/equity.
Fees/funding фактического теста учитываются без двойного вычитания.
Отсутствие ликвидации в отчёте не является доказательством точной Bybit-модели.

### 7.3. Состояния limiter и priority=0

- `L=0`: limiter выключен; все разрешённые пары могут входить.
- `L>0`: учитываются уникальные PairSlots с ненулевой позицией; partial fill
  занимает slot, partial close его не освобождает, flat освобождает.
- `position_priority=0`: пара исключена из счётчика и ограничений limiter,
  но не из общей маржи/ликвидности. Её заявки не снимаются только из-за L.
- Приоритеты 1..N определяют выбор лишних позиций для market close.
  Направление сравнения и ties — capability contract, пока не доказанный default.

Пусть Z — exempt pairs, C — counted pairs, k — число открытых из C.
При k<L активны openings ещё не открытых C; при k=L бот инициирует их отмену.
Openings Z и разрешённые незаполненные уровни уже открытых позиций остаются
согласно стратегии. В нормальном состоянии число позиций может достигать
`|Z| + min(L, |C|)`; при L=0 — числа всех допускаемых пар.
Это operational/report diagnostic; margin precheck всегда использует race
envelope ниже, а не normal-state maximum.

При отмене заявки или закрытии позиции маржа освобождается только после
подтверждённого состояния. Race envelope включает L+1 И БОЛЕЕ counted positions,
pending cancels, partial fills/closes, fees и потери принудительного выхода.
До подтверждения более узкой гарантии проверяется верхняя комбинация всех
ещё способных исполниться разрешённых заявок, включая exempt pairs.
При отсутствии цены/исполнения для shock используется явный conservative
сценарий с provenance или UNKNOWN, а не гарантированное мгновенное закрытие.

До реализации policy задаёт предел полного перебора достижимых состояний.
За ним именованный conservative fallback суммирует requirements всех ещё
исполнимых positions/orders, включая exempt, под худшими применимыми tier/price
assumptions. Resource failure допустим, только если нельзя вычислить и этот
bound. Запрет считать только L самых тяжёлых позиций не отменяется из-за
ограничения времени алгоритма.
Произвольные partial fills и допустимый порядок исполнения учитываются;
последовательность «первый ордер, второй...» требует capability evidence.

### 7.4. Precheck и post-test

Precheck проверяет совместную IM/MM нагрузку по состояниям, quantity/capacity,
профильные ограничения и явный запас. Отдельные component IM limits не заменяют
проверку суммы. Post-test использует actual joint equity, exposure, concurrency
и реконструируемые позиции; margin guards считаются по протестированным settings.
Каждая equity/DD метрика хранит sampling resolution, coverage, boundary и
censoring. Недостаточная по profile policy полнота не превращается в точный DD
и блокирует зависимый gate.

Нельзя выдавать сумму несовпадающих по времени максимумов за наблюдённый пик.
Консервативная комбинация worst requirement и min equity может использоваться
как upper/lower bound с явной маркировкой. Equity<=0, invalid tiers или
невыполненное обязательное ограничение — FAIL независимо от PnL.

Явно зарегистрированная ликвидация в основном/validation прогоне — FAIL.
Нет liquidation-поля — не значит, что число ликвидаций достоверно равно нулю;
margin evaluation остаётся отдельной обязанностью оптимизатора.

В отчёте показывается достаточность заданного депозита. Если рассчитывается
минимально необходимый депозит, он относится к конкретному фиксированному
пакету абсолютных размеров и объявленным guards/model assumptions. Нельзя
менять депозит при percentage sizing, автоматически менять лоты и выдавать
это за минимальный капитал для прежних лотов. Недоступная нижняя граница
маркируется UNKNOWN. Это диагностика одного счёта, не распределение общего капитала.

## 8. Phase 1 — поиск, renderer и безопасный тестер

### 8.1. Поиск

Оси: composition только из exact `FINALIST` snapshot, directional choice, scalars,
limiter, priorities включая явно разрешённое исключение 0, opposite-order policy
и выбранные cap-сценарии. Новые shifts/MA не создаются. Индивидуальные metrics
задают только порядок proposals, не фактический ranking портфелей.

Бюджет ограничивает число proposals/проверок/test attempts и refinement rounds.
До tester детерминированно выполняются structural → liquidity ceiling → margin
→ individual-DD gates. M4 рассматривает только конечные варианты из config:
sizing grid в единицах экспортируемого percentage-поля, её границы, порядок,
composition enumeration is exhaustive up to the Campaign's hard
`max_enumerated_combinations` safety bound; exceeding that bound fails closed.
The selected scalar does not exceed the minimum
всех ceiling и после round-down снова проходит все gates. Проверяются все точки
этой конечной grid без раннего останова; Seed — максимальный прошедший uniform
scalar либо явный override. Локальное уточнение меняет одну сторону/общий scalar,
L или priority в ограниченном наборе; любой новый вариант снова проходит gates.

Прошедшие варианты получают только scheduling key:
`estimated_individual_net_pnl_at_selected_scalar / worst_calculated_initial_margin_requirement`.
Numerator — линейная planning estimate individual historical net PnL при выбранном
scalar; denominator — положительная известная worst initial-margin requirement
из уже рассчитанного envelope. Нулевой/unknown denominator исключает variant;
negative numerator остаётся допустимым числом и сортируется по обычному числовому
порядку. Равенство key разрешается canonical candidate identity. Key, его inputs и
enum/version сохраняются для воспроизведения порядка, но никогда не выдаются или
отображаются как portfolio PnL, portfolio ranking либо recommendation.
Монотонность DD/PnL по размеру не предполагается, бинарный поиск по DD запрещён.
Все tried/excluded варианты и исчерпание бюджета отражаются в Campaign.

### 8.2. Renderer

На каждый symbol создаётся один JSON. LONG/SHORT geometry и sizing берутся
строго из своих candidates, leverage общий. Разные TF сторон разрешены
только при подтверждённой поддержке конкретной версией bot/tester. Имена
physical fields изолированы в adapter, не выдумываются из пожеланий.

Общие runtime fields проверяются на совместимость. Неподдержанный mixed-TF или
конфликт closing/settings не решается копированием одной стороны поверх другой.
После рендера выполняется обратное typed-сравнение; запрещены неподтверждённые
изменения immutable settings и молчаливое отключение выбранной стороны.
Capability для mixed-TF, opposite opening и dedicated close привязана к
binary/adapter fixture manifest. `UNKNOWN` трактуется как unsupported: ось
исключается из поиска с причиной, удобный default не подставляется.

### 8.3. Runner

Portfolio mode существующего тестера проверяется отдельно от индивидуального
`SINGLE_MODE`. Сначала capability/report fixtures, затем только с явным
разрешением пользователя ограниченный реальный прогон. Нельзя использовать
непроверенный `--help` или запуск executable для исследования формата.

Текущие panel registry и output-CSV lock не дают target-wide cross-process
гарантию. До любого реального optimizer run общий runner получает один
cross-process lock по resolved tester target, и тот же primitive обязаны брать
panel, RETEST, CLI и optimizer до изменения конфигов/стратегий/reports/process.
Lock хранит PID, process-start, host/machine и boot/container-instance identity.
Foreign/unknown host или boot, живой или неидентифицируемый owner — preflight
stop. Reclaim разрешён только на том же host/boot для доказанно мёртвого PID с
проверенной start identity. Для непроверяемого owner применяется тот же
`LOCK_OWNER_UNVERIFIABLE` и manual ownership-attestation contract §5.5;
автоматического bypass/clear нет. Чужой процесс не завершается. Tester-target lock
не заменяет Portfolio DB lease; DB transaction не держится во время ожидания
tester lock. Отдельный уже
предоставленный remote tester допускается; развёртывание машины не требуется MVP.
Один instance не обслуживает два меняющих его конфиги задания одновременно.

Run manifest перечисляет exact ожидаемые strategy/config/report artifacts,
включая каждый разрешённый HTML report, и связывает их с run_id/attempt_id,
expected members и digest. Удаляемым считается только сматченный report внутри
зарегистрированного run-owned каталога; несовпавший файл остаётся с warning.
Пути берутся из проверенного config, не из произвольной строки HTML.
Временно изменяемые strategy/tester/account settings сохраняются и восстанавливаются
после успеха, ошибки и отмены. Secrets в campaign/deployment/log не попадают.
Предпочтителен изолированный тестовый аккаунт/config, не live trading config.

Запрещены безусловные очистки общих каталогов до запуска. Чужие файлы или
неподтверждённое владение — остановка preflight, не разрешение их удалить.
Не останавливать чужой bot/process/listener. Remote adapter обеспечивает те же
ownership/restore/path/hash свойства и не ослабляет их ради транспорта.

Runner проверяет ожидаемый набор reports, завершённость и свежесть, имена и
настройки всех members, exact periods, limiter/account settings по manifest
и доступному embedded evidence. Неподтверждённая семантика не объявляется
проверенной из-за отсутствия поля в HTML. Timeout/retry/cancel сохраняют
attempt identity; неполный run не ранжируется как успешный.

## 9. Phase 1 — импорт и результат

### 9.1. Report data contract

Portfolio parser принимает фактическую схему report и сохраняет:

- balance/equity boundaries, net PnL, DD, fees/funding, volume;
- portfolio и symbol summary: gross profit/loss, PF, recovery, если доступны;
- все execution rows: timestamp, stable source index, symbol, order ID,
  side/action, size, price/cost, fee/PnL/balance, post_size/post_side — по наличию;
- wallet/equity, margin-balance и notional series с точными timestamps,
  availability и разрешением наблюдения.

Не вся колонка обязана существовать в любом report. Phase 0 фиксирует mapping
и обязательный набор для каждой производной метрики; missing price/funding/
notional не синтезируется. Объявленное число actions сверяется с parsed count.
`Trades` сохраняет исходную execution-семантику, не называется числом positions.
Source summaries и proxy метрики не подменяют actual joint results.

### 9.2. Position cycles и временные границы

Cycle начинается при переходе flat→nonzero и заканчивается nonzero→flat.
Внутри увеличения и сокращения могут чередоваться. Поддерживаются partial
fills, стабильный порядок одинаковых timestamps, carry-in/open-at-end и
отдельная диагностика unexpected reversal. Незакрытый cycle не удаляется и
не получает выдуманную дату выхода. Dedicated/forced close классифицируется
только при наличии доказуемых полей, иначе UNKNOWN.

Сохраняются opened/closed, duration/censoring, realised PnL/fees, максимальная
наблюдённая позиция, число исполнений. Funding без attribution к symbol/cycle
остаётся account-level. Requested/filled ratio не выводится из filled Size,
если requested quantity нельзя однозначно восстановить.

Основной portfolio result равен `final_equity - initial_equity`; поэтому
unrealised PnL открытой на границе позиции не исчезает. Realised PnL хранится
отдельно, `OPEN_AT_END` остаётся diagnostic и сам по себе не блокирует READY
при полной boundary/equity evidence и пройденных guards. Наблюдаемый DD
считается по той же полной equity path, не сумме closed PnL. Потерянные
ticks между samples не объявляются измеренным минимумом. Unrealised PnL на
границах, fees, funding и warm-up входят по явному metric contract с reconciliation;
они не вычитаются повторно и не переносятся из будущего окна.

### 9.3. Transactional import и cleanup

Parse → identity/schema/count/range/financial consistency → одна DB transaction
→ readback normalized facts → COMMITTED. Финальная торговая история доступна
целиком либо не опубликована; failed sibling не превращает частичный
портфель в полный результат. Повтор report digest не создаёт duplicate actions.

Удаление собственного raw HTML разрешается только после committed/readback,
проверенного manifest и отдельно принятого portfolio-specific safe-delete
контракта. Он обязан доказать ownership по run/attempt/report digest, полноту
всех replay-required normalized facts и зафиксированные parser/metric versions.
Контракт portfolio report/schema должен быть утверждён, а не заимствовать
`schema_version=4` как доказательство полноты другого формата. До этого gate
автоматическое удаление выключено. Ошибка parse/import сохраняет evidence;
ошибка cleanup после commit — warning, не откат подтверждённого результата.

Успешный результат хранится без обязательного постоянного HTML после safe-delete.
Отчётные настройки, нормализованные факты и provenance достаточны для decision
replay; raw HTML, binary, ticks не коммитятся в репозиторий.
Новая версия parser не переписывает старые normalized evidence без доступного
raw report; до принятия этих правил удаление остаётся выключенным.

## 10. Phase 1 — типы портфеля, validation и deployment

### 10.1. Цели типов

| Тип | Цель после одновременного прохождения всех ограничений |
| --- | --- |
| AGGRESSIVE | максимизация net profit в допустимой нагрузке и с минимальным заданным запасом |
| BALANCED | согласованный компромисс PnL, DD и margin risk |
| CONSERVATIVE | минимизация риска при минимально приемлемом net profit |

### 10.1.1. Стартовая `portfolio_optimizer_research_risk_v1` для исследования

Следующие значения являются defaults именно для исследования и калибровки. Они
не дают автоматического допуска к торговле, не ослабляют другие gates и могут
быть изменены только новой versioned policy/Campaign. Их формулировка вынесена
в [ADR-0029](../decisions/0029-portfolio-optimizer-research-risk-profile-v1.md):

| Ограничение | AGGRESSIVE | BALANCED | CONSERVATIVE |
| --- | ---: | ---: | ---: |
| `max_actual_equity_dd_pct` | 20% | 10% | 5% |
| `min_calculated_free_margin_reserve_pct` | 20% | 40% | 60% |
| `max_calculated_account_mm_load_pct` | 50% | 35% | 20% |

`actual_equity_dd_pct` — maximum peak-to-trough DD из полной actual joint
equity series TradingRun: положительный процент падения от running peak. Он
считается по всей доступной series без сокращения окна, resampling, synthetic,
extrapolated или per-symbol aggregated substitute. Missing, truncated или
non-joint series даёт `UNKNOWN`, не PASS. Для каждого рассчитанного
margin-envelope state с корректным `MarginBalance` (с применимыми Haircut/Order
Loss) используются:

```text
calculated_free_margin_reserve_pct =
    (MarginBalance - calculated_total_IM) / MarginBalance * 100
calculated_account_mm_load_pct =
    calculated_total_MM / MarginBalance * 100
```

Margin guards берут minimum reserve и maximum MM load по полному проверяемому
race envelope, включая conservative bound. Каждый state использует один
согласованный snapshot: timestamp, account currency, `MarginBalance`,
`calculated_total_IM` и `calculated_total_MM` из одного источника. Это не
подмена envelope наблюдённой actual equity series. `MarginBalance` обязан быть
положительным числом той же currency; null, stale или currency mismatch дают
`UNKNOWN`. Reserve может быть отрицательным и ниже profile minimum означает
`FAIL`.

Каждый guard возвращает `PASS`, `FAIL` или `UNKNOWN`. Любой missing, invalid,
stale или unverified input (equity series, IM, MM, MarginBalance, timestamp,
currency) даёт `UNKNOWN`, который никогда не повышается до PASS. Profile PASS
возможен только когда все три guard PASS в одном Evaluation; иначе итог FAIL
или UNKNOWN. Thresholds fixed per named profile: запрещены automatic relaxation,
interpolation/blending, derived intermediate profiles, fallback к другому или
default profile, а также per-run override. Любое отклонение — policy violation,
не passing result. Изменение threshold, formula, verdict rule или profile set
требует новый policy ID и ADR; `portfolio_optimizer_research_risk_v1`
неизменяем, а каждый результат хранит применённый policy ID.

Эти три лимита сами по себе не создают
`RECOMMENDATION_READY`: до него остаются обязательными PnL, liquidity/freshness,
exact ranking, validation, capabilities и отдельные разрешения. Числа,
минимальный PnL, точное Balanced/Conservative ранжирование, near-tie и duration
limits, кроме трёх строк таблицы, остаются OPEN-POLICY.

`each_strategy_max_dd_pct` — четвёртое, отдельное profile поле для pre-tester
ограничения размера одной стратегии по §6.2.1. Его численные значения ещё
OPEN-POLICY; оно не заменяет `max_actual_equity_dd_pct` и не меняет принятую
таблицу `portfolio_optimizer_research_risk_v1`.

Ограничения и ranking хранятся в profile config; универсальной DD-first
сортировки для всех типов нет. No feasible candidate — явный результат,
а не автоматическое ослабление риска. Исторический PnL/30d не прогноз доходности.
Открытый ranking относится только к profile selection/recommendation ordering и
не изменяет, не переупорядочивает и не перераспределяет фиксированные thresholds.
Прохождение research thresholds не разрешает implementation, tester run,
`RECOMMENDATION_READY`, trading admission или live use; все remaining gates
(PnL floor, liquidity/freshness limits, profile ranking) остаются open blockers.

### 10.2. Базовая защита от подгонки — уже MVP

До поиска фиксируются development и untouched validation periods, upstream
период отбора стратегий, список гипотез, ranking и budget. Development ranking
задаёт полный порядок: versioned ordered metrics из OPEN POLICY и canonical
candidate identity как последний tie-break. Metric list/version/tie-break
хранятся именно в decision Campaign, входят в её canonical content identity и
используются decision replay. Новая decision Campaign заново фиксирует свою
ranking version; молчаливое наследование другой версии запрещено.
На validation
не подбираются состав, shifts, scalar, L, priority, thresholds и cap.

Если индивидуальные кандидаты уже выбирались с использованием validation,
он называется повторной проверкой на использованной истории, не независимым OOS.
Для настоящего OOS нужен позднейший untouched период либо повтор полного
upstream selection только на development. Короткая история/мало cycles —
INSUFFICIENT_EVIDENCE; точные минимумы согласуются отдельно.

Warm-up и state на границе задаются заранее. Carry-in нельзя убрать, сохранив
его прибыль: фиксируются initial wallet/equity/positions и правила attribution.
Baseline cold-start и continuation не смешиваются в одном сравнении.
Точный протокол и минимальные окна — gate M0, не скрытая реализация.

На validation проверяется заранее замороженный winner/набор финалистов по
заранее заданному PASS/FAIL acceptance rule. Порядок и tie-break финалистов
заморожены на development; если прошли несколько, выбирается первый в этом
порядке, без сортировки по validation metrics. Выбор нового победителя по увиденным
validation returns — новая оптимизация, а не независимая проверка. После
изменения политики требуется новый untouched evidence; провал не маскируется.
Если не прошёл ни один frozen finalist, profile decision и decision Campaign получают
`INSUFFICIENT_EVIDENCE/NO_VALIDATION_PASS`; отдельный invalid TradingRun status
сохраняется вместе с этой причиной и не заменяется ею.
Все experiments, включая неудачные и повторные, сохраняются в Portfolio DB.
Расширенные walk-forward/multiple-testing методы — Phase 7.

### 10.3. Выходы и статусы

Пользователь получает manifest/JSON по symbol и сопроводительный отчёт:
тип портфеля, депозит/cap, selected IDs и TF, scalars и rounded quantities,
leverage, limiter/priorities/opposite policy, net PnL/DD/recovery, margin/liq
guards, validation status, периоды и причины выбора/отказа, model limitations.

Исследовательский результат может быть `RESEARCH_ONLY`. Финальный
`RECOMMENDATION_READY` требует утверждённых policies, complete committed joint
test, требуемого validation evidence, пройденных risk/liquidity gates и
совместной liquidity-проверки всего PortfolioSet. UNKNOWN не равен PASS.
Composition/load digest PortfolioSet входит в manifest/evaluation. Любое
изменение состава сохраняет underlying account TradingRuns, но переводит ранее
готовые общие рекомендации в `NEEDS_RESCREEN` до нового shared screen.

Перед export обновляются exchange reference и liquidity freshness. Generated
package должен совпадать с протестированным по исполняемым настройкам.
Изменение leverage, quantity после rounding, geometry, policy бота или других
влияющих на торговлю полей требует нового test/validation, а не замены JSON
в старом winner. Обновление не влияющего на исполнение reference допускает
новую оценку gates с сохранением provenance. Stale обязательные данные
блокируют READY, но могут сопровождать явно неготовый исследовательский export.

Никакой автоматической установки в live bot, открытия/закрытия позиций или
переводов средств. Отсутствие исторической ликвидации не гарантирует будущую
безопасность; «консервативный» — относительное имя внутри стратегии без стопов.

### 10.4. DoD Phase 1

- Immutable campaign snapshot воспроизводит решение после изменения source DB.
- Есть несколько portfolio compositions, sizes, priorities и L, включая
  disabled, ограниченный режим и exempt pairs, а не только один happy-path.
- Проверены mixed directions/TF capability gates, rounding/tiers/capacity,
  динамическая sizing база ниже/выше cap и account isolation.
- Проверены normal и multi-overflow states, partial fill/close и pending cancel;
  UNKNOWN и ресурсный отказ не принимаются за margin PASS.
- Реальный разрешённый portfolio test и report fixture подтверждают целевой
  режим; single-member контроль согласуется с сопоставимым одиночным тестом.
- Atomic import/replay/failed-run recovery и safe cleanup подтверждены отдельно.
- У каждого типа есть явные policies и фактический validation disposition;
  отсутствие подходящего портфеля корректно отображается.
- Повторяющаяся пара проверяется совместно на нескольких независимых счетах.
- Exact deployment reproducible; изменившийся исполняемый payload не выдаётся
  со старым successful run ID без нового evidence.
- Пропорциональные tests, independent review и acceptance ledger выполнены
  перед признанием реализации завершённой; сейчас эти пункты не выполнены.

## 11. Phase 2A — advanced liquidity и margin research

**Вход:** работающий Phase 1 хотя бы в статусе `RESEARCH_ONLY` и новые факты
исполнения. Phase 1 READY не является prerequisite. **Не prerequisite MVP:**
точный queue/fill simulator, live order lifecycle и полная UTA replica.

Scope: requested/filled, time-to-first/full-fill, partial count/remaining при
cancel/replace, lot-degradation curves; capacity по symbol/side/TF, затем
сессиям. Полный placement/replace/cancel/reduce-only lifecycle позволяет
уточнить Active Order IM вместо conservative envelope.

Уточняется работа с Mark/Order Loss, collateral haircuts, borrow, close fees
и при отдельном расширении scope hedge-mode. Неизвестные необходимые поправки
MVP всё равно обязано маркировать, откладывание точности не разрешает их игнорировать.
Добавляются correlated price/equity shocks, снижение liquidity/leverage и
одновременный deepest fill. Multi-overflow уже проверяется в MVP.

**Выход/DoD:** сравнение прежнего proxy и измеренного исполнения, versioned
calibration с диапазоном применимости; missing requested не даёт fill ratio;
изменение capacity/size снова проходит общий test. Новая модель улучшает
проверяемый результат, а не задним числом исправляет прошлый backtest.

## 12. Phase 2B — read-only Live Account Monitor

**Вход:** явный deployment manifest и отдельно разрешённый доступ к нужному
account. Состояние берётся с private REST wallet/positions/orders, затем
wallet/position/order/execution WS с REST reconcile при reconnect и периодически.
Тестер не источник истины о live позициях.

Dashboard: wallet/equity/margin/available balances, realised/unrealised PnL,
Total IM/MM и account rates, gross exposure, открытые counted/exempt позиции,
limiter, connection/reconciliation freshness. По symbol: selected IDs,
side/size/entry/mark/leverage, orders/fills, fees/funding и reference state.
Графики account и symbol paths, event markers, фактические результаты против
test baseline; liquidity-based предложение изменения размера — только рекомендация.

Watchdog: при counted>L — LIMITER_OVERFLOW; восстановление в заданный grace
period — LIMITER_RECOVERED; оставшееся превышение — LIMITER_BREACH и alert.
При L=0 нет нарушения по количеству; exempt не считаются в L, но видны в марже.
Хранятся начала/концы, involved symbols/priorities, expected/actual closes и
availability. Grace/alert thresholds — отдельные versioned settings.

Margin/connection/config drift alerts включают unexpected symbol, изменённые
leverage/order size/settings, stale references и несогласованное состояние.
Отсутствие позиции само по себе не доказывает отсутствие стратегии в боте.
Missing/stale API snapshot не считается здоровым пустым счётом.

Storage live-подсистемы выбирается отдельным уточнением до реализации;
Performance DB не используется. Read-only permissions, secrets вне DB/log,
notification transport без trading credentials. Никаких emergency close API.

**DoD:** fixtures REST/WS/reconcile не дублируют executions/PnL, проверены
disconnect/stale/overflow/exemption и false alarms; никакие сценарии Monitor
не выполняют торговых команд. Любая future emergency action требует нового scope.

## 13. Phase 3 — time/session analysis

**Вход:** versioned position cycles, liquidity history и market calendar.
US equity underlying размечается в `America/New_York`, с DST, weekends,
holidays и shortened sessions. Базовые окна: 04:00–09:30 premarket,
09:30–16:00 regular, 16:00–20:00 postmarket и вне extended session;
реальный календарь переопределяет обычный день. Для commodities требуется свой
календарь, не автоматическое применение US-stock расписания.

Диагностика по режиму: independent entries/cycles, net PnL, DD, worst cycles,
duration median/p90/p95/max, deepest fill, position size, зависания и
spread/depth на входе. Доступность/количество samples и consistency нескольких
периодов обязательны; min evidence фиксируется до исследования.

Отдельные configurable boundary windows вокруг открытия/закрытия сессий;
произвольный поиск лучшей минуты суток не допускается. Policies ALWAYS_ON,
NO_WEEKEND_ENTRIES и другие evidence-backed варианты запрещают только новые
входы, не бросают сопровождение открытой позиции. Поведение добора уровней
в уже открытой позиции уточняется явно в entry-policy contract.

**DoD:** DST/holiday/short-session fixtures, INSUFFICIENT_EVIDENCE при малой
выборке, сравнение с ALWAYS_ON в совместном test и untouched validation.
Индивидуальное улучшение не признаётся улучшением портфеля без joint retest.

## 14. Phase 4 — dependency и robustness

**Вход:** сопоставимые независимые и portfolio series/cycles. Scope: overlap
позиций/entries/deepest fills, simultaneous losing periods, long-hold overlaps,
downside dependence и общие asset/sector/direction факторы. Одна корреляция
итогового PnL не является достаточным критерием диверсификации.

Тесты соседних sizes/deposit/L, замены одной стратегии, priority/composition
и позднее time policy выполняются в ограниченном заранее заданном наборе.
Предпочтение устойчивой области подтверждается сравнением, не названием метода.

**DoD:** matrix/group diagnostics с missing-data flags и периодами, воспроизводимые
perturbation experiments, проверка чувствительности winners вне search period;
никаких новых shifts вне pretested universe.

## 15. Phase 5 — degradation analytics

**Вход:** reconciled live evidence Phase 2B и frozen test baseline. Сравниваются
frequency, PnL/cycle, holding, deepest fill/partial fills, actual sizes, DD,
liquidity, limiter events и IM/MM trajectories в сопоставимых окнах.

Versioned recommendation states: NORMAL, WATCH, REDUCE_RISK, NO_NEW_ENTRIES,
RETEST_REQUIRED. Thresholds, minimum samples, missing-data и переходы
утверждаются отдельно; UNKNOWN не переводится автоматически в NORMAL.

**DoD:** воспроизводимые переходы на fixtures улучшения/ухудшения/пропусков,
объяснение причин, защита от дребезга состояний; рекомендации не меняют bot config.

## 16. Phase 6 — controlled rotation

Новые finalists сравниваются с текущим manifest. Replacement проходит общий
test/validation и refreshed capacity/margin gates. Ограниченный trial sizing,
наблюдение перед увеличением и REDUCE_RISK/NO_NEW_ENTRIES для деградирующих
компонентов описываются как рекомендации, исполняемые пользователем.

**DoD:** immutable old/new manifests, причины замены, совместный retest,
возможность восстановить прежние настройки без переписывания результатов;
это не обещание экономически откатить уже исполненные сделки. Автоматическое
развёртывание/торговля требует отдельного решения.

## 17. Phase 7 — advanced validation и управление несколькими портфелями

Базовый development/validation gate уже входит в MVP. Здесь — walk-forward,
учёт числа trials/selection bias и специальные методы переоптимизации при
достаточной истории. Дополняются tail loss, conditional DD, contributions и
оценка числа независимых источников риска.

Multi-account development: уточнённая global capacity вместо грубого MVP
screen, общие exposure diagnostics, hidden risk duplication, размещение новых
стратегий по счетам и сравнение keep/replace.

**Отложенная идея:** рекомендовать распределение заданного общего капитала
между AGGRESSIVE/BALANCED/CONSERVATIVE с учётом risk/capacity. В MVP депозиты
задаёт пользователь. Это не перевод средств и не объединение Cross collateral.

Profit policy research: accumulation с cap, изъятие суммы выше порога,
cascade из более агрессивного в менее рисковый портфель. Раздельно считаются
equity каждого счёта, защищённая/выведенная прибыль, transfer cash flows и
общий капитал без двойного счёта. Изъятие уменьшает margin buffer и требует
повторного расчёта; момент изъятия при открытых позициях — отдельный контракт.

**DoD:** отдельная принятая спецификация численных policies и cash-flow
semantics, воспроизводимые сценарии, joint retests после изменения deposit/size,
нулевое двойное включение transfers в прибыль и отсутствие trading permissions.

## 18. Открытые вопросы и capability ledger

| ID | Что ещё определить | Блокирует |
| --- | --- | --- |
| Q01 | физические dual-TF fields и список общих LONG/SHORT runtime fields | mixed-TF renderer и соответствующий test |
| Q02 | set-leverage через bot, допустимые tiers/steps и mismatch handling | final leverage/deployment |
| Q03 | exact limiter storage, частота реакции, priority order/ties, versioned overflow bound | limiter adapter и точность margin envelope |
| Q04 | обработка opposite opening fill в one-way, cancel_opposite, partial orders | active-order reserve и faithful renderer |
| Q05 | balance/equity sizing basis, moment, resting resize, risk_* mapping, disabled cap encoding | sizing model и динамические guards |
| Q06 | portfolio mode/config и фактические report fields/series, fees/funding/close attribution | portfolio adapter, metrics и safe deletion |
| Q07 | PnL/liquidity/freshness policies, `each_strategy_max_dd_pct` и exact Balanced/Conservative ranking; DD/free/MM имеют только research defaults §10.1.1 | READY, не fixture development |
| Q08 | research windows, state/warm-up boundary, minimum evidence и upstream OOS provenance | независимая validation и финальный статус |
| Q09 | достаточность selection snapshot, tick/binary identity и безопасное чтение concurrent Performance DB | воспроизводимость campaign/run |
| Q10 | target local/предоставленный remote, ownership и restore contract | разрешённые реальные runs |
| Q11 | минимально доступный collateral/order-loss/mark reserve contract и bound при missing facts | доказуемость margin guards |
| Q12 | coarse global liquidity aggregation/threshold и position/exit diagnostic | shared-liquidity gate PortfolioSet |

Capability ledger различает три исхода: `BLOCKING_UNKNOWN`,
`APPROVED_CONSERVATIVE_BOUND` и `CONFIRMED_CAPABILITY`. К началу M2 допустимы
не все закрытия Q01–Q12: достаточно закрыть M0/M1 и иметь working result Phase 1
в `RESEARCH_ONLY`. До `RECOMMENDATION_READY` обязаны быть закрыты Q07–Q12 и
все используемые ветви Q01–Q06. Неизвестные denominator/equity path,
instrument/tier/qty/price identity, sizing/limiter semantics, обязательное
качество liquidity evidence и применимые collateral/borrow facts всегда
остаются `BLOCKING_UNKNOWN`. Conservative bound допустим только с сохранёнными
формулой, числом, provenance и перечнем покрытых состояний.

Q03 не переоткрывает согласованные L=0, count-by-pair, exempt priority=0,
снятие других openings при L и возврат после flat. Q04 не переоткрывает
целевое отсутствие одновременных LONG+SHORT positions. Требуются точные
adapter evidence, а не повторное голосование о пользовательском поведении.

Пожелания разработчику tester: self-contained limiter/priority/version metadata
в HTML; limiter events с count before/after, symbol, priorities, выбранным
forced-close, временем и результатом исполнения. Отдельное requested quantity
поле не обязательно, если оно однозначно восстанавливается из подтверждённого
sizing contract. Без этого нельзя заявлять точный fill ratio.

Не требуется переделывать tester ради собственного liquidity collector,
risk-limit snapshots, Bybit formula engine или новой fee/funding подсистемы.
Закрытие вопросов отражается изменением этой спецификации и evidence ledger,
а не неявным предположением в коде. Новые поля/capabilities должны иметь fixture.

## 19. D7 — Panel и развитие config

Интерфейс, Campaign jobs, Settings CAS и XLSX регулируются отдельной
[спецификацией Panel](2026-09-06-portfolio-optimizer-panel-ui.md) и
[ADR-0031](../decisions/0031-portfolio-optimizer-panel-ui-and-campaign-boundary.md).
Эта основная спецификация и ADR-0030 имеют приоритет над UI-спецификацией и
ADR-0031 при любом конфликте о допуске, ликвидности, leverage, sizing и
индивидуальном DD; D7 сохраняет соответствующий смысл D6 без изменений.

Текущая строгая схема config v1 остаётся нормативной. Необязательный
`max_balance_usdt` в запуске Campaign не является config-полем. Nullable или
отсутствующий `scenarios[].max_balance` и UI defaults требуют отдельной принятой
схемы v2, миграции и поддержки parser.

Этап 1 фиксирует входы и config digest в неизменяемом Campaign и не запускает
совместный тестер. Канонический отказ этапа 2 —
`PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED`; сокращённая форма не используется. Этап
2 закрыт до принятых M5/M6 и отдельного явного разрешения. U0/U1 не меняют
порядок M-задач: уже принятый M2 не зависел от UI,
а после независимой приёмки M3–M4 следующим серверным этапом остаётся M5. Готовность UI не закрывает
PnL/liquidity/freshness/ranking policies и не разрешает tester, рекомендации,
торговый допуск или live use.

## 20. Приёмка документации и дальнейшая работа

Полнота консолидации: MVP implementation design, advanced liquidity/margin,
Live Monitor, sessions, phases 4–7, collector integration, developer wishes,
исходная концепция и уточнения config/replay распределены по разделам выше.
Исходное ТЗ collector заменено уже действующими spec/ADR, а не дублировано.
Численные предложения, кроме явно принятой стартовой
`portfolio_optimizer_research_risk_v1`,
неподтверждённая точность и запрещённые cleanup defaults не становятся
требованиями при переносе.

Документационная проверка включает существование ссылок, фазовую трассировку
spec→plan, отсутствие зависимости от прежней рабочей папки и отсутствие
ложных отметок PASS/Implemented. Независимый Opus review редакции D4 вернул
`PLAN_APPROVED` для архитектурной документации и D5 policy amendment. Это не
`CODE_REVIEW_PASS`, не implementation authorization и не production safety
evidence; обязательные policy/capability gates остаются открыты.

Для D6 дополнительно проверяются: docs-only diff без runtime/tester/live files;
ссылки на ADR-0030 из spec/plan/PRD/progress/AGENTS; exact `FINALIST` universe;
наличие `each_strategy_max_dd_pct` как OPEN-POLICY без нового числа; сохранение
открытыми PnL/liquidity/freshness/ranking blockers. Упомянутые в плане M2/M3/M4
tests — будущие описания acceptance tests, не test code этой редакции.

Для D7 дополнительно проверяются: единый API-контракт Panel; точный отбор по
`User Status=FINALIST` и усечение только по `User Rank`; неизменяемость Campaign;
CAS и строгая схема настроек; сохранение заданий с restart→`INTERRUPTED`;
успешная публикация XLSX только при `SUCCEEDED`; отсутствие путей и секретов вне
Settings GET/PUT; постоянный gate этапа 2 до M5/M6 и отдельного разрешения.

Справочные первичные источники для будущего venue adapter:
[Bybit UTA formulas](https://www.bybit.com/en/help-center/article/?id=000001912),
[liquidation process](https://www.bybit.com/en/help-center/article/UTA-Trading-Rules-Liquidation-Process),
[instruments](https://bybit-exchange.github.io/docs/v5/market/instrument),
[risk limits](https://bybit-exchange.github.io/docs/v5/market/risk-limit),
[tickers/24h turnover](https://bybit-exchange.github.io/docs/v5/market/tickers),
[account fee rate](https://bybit-exchange.github.io/docs/v5/account/fee-rate).

## D8 amendment: adapter/core contract and config schema v2

This amendment is normative for the changed rules below and is linked to
ADR-0033. ADR-0030 remains historical and unchanged for every rule that this
amendment does not explicitly replace. The legacy adapter behavior described
below applies only when an explicitly legacy path is selected; current Panel
Campaigns use the Stage 1 PRETEST_PROXY amendment at the end of this
specification. The implementation boundary remains fixture/research-only; it
does not authorize a tester run, recommendation, trading admission, or live
use.

### Config v2

`schema_version` is `2`. v2 is strict: unknown keys at the document,
scenario, search, liquidity, profile, ranking, input, or runner object levels
are rejected. A v1 document is accepted only through deterministic in-memory
migration. Migration changes the version to 2, removes the legacy monetary
`scenarios.<name>.sizing.grid`, inserts the required fields below, and
preserves every other accepted value. The v1 source bytes remain available for
CAS; Panel Save writes the migrated v2 document.

The active sizing mode is the fixed `search.sizing_mode =
liquidity_cap_single`. `search.max_enumerated_combinations` defaults to
`100000`. Each scenario keeps one deposit, collateral, max balance, and sizing
upper bound. There is no active monetary sizing grid.

Each profile has configurable `individual_max_dd_pct` with v1 migration
defaults of 30, 20, and 15 for AGGRESSIVE, BALANCED, and CONSERVATIVE, plus
`individual_net_pnl_min_exclusive` defaulting to 0. The preliminary ranking
metric order is fixed per profile:

| Profile | Ordered metrics |
| --- | --- |
| AGGRESSIVE | `net_pnl DESC`, `recovery_factor DESC`, `max_dd_pct ASC` |
| BALANCED | `recovery_factor DESC`, `net_pnl DESC`, `max_dd_pct ASC` |
| CONSERVATIVE | `recovery_factor DESC`, `max_dd_pct ASC`, `net_pnl DESC` |

The global liquidity settings are `liquidity.parameters.close_volume_participation_pct`
(integer 1..200, default 30), `round_down_usdt` (default 50),
`maximum_age_hours` (default 2) for the market reference snapshot,
`weekend_start_utc` (default `SATURDAY 00:00`),
`weekend_end_utc` (default `MONDAY 00:00`), each configurable as a valid
weekday and `HH:MM` UTC pair with a non-empty weekly interval, and
`archive_publication_lag_hours` (integer
0..48, default 6), and `backfill_write_enabled = false`. The accepted local
input `inputs.bybit_minute_data_root` defaults from the configured tester root
to its `data/bybit` directory. With the default `backfill_write_enabled =
false`, missing archive data is reported without writes. If explicitly enabled,
an adapter may atomically publish a downloaded complete daily CSV; tests use an
injected fetcher and temporary output root.

### Liquidity and individual gates

Liquidity is calculated over the full accumulated directional position: every
opening, averaging, and DCA increase contributes to the maximum absolute
directional position before it returns flat. The largest closing Limit
quantity is also recorded and must fit the same published capacity evidence;
closing orders cannot bypass the gate. The MVP emits one member per profile,
with its size equal to the calculated rounded-down full-position cap. It emits
no 50/75/100 variants and no partial-close recommendation. Multi-size
calibration remains deferred Phase 8.

The individual gate reads the report's direct `max_dd_pct` value and requires
`max_dd_pct <= profiles.<PROFILE>.individual_max_dd_pct`. It is independent of
the portfolio-level research policy: actual joint DD remains 20/10/5,
free-margin reserve remains 20/40/60, and MM load remains 50/35/20. A report
also passes the profile PnL gate only when
`net_pnl > individual_net_pnl_min_exclusive`; source PnL is never presented as
portfolio PnL.

### Candidate and reference determinism

The future multi-pair `PortfolioCandidate` identity is the sorted tuple of
canonical pair/direction member identities, followed by the fixed profile and
scenario identities. Pair order is never an incidental input order. One
Campaign freezes exactly one validated market-reference snapshot and digest;
all candidate, leverage, quantity, and capacity calculations in that Campaign
use that snapshot. A changed snapshot creates a new decision Campaign.

The selected pair list is the candidate universe. For each selected symbol,
search includes an explicit empty choice, so every candidate contains any
non-empty subset of the selected symbols, from one pair through all selected
pairs; a symbol may also contribute its admissible mixed LONG/SHORT option.
Symbols with no usable finalist direction options are skipped. The search
returns `INSUFFICIENT_DIRECTIONAL_UNIVERSE` only when no usable option remains
across the entire selected universe. With `k_s` non-empty options for symbol
`s`, the full universe count is exactly
`product(1 + k_s) - 1`. In the active contract every candidate is returned
when this full count is within `max_enumerated_combinations`;
`max_candidates` is retained on the Campaign profile for future selection
after joint tests and never truncates the pre-test composition universe.
Enumeration sorts symbols and options, places the empty choice first for each
symbol, uses product order, and omits the all-empty composition.

The Panel is a thin facade. It owns HTTP validation, Settings CAS, immutable
Campaign capture, job lifecycle, progress, and artifact delivery. The
`src/mrs3/portfolio` package owns migration, candidate identity, sizing,
liquidity, gates, ranking, and all calculations. Panel does not duplicate those
algorithms or write PerformanceDB.

## Stage 1 PRETEST_PROXY contract (2026-09-10)

Stage 1 searches bounded PRETEST_PROXY compositions using current
PerformanceDB equity paths. It reads exact current `FINALIST` rows, their
initial balances, actions, and equity series through one read-only bulk
snapshot. Effective report intervals intersect into one UTC whole-day period;
the period requires at least 14 days. Current-result equity is a sparse
observation series: the last observation at or before each boundary is carried
forward, including the terminal boundary. A prior observation or a positive
source initial balance must seed the first boundary; future observations never
seed earlier boundaries. Observation density and maximum gap are preserved as
diagnostics and do not reject an otherwise usable current result. Period
exclusions and zero-activity windows are persisted as evidence.

For a source member, tested size is
`source_initial_balance_usdt * sum(original opening lot_x)`. Its scaled daily
increment is `(equity - source_initial_balance) * actual_size / tested_size`.
Portfolio equity is campaign equity plus the sum of member increments. This
linear scale is labelled `PRETEST_PROXY` and
`LINEAR_SCALING_ASSUMPTION_UNVERIFIED`; balance-percentage and risk are
`UNKNOWN`, and joint metrics are `NOT_TESTED`.

Individual DD and ranking `top_n` values are compatibility fields and do not
filter Stage 1 candidates. Each symbol has one shared full-position capacity.
Members receive equal initial shares, exchange-rounded quantities, and
deterministic canonical residual steps. Uniform `k` is chosen from portfolio
DD and margin reserve constraints and is recomputed after rounding; one ratio
correction is allowed.

The mandatory search budget is `sum(k_s) + sum(k_s*k_t)`, where
`k_s = n_long + n_short + n_long*n_short`. It covers every singleton and
two-symbol composition; higher cardinalities use the remaining bounded
budget. `max_candidates` returns only the top N preliminary candidates for a
future joint tick test. Stage 1 never recommends a strategy and never invokes
the tester.

The built-in PRETEST_PROXY evaluator may execute in a bounded process pool.
Its width comes only from the machine-wide `config.local.json`
`duckdb_import.workers` value (16 when the Panel has no such setting); it is a
scheduling input and is absent from Campaign identity and the Portfolio UI.
Arbitrary injected evaluators remain serial. Workers return results to one
ordered parent commit path, so worker completion order cannot change candidate
identity, budget accounting, ranking, or output. Search retains compact member
and metric facts; source equity/actions are stored once and restored only for
the final shortlist before minute refinement and artifact output.
При реализации проверяется актуальная версия и фиксируется reference date;
эти ссылки не заменяют сохранённые campaign facts и не задают наши risk limits.
