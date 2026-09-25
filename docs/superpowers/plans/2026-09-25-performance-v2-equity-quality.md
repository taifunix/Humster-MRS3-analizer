# Performance v2 Equity Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. This document is a proposal, not implementation authorization.

**Goal:** независимые equity-filter №2 и equity-метод существующего Top N,
учитывающие устойчивость роста, просадку, неровность и свежую динамику.

**Architecture:** один pure facts engine, один небольшой cache adapter,
существующий общий путь подготовки окон и общий analog/Top N/XLSX pipeline.
Никаких новых сервисов, очередей, библиотек или отдельного Excel writer.

**Tech Stack:** Python, Decimal/stdlib, существующие DuckDB/pandas/openpyxl,
нативные HTML controls и JavaScript; tests только через локальную `.venv`.

**Spec:** [Performance v2 equity quality](../../specs/2026-09-25-performance-v2-equity-quality.md).

**Version:** canonical R6.1, 2026-09-25. Основание: Planner `PLAN_REVISION R6.1`, source R6.
R5 получил independent `PLAN_APPROVED`; RV3/RV4 findings closed. R6 меняет
policy по решению пользователя «только понижать место»; review R6 запросил
два уточнения ниже; re-review R6.1: **`PLAN_APPROVED`**, 2026-09-25.
Engineering review не заменяет acceptance покрытия/скорости и не разрешает runtime.
Все implementation checkboxes ниже открыты. Реализация не начата.

## Global constraints

- Только Performance v2 card «4. Pareto and filters» и её существующие consumers.
- `filter_equity_regime` OFF по умолчанию; optional `rank_robust_top_n`,
  `method=robust_v1|equity_quality_v1`; omitted method = robust; N default 20.
- Окна 7/14/28d, UTC `T=report_end`, H по возрасту 28/14/7; нет 3d gate.
- При UP H и валидных facts flat/declining короткие окна не вызывают ERF BLOCK;
  WEAKENING только понижает приоритет enabled equity-ranking.
  Нет minimum trades/week и hard-exclusion только за возраст <28d.
- Decimal precision 38; eps `1e-8` log-pp/30d; score HALF_EVEN 12 знаков.
- Одна сетка <=113 points; raw DD/P только H. Нет historical blocks/HWM age.
- Старый wallet/A/B расчёт не менять; новый путь MTM не snap к flat boundary.
- Missing required cache = request error; cached unknown = ready/unassessed.
- Legacy hashes/columns/robust decisions не меняются при отсутствии новых полей.
- Один source load, существующий pool/worker limit, DuckDB reader threads=1,
  порция <=2*workers; readers закрыты до единственного writer.
- Четыре новые видимые Excel колонки; existing final score/rank переиспользовать.
- Не трогать чужие dirty changes; не запускать tester и не писать в live DB.
- Все проверки Python: `.venv\Scripts\python.exe -m pytest ...`.

## Review focus

1. Редкие сделки: flat неделя не превращается в отрицательную оценку;
   длительный разрыв при открытой позиции не считается доказанным flat (M1/M2).
2. REPLACE переиспользует result_id: stale source нельзя публиковать ни в
   facts, ни в selection snapshot, даже если список IDs не изменился (M1/M3).
3. Legacy asdict/hash, boolean trace и protected XLSX headers сохраняются;
   method проходит через review, export и RETEST recovery (M3/M4).
4. Ранний lot winner не удаляет единственный пригодный ERF variant; rank-only
   сохраняет явное действие пользовательского lot prefilter (M2/M3).
5. Warm performance — не повторная загрузка raw и не молчаливый backfill;
   frozen-corpus benchmark отделён от функциональных unit tests (M1/M5).

## Findings ledger / root integration

| ID | Источник | Disposition |
| --- | --- | --- |
| R-01 | Planner R2: другой ID, обязательный rank | Accepted R3: совместимые IDs, optional rank, N20 |
| R-02 | Planner R2: invalid автоматически отвергается | Accepted R3: unknown passthrough; только наблюдаемая nonpositive equity BLOCK |
| R-03 | Planner R2: FK/new revision column/raw hash | Accepted R3: existing metadata revision, no FK, transactional invalidation |
| R-04 | Planner R2: float и незаданный epsilon | Accepted R3: Decimal grid, fixed epsilon/rounding |
| R-05 | Planner R2: весь preview без raw | Accepted R3: только новая equity ветка; legacy actions reads остаются |
| R-06 | Planner R2: лишние DD windows/неявный fallback | Accepted R3: DD/P только H, H только по report age |
| R-07 | Planner R2: незаданные benchmark floors | Accepted R3: конкретные proposed бюджеты/повторы/сценарии |
| RV3-01 | Advisor: одинаковые strategy_id у lot variants | Premise rejected / resolved: loader dictionary keyed strategy_id, variants distinct; uniqueness invariant и permutation tests |
| RV3-02 | Advisor: passthrough можно спутать с PASS | Accepted R4: только decisive PASS; разные skip reasons для unassessed/blocked |
| RV3-03 | Advisor: readiness нового cache неявна | Accepted R4: old-ready AND presence/revision/algo/canonical digest для enabled consumers |
| RV3-04 | Advisor: неоднозначный batch bound | Accepted R4: <=w active sources, <=2*w metadata jobs/compact results |
| RV3-05 | Advisor: lifecycle deletion неполон | Accepted R4 с проверкой кода: REPLACE transaction; prune verified backup/restore, других parent-delete APIs не найдено |
| RV3-06 | Advisor: sparse quality не имеет release gate | Accepted R4: proposed <=5% coverage-unrankable, 0 false unknown на verified flat paths; predecessor action proof |
| RV3-07 | Advisor: mixed T обнаруживается поздно | Accepted R4: cheap all-input metadata preflight до compute/writes, payload и remediation |
| RV3-08 | Advisor: LRU может вернуть старые решения | Accepted R4: settings-independent facts only; fresh policy outside LRU |
| RV4-01 | Advisor: read-only schema v5 после миграции | Accepted R5: strict readable v5/v6, no DDL; damaged v6 fail-closed; explicit writable initialization |
| RV4-02 | Advisor: toggle-dependent facts и single LRU entry | Accepted R5: toggle-independent cached reads; explicit sentinels; cold publish legitimate miss |
| RV4-03 | Advisor: нет оценки sparse correction-block rate | Accepted R5: M0 distributions и explicit user acceptance, не скрытая смена tolerance |
| U6 | Пользователь: «только понижать место» | Accepted R6: H-UP GROWING/WEAKENING оба PASS; ranking class priority прежний; RV4-03 M0 теперь проверяет zero short-only BLOCK и ranking demotion |
| R6-01 | Advisor: комбинация ERF ON + robust | Clarified R6.1: намеренно без equity-штрафа; понижение только new method; controlled robust-equivalence fixture |
| R6-02 | Advisor: недостаточно adversarial rank tests | Accepted R6.1: class1 score10 ниже class0 score1; within-class tie-chain; оба коротких окна отрицательны; policy/hash assertions |

## File map and interfaces

Новые production files — только:

- `src/mrs3/performance_v2_equity_quality.py`: pure geometry и typed facts;
  `EquitySample(sample_index, timestamp_utc, equity)` и
  `PositionSample(action_index, timestamp_utc, post_size)` — frozen dataclasses.
  Inputs sorted by UTC/index; adapter нормализует порядок, engine валидирует.
  `calculate_equity_facts(*, report_start: datetime, report_end: datetime,
  equity: Iterable[EquitySample], actions: Iterable[PositionSample]) -> EquityFacts`.
  `EquityFacts` содержит H, version, quality, window facts keyed 7/14/28,
  D/P H и structured reasons; точные поля/диапазоны из spec §§4–6.
- `src/mrs3/performance_v2_equity_cache.py`: revision, strict canonical
  encode/decode, scoped reads, per-batch publication; не selection policy.
  `equity_source_revision(result_metadata: Mapping[str, object]) -> str`;
  `encode_equity_facts(facts: EquityFacts) -> str`;
  `decode_equity_facts(payload: str, expected_digest: str) -> EquityFacts`.
  Existing worker получает facts рядом с WindowMetrics, не отдельной задачей.

Classifier/score остаются в selection module: они принимают готовые facts,
не открывают DB. `_rank_equity_quality(group)` имеет тот же return contract,
что `_rank_robust`: `(DataFrame, ordered_indexes)`. Policy version отдельно
от facts algorithm version; не cache final decisions.

Existing edits: `performance_v2_selection.py`, `performance_v2_windows.py`
(только минимальный sharing), `performance_v2_store.py`,
`performance_v2_import.py`, `performance_v2_prune.py`,
`performance_v2_selection_review.py`, `panel.py`, `panel_performance_v2.py`,
`panel_web/app.js`, `panel_web/index.html`. Не выделять общий framework.

## M0 — проверка идеи и критериев до implementation

**Files:** spec/this plan; future `scripts/benchmark_equity_quality.py` и
`docs/superpowers/plans/2026-09-25-performance-v2-equity-quality-evidence.md`
только при назначении этапа. Новый ADR создаётся после принятия контракта,
со следующим свободным номером; не резервировать занятый номер заранее.

- [ ] Зафиксировать frozen cohort и ревизии, распределение возраста, дат T,
  количества samples/actions и gaps. Read-only live scan либо DB copy;
  ни finalists, ни производственный кэш не менять.
  Live gap distribution показать рядом с denominators; восемь ранее
  просмотренных результатов с95–265h gaps не выдавать за весь корпус.
- [ ] Воспроизвести monotone, ступени 2–3 trades/week, flat last7, last7 loss,
  late jump, recovery при отрицательном28, intragrid DD, sparse-flat/open.
  Сравнить сетки 1/3/6h и возрастные переходы около7/14/28; показать пользователю
  различия класса/score, а не выбирать настройки по количеству прошедших.
- [ ] Зафиксировать ограничения event-based equity: missing MTM и неизвестные
  cashflows не восстанавливаются математикой. Проверить, есть ли достаточное
  число оценимых стратегий при контракте coverage.
- [ ] Проверить spec §11 coverage gate по strata r=0/0<r<=3/r>3 trips/week,
  где r=completed trips within H *7/H (дробные частоты не теряются):
  <=5% coverage-unrankable overall и в каждом sparse stratum; 0 ложных unknown
  на вручную подтверждённых complete flat paths. Missing boundary/initial
  unknown/open gaps/invalid показывать раздельно с denominator и размером
  strata. Пустая stratum не PASS. Не достигнуто — решение по источнику/дизайну,
  не молчаливое ослабление coverage. Бюджет согласовать до runtime coding.
- [ ] Измерить старый код: one warmup +3 repeats на одинаковой DB copy,
  hardware/settings. Записать baseline wall/RSS/SQL/rows/writes.
- [ ] Принять с пользователем policy примеры и performance targets из spec §9.
  До этого M1–M5 — план, не разрешение менять runtime.
- [ ] Для тех же strata проверить ноль ERF BLOCK solely-by-short-decline
  при валидном H UP. Показать доли WEAKENING от всех и от H-UP, magnitude
  p50/p90/max и примеры понижения порядка. Отдельно отметить действия других
  фильтров/lot/analog/Top N. Решение пользователя уже принято: новый tolerance
  и повторное согласование short-window отсева не нужны.

**Exit:** evidence объясняет flat-week admission, возможный отказ recovery,
риск сравнения разных H и реальную оценимость данных; предиктивность не заявлена.

## M1 — pure engine, lifecycle и быстрая единая подготовка

**Files:** оба новых модуля; existing store/import/prune/windows/selection;
`tests/test_performance_v2_equity_quality.py`,
`tests/test_performance_v2_equity_cache.py`, existing store/import/prune/windows tests.

- [ ] Сначала добавить failing parameterized pure checks на eps, scale
  invariance, nonpositive priority, Decimal values, equal timestamp spikes,
  left boundary, age6.99/7/13.99/14/27.99/28, no fallback при broken H.
  Минимальный fixture для flat tail: equity `1000 + 10*count(day>=d)` при
  `d=[3,6,10,13,17,20]`, sample каждые6h до28d, непрерывно flat position.
  Ожидание: H28 UP, 14 NONDECLINING, 7 slope=endpoint=ER=0.
- [ ] Запустить новый тест, получить ожидаемый FAIL, затем реализовать один
  merge-проход equity/actions, causal grid, до113 ln, slices OLS/ER, raw DD/P H.
  Не добавлять historical scan или float conversion.
- [ ] Добавить boundary tests open/unknown gap6h vs >6h, unknown gap между
  grid nodes, continuous flat gap, закрытие без нового equity, неизвестное
  состояние до первого action. Unknown не подменяет ноль и не скрывает E<=0.
- [ ] Red/green schema v6: fresh DB, поддержанные old migrations, exact catalog,
  round-trip strict facts JSON/digest. Table `equity_quality_metrics` с
  PK(result_id,algo_version), без FK и без изменения source schema columns.
- [ ] Red/green `require_performance_v2_readable(connection)->int`: exact valid
  v5/v6, ноль writes. Для v5 legacy reads сохраняются, new consumer получает
  EQUITY_SCHEMA_UPGRADE_REQUIRED без SELECT новой таблицы. v6 без table =
  corrupt schema error, не legacy fallback. v2–4 read-only по-прежнему
  upgrade-required; явный initializer поддерживает цепочки до v6.
  Записать фактический writable initialization entry point в normal Panel
  workflow и test upgrade-required -> этот entry point -> v6 ready.
- [ ] Red/green REPLACE и prune: delete/count lists, transaction rollback,
  reused result_id с новым imported_at, corruption/version mismatch.
  В REPLACE cache invalidation атомарна с imported_at. В prune сохранять
  writer lock/checkpoint/whole-file backup/restore, не придумывать общую
  transaction: текущий DuckDB FK-контур использует backup boundary.
  Проверить single/multiple prune и failure restore вместе с новой таблицей,
  zero orphans; повторить поиск всех source parent-delete/cache-delete sites.
- [ ] Расширить существующий worker: union missing jobs; один full `_load_source`
  когда нужны old windows, иначе H-tail query с predecessor equity/position.
  Метаданные revision и raw source читать в одной read transaction.
  Position predecessor нужен на/до predecessor equity sample; все actions
  от этого sample до T. Last action только на H start недостаточен для
  доказательства, что старое equity наблюдалось уже в flat-состоянии.
- [ ] Spy tests: old+new cold = один source load; new-only cold не читает
  историю до predecessor; warm new branch = ноль raw reads/writes;
  change N/method/order/ab_final_days не инвалидирует equity facts.
- [ ] Red/green bounded batch <=2*workers, no overlapping connection modes,
  one writer transaction и revision recheck; race откатывает порцию, не
  публикует stale facts, предыдущие валидные порции остаются доступны.
  При w workers живы <=w raw sources; queue <=2*w metadata jobs, завершённые
  outputs компактны. Не материализовать source до отправки job в worker.
- [ ] Проверить exact RETEST cohort для readiness/queries/invalidation tokens;
  serial и parallel дают одинаковые JSON/digests.

Run: `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_equity_quality.py tests/test_performance_v2_equity_cache.py tests/test_performance_v2_store.py tests/test_performance_v2_import.py tests/test_performance_v2_prune.py tests/test_performance_v2_windows.py`.

**Exit:** функциональные tests и instrumentation подтверждают не только
формулу, но и один load/zero warm writes. Независимый review перед scoped commit.

## M2 — фильтр №2 и lot protection

**Files:** `performance_v2_selection.py`, `tests/test_performance_v2_selection.py`.

- [ ] Red/green parser `filter_equity_regime`, fixed pair_side, default OFF;
  submitted/effective order, explicit/implicit lot first, rank last.
- [ ] Табличные policy tests: flat7 + UP14/28 PASS; negative7 + UP H14/28
  PASS/WEAKENING; negative14 + UP H28 PASS/WEAKENING; H7 negative BLOCK;
  positive7/14 + negative28 BLOCK; validshort10d UP PASS; under7 unassessed;
  malformed/gap unassessed; nonpositive BLOCK даже при другом unknown.
- [ ] Реализовать self-only gate из готовых facts. Stage counts оставляют
  стандартную семантику; отдельный unassessed count и reason, не ложный PASS.
- [ ] Red/green mixed lot group: old winner BLOCK, sibling PASS -> не
  схлопывать group перед ERF. All PASS -> прежний lot winner; unknown member
  -> skip collapse. Filter OFF -> byte-equivalent legacy result frame.
  All-unassessed тоже skip; any unassessed reason
  LOT_GROUP_EQUITY_UNASSESSED имеет приоритет над LOT_GROUP_EQUITY_BLOCKED.
  Passthrough != decisive PASS.
  Mixed GROWING/WEAKENING с валидными facts — all-PASS, обычный lot collapse;
  class1 не должен остаться BLOCK по старому контракту.
- [ ] Проверить prior elimination, отсутствие resurrection и отсутствие
  raw compute при selection/preview.

Run: `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection.py`.

**Exit:** отдельный filter-only режим работает без включённого ranking.

## M3 — альтернативный ranking и воспроизводимый review

**Files:** selection/selection_review и matching tests.

- [ ] Сначала golden tests legacy request JSON/hash/results. Optional method
  только у rank stage; отсутствующий/disabled rank инертен; robust untouched.
- [ ] Реализовать `_rank_equity_quality` и общий dispatch, не копировать analog
  grouping. Exact key `(class,-score12,D,P,-H,strategy_id)`; все формулы spec §8.
  В combined режиме ERF сохраняет WEAKENING eligible, equity-ranking ставит
  class0 выше class1; rank-only использует тот же порядок. Robust без изменений,
  filter-only/disabled ranking не переставляет строки. Это не fixed-rank-drop.
  Adversarial fixture: class1 score10, class0 score1 -> class0 выше независимо
  от score; внутри каждого класса отдельно проверить -score,D,P,-H,id.
  H28 UP с обоими отрицательными7/14 -> PASS/class1.
  Robust equivalence fixture: все кандидаты H UP, среди них WEAKENING,
  lot и прочие фильтры OFF, одинаковый input/N. ERF ON/OFF + robust даёт
  одинаковый порядок, без нового equity-штрафа. Не переносить эту эквивалентность
  на произвольный cohort, где ERF действительно исключает другие строки.
- [ ] Red/green score cases: при G>0 рост D/P или снижение Q понижает score;
  G<0 большая DD делает score хуже, не ближе к0; near-eps score0; deterministic
  ties; flat7 не обнуляет положительный G28; young growing может быть выше old
  weakening; H — только поздний tie-break.
- [ ] Assert candidate strategy_id unique; different lot-strategy IDs с
  одинаковыми метриками сохраняют порядок при permutations/worker counts;
  duplicate IDs дают typed invalid input. Не менять tie-key из-за неверного
  предположения, что order rows являются отдельными candidate rows.
- [ ] Проверить missing-score RESERVE, rank-only отрицательные rankable,
  Top N по analog representatives, prior REJECTED, одинаковые T у input cohort,
  typed mismatch и отсутствие влияния чужого RETEST cohort.
  Общий T проверяется по всем ACTIVE input results до фильтров, до heavy
  source reads/cache publication. Error payload result_id/date; mixed-T
  rank request = zero heavy reads/writes, но facts-only warming разрешён.
- [ ] Version-aware canonical projection: old request не получает новые null
  fields от asdict. Новый contract v2 сохраняет method/policy/effective order,
  revisions/digests/decision facts в существующих JSON-полях, не новой таблице.
  Filter policy R6 = `equity-regime-v2`; facts version/ranking method прежние.
  Policy-only изменение не инвалидирует facts и само не требует DB migration.
  Assert snapshot policy ID и byte-identical прежний legacy-v1 canonical hash.
- [ ] Red/green v1/v2 review round-trip, snapshot recheck при same-ID REPLACE,
  old snapshot после удаления cache, equivalent-run checks и rollback при race.

Run: `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection.py tests/test_performance_v2_selection_review.py`.

**Exit:** filter-only/rank-only/both/legacy воспроизводимы; review не доверяет Excel precision.

## M4 — UI, общий XLSX, readiness и RETEST consumers

**Files:** panel.py, panel_performance_v2.py, panel_web/app.js/index.html,
shared writer в selection.py; tests/test_panel_performance_v2.py,
test_panel_performance_v2_export.py, test_panel_performance_v2_retest.py,
test_panel_static_ui.py, selection/review tests.

- [ ] Red/green service readiness учитывает только enabled equity consumers;
  explicit recalc может прогреть facts при выключенном consumer. Добавить
  source/digest/version/cohort в candidate LRU identity.
  Проверить missing/stale revision/wrong algo/corrupt JSON/digest => not-ready;
  cached UNKNOWN => ready; disabled consumer => прежняя readiness.
  Одно warm LRU entry содержит только candidates/facts: toggles method,
  checkbox, N дают новые outputs без raw/recompute/writes, policy применяется
  после lookup. Settings-independent не означает cached final output.
  Начать warm-entry test при обоих OFF, но fresh facts уже есть: optional
  cached reads toggle-independent. После ON/method/N — hit. Отдельно cold
  ABSENT sentinel -> ON not-ready -> explicit publish -> новый key/miss.
  SCHEMA5/STALE/INVALID sentinels и zero new raw/write проверяются отдельно.
- [ ] Добавить checkbox №2 и native method select у текущего Top N, не новую
  панель. Fixed-prefix barrier действует также при перемещении соседнего ряда.
  Disabled rank не требует кэш; stale HTTP responses не перетирают новый выбор.
- [ ] Показать предупреждение rank-only+lot и unassessed count. Labels,
  keyboard/aria-live сохраняют доступность; CSS framework не нужен.
  Help явно различает short-window WEAKENING (понижение equity-rank) и ERF
  BLOCK (основной H не UP либо nonpositive); другие filters не отключаются.
  Для ERF ON + robust явно писать: PASS без нового equity-штрафа; понижение
  только при equity_quality_v1. Никакого автоматического переключения метода.
- [ ] Shared writer добавляет только четыре equity columns для нового request.
  Existing Final score/rank переиспользуются, метод явно подписан без изменения
  protected headers: cell comment и workbook metadata, без header rename.
  State/basis объясняют short/unknown/invalid; null blank.
- [ ] XLSX tests: legacy columns неизменны, new block справа, исключённые
  строки сохраняются; нет generic .01 rounding для equity metrics и нет
  полной диагностики в hidden columns. Full facts находятся в snapshot JSON.
- [ ] Read-only Performance export показывает только свежие имеющиеся facts,
  не compute/migrate/write и не меняет manual FINALIST/RETEST.
  Valid v5 fixture экспортируется прежними колонками с неизменным catalog;
  review-read работает. Snapshot/review-import writers требуют explicit
  initialized v6. Test той же копии после normal writable initialize v5->v6.
- [ ] Проверить recovery/bulk retest/control export: method не теряется,
  exact successful cohort и существующие snapshot/review guarantees сохранены.

Run: `.venv\Scripts\python.exe -m pytest tests/test_panel_performance_v2.py tests/test_panel_performance_v2_export.py tests/test_panel_performance_v2_retest.py tests/test_panel_static_ui.py tests/test_performance_v2_selection_review.py`.
Run: `node --check src/mrs3/panel_web/app.js`.

**Exit:** минимальный UI и четыре новых колонки, без скрытой новой вычислительной ветки.

## M5 — performance, интеграция и приёмка

**Files:** benchmark/evidence из M0; spec/plan/progress/PRD по verified facts.

- [ ] Прогнать один frozen corpus в четырёх режимах: legacy/filter-only/rank-only/
  both. Проверить повторяемость, список изменившихся решений и причины.
  Отдельно сравнить legacy baseline с новым v6 при обоих controls OFF:
  optional cached-facts read имеет измеряемую цену, её не считать бесплатной.
- [ ] На DB copies сравнить old-cold+new-cold, old-warm+new-cold backfill,
  all-warm, one REPLACE. One warmup+3 measured repeats, тот же cohort/settings.
  Профиль 1/4/8/16 workers; timestamped evidence с версиями и SQL counters.
- [ ] Проверить согласованные в M0 budgets: cold median <= baseline+
  max(20%,0.5s); warm <= baseline+max(10%,0.05s); RSS <= baseline+
  max(25%,64MiB). Проценты берутся от соответствующего baseline; absolute
  floors не складываются с процентами. Backfill показывается отдельно.
- [ ] При превышении измерить load/compute/publish, устранить bottleneck либо
  получить явное принятие нового бюджета. Не менять Decimal/version молча.
- [ ] Повторить все focused commands M1–M4 и relevant broader suites после
  интеграции. `git diff --check`, осмотр scoped/staged diff; independent
  `CODE_REVIEW_PASS`, подтверждённые fixes и re-review.
- [ ] Обновить evidence/progress/PRD только проверенными результатами. Scoped
  conventional commits, без live reports/data/generated artifacts и чужих edits.

**Exit:** пользователь принимает инженерное поведение и скорость. Новые defaults
остаются OFF/robust. Улучшение будущих результатов требует отдельной walk-forward
проверки; этот план не выдаёт research score за trading admission.

## Handoff

Документы предоставляются для согласования. Следующее действие после принятия —
M0 evidence, затем отдельное решение о начале реализации и способе исполнения.
Никаких implementation этапов не считать выполненными по наличию этого плана.
