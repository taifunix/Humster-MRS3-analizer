# Plan: Panel — one button, one finished result

Contract: [docs/specs/2026-10-20-panel-one-button-actions.md](../../specs/2026-10-20-panel-one-button-actions.md)
(requirements R1–R6, acceptance evidence). Read it first. Approved by the user
2026-10-20.

Rules for every task (CLAUDE.md):
- spec first: if a task changes the contract, update the spec in the same commit;
- TDD: a narrow failing test, then the code, then focused and related broader tests;
- tests only from `.venv` (`.venv\Scripts\python.exe -m pytest …`), with
  `--basetemp` in a new folder under `C:\Temp`, which is deleted afterwards;
- block the network in test runs: `HTTPS_PROXY=http://127.0.0.1:9` and
  `HTTP_PROXY=http://127.0.0.1:9`. No test may reach BingX, Bybit or the real
  tester;
- `git diff --check`; an independent review of the staged diff (a separate
  agent; send it a compact ASCII packet with requirements, paths and test
  results); fix the confirmed findings and re-review; then one scoped
  conventional commit per task;
- after each task, update `progress.md` (verified commands, evidence, next step).

Status: ☐ not started · ◐ in progress · ☑ done (with the review verdict).

## T1 ☐ Move cards 3, 4, 5 to «Настройки → Ручной режим» (R5)

- `src/mrs3/panel_web/index.html`: move `#tester-card`,
  `#performance-v2-finalist-retest-card` and `#performance-v2-retest-card` out
  of `#strategies-dd5` (after `#main-screen`), into a new collapsed
  `<details id="manual-mode">` («Ручной режим», with the one-line note) at the
  end of `#settings`.
- Keep every element id. Check in `app.js` that nothing depends on the cards
  being inside `#strategies-dd5` (screen switching, `querySelector` scopes,
  the recovery on load).
- `app.js`, `startRun`: the tick and retest status texts no longer point to the
  cards. Until T3/T4 they say «Тик-тест запущен …» / «Ретест запущен …»; the
  old cards can still be used from «Ручной режим».
- Tests: `tests/test_panel_static_ui.py`. It asserts the order
  `main-screen` < `tester-card` inside `strategies-dd5`; change it to the new
  place, and assert the cards are not in the main screen. Run the card suites
  (`test_panel_performance_v2*`, `test_panel_store_ready`, `test_panel_main`).
- Commit: `feat: Panel manual-mode cards move to «Настройки»`.

## T2 ☐ In-process orchestrated task in the main-screen task area (R4 base, R6)

- Choose the shape (spec «Design notes»): an in-process task with named steps,
  reported through the same `/api/v2/main/task` status the task area renders.
  - Every step is a callable.
  - A step that waits on a controller job polls its state; no busy loop: a
    sleep between polls, and a stop check.
- **State file** (`<Output>/panel-main-task.json` or next to the `panel_jobs`
  journal), written atomically:
  - kind, exchange, steps done, the job ids (tester, import), the current
    step, the error;
  - after a restart, the task area shows the interrupted task, with
    «Продолжить» when allowed.
- **One task at a time** across chain tasks and orchestrated tasks.
- **«Стоп»** (R4):
  - in a tester step → `strategies.tester.cancel`;
  - between steps → end;
  - in an import/replace step → wait for it, then end; the status says
    «остановится после записи».
- **«Продолжить»:** a new route `POST /api/v2/main/continue`. It resumes from
  the failed step. The task area JS gets the button, shown only when the
  status allows it.
- Tests with fake steps: progress and step names in the status; error ends the
  task; stop in each kind of step; continue after a failure; a restart restores
  the state; one-at-a-time with a chain task.
- Commit: `feat: Panel main-screen tasks with in-process steps, stop and continue`.

## T3 ☐ «Тик-тест MRS3» end to end (R1, R3)

- **`MainScreenService._run_tick` becomes an orchestrated task.** Steps:
  1. Подготовка (`store_ready_prepare`);
  2. Тестер (`panel_job_submit("strategies.tester.start", …)` and wait for
     `COMMITTED`; BingX data step as today);
  3. Проверка отчётов (`strategies_tester_verify_inbox(job_id)`);
  4. Запись в базу (`strategies_performance_v2_import({"tester_job_id": …})`,
     wait for its job; its rejections → task errors with name and code);
  5. Обновление таблиц (the existing `pipeline` argv for the exchange, as a
     child process).
- The plan dialog stays as it is (strategies, window).
- **Tests:**
  - a fake controller: the five steps in order, with one task status;
  - after the run, `pair_tests` has `mrs3_tick` rows of the exchange (a real
    registry, a fake PerformanceDB with results), so «Пары» shows the count;
  - failure paths: rejected report, failed verification,
    `PERFORMANCE_V2_BUSY`, BingX `MISSING_DAYS`;
  - continue after a committed tester does not resubmit the tester;
  - a BingX continue after a failed tester starts anew.
- Commit: `feat: Panel tick test runs tester, check, import and registry update as one task`.

## T4 ☐ «Ретест финалистов» end to end, with the explicit equity filter (R2, R3)

- The retest plan dialog gets the checkbox «Применить эквити фильтр после
  ретеста» (default off); `options.apply_equity_filter` (boolean, validated).
- **Steps:**
  1. Тестер (`strategies.performance.v2.finalist-retest.start` with the
     exchange as today; wait for `COMMITTED`);
  2. Проверка отчётов (`strategies_tester_verify_inbox` of the bulk job);
  3. Замена результатов (`…finalist_retest_import`, wait);
  4. Эквити фильтр (only when checked; `…finalist_retest_equity_filter
     {"job_id": bulk}`, wait);
  5. Обновление таблиц (pipeline).
- **Tests:**
  - the steps with and without the filter;
  - the filter is never run unchecked (the contract: never automatic);
  - a BingX retest keeps `REGISTRY_REQUIRED` / `COHORT_EXCHANGE_MISMATCH`
    as step errors;
  - continue after the replacement failed.
- Commit: `feat: Panel finalist retest runs as one task with an explicit equity filter step`.

## T5 ☐ Restart recovery and wording sweep (R4, R6)

- **A Panel restart during each step kind** (fake controller, persisted
  state):
  - the task shows as interrupted, with the right «Продолжить»;
  - a committed tester waiting for verification is resumed, not re-run.
- **Wording:** the main screen and the task area show only the step names of
  R6; internal names appear only in the task log.
  - grep `app.js` / `panel_main.py` for «Конвейер», «inbox», «COMMITTED» in
    user-facing texts.
- Commit: `fix: Panel main-screen task recovery and plain step names`.

## T6 ☐ Docs and the real run

- Spec: fill «Acceptance evidence» with the commands and counts.
- **`PRD.md`:**
  - add the spec and this plan to the Panel row and to the navigation row
    «Panel rework»;
  - update the Panel status.
- `README.md`: only if a public command changed (none expected).
- **Real run, when the user has the tester free:** one Bybit tick test of 2–3
  strategies end to end. Record the task log, «Пары» and «Стратегии» rows in
  `progress.md`. If the tester is not available, record «not run» and why.
- Commit: `docs: one-button Panel actions evidence`.
