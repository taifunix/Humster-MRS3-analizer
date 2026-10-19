# Panel for the simulation workflow: three screens, obsolete parts removed

**Status:** Approved by the user 2026-10-18 («вноси эти правки и начинай с
этапа 1»). Stage 1 implemented: 1a, 1b part A (tick test from a store READY
folder) and 1b part B (retired screens removed, three-screen shell); see the
evidence sections. Stages 2–5 pending (stage 2 = pair registry, in progress).
**Depends on:** [pair registry](2026-10-18-pair-registry.md) (pairs, decisions,
settings, finalists, sync), [MRS2 trade store](2026-10-17-mrs2-history-store-and-tester-sampling.md)
(history, READY, tester sampling), PerformanceDB (MRS3 tick-test results).
**Replaces:** the screens «01 Тестирование», «02 Source DB», «03 Поверхности»,
«04 Стратегии и DD5» and the card «MRS2 STORE».

## Why

With the MRS2 simulator, history is computed for every pair in hours instead of
being collected by tester runs. Half of the Panel served the retired chain
(tester grid runs → Source v6 → surfaces → plateau shortlist → READY JSON) and
the multi-stage Performance v2 selection whose filters the READY v4 study
dropped (Pareto, hidden ranks, A/B, legacy filters, filters 6–9). It still
loads on every Panel start.

## Screens

Three screens: **Пары и стратегии**, **Портфели**, **Настройки**.

### ① Пары и стратегии (main screen)

- **Exchange switch** at the very top: Bybit / BingX (user decision 2026-10-18:
  BingX is a full second exchange). Everything below — tables, actions, READY,
  tester, tick tests — works for the selected exchange.
- **Action bar:** Синхронизировать · Пересчитать историю · Пересобрать READY ·
  Проверить тестером · Тик-тест MRS3 · Ретест финалистов. Every action except
  «Синхронизировать» first opens a **plan dialog**: one row per pair (or per
  strategy) with what would be done, a checkbox per row, «Выделить все» /
  «Снять выделение»; only checked rows run.
- **Task area** (one task at a time), from real facts reported by the task:
  - progress bar (done / total units of the current step, e.g. segments,
    tester jobs, strategies);
  - elapsed time and remaining-time forecast (from the rate so far);
  - current step and pair;
  - errors as they happen (pair, step, message) and a final summary;
  - «Стоп». For tester runs the stop is cooperative (after the current job,
    the tester's files restored); for other tasks it stops the whole process
    tree.
- **Views** (switch buttons), each a table with sorting, filters, a column
  chooser and «Excel» export of what is shown:
  - **Пары** — stage, history sufficiency, periods, READY now, which source the
    newest period uses (simulator / tester), tester check, tick test, finalist,
    decision, base lot;
  - **Стратегии** — see below;
  - **Справочник**, **Ликвидность**, **Изменения** (registry views).
- **Pair card** (click on a pair): periods, READY per TF, tests history,
  decisions and personal thresholds (editable).

### ② Портфели

The portfolio optimizer as today; it reads finalists and computed liquidity
from the registry.

### ③ Настройки

PerformanceDB and exchange; common READY thresholds; liquidity window; pair
universe rules per exchange; paths (bot, trade store, registry, sync
repository); optimizer settings; PerformanceDB maintenance.

## «Стратегии» view (replaces the Pareto/filters screen)

User decision 2026-10-18: keep only the agreed hard filters; the selection
screen has no remaining value of its own. Its useful parts move into this view:

- **Rows:** READY structures of the selected exchange (pair, TF, orders, score,
  training DD, category, rank in pair) joined with their MRS3 tick-test
  results from PerformanceDB.
- **Metrics after the test** (user request): PnL, PnL/30, MaxDD, time under
  water and the other most important XLSX columns. A **column chooser** picks
  which are shown; the choice is saved on the server (survives restarts and is
  the same in any browser); more columns can be added later without code
  changes to the view.
- **Filters and sorting** on the important columns. The two agreed hard filters
  are **filter toggles**, computed on the fly from the metrics:
  - hard cutoffs (`DD > 23 and PnL/30 < 3·DD`, `PnL/30 ≤ 4`, the ratio branch —
    [filter sequence §3](2026-10-01-performance-v2-filter-sequence.md));
  - top-5 trades share > 80 % (§5 there).

  A failing row shows the triggered rule and values in a «Причина» column;
  «скрыть отсечённые» hides them.
- **Rank in pair:** category (low → moderate → high) → score descending →
  lower Ulcer of the training tick test (plateau v4 plan).
- **Finalist marks:** FINALIST / RESERVE / REJECTED / empty set directly in the
  table, plus a user rank. They are written to the registry `finalists` table
  (append-only, synced) — the only place a mark is stored.
- **Excel round trip** (still needed): «Excel» exports the rows with marks and
  ranks and a stable strategy id; the user edits marks/ranks in Excel; «Загрузить
  отметки» imports them back (matched by strategy id, every change logged).
  **Export has no side effects**: unlike today, it no longer writes a selection
  run or REJECTED tags to the database — filters are recomputed, not stored.
- **Ретест финалистов** action: retests current finalists on the newest period,
  with an option «включая резерв».

## Tester in the workflow

The user will run a couple of dozen READY pairs through the tester and decide
whether tester runs change the finalist choice; until then both ways work:
- READY uses the tester results of a pair's newest period automatically when
  they exist, otherwise the simulator (the «Пары» view shows which);
- «Проверить тестером» (plan → run → import of the sampled points of the
  newest window, `select-v1`) stays as a separate action.

## Removed — and disconnected, not just hidden

Panel routes, controller services, startup requests, JS and tests of:
- the screener (SCREENER 01), RUNNER 01 and RUNNER 02;
- Source DB merges and surfaces;
- surface analysis, the plateau shortlist and READY JSON generation;
- A/B Performance analysis, the Pareto/filters selection screen, legacy filters
  11–27, filters 6–9 and their settings, the analysis profile;
- the «MRS2 STORE» card (its functions become the actions above).

The underlying modules that other kept code still imports stay; modules used
only by removed screens are retired with their tests in the same change, after
an import check.

Kept and moved: MRS3 tick-test run and import into PerformanceDB (→ «Тик-тест
MRS3»), finalist retest (→ «Ретест финалистов»), PerformanceDB export (→
«Excel» of «Стратегии»), PerformanceDB maintenance (→ «Настройки»), the
optimizer (→ «Портфели»).

## BingX as a full second exchange

Everything that is Bybit-only today gets an exchange dimension, and BingX
works on its own full TradFi catalog, not only on assets Bybit also lists.
Names, status, the minute data layer and the minute verification are
specified in the registry ([BingX names and data](2026-10-18-pair-registry.md));
here:
- **trade store:** BingX minute data through `ensure_minute_days`; contract
  parameters (tick, qty step) from the registry; BingX fees in the run settings
  (maker 0.00012, taker 0.0003 — the 40 % discount, user decision 2026-10-16);
  a BingX day is used only when the minute verification accepted it (`OK` or
  `VERIFIED_GAPS`);
- **MRS2 tester sampling and MRS3 tick tests:** data ensured before every run,
  `UpdateData = false`; tester config `settings[*].basic.symbol` = BingX API
  symbol; strategy `exchange.name = BingX`, `exchange.account = bingx`; BingX
  fees and the BingX templates (`templates/exchanges/BingX/…`; the Bybit-only
  `maintenance_margin_rate` omitted); report symbols mapped back to the human
  ticker;
- **READY, PerformanceDB and finalists** keyed by (exchange, human ticker);
- the old consumers of the BingX code (screener BingX mode, RUNNER 01 BingX
  mode, `build_bingx_screening_sheet.py`) are removed with the screens in
  stage 1.

## Defect to fix (found 2026-10-18)

A «Посчитать историю» task started from the Panel stayed RUNNING for hours: its
main process had ended, two process-pool workers survived holding the output
pipe, and the Panel waited for that pipe to close. Fix with the new task area:
stop the whole process tree, do not let workers inherit the output pipe, and
finish the task when the main process exits even if a pipe is still held.

## Stage 1 contract: removal without losing a working path

Found while mapping dependencies (2026-10-18): the MRS3 tick test
(`strategies_tester_start`, card «3. Test and Import to Performance DB»)
takes an `analysis_run_id` of the old fresh analysis and reads its READY JSON
generation manifest (protocol v2). Removing the old analysis first would leave
the tick test without input, and removing the selection screen before the
«Стратегии» view exists would leave the user without the finalist Excel round
trip. Stage 1 therefore has two parts, and the selection screen goes in
stage 4:

Revised 2026-10-18 after mapping the cross-dependencies: the Source DB and
surfaces screens share helpers with the old fresh analysis, and A/B shares its
code with the selection screen, so they move out of 1a.

- **1a — nothing kept depends on it:** remove, with their routes, controller
  services, startup requests, JS, HTML and tests:
  - screener (SCREENER 01), RUNNER 01, RUNNER 02 and their BingX mode
    (`/api/v2/testing/*`, the screener package, `build_bingx_screening_sheet.py`,
    the screening tester templates `config_tester_*_screen.json`);
  - the analysis profile (`/api/v2/settings/analysis-profile`,
    `analysis_profile.py`);
  - the old single-page legacy panel: `/legacy`, `PANEL_HTML`, the
    «Default panel» setting and every non-`v2` `/api/...` route (`/api/start`,
    `/api/status`, `/api/browse`, DuckDB import/migration/direct build, Source v6
    library/import/merge/analysis, legacy analysis library/shortlist/strategies),
    with the controller methods, constructor injection points and job state
    only they used.

  Kept on purpose: `/api/artifact` (it serves the Performance v2 import failure
  report — now its only artifact); `panel_testing.LocalTestingService` (the
  portfolio stage 2 tester hand-off uses it) and its BingX tester settings
  (reused in stage 5); `panel_remote_testing` (the remote Source DB card uses it
  until 1b). A saved `panel.default_root` is still accepted in the config but
  no longer changes anything: `/` always serves the static Panel.
- **1b — the tick test reads READY from the store:** the «Test and Import»
  card takes a READY folder built by `ready_from_store` (its manifest adapted to
  what `SingleModeStrategyTest` expects); then the old fresh analysis and
  shortlist (`/api/v2/strategies/fresh/*`, `/api/v2/strategies/analysis/*`,
  cards 1–2), the Source DB screen with remote Source DB (`/api/v2/source/*`,
  `panel_source_db`, `panel_source_jobs`, `panel_remote_source_db`,
  `panel_remote_testing`) and the surfaces screen (`/api/v2/surfaces/*`,
  `panel_surfaces`) are removed.
- **Stage 4:** A/B Performance analysis
  (`/api/v2/strategies/performance-v2/windows`) goes together with the
  selection screen once the «Стратегии» view replaces it.

  Modules are deleted only when nothing kept imports them (checked by an
  import scan and the full test suite); otherwise only their Panel wiring goes.
- **Shell:** the navigation becomes «Пары и стратегии» / «Портфели» /
  «Настройки». Until stages 3–4, «Пары и стратегии» holds the kept cards
  (MRS2 STORE, Test and Import, the PerformanceDB cards) as they are.

Acceptance: the kept functions work as before (their tests pass); removed
routes answer 404; the Panel start no longer requests removed services; the
full suite has no new failures against the known baseline.

## Stage 1b contract: the MRS3 tick test from a store READY folder

**Goal.** The «Test and Import» card runs the SINGLE_MODE tick test on the
strategies of a READY folder built from the trade store
(`<trade store>/ready/<name>/`, written by `ready_from_store`). With this
path working, the old fresh analysis and shortlist can be removed. Only after
that are the Source DB and surfaces screens removed.

**Non-goals.**
- No change to the tester run, the report collection or the Performance v2
  import.
- No new READY rule: `ready-v4-store-1` is unchanged.
- No BingX: that is stage 5.
- Finalist retest keeps working on what PerformanceDB already stores.

**Why an adapter.** Everything after the launch reads only the generation
manifest that `validate_strategy_manifest` accepts:
- `format_version 1`, `analysis_run_id`, `event_mode`;
- the signed hash, the strategy JSON hashes and the count;
- `candidate_identity_to_strategy_names` and `candidate_diagnostics` (one
  entry per order: `order_id`, `plateau_id`, `plateau_point_count`,
  `base_point_trades`, `plateau_total_trades`).

Nothing downstream reads surface or shortlist fields. The strategies
themselves must meet protocol v2 for the active PerformanceDB.

**Inputs and outputs.**
- `build_ready` additionally records three zone facts for every order of a
  structure, all from the newest period:
  - `zone_points`: the 26-neighbour cube that the score uses;
  - `trades`: the trades of the order's own point;
  - `zone_trades`: the trades of the point plus its neighbours that are
    present.

  The change is additive. `inputs_sha256`, the `ready_id` and the selection
  stay the same.
- **New module `store_ready_tick`.**
  - `list_ready_folders(store_root)`: the READY folders that have a
    `ready_manifest.json`, each with its name, rule version, exchange,
    pair/READY/strategy counts and whether it can be tested.
  - `prepare_tick_batch(ready_dir, out_root, template, evaluation)`:
    1. Re-renders every admitted structure into an EQUAL MRS3 strategy, with
       the same code `build_ready` uses and the template of the active
       PerformanceDB. Strategy names are the same as in the READY folder.
    2. Applies protocol v2 to each strategy.
    3. Writes `<out_root>/<analysis_run_id>/strategies/*.json` and
       `strategy_manifest.json` atomically.
  - `analysis_run_id` is the SHA-256 of the READY manifest bytes, the template
    bytes and the target base/exchange. Preparing the same folder for the same
    target again reuses the existing batch.
  - The candidate identity is `<exchange>/<pair>/<timeframe>/<structure_id>`.
  - Per order, the diagnostics are:
    - `plateau_id = zone:<pair>:<point_id>`: the pair is included because a
      point id does not name the pair, and the import keys plateaus by
      (`analysis_run_id`, `plateau_id`);
    - `plateau_point_count = zone_points + 1`;
    - `base_point_trades = trades`;
    - `plateau_total_trades = zone_trades`.

    These are honest zone facts, not shortlist plateaus. Only the Pareto
    selection stages read them, and those are retired in stage 4.
  - A folder without zone facts, written before this change, is refused:
    «пересоберите READY».
- **Panel.**
  - `GET /api/v2/strategies/store-ready/catalog` lists the folders of the
    configured `trade_store.root`.
  - `POST /api/v2/strategies/store-ready/prepare {"ready": "<folder name>"}`
    prepares the batch into `Output/store-ready/` and returns
    `analysis_run_id`, the strategy count and the pair counts.
  - The existing `strategies.tester.start` runs it unchanged.
  - `_fresh_strategy_manifest` also finds batches under `Output/store-ready/`,
    so the card restores the last batch after a restart.
- **UI.** Card 3 gets a «READY из хранилища» picker and a «Подготовить»
  button, which sets the batch the tester starts.

**Invariants.**
- A folder name is a plain child of `<store>/ready/`: no path from the
  client, no symlinks.
- The prepared files never overwrite another batch.
- A refused strategy (protocol v2) fails the whole preparation, and nothing
  is published.

**Acceptance evidence.**
- Unit tests:
  - zone facts;
  - re-rendered names equal the READY names;
  - the manifest passes `validate_strategy_manifest` and the tick-test
    diagnostics check;
  - protocol v2 is applied;
  - a repeat run is idempotent;
  - old folders and unsafe names are refused.
- Panel route tests.
- A run on the real store and the real tester for a few strategies, then the
  import into PerformanceDB.

## Stage 1b evidence, part B: retired screens removed and three-screen shell (2026-10-18)

**Removed and disconnected**
- Routes:
  - `/api/v2/source/*` (local and remote Source DB);
  - `/api/v2/surfaces/*`;
  - `/api/v2/strategies/fresh/*` except `/batch`, which the card uses to
    restore the last batch;
  - `/api/v2/strategies/analysis/catalog`.
- Controller methods, constructor parameters and job state that only those
  routes used. Among them: the remote Source DB start-up reconciliation and
  the fresh-generation busy flag of `has_active_panel_jobs`.
- Modules `panel_source_db`, `panel_source_jobs`, `panel_remote_source_db`,
  `panel_remote_testing` and `panel_surfaces`, with their tests.
- UI:
  - the Source DB and surfaces screens;
  - cards 1–2 of the strategies screen (fresh analysis, shortlist,
    «Generate READY JSON»);
  - their JS: the fresh-shortlist state helpers, the shortlist renderer, the
    source and surface workflows and the source path defaults.
- The analysis modules (`fresh_shortlist`, `fresh_analysis_strategies`,
  `source_v6_*`) stay, because CLI tools and other modules still import
  them. Only their Panel wiring is gone.

**Shell.** The navigation is «Пары и стратегии» / «Портфели» / «Настройки».
«Пары и стратегии» (`#strategies-dd5`, the default screen) now holds:
- the MRS2 STORE card, moved from the retired «Тестирование» screen;
- the «Test and Import» card with the READY picker;
- the PerformanceDB cards, as they were.

Retired hashes (`#testing`, `#source-db`, `#surfaces`) fall back to the first
screen.

**Tests.** The tests of the removed features were deleted. Mixed tests were
updated:
- navigation;
- card 3 action rows;
- JSON request helpers;
- protocol v2 flows;
- the store card placement;
- the portfolio start-up order.

`test_root_serves_the_static_panel_and_legacy_routes_are_gone` now also
checks that the part B routes answer 404 (GET and POST). Panel suites: only
the two baseline failures and the known flaky portfolio stage-2 test remain.

## Stage 1b evidence, part A: the tick test from a store READY folder (2026-10-18)

**Code**
- `ready_store.zone_facts` and the per-order zone facts in `best_structures`.
- `store_ready_tick` (catalog, folder resolution, batch preparation).
- Panel routes `GET /api/v2/strategies/store-ready/catalog` and
  `POST /api/v2/strategies/store-ready/prepare`, plus the
  `Output/store-ready/` restore in `_fresh_strategy_manifest`.
- Card 3 picker.

**Tests**
- `tests/test_store_ready_tick.py`, `tests/test_panel_store_ready.py`.
- New cases in `tests/test_ready_store.py` and
  `tests/test_panel_static_ui.py`.

**Real run.** The chain ran through the Panel controller:
1. A READY folder from the real trade store: XLFUSDT and LLYUSDT, giving
   7 strategies of XLFUSDT.
2. Preparation of the batch.
3. The real SINGLE_MODE tick test over 2026-09-17…2026-10-17: 7/7 reports,
   `COMMITTED`.
4. Inbox verification.
5. Performance v2 import into a **temporary** protocol v2 PerformanceDB:
   `COMMITTED`, 7 strategies, 14 orders, 14 plateaus with
   `plateau_id = zone:XLFUSDT:<point>`.

The tester files were byte-identical before and after.

**Found and fixed in the same part**
- **Data loss in the tick-test service.** `_clear_strategy_directory` emptied
  the whole tester strategy folder, but capture/restore saves only root
  `*.json` files. A user's file (`ADA.json111`) was deleted and never
  restored; it was put back from a backup taken before the run.
  - Clearing now removes only root `*.json` files and refuses a JSON-named
    directory or link.
  - Tests: `test_strategy_cleanup_removes_only_root_json_files` and
    `test_strategy_cleanup_refuses_a_json_named_directory`.
  - The second real run kept the file.
- **Local configuration** (not in the repository). `tester_runner.strategy_dir`
  still named the legacy `settings_strategy`. The current bot reads only
  `tester/settings_strategy` (user, 2026-10-18), so the first real run tested
  a stale strategy and stalled. The local config now names the current folder.
  The validator still accepts the legacy folder; refusing it is a separate
  change, because many runner tests use it.

**Independent review:** `CODE_REVIEW_PASS` after fixes. Five minor findings
were fixed and are covered by tests:
1. A damaged READY folder is flagged and does not break the catalog.
2. Preparation runs under a lock; `OSError` answers 409 and a self-check
   failure answers 500.
3. The early POST routes, including `store/start|stop`, require
   `application/json` (415 otherwise).
4. A late batch restore can no longer overwrite a batch just prepared.
5. `ADAPTER_VERSION` carries a note to bump it when the rendering code changes.

Two non-blocking notes stay open:
- order field types are checked only as keys;
- an `OSError` outside the batch folder is also reported as «files busy».

**Open.** The active PerformanceDB of this machine is protocol v1, so the
Panel refuses every MRS3 tick test with `DATABASE_PROTOCOL_V1` until a
protocol v2 database is created and activated (Settings, PerformanceDB). This
is not caused by stage 1b.

## Stage 1a evidence (2026-10-18)

- **Static checks:** `import mrs3.panel` works; `node --check
  src/mrs3/panel_web/app.js` passes; `git diff --check` is clean. Every one
  of the 36 endpoints in the `do_POST` allow-list has its own handler.
  Without the legacy `/api/start` fallback, an unknown endpoint now gets 404.
- **New test:** `tests/test_panel.py::test_root_serves_the_static_panel_and_legacy_routes_are_gone`
  covers two things:
  - `/` serves `panel_web/index.html`, including when `panel.default_root =
    "legacy"`;
  - the removed GET and POST routes (`/legacy`, legacy `/api/*`,
    `/api/v2/testing/*`, analysis profile) answer 404.
- **Full suite** (`.venv`, drive C): 54 failed / 6,516 passed / 12 skipped.
  - All 42 baseline failures are still among them.
  - 9 failures were tests of removed behaviour that the review found. They
    were fixed (deleted, or turned into "is absent" assertions).
  - The new route test failed once from a socket race. It now opens one
    connection per request and sends POST without a body.
  - `test_panel_portfolio.py::test_stage2_rejects_ambiguous_or_non_file_report[fake_kwargs0]`
    and `test_stage2_waits_when_wizard_result_precedes_report` are timing-flaky
    on HEAD as well (2 of 6 HEAD runs failed). They use only the untouched
    portfolio service.
- **After the fixes:** the focused rerun covered:
  - all `tests/test_panel*.py`;
  - `test_fresh_analysis_strategies`, `test_performance_v2_import`,
    `test_performance_v2_selection_review`;
  - `tests/runner/test_inbox.py`, `test_debian_import_bundle`.

  Result: 3 failed / 1,640 passed / 7 skipped. The failures are the two
  baseline panel failures and the flaky portfolio test.
- **Independent review:** the first pass asked for changes (the 9 tests above
  and staging the two new specs in the same commit). The re-review returned
  `CODE_REVIEW_PASS`.

## Order of work

1. Remove the obsolete screens and their backend (1a, 1b above); three-screen
   shell. ✅ 1a, 1b part A, 1b part B and the shell done (2026-10-18, reviewed).
2. ⏳ Registry stages 1–3 ([pair registry](2026-10-18-pair-registry.md)).
3. ⏳ «Пары и стратегии»: exchange switch, actions with plan dialogs, task area
   with progress/ETA/errors, views, pair card.
4. ⏳ «Стратегии» view: tick-test metrics, column chooser, filter toggles, ranks,
   finalist marks, Excel round trip, finalist retest.
5. ⏳ BingX in the trade store, READY, tester sampling and tick tests.

Each stage: spec details first, TDD, `.venv` tests on drive C, independent
review. Changes stay uncommitted until the whole rework is done, then one
commit (the user's standing rule for multi-stage features), unless the user
asks for a commit earlier.
