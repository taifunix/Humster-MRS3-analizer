# Panel: one button, one finished result (tick test and finalist retest end to end)

**Status:** approved by the user 2026-10-20 («Хотелось бы попонятнее и пологичнее…
подумай как упростить»; «Старые карточки 3, 4 и 5 — пока убери в настройки»; «пиши
план и спеку — работы отдам другому агенту»). Not implemented. Implementation plan:
[2026-10-20-panel-one-button-actions](../superpowers/plans/2026-10-20-panel-one-button-actions.md).
**Depends on:** [Panel for the simulation workflow](2026-10-18-panel-simulation-workflow.md)
(main screen, task area, plan dialogs), [pair registry](2026-10-18-pair-registry.md)
(pipeline, `pair_tests`), [SINGLE_MODE report collection](2026-09-28-single-mode-report-collection.md),
[finalist RETEST control](2026-09-09-performance-v2-finalist-retest-control.md),
[ADR-0083](../decisions/0083-one-performancedb-for-both-exchanges.md),
[ADR-0084](../decisions/0084-exchange-tag-on-every-strategy-name.md).

## Problem (what the user sees today)

One action is spread over three places:

1. **«Тик-тест MRS3»** on the main screen only prepares the batch and starts the
   tester (`MainScreenService._run_tick` → `store_ready_prepare` →
   `panel_job_submit("strategies.tester.start", collect_reports=False)`). The
   status says «ход — в карточке «3. Test and Import»».
2. **The reports reach the PerformanceDB only by hand**, in the card «3. Test and
   Import to Performance DB»: «Проверить» (`POST /api/v2/strategies/tester/verify-inbox`
   with the tester job) and then «Импортировать в Performance DB» (job
   `strategies.performance.v2.import` with `tester_job_id`).
3. **The registry learns about it later still.** `pair_tests` (kind `mrs3_tick`)
   and the «Тик-тестов» / «Стадия» columns of «Пары» are filled by the pipeline
   (`compute_pipeline` → `import_tick_tests`). The pipeline runs as the last step
   of «Синхронизировать», «Пересчитать историю», «Пересобрать READY» and
   «Проверить тестером», but **not** after a tick test or an import. So
   «Стратегии» (which reads the PerformanceDB directly) shows a result that
   «Пары» does not.

«Ретест финалистов» has the same shape: the main screen starts the job
(`strategies.performance.v2.finalist-retest.start`), and «IMPORT & REPLACE»
and «Применить эквити фильтр» are in the card «4. Bulk RETEST current
FINALIST».

There are also two task systems:
- the main screen's task area (`panel_tasks.TaskRunner`, child processes,
  JSON-line progress);
- the controller jobs (`panel_jobs`), whose progress the old cards show.

## Goal

**One button on the main screen gives one finished result, followed in one task
area.** After «Тик-тест MRS3» or «Ретест финалистов» ends:
- the results are in the PerformanceDB;
- «Стратегии» and «Пары» both show them;
- the registry is up to date.

The user never opens another card and never meets the word «Конвейер».

## Requirements

### R1. The tick test end to end

«Тик-тест MRS3» (plan dialog as today: strategies of the newest READY folder of
the exchange, test window) runs these steps as one main-screen task. The task
area shows each step by name, with progress:

1. **Подготовка** — `store_ready_prepare` (as today; an untagged READY folder
   refuses with «пересоберите READY», ADR-0084).
2. **Тестер** — the SINGLE_MODE tester job. Progress = reports done / total,
   from the controller job. On BingX, the data step (minute days) runs first, as
   today (`MISSING_DAYS` ends the task with that reason).
3. **Проверка отчётов** — the same check as «Проверить»
   (`strategies_tester_verify_inbox` of the committed job). The safety rule
   «import only a verified inbox» stays. The orchestrator performs the
   verification instead of a click, and a failed verification stops the task.
4. **Запись в базу** — the same import as «Импортировать в Performance DB»
   (`strategies.performance.v2.import` with the tester job), mode ADD. Its
   rejections (e.g. `EXCHANGE_NAME_TAG`, `PROTOCOL_V2_INCOMPLETE`) appear in the
   task's error list with the strategy name and code.
5. **Обновление таблиц** — the registry pipeline of the exchange: the same
   command the other actions run, `python -m mrs3.pair_registry pipeline
   --config … --exchange E`.

The task ends `done`, `failed` (step and reason shown) or `stopped`.

### R2. The finalist retest end to end

«Ретест финалистов» (plan dialog as today: cohort preview, «включая резерв»)
runs as one task with these steps:
- **Тестер** — `strategies.performance.v2.finalist-retest.start`;
- **Проверка отчётов** — the verification of the committed bulk job:
  `strategies_tester_verify_inbox`, which for this kind captures the bulk
  inbox;
- **Замена результатов** — `strategies.performance.v2.finalist-retest.import`,
  IMPORT & REPLACE;
- **Обновление таблиц** — pipeline.

**The equity filter stays an explicit choice.** Its contract says that import
completion never invokes it automatically. The retest plan dialog gets a
checkbox «Применить эквити фильтр после ретеста», default off; when checked,
the task adds the step **Эквити фильтр**
(`finalist-retest/equity-filter` with the bulk job id) after the replacement
and before the pipeline. Checking it is the explicit action.

### R3. Every other action ends with the registry up to date

This is already true for sync, history, READY and tester sampling. It becomes
true for the tick test and the retest (R1, R2). «Пересчитать ликвидность» does
not need the pipeline: the pipeline does not read liquidity.

### R4. Stop, failure, restart

- **«Стоп»:**
  - during «Тестер» it cancels the controller job cooperatively
    (`strategies.tester.cancel`; the tester's files are restored, as today);
  - between steps it ends the task before the next step;
  - it never stops in the middle of an import or replacement: the stop waits
    for that step to finish, and the task area says so.
- **A failed step** ends the task with the step, the code and the message;
  nothing after it runs.
- **The task area offers «Продолжить»** when the tester job is committed but a
  later step failed (e.g. verification). «Продолжить» continues from the
  failed step, never re-running the tester.
- **A Panel restart during a task:** after the restart the task area shows the
  last orchestrated task as interrupted, with its finished steps and «Продолжить»
  when a committed tester job is waiting for verification or import. The
  controller jobs persist already (`panel_jobs` journal); the orchestration
  keeps its own small state file next to them.
- **One task at a time,** as today: a running orchestrated task blocks the other
  main-screen actions, and a running chain task blocks the orchestrated ones.

### R5. The old cards move to «Настройки → Ручной режим»

The cards «3. Test and Import to Performance DB», «4. Bulk RETEST current
FINALIST» and «5. CHECK & RETEST» leave the «Пары и стратегии» screen. They
go to a new collapsed section «Ручной режим» at the end of «Настройки», with a
one-line note: «Для ручного разбора; обычная работа — кнопками на главном
экране».
- They keep their element ids and behaviour (JS finds them by id), so
  recovery of their own jobs keeps working.
- Only their place changes. No route, controller method or test of theirs is
  removed by this change.
- The main-screen status texts stop pointing to them («ход — в карточке
  «3. Test and Import»» goes).

### R6. Wording

The main screen speaks of steps a user understands: «Подготовка», «Тестер»,
«Проверка отчётов», «Запись в базу», «Замена результатов», «Эквити фильтр»,
«Обновление таблиц». Internal names (pipeline, inbox, `pair_tests`,
`COMMITTED`) are not shown, except in the task log.

## Non-goals

- No change to the tester run, the report collection rules, the Performance v2
  import rules, the replacement rules or the equity filter itself.
- **No change to the data model.** The PerformanceDB stays the store of
  results. The registry keeps its short copy (`pair_tests`), because the
  registry is synced between machines through git and the PerformanceDB is
  not.
- The «Объединять отчёты» collection mode of card 3 is not part of the main
  button. It stays available in «Ручной режим».
- Fees (see [fees per exchange](2026-10-20-fees-per-exchange.md)) are a separate
  plan.

## Design notes for the implementer (constraints, not a prescribed shape)

- **Where the orchestration lives.** The tester and import are controller jobs
  in the Panel process, so the orchestration must run in the Panel process
  too, not in a `panel_chain.py` child. Two shapes are acceptable:
  - extend `TaskRunner` with in-process tasks of named steps, sharing the
    status shape `{"task": {...}}` the task area already renders;
  - or a small orchestrator in `panel_main` that `TaskRunner.status()` /
    `/api/v2/main/task` report together.

  Either way, the existing task-area JS (progress bar, steps, errors, «Стоп»)
  renders it, with at most a «Продолжить» button added.
- **Reuse, do not copy.** Every step calls the existing controller methods
  (`store_ready_prepare`, `panel_job_submit`, `strategies_tester_verify_inbox`,
  `strategies_performance_v2_import` / `…_finalist_retest_import`,
  `…_equity_filter`), so their checks (protocol v2, exchange target, writer
  lock, inbox validation) stay in one place. The orchestrator waits on the
  controller job states the cards already poll (`/api/v2/jobs`,
  `…/tester/status`, `…/import/status`, `…/finalist-retest/status`).
- **The pipeline step** reuses the chain machinery (`_start_chain` with the
  existing `pipeline` argv) or calls it as a child process the same way. It
  must not hold a registry write connection inside the Panel process.
- **BingX:** a BingX tester job cannot be retried (`RETRY_BINGX_START_ANEW`).
  «Продолжить» after a failed «Тестер» therefore starts the tester anew. After
  a later step it continues without the tester.
- **The writer lock:** the import waits for or refuses a busy PerformanceDB
  writer as today (`PERFORMANCE_V2_BUSY`). The task shows it as a step error
  with «Продолжить».

## Acceptance evidence (to fill when implemented)

- **Tick test, fakes:** one main-screen action runs prepare → tester → verify
  → import → pipeline; one task status shows each step; after it the
  PerformanceDB has the results and `pair_tests` has the `mrs3_tick` rows of
  the exchange («Пары» count updated) without any other action.
- **Retest, fakes:** start → verify → replace → (equity filter only when
  checked) → pipeline.
- **Failure paths:** a rejected report, a failed verification, a busy writer
  and BingX `MISSING_DAYS` each end the task at their step with the reason;
  «Продолжить» resumes after a committed tester without re-running it.
- **Stop** during the tester cancels the controller job; stop during the
  import waits for it.
- **Restart:** the state file restores an interrupted task with «Продолжить».
- **UI:** cards 3/4/5 are inside «Настройки → Ручной режим», with ids
  unchanged and no longer on the main screen. The static UI tests are
  updated. No main-screen text points to the cards.
- **Regression:** the existing card flows still work from «Ручной режим»
  (their suites pass); the full related suites pass with the network blocked
  (`HTTP(S)_PROXY=http://127.0.0.1:9`).
- **Real run (when the tester is available):** one Bybit tick test of a few
  strategies end to end, with the task log and the «Пары» / «Стратегии» rows.
  Recorded in `progress.md`.
