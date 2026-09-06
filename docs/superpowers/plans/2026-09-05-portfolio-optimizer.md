# Portfolio Optimizer — план внедрения по фазам

**Дата:** 2026-09-05

**Версия:** D7.

**Статус:** `PLAN_APPROVED`; M0–M2 accepted after independent
`CODE_REVIEW_PASS` by Claude Opus 5 high. Runtime M3–M8 remain unstarted.

**D5 review:** `PLAN_APPROVED`.
**D6 review:** `PLAN_APPROVED` — Claude Opus 5 high.
**D7 UI review:** `PLAN_APPROVED` — Claude Opus 5 high.
**D7 UI docs review:** `CODE_REVIEW_PASS` — Claude Opus 5 high, two rounds.
**M0 review:** `CODE_REVIEW_PASS` — Claude Opus 5 high, three rounds.
**M1 review:** `CODE_REVIEW_PASS` — Claude Opus 5 high, five rounds.
**M2 review:** `CODE_REVIEW_PASS` — Claude Opus 5 high, four rounds.

**Спецификация:** [Portfolio Optimizer](../../specs/2026-09-05-portfolio-optimizer.md).

**Решение:** [ADR-0025, Proposed](../../decisions/0025-portfolio-optimizer-evidence-and-phases.md).

**Research risk policy:** [ADR-0029, Accepted](../../decisions/0029-portfolio-optimizer-research-risk-profile-v1.md).

**M2/M4 admission contract:** [ADR-0030, Accepted](../../decisions/0030-portfolio-optimizer-m2-admission-and-sizing-contract.md).

**Panel UI contract:** [UI spec](../../specs/2026-09-06-portfolio-optimizer-panel-ui.md),
[ADR-0031, Accepted](../../decisions/0031-portfolio-optimizer-panel-ui-and-campaign-boundary.md).

## 1. Назначение плана и текущая граница

План определяет порядок работ и проверок; требования, формулы, открытые
capabilities и DoD находятся в спецификации. Это не сокращённый MVP взамен
согласованного scope. Ни один пункт ниже не означает выполненную реализацию
или пройденный review. Контекст переписки не требуется: перед задачей
исполнитель читает указанные в ней разделы спецификации и фактический код.

- [ ] Согласовать новую спецификацию и архитектурную границу ADR-0025.
- [x] Получить `PLAN_APPROVED` от независимого reviewer после review D4.
- [x] Получить `PLAN_APPROVED` от независимого reviewer после review D5.
- [x] Получить `PLAN_APPROVED` от независимого reviewer после review D6.
- [x] Получить `PLAN_APPROVED` для документационного UI-контракта D7.
- [x] Закрыть или изолировать capabilities Q01–Q12 из §18 спецификации.
- [x] Зафиксировать `portfolio_optimizer_research_risk_v1` только для исследования и калибровки: AGGRESSIVE 20%/20%/50%, BALANCED 10%/40%/35%, CONSERVATIVE 5%/60%/20% (DD/free-margin/MM).
- [ ] Согласовать PnL/liquidity/freshness policies, `each_strategy_max_dd_pct` и exact Balanced/Conservative ranking до финальных рекомендаций.
- [ ] Получать отдельное разрешение перед реальными tester/bot runs.

Завершены документационная консолидация, M0 inventory и fixture-only M1–M2
contracts. `portfolio_optimizer_research_risk_v1` не
даёт READY без остальных policy/capability gates.
Прохождение research thresholds не разрешает implementation, tester run,
`RECOMMENDATION_READY`, trading admission или live use; все remaining gates
(PnL floor, liquidity/freshness limits, profile ranking) остаются open blockers.

## 2. Зависимости и правила реализации

- [PRD](../../../PRD.md) — границы и реестр; [progress](../../../progress.md) — статус/evidence.
- [Performance v2](../../specs/2026-08-28-unified-performance-analytics-v2.md),
  [RETEST](../../specs/2026-09-03-performance-v2-retest-workflow.md),
  [typed config](../../specs/2026-09-04-performance-v2-config-dedup.md).
- [Collector Revision 2](../../specs/2026-09-05-bybit-market-data-collector.md),
  [ADR-0024](../../decisions/0024-bybit-market-data-collector-archive.md),
  [его существующий план](2026-09-05-bybit-market-data-collector.md).
- Исторический v0.4 читается только в границе, указанной новой спецификацией;
  его собственная replay-симуляция не является заданием этого плана.

Работа ведётся в корневых `src/mrs3`, `tests`, `scripts`. Перед каждым изменением
поведения проверяется spec, пишется узкий failing test, затем реализация,
focused и relevant broader checks. Все тесты только из `.venv`.
Новый matching engine, полная копия Performance DB и второй collector не создаются.
Независимая тяжёлая подготовка — bounded parallelism, стандартно 16 workers;
DuckDB publication — single writer, один tester instance — один owner.

## 3. Карта переиспользования и предполагаемые файлы

Пути ниже проверены как существующие; совместимость функций не предполагается
по одному имени. M0 фиксирует чистый/read-only subset и необходимые adapters.

| Контур | Существующие точки |
| --- | --- |
| Performance schema/current facts | `src/mrs3/performance_v2_store.py`, `performance_v2_input.py` |
| Selection/review | `src/mrs3/performance_v2_selection.py`, `performance_v2_selection_review.py` |
| Window metrics/HTML/import | `src/mrs3/performance_v2_windows.py`, `performance_v2_html.py`, `performance_v2_import.py` |
| Typed rendering/RETEST | `src/mrs3/performance_v2_retest.py`, канонические `templates/strategies/` и `templates/tester/` |
| Runner | `src/mrs3/runner/config.py`, `files.py`, `process.py`, `http.py`, `monitor.py`, `inbox.py`, `workflow.py`, `report_library.py` |
| Collector reader/reference | `src/mrs3/bybit_collector/storage.py`, `archive.py`, `reference.py` |

Новые ответственности предлагается размещать внутри `src/mrs3/portfolio/`:
`config.py`, `input.py`, `store.py`, `canonical.py`, `liquidity.py`, `margin.py`, `search.py`,
`render.py`, `runner.py`, `reports.py`, `metrics.py`, `export.py`, `cli.py`.
Это распределение ownership, не требование создавать пустые файлы заранее:
границы уточняются в M0, небольшие соседние ответственности могут жить вместе.

Будущие focused tests: `tests/test_portfolio_config.py`, `test_portfolio_input.py`,
`test_portfolio_store.py`, `test_portfolio_liquidity.py`, `test_portfolio_margin.py`,
`test_portfolio_search.py`, `test_portfolio_render.py`, `test_portfolio_runner.py`,
`test_portfolio_reports.py`, `test_portfolio_metrics.py`, `test_portfolio_export.py`,
`test_portfolio_integration.py`. Эти файлы пока не реализованы.
`test_portfolio_canonical.py` проверяет один общий digest contract для четырёх
consumers; отдельные реализации canonicalization запрещены.

## 4. Phase 0 — существующий collector и readiness

**Spec:** §4. **Цель:** подтвердить входы без нового запуска/переписывания collector.

### Работы

- [x] Сверить реальный producer с актуальной spec/ADR-0024 и progress evidence.
- [x] Зафиксировать актуальную schema v2, FLOAT64, required metadata и
  combined/side-specific bid/ask coverage/completeness.
- [x] Зафиксировать `published_hours` как authority для liquidity hour files.
- [x] Читать через `SQLiteSpool.open_read_only`, не через writer-конструктор `SQLiteSpool(...)`.
- [x] Игнорировать unmarked finals и диагностировать missing marked file.
- [x] Не вводить glob-authority, manifests/per-file hashes, quarantine или max-ten WS topology.
- [x] Проверить reference/symbol-events format; реальные доступные периоды и
  symbols оставить UNKNOWN без чтения local archive.
- [x] Проверить наличие Performance inputs и approved templates; sanitized joint
  portfolio report fixture отсутствует и остаётся Q06/M6 blocker.
- [x] Составить список отсутствующих inputs без заполнения вымышленными фактами.
- [x] Не выполнять outbound ticker request: Phase 0 использует fixture и только
  документирует public read-only adapter.

### Выход и acceptance

Readiness report: версии, доступные периоды, качество, gaps и список capabilities.
Fixtures без credentials, generated corpus и реальных локальных путей.

- [x] Reader fixtures читают только опубликованные совместимые files.
- [x] Недостаточное quality не считается достаточным из-за одного valid marker.
- [x] Проверено отсутствие записи в collector archive/spool и Performance DB.
- [x] Live/soak collector не помечается выполненным этим планом: evidence берётся из его контура.

## 5. Phase 1 — MVP

Нормативная последовательность: M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8.
M2 требует M0, M1 и collector readiness. После M0 параллельно M1 разрешено
только read-only исследование report fixture. Fixture parser M6 можно начать
после M0, но его runner integration требует M5. Параллельная работа имеет
отдельное ownership и не переписывает чужие изменения. M1 устанавливает
Portfolio DB lease до первой записи; все последующие writers используют её.

### M0 — contracts и inventory

**Spec:** §2, §4, §18. **Вход:** документационный gate разрешил следующую работу.

**Evidence:** [M0 capability inventory](2026-09-06-portfolio-optimizer-m0-evidence.md).
Source/fixture inventory выполнен; M0 accepted after independent
`CODE_REVIEW_PASS` by Claude Opus 5 high in three rounds.
Физические Q01–Q12, которые код и sanitised fixtures не подтверждают, остаются
`BLOCKING_UNKNOWN` и закрывают только указанные в evidence зависимые ветви.

- [x] Зафиксировать real schema/current-result relationships, tags и review selection.
- [x] Проверить `selection_runs/results` по полям; не считать их полным campaign snapshot.
- [x] Проследить write side effects selection/windows. Default `load_selection_candidates`
  может вычислять и записывать window cache; optimizer не вызывает этот writer path.
- [x] Зафиксировать вызов selection только с `cache_only=True`; при miss вычислять производные
  из уже прочитанных typed facts и сохранять только в Portfolio DB.
- [x] Проверить read-only transaction primitive и зафиксировать fail-closed поведение,
  если concurrent source writer не позволяет получить consistent snapshot.
- [x] Проследить panel, RETEST, CLI/common runner и будущие optimizer mutating paths,
  точные path owners, restore/cleanup и existing locks.
- [x] Зафиксировать, что `PanelJobRegistry` ограничен процессом, а legacy lock
  привязан к output CSV; ни один из них пока не является target-wide ownership.
- [x] Проследить все будущие Portfolio DB writers M1–M8, canonical DB path и источник
  PID/start/host/boot identity; зафиксировать общий порядок DB/tester locks.
- [x] Проверить portfolio-mode invocation отдельно от индивидуального SINGLE_MODE;
  текущий runner его не поддерживает, exact portfolio invocation остаётся Q06/M5 blocker.
- [ ] Закрыть mapping dual-TF/shared fields, sizing/base/cap и opposite orders.
- [ ] Подтвердить priority comparison/ties/physical storage и реакцию multi-overflow.
- [x] Зафиксировать доступную single-strategy report schema, actions order и
  equity/fees/funding availability; joint portfolio report остаётся Q06/M6 blocker.
- [x] Описать boundaries development/validation, warm-up/state и upstream provenance;
  exact policy остаётся Q08/M7 blocker.
- [x] Связать каждый Q01–Q12 с owner, evidence/fixture `NONE`, fail-closed outcome
  и блокируемым этапом.

**Выход:** versioned field/capability matrix SUPPORTED/UNSUPPORTED/UNKNOWN,
sanitized fixtures и границы reuse. Неизвестная capability блокирует свой путь,
а не подменяется guessed field. No tester executable probes.

Q01–Q06 и Q09–Q11 блокируют только зависимые implementation/runtime paths;
Q07 блокирует READY/final ranking; Q08 — validation/READY; Q12 — shared
PortfolioSet READY, но не single-account fixture research. Перед READY каждый
обязательный факт имеет статус `CONFIRMED_CAPABILITY`, `APPROVED_CONSERVATIVE_BOUND`
с формулой/значением/provenance либо остаётся `BLOCKING_UNKNOWN`.

**Проверка:** договорённости и fixtures покрывают каждый физический field;
после разрешения реализации — узкие adapter-contract tests. Если обнаружено
новое поведение, сначала уточняются spec/ADR, не пишется обходной renderer.

### M1 — config, Portfolio DuckDB и snapshot

**Spec:** §5. **Зависимости:** M0.

- [x] Создать versioned `portfolio_optimizer.local.json` loader и безопасный `.example`.
- [x] Добавить рабочий local config в gitignore; не публиковать secrets/реальные paths.
- [x] Валидировать schema/policy versions, units, required profile fields/ranking IDs.
- [x] Внести ровно `portfolio_optimizer_research_risk_v1` из spec §10.1.1; не добавлять defaults для PnL, liquidity/freshness или ranking.
- [x] Хранить user deposit, cap и конечный sizing-balance envelope отдельно по
  каждому scenario/account; заморозить finite sizing grid и её порядок.
- [x] Создать transactional Portfolio DuckDB с required series в child tables.
- [x] До первой записи реализовать DB-scoped cross-process lease по canonical
  Portfolio DB path; все writers M1–M8 используют её для короткой publication.
- [x] Owner lease хранит PID/start/host/boot identity; foreign/unknown host/boot
  блокирует запись, reclaim — только same-host/same-boot proven-dead owner.
- [x] Для `LOCK_OWNER_UNVERIFIABLE` прекратить write без retry/bypass/partial
  publication. Manual clear разрешать только после atomic durable operator
  attestation sidecar с lock kind/path/target, stale owner, operator, UTC и reason;
  никакой автоматический процесс не создаёт attestation и не делает clear.
- [x] Не держать DB transaction при ожидании tester-target lock.
- [x] Добавить transactional UNIQUE Campaign content identity: concurrent duplicate
  возвращает exact row либо после uniqueness conflict перечитывает её; mismatch
  fail-closed и никогда не создаёт вторую identity.
- [x] Разделить Campaign, TradingRun, Evaluation и PortfolioSet composition identity.
- [x] TradingRun хранит immutable `execution_campaign_id`; Evaluation хранит его
  вместе с отдельным `decision_campaign_id` текущих decision/reference facts.
- [x] Снять одной read-only transaction согласованный snapshot всех typed
  candidate/fact/window inputs; не останавливать и не ставить на паузу чужой writer.
- [x] Вызывать source selection/window API только с `cache_only=True`; cache miss
  вычислять из snapshot и записывать только в Portfolio DB.
- [x] Сохранить canonical content digest полного snapshot, source schema/version,
  read time, geometry, periods, metrics/features, exclusions и review provenance.
- [x] Реализовать единый `canonical_digest_v1` по spec §5.4 и использовать его
  для Campaign/ticker/semantic/PortfolioSet digests.
- [x] Добавить literal golden vectors Campaign, semantic result и PortfolioSet;
  проверить ordering, type/unit, decimals, missing/null/UNKNOWN и source ordinal.
- [x] Зафиксировать отдельные versioned disposition, gate-result, evidence-class,
  capability-result и reason-code enums spec §5.6; golden vector проверяет exact
  строки, rename требует version bump.
- [x] Зафиксировать upstream selection window, config/algorithm hashes и seed.
- [x] Старый decision replay выполнять из frozen facts; изменение source создаёт
  новую Campaign, но не инвалидирует старую.
- [x] Различать decision replay и tick replay с доступностью binary/ticks.
- [x] Обеспечить single writer Portfolio DB и fail-closed source read при
  невозможности consistent snapshot.
- [x] Хранить typed dispositions `RESEARCH_ONLY`, `RECOMMENDATION_READY`,
  `NEEDS_RETEST`, `NEEDS_RESCREEN`, `INSUFFICIENT_EVIDENCE` и конкретные reasons,
  не создавая отдельный workflow engine.
- [x] Реализовать exact condition/scope/disposition/reason mapping из spec §5.6;
  candidate-local FAIL не завершает Campaign, passing conservative bound сам по
  себе не блокирует READY.

**Acceptance tests:** read-only enforcement и отсутствие cache writes;
`cache_only=True` miss; current result заменён при том же ID; concurrent writer
даёт consistent snapshot либо fail-closed; replay старой Campaign после source
mutation; fresh ticker даёт новый decision Campaign при прежнем execution Campaign;
DB lease contention/foreign owner/proven-dead recovery; concurrent duplicate
Campaign; golden digests; duplicate snapshot/run; transaction rollback;
missing config/invalid units. Decision replay не требует
текущего source DB; exact rerun не обещается без ticks.

**Будущая focused команда:**
`.venv\Scripts\python.exe -m pytest tests/test_portfolio_config.py tests/test_portfolio_canonical.py tests/test_portfolio_input.py tests/test_portfolio_store.py -q`.

### M2 — liquidity и exchange reference

**Spec:** §6, §7.1. **Зависимости:** M0–M1, collector readiness.

- [x] Реализовать indexed read через `published_hours`, typed reference reader.
- [x] Разрешать depth bands только `10/25/50/100 bps`; не интерполировать
  неподдерживаемый band.
- [x] Применять distribution minute depth за последние семь полностью завершённых
  UTC-суток; не подменять liquidity ceiling мгновенным стаканом или последней
  минутой. Любое изменение horizon — versioned policy.
- [x] Применять configured quantile/coverage/completeness/freshness.
- [x] Рассчитать directional depth proxy ceiling, сохранить used facts/digest/window.
- [x] Рассчитывать per Strategy ID/direction `liquidity_scalar_pct_max` по всей
  opening geometry (grid/averaging/DCA), сохранить snapshot source/timestamp/expiry;
  missing/stale/changed geometry или filters блокируют его использование.
- [x] Разделять opening-order ceiling, position/exit diagnostic и empirical capacity.
- [x] Читать instrument filters и все risk tiers; проверять pagination и units.
- [x] Выбирать применимый tier с position и active-order exposure.
- [x] Получать on-demand публичный Bybit `GET /v5/market/tickers?category=linear`
  snapshot для exact symbol и сохранять `turnover24h`, `volume24h`, время,
  units, freshness, provenance и canonical digest.
- [x] Выполнять ticker request вне Performance DB transaction; отдельно хранить
  source snapshot time и ticker server/capture times.
- [x] Max source↔ticker skew и staleness оставить OPEN POLICY без default;
  timeout/rate-limit/failure дают `TURNOVER_REQUEST_FAILED`, не zero/unlimited.
- [x] Это единственный Phase 1 outbound endpoint; private endpoints запрещены,
  actual request требует отдельного разрешения implementation.
- [x] Не создавать второй collector и не выдавать одиночный 24h snapshot за
  median/monthly statistic; missing/stale turnover оставлять UNKNOWN.
- [x] Построить coarse global screen `PASS/FAIL/UNKNOWN` с `COARSE_ESTIMATE` label.
- [x] Учитывать повтор symbol на всех выбранных accounts без netting LONG/SHORT потоков.
- [x] Хранить PortfolioSet composition digest; изменение member/load требует rescreen.
- [x] Не путать order/turnover ratio с дневным turnover стратегии или гарантией fill.
- [x] Разделять current feasibility и историческое liquidity evidence.
- [x] Для capacity использовать верх sizing base: при cap —
  `min(B_envelope_max, max_balance)`, без cap — конечный `B_envelope_max` и
  threshold повторной оценки; current balance остаётся диагностикой.

**Acceptance tests:** unmarked glob-only file; marked missing file; schema
mismatch; unsupported band; partial/no coverage; insufficient/stale history;
different sides; missing/stale turnover, wrong category/symbol/unit; repeated
symbol; changed composition; unbounded sizing envelope; tier boundary.
Отдельно: seven-day distribution отличается от последней минуты; closing order
не входит в ceiling; один отсутствующий opening level не выдаёт unlimited size;
изменение geometry/filter делает ранее сохранённый ceiling UNKNOWN.
Никаких writes collector и ни одного доказанного capacity PASS из отсутствующих данных.

**Focused:**
`.venv\Scripts\python.exe -m pytest tests/test_portfolio_liquidity.py tests/test_bybit_collector_archive.py tests/test_bybit_collector_reference.py -q`.

### M3 — margin и limiter state envelope

**Spec:** §6.2, §7. **Зависимости:** M0–M2.

- [x] Реализовать pure state evaluator с position/order IM и MM компонентами.
- [x] Хранить denominator, fee/order-loss/haircut availability и модель guard.
- [x] Различать observed, calculated, conservative bound и unknown values.
- [x] После выбранного scalar определить notional/exposure и applicable tier,
  взять maximum valid symbol-level leverage, округлить вниз по `leverage_step` и
  одинаково применить в renderer/test/guards/export. Historical individual
  leverage — provenance, не gate; unknown/non-convergent tier и конфликт одного
  symbol блокируют variant. Missing/mismatched applied leverage joint run делает
  весь TradingRun `NEEDS_RETEST`.
- [x] Quantity округлять вниз по `qtyStep`, никогда не вверх ради minimum;
  после rounding повторить minQty/minNotional/maxQty/geometry/liquidity/margin guards.
- [x] Ноль или нарушение minimum/immutable geometry отклоняет candidate с reason,
  а не молча удаляет отдельный order.
- [x] Проверять dynamic base и B/max_balance recalculation.
- [x] Для backtest использовать exact MakerFee/TakerFee из tester manifest;
  current deployment rates принимать только с provenance. UNKNOWN fee не равна нулю.
- [x] L=0 допускает все разрешённые pairs; L>0 считает non-flat symbol slots.
- [x] Priority=0 не занимает L и не отменяется limiter, но всегда входит в margin/liquidity.
- [x] Моделировать openings до L, pending cancel при L и восстановление после flat.
- [x] Оценивать L+1 и более, partial market closes и остающиеся orders до подтверждений.
- [x] Не считать исполненную часть одновременно position и full pending order.
- [x] Подтвердить произвольные partial states/order sequencing и same-symbol reserve.
- [x] Ограничить exact enumeration versioned config; при превышении считать
  conservative all-executable bound для всех positions/orders, включая exempt,
  с worst applicable tier/price assumptions. Top-L shortcut запрещён.
- [x] Resource failure допустим только если нельзя посчитать даже этот bound.
- [x] Применять stable reasons spec §5.6; passing fallback хранит
  `CONSERVATIVE_BOUND/ENUMERATION_FALLBACK_USED`, candidate rejection не
  подменяет disposition всей Campaign.
- [x] Сохранить worst-case witness и не называть несинхронные maxima observed peak.
- [x] Диагностировать достаточность user deposit; minimum-deposit estimate
  привязать к фиксированным абсолютным размерам, не к изменяемому percentage lot.

**Acceptance tests:** L0/L1/ограниченный L; all-exempt/mixed/no-exempt;
L+2 и верхний разрешённый race set; ties/unknown priority; fill/cancel race;
partial close без освобождения slot; opposite order; tier step; nonpositive
denominator; absent mark/order-loss evidence; cap crossing; quantity round-down;
below minimum; leverage mismatch/unreadable readback; same-symbol conflict;
unknown fee; enumeration limit; late confirmation.
Tester не используется как liquidation oracle. Неизвестный обязательный guard
блокирует READY, прибыль не компенсирует нарушение.

**Focused:** `.venv\Scripts\python.exe -m pytest tests/test_portfolio_margin.py -q`.

### M4 — proposals и renderer

**Spec:** §8.1–8.2. **Зависимости:** M1–M3.

- [x] Строить LONG-only, SHORT-only и BOTH PairSlot только из frozen exact
  `FINALIST` strategies; RESERVE/manual/missing status не имеют fallback.
- [x] Проверить dual-TF capability и совместимость общих runtime fields.
- [x] Сохранить dedicated close и явно выбранную opposite-order policy.
- [x] Масштабировать directional scalar, не изменяя internal lot_x/geometry.
- [x] Ограничивать scalar минимумом per-direction liquidity ceiling, margin/
  exchange limits и `dd_scalar_pct_max`; для последнего использовать current
  portfolio equity, а не start deposit, `max_balance` или MarginBalance. Missing
  D100/equity/currency/expiry даёт UNKNOWN; missing profile cap — OPEN_POLICY.
- [x] Генерировать один JSON на symbol, выполнять обратное typed comparison.
- [x] Не вводить фиксированный продуктовый лимит числа пар.
- [x] Предлагать composition, sizing, limiter и priority включая разрешённый 0.
- [x] Заморозить finite sizing grid в экспортируемых percentage units, version,
  порядок и ties; проверить каждую точку без early stop или monotonicity assumption.
- [x] Maximum выбирать только среди фактически прошедших точек.
- [x] Выполнять structural → liquidity → margin → individual-DD gates до отправки tester.
- [x] Фиксировать deterministic order, seed, hash и причины исключения.
- [x] Development ranking сделать полным порядком: versioned ordered metrics
  из OPEN POLICY и canonical candidate identity как последний tie-break;
  сохранить это правило в decision Campaign и включить в её canonical identity.
- [x] Каждая новая decision Campaign заново фиксирует ranking version; не
  наследовать её молча от execution Campaign или прежней Evaluation.
- [x] Предварительно упорядочивать только прошедшие конечные варианты по
  `estimated_individual_net_pnl_at_selected_scalar / worst_calculated_initial_margin_requirement`:
  denominator positive/known, negative numerator допустим, canonical identity
  разрешает tie. Это только порядок расходования test budget, не portfolio PnL,
  ranking или recommendation; grid и `max_pretest_variant_count` versioned/frozen.

**Acceptance tests:** одинаковые inputs дают одинаковый package; immutable fields
сохранены; unsupported mixed TF не скрывается; невозможный rounded order не
исчезает; все grid points проверены при немонотонных PASS/FAIL; запрещённый
размер не достигает runner; одинаковый executable payload
не становится новым trading run только из-за fresh reference timestamp.
Изменившиеся meaningful reference facts создают новую evaluation, изменившийся
исполняемый leverage/quantity — новый trading run.
Отдельно: scalar не превышает сохранённые ceilings; DD formula/current-equity
snapshot и currency fail-closed; below-minimum после round-down не clamp-up;
нулевой/unknown scheduling denominator не проходит; одинаковый score имеет
canonical tie-break.

**Focused:**
`.venv\Scripts\python.exe -m pytest tests/test_portfolio_search.py tests/test_portfolio_render.py -q`.

### M5 — target-wide ownership и safe portfolio tester adapter

**Spec:** §8.3. **Зависимости:** M0, M1, M4.

- [ ] Переиспользовать/расширить один existing runner ownership primitive;
  независимый optimizer lock не создавать.
- [ ] Ввести target-wide cross-process owner для resolved local/remote tester
  identity с PID, process-start, host/machine и boot/container identity и
  применить его в panel, RETEST,
  CLI/common runner и optimizer до любого реального запуска.
- [ ] Foreign/unknown host/boot, live либо unverifiable owner всегда блокирует
  запуск; reclaim — только same-host/same-boot proven-dead PID/start owner.
  Чужой process никогда не завершать.
- [ ] Для unverifiable tester owner использовать тот же manual attestation
  contract; без audit нет clear, retry loop или bypass.
- [ ] Соблюдать M0 lock order и не ожидать tester lock внутри DB transaction.
- [ ] Добавить portfolio-mode adapter, не дублировать весь runner и не подменять SINGLE_MODE.
- [ ] Поддержать локальный либо уже предоставленный пользователем remote target.
- [ ] Exact configured paths и instance identity задаются config; credentials — вне artifacts/log/DB.
- [ ] Создать run-owned workspace и manifest exact input/output artifacts.
- [ ] Snapshot/restore изменяемых strategy/tester/account settings без утечки secrets.
- [ ] Не очищать общие папки и не удалять unowned reports.
- [ ] Проверять binary/settings/tick identity и ожидаемый complete report set.
- [ ] Реализовать timeout/cancel/retry/resume с отдельной attempt identity.
- [ ] Сохранять failed/interrupted artifacts; cleanup только после M6 commit/readback gate.
- [ ] Проверить crash recovery восстановления settings, не только обычный `finally`.

**Acceptance tests:** два panel processes; panel/RETEST и CLI/optimizer races;
PID reuse/start identity; live/unknown/proven-dead owner; fake process/HTTP/remote transport;
failure before/after config install; crash/restart; timeout; late/wrong report;
unexpected paths; missing reports; retry without duplicate committed run;
remote disconnect. Разные предоставленные instances независимы, один instance
никогда не получает два одновременных изменяющих задания.

**Focused:**
`.venv\Scripts\python.exe -m pytest tests/test_portfolio_runner.py tests/runner -q`.
Реальный smoke — только отдельное разрешённое действие M8.

До полного M5 запрещены real tester runs и любые writes в shared tester target.
Артефакты M1–M4 остаются fixture/research-only; зависящие от прежнего target
state факты после принятия M5 выводятся заново, а не повышаются до READY.

### M6 — report import, cycles и metrics

**Spec:** §9. **Зависимости:** M0–M1, M5 для интеграции.

- [ ] Парсить portfolio/symbol summaries, все actions и доступные required series.
- [ ] Сохранить typed values, source order, availability и report provenance.
- [ ] Не переименовывать execution Trades в position count.
- [ ] Восстановить flat/nonflat cycles с чередующимися increases/decreases.
- [ ] Поддержать partial fills/closes, одинаковые timestamps, carry-in и open-at-end.
- [ ] Фиксировать unknown reversal/forced-close attribution, не выдумывать события.
- [ ] Основной результат считать как `final_equity - initial_equity`, realised PnL
  хранить отдельно; `OPEN_AT_END` оставлять diagnostic.
- [ ] Actual DD считать по той же полной equity series; хранить sampling resolution,
  coverage, start/end boundaries, gaps и censoring. Требуемая неполнота блокирует READY.
- [ ] Сверить counts/range/identity, fees/funding/PnL и equity boundaries.
- [ ] Вычислить actual concurrency и отдельно calculated margin guards.
- [ ] Сохранить raw report digest и canonical semantic digest normalized
  result/actions/series. Только semantic mismatch при одинаковых executable
  manifest/binary/ticks означает `NONDETERMINISTIC_RESULT`.
- [ ] Опубликовать полный portfolio run одной транзакцией и выполнить readback.
- [ ] Сохранить фактически применённый leverage при наличии report field и
  сверить с planned value; mismatch → `NEEDS_RETEST/LEVERAGE_MISMATCH`.
- [ ] Оставить safe-delete выключенным до отдельного portfolio-specific контракта,
  доказывающего commit/readback всех replay facts и parser/metric versions.
- [ ] Не переписывать старое evidence новым parser/metrics без raw report.
- [ ] При parse/import failure оставить evidence; cleanup failure после commit — warning.
- [ ] Сохранить hashes/parser/metrics versions для replay без HTML.

**Acceptance tests:** sanitized portfolio fixture; malformed schema/count/identity;
duplicate report; rollback на одном повреждённом member; reconciliation;
open unrealised loss при отдельном realised profit; missing equity sample;
semantic-equivalent HTML с иным raw digest; semantic mismatch; missing fee/funding/price;
censored cycle; interleaved reductions/additions; undefined metrics. Imported result не становится полным
за счёт исключения проблемной пары.

**Focused:**
`.venv\Scripts\python.exe -m pytest tests/test_portfolio_reports.py tests/test_portfolio_metrics.py tests/test_portfolio_store.py -q`.

### M7 — search loop, типы портфеля и frozen validation

**Spec:** §8.1, §10.1–10.2. **Зависимости:** M1–M6 и agreed policy для READY.

- [ ] Заморозить development/validation windows и upstream selection provenance до поиска.
- [ ] Фиксировать warm-up/state boundary и минимальное evidence.
- [ ] Не называть повтор на upstream-used периоде независимым OOS.
- [ ] Реализовать propose → precheck → test → import → refine в общем budget.
- [ ] Проверить каждую точку finite sizing grid; не использовать early stop или
  предположение монотонности.
- [ ] Сохранять failed/rejected/tried/not-tested-budget candidates и attempts.
- [ ] Вынести `portfolio_optimizer_research_risk_v1` DD/free/MM limits в profile config и проверять их совместно; не auto-relax.
- [ ] AGGRESSIVE: max profit только после всех gates.
- [ ] BALANCED/CONSERVATIVE: реализовать exact ranking только после его отдельного согласования.
- [ ] Не добавлять PnL, liquidity/freshness или ranking formulas как defaults.
- [ ] Preliminary исследования маркировать; missing mandatory policy блокирует READY.
- [ ] Заморозить development ranking и finalist order до validation.
- [ ] На validation выполнять только PASS/FAIL; если прошли несколько finalists,
  выбрать первый в frozen development order, не сортировать по validation return.
- [ ] Не ретюнить по validation и не скрывать провал новым ranking.
- [ ] Если zero finalists прошли, дать profile decision
  и decision Campaign `INSUFFICIENT_EVIDENCE/NO_VALIDATION_PASS`, сохранив
  отдельные invalid TradingRun reasons.
- [ ] Совместно проверить global liquidity всего PortfolioSet.
- [ ] Депозиты пользовательские; никаких allocation/transfer алгоритмов в MVP.

**Acceptance tests:** deterministic resume и бюджет; no auto-relaxation;
holdout leakage; upstream-used validation label; missing policy; failed risk
gate с высоким PnL; no feasible candidate; new policy/new evaluation;
независимое состояние нескольких accounts при общем liquidity screen.

**Focused:**
`.venv\Scripts\python.exe -m pytest tests/test_portfolio_search.py tests/test_portfolio_metrics.py tests/test_portfolio_integration.py -q`.

### D5 — verification checklist for research risk policy

- [x] Нормативные spec/ADR используют exact ID `portfolio_optimizer_research_risk_v1`,
  nine values, formulas и tri-state verdict rule; PRD/progress/AGENTS ссылаются
  на этот единственный контракт без собственных формул.
- [x] Q07 по-прежнему оставляет PnL/liquidity/freshness/ranking blockers для READY.
- [x] Ни один D5 artifact не заявляет READY, tester-run, trading admission или public/product behavior.
- [x] D5 не меняет README; code/config/API/tester/runtime diff отсутствует.
- [x] Spec, plan и ADR-0029 явно указывают единый итоговый статус: D5 `PLAN_APPROVED`.

### M8 — exact export и end-to-end acceptance

**Spec:** §10.3–10.4. **Зависимости:** M0–M7.

- [ ] Refresh exchange reference, проверить freshness/quality и shared liquidity.
- [ ] Разделять TradingRun inputs и Evaluation facts: fresh reference может создать
  новую Evaluation на прежнем run; material payload/settings change требует retest.
- [ ] Evaluation хранит `execution_campaign_id` TradingRun и новый
  `decision_campaign_id`; replay/verification используют соответствующую ссылку.
- [ ] Сопоставить export payload с exact tested/validated settings.
- [ ] При changed leverage/rounded quantity/material config создать NEEDS_RETEST.
- [ ] Сохранить один strategy JSON на symbol, portfolio/PortfolioSet manifests и human report.
- [ ] Сохранить PortfolioSet composition digest; изменение member/load переводит
  shared evaluation в `NEEDS_RESCREEN`, не инвалидируя single-account TradingRun.
- [ ] Включить account/scenario deposit, cap, L/priority/opposite mode, periods,
  candidate IDs, initial/final equity, primary result, realised PnL, DD coverage,
  metrics, gates, confidence limitations и причины выбора.
- [ ] Не включать secrets, не публиковать live commands и не запускать trading bot.
- [ ] Проверить decision replay из Portfolio DB без Performance DB/raw HTML.
- [ ] Явно показать недоступность exact tick replay при отсутствии binary/ticks.
- [ ] После отдельного разрешения выполнить минимальный настоящий joint test и import.
- [ ] Проверить single-member equivalence на сопоставимых условиях.
- [ ] Проверить несколько compositions, scalars, priorities/L и repeated-symbol accounts.
- [ ] Зафиксировать реальные risk/validation dispositions, а не заполнить их из mocks.

**Acceptance:** fixture E2E содержит liquidity/margin reject, успешный committed
run, cancellation/recovery и export; READY невозможен при missing/stale
mandatory evidence. Реальный E2E не считается выполненным по успешному unit test.

**Focused:**
`.venv\Scripts\python.exe -m pytest tests/test_portfolio_export.py tests/test_portfolio_integration.py -q`.
Relevant broader: `tests/test_performance_v2_*.py`, panel Performance tests,
`tests/runner`, затронутые collector tests; конкретный набор фиксируется по diff.

## 6. Phase 2A — advanced liquidity/margin

**Spec:** §11. **Entry:** работающий Phase 1 хотя бы в статусе `RESEARCH_ONLY`
и новые execution facts. Phase 2A не является prerequisite для MVP READY;
отдельное уточнение схемы и плана требуется до изменения поведения.

- [ ] Requested/filled, time-to-first/full-fill, partial counts и remaining quantity.
- [ ] Размерные degradation curves по symbol/side/TF, затем time regimes.
- [ ] Placement/replace/cancel/reduce-only lifecycle и более точный active-order reserve.
- [ ] Уточнённые Order Loss/haircuts/borrow/close fees только при наличии данных.
- [ ] Correlated shocks, liquidity/leverage deterioration и simultaneous deepest fills.
- [ ] Сравнение proxy и empirical bounds с confidence/применимостью и версиями.

**DoD:** missing requested не даёт fill ratio; calibration не меняет старые
evaluations; новую sizing-рекомендацию подтверждает joint retest. Multi-overflow
и грубая общая liquidity-проверка не откладываются сюда из MVP.

## 7. Phase 2B — read-only Live Account Monitor

**Spec:** §12. **Entry:** versioned deployment и отдельно разрешённые read-only
account credentials; storage/reconcile контракт уточняется перед реализацией.

- [ ] REST initial snapshot + private WS wallet/position/order/execution.
- [ ] REST reconcile при reconnect и периодически, idempotent executions.
- [ ] Account/symbol dashboard и графики PnL/equity/IM/MM/available/exposure/orders.
- [ ] Manifest drift, liquidity resize recommendations и warnings по freshness.
- [ ] Limiter OVERFLOW/RECOVERED/BREACH, configured grace и exempt handling.
- [ ] Versioned margin alerts без hardcoded UI thresholds.
- [ ] Изолировать secrets и notification permissions, не использовать Performance DB для live storage.
- [ ] Не иметь market-close/trading permission и не менять bot config.
- [ ] Сохранять исторические данные ПНЛ и других исторических показателей по каждой стратегии локально для ускорения при новом старте а так-же сохранения данных превышающих период доступный для хранения на бирже.

**DoD:** REST/WS duplicate/reconnect/stale fixtures, L0 и priority0 без false
alarms, фактические forced closes отличимы от неизвестных; отсутствие позиции
не выдаётся за отсутствующую стратегию. Monitor не исполняет аварийных команд.

## 8. Phase 3 — time/session analysis

**Spec:** §13. **Entry:** cycles, liquidity coverage, market calendars; уточнить
policy contract и минимальную выборку до исследования.

- [ ] Разметить US equity ET sessions, DST, holidays, shortened days и boundary windows.
- [ ] Отдельно определить календарь commodities при их включении.
- [ ] Считать event/cycle counts, PnL/DD, holding quantiles, deepest fill и liquidity conditions.
- [ ] Проверять consistency и INSUFFICIENT_EVIDENCE, не перебирать произвольные минуты.
- [ ] Сравнить ALWAYS_ON с небольшим заранее заданным набором entry restrictions.
- [ ] Определить добор открытой позиции; сопровождение/выход не отключать.
- [ ] Любая выбранная policy проходит общий portfolio test и независимую проверку.

**DoD:** calendar/DST fixtures, sparse-history отказ, unchanged position
management, evidence-backed improvement общего портфеля, не только одной стратегии.

## 9. Phase 4 — dependency/robustness

**Spec:** §14. **Entry:** comparable series/cycles и отдельный bounded research plan.

- [ ] Overlap entries/positions/deepest fills, heavy losses и longest holds.
- [ ] Downside/asset/sector/direction dependencies и uncertainty diagnostics.
- [ ] Perturb composition/scalars/deposit/L/priority; позднее session policy.
- [ ] Зафиксировать budget и сравнить устойчивую область с одиночным максимумом.
- [ ] Сохранить immutable baseline и все perturbation outcomes.

**DoD:** метрики воспроизводимы, missing periods не синтезируются, изменённый
winner проходит validation, нет новых непроверенных shifts.

## 10. Phase 5 — degradation analytics

**Spec:** §15. **Entry:** reconciled live history, frozen test baseline и
согласованные сопоставимые окна.

- [ ] Сравнивать live/test frequency, holding, fills, sizes, net PnL/DD, liquidity и margin.
- [ ] Реализовать NORMAL/WATCH/REDUCE_RISK/NO_NEW_ENTRIES/RETEST_REQUIRED как рекомендации.
- [ ] Зафиксировать thresholds, minimum evidence, missing-state и антидребезг.
- [ ] Показывать причину, baseline ID и известные ограничения сравнения.

**DoD:** replay state transitions, missing API не даёт NORMAL, high PnL не
скрывает нарушение margin evidence, ни одно состояние не запускает торговлю.

## 11. Phase 6 — controlled rotation

**Spec:** §16. **Entry:** работающий baseline comparison и отдельный контракт
replacement/trial/rollback, без автоматического deployment по умолчанию.

- [ ] Сравнивать нового finalist с действующим portfolio manifest.
- [ ] Выполнять replacement через новый joint test и validation.
- [ ] Пересчитать global capacity, актуальные tiers, deposit/scalar guards.
- [ ] Сформировать рекомендации reduced trial/наблюдения/роста размера по evidence.
- [ ] Сохранить old/new manifests и причины rollback/replacement.

**DoD:** неизменяемые strategy internals, восстановимый прежний config,
прозрачная история; rollback настроек не называется отменой исполненных сделок.

## 12. Phase 7 — advanced validation, allocation и profit policies

**Spec:** §17. **Entry:** достаточная история независимых campaigns; каждое
расширение получает детальный план и согласованные численные policies.

- [ ] Walk-forward и multiple-trial/selection-bias controls поверх MVP holdout.
- [ ] Tail/conditional DD, risk contributions и independent-risk diagnostics.
- [ ] Уточнённая global capacity, cross-account exposure, hidden duplication и keep/replace comparison.
- [ ] Исследовать рекомендацию распределения пользовательского общего капитала между типами портфеля.
- [ ] Исследовать accumulation, withdrawal above cap и cascade между accounts.
- [ ] Раздельно хранить equity, защищённую прибыль, cash flows и total capital.
- [ ] Не считать transfers прибылью, не объединять независимую Cross margin.
- [ ] После изменения deposit/size снова проверить affected portfolios.
- [ ] Не выполнять автоматические transfers или trading commands.

**DoD:** no-lookahead, учёт всех trials, отсутствие двойного счёта capital/capacity,
воспроизводимые scenarios и explicit withdrawal-at-open-position contract.
Ни один из этих пунктов не добавляется как обязательный capital allocator MVP.

## 13. Общая failure/recovery матрица

| Граница | Инъекция/пример | Ожидаемое поведение |
| --- | --- | --- |
| Input | current Result ID тот же, новые facts/window | новая input identity; старый replay неизменен |
| Read-only | default cache writer или concurrent source writer | одна consistent transaction либо fail-closed; нет записи/остановки writer |
| Portfolio DB | два writers / foreign host owner | одна DB lease; foreign/unknown owner блокирует без reclaim/kill |
| Campaign identity | concurrent одинаковый content | одна exact row либо conflict+exact re-read; не fork identity |
| Digest | иной map order / иной type-unit-state | тот же digest для порядка ключей; иной для meaning |
| Collector | unmarked/missing/schema mismatch | не читать как ready evidence |
| Turnover | missing/stale или wrong symbol/category | UNKNOWN; dependent READY blocked |
| Liquidity | gaps/stale/повтор symbol/нет turnover | UNKNOWN/FAIL по policy, не гарантированная capacity |
| Quantity | round-down ниже minimum | candidate FAIL; не округлять вверх |
| Margin | tier boundary, missing denominator, partial fill | approved bound либо blocking UNKNOWN; no double counting |
| Limiter | L0, priority0, L+2, pending cancel/partial close | slot/margin освобождается только по подтверждению |
| Renderer | unsupported dual-TF/shared-field conflict | fail-fast без потери стороны |
| Runner | live/unknown owner, crash после install, remote disconnect | stop без kill; restore/recovery, сохранить evidence |
| Report | wrong members/window/count/partial portfolio | нет успешной частичной публикации |
| Import | сбой до commit/после commit до cleanup | rollback+keep либо COMMITTED+cleanup warning |
| Equity | realised profit и open unrealised loss | primary result по final−initial equity; OPEN_AT_END diagnostic |
| Semantic replay | разный HTML, равные normalized facts | не nondeterminism; сравнивать semantic digest |
| Search | budget exhausted, no feasible, missing policy | явный статус; no auto-relaxation |
| Validation | upstream leakage, retuning или сортировка по holdout | не выдавать независимый OOS |
| Validation | zero frozen finalists pass | `INSUFFICIENT_EVIDENCE/NO_VALIDATION_PASS` |
| PortfolioSet | member/load изменён | NEEDS_RESCREEN; single-account run сохраняется |
| Export | changed executable leverage/quantity/settings | NEEDS_RETEST/READY blocked |

### Конкретные проверочные примеры для исполнителя

Числа ниже — синтетические fixtures, не торговые risk/PnL defaults.

1. **Exempt и overflow.** A имеет priority=0; B/C/D/E — counted, L=2.
   После открытия B и C счётчик равен 2. Открытие A не повышает его;
   A входит в margin totals. Пока отмены D/E не подтверждены, их orders
   продолжают участвовать в envelope. При исполнении обоих counted=4 (L+2),
   а вместе с A возможны 5 позиций. Нельзя считать пределом 2 или 3 позиции.
2. **Partial close.** В позиции осталось 0.1 единицы: slot ещё занят.
   Отправленный market close не освобождает его; подтверждённый flat освобождает.
3. **Cycle.** При изначальном flat signed changes `+1,+1,-0.5,+1,-2.5`
   дают post sizes `1,2,1.5,2.5,0`: один закрытый cycle, пять executions.
   Добор после частичного сокращения не начинает второй cycle.
4. **Cap.** Только для подтверждённого balance-percentage mapping:
   B=100, cap=200, s=0.1, lot_x=[1,2] дают notionals [10,20].
   При B=300 получаются [20,40], но реальный Cross balance не становится 200.
   Момент перерасчёта resting orders этим арифметическим тестом не подтверждается.
5. **Source replacement.** Campaign C1 сохранила Result ID=17, window W1,
   metrics F1. После RETEST строка 17 содержит W2/F2: C1 replay всё ещё
   использует W1/F1; новая C2 фиксирует W2/F2. Нельзя перечитать F2 для C1.
6. **Reference refresh.** Только captured_at изменился, trading JSON/binary/ticks
   прежние: можно переоценить existing run. Leverage в JSON изменился:
   export получает NEEDS_RETEST, даже если старый результат прибыльный.
7. **Повтор symbol.** Два accounts с нагрузками V1 и V2 на один symbol:
   coarse screen использует совместную нагрузку V1+V2 по согласованному
   сценарию, а не два независимых допуска на полный capacity budget.
8. **Unknown versus zero.** Нет equity series — DD неизвестен, не 0.
   Нет requested size — fill ratio неизвестен, не 100%. Нет threshold —
   профиль не READY, а не «ограничение выключено».
9. **End boundary.** Initial equity 100, realised PnL +10, open unrealised −25,
   final equity 85: primary result −15, realised +10, `OPEN_AT_END=true`.
10. **Немонотонная сетка.** Для `[1%, 2%, 3%]` outcomes PASS/FAIL/PASS:
    проверяются все три точки, maximum среди фактических PASS равен 3%.
11. **Round-down.** Raw quantity 1.09 при `qtyStep=0.1` даёт 1.0. При
    `minQty=1.1` candidate FAIL, а не quantity 1.1.
12. **Fresh decision lineage.** TradingRun R1 сохраняет execution Campaign C1;
    ticker T2 создаёт decision Campaign C2 и Evaluation E2 с обеими ссылками.
13. **DB lease/digest.** Два concurrent inserts одного canonical snapshot дают
    одну Campaign identity; перестановка object keys digest не меняет, смена
    type/unit/UNKNOWN reason меняет.

## 14. Проверки, review и коммиты в будущей реализации

- [ ] Сверить каждый scoped diff со spec, не включать unrelated work.
- [ ] Узкий failing test → implementation → focused suite → relevant broader suite.
- [ ] Тесты только `.venv\Scripts\python.exe -m pytest ...`.
- [ ] `git diff --check`, осмотр staged diff, проверка документационных ссылок.
- [ ] Независимый review; исправления → повторные проверки → re-review.
- [ ] Обновить spec/ADR/PRD/progress по изменившимся контрактам и evidence.
- [ ] Создать scoped conventional commit после review, не раньше.

M0–M4 are accepted with independent `CODE_REVIEW_PASS` by Claude Opus 5 high.
M5 fixture/fake implementation is in progress; M6–M8 remain unstarted.

## U0/U1 — отдельная сквозная дорожка Panel

Эта дорожка не меняет и не перенумеровывает M-задачи. M2 был реализован без UI;
после независимой приёмки M3–M4 следующим серверным этапом остаётся M5.

### U0 — архитектура и контракт

- [x] Зафиксировать экран запуска, Settings, Campaign job, прогресс, API и XLSX
  в [UI spec](../../specs/2026-09-06-portfolio-optimizer-panel-ui.md).
- [x] Принять границу в
  [ADR-0031](../../decisions/0031-portfolio-optimizer-panel-ui-and-campaign-boundary.md).
- [x] Получить независимый `PLAN_APPROVED`.
- [x] Получить финальный `CODE_REVIEW_PASS` документационного diff.
- [x] Не изменять код, config, тесты, runtime или tester.

### U1 — реализация Panel, не начата

U1 планируется ближе к M8 и начинается только после отдельного назначения и
принятого backend API scope. До кода исполнитель должен:

1. переиспользовать существующие SPA Panel и серверный реестр заданий;
2. добавить API-адаптер, не копирующий selection/ranking/sizing из
   `src/mrs3/portfolio` и не пишущий в PerformanceDB;
3. показывать только поля config, принятые текущим parser; nullable config
   `max_balance` и UI defaults ждать отдельной схемы v2;
4. реализовать Campaign freeze, одно активное задание, журнал, восстановление
   страницы, restart→`INTERRUPTED`, безопасную отмену и success-only XLSX;
5. покрыть API, CAS, rank isolation, прогресс, redaction и lifecycle тестами;
6. держать передачу тестеру выключенной до принятых M5/M6 и отдельного явного
   разрешения пользователя.

U1 не разрешает менять алгоритмы оптимизатора ради удобства интерфейса. Поля и
кнопки появляются только после принятия соответствующей backend capability.

## 15. Ближайший следующий этап

M0–M4 are accepted after independent `CODE_REVIEW_PASS`; M5 is the next server
stage. Evidence: [M3 ledger](2026-09-06-portfolio-optimizer-m3-evidence.md),
[M4 ledger](2026-09-06-portfolio-optimizer-m4-evidence.md). Q01–Q12 remain isolated or
fail-closed where unknown. Real tester/bot execution additionally requires
accepted M5 and M6 plus separate explicit user authorization.
Не задавать пользователю вопросы повторно, если поведение уже закреплено в
§2/§7 спецификации: выяснять physical mapping и evidence.

## 16. Критерий независимости нового пакета

Canonical spec/plan/ADR и навигация не ссылаются на рабочую подборку как на
обязательный источник. Утверждённый collector имеет собственный canonical
пакет и не заменяется её старым ТЗ. Исходные заметки не удаляются этим планом;
после проверки переноса их можно удалить отдельным действием пользователя.
Историческая v0.4 и старые ADR сохраняют происхождение и не переписываются
задним числом. Нельзя смешивать эту независимость документов с принятием
нового runtime-контракта или успешным review.

## 17. Инструкция исполнителю без контекста переписки

1. Выполнять только назначенную M-задачу/фазу. Сначала прочитать связанные
   разделы spec, затем перечисленные существующие функции и их callers/tests.
   Не считать название функции доказательством отсутствия записи или cleanup.
2. Если поле зависит от OPEN Q01–Q12, не выбирать удобный default. Реализовать
   проверку capability/missing data и передать конкретный недостающий контракт
   root. Fixtures проверяют отказ; они не подтверждают поведение реального бота.
3. Согласованные business rules не переоткрывать: L0 disabled, count by PairSlot,
   exempt priority0, отдельные Cross accounts, отсутствие simultaneous LONG+SHORT,
   immutable pretested geometry, exact `FINALIST` universe, maximum current
   symbol-level leverage и liquidity distribution за семь завершённых суток.
   Неизвестны только отмеченные physical mappings.
4. Числа PnL/risk/liquidity не брать из примеров старых заметок или из тестовых
   fixtures. Синтетические значения допустимы только в тестах; отсутствие
   принятой пользовательской policy запрещает финальный READY.
5. Не создавать native/remote tester runtime и не запускать executable ради
   проверки догадки. Реальные runs — только после явного разрешения и M5 ownership.
6. Не изменять чужие данные, не чистить директории по шаблону и не писать в
   Performance DB. При конфликте API с read-only/ownership контрактом сначала
   выделить безопасный общий primitive, не обходить проверку локальным wrapper.
7. Перед любой Portfolio DB записью брать DB-scoped lease; перед tester mutation
   — отдельный target lock. Foreign/unknown host/boot не reclaim, чужой process
   не kill, tester lock не ожидать внутри DB transaction.
8. Использовать только общий `canonical_digest_v1`; не смешивать missing/null/UNKNOWN,
   binary-float rendering и порядок одинаковых timestamps без source ordinal.
9. Evaluation всегда связывать с execution и decision Campaign. Fresh reference
   не создаёт новый run без payload change.
10. Quantity округлять только вниз; candidate-local FAIL не завершает Campaign.
    Проверять все grid points, full overflow envelope и не использовать top-L.
11. Validation не пересортировывает finalists; zero PASS получает
    `NO_VALIDATION_PASS`. Passing conservative bound не блокирует READY автоматически.
12. Expected failure — самостоятельный результат: сохранить reason, stage,
   availability и исходный context без secrets. UNKNOWN не преобразовывать
   в 0, false, empty successful report или PASS.
13. Перед сдачей назвать изменённые файлы, фактические команды и результаты,
   все незакрытые capabilities и ограничения replay. Не отмечать последующие
   фазы завершёнными и не выдавать свой self-check за independent review.
