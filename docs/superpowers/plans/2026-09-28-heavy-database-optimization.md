# План оптимизации тяжелых процессов БД

**Версия:** R5, source R4 `PLAN_APPROVED`; учтены fetched upstream changes.
**Дата / implementation baseline:** 2026-09-28 / `origin/main@8f59c2c`, DuckDB `1.5.5`.
**Ветка:** `perf/heavy-db-optimization`, worktree `.worktrees/heavy-db-optimization`.
**Статус:** R5 `PLAN_APPROVED`, независимый Claude Opus 5/high, 2026-09-28;
user authorization на внедрение получена, runtime еще не изменен.

**Goal:** исправить подтвержденные дефекты и уменьшить время/память цепочки
HTML → Source DB → materialization → analysis → PerformanceDB → selection/export.
**Architecture:** сохранить существующие stages, один publisher общей БД,
bounded CPU/read workers, native bulk SQL и атомарную публикацию.
**Tech Stack:** Python `.venv`, DuckDB, concurrent.futures, существующие pandas/pytest.
**Spec / evidence:** [аудит](../../reports/2026-09-28-heavy-database-processes-audit.md),
[AGENTS](../../../AGENTS.md), [PRD](../../../PRD.md), [progress](../../../progress.md).
Перед каждой будущей правкой поведения обновляется соответствующая active spec.

Пользователь разрешил внедрение в новой ветке после получения изменений из Git.
Runtime меняется только в этой ветке; рабочие БД и tester в текущую задачу не входят.
`safe_to_delete` явно отложен и не блокирует остальные задачи.
Новая схема, постоянный cache/service, зависимости и универсальный connection pool
не требуются. Legacy v3/v4 runtime не переносится и не переписывается этим планом.

## Приоритет и зависимости

Начать с четырех небольших исправлений PerformanceDB. Затем снять baseline
конкретного следующего участка и сделать ANA-01, DEC-01 и CALC-01: у них наиболее
прямой резерв в количестве операций. Source и большой import требуют более
осторожного измерения, поскольку много работы там уже оптимизировано.

Нумерация фаз группирует контуры, а не требует полного последовательного waterfall.
COR-01 обязателен перед OPT-01; COR-05 — перед IMP-01/03; COR-03 — перед сравнением
XLSX. Другие контуры независимы. MAT-02 можно исправить отдельно от ускорения metrics.
COR-04 и legacy empty evidence остаются deferred.

## R5: порядок исполнения и изменения upstream

Binding contract: [active optimization spec](../../specs/2026-09-28-heavy-database-optimization.md).
Получены коммиты `128fccb`, `a7efa97`, `8f59c2c`. Ordinary SINGLE_MODE доверяет
только repository `Output`; перенаправленные roots/descendants, symlink/reparse,
non-regular files, outside paths и неверные hashes отклоняются. Первый явный
Panel import создает только отсутствующий canonical target через writer lock,
validated staging и no-clobber hard link. Существующие empty/corrupt/foreign или
конкурентно созданные targets не переинициализируются и не перезаписываются.
Supported older schemas по-прежнему могут мигрировать под importer lock.

No partial publication означает одну атомарную публикацию **допущенного набора**.
Существующие mode-specific rejection/skip policies сохраняются: например, stale
frozen member может быть отклонен, а его валидные siblings — опубликованы.
Нельзя делать parser/chunk commits или вводить новое правило all-inbox-valid.

Baseline новой ветки: `221 passed, 5 skipped, 8 warnings`, 164,77s, tests
`panel_performance_v2`, `performance_v2_input/html/optimizer/prune`.
Windows skips относятся к недоступным symlink. Исторические замеры ниже относятся
к первоначальному audit baseline и не являются результатом новой реализации.

Root владеет документацией, интеграцией, review и commits. Один Executor одновременно,
с явным владением code/test files; каждый task получает отдельный review и scoped
commit. Root обновляет module spec, когда umbrella не содержит точного контракта.

| Task | Планируемое изменение | Ownership Executor (src/mrs3 и соответствующие tests) |
| --- | --- | --- |
| T1 | COR-01 invalid prepared repair | performance_v2_optimizer |
| T2 | COR-02 prune prepared FK children | performance_v2_prune |
| T3 | COR-03 completed Trades export | panel_performance_v2, test_panel_performance_v2_export |
| T4 | COR-05 source HTML order | performance_v2_html/import, их tests |
| T5 | MAT-02 incremental progress | source_v6_materializer, materializer/worker tests |
| T6 | ANA-01 bounded bulk publication | source_v6_analysis_fresh, existing analysis/benchmark tests |
| T7 | DEC-01 bulk history replay | performance_v2_selection_review, его tests |
| T8 | CALC-01/02 shared seven-window timeline/bulk cache | performance_v2_windows/selection, их tests |
| T9 | SRC-03 streaming identity hash | source_v6_merge, его tests |
| T10 | SRC-01 parallel existing staging proof | source_v6_importer/storage, их tests |
| T11 | MAT-01/03 shared preparation/read batches | source_v6_materializer/storage, materializer/worker tests |
| T12 | IMP-03 one HTML inventory boundary | performance_v2_html, shared performance parser, html/import tests |
| T13 | IMP-01/02 bounded preparation/publication | performance_v2_import, его existing helpers, import/input/store/Panel tests |

До T1 root review/commit документации. Для T12/T13 обязательны upstream bootstrap
и Output-containment regressions в `test_panel_performance_v2.py`. Проверять точные
ADD/REPLACE outcomes, accepted subset, rollback/readback, phases и writer-held time.
Не менять переносом CPU work окончательные IDs и Decimal(38,12).

Остальные задачи из детализации ниже — **evidence-gated follow-ups**: root измеряет
соответствующий путь на bounded representative fixtures/clone и назначает Executor
при материальном повторении работы: SRC-02 overlap, SRC-04 ownership RSS, MIG-01
one-time backfill, EQ-01 metadata/upserts, WIN-01 cold pair, CACHE-01 warm helper,
SEL-01/02 action scans/warm validation, OPT-01 digest, RET-01 global freeze.
Неподтвержденные гипотезы не превращаются в обязательный refactor. Результат каждого
profile фиксируется как accepted change либо measured/no change с причиной.
Legacy, review/control, XLSX residual, remote и plateau остаются profile-only.
COR-04/B5 и B7 по-прежнему явно deferred.

Приемка каждого task: failing-first → minimal diff → focused/relevant broader
`.venv` checks → exact output comparison и соответствующие замеры → diff check →
независимый `CODE_REVIEW_PASS` → progress и scoped conventional commit.
Full suite запускается один раз после интеграции, повторяется при invalidating fixes.
Финальный whole-branch review обязателен. Ветка остается отдельной до назначения
интеграции; rollback — revert scoped commit и прежний artifact/backup.

## 1. Подтвержденные ошибки

- [ ] **COR-01 / B1 — optimizer prepared payload.**
  `src/mrs3/performance_v2_optimizer.py`, `tests/test_performance_v2_optimizer.py`.
  Failing-first: совпадающие metadata и поврежденный `prepared_json={}` → prepare
  сообщает available, но strict read падает. Writer не должен metadata-skip строку,
  явно пересобранную после невалидного decode. Использовать существующие validators;
  не добавлять еще один полный hash/decode проход на каждом корректном cache hit.
  Приемка: repair проходит strict read, корректный repeat не пересобирается,
  source revision recheck сохранен, cross-revision digest reuse отсутствует.
- [ ] **COR-02 / B2 — prune FK.**
  `src/mrs3/performance_v2_prune.py`, `tests/test_performance_v2_prune.py`.
  Добавить prepared children в preview counts и deletion до `strategy_results`.
  Сохранить последовательный autocommit согласно
  [prune spec](../../specs/2026-09-11-performance-v2-prune.md), checkpoint, backup,
  restore и dry-run. Failing-first: FK failure при наличии prepared row;
  затем successful deletion и injected-failure restore. Не делать единый cascade transaction.
- [ ] **COR-03 / B3 — XLSX Trades.**
  `src/mrs3/panel_performance_v2.py`, `tests/test_panel_performance_v2_export.py`.
  Только exported `Trades` берет completed-round-trip count из готового cache.
  Fixture: cached=1, raw total=777 → XLSX=1, raw остается777. Missing cache остается
  unavailable; export не рассчитывает окна и не пишет БД.
- [ ] **COR-05 / B6 — исходный порядок HTML actions.**
  `src/mrs3/performance_v2_html.py`, `src/mrs3/performance_v2_import.py`.
  Проверить source order до normalization либо сохранить его до существующего rejection.
  Actual swapped HTML → parser → warmup/import должен дать `ACTIONS_OUT_OF_ORDER`;
  fixture сейчас сортируется в indexes `[1,0]` и принимается. Rejection не публикует
  данные; equal timestamps сохраняют стабильный source order. Existing dataclass-only
  test остается дополнительным. Уточнить error mapping на parser/import boundary в spec.

Все четыре исправления имеют цель correctness, а не обещание ускорения.
Проверять каждое своим тестом до/после и отдельно отправлять на независимый review.

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_optimizer.py tests/test_performance_v2_prune.py -q
.venv\Scripts\python.exe -m pytest tests/test_panel_performance_v2_export.py -q
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_html.py tests/test_performance_v2_import.py -q
```

## 2. Baseline и измерения

- [ ] Использовать coverage ledger из отчета и существующие benchmark scripts/tests;
  не создавать второй ledger или общий новый harness без необходимости.
- [ ] Для изменяемого пути фиксировать commit, versions, immutable input identity,
  workers, elapsed median, peak RSS, SQL/statement count и exact output identity.
  Cold/warm и startup измерять отдельно. Before/after — один host и snapshot,
  желательно не менее трех повторов. Full corpus — только offline frozen clone.
- [ ] Для CPU/read worker workloads сравнить 1/4/8/16; сохранять default16 до
  сопоставимых замеров. Ограничить одновременно in-flight objects по количеству
  и приблизительному объему bytes, а не только workers. Один writer общей БД.

Уже имеющееся evidence, не новый прогноз: materialization60points 24,41→9,43s;
W6 88/1764s≈5%; запись1000rows autocommit2,3534s, transaction0,5257s,
registered relation0,0473s. Последний опыт доказывает цену SQL-способа записи,
а не 50× ускорение полного analysis. M5 withdrawn thresholds не возвращаются.
Нет произвольного обязательного процента; принимать оптимизацию по точности,
повторяемому практическому эффекту и цене усложнения.

```powershell
.venv\Scripts\python.exe -m pytest tests/test_benchmark_source_v6.py tests/test_benchmark_performance_v2_equity_selection.py -q
```

## 3. Source import и merge

- [ ] **SRC-01 — parallel W6.** `source_v6_importer.py`, `source_v6_storage.py`.
  После закрытия writer использовать существующий `verify_published_identity_parallel`
  на той же staging DB. Explicit caller control сохраняет standalone reducer proof;
  нельзя глобально отключить checks. Verifier failure запрещает публикацию.
  Manifest, payload identity, quarantine и hashes совпадают при разных workers.
  Full import: **5–15%**, **15–30%** только если profile покажет dominant serial tail.
- [ ] **SRC-02 — overlap persistence.** Пакетно готовить/писать только multi-fragment
  resolutions через имеющиеся helpers. Singleton с пустыми requests уже не делает
  DB calls. Приемка: те же reasons, manifest/hashes, bounded memory, no partial publish.
- [ ] **SRC-03 — streaming hash merge.** `source_v6_merge.py:_content_identity`.
  Заменить `read_bytes()` ограниченными chunks с теми же именами, length prefixes,
  absent markers. Все пять preflight/execute checks сохранить; проверить byte-for-byte
  digest equivalence и изменяющиеся DB/WAL/TMP. Не подменять hash размером/mtime.
  Merge: **0–15%**, основной эффект — память. Metadata reuse / fewer scans с возможными
  **10–30%** — отдельная будущая гипотеза с доказанным immutable/recheck contract.
- [ ] **SRC-04 — optional ownership memory.** После profile проверить chunked/native
  generation вместо полного списка day rows плюс pandas copy
  (`source_v6_merge.py:301`, `source_v6_storage.py:1074,1454`). Exact ownership/counts/order.
  Гипотеза: peak RSS **−20–40%**, время этапа **−0–10%**.

Fresh-target compact и COUNT checks до/после него сохраняются. COUNT не считается
автоматически full scan. Пропуск compact требует отдельной spec. COR-04 final payload
proof и legacy empty evidence отложены; параллельная текущая staging-проверка не
заявляется исправлением COR-04. [ADR-0015](../../decisions/0015-source-v6-published-file-identity-readback.md).

```powershell
.venv\Scripts\python.exe -m pytest tests/test_source_v6_importer.py tests/test_source_v6_storage.py tests/test_source_v6_merge.py -q
```

## 4. Materialization и analysis

- [ ] **MAT-01 — общая подготовка A/B.** `source_v6_materializer.py` и metric helpers.
  Point decode уже выполняется один раз; переиспользовать seam/cycle/series preparation
  в полном и B14day окне. Не заменять B простым срезом готовых A metrics.
  Приемка: exact values/witnesses, carry-in/open-tail, quiet-tail, window boundaries
  и unavailable reasons. Весь materialization: **10–25%** вместе с MAT-03.
- [ ] **MAT-02 / B4 — progress и очередь.** Получать completed futures по мере
  завершения вместо успешного ожидания всех через FIRST_EXCEPTION; ограничить in-flight.
  Quick/slow test: первое completion до окончания slow. При ошибке прекратить новые
  submissions, дождаться закрытия файлов running workers перед Windows cleanup.
  Ускорение вычислений не обещается.
- [ ] **MAT-03 — process-local read connection.** `source_v6_materializer.py`,
  `source_v6_storage.py:decode_fragment_slice`. Небольшие batches на connection либо
  один RO connection на worker. Покрыть initializer/restart/reopen/close и snapshot
  identity; connection не пересекает границы процессов. Без общего pool.
- [ ] **ANA-01 — bulk publication.** `source_v6_analysis_fresh.py:_publish`.
  Явная transaction и bounded registered relation/INSERT SELECT по существующему
  паттерну. Сравнить с transaction executemany; не держать второй полный JSON-list.
  Exact canonical JSON, без float coercion, те же digests и logical order, atomic rename.
  Failure не оставляет partial generation. Analysis целиком **5–30%**, запись **40–90%**.

Plateau support reuse и structures combinations сначала профилировать. Полный universe,
diagnostic rows, ranking predicates и witnesses не сокращать эвристикой. При одном
scope добавление scope workers не ускоряет последовательный анализ.

```powershell
.venv\Scripts\python.exe -m pytest tests/test_source_v6_materializer.py tests/test_source_v6_worker_materialization.py tests/test_source_v6_analysis_fresh.py -q
```

## 5. Performance import, migration, equity publication

- [ ] **IMP-01 — ограничить срок жизни parsed data.** `performance_v2_import.py`.
  Bounded chunks/process parsing → compact prepared chunks/private staging → один
  deterministic publisher. Ограничить не только futures, но и накопленные результаты.
  Перенести чистую CPU/IO подготовку до открытия writer; при reopen повторить current
  IDs, typed dedup, revision/stale/concurrent checks. Не публиковать частичные batches.
  Приемка: exact ADD/REPLACE outcomes, metadata/actions/equity/rejections, failure cleanup.
- [ ] **IMP-02 — batch metadata.** Заменить SQL в циклах пакетными reads/writes;
  schema metadata получить один раз на текущем validated connection. Phase8 preparation
  вынести из долгой writer-секции лишь с сохранением окончательных IDs и Decimal(38,12).
- [ ] **IMP-03 — общий HTML inventory.** `performance_v2_html.py`, `performance.py`.
  Переиспользовать decode/raw-markup inventory current-header gate и общего parser.
  Не убирать required/duplicate header, size/action limits и source order checks.
  **5–20% parser CPU**, включено в **10–30% полного Performance import**, не дополнительно.
- [ ] **MIG-01 — v4→v5 typed backfill.** `performance_v2_store.py`.
  Bounded JSON decode и set-based UPDATE внутри migration transaction вместо полного
  fetchall и запроса на action/result. Exact nullable/malformed facts, revision и rollback.
  Не делать eager optimizer rebuild истории. Большая однократная migration: **40–80%**.
- [ ] **EQ-01 — batch metadata/revisions и UPSERT.** `performance_v2_equity_cache.py:596`.
  Сейчас metadata читается per item. Проверить все result revisions пакетно до записи
  и сохранить equality recheck каждого result в publication snapshot. Использовать
  существующую batch transaction, без nested transaction. Exact facts/invalidation.
  Запись facts **10–30%**, equity workflow целиком **0–15%**.

Укорочение writer-held time измерять отдельно от времени импорта. RSS improvement
не объявлять автоматически elapsed improvement. Review/control import пока низкий
приоритет; ZIP/formula/identity/stale/atomic-ledger проверки сохраняются.

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_html.py tests/test_performance_v2_input.py tests/test_performance_v2_import.py tests/test_performance_v2_store.py -q
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_equity_cache.py tests/test_performance_v2_equity_quality.py -q
```

## 6. Windows и cache

- [ ] **WIN-01 — standalone pair.** `performance_v2_windows.py:get_or_calculate_window_pair`.
  Один source load и общая immutable preparation для cold пары; корректно смешивать
  cached/missing windows. Exact metrics/Decimal/error behavior; сохранить необходимый
  post-write readback. Условный эффект cold pair **20–50%** при дорогом source load.
- [ ] **CALC-01 — семь окон.** `performance_v2_windows.py`, `performance_v2_selection.py`.
  Selection уже читает source один раз/result. Переиспользовать flat timeline и boundary
  indexes; сохранить window-local peaks, fees, W0 exclusion и все metrics.
  Cold recalculation **15–35%** вместе с CALC-02.
- [ ] **CALC-02 — cache statements.** Bulk/native upsert вместо per-window statements
  внутри уже существующей batch transaction. Сохранить all-or-none publication.
- [ ] **CACHE-01 — helper no-republish.** Для fully-valid cache и `include_equity=False`
  вернуть пустой write set, сохранив доступность результата и progress. Обычный preview
  уже read-only; не выдавать это за новый фикс всего preview. Readiness/completeness остаются.

Equity-only full-history aggregate и bounded path+sentinels имеют разные функции,
одно чтение не удаляется как дубликат. LRU hit не сканирует raw actions; cold candidate
fill вправе читать aggregates даже при готовых window caches. Equity preview не
читает raw equity. Warm LRU **5–20%** относится прежде всего к SEL-02 следующей фазы.

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_windows.py tests/test_performance_v2_selection.py tests/test_performance_v2_equity_cache.py -q
```

## 7. Selection, history, optimizer, RETEST

- [ ] **SEL-01 — action aggregates.** `performance_v2_selection.py`.
  Общий scan/relation только с сохранением разных side/open predicates трех агрегатов.
  Fixtures: side flip, carry-in, partial close, exact facts/candidates/ties. Cold load **10–30%**.
- [ ] **SEL-02 — warm validation.** `panel.py`, `performance_v2_selection.py`.
  Объединить readiness/version/facts reads в одном snapshot. Не заменять содержимое/
  revisions TTL/mtime; REPLACE может сохранить result_id. Warm LRU preview **5–20%**.
- [ ] **DEC-01 — history replay.** `performance_v2_selection_review.py`.
  Bulk runs/reviews/selection rows → тот же ordered replay вместо трех SQL/run.
  Ordinary replacement, scoped overlay, latest user decisions и lineage идентичны.
  2/102 runs: exact equality, запросы не растут как3N. Этап **50–85%**, history-heavy
  export/catalog **10–40%**, не два независимых эффекта. Не брать просто последний run.
- [ ] **OPT-01 — строгие batch reads.** `performance_v2_optimizer.py`, после COR-01.
  Batch writer rechecks и один digest immutable source object в пределах request/snapshot/
  revision. Между snapshot/revision проверки не переиспользовать. Strict payload validation
  и candidate ordering обязательны; metadata-only trust contract не вводится. **15–40%**.
- [ ] **RET-01 — global freeze orders.** `performance_v2_finalist_retest.py`.
  Один bulk query orders, тот же frozen ordering, missing/duplicate behavior.
  Normal `performance_v2_retest.py` уже читает orders пакетно. Global preparation **10–30%**;
  ordinary preparation **0–5%**. Время реального tester сюда не входит.

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection.py tests/test_performance_v2_selection_review.py tests/test_performance_v2_optimizer.py -q
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_retest.py tests/test_performance_v2_finalist_retest.py tests/test_panel_performance_v2_export.py -q
```

## 8. Низкие приоритеты и приемка

- [ ] XLSX writer: **0–10%** сверх уже реализованных single-save/style reuse. После
  полного snapshot read закрывать RO connection до генерации workbook, если profile
  подтвердит пользу для времени занятости БД. Это не ускорение openpyxl само по себе.
- [ ] Review import: **0–10%** только после profile. Integrity checks сохранить.
- [ ] Legacy: **0–15%** полного подтвержденного workload как гипотеза; сначала измерить
  full prepared retention, replacement deletes, существующие validation boundaries.
  v3/v4 writer stage **20–60%** — лишь условная гипотеза отдельной работы по собственной
  spec и реальному report path; v4 требует соседний v3 codec.
- [ ] Remote delivery: сохранить full hashes на обоих концах. Сеть/диск сначала измерить.
  Empty legacy `COMMITTED 0,0,YES` отложен вместе с `safe_to_delete`; нового gate нет.
- [ ] Каждая будущая задача: обновленная spec → failing-first test для новой логики →
  минимальная реализация → focused/relevant broader `.venv` checks → exact identity
  comparison → `git diff --check` и staged diff → независимый `CODE_REVIEW_PASS` →
  `progress.md` и один scoped conventional commit.

Сохраняются source hashes/manifest/quarantine, версия конкретной схемы (v4 gate только
для соответствующего legacy import), Decimal, canonical JSON, witnesses, lineage,
determinism, atomic replacement, backup/restore и отсутствие partial publication.
Source MRS2 PnL не становится доказательством результата MRS3. Открытые M5 gates
этим планом не закрываются. Все проценты — ожидание сокращения elapsed, не обещание
и не сумма выигрышей. Полная таблица по всем модулям находится в отчете.

Полная будущая интеграция завершается `.venv\Scripts\python.exe -m pytest tests`
и `git diff --check`. Rollback — revert отдельного scoped commit и прежний atomic
artifact/существующий backup. Дополнительные постоянные flags/abstractions не нужны.

## Findings ledger: R1 → R4

| ID | Решение и отражение в текущем плане |
| --- | --- |
| RV-R1-001 | Accepted/deferred пользователем: COR-04 не prerequisite |
| RV-R1-002 | Accepted: prune sequential autocommit, backup/restore |
| RV-R1-003 | Accepted: не пропускать fresh compact |
| RV-R1-004 | Accepted: progress до slow completion, без speed promise |
| RV-R1-005 | Accepted: разные predicates selection сохранены |
| RV-R1-006 | Accepted: helper no-republish отделен от read-only preview/cold fill |
| RV-R1-007 | Accepted: существующая batch transaction, без nested |
| RV-R1-008 | Accepted: условные non-additive ranges, без произвольных thresholds |
| RV-R1-009 | Accepted: старые39 passing tests не заменяют regression |
| RV-R1-010 | R5 scope supersedes audit-only: user authorized branch runtime; live writes/tester вне scope |
| RV-R1-011 | Accepted: только XLSX Trades, raw report total неизменен |
| RV-R1-012 | Accepted: gates по зависимым путям, strict revisions/frozen ordering |
| RV-R1-013 | Accepted: overlap-only, process affinity, canonical publication, import/decision equivalence |
| ROOT-R2-001 | Accepted: Source5–15%, streaming SHA с пятью checks, COUNT сохранен |
| ROOT-R2-002 | Accepted: actual HTML order bug добавлен как COR-05 |
| ROOT-R2-003 | Accepted: reuse HTML inventory, parser estimate внутри import |
| ROOT-R2-004 | Accepted/partial deferred: legacy evidence отложен, runtime требует отдельной spec |
| ROOT-R2-005 | Accepted: pair source read отделен от seven-window flat preparation |
| ROOT-R3-001 | Accepted: ownership — Source SRC-04; equity — batch metadata/revisions EQ-01 |
| ROOT-R3-002 | Accepted: существующие test files и harness, без duplicate ledger |
| NEW-R5-001 | User authorized implementation; повторный user design gate не нужен |
| NEW-R5-002 | Fetched origin/main8f59c2c, isolated branch, dirty main сохранен |
| NEW-R5-003 | Ordinary SINGLE_MODE доверяет только Output |
| NEW-R5-004 | Redirect/containment/reparse/non-regular/hash checks сохранены |
| NEW-R5-005 | Missing canonical target bootstrap: lock/staging/schema/no-clobber hard link |
| NEW-R5-006 | Existing/concurrent/empty/foreign/corrupt/redirected targets не reinitialize/overwrite |
| NEW-R5-007 | Upstream Panel bootstrap и containment regressions обязательны для import tasks |
| NEW-R5-008 | Umbrella binding; module specs уточняются до изменения поведения |
| NEW-R5-009 | Concrete core tasks; прочие hypotheses имеют profile gate |
| NEW-R5-010 | Один Executor; scoped independent review; full suite в конце |
| NEW-R5-011 | Atomic admitted subset, mode-specific rejection policies сохранены |

Root при оформлении R4 уточнил точное имя `get_or_calculate_window_pair`, минимальный
repair COR-01 без лишнего validation pass и привязку warm preview estimate к SEL-02.
Это уточнения текущих mechanisms, не расширение scope.

Финальный Advisor проверил весь ledger R1–R4 и не нашел новых блокирующих замечаний.
Неблокирующие напоминания: B7 остается deferred; каждый будущий bulk-path проверяет
свой exact canonical digest, не опирается на слабый microbench XOR; default16 не
меняется по старому срезу64results. Эти условия уже включены в приемку выше.
