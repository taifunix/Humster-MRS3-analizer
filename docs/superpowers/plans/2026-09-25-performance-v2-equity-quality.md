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

**Version:** canonical R7.3, 2026-09-25. Independent Opus 5
`PLAN_APPROVED`, 2026-09-25. R7.3 replaces conflicting R6.1 rules below;
the R6.1 findings ledger is historical only. Approval does not accept M0
coverage/performance evidence and does not authorize runtime implementation.
Implementation was separately authorized by the user. M0–M4 are accepted;
M5 remains open. Checkboxes close only after verified evidence and
independent review.

## R7.3 canonical override (normative)

Follow the [R7.3 contract in the spec](../../specs/2026-09-25-performance-v2-equity-quality.md#0-canonical-r73-contract-normative-supersedes-r61).
This block supersedes any contradictory lower checklist text; revise that text
before starting M1. In particular, the old common-T preflight, action/position
gap proofs, age-based H selection, <=5% gap-unrankable gate, and
`CONTINUATION_UNASSESSED` are deleted.

- Pure facts depend only on stored report `[start,T]` and owned equity rows.
  No actions/positions/trade counts/timeframes are inputs; zero action-fact
  queries/rows are attributable to equity computation. Optional trip strata
  are separate and may be omitted.
- Enforce aware UTC `TIMESTAMPTZ`, numeric Decimal finite values and inclusive
  source ownership. Convert Decimal inputs directly, otherwise parse
  `Decimal(str(value))` with no float round-trip. Precedence is structural
  source validation -> in-report nonpositive Decimal -> age/window availability
  -> metrics/class/score. Malformed/nonfinite/out-of-interval/invalid-index
  input is UNKNOWN_INVALID_SOURCE/NOT_EVALUATED even if an in-report value is
  nonpositive; only structurally valid source can be NONPOSITIVE_EQUITY/BLOCK.
- `E(t)` is right-continuous, duplicate timestamps use greatest sample_index,
  and all raw duplicates remain in raw risk. Carry unconditionally to the next
  update and T; gaps and quiet tails never invalidate. No future/pre-report
  baseline or fallback from corrupt input.
- W baseline exists iff start<=T-W and an in-report sample exists at/before
  T-W. Select longest available H in 28/14/7; no baseline => under7
  INSUFFICIENT_HISTORY, otherwise MISSING_BASELINE. Compute each available
  window on the exact T-anchored 6h grid with Decimal38 formulas and eps
  `1e-8`; raw H D/P uses `[T-H,T]` and the exact no-double-anchor rules from
  spec §0.
- Decision policy: H-UP + all shorts NONDECLINING => GROWING class0/PASS;
  H-UP + any short decline => WEAKENING class1/PASS; H-flat => FLAT class2,
  blocked only by enabled ERF; other valid non-UP => DECLINING_OR_MIXED
  class2/3 BLOCK. Short decline alone never blocks. Remove
  `CONTINUATION_UNASSESSED`.
- Ranking is exactly `(class,-score12,D,P,-H,strategy_id)`; unscoreable nulls
  are RESERVE outside N. Each candidate uses its own stored T; mixed report
  ends are valid, with no common-T gate/freshness/date tie. Include T in
  revision/digest so T-only changes recompute facts.
- Any later worker/cache source contract is equity-only. UI defaults remain
  OFF/robust, and four XLSX columns do not change. Production work follows
  the user's separate implementation authorization.

## Global constraints

- Только Performance v2 card «4. Pareto and filters» и её существующие consumers.
- `filter_equity_regime` OFF по умолчанию; optional `rank_robust_top_n`,
  `method=robust_v1|equity_quality_v1`; omitted method = robust; N default 20.
- Окна 7/14/28d, UTC `T=report_end`, H по baseline availability 28/14/7;
  report age gates only under-7 insufficient history, not H selection.
- При UP H и валидных facts flat/declining короткие окна не вызывают ERF BLOCK;
  WEAKENING только понижает приоритет enabled equity-ranking.
  Нет minimum trades/week и hard-exclusion только за возраст <28d.
- Decimal precision 38; eps `1e-8` log-pp/30d; score HALF_EVEN 12 знаков.
- Fixed T-anchored 6h grids; raw DD/P only on exact H path. No gap/position
  invalidation, historical blocks, HWM age, or common-T grouping.
- Старый wallet/A/B расчёт не менять; новый путь MTM не snap к flat boundary.
- Missing required cache = request error; cached unknown = ready/unassessed.
- Legacy hashes/columns/robust decisions не меняются при отсутствии новых полей.
- Один source load, существующий pool/worker limit, DuckDB reader threads=1,
  порция <=2*workers; readers закрыты до единственного writer.
- Четыре новые видимые Excel колонки; existing final score/rank переиспользовать.
- Не трогать чужие dirty changes; не запускать tester и не писать в live DB.
- Все проверки Python: `.venv\Scripts\python.exe -m pytest ...`.

## Review focus

1. Sparse updates/quiet tails use equity-only right-continuous carry; gaps never
   need action/position-state proof and do not invalidate metrics (R7.3).
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
| R-06 | Planner R2: лишние DD windows/неявный fallback | H-only DD/P retained; R6.1 age-selected H superseded by R7.3 baseline-availability H |
| R-07 | Planner R2: незаданные benchmark floors | Accepted R3: конкретные proposed бюджеты/повторы/сценарии |
| RV3-01 | Advisor: одинаковые strategy_id у lot variants | Premise rejected / resolved: loader dictionary keyed strategy_id, variants distinct; uniqueness invariant и permutation tests |
| RV3-02 | Advisor: passthrough можно спутать с PASS | Accepted R4: только decisive PASS; разные skip reasons для unassessed/blocked |
| RV3-03 | Advisor: readiness нового cache неявна | Accepted R4: old-ready AND presence/revision/algo/canonical digest для enabled consumers |
| RV3-04 | Advisor: неоднозначный batch bound | Accepted R4: <=w active sources, <=2*w metadata jobs/compact results |
| RV3-05 | Advisor: lifecycle deletion неполон | Accepted R4 с проверкой кода: REPLACE transaction; prune verified backup/restore, других parent-delete APIs не найдено |
| RV3-06 | Advisor: sparse quality не имеет release gate | R6.1 coverage/action-proof disposition superseded by R7.3 unconditional carry and baseline availability; reassess only with R7.3 M0 evidence |
| RV3-07 | Advisor: mixed T обнаруживается поздно | R6.1 common-T gate superseded: each current candidate uses its own T |
| RV3-08 | Advisor: LRU может вернуть старые решения | Accepted R4: settings-independent facts only; fresh policy outside LRU |
| RV4-01 | Advisor: read-only schema v5 после миграции | Accepted R5: strict readable v5/v6, no DDL; damaged v6 fail-closed; explicit writable initialization |
| RV4-02 | Advisor: toggle-dependent facts и single LRU entry | Accepted R5: toggle-independent cached reads; explicit sentinels; cold publish legitimate miss |
| RV4-03 | Advisor: нет оценки sparse correction-block rate | Historical R5/R6.1 <=5% gap-unrankable gate superseded by R7.3 unconditional carry; M0 reports availability/invalidity evidence only |
| U6 | Пользователь: «только понижать место» | Accepted R6: H-UP GROWING/WEAKENING оба PASS; ranking class priority прежний; RV4-03 M0 теперь проверяет zero short-only BLOCK и ranking demotion |
| R6-01 | Advisor: комбинация ERF ON + robust | Clarified R6.1: намеренно без equity-штрафа; понижение только new method; controlled robust-equivalence fixture |
| R6-02 | Advisor: недостаточно adversarial rank tests | Accepted R6.1: class1 score10 ниже class0 score1; within-class tie-chain; оба коротких окна отрицательны; policy/hash assertions |

## File map and interfaces

Новые production files — только:

- `src/mrs3/performance_v2_equity_quality.py`: pure equity-only geometry and
  typed facts; frozen `EquitySample(sample_index,timestamp_utc,equity)` only.
  Inputs include stored report start and report-end T; no action/position input.
  UTC, ownership, duplicate timestamps, baseline availability, finite Decimal,
  and window/risk semantics follow canonical R7.3 spec §0.
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

## M0 — проверка идеи и критериев до implementation (R7.3)

**Files:** spec/this plan; future `scripts/benchmark_equity_quality.py` и
`docs/superpowers/plans/2026-09-25-performance-v2-equity-quality-evidence.md`
только при назначении этапа. Новый ADR создаётся после принятия контракта,
со следующим свободным номером; не резервировать занятый номер заранее.

- [ ] Freeze ACTIVE/current-result metadata identity incl. each candidate's T;
  read source/report/action/equity timestamp types and enforce native UTC
  `TIMESTAMPTZ`/aware offset +00:00. Report exact SQL queries by purpose.
- [ ] Run the canonical equity-only self-check matrix from spec §0, including
  5m/4h invariance, quiet tails, arbitrary gaps, exact baseline/L/T and duplicate
  paths, invalid/non-finite/nonpositive precedence and collisions, H availability boundaries,
  mixed T, score/class behavior, and duplicate raw D/P deltas. Repeat output and
  verify byte-identical self-check output.
- [ ] Read-only scan largest feasible deterministic scope (default bounded
  per Pair+Side sample; `--full` only if resource/time budget permits). Make
  sample selection counts/reasons auditable and assert no duplicate/drop.
  Report overall/by-age baseline availability, invalid/outside/nonpositive,
  H/classes/score/rank/Top-N, raw/grid rows, max internal gap, last-sample-to-T
  carry duration, duplicate raw risk deltas, and timeframe/inactivity cross-tabs
  only for available fields. Assert ordered streamed groups and per-result row
  counts reconcile exactly to scanned rows, including empty results and final
  group flush. Explain limits; no unsupported representativeness.
- [ ] Optional action trip strata may be omitted. If retained, isolate and count
  their action query/rows separately; equity calculation must issue zero action
  queries and read zero action rows.
- [ ] Compare diagnostic ER under 1/3/6h grids and raw-vs-grid ER when feasible;
  canonical facts remain fixed at 6h. Preserve the previous warm-cache baseline.
- [ ] If feasible, measure R7.3 diagnostic with one code warmup and three
  measured repeats; record wall/RSS/query/rows/writes and exact DB size/mtime/
  schema/catalog/table-count invariants before and after. No copy, backfill,
  migration, DB write, Panel, tester, or service restart. Do not claim budgets
  pass absent comparable old/new measures; proposed cold/warm/RSS budgets are
  spec §0.
- [ ] M0 has no common-T blocker and no action-proven-gap or <=5%
  coverage-unrankable gate under R7.3. Record M0 limits and unresolved user decisions only from
  observed R7.3 evidence. Approval is not M1 authorization.

**Exit:** reproducible R7.3 evidence and exact limitations; no claim of
predictive validity, accepted speed budgets, or implementation readiness.

## M1 — pure engine, lifecycle и быстрая единая подготовка

**Accepted:** 2026-09-25, independent Opus 5 `CODE_REVIEW_PASS` for pure facts,
schema/cache lifecycle, v5 reads and worker integration. Final relevant suite:
429 passed, 2 platform skips. The synthetic seven-window worker fetch fell
from seven SELECTs to one with identical cached metrics. Comparable full-corpus
wall/RSS budgets remain an M5 gate, not an M1 claim.

**Files:** оба новых модуля; existing store/import/prune/windows/selection;
`tests/test_performance_v2_equity_quality.py`,
`tests/test_performance_v2_equity_cache.py`, existing store/import/prune/windows tests.

- [ ] Сначала добавить failing pure checks на R7.3 §0: timezone/source
  ownership, baseline availability (not age-selected H), finite Decimal,
  structural validation before nonpositive classification, duplicate timestamp sample_index, exact L/T anchors,
  scale/log-grid invariance, classification and ranking score.
  Flat-tail fixture uses equity only: expected H-UP, flat short windows and
  GROWING/PASS despite a quiet multi-day tail.
- [ ] Implement one ordered equity-only pass, right-continuous 6h grids,
  Decimal38 OLS/ER and exact raw H D/P. The engine API must not accept/read
  actions or positions. No historical scan or float conversion.
- [ ] Replace old open/unknown gap/action-proof tests with arbitrary internal,
  leading and terminal carry tests, in-report predecessor, missing baseline,
  pre-report/future invalidity, nonpositive inside/outside H, and duplicate raw
  risk-path tests. Gaps never turn otherwise valid equity facts unknown.
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
- [ ] Расширить существующий worker: union missing jobs; один source query
  который выбирает только owned equity rows needed for available windows,
  retaining rows required for raw H D/P. Metadata revision (including stored
  report T) and equity source are read in one read transaction. No actions or
  position predecessor are loaded for equity facts.
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

- [x] Red/green parser `filter_equity_regime`, fixed pair_side, default OFF;
  submitted/effective order, explicit/implicit lot first, rank last.
- [x] Табличные policy tests use baseline availability: H28 with short
  nondeclining => GROWING/PASS; H28 plus any short decline => WEAKENING/PASS;
  H14/H7 use only available shorts; H-flat => FLAT/class2 (ERF-only block);
  other valid non-UP => DECLINING_OR_MIXED/BLOCK; under7 and missing baseline
  unassessed; structural malformed/out-of-interval/invalid-index input is
  UNKNOWN_INVALID_SOURCE even when an in-report nonpositive value also exists;
  only structurally valid in-report nonpositive is BLOCK. Gaps/tails do not
  invalidate otherwise valid facts.
- [x] Реализовать self-only gate из готовых facts. Stage counts оставляют
  стандартную семантику; отдельный unassessed count и reason, не ложный PASS.
- [x] Red/green mixed lot group: old winner BLOCK, sibling PASS -> не
  схлопывать group перед ERF. All PASS -> прежний lot winner; unknown member
  -> skip collapse. Filter OFF -> byte-equivalent legacy result frame.
  All-unassessed тоже skip; any unassessed reason
  LOT_GROUP_EQUITY_UNASSESSED имеет приоритет над LOT_GROUP_EQUITY_BLOCKED.
  Passthrough != decisive PASS.
  Mixed GROWING/WEAKENING с валидными facts — all-PASS, обычный lot collapse;
  class1 не должен остаться BLOCK по старому контракту.
- [x] Проверить prior elimination, отсутствие resurrection и отсутствие
  raw compute при selection/preview.

Run: `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection.py`.

**Exit:** отдельный filter-only режим работает без включённого ranking.
M2 independently reviewed by Opus 5: `CODE_REVIEW_PASS` after focused race,
cache-integrity, lot-group and XLSX regressions. Final M2 suite:
`273 passed, 2 existing platform skips`; no live DB mutation or backfill.

## M3 — альтернативный ranking и воспроизводимый review

**Files:** selection/selection_review и matching tests.

- [x] Сначала golden tests legacy request JSON/hash/results. Optional method
  только у rank stage; отсутствующий/disabled rank инертен; robust untouched.
- [x] Реализовать `_rank_equity_quality` и общий dispatch, не копировать analog
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
- [x] Red/green score cases: при G>0 рост D/P или снижение Q понижает score;
  G<0 большая DD делает score хуже, не ближе к0; near-eps score0; deterministic
  ties; flat7 не обнуляет положительный G28; young growing может быть выше old
  weakening; H — только поздний tie-break.
- [x] Assert candidate strategy_id unique; different lot-strategy IDs с
  одинаковыми метриками сохраняют порядок при permutations/worker counts;
  duplicate IDs дают typed invalid input. Не менять tie-key из-за неверного
  предположения, что order rows являются отдельными candidate rows.
- [x] Проверить missing-score RESERVE (не занимает N), rankable non-UP,
  Top N по analog representatives, prior REJECTED, candidate-specific T in
  facts revision/digest, T-only recompute, and отсутствие влияния чужого
  RETEST cohort. Mixed-T is valid; no common-T preflight or date tie-break.
- [x] Version-aware canonical projection: old request не получает новые null
  fields от asdict. Новый contract v2 сохраняет method/policy/effective order,
  revisions/digests/decision facts в существующих JSON-полях, не новой таблице.
  R7.3 contract/T must be represented in source revision/digest; policy-only
  changes do not invalidate otherwise current facts or silently change scoring.
  Assert snapshot policy ID и byte-identical прежний legacy-v1 canonical hash.
- [x] Red/green v1/v2 review round-trip, snapshot recheck при same-ID REPLACE,
  old snapshot после удаления cache, equivalent-run checks и rollback при race.

Run: `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection.py tests/test_performance_v2_selection_review.py`.

Accepted M3 evidence (2026-09-25): 209 passed, 6 pandas warnings; `git diff --check`
passed. Opus 5 final `CODE_REVIEW_PASS` after v1 disabled-method, v2 stage-order,
source-revision/timezone, mixed RETEST reserve/Decimal and decision-facts checks.
M4 candidate-LRU hydration, XLSX columns and Panel integration are accepted
after independent Opus 5 `CODE_REVIEW_PASS`; M5 remains open.

**Exit:** filter-only/rank-only/both/legacy воспроизводимы; review не доверяет Excel precision.

## M4 — UI, общий XLSX, readiness и RETEST consumers

**Files:** panel.py, panel_performance_v2.py, panel_web/app.js/index.html,
shared writer в selection.py; tests/test_panel_performance_v2.py,
test_panel_performance_v2_export.py, test_panel_performance_v2_retest.py,
test_panel_static_ui.py, selection/review tests.

- [x] Red/green service readiness учитывает только enabled equity consumers;
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
- [x] Добавить checkbox №2 и native method select у текущего Top N, не новую
  панель. Fixed-prefix barrier действует также при перемещении соседнего ряда.
  Disabled rank не требует кэш; stale HTTP responses не перетирают новый выбор.
- [x] Показать предупреждение rank-only+lot и unassessed count. Labels,
  keyboard/aria-live сохраняют доступность; CSS framework не нужен.
  Help явно различает short-window WEAKENING (понижение equity-rank) и ERF
  BLOCK (основной H не UP либо nonpositive); другие filters не отключаются.
  Для ERF ON + robust явно писать: PASS без нового equity-штрафа; понижение
  только при equity_quality_v1. Никакого автоматического переключения метода.
- [x] Shared writer добавляет только четыре equity columns для нового request.
  Existing Final score/rank переиспользуются, метод явно подписан без изменения
  protected headers: cell comment и workbook metadata, без header rename.
  State/basis объясняют short/unknown/invalid; null blank.
- [x] XLSX tests: legacy columns неизменны, new block справа, исключённые
  строки сохраняются; нет generic .01 rounding для equity metrics и нет
  полной диагностики в hidden columns. Full facts находятся в snapshot JSON.
- [x] Read-only Performance export показывает только свежие имеющиеся facts,
  не compute/migrate/write и не меняет manual FINALIST/RETEST.
  Valid v5 fixture экспортируется прежними колонками с неизменным catalog;
  review-read работает. Snapshot/review-import writers требуют explicit
  initialized v6. Test той же копии после normal writable initialize v5->v6.
- [x] Проверить recovery/bulk retest/control export: method не теряется,
  exact successful cohort и существующие snapshot/review guarantees сохранены.

Run: `.venv\Scripts\python.exe -m pytest tests/test_panel_performance_v2.py tests/test_panel_performance_v2_export.py tests/test_panel_performance_v2_retest.py tests/test_panel_static_ui.py tests/test_performance_v2_selection_review.py`.
Run: `node --check src/mrs3/panel_web/app.js`.

Accepted M4 evidence (2026-09-26): 524 passed, 4 Windows symlink skips across
selection/review/Panel/export/RETEST/static UI; 132 passed in the separate
equity-quality/cache/store/benchmark slice. `node --check` and scoped
`git diff --check` passed. Independent Opus 5 selection/writer, Panel/UI/export
and final integration reviews returned `CODE_REVIEW_PASS`. Local headless Chrome
QA at 820/901/1100/1279/1280px found no selection overflow or clipped inputs;
portfolio breakpoint remained 760px. No live DB migration/backfill or tester
run was performed. M5 comparable full-corpus speed/RSS evidence remains open.

**Exit:** минимальный UI и четыре новых колонки, без скрытой новой вычислительной ветки.

## M5 — performance, интеграция и приёмка

Partial tooling evidence (2026-09-25): the copy-only benchmark harness
`scripts/benchmark_performance_v2_equity_selection.py` received independent
Opus 5 `CODE_REVIEW_PASS` after 13 fixture tests. It measures four all-warm
consumer modes and cold/backfill recalculation on isolated v6 copies, with
explicit partial SQL/fetched-row and parent-RSS scope. Prior-runtime baseline,
one-REPLACE, full-corpus wall/RSS budgets and M5 acceptance remain open.

**Files:** benchmark/evidence из M0; spec/plan/progress/PRD по verified facts.

**Cache-path audit to measure:** M1 removed the proven per-result window-cache
read duplication (seven SELECTs to one in the synthetic seven-window case) and
one duplicate publication revision check. M4/M5 must separately measure warm
preview's repeated scoped `window_metrics` scans and source/equity token reads,
and recalculate-all's per-pair canonical facts decoding. Preserve full-payload
validation for enabled consumers until a safe equivalent is demonstrated. The
equity-only cold path still performs an SQL full-history aggregate for exact
raw/invalid counts but transfers only bounded rows; do not call that a bounded
database scan or remove it without a replacement validity source.

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
