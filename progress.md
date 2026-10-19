# MRS3 Current Status

Last updated: 2026-10-19

This file is a current-status snapshot, not a session log. Earlier progress notes remain in Git history; feature contracts and detailed evidence belong in the linked specs, reports, and plans.

Use [PRD.md](PRD.md) for the feature registry and [docs/README.md](docs/README.md)
for the documentation map. Read the linked contract only for the active task;
do not treat this file as a replacement for a feature specification.

## Panel stage 5a: the minute data layer (2026-10-19, branch `wip/period-csv-generator`)

Contract: [Panel for the simulation workflow](docs/specs/2026-10-18-panel-simulation-workflow.md)
(«Stage 5 plan», «Stage 5a contract» and «Stage 5a evidence»).

- `ensure_minute_days(con, settings, exchange, symbol, first, last)`:
  - Bybit uses the trade archive;
  - BingX uses klines, verified per day: `OK`, `VERIFIED_GAPS` by the hourly
    klines, or `SUSPECT` after three refetches.
  - Results are recorded in the local registry table `minute_days` and
    reused while the file hash is unchanged.
- **Verification:**
  - `tests/test_minute_days.py`: 19 passed;
  - registry, BingX downloader and Panel main-screen suites: 170 passed;
  - independent review `CODE_REVIEW_FAIL`: a MAJOR, an empty BingX answer
    removed a valid day file. Fixed: the file changes only when the day is
    accepted, and never for a worse or empty answer. The MINOR findings were
    fixed too (relative tolerance, malformed answer, workers from
    `duckdb_import.workers`).
  - Re-review `CODE_REVIEW_PASS`. Open:
    - MINOR: a `SUSPECT` record describes the last refetched candidate, not
      the kept file whose hash it stores;
    - NIT: a transient read error on a valid file plus an empty answer could
      remove the file.
  - live on BingX (temporary folders, removed): AMD four days `OK`, KOPN
    10-15 `VERIFIED_GAPS` `22:52-23:02`, as recorded in the spec.
- **Next:**
  - 5b: BingX history in the trade store and READY;
  - then 5c (tester sampling, tick tests) and 5d (liquidity, Panel actions).

## Pair registry stage 5: git sync between machines (2026-10-19, branch `wip/period-csv-generator`)

Contract: [pair registry](docs/specs/2026-10-18-pair-registry.md) («Stage 5
contract» and «Stage 5 evidence»), [ADR-0081](docs/decisions/0081-pair-registry-git-sync-format.md).

- **Synced tables:** decisions, per-pair settings, finalist marks, pair tests
  and pipeline. They go through a private git repository, with one file per
  table and machine.
- **When sync runs:**
  - export after every CLI or Panel write; the Panel commits and pushes in
    the background;
  - «Синхронизировать» pulls first and pushes last.
- **Sync repository:** the user asked for it, and it was created as a private
  GitHub repository. `sync-init --remote` was run on the first machine, and
  the first export was pushed.
- The sync repository uses the `gh` login as its credential helper. The
  system credential manager holds a different GitHub account and got
  «Repository not found».
- **Verification** (`.venv`, basetemp on drive C):
  - independent review `CODE_REVIEW_FAIL`, with 1 BLOCKER and 1 MAJOR, both
    fixed:
    - BLOCKER: a non-ASCII computer name stopped every registry command;
    - MAJOR: there was no lock between the Panel and the CLI, and file
      writes were not atomic;
    - the MINOR findings were fixed too.
  - Re-review `CODE_REVIEW_PASS`. Open NITs:
    - a sync file in another encoding (UnicodeDecodeError) is not turned
      into `SyncError`;
    - an `export-*.tmp` is left in `.git` when the rename fails.
  - Registry, Panel main-screen, «Стратегии», base-lot, optimizer and READY
    suites: 286 passed;
  - `panel_portfolio`, `v5_read_compat` and `portfolio_input`: 20 failed,
    the same known failures as HEAD (21 on HEAD, with the flaky stage 2
    test).
- **Next:**
  - on the second machine: `gh auth login`, then
    `python -m mrs3.pair_registry sync-init --remote <URL>` and
    «Синхронизировать»;
  - Panel stage 5 (BingX).

## Panel rework stage 4c: the A/B and Pareto screens removed (2026-10-19, branch `wip/period-csv-generator`)

Contract: [Panel for the simulation workflow](docs/specs/2026-10-18-panel-simulation-workflow.md)
(«Stage 4c contract and evidence»).

- Removed with their routes, controller code and tests:
  - the A/B card;
  - the Pareto/filters card with filters 6–27 and their settings;
  - the selection-XLSX user-fields import;
  - the PerformanceDB export card.
- PerformanceDB maintenance moved to «Настройки».
- The two retest cards stay, renumbered 4 and 5.
- **Verification:**
  - Panel tests plus `test_performance_v2_selection_review`: 1,126 passed,
    3 failed. All three already fail on HEAD (shell `previewtoken`, the
    journal retry constant, the flaky stage 2 test).
  - Independent review `CODE_REVIEW_PASS`; the MINOR findings were fixed and
    the re-review returned `CODE_REVIEW_PASS`. Four recalculate checks were
    ported to recalculate-all, and a 404 test covers every removed route.
  - PerformanceDB, single-mode, registry, «Стратегии» and main-screen suites:
    1,487 passed, 15 failed, all pre-existing:
    - `equity_regime_selection` fails the same way on HEAD;
    - `v5_read_compat`: 6, the same on HEAD;
    - 4 Pareto Panel tests in `selection_review` were removed with the
      feature, and the file then passes: 128.
  - `test_portfolio_input`: the 14 known failures.
- **Next:**
  - registry stage 5 (git sync);
  - Panel stage 5 (BingX);
  - the deferred items in the panel spec «Order of work».

## Panel rework stage 4b: retest and optimizer read the registry marks (2026-10-19, branch `wip/period-csv-generator`, committed `9c9c5d0`)

Contract: [Panel for the simulation workflow](docs/specs/2026-10-18-panel-simulation-workflow.md)
(«Stage 4 contract» 4b, «Stage 4b evidence»), [ADR-0080](docs/decisions/0080-finalist-marks-in-the-pair-registry.md).

- With a registry, the finalist retest (start, plan, control export) and the
  optimizer (finalist rows, pairs, provenance) read the registry marks,
  matched by strategy name. Without a registry they read the PerformanceDB
  reviews as before.
- While the registry decides, the old imports that set user decisions are
  refused (409 `MARKS_IN_REGISTRY`).
- **Verification:**
  - `tests/test_finalists_from_registry.py` (10).
  - Retest, optimizer-input, portfolio Panel, registry and «Стратегии»
    suites: 586 passed, 15 failed. Those 15 are the 14 `test_portfolio_input`
    failures that already fail on HEAD plus the flaky stage 2 test.
  - Live: no marks exist yet, so nothing changes.
  - Independent review: the first pass returned `CODE_REVIEW_FAIL` (a
    BLOCKER: the manifest froze without the decisions); fixed, and the
    re-review returned `CODE_REVIEW_PASS`.
- **Next:**
  - 4c, removal of the A/B card and the Pareto/filters screen, with their
    routes and the decision imports;
  - the non-blocking re-review notes:
    - a handler-level test of the 409 guard;
    - an `include_series=True` test with marks;
    - read the request body before the early 409;
    - merge two imports;
    - take the writer lock in `_registry_decides_for`.
  - Then registry stage 5 (git sync) and Panel stage 5 (BingX).
  - Deferred, not urgent (user, 2026-10-19): retire the base-lot Excel
    export; let `load_listing_dates` read the registry and drop the
    `pairs.xlsx` bridge. See the panel spec «Order of work».
- **Local setup done (2026-10-19, not in git):**
  - `panel_workflow.listing_dates_path` now points at the generated
    `Data/pair_registry/pairs.xlsx`: 274 pairs, equal to the old workbook
    for all 242 shared pairs.
  - The registry holds 14 days of liquidity windows
    (`liquidity --days 14`).
- **32 pairs UNAVAILABLE in liquidity:** these are the new listings
  (2026-09-18..10-13) that the old workbook lacked. No minute data has been
  downloaded for them yet. «Пересчитать историю» for them downloads the
  minutes; pairs with fewer than 7 weekdays since listing stay UNAVAILABLE
  until they have them.

## Panel rework stage 4a: the «Стратегии» view and finalist marks (2026-10-19, branch `wip/period-csv-generator`, committed `39c3c6d`)

Contract: [Panel for the simulation workflow](docs/specs/2026-10-18-panel-simulation-workflow.md)
(«Stage 4 contract», «Stage 4a evidence»), decision
[ADR-0080](docs/decisions/0080-finalist-marks-in-the-pair-registry.md).

- The «Стратегии» view shows the READY structures and the tick-tested
  strategies joined by name, with:
  - read-only metrics, including the report columns;
  - the hard cutoffs and top-5 toggles with «Причина», and «скрыть
    отсечённые»;
  - rank in pair;
  - «Посчитать метрики» (the existing cache preparation).
- Marks (FINALIST / RESERVE / REJECTED, rank) are set in the table or loaded
  from Excel. They are written only to the registry `finalists`.
- The Excel export writes nothing.
- The PerformanceDB reviews seed the marks once.
- **Verification:**
  - `tests/test_panel_strategies.py` (7) plus the main-screen, static UI and
    registry suites: 200 passed. The selection suite after the shared-rule
    extraction: 423 passed.
  - Live read of the real data: 1,812 rows in 6.6 s, a repeat read in 0.3 s.
  - Independent review: `CODE_REVIEW_PASS`; the MINOR findings are fixed, and
    the re-review returned `CODE_REVIEW_PASS`.
- **Next:**
  - 4b: the finalist retest and the optimizer read the registry marks;
  - 4c: remove the A/B card and the Pareto screen.
## Pair registry stage 4: the consumers read the registry (2026-10-19, branch `wip/period-csv-generator`, committed `d2f18cd`)

Contract: [pair registry](docs/specs/2026-10-18-pair-registry.md) («Stage 4
contract», «Stage 4 evidence»), decision
[ADR-0079](docs/decisions/0079-lot-model-v2-weekday-liquidity.md).

- **Optimizer:** sizes positions with LOT_MODEL_V2 from the registry whenever
  the registry exists. `registry_capacity` checks that the window ends on the
  last weekday before the Campaign anchor; no minute files are read. Rows
  carry `liquidity_lot_model`.
- **Base-lot export:** reads the registry by default
  (`--source minute-files` keeps V1).
- **History scripts:** take the pair list and listing days from the registry.
- **Loader-based consumers** (the PerformanceDB import and others): the
  registry writes a compatible listing workbook, `<registry root>/pairs.xlsx`,
  on every «Синхронизировать».
- **Verification:**
  - `tests/test_pair_registry_consumers.py` (16) plus the adapter, sizing,
    base-lot and registry suites (877 passed after the review fixes).
  - Independent review: the first pass returned `CODE_REVIEW_FAIL` (V1/V2
    mixing in the base-lot sheet); fixed, then re-reviewed: `CODE_REVIEW_PASS`.
  - Broad run: 2,289 passed. The 17 failures also fail or are flaky on HEAD.
  - Live: the generated workbook equals the current one for all 242 shared
    pairs; V2 capacity is available for 242 of 274 TRADING pairs.
- **Next:**
  - Panel stage 4: the «Стратегии» view; removal of A/B and the Pareto screen.
  - Registry stage 5: git sync.
  - Panel stage 5: BingX.
- **User actions:**
  - point `panel_workflow.listing_dates_path` at the generated `pairs.xlsx`
    to move the import to the registry;
  - before the next base-lot export (it now refuses the existing V1 sheet),
    run `python -m mrs3.pair_registry liquidity --days 14`, then
    `calculate_bybit_base_lots.cmd --rebuild-history`, or keep
    `--source minute-files`.

## Panel rework stage 3: the «Пары и стратегии» main screen (2026-10-19, branch `wip/period-csv-generator`, committed `61ee0df`)

Contract: [Panel for the simulation workflow](docs/specs/2026-10-18-panel-simulation-workflow.md)
(«Stage 3 contract», «Stage 3 evidence»).

- The top of the «Пары и стратегии» screen now has:
  - the exchange switch;
  - the actions «Синхронизировать», «Пересчитать историю», «Пересобрать
    READY», «Проверить тестером», «Тик-тест MRS3» and «Ретест финалистов»,
    with plan dialogs (row checkboxes, «Выделить все» / «Снять выделение»);
  - a task area with progress, elapsed time, ETA, errors, log and «Стоп»;
  - four views (Пары, Справочник, Ликвидность, Изменения), each with sort,
    filter, a column chooser saved on the server, and Excel;
  - a pair card where decisions and personal thresholds are entered.
- Tasks run in `panel_tasks.TaskRunner`, inside a Windows job object (fixes
  the orphan-pipe defect).
- READY takes the tester segment of the newest period automatically
  (`--latest-source auto`).
- The MRS2 STORE card, `/api/v2/store/*` and `panel_store` are removed.
- **Verification:**
  - `test_panel_tasks` (7), `test_panel_chain` (5), `test_panel_main` (18),
    `test_panel_static_ui` (136) and `test_ready_store` (9).
  - Related set: 300 passed. The one failure,
    `test_panel.py::test_static_panel_shell_contains_only_navigation_contract`,
    already fails on HEAD.
  - Live run on the real registry and store, with the plans and
    «Синхронизировать»: done in 69 s with no errors.
  - Independent review: the first pass returned `CODE_REVIEW_FAIL`. The
    findings were fixed (read-only catalog access, JSON for every route
    failure, chain stop and UTF-8, table/Excel parity), then re-reviewed: `CODE_REVIEW_PASS`.
- **Next:** registry stage 4 (switch the consumers to `read_liquidity` and the
  registry). Then panel stage 4 («Стратегии» view, A/B and the Pareto screen
  removed), registry stage 5 (git sync) and panel stage 5 (BingX).
- **Blockers / notes:** the active PerformanceDB is protocol v1, so the Panel
  refuses MRS3 tick tests (`DATABASE_PROTOCOL_V1`) until a v2 database is
  created and activated. BingX actions other than «Синхронизировать» stay
  disabled until stage 5.

## Pair registry stage 3: pipeline, tests, finalists, decisions, pair settings (2026-10-19, branch `wip/period-csv-generator`, committed `3c7c84e`)

Contract: [pair registry](docs/specs/2026-10-18-pair-registry.md) («Stage 3
contract», «Stage 3 evidence»).

- `records` holds the append-only decisions, per-pair READY thresholds
  (`ready_v4`), finalists (imported from the PerformanceDB reviews) and
  `pair_tests` (tester samples, MRS3 tick tests).
- `track` computes the walk-forward track record. `pipeline` holds the stage
  rule and the per-pair state.
- READY builds read the per-pair rules read-only.
- Live: 274 pairs, stages TICK 9 / READY 1 / HISTORY 220 / NEW 44; 1,805 tick
  tests imported; track records for 198 pairs.
- **Verification:** `tests/test_pair_registry_records.py` (21) plus the
  registry, READY and store-READY suites (93 passed). Independent review
  `CODE_REVIEW_PASS` after fixes.
- **Next:** panel stage 3 (main screen) together with registry stage 4
  (Panel screen, Excel, buttons, switch of the consumers).

## Pair registry stage 2: weekday liquidity, LOT_MODEL_V2 (2026-10-18, branch `wip/period-csv-generator`, committed `3e57aac`)

Contract: [pair registry](docs/specs/2026-10-18-pair-registry.md) («Stage 2
contract», «Stage 2 evidence»), decision
[ADR-0079](docs/decisions/0079-lot-model-v2-weekday-liquidity.md).

- `mrs3.pair_registry.liquidity` computes the window: V25 / A15 / L / base lot
  over the last N weekdays (default 7), identical to V1 on the same days. It
  stores the raw inputs and file hashes.
- The reader `read_liquidity` fails closed: a newer UNAVAILABLE window hides
  an older READY one. Runs are journaled.
- Live: 242 READY, 32 UNAVAILABLE (pairs without minute files).
- **Verification:** `tests/test_pair_registry_liquidity.py` (22) and
  `tests/test_pair_registry.py`. Independent review `CODE_REVIEW_PASS` after
  fixes.
- **Next:** registry stage 3 (pipeline, tests, finalists, decisions, pair
  settings). The consumers switch in registry stage 4.

## Panel rework stage 1b, part B and the three-screen shell (2026-10-18, branch `wip/period-csv-generator`, committed `4d087f1`)

Contract: [Panel for the simulation workflow](docs/specs/2026-10-18-panel-simulation-workflow.md)
(«Stage 1b evidence, part B»).

- Removed from the Panel:
  - the Source DB screen (local and remote);
  - the surfaces screen;
  - the old fresh analysis, shortlist and «Generate READY JSON».

  Their routes, controller code and the modules `panel_source_db`,
  `panel_source_jobs`, `panel_remote_source_db`, `panel_remote_testing` and
  `panel_surfaces` went with them, together with their tests.
- Navigation: «Пары и стратегии» (MRS2 STORE, tick test from a READY folder,
  PerformanceDB cards) / «Портфели» / «Настройки».
- **Verification:** panel suites pass apart from the two baseline failures and
  the flaky portfolio stage-2 test. The full suite is compared against the
  baseline. Removed routes answer 404.
- **Next:** registry stage 2 (weekday liquidity, `LOT_MODEL_V2`).

## Pair registry stage 1 (2026-10-18, branch `wip/period-csv-generator`, committed `d699dff`)

Contract: [pair registry](docs/specs/2026-10-18-pair-registry.md) («Stage 1
contract», «Stage 1 evidence»).

- `mrs3.pair_registry`: DuckDB `registry.duckdb` holding:
  - `pairs`, `pair_changes`, `risk_tiers` and `journal`;
  - the Bybit and BingX reference refresh;
  - history sufficiency;
  - the CLI `python -m mrs3.pair_registry refresh`.
- Live: Bybit 274 TradFi pairs with 8,240 risk tiers; BingX 605 TradFi
  contracts (369 trading, 236 suspended). A second run writes no change rows.
- **Verification:** `tests/test_pair_registry.py` (27). Independent review
  `CODE_REVIEW_PASS` after fixes.
- **Next:** registry stage 2, weekday liquidity `LOT_MODEL_V2`
  ([ADR-0079](docs/decisions/0079-lot-model-v2-weekday-liquidity.md)).

## Panel rework stage 1b, part A (2026-10-18, branch `wip/period-csv-generator`, committed `135b607`)

Contract: [Panel for the simulation workflow](docs/specs/2026-10-18-panel-simulation-workflow.md)
(«Stage 1b contract», «Stage 1b evidence, part A»).

- The «Test and Import» card runs the MRS3 tick test on a trade-store READY
  folder. `store_ready_tick` re-renders the folder for the active PerformanceDB
  and writes the generation manifest the tester expects. Its per-order zone
  facts come from `ready_store.zone_facts`.
- Real run: store READY → preparation → real SINGLE_MODE tick test (7/7) →
  import into a temporary protocol v2 PerformanceDB (7 strategies, 14 orders,
  14 plateaus). The tester files were byte-identical afterwards.
- Fixed: the tick-test service deleted non-JSON files from the tester strategy
  folder. It now clears only root `*.json` files.
- **Verification:** all `tests/test_panel*.py`, `tests/test_store_ready_tick.py`,
  `tests/test_ready_store.py`, `tests/test_single_mode_handoff.py`,
  `tests/runner`, `tests/test_trade_store.py` and `tests/test_tester_jobs.py`
  pass, apart from the two baseline failures and the flaky portfolio stage-2
  test.
- **Next:** stage 1b part B, which removes the old fresh analysis/shortlist,
  Source DB and surfaces screens; then the three-screen shell.
- **Blockers / user actions:**
  - The active PerformanceDB is protocol v1, so the Panel refuses MRS3 tick
    tests until a protocol v2 database is created and activated.
  - `tester_runner.strategy_dir` in the local config was corrected to
    `tester/settings_strategy`.

## Panel rework stage 1a (2026-10-18, branch `wip/period-csv-generator`, committed `914c28c`)

Contract: [Panel for the simulation workflow](docs/specs/2026-10-18-panel-simulation-workflow.md)
(«Stage 1 contract», «Stage 1a evidence»); companion draft
[pair registry](docs/specs/2026-10-18-pair-registry.md).

- **Removed and disconnected:** SCREENER 01, RUNNER 01/02 with their BingX
  mode (`/api/v2/testing/*`, `src/mrs3/screener/`,
  `scripts/build_bingx_screening_sheet.py`, `config_tester_*_screen.json`);
  the analysis profile (`/api/v2/settings/analysis-profile`,
  `analysis_profile.py`); the legacy single-page panel (`/legacy`,
  `PANEL_HTML`, «Default panel», every non-`v2` `/api/...` route) with the
  controller methods, constructor parameters and job state only they used.
- **Kept:** every `/api/v2/*` route, `/api/artifact` (Performance v2 failure
  report), `LocalTestingService` (portfolio stage 2), `panel_remote_testing`
  (remote Source DB until 1b).
- **Verification:** see the spec section «Stage 1a evidence». It covers the
  focused panel suites, the full suite against the 42-failure baseline and the
  independent review (`CODE_REVIEW_PASS` after fixes). Committed as
  `914c28c`.
- **Next:** stage 1b — the MRS3 tick test reads READY from the trade store;
  then remove the old fresh analysis/shortlist, Source DB and surfaces screens;
  three-screen shell.
- **Blockers:** none. Orphan processes from the stuck «Посчитать историю»
  task (PIDs 19204, 2532) still need to be ended by the user or a Panel
  restart; `gh auth login --web` is needed before the sync repository (registry
  stage 5).

## MRS2 trade store (2026-10-17, branch `wip/period-csv-generator`, committed)

Contract: [MRS2 trade store](docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md) (stages 1–4 with evidence), decisions
[ADR-0078](docs/decisions/0078-mrs2-trade-store.md) and
[ADR-0077](docs/decisions/0077-mrs2-base-limit-fill-plus-tick.md).

- **Stage 1** (reviewed, `CODE_REVIEW_PASS`): `trade_store.py`,
  `trade_history.py`, CLI `scripts/simulate_history.py`.
  - History is stored as P2-period segments plus the latest 20-day window.
  - Every segment is three immutable Parquet files and a DuckDB catalog.
  - Missing candle days block a segment (`MISSING_DAYS`).
  - Superseded plans are reactivated.
- **Stage 2** (reviewed, `CODE_REVIEW_PASS`): `trade_windows.py`,
  `ready_store.py`, CLI `scripts/ready_from_store.py`.
  - Window metrics from whole segments: a one-segment window equals the
    simulator; MaxDD and Ulcer within 1e-3.
  - READY v4 `ready-v4-store-1` (EQUAL only).
- **Stage 3** (validated on the real tester, reviewed, `CODE_REVIEW_PASS`):
  `tester_sampling.py`, `tester_jobs.py`, CLI `scripts/tester_jobs.py`.
  - XOM: 827 sampled points, 27 jobs, about 540 s.
  - The tester files were restored byte-identical.
  - Tester and simulator agree on 99.8 % of the points.
- **Stage 4** (reviewed, `CODE_REVIEW_PASS`): `panel_store.py`, Panel card MRS2 STORE
  (history, tester plan/run/import, READY; simulation/tester mode).
- **Verification:**
  `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_trade_store.py tests/test_trade_windows.py tests/test_ready_store.py tests/test_tester_jobs.py tests/test_panel_store.py tests/test_mrs2_simulator.py tests/test_period_csv.py tests/test_panel_static_ui.py`.
- **Full history run (2026-10-17):** all 242 registry pairs, 1,018 segments,
  0 failed, ~2h12m on 15 processes; store 4.6 GB. Pushed as `c9e86a9` on
  `wip/period-csv-generator`.
- **READY v4 threshold calibration (2026-10-17):** walk-forward study over the
  whole store (198 pairs, 544 cuts, 3,782 candidates) plus 6 real MRS3 tick
  tests — see [MRS2 trade store](docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md)
  («READY v4 threshold calibration»). DD bands (2/3.5/6 %) kept unchanged: a
  sharp forward-outcome cliff sits right at 6–8 %, so they transfer from the
  daily-scale study without change. `admit_score` raised 20 → 30 (loss rate
  26.8 % → 17.9 % at the same DD ceiling); no comparable cliff exists for
  score, so this is a judged, data-backed step, not an implied cutoff. The 6
  tick tests agreed with the simulator proxy on loss direction (6/6) and
  MaxDD% (within 0.4 pp).
- **Next:** rebuild READY from the store with the new threshold across the
  registry (the screener simulation mode is moot: the screener was removed in
  Panel rework stage 1a). Committed as `c9e86a9` and `7551d7a`.
- **Blockers:** none.

## Merge of origin/main into the BingX line (2026-10-16)

Local BingX commits (`6b723ab`..`7528b3f`) were merged with origin/main up to
`8f73d19`; backup branch `backup/bingx-before-merge-2026-10-16`. Conflicts in
`PRD.md`, `progress.md` and the Panel POST allow-list in `src/mrs3/panel.py`
were resolved by keeping both sides. Verification from `.venv`:
BingX/panel testing/base-lot/minute-capacity tests 205 passed; full suite
43 failed, 6752 passed, 12 skipped. Clean origin/main gives 44 failed with the
same set except three timing-dependent Panel tests that pass or fail between
reruns; no new failure comes from the merge.

Both follow-ups are closed below in «BingX parquet day files (2026-10-16)».

## BingX parquet day files (2026-10-16)

Part B of the
[remote handoff](docs/superpowers/plans/2026-10-16-remote-handoff-bingx-parquet-source-v6-csv.md),
contract in the [BingX data downloader](docs/specs/2026-10-10-bingx-data-downloader.md)
section «Формат дня (parquet тестера)».

- The Panel `bot_root` is now the new tester installation (parquet only); the
  former installation was renamed and keeps the original CSV. Its BingX archive
  (197 pairs, 22 455 days) and Bybit archive (217 pairs, 12 239 days) were
  copied and converted by the tester's own `hb_c.exe --convert-data`: 0 errors;
  every converted day equals its CSV row by row (BingX 30 401 707 rows, Bybit
  7 138 735 rows).
- The converter is the format authority: BingX `buy_volume`, `sell_volume`,
  `trades` stay `0` and zero-volume candles stay, exactly as in the former CSV
  (MEXC, also without buy/sell, is converted the same way).
- `src/mrs3/tester_candles.py` holds the one tester day format (REQUIRED, ZSTD,
  only `hb.schema = candles_1m/1`); `portfolio/minute_capacity.py` and the
  BingX downloader use it. The downloader writes and validates only
  `_1m.parquet` (temporary file → read-back check → `os.link`, `os.replace`
  only for a damaged day or overwrite mode).
- Evidence: the downloader's encoding of every archived BingX CSV day equals the
  tester-converted file in schema and values (22 455 of 22 455), and the
  downloader's validator accepts all 22 455 tester files.
- RUNNER 01 BingX fees are 0.00012/0.0003 (40% discount), as in
  `templates/exchanges/BingX/mrs3-tester.json` (user decision 2026-10-16).
- Verification: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_bingx_downloader.py tests/test_panel_bingx_mode.py tests/test_tester_candles.py tests/test_portfolio_minute_capacity.py tests/test_calculate_bybit_base_lots.py tests/test_panel_testing.py tests/test_build_bingx_screening_sheet.py tests/screener`.
- Tester run on parquet (B4, 2026-10-16, current tester, `hb_c.exe --run-tester`
  with the Panel config/strategy restored byte-identical afterwards): BingX
  TSLL 2026-10-02..09 loaded 9 parquet days (11 521 bars); 0.7% shift gave 19
  trades per point at 5m and 15m, identical with `LimitOrderVolumeCheck` on and
  off (BingX zero buy/sell volume does not block fills).
- The RUNNER 01 / SCREENER 01 MRS2 templates were still in the previous tester
  schema; rendered as is, the current tester writes HTML the project parser
  rejects (`metrics contain duplicate or empty keys`). They now follow
  [MRS2 tester configs in the current format](docs/specs/2026-10-16-mrs2-tester-config-current-format.md)
  (ADR-0072 core report profile + `reports_history` with `UlcerIndex`; BingX
  render drops the Bybit-only `maintenance_margin_rate`). Live one-point
  RUNNER 01 renders: BingX TSLL 23 trades, Bybit CLSK 34 trades; both reports
  parse (legacy and 2026-10 parser modes), normalise to Source v6 fragments,
  and the screener reads `reports_history*.csv` with `UlcerIndex`.
- Verification: the command above plus `tests/test_panel.py tests/test_panel_static_ui.py`
  — 629 passed, 1 failed (`test_static_panel_shell_contains_only_navigation_contract`,
  the recorded pre-existing `previewToken` failure).

## BingX data downloader (2026-10-10)

Draft contract: [BingX data downloader](docs/specs/2026-10-10-bingx-data-downloader.md).
BingX API facts in the spec were verified with live public requests on
2026-10-10.

Stage 1 is implemented: `src/mrs3/bingx_downloader.py`, manual launch
`scripts\run_bingx_download.bat` (`scripts/download_bingx_data.py`, block
`bingx` in `config.local.json`). Verification:
`.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_bingx_downloader.py`
— 17 passed. A live run into a scratch `bot_root` downloaded PANW/BABA/TSLL
for 2026-08-03..05 (BABA listing day 870 rows, pre-listing day skipped) and a
repeat run skipped all 8 valid days.

Stage 2 is implemented: «BingX» checkbox on SCREENER 01 and RUNNER 01;
«Подготовить файлы» first tops up data (`StartDate − 1` … `EndDate`) through
`/api/v2/testing/bingx/download` + `/status`, then fills the tester with API
symbols, BingX fees, `UpdateData: false` and a `BingX`/`bingx` strategy. The
screener takes BingX listing dates from `launchTime` and does not write BingX
verdicts to the Bybit registry. Verification:
`.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_panel_bingx_mode.py tests/test_bingx_downloader.py tests/test_panel_testing.py tests/test_panel_static_ui.py tests/screener tests/test_panel.py`
— all BingX tests pass; 3 failures are pre-existing and reproduce without
these changes (`test_panel_static_ui.py`: two `WinError 206` node command-line
length failures; `test_panel.py::test_static_panel_shell_contains_only_navigation_contract`:
existing maintenance `previewToken` identifiers). `node --check` passed.

Probe run accepted by the user on 2026-10-11: a SCREENER 01 BingX run on
TSLL (304 runs) used `NCSKTSLL2USD-USDT`, BingX fees and data from
`tester\data\bingx\` (trades on 2026-10-07/08, after the last Bybit TSLL day).

Ticker mapping now uses BingX TradFi contracts only (212 of 242 registry pairs
resolve, 30 absent). The registry sheet «Скрининг BingX» was created with
`scripts/build_bingx_screening_sheet.py` (212 pairs, backup kept next to the
registry). Automatic BingX verdict writes to Excel are postponed.

The Panel checkbox is split into «Режим BingX» and «Докачать данные BingX»
(inactive while BingX mode is off). BingX mode without top-up prepares files
on existing local data and refuses with the missing days. Verification:
`.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_panel_bingx_mode.py tests/test_bingx_downloader.py tests/test_build_bingx_screening_sheet.py tests/test_panel_testing.py tests/screener`
plus `node --check src/mrs3/panel_web/app.js`. The Panel needs a restart to
show the new checkboxes.

Day validation now accepts minutes without BingX candles (no-trade minutes
are omitted by BingX on illiquid pairs; gaps are kept as in Bybit files, no
rows are invented). A live top-up of ALAB/CSCO/TTWO/BSP for 2026-07-03..06
saved all 16 days (e.g. CSCO 2026-07-04: 1171 rows).

Some BingX stock contracts trade only in US market hours (XLP, BAC, SKUU,
SPCH: 391 candles on weekdays, 0 on weekends). A day with no candles now
creates no file and is not an error; it is re-checked at BingX on every
top-up and before fill, never cached. Live check for 2026-07-24..27: 8 days
saved, 8 no-trade days, fill check reports nothing missing.

The Panel moved to the newer tester installation on 2026-10-16 (see above).
Probe run 2026-10-11 confirmed the tester accepts missing day files: a
SCREENER 01 BingX run on XLP (2026-07-22..10-10, 70 day files, 12 weekend days
without files) produced 304 BingX reports with trades in every month and no
tester log errors.

## Local tester v2 strategy directory (2026-10-14)

The local tester now stages Single Mode strategies in `tester/settings_strategy`
and retains the exact legacy root-level directory as a supported config value.
Focused verification of runner config, config defaults, report cleanup, and
strategy staging passed: 10 tests passed, 183 deselected. An earlier broad
Panel sweep was stopped before completion; the later report-profile suite
covering the related Panel/protocol modules passed 594 tests with one Windows
symlink-capability skip.

The live Single Mode run used a 20-strategy manifest for 2026-09-13 through
2026-10-13. Panel recorded `COMMITTED`, 20/20 verified reports, zero failed
strategies, and no job error; the report collection is `VERIFIED` with 20
reports. The read-only Performance v2 parser accepted all 20 as format
`2026-10` / tester `2026/10/10`, with settings, Metric/Value tables, action
tables, wallet series, and equity series. Across the collection there are 268
action rows, 25,999 wallet samples, 25,999 equity samples, 2,000 summary metric
cells, and 792 non-empty scalar analytics values. The separate analytics output, monthly and
regime rows are absent; HTML scans found zero OHLC chart, position chart,
position-statistics, or timing-section markers, and zero Monte Carlo mentions.
No PerformanceDB import was launched. This is not a DD5 retest or a verdict on
strategy quality. Next:
decide whether to import the verified collection or run a separate DD5 retest.
Blockers: none for the completed report-profile check.

## Next: PerformanceDB upgrade for the new tester report (2026-10-12)

Approved contract: [PerformanceDB new report format](docs/specs/2026-10-12-performance-db-new-report-format.md),
implemented together with P2–P6 of the
[MRS3 evaluation protocol v2](docs/specs/2026-10-12-mrs3-evaluation-protocol-v2.md)
in this session (user decision). Order: N1 parse → P2+N2 schema v12 → P3+N3
import → N4 maintenance/compaction → P4–P6 → P7 Panel database selection.
N1 is done: `parse_current_performance_v2_html` reads the 2026-10 format
(version-only detection, compatible duplicate metrics, 59 analytics labels,
regimes and monthly returns); the real sample parses fully; legacy parsing is
unchanged (reviewed against HEAD on all fixtures and real 2026/09/22 reports).
P2a is committed (schema v12): additive migration v11 → v12
(MA type/source columns, three sizing columns, `tester_version`,
`report_format`, tables `strategy_report_metrics`, `strategy_report_regimes`,
`strategy_report_monthly`), optional `schema_info` markers
`evaluation_protocol`/`exchange` written at creation from
`unified_performance_v2.evaluation_protocol`/`exchange`. The Panel schema
gate (`_ensure_performance_v2_schema`, keyed also by the configured protocol
and exchange) and the import runner refuse a mismatching database before any
migration (`EVALUATION_PROTOCOL_MISMATCH`, `EXCHANGE_MISMATCH`); read-only
routes that bypass the gate do not check identity yet (P7 adds database
selection). Readers accept v5–v12; Panel migrates a v9–v11 database to v12 on
open. Maintenance, compaction and prune cover the new tables and appended
columns; import REPLACE clears the report rows. Evidence: a copy of the live
v11 database (16.9 GB, 49 125 strategies, 122 M equity rows) migrated in 45 s
with identical row counts and empty new columns/tables. Broad suite: the only
failures besides the 57 that already fail on clean HEAD are the two
template-pinned finalist-retest tests.
P2b is committed: the import records MA type/source of the
close MA and every order (ADD writes them, REPLACE fills empty ones) and the
v12 sizing facts (`my_fix_balance`, SHORT percentage/risk); a report whose
known MA facts differ from the stored strategy or from its same-batch
representative is rejected alone as `REJECTED:MA_SETTINGS_CHANGED` on the
REPLACE, non-covering SKIPPED and tombstone paths.
P2c: Stage 2 builds each strategy with the stored MA type/source of every
order and of the close MA (protocol v2 §4.5); a member without facts keeps the
`portfolio-weighted-mrs` prototype and its JSON is unchanged. Finalist source
documents carry the facts only when set. The Stage 2 variant records
`legacy_ma_prototype_members` (members with any missing fact) and the Panel
summary shows it as `Legacy MA prototype members` (`UNKNOWN` for older results).
P2d: selection and PerformanceDB workbooks add the display-only `MA type`
column after `MA` (amendment in the
[XLSX column contract](docs/specs/2026-10-07-performance-v2-xlsx-column-contract.md));
legacy rows and pre-v12 databases give blank cells; aggregate workbooks
exported without it stay importable. The Panel strategy card shows the MA
type/source next to Close MA and each Open MA when stored.
P3+N3: the parser accepts `use_upnl` true (protocol v1) or false (v2); the
import rejects a report alone when it is an incomplete v2
(`PROTOCOL_V2_INCOMPLETE`, names the unmet fields), when its protocol differs
from the database marker (`EVALUATION_PROTOCOL_MISMATCH`, an unmarked database
is v1) or when the database has an exchange marker and `exchange.name` differs
(`EXCHANGE_MISMATCH`). Every result records `tester_version`/`report_format`;
2026-10 reports take `max_drawdown` from `Max Drawdown, USDT` and `total_trades`
from `Fills (reduce / close)` and store the analytics tables in the same
transaction; a report whose core the import recomputes (warm-up trim) stores no
analytics. Consequence until P6: reports rendered from the user's edited
`retest-mrs3` template (`use_upnl=false`) are rejected by a v1 database as
`EVALUATION_PROTOCOL_MISMATCH`; they need a v2 database.
P4 (FIXED metrics): `performance_v2_sizing.fixed_lot_base` recognises a stored
protocol-v2 fixed lot (use_upnl=false, frozen balance, use_fix, max_balance=0,
active side 100%/risk 1) and gives B = my_fix_balance × percentage / 100. For
such results window metrics use R = ΔW / B (growth exp(R), daily log R/days,
fees / B, linear drawdown (peak − E) / B), equity quality and regime use
ln Ẽ = (E − E0) / B with linear drawdowns and peak gap, and selection rebases
the report `max_drawdown_pct`, `total_pnl_pct` and `pnl_without_best_trade_pct`
on B (hence risk_scale, DD5, CE); EQUAL/INCOME compare only within one lot mode
and base. Workbooks add `Lot mode` after `MA type`; the Panel card shows
`Lot FIXED B=…`. DYNAMIC and legacy results are unchanged. Invariant 2 is
pinned on a synthetic fixture. The E1–E6 evidence scripts live in
`scripts/equity_research/` (`sizing_evidence.py`, `sizing_fills.py`,
`base_size.py`); a smoke test runs them read-only on a small synthetic database.
P5: the optimizer accepts FIXED results (use_fix=true and use_upnl=false with a
stored base, active side 100%/risk 1, max_balance 0; the inactive side may be
NULL) next to DYNAMIC; FIXED cycles are normalised as (closing − opening
balance) / B, and tested capacity in sizing and the default pre-test evaluator
uses B (finalist rows of FIXED results carry `fixed_lot_base`). DYNAMIC source
documents, digests, prepared JSON and availability reasons are unchanged for
rows without schema-v12 facts (checked on a 1 458-combination grid). DYNAMIC
rows imported after P3 that store v12 facts get a new source digest and are
prepared again on the next preparation.
P6a/b: `src/mrs3/evaluation_protocol.py` gives the target of the active
database (base B = `InitialBalance` of the MRS3 tester template, exchange from
`unified_performance_v2`; a v1 database is refused as `DATABASE_PROTOCOL_V1`),
applies `my_fix_balance = B` and `exchange.name` and checks the §4.2 predicate
(`PROTOCOL_V2_RENDER_REJECTED` names the unmet fields). The READY JSON
generators (fresh, legacy, v6), RUNS (strategy and embedded tester config),
RETEST and finalist RETEST accept the target (the finalist config digest
includes the base and exchange). P6c1: the Panel takes the target before every
MRS3 flow (READY JSON fresh/legacy/v6, RUNS publication and start, single-mode
tester start, RETEST, finalist RETEST, legacy `tester-run`), checks the tester
template and every manifest strategy or RUNS snapshot before launch, answers
refusals with HTTP 409 and their code (`DATABASE_PROTOCOL_V1`,
`DATABASE_UNAVAILABLE`, `EVALUATION_PROTOCOL_MISMATCH`, `EXCHANGE_MISMATCH`,
`PROTOCOL_V2_RENDER_REJECTED`, `PROTOCOL_V2_TESTER_CONFIG_REJECTED`), and no
longer takes a start balance (API and UI). New MRS3 jobs record
`evaluation_protocol`/`evaluation_base`/`evaluation_exchange`; a retry or a RETEST
inbox reuse of a job without the same marker is refused
(`RETRY_PRE_PROTOCOL_V2`) or skipped. The protocol v2 templates are committed.
MRS3 tests need a protocol v2 PerformanceDB: set
`unified_performance_v2.database_root` to a new folder, `evaluation_protocol: v2`
and `exchange` in `config.performance.json` (P7 adds this to the Panel
settings). P6c3: RETEST and finalist RETEST repeat the stored MA type/source
of every order and of the close MA (a strategy without stored facts keeps the
template prototype), so a template change does not turn retest reports into
`MA_SETTINGS_CHANGED`. P6 is complete.
P7 ([spec](docs/specs/2026-10-13-panel-performance-db-selection.md), approved
2026-10-13): P7a spec. P7b: `config.local.json` `performance_databases.active`
overrides root/protocol/exchange in `load_performance_v2_config`; job markers
add `evaluation_database`/`evaluation_profile`; the finalist digest adds the
exchange templates; imports of jobs rendered for another target are refused
(`IMPORT_TARGET_CHANGED`); listing dates read the exchange's sheet. (The P7b
`exchange_profiles` overrides and `results/.tester-templates` were replaced by
P7e template folders.) P7c: Settings → «PerformanceDB и биржа»
lists the default and registered databases (folder, protocol, exchange, file
markers, size), switches the active one (`performance_databases.active`, refused
with `RESOURCE_BUSY` while a job runs or a report collection is open or
verified) and creates an empty protocol v2 database with exchange markers in a
new folder under `data/` (`/api/v2/settings/performance-databases`,
`…/activate`, `…/create`). P7d: portfolio Stage 1 (readiness blocker,
finalist preparation, campaign submit) refuses a PerformanceDB marked with an
exchange other than Bybit (`EXCHANGE_DATA_UNAVAILABLE`, HTTP 422); unmarked v1
and Bybit databases work as before; Stage 2 reuses the checked campaign. P7e
(user decision 2026-10-13): exchange templates replace the `config.local.json`
profiles. `templates/exchanges/<exchange>/` holds `exchange.json` (listing-date
sheet), `mrs3-strategy.json`, `mrs3-tester.json` and the optional optimizer
`portfolio-*.json`; the former MRS3 and optimizer templates moved byte-for-byte
to `templates/exchanges/Bybit/`, `BingX/` has the BingX account and fees. The
active database's exchange picks the folder for READY, RETEST and finalist
RETEST; launch checks compare `exchange.account` and the fee/funding fields
(`account_type` is not checked). A new exchange is a new folder. P7 is
complete.
Plan (deferred, user decision 2026-10-13): optimizer adaptation for BingX
(margin parameters from the exchange API, minute liquidity in the `BingX`
folder next to the Bybit one, `portfolio-*.json` for BingX). Next work: the
[CSV Direct integration and shift grid 7.5% plan](docs/superpowers/plans/2026-10-13-csv-direct-integration-and-shift-grid-750.md)
— part A merges `feat/csv-direct-analysis` into main (ADR renumber to 0071,
exchange key in the analysis DB, background job); part B extends the MRS2 shift
grid 5.5% → 7.5% (step 0.4%) with backward compatibility (effective grid =
legacy grid plus the longest present extension prefix); seven user decisions
listed in B3.
Before part B changes the plateau logic (2026-10-14): a walk-forward study on
the full Source v6 databases (remote machine, no surfaces or analysis DBs
needed) collects facts on whether plateau-based selection predicts future
PnL and drawdown better than alternatives (neighbourhood robustness, plateau
centre, shift depth). The remote session delivers facts and a parquet export
only; reconciliation with the local analysis DBs and final conclusions are
done locally. Prompt:
[plateau OOS study](docs/superpowers/plans/2026-10-14-plateau-oos-study-prompt.md).
The remote study delivered facts and an export (2026-10-15):
[remote facts](docs/reports/2026-10-14-plateau-oos-facts.md). A local study on
that export ([stable zones](docs/reports/2026-10-15-plateau-zones-local-study.md))
found that the current plateau adds nothing to the economic filter, while the
neighbourhood median of PnL30/Ulcer predicts forward quality and risk.
Validation (2026-10-15..16,
[plateau v4 validation plan](docs/superpowers/plans/2026-10-15-plateau-v4-validation-plan.md),
scripts `scripts/plateau_research/`): real MRS3 tick tests of 1 419 strategies
28 days forward and 958 runs on their training windows. Admitting a pair/TF
only when its best 2-order v4 structure scores > 20 with training DD <= 6%
gave about 10-11%/month at a fixed lot with 1-3% losing strategies; scores
<= 10 gave about 3%/month with 20% losing. Not better than the plain
structure: first order 0.5-0.6% on liquid pairs, 3-order/deep/wide order sets,
diverse twins, INCOME lots ([ADR-0076](docs/decisions/0076-mrs3-equal-lots-only.md)),
the A/B filter. Within a pair the forward return is not predictable, the risk
is. Draft rules in the plan: READY v4, risk categories (training DD <= 2 /
2-3.5 / 3.5-6%), ranking and the optimizer finalist per category, removal of
Pareto/legacy ranks, 20-day periodic MRS2 CSV runs merged per period in CSV
Direct with a data repository journal. Separate 21-day periods scored as the
mean of per-period scores were not worse than one window (section A12a).
Test sets and results stay local, not committed. Remote handoff prompt:
[remote handoff](docs/superpowers/plans/2026-10-16-remote-handoff-bingx-parquet-source-v6-csv.md);
the CSV Direct prototype stays on branch `feat/csv-direct-analysis` (pushed,
not merged: 47 commits behind main, its ADR-0070 number is taken in main).
Next: remote handoff (merge
the BingX candle work, tester-format parquet packing, Source v6 -> period CSV
generator with a check against real tester CSVs), threshold calibration, then
the implementation specification. Out-of-time recheck around 2026-11-12.
User decision 2026-10-12: one PerformanceDB per exchange with an
`schema_info.exchange` marker (protocol v2 §4.1); the CSV Direct analysis DB
(branch `feat/csv-direct-analysis`) must also record the exchange from the run
snapshot `exchange.name` — pending, separate commit on that branch. The current parser rejects the new HTML
(`metrics contain duplicate or empty keys`). The old database must keep
working after the additive v11 → v12 migration; missing analytics is not an
error. The protocol v2 templates are committed with P6c1 (below).

## Portfolio input snapshot registry (fix 2026-10-13)

Since `eae54c0` (2026-10-01) every portfolio input snapshot failed with
`UNKNOWN_DECIMAL_FIELD`: 33 new Decimal `SelectionConfig` settings and the
candidate facts `history_days`, `completed_cycle_net_pnl`, `top5_pnl`,
`pnl_after_top5`, `fees_share_pct`, `fees_per_30d_pct` had no snapshot type.
All are registered explicitly (a test fails when a new Decimal setting or
candidate fact has no type), as are the stage settings `min_shift_pct` and
`pnl_tolerance_pct`. The snapshot now also recomputes `fees_share_pct` and
`fees_per_30d_pct` from the frozen series, so its digest no longer depends on
the window cache. 14 tests in `test_portfolio_input.py` still fail on stale
fixtures (`selection_results` 12 columns, `selection_review_imports` 6 columns,
schema version `'12' == '8'`), unrelated to this fix.

## Reason of REJECTED rows in selection workbooks (2026-10-12)

In the pair selection and all-pairs workbooks, a row shown as `REJECTED` now
names the filter or review that set `REJECTED` (for example
`AB_DETERIORATION;REJECTED`) instead of the first stage that fired in the
current run. Display only; persisted selection results are unchanged. Finalist
control/retest workbooks are excluded because their `Причина` is a protected
import field. Known limit: review imports and finalist retest rewrite
`REJECTED` tags as `SELECTION_REVIEW`. Contract: the amendment in the
[XLSX column contract](docs/specs/2026-10-07-performance-v2-xlsx-column-contract.md).
Restart Panel to use it.

## MRS2 point settings: out-of-sample check (2026-10-12)

A read-only study compared point facts in an earlier analysis with the same
point's B tail in a later analysis of the same pair (14 analysis pairs, 20
symbol links). Within pairs only the current DD ≤ 12 cap predicts later
results; PnL > 5, PnL/DD ≥ 3 and WR ≥ 68 do not, and tighter PF, WR, PnL/DD or
plateau size rules do not separate points either. A DD cap of 10% is not
supported by the data (no direction at 10–11%, a weak unproven lean at 8–9%);
it is only an optional risk policy in the user's Panel settings. The current
PRETEST A/B rule removes points that later lose less often (14.2% vs 23.9%),
so CSV Direct needs no A/B replacement on this evidence. A first attempt via PerformanceDB is withdrawn:
its MRS3 use source `open`, the MRS2 points `ohlc4`. Gap rules, READY and
plateau settings need the protocol-v2 (`ohlc4`) MRS3 database. Report and
deferred list: [MRS2 point settings OOS](docs/reports/2026-10-12-mrs2-point-settings-oos.md);
scripts: `scripts/mrs2_research/`.

## Fee efficiency columns in the selection XLSX (2026-10-12)

The selection workbooks now show `Fees share, %` (fees as a share of the gross
pre-fee return) and `Fees/30, %` right after `Top 5 share, %`. Both come from the
cached full-report window facts and are display-only. Contract: the fee
amendment in the
[XLSX column contract](docs/specs/2026-10-07-performance-v2-xlsx-column-contract.md).
Aggregate workbooks exported before these columns remain importable (the
previous header tuple is accepted with its own hash). Verification: selection,
aggregate workbook, publication, export and review suites, 481 passed.

## Equity evaluation research (2026-10-12)

A read-only walk-forward study compared equity facts with what each strategy
did next, within its pair, on three non-overlapping periods (cuts T−14, T−28
and T−42).

- **Return within a pair is not predicted** by equity speed, STRONG/WEAK,
  smoothness, B, DD5 or the R7.3 score.
- **Risk is predicted:** most consistently by depth below the high-water mark
  and relative smoothness within the pair; less consistently by the latest
  weekly loss and DECLINE (DECLINE measured only on the T−14/T−7 cuts).
- **Filter 7 shows no measurable effect** (approximate replay).
- **Self-computed risk analytics** (`rolling_risk.py`, no look-ahead, also
  measured on the new drawdown after the cut): the mean daily worst floating
  loss is the strongest predictor of the new drawdown after the cut on all three
  cuts, ahead of CVaR, Ulcer and smoothness; against the all-time high-water
  mark it is comparable to Ulcer (report §2a). User decision 2026-10-12: keep
  both in-pair risk flags (smoothness `ROUGH_IN_PAIR` and MAE-like
  `HIGH_FLOAT_IN_PAIR`) and choose after the protocol-v2 recheck. Depth below
  the high-water mark mostly describes the current state:
  on the clean outcome it predicts future drawdown only weakly. Preliminary,
  pending the protocol-v2 recheck.
- **Caveat: survivorship bias.** The sample contains only strategies still in
  the database. Rejection filters partly looked at the outcome windows (A/B
  deterioration uses the last 14 days), so the conclusions are preliminary
  until the protocol-v2 database is analysed before rejection filters run.

Evidence:
[walk-forward report](docs/reports/2026-10-12-equity-evaluation-walk-forward.md).
Proposed contract; the user decided D1 (pause is a finalist) and D15 (unheld breakout ≤5% is a pause) on 2026-10-12, other items are still open:
[equity risk evaluation](docs/specs/2026-10-12-equity-risk-evaluation.md).
The recommended filter set keeps filters 2–6 and 8–10 and turns off filter 7.

The research scripts are in `scripts/equity_research/`; the report lists each
script, its input and how to run it. Metrics of the new tester report worth
recording for the next analysis:
[tester metrics to record](docs/reports/2026-10-12-tester-report-metrics-to-record.md).

Next step: repeat the rolling-origin study on the protocol-v2
database once longer tests exist.

## PerformanceDB compaction and faster verification (2026-10-12)

The live PerformanceDB was compacted from 31.14 GiB to 15.76 GiB and is now
schema v11. Cause of the old size: DuckDB never shrinks the file on DELETE.
About half of the physical rows in `strategy_equity` (238 M vs 122 M live) and
`strategy_actions` were deleted tuples, plus 8.4 GiB of free blocks. Rejected
retirement itself had removed all heavy facts correctly.

The candidate was built with `scripts/compact_performance_v2.py` and verified
with `scripts/verify_performance_v2_candidate.py`. All 20 tables matched
row-for-row, and the source SHA-256 was unchanged before and after. It was
swapped in on D:; the old file is kept as
`data/performance-v2/strategy_performance.pre-compact-20261012.duckdb` until
the user confirms the Panel works.

The verifier compared large tables in 128-result-id ranges, so each range
rescanned the unclustered source: about 90 s per range and 384 ranges for
`strategy_equity`, roughly 9 h. Large tables are now copied and verified in
row-sized ranges of about 10 M rows. The exact `EXCEPT ALL` comparison in both
directions is unchanged. Contract: the 2026-10-12 amendment in
[lossless compaction](docs/specs/2026-09-30-performance-db-lossless-compaction.md).

On the real DB, verification took 3 h 47 min in total. That includes source and
candidate hashing, `strategy_actions` (2 ranges), `strategy_equity` (13 ranges,
about 1 h 45 min) and about 2 h for the single-threaded typed comparison of
`window_metrics`, `selection_results` and `optimizer_prepared_inputs`. Copying
the candidate to D: and hash-checking it took 132 s.

Verification: `tests/test_performance_v2_compact.py` 37 passed. Independent
review passed after one fix round.

Next step: speed up the typed comparison of medium tables and the prepared
payloads (SQL `EXCEPT ALL` where types allow, parallel payload decoding).
Delete the pre-compact backup only after user confirmation.

## MRS3 evaluation protocol v2 (2026-10-12)

Approved; P1 implemented, P2–P6 pending. Evaluation tests of MRS3 move to a
fixed lot of 10 000 USDT with `LimitOrderVolumeCheck=false` and entry MA
source `ohlc4`, and their results go to a **new** PerformanceDB. The current
database stays the protocol-v1 database and keeps working without data
changes; mixing protocols in one database is refused. Stage 2 of the
optimizer keeps its own tester template with %-of-balance sizing and the
volume check. Contract:
[protocol v2 spec](docs/specs/2026-10-12-mrs3-evaluation-protocol-v2.md);
decision: [ADR-0070](docs/decisions/0070-mrs3-evaluation-protocol-v2.md).

Found while preparing it: in both MRS3 strategy templates the first LONG
entry prototype is `SMA/open` (SHORT is `SMA/ohlc4`), and the generator copies
it to every level, while the MRS2 plateau points are tested with `ohlc4`. All
2 361 JSON files of the last finalist RETEST are LONG with `SMA/open` entries.
PerformanceDB does not store MA type or source; P2 adds them (additive schema
v12).

Read-only evidence (2026-10-12, 30 370 current results): compounding alone
barely changes the top-5 per pair (identical in 92–96% of pools); fixed-lot
curves measured by the current window formulas would match only 41%, so P4
normalizes fixed-lot metrics. A 10 000 USDT base keeps the qty-step error of
the smallest order level under 1% for 80 of 81 pairs. Full evidence:
[discussion A3/B2](docs/superpowers/plans/2026-10-12-universal-equity-evaluation-discussion.md).

P1: optimizer Stage 2 reads `templates/tester/portfolio-stage2/config_tester.json`
(a byte copy of the old MRS3 template); its rendered config is pinned by
SHA-256 in tests. Verification: `.venv\Scripts\python.exe -m pytest
-p no:cacheprovider -q tests/test_panel_portfolio.py tests/test_panel_testing.py
tests/test_panel_fast_strategy_test.py tests/test_panel_strategy_batch.py
tests/test_tester_run_files.py` — 466 passed, 1 skipped (Windows symlinks).
Independent review: `CODE_REVIEW_PASS`; the pin and three nits were applied.

The new tester report format of 2026-10-09 is not yet parsed by Performance
v2 import (duplicate analytics metric names); its adaptation is a separate
task before the tester update.

Next step: P2 (schema v12 audit first). Mass testing only after P6, in the
new database.

## Portfolio MILP composition selection (2026-10-10)

Stage 1 no longer fails when the finalist product exceeds
`search.max_enumerated_combinations`. Above the bound, one MILP per profile
over the union of slot options ranks up to `2 × max_candidates` distinct
portfolios; each is then evaluated by the unchanged exact path. The form
shows the live combination count, the bound and the mode. Contract:
[UI spec amendment 2026-10-10](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md);
decision: [ADR-0066](docs/decisions/0066-portfolio-milp-composition-selection.md).
The adapter progress sink lacked `import time`, which silently dropped every
adapter-level progress event. That is fixed in the same change. The change
also carries the 2026-10-09 lightweight readiness and combination-limit detail
hunks.

Diagnosis evidence: one composition × one profile costs about 49 s with 30
workers (106 s single-threaded). Only bootstrap is parallel, and it spawns a
process pool per call. On the real 117-finalist snapshot, facts took 102 s
(minute files are reused, about 0.2 s per symbol) and layer preparation 58 s.
Each MILP solve took 9–130 s, rising as cuts accumulate; 20 solves took 19 min.
The earlier 32-minute facts stage did not reproduce. Measured overheads:
per-request SSL context creation in the market snapshot, and a full 14 MB
`.panel-jobs.json` rewrite on each progress/journal sync.

Verification: composition selection 10 passed; adapter + selection 365
passed. A broad run of portfolio/Panel modules gave 1143 passed and 1 skipped.
Its 34 failures are known and pre-existing: 32 `ab_decline_cap_pct` /
11-of-12-column fixtures, the maintenance copy expectation, and the Windows
Node command-line length. `node --check` and `git diff --check` passed.
Independent review: one MAJOR (silent profile loss on a selector time-out) and
the MINOR findings were fixed; the re-review passed. Adapter progress now
reaches the Panel for the first time. The Panel reporter marks the total
inconsistent after the first substage switch, so later progress shows no
ETA. Next step: restart Panel to load the code, then rerun the
full-universe Campaign. No PerformanceDB or
tester was used.

## Portfolio own-history drawdown cap (2026-10-10)

Stage 1 now caps each member by its own worst drawdown: `x_i·d_i ≤ max_dd·B`.
Here `d_i` is the member's per-unit drawdown over its full own history, not
only the common window, and `max_dd` is the profile DD limit. This is a
per-member concentration cap, not a sum of DDs. The common-path DD, CDaR and
bootstrap stay as they were. The cap is enforced in the LP/MILP, in the
fixed-bank LPs and in the exact bank. Decision:
[ADR-0069](docs/decisions/0069-portfolio-own-history-drawdown-cap.md);
contract: the own-history amendment in the
[weighted-search spec](docs/specs/2026-09-14-portfolio-optimizer-weighted-search.md).
The Stage 1 summary, XLSX and `/results` show `own_history_dd_bank_usdt`;
the web table column waits for the parallel `app.js` work.

Why: in Stage 2 tester runs, the 9-pair candidate (bank 72) reached a 61%
drawdown. Under the new rule its TSEM/MSTU sizes would need a bank of about
730.

Real evidence: the frozen 61-pair Campaign, rerun offline with 30 workers,
gave PASS in 19 min.

| Level | Members | Bank | P30 | Own-history bank |
| --- | --- | --- | --- | --- |
| top | 61 | 715 | 1641 | 390 |
| bottom | 61 | 72 | 328 | 58 |

Before the cap the same ladder went from 61 members down to 9, with P30
1643 → 360. Lower levels now shrink the whole portfolio instead of
concentrating it. The margin bank binds at every level.

Verification: the own-history, weighted-search, adapter, optimizer and
panel portfolio suites give 933 passed, 1 skipped. `test_portfolio_input.py`
has 34 failures, all from the parallel v11 schema changes. Independent review
passed after one fix round.

## Stage 2 strategy files in tester format (2026-10-10)

Stage 2 strategy and tester-config JSON are now indented, with keys in the
template order. `balance_percentage_*` round up to a whole percent (0 stays
0). `lot_x` rounds to 0.01, with the remainder on the last order; a
non-positive last order fails closed. `MakerFee` is 0 for Bybit-only
batches. Contract: the 2026-10-10 tester-rendering amendment in the
[UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md).

Accepted by the operator: rounding percentages up enlarges positions (small
`q` the most) and can exceed the liquidity cap C.

Follow-up (operator rules): Stage 2 tester config uses `UpdateData=true`.
Report settings live only inside `report`; the shared MRS3 tester template
and the fast strategy-test writer no longer put them at the root. Both local
exports were updated.

Evidence: portfolios 01 (61 strategies, bank 716) and 10 (9 strategies,
bank 72) of `campaign-df84e07d885f4fb8a7b5d9c97a05b5d2` were re-exported
locally. All 70 files were checked. No tester run was started; the Panel
backend needs a restart to use the new rendering.

Verification: `tests/test_panel_portfolio.py` 297 passed, 1 skipped.
Independent review passed.

## Pair table controls and Stage 2 portfolio selection (2026-10-10)

The pair table gets a separate `История` column (bold `≈ N д` and the date
range), a `Выбрать N финалистов` button with an N field, and a display sort:
A–Z, shortest history first, or most finalists first.

The Stage 2 card lists every committed Stage 1 portfolio with its main facts
and a checkbox. `Отправить на тест` sends only the checked ones, in artifact
order. `/results` exposes the rows, and submission accepts `candidate_ids`;
each distinct selection is its own batch.

Contract: the 2026-10-10 pair-table/Stage 2 amendment in the
[UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md), which also
lists where Stage 2 writes on disk. Live: `/results` for the 61-pair Campaign
returns 10 portfolio rows. No tester run was started; the operator will start
it from the Panel.

Verification: static UI and panel portfolio suites pass, except the 2 known
pre-existing failures. Independent review passed.

## Portfolio pair history and selection summary (2026-10-10)

The Stage 1 form now shows, for every pair, the history of the finalists its
current limits would select. Under the table it shows the totals: pairs,
finalists, the common history of the whole selection, and a rough runtime
estimate. Readiness exposes `finalist_history` (day-aligned periods in rank
order, with no series reads); a history failure never blocks Stage 1.
Contract: the 2026-10-10 pair-history amendment in the
[UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md).

Live check: all 61 pairs and 117 finalists give ≈ 21 days of common history
(16.09–07.10) and ≈ 17 min, against 16 min measured. The shortest histories,
21 days each, are HORIZON, MEITU and SENSETIME.

The "minimum risk at target P30" search mode is recorded as a planned option
in the [optimizer spec](docs/specs/2026-09-05-portfolio-optimizer.md), Phase 4.
It has only a prototype so far; the product keeps the bank-ladder frontier as
"maximum P30 under a bounded risk/bank".

Stage 2 dry run (read-only, no tester start): for all 10 candidates of
`campaign-df84e07d885f4fb8a7b5d9c97a05b5d2`, `_stage2_material` built a tester
config and strategy JSONs (61 → 9 strategies, 16.09–06.10, `InitialBalance` =
required bank 716 → 72). Observation: candidate 10 has `InitialBalance` 72 while
its strategies' `max_balance` is 72.17 (whole-USDT bank vs exact sizing). The
real tester batch still needs explicit user authorization.

Verification: Panel portfolio and static UI suites pass, except the 2 known
pre-existing failures (maintenance copy, Windows `node -e` length).
Independent review passed.

## Portfolio bank-ladder frontier (2026-10-10)

Stage 1 now returns, per profile, up to `max_candidates` genuinely different
portfolios. Each is the MILP-optimal choice of finalists and weights for one
bank level across all compositions, then exactly evaluated. This replaces
both the exhaustive enumeration and the top-K MILP ranking, which produced
near-copies. Decision: [ADR-0067](docs/decisions/0067-portfolio-bank-ladder-frontier.md);
contract: the 2026-10-10 frontier amendment in the
[UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md).

The same change fixes three issues found on real data:
- binding bank ceilings failed exact LPs with `BANK_UNAVAILABLE`;
- a MILP on the default HiGHS thread count broke every later LP in the
  process with "Status 0: Not Set";
- scaled zero weights (`0E-90`) failed payloads.

Real evidence: the full universe (61 pairs, 117 finalists, 4·10^14
compositions, AGGRESSIVE, bank 1600) SUCCEEDED in 16 min with 10 monotone
candidates. Margin bank went 715 → 71, P30 1643 → 360, members 61 → 9.

Walk-forward check (14 days in-sample, 7 out): returns hold out of sample. In
this data the margin constraint binds rather than drawdown, so lower levels
concentrate. Their out-of-sample P30/maxDD (57 → 12) is worse than
proportionally scaling the full portfolio (60). The ladder is therefore
"maximum P30 per margin budget", not a minimum-risk frontier. A
min-CDaR-at-target-P30 mode was prototyped and kept diversification, with
out-of-sample P30/maxDD 38–61. It is recorded as a future option in the
optimizer spec (Phase 4).

Verification: portfolio suites 593 passed; independent review and re-review
passed.

## Portfolio Stage 1 throughput and hang fix (2026-10-10)

Three reviewed infrastructure changes. Contracts: the 2026-10-10 amendments in
the [weighted-search spec](docs/specs/2026-09-14-portfolio-optimizer-weighted-search.md)
and the [UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md).

- **Job journal.** The frozen Campaign in a job runtime is shared by reference
  and never serialized into `.panel-jobs.json`. Measured: 2.26 s → 0.16 s per
  progress update with the real 14 MB journal and a 16 MB Campaign.
- **Market reference.** One keep-alive HTTP client and 10 requests/sec instead
  of 2/sec, with retry and cooldown unchanged. Measured: 14.9 s → 6.0 s on 10
  symbols. The snapshot is taken once per Campaign per unique pair; it is not
  repeated per strategy or portfolio.
- **Bootstrap pool.** The worker context goes through a private temp file in
  `%TEMP%\mrs3-process-context`, not the child start-up pipe. A child dying at
  start-up now raises `BrokenProcessPool`; the pool is replaced and the
  unfinished tasks are retried once. Before, the parent blocked forever; a
  2026-10-09 Panel job hung for over an hour this way. One pool serves the
  whole Campaign and is owned by the job thread. Measured: 49 s → about 12 s
  per composition.

Verification: focused suites pass (panel jobs 49, market snapshot 36, process
worker 6). The independent review passed, and its MINOR findings (thread
ownership, cache release, temp-file sweep) were fixed with tests.

## Portfolio executable identity on real lot-model data (2026-10-10)

Every real lot-model Campaign failed post-search with
`WEIGHTED_EXECUTABLE_IDENTITY_INVALID`: the Decimal `liquidity_v25_usdt` and
`liquidity_a15` evidence was not JSON serializable. A real-data probe found no
other Decimal path. Both fields are now bound as exact, finite canonical text;
non-finite values fail closed. Contract: the 2026-10-10 identity amendment in the
[UI spec](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md). Verification:
adapter and selection suites, 366 passed; a restarted Panel run of the 61-pair
5×2 Campaign passed composition 0 and continued. Independent review passed.
The earlier run of that Campaign hung in a Windows multiprocessing spawn: a
bootstrap child died at start-up while the parent was blocked writing to its
pipe. It needed a Panel restart. The bootstrap pool spawn path is unchanged,
and this remains an open reliability risk.

Real-data evidence: Campaign `campaign-00b399c7100c42bf8692075d4ab9b72c` covered
61 pairs, 5 of them with two finalists (32 compositions, exhaustive
path), AGGRESSIVE profile, bank 1600. It SUCCEEDED in 78 min (about 2.4 min
per composition) with 10 candidates. Candidate 1: required bank 719 USDT,
source-proxy P30 1502 USDT/30d, historical DD 30%; candidates 2–10 differ only
in the SOXX/SPCH/VST/XLK finalist choice (P30 1485–1502). These are
pretest proxies, not tick-tested MRS3 results.

## Panel job admission reliability (2026-10-09)

Panel job submission now persists `QUEUED` before worker dispatch, removes an
unsaved admission from memory, retries only transient Windows journal replace
errors (5/32/33) up to five attempts, and returns `JOB_PERSISTENCE_FAILED` with
HTTP 503 through direct routes and portfolio service wrappers. Contract:
[SINGLE_MODE report collection](docs/specs/2026-09-28-single-mode-report-collection.md);
safety decision: [ADR-0063](docs/decisions/0063-panel-job-admission-on-journal-persistence.md).

Verification: 9 focused journal/portfolio/report-collection regressions passed;
the related combined run had 467 passed, 1 skipped, and 1 pre-existing fixture
failure. The same failure was reproduced from a clean `HEAD` archive: the
portfolio test helper supplies 11 values to the 12-column `selection_results`
table. `git diff --check` passed. Live Panel restart and status check are still
complete: Panel API on port 8766 responds, the stale in-memory QUEUED job is
gone, and no `hb_c.exe` process is running. No tester run or PerformanceDB was
used.

## Performance v2 import launcher source (2026-10-09)

Import job `8dda736e70b3449e890d1a4c0f908af7` failed before reading reports.
Diagnosis found the local venv's editable install resolving code from
`.worktrees/heavy-db-optimization`, which accepts schema v8, while the
configured target is schema v10. The job's public error does not expose the
underlying DuckDB open exception, but this runtime mismatch is incompatible;
the current checkout's `src` passes the read-only v10 schema guard. No import
was retried; database inspections used read-only connections.

The Windows Panel launchers disable delayed expansion; the shared launcher
prepends its checkout's `src` to `PYTHONPATH` without an empty path component
and preserves existing entries. The other launchers delegate to it. Git pins
Windows `.bat`, `.cmd`, and `.ps1` scripts under `scripts/` to CRLF. Focused
`tests/test_panel_launcher.py` passed cmd.exe source-resolution checks for
direct start and both wrappers with unset and pre-populated paths, including
`!`, `^`, `%`, and a later duplicate `src` entry while caller delayed expansion
was enabled. Panel was restarted on port 8766 and `/` returned HTTP 200. A
stale non-listening `start_new_panel` process tree was stopped; the active
listener remained available. No import was retried; the next step is user
verification.

## PerformanceDB maintenance

The maintenance card is implemented in the Strategy and DD5 tab. It supports pair selection, strategy-count previews, Rejected retirement, full pair deletion, progress reporting, elapsed time, and actionable database errors. `Удалить Rejected` now physically removes per-strategy facts, window/equity caches, tags, rejection sources, and selection rows, compacts the retained result to interval/provenance identity, then atomically marks the typed identity as `DISCARDED`; only strategy settings/orders and that compact tombstone remain for deduplication. The code targets schema v10 and adds no cleanup markers, deletion timestamps, backups, or maintenance tables. Contract: [PerformanceDB maintenance](docs/specs/2026-10-06-performance-db-maintenance.md); retirement decision: [ADR-0061](docs/decisions/0061-performance-db-rejected-retirement.md).

Retirement verification after the expansion: maintenance 52 passed, selection 239 passed, importer 120 passed, XLSX export 11 passed, and the Panel discarded-catalog check passed; `node --check src/mrs3/panel_web/app.js`, `py_compile`, and `git diff --check` passed. All database tests used isolated temporary DuckDB fixtures; no live PerformanceDB or Panel process was used.

The legacy v6-to-v7 migration fix and the narrowly scoped recovery for the known v9 catalog error are included. The feature contract is [PerformanceDB maintenance](docs/specs/2026-10-06-performance-db-maintenance.md). Independent Claude Opus review passed. Focused migration, maintenance, static UI, and Panel HTTP checks passed. No live PerformanceDB was accessed or modified during verification.
On 2026-10-08 a live reproduction with only `AALUSDT` selected showed that
Rejected preview memory growth came from a repeated correlated residual check,
not from the selected pair set. The residual recovery lookup is now one
set-based query. The focused regression and full maintenance suite passed:
`60 passed`; an isolated AAL preview completed in `0.79 s` with a 4 GB DuckDB
memory limit. The Panel process was stopped after the reproduction; no live
database rows were changed.

## Performance v2 selection and equity regime

The Performance v2 fixed-filter sequence is implemented and independently
reviewed. Its fixed Minimum Shift gate is enabled at `0.3%` immediately before
`PnL DD5/30 + PnL B/30`; the contract is [the filter-sequence specification](docs/specs/2026-10-01-performance-v2-filter-sequence.md).

The shared [XLSX column contract](docs/specs/2026-10-07-performance-v2-xlsx-column-contract.md)
now gives stable columns and separates the PRE28/W28/W14/W7 regime blocks.
The equity-regime classifier, cache, selection and Panel integration are
implemented locally; its live migration remains open. Evidence and exact
status rules are linked from the [equity status map](docs/specs/2026-10-03-equity-regime-status-map.md).

Card 6 partial selection review import and the global finalist retest control
are implemented. In the Card 6 `Импортировать статусы и ранги из XLSX` path,
each import now reconciles prior FINALIST decisions for only the imported
Pair + Side: an absent ID or a row without FINALIST plus rank becomes RESERVE
with no rank; explicit REJECTED remains REJECTED with no rank. The ledger stays
append-only. Other blank `User Status` and `User Rank` cells retain the
documented clear behavior. Final focused verification with
`.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_performance_v2_selection_review.py tests/test_performance_v2_store.py tests/test_performance_v2_compact.py tests/test_performance_v2_maintenance.py tests/test_panel_performance_v2.py`
passed 477 tests with 4 platform skips. This covers transactional v9-to-v10
migration, rollback/retry, and Panel schema preflight. A separate clean-HEAD
control reproduced 15 existing failures across the static-shell and legacy
v6-v8 benchmark suites (18 tests passed); these failures predate this change.
The live v9 database migration is still pending: after deploying this version,
the Panel schema preflight must run before the Card 6 import. No live database
mutation or Panel restart was performed. Independent code review returned
`CODE_REVIEW_PASS`.

### 2026-10-09 - Card 6 XLSX imports: reconcile prior finalists

- Both Card 6 import paths reconcile prior FINALIST decisions for the exact Pair + Side: a prior FINALIST remains FINALIST only when re-imported with FINALIST and a non-empty rank; explicit REJECTED remains REJECTED and clears rank; every other prior FINALIST, including IDs absent from the current selection run, is appended as RESERVE with rank NULL. Existing review comments/history are preserved. The selection-review button contract is [ADR-0065](docs/decisions/0065-performance-v2-card6-partial-import-finalist-reconciliation.md); the combined-control path is covered by [ADR-0064](docs/decisions/0064-performance-v2-finalist-retest-review-reconciliation.md).
- Verification after review fixes: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp <unique C:\Temp folder> --tb=short tests/test_performance_v2_selection_review.py tests/test_performance_v2_finalist_retest.py tests/test_panel_performance_v2_retest.py -k "not PARETO_PLATEAU_POINTS_PER_ORDER"`: 215 passed, 1 skipped, 1 deselected; temp folder removed. Four later edge-case regressions also passed. The skip is Windows symlink availability.
- The 215-test run and four edge-case runs preceded the final REJECTED-tag-scope patch. Tests were not rerun after that patch per the user's explicit instruction. The final tag-sync change is therefore unverified; if testing is later authorized, cover both preservation of an omitted finalist's existing REJECTED tag and cleanup/re-add of the tag for a submitted REJECTED row.
- Three unrelated broader-suite failures were reproduced on clean HEAD `8b755c4a292e0107ea6e2b701df6b865f9d792a9`: PARETO reason-alias rendering fails before import; v4 import migration test expects schema v9 though migration returns v10; prune fixture inserts 11 values into the 12-column `selection_results`. No production change was made for them. The Panel endpoints serialize writers with process and cross-process PerformanceDB locks; the import transaction covers finalist lookup, review append, and REJECTED tag synchronization.
- The user imported `AMGN.xlsx`. Read-only verification matched its workbook hash to an import journal entry for AMGNUSDT/LONG (15 rows): 3 FINALIST (ranks 1–3), 8 RESERVE, 4 blank. No database mutation by the agent; no Panel restart performed by the agent. Independent code review returned `CODE_REVIEW_PASS`. Next: after restarting Panel from this checkout, the user can re-import the 81 workbooks; the final tag-scope patch remains unverified by tests at the user's request.

## Other verified Panel changes

Panel PerformanceDB recalculation now serializes access through the controller
lock; live PURR confirmation remains open below. Performance v2 researched
stages 6–9 and the near-duplicate rule are governed by the
[researched-filter contract](docs/specs/2026-10-02-performance-v2-researched-filters.md).

Portfolio Stage 2 ordered-batch is fixture/fake-only and does not authorize a
real tester run. The Source v6 fresh compact multi-scope pipeline is complete;
this does not establish realized MRS3 performance.
## Bulk finalist equity-filter application (2026-10-07)

Card 6 has an explicit asynchronous `Применить эквити фильтр` action for the
frozen successful FINALIST or FINALIST+RESERVE cohort. It warms only missing
equity cache rows, runs only `filter_equity_regime`, publishes group snapshots
atomically, and reports phase/progress/result counts/typed errors. Import and
recovery do not invoke it automatically. Contract: [Performance v2 finalist
retest control](docs/specs/2026-09-09-performance-v2-finalist-retest-control.md).

Local backend, UI, equity and publication checks passed; the pre-existing
`PARETO_PLATEAU_POINTS_PER_ORDER` alias expectation is unchanged. No live
database, tester or Panel process was used.
## Bybit base-lot XLSX export (2026-10-07)

The one-command [Bybit base-lot export](docs/specs/2026-10-07-bybit-base-lot-export.md)
now maintains ten UTC-date size columns in `Actual!C:L`; each header is a date
and each value uses the seven complete dates preceding that header date. The
pair and listing-date columns A/B are preserved; legacy output columns C/D are
replaced. Later runs rotate only the date columns. A complete current date is
skipped unless retained error cells need retry. Missed dates rebuild a
contiguous ten-day window while mapping overlapping values by date; future
listing dates remain blank. Historical errors are retried and empty filters are
created through L. The filter preserves criteria outside the rewritten dates.
Focused verification: 39 tests passed; `git diff --check` and `py_compile`
passed; independent Opus `CODE_REVIEW_PASS`. No live workbook or network run
was performed.
## Bybit base-lot export: tester parquet, completeness, 10-day average (2026-10-16)

The [base-lot export](docs/specs/2026-10-07-bybit-base-lot-export.md) now
reads only the tester's `<SYMBOL><date>_1m.parquet` day files (CSV is no longer
produced). A missing day is backfilled from the official Bybit trade archive
and written in the tester's own parquet format (INT64/DOUBLE/INT32, REQUIRED,
ZSTD, `hb.schema = candles_1m/1`) through `pyarrow`, a new dependency. Every
run aligns the ten date columns with the run date and fills every blank or
`ERROR:` cell of a pair listed by that date; numeric cells are kept. Layout:
A pair, B listing, C `Среднее 10д, USDT` (floored mean of the numeric lots in
D:M, recomputed on every saved run), D:M ten dates. A sheet with dates in C:L
is moved one column right once (values, styles, widths without overlapping
`<col>` ranges, filter criteria); foreign data in C1 or in M fails without
replacing the workbook. ISO text date headers left by an Excel table are
accepted and rewritten as dates.
Verification: 132 focused tests passed (`test_calculate_bybit_base_lots`,
`test_portfolio_minute_capacity`, `test_portfolio_minute_refinement`,
`test_portfolio_position_sizing`); `git diff --check` clean; independent
`CODE_REVIEW_PASS` after one fix (parquet column types are now validated). Live run on the
local workbook: 546 lots for 2026-10-07..16, 91 pairs, 0 errors, 0 blank
listed cells; a second run the same day is a byte-identical no-op. Open: one
tester run on a day written by the export to confirm Parquet.Net reads it.

## Fresh shortlist Minimum Shift gate (2026-10-08)

The fresh **Shortlist and READY JSON** flow has an optional
[Minimum Shift contract](docs/specs/2026-10-08-fresh-shortlist-minimum-shift.md)
with [ADR-0060](docs/decisions/0060-fresh-shortlist-minimum-shift.md) and an
[implementation plan](docs/superpowers/plans/2026-10-08-fresh-shortlist-minimum-shift.md).
It checks only the first order Shift, preserves strictly increasing later
orders, and canonicalizes `0.3%` as `30 bp` across shortlist, audit, READY and
RUNS provenance. Focused tests and independent review passed.

No live database, tester, migration, or Panel restart was performed; reload is
the first operational step below.
## Blockers and open tracks

- Live PerformanceDB: a read-only check on 2026-10-07 validated the schema v9 catalog and required tables. Finalist job `a21a55e57fc749ce9a132453594284d3` is `COMMITTED` at 250/250 and its metadata inbox is ready. Import child `5ee6a404025f4e15b63a4fe2a2b9c075` failed on a shorter effective period; rollback checks confirm current results were not changed. The per-strategy rejection fix passed independent review and Panel was restarted; no retry was started. Use a fresh retest dated 2026-06-01 through 2026-10-05.
- READY JSON: at the last diagnostic on 2026-10-04, the tester job journal showed `RUNNING / BOT_RUN` with zero of 2,418 completed, and the Panel process had not been restarted. Reload the Panel and confirm that no tester process is active. The recovery procedure for this exact journal state is not linked or verified here. Do not edit the journal; identify and verify the supported Panel recovery action before recovering the job.
- Live PURR recalculation: as of 2026-10-06, no confirmation is recorded after the controller-lock change.
- MRS3 performance evidence: real tester tick-test results and DD5 retesting are still required before making final performance claims. Source metrics alone do not establish realized strategy performance; see [PRD.md](PRD.md).
- Performance v2 equity-quality M5: evidence covers bounded real-data slices only. Full-corpus timing and user acceptance remain open in [the M5 evidence plan](docs/superpowers/plans/2026-09-26-performance-v2-equity-quality-m5-slice-evidence.md).
- Heavy PerformanceDB follow-ups: residual import, analysis, cache, and materialization profiling is explicitly deferred and tracked in [the deferred follow-up plan](docs/superpowers/plans/2026-10-01-deferred-heavy-db-follow-ups.md). That plan does not authorize implementation.
- Bybit collector: phases 1 through 8 are delivered; live integration and soak evidence in phase 9 remain open. See the [collector specification](docs/specs/2026-09-05-bybit-market-data-collector.md) and [implementation plan](docs/superpowers/plans/2026-09-05-bybit-market-data-collector.md).
- Canonical Phase 1: Tasks 0–12B are recorded complete; Task 12C fresh real-source smoke/performance remains open. Follow the [active specification](docs/specs/2026-08-16-mrs3-v07-canonical-phase1.md) and [implementation plan](docs/superpowers/plans/2026-08-16-mrs3-v07-canonical-phase1.md).
- Portfolio Optimizer: Stage 2 ordered-batch implementation has fixture/fake evidence and independent `CODE_REVIEW_PASS`; no real tester run occurred. M6–M8 evidence ledgers are unadopted until the governing phased specification is reconciled. This does not authorize a real joint tester run, recommendation, trading admission, or live use. M5/M6 readiness, PnL floor, individual-DD ceiling, liquidity/freshness limits, profile ranking, and fresh user authorization remain open gates. The Phase 13 limiter work remains off-only while the bot limiter is not operational. See the [optimizer specification](docs/specs/2026-09-05-portfolio-optimizer.md), [UI contract](docs/specs/2026-09-06-portfolio-optimizer-panel-ui.md), [batch decision](docs/decisions/0059-portfolio-stage2-sequential-batch.md), and [implementation plan](docs/superpowers/plans/2026-10-07-portfolio-stage2-sequential-batch.md).
- Portfolio MILP selection: oversized universes rank by the discovery-LP proxy; the union grid can be shorter than a composition's own period and LONG+SHORT symbols use an approximate one-way mask. Near-duplicate finalists can yield micro-variant candidates. Lightweight readiness does not compare prepared `source_digest`; a stale preparation is caught only at Campaign creation/run. See [ADR-0066](docs/decisions/0066-portfolio-milp-composition-selection.md).
## Next steps

1. Restart or reload the Panel before using the Minimum Shift, finalist equity-filter and Finalist/Reserved-only selection controls; no live restart was performed during verification.
2. Run a fresh finalist retest from 2026-06-01 through 2026-10-05; then review the per-strategy import outcome before confirming it.
3. Confirm the live PURR recalculation after the Panel lock change.
4. Verify the READY JSON job state and identify the supported recovery procedure; do not edit the journal.
5. Keep the remaining Performance v2, database profiling, collector, and Portfolio Optimizer gates within their linked plans and specifications.
6. Do not make final MRS3 performance claims until real tick-test results and DD5 retesting are available.

Maintenance preview memory fix: rejected preview decision resolution is now
scoped to each selected symbol and its strategy IDs instead of loading the
entire selection/review history. Focused maintenance and selection-review
regressions pass; the final targeted suites pass
161 tests, and the Opus implementation review returned CODE_REVIEW_PASS. Live
PerformanceDB was not used.

Finalist/Reserved-only Performance v2 mode: implemented and independently
reviewed. Targeted request, UI, pipeline, readiness and XLSX checks pass (7
tests); the six-module selection/UI/export batch reports 798 passed and 5
platform skips. Its 3 failures were reproduced on clean HEAD: the maintenance
card string expectation, a Windows Node command-line length error, and the
PARETO reason-alias expectation. The UI toggle was exercised with a Node event
simulation; no live database or browser smoke was run. Reload the Panel before
using the checkbox.

Maintenance preview preflight fix (2026-10-08): removed the database-wide
reachability audit from selected-pair preview and apply revalidation; catalog
inspection retains that full audit. Replaced pair/global fingerprint sorting
with bounded DuckDB multiset aggregates, leaving key collection only for the
small strategy-ID target scan. This removes the `ORDER BY ALL` sort and the
long post-sort Python walk over the large equity/actions tables. Maintenance
and selection-review suites pass 165 tests; the new regressions verify the
preview bypass and order-independent fingerprint. No live PerformanceDB was
modified. The Opus re-review returned `CODE_REVIEW_PASS` after the orphan
preflight and two-direction action ownership fixes.

Follow-up review fixes (2026-10-08): full apply now repeats the complete
reachability audit after fingerprint revalidation and before the first DELETE,
so pre-existing orphan rows fail closed without deleting the selected pair.
Scoped action ownership now checks both `strategies.symbol` and
`strategy_actions.symbol`. Added regressions for an orphan preflight and an
action-symbol-only mismatch; maintenance suite passes 58 tests. No live
PerformanceDB was modified.

Panel maintenance recovery checks also pass 14 tests. The catalog endpoint
optimization is recorded separately below.

Catalog picker follow-up (2026-10-08): the Panel maintenance catalog endpoint
now calls `catalog_symbols()` instead of the audited `catalog()` helper. The
pair picker performs schema validation and symbol lookup only; full reachability
validation remains in full apply before DELETE. Maintenance and Panel
maintenance checks pass 73 tests in the focused run. No live PerformanceDB was
modified.

Interrupted finalist RETEST recovery (2026-10-08): recovered the existing
2360 indexed reports plus the already-tested manual finalist report into the
same 2361-member frozen cohort; no strategy was resubmitted. The metadata inbox
and replacement import both committed: 2361 imported/successful, 0 skipped,
0 rejected, 0 failures; cohort outcomes are finalized and the equity filter is
eligible for all 2361 strategies. Targeted recovery suites pass 180 tests with
1 Windows symlink-capability skip. The live PerformanceDB was intentionally
updated by this import. Next step: run the eligible equity filter when desired;
no full finalist RETEST is needed. Blockers: none.

Portfolio Optimizer lightweight readiness (2026-10-09): startup readiness now
reads current FINALIST identity/count metadata and prepared-row headers only;
it does not load strategy/action/equity payloads or decode `prepared_json`.
Explicit finalist preparation and Campaign submission retain the strict
prepared-artifact validation. Recovery keeps the latest terminal Campaign
visible while unlocking Stage 1 for `SUCCEEDED`, `FAILED`, `CANCELLED`, and
`INTERRUPTED`; only a nonterminal job freezes the form. Focused checks pass 9
tests; review follow-up coverage passes 12 tests. Full Panel UI/Portfolio
modules report 444 passed, 1 platform skip, and 3 known unrelated failures
(maintenance copy expectation, Windows Node command-line length, and the
11/12-column selection fixture). The shared selection insert was made
column-explicit for schema compatibility; the full `test_portfolio_input.py`
run then reports 117 passed and 32 unrelated failures from the in-progress
`SelectionConfig.ab_decline_cap_pct` snapshot contract and remaining historical
raw inserts. A separate strict-series/light-metadata contract run passes all 7
tests. The maintenance copy failure reproduces from an isolated clean `HEAD`
archive. No live database or Panel process was changed. Next step:
restart/reload Panel and confirm the pair picker becomes editable without a
full startup audit.

Portfolio combination-limit error detail (2026-10-09): when weighted finalist
composition enumeration exceeds `search.max_enumerated_combinations`, the
persisted failed-Campaign message now retains `COMBINATION_LIMIT_EXCEEDED` and
adds the exact product and configured limit. The adapter count/limit regression
and persisted Panel status regression pass (3 focused tests). Reload Panel
before the next run; the failed status will show `COMBINATIONS=N; LIMIT=M`.

## Performance v2 P5.2 all-pairs filtering and direct rejection (2026-10-10)

P5.2 is implemented and independently reviewed. The root-verified evidence
ledger is: additive v11 migration tests/review `PASS`; selection suite `253
passed`; aggregate-import suite `12 passed`; publication suite `65 passed`;
all-pairs suite `17 passed`; Panel suite `205 passed, 4 skipped`; and static UI
suite `172 passed`. Task6 styling/STALLED-boundary work and Task7
filters-11-27/performance work are now root-verified, each with independent
`CODE_REVIEW_PASS`.

The current v11 contract-suite accounting is `469 passed` with `20 warnings`
total. The initial full rerun recorded `455 passed` plus `14` compact failures,
all solely from the `C:\Temp` capacity guard. A sanctioned capacity mock reran
the compact module with `30 passed`, so all `469` logical tests pass. No real
full-corpus compaction rehearsal is claimed; the guard itself was tested by the
mock.

The bounded PARETO/filter measurement conclusion is that the existing
`PARETO_PLATEAU_POINTS_PER_ORDER` reason/alias expectation is unchanged;
disabled legacy filter stage bodies are skipped, but candidate preparation is
unconditional. Legacy filters are thus collapsed presentation-only; no risky
loader optimization or safe-disable branch landed. This is not a production
performance claim. The final holistic independent review returned
`CODE_REVIEW_PASS`; no live-database, tick-test, or DD5-retest claim is made.

## Bybit base-lot history repair (2026-10-10)

The partially initialized Actual-sheet history was rebuilt for 2026-10-01 through
2026-10-10. `--rebuild-history` recalculates all eligible pair/date cells,
preserves the workbook if any calculation fails, clears stale output filters,
and formats integral lot sizes without decimal digits. The workbook now has
910 numeric values for 91 pairs and ten dates, zero calculation errors, and
the expected 10 blank cells in the empty trailing row; AutoFilter is `A1:L93`.
The focused suite passes 44 tests, `py_compile` and `git diff --check` pass, and
independent Opus review returned `CODE_REVIEW_PASS`. Next step: normal daily
execution uses the no-flag rotation path. Blockers: none.

## Published-surface analysis catalog (2026-10-14)

The "1. Analysis of published surface" picker now lists validated
`*.analysis-v6.duckdb` files directly under the configured analysis root;
nested folders are excluded. Verification: the catalog, fresh-strategy,
shortlist and static-UI panel tests pass (267 tests) using
`.venv\Scripts\python.exe -m pytest -q tests\test_panel_analysis_catalog.py tests\test_panel_fresh_strategies.py tests\test_panel_fresh_shortlist_v2.py tests\test_panel_static_ui.py`.
The complete `test_panel*.py` sweep was stopped before completion. Next step:
restart the Panel to load the updated catalog logic.

## SINGLE_MODE collection launch checkbox and import integrity (2026-10-14)

The report-collection checkbox now starts unchecked on Panel page load, and
collection status refreshes no longer change the choice. The checkbox now also
selects the target of the shared Verify/Import controls: unchecked uses the
current ordinary tester job even when an older collection remains OPEN or
VERIFIED. Reload recovery includes the newest COMMITTED ordinary tester job
before inbox verification, so an explicit Verify remains available after a
page refresh. Four focused static UI checks pass. The current ordinary
20-strategy tester job is COMMITTED and awaits its ordinary inbox verification;
the older 20-report collection remains VERIFIED but is not selected while the
checkbox is off. No PerformanceDB import was launched by the agent. Next step:
hard-refresh Panel and press Verify with the checkbox off.

## Panel job journal locked-replacement fallback (2026-10-14)

The failed SINGLE_MODE admission was rejected before tester startup because an
external process kept the 14 MB `.panel-jobs.json` open without delete sharing.
The lock remained after stopping Panel, while direct read/write access remained
available. Atomic replacement remains primary and retains its bounded 10-attempt,
0.45-second Windows retry. If that retry is exhausted for an existing writable
journal, the registry saves and fsyncs the old journal to a recovery sidecar,
then overwrites and verifies the completed payload in place. The complete
sidecar is atomically published after storing both old and intended journal
bytes and is directory-synced before the overwrite where supported. Startup and
later saves preserve either complete payload, or restore old bytes after a
partial overwrite; a missing journal is restored and an unreadable or malformed
sidecar fails closed while preserving both files. Format-1 records with an
intended digest remain readable. Directory entries are synced after atomic
replacement and sidecar/staging-file changes where supported. Missing journals
and failed direct writes remain fail-closed. Strategy paths now reject symlink
and junction/reparse-point components, derive cleanup expectations from fixed
allowed paths, and remove child reparse points without traversing targets. A
real locked-journal save previously retained all 190 jobs. The seven changed
config/runner/Single Mode/UI/template test modules pass 467 tests; one real
symbolic-link test skips because this Windows account cannot create symlinks.
Portable link checks and real Windows junction path/cleanup tests pass. No
tester or PerformanceDB import was started by the agent. Next step: launch
SINGLE_MODE from the refreshed Panel.
