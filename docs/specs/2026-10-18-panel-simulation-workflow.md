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

## Stage 3 contract: the «Пары и стратегии» main screen (2026-10-19)

**Task area: `panel_tasks.TaskRunner`.** One task at a time, built from
real facts reported by the child process.
- JSON lines: `progress` (done, total, step, pair), `error` (pair, step,
  message); other objects are results; other lines are the log.
- Status: state, progress, elapsed time, ETA from the rate so far, errors as
  they happen, results, log. Local paths are redacted.
- «Стоп»: a tester run stops cooperatively through its stop file, after the
  current job and with the tester's files restored. Any other task kills the
  whole process tree, held in a Windows job object.
- The defect: the task finishes when the main process exits, even if
  orphaned workers still hold the output pipe. They are ended through the
  job object, and the pipe is never closed under the reader.
- The MRS2 STORE card, its routes `/api/v2/store/*` and `panel_store` are
  removed: its functions are the actions below. The history and READY
  scripts emit `progress` lines; a chain script (`scripts/panel_chain.py`)
  runs the steps of one action and honours the stop file.
- A running task blocks the panel restart (`has_active_panel_jobs`).

**Actions** (bar at the top, for the selected exchange):
- «Синхронизировать»: registry refresh, then liquidity, then pipeline. No plan
  dialog. The git sync is added in registry stage 5.
- «Пересчитать историю»: the plan lists one row per pair:
  - «новая пара: N периодов»: a pair with no store segments and enough days;
  - «продлить последнее окно / N новых периодов»: planned P2 segments (from
    the listing day, `trade_history.pair_segments`) that the store lacks;
  - «без изменений».
  Rows with work are pre-checked. The run takes the checked pairs.
- «Пересобрать READY»: the plan row says «READY ещё не собирался», or
  «появились новые периоды» when the pair's active segments differ from its
  latest READY catalog inputs, else «без изменений». The run is
  `ready_from_store --latest-source auto` with the checked pairs, then the
  pipeline.
- `latest_source="auto"` (`ready_store.pair_periods`): the newest period is
  the imported tester segment of the same span when it exists, otherwise the
  simulator segment. The entry records `newest_source`, the plan uses the
  same periods for its inputs hash, and the «Пары» view shows it as
  «Последний период».
- «Проверить тестером»: plan / run / import of `tester_jobs` for the checked
  pairs (sampling rule `select-v1` unchanged).
- «Тик-тест MRS3»: the plan lists the strategies of the newest READY folder.
  The run prepares a batch of the checked strategies (`prepare_tick_batch`
  with a strategy filter) and starts the existing SINGLE_MODE tick test.
- «Ретест финалистов»: the existing finalist retest, with an option
  «включая резерв».

**Plan dialog.** One row per pair or strategy, with what would be done, a
checkbox per row, «Выделить все» / «Снять выделение», and «Запустить».

**Views** (switch buttons). Each is a table with:
- sorting by column;
- a text filter;
- a column chooser, saved on the server in `pair_registry/view_settings.json`;
- «Excel» export of exactly the rows and columns shown, generated on the
  server with openpyxl, without changing any data.

The views:
- **Пары**: latest pipeline rows — stage, periods, READY, tester, tick tests,
  finalists, decision, history sufficiency, track record, base lot from
  `read_liquidity`;
- **Справочник**: reference, current risk tiers and maximum leverage;
- **Ликвидность**: the latest liquidity row of each pair;
- **Изменения**: `pair_changes`, newest first.

The «Стратегии» view comes in stage 4.

**Pair card** (click on a pair): reference, periods, READY per TF, tests,
decisions with history, personal thresholds next to the common defaults. A
new decision and a threshold (or a reset) are entered here, through
`records.record_decision` and `record_setting`.

**Exchange switch** (Bybit / BingX, top of the screen): views and plans take
the exchange. Actions that need the BingX data layer show «BingX — этап 5»
and stay disabled until stage 5.

**Routes.**
- `GET /api/v2/main/view?name=…&exchange=…`
- `GET /api/v2/main/view.xlsx?…`
- `GET/POST /api/v2/main/view-settings`
- `GET /api/v2/main/pair?exchange=…&symbol=…`
- `POST /api/v2/main/decision`
- `POST /api/v2/main/setting`
- `POST /api/v2/main/plan {action, exchange}`
- `POST /api/v2/main/run {action, exchange, items, options}`
- `POST /api/v2/main/stop`
- `GET /api/v2/main/task`

POST routes require JSON.

**Acceptance.**
- Task runner tests with real processes: progress / ETA / errors, tree kill,
  the orphan-pipe defect, cooperative stop, a failing child.
- Plan builders on synthetic stores.
- Routes for views, Excel, settings, pair card, decision and setting entry,
  plan, run and stop.
- Static UI tests for the action bar, plan dialog, task area, views and pair
  card.
- A live check on the real registry and store: «Синхронизировать» and the
  READY plan.

## Stage 3 evidence: the «Пары и стратегии» main screen (2026-10-19)

**Code**
- `panel_tasks.TaskRunner`, `panel_main.MainScreenService`,
  `scripts/panel_chain.py`.
- `pair_registry` CLI `sync`, progress lines in `trade_history.run_history`
  and `ready_store.build_ready`.
- Strategy filter in `prepare_tick_batch`; `latest_source="auto"`.
- `/api/v2/main/*` routes in `panel.py`.
- UI: section `#main-screen` and dialogs `#main-plan` and `#main-pair` in
  `index.html`, the main-screen block and `mainScreenHelpers` in `app.js`.
- Removed: `panel_store.py`, `tests/test_panel_store.py`, the store card and
  `/api/v2/store/*`. The store-lock test moved to `tests/test_trade_store.py`.

**Review fixes** (independent review, first pass `CODE_REVIEW_FAIL`)
- The Panel reads the store catalog through `TradeStore.reader` and never
  opens it writable. Before this fix, a writable open was refused while
  another thread held a read-only connection.
- Every `/api/v2/main/*` failure answers JSON:
  - a busy DuckDB file → 409;
  - validation errors → 400, local paths redacted;
  - anything else → 500, logged.
- The chain ends after a step reports a cooperative stop
  (`tester_jobs run` deletes the stop file itself). Children run with UTF-8
  output. `--cleanup` deletes the steps file.
- The table and the Excel export share one rule for value text
  (JavaScript `String`) and for numbers (decimal strings only). A sort on a
  hidden column is ignored in both.
- Excel cells starting with «=» are written as text.
- The history plan says «пересчитать последние окна» when the P2 grid
  realigns stored windows.
- `simulate_history --pairs` takes the listing day from the registry first,
  the same source as the plan.
- READY folders from the Panel are named `<day>_ready_<time>`. The tick
  plan takes the newest testable folder by its manifest time, not by its
  name.
- `simulate_history` falls back to the workbook listing days, with a
  warning line, while a writer holds the registry.
- Second pass: `CODE_REVIEW_PASS`. Its two follow-ups (the newest folder by
  manifest time, the busy registry) are fixed.

**Tests** (`.venv`, basetemp on drive C)
- `test_panel_tasks` (7), `test_panel_chain` (5), `test_panel_main` (18,
  with the routes), `test_panel_static_ui` (5 new main-screen tests),
  `test_ready_store` (auto source), `test_trade_store` (reader),
  `test_pair_registry_records` (CLI sync).
- Related set: 300 passed, 1 failed. The failure is
  `test_panel.py::test_static_panel_shell_contains_only_navigation_contract`,
  which already fails on HEAD (the `previewtoken` identifiers predate this
  stage).

**Live check** (real registry and store, real `TaskRunner`)
- Views: Bybit 274 rows in each view; BingX reference 605 and changes 605.
- Plans:
  - history 274 rows, 254 checked;
  - READY 274 rows: 228 «READY ещё не собирался», 44 «мало периодов»,
    2 «без изменений»;
  - tester 242 rows;
  - tick 7 strategies of the newest testable READY folder;
  - BingX READY: «BingX — этап 5».
- «Синхронизировать»: done, exit code 0, no errors, progress and ETA by step,
  69 s.
  - Refresh: Bybit 274 pairs, BingX 610.
  - Liquidity: 242 READY / 32 UNAVAILABLE.
  - Pipeline: TICK 9 / READY 1 / HISTORY 220 / NEW 44.
- HTTP smoke on the real controller:
  - index with `#main-screen`, view, Excel, tick plan, pair card and task all
    return 200;
  - `/api/v2/store/status` returns 404.

## Stage 4 contract: the «Стратегии» view and finalist marks (2026-10-19)

Decision: [ADR-0080](../decisions/0080-finalist-marks-in-the-pair-registry.md).
The marks are stored only in the registry `finalists` table. Three parts, each
committed after review:

- **4a:** the view, marks in the registry, and the Excel round trip.
- **4b:** the finalist retest and the optimizer read the registry marks.
- **4c:** the A/B card and the Pareto/filters screen are removed.

**4a. Rows** (`panel_strategies.strategy_rows`) are the union of:
- the READY structures of the latest READY catalog row of every pair of the
  exchange (pair, TF, orders, score, training DD, category, READY rank);
- the ACTIVE PerformanceDB strategies of that exchange (`database_exchange`).

They are joined by strategy name: names are stable because
`prepare_tick_batch` refuses a renamed strategy. A READY row without a test
shows empty metrics; a tested strategy outside READY shows empty READY
fields.

**Metrics** are read-only:
- the strategy result: PnL %, MaxDD %, trades, report window;
- the cached selection metrics of `load_selection_candidates(cache_only=True)`:
  PnL/30, history days, top-5 share, B PnL/30 and the others;
- every column of `strategy_report_metrics` when the table exists (Ulcer,
  time under water, Sharpe, …), labelled from the report profile. A new
  report column appears in the view without code changes.

Missing cache means empty metrics. «Посчитать метрики» runs the existing
cache preparation (`/api/v2/strategies/performance-v2/recalculate-all`).

**Filter toggles** are computed on the fly and never stored:
- «Жёсткие отсечки»: hard cutoffs
  ([filter sequence §3](2026-10-01-performance-v2-filter-sequence.md));
- «Топ-5 > 80 %»: §5.

A row cut by an enabled toggle shows the triggered rules and values in
«Причина»; «скрыть отсечённые» hides it. Both toggles are on by default, and
hiding is off.

**Rank in pair:** category (low → moderate → high), then score descending,
then the lower Ulcer of the strategy's tick test (empty last), then the name.
Only rows with a category are ranked.

**Marks.** A status (FINALIST / RESERVE / REJECTED / empty) and a user rank
are set in the table. `POST /api/v2/main/finalist` writes them with
`records.record_finalist(source="panel")`. The «Стратегии» view reads the
marks in force from the registry.

**Excel round trip:**
- «Excel» exports exactly the rows and columns shown (the same filter and
  sort), with the strategy name as the stable id. It writes nothing.
- «Загрузить отметки» is `POST /api/v2/main/finalists.xlsx` with the
  workbook body:
  - rows are matched by the «Стратегия» column; the «Отметка» and «Ранг»
    columns are read;
  - only changes are written, with `source="excel"`;
  - the answer lists changed, unchanged and unknown strategies and per-row
    errors.

**Pipeline.** `import_finalists` runs only while the registry holds no
finalist row of the exchange: this is the one-time migration of the old
PerformanceDB marks.

**Routes.**
- `GET /api/v2/main/view?name=strategies&exchange=…&hard=1&top5=1&hide=0`
- `GET /api/v2/main/view.xlsx?name=strategies&…`
- `POST /api/v2/main/finalist {exchange, strategy, symbol, side, status, user_rank}`
- `POST /api/v2/main/finalists.xlsx` (body: the workbook)

The column choice is saved like the other views.

**4b. Consumers of the marks.** With a registry, the marks in force (status
other than NONE) are matched by strategy name to the ACTIVE strategies of the
PerformanceDB.
- Finalist retest: `freeze_finalist_cohort(decisions=…)` and
  `current_effective_finalist_members(decisions=…)` take strategy_id →
  (status, rank, None) from `PanelController._registry_finalist_decisions`
  for the start, the plan and the control export.
- Optimizer: `read_current_finalists(marks=…)` takes the FINALIST set from
  the registry.
  - The provenance is the mark row: `source="pair_registry"`, `mark_seq`,
    `mark_source`, `mark_created_ms`, plus the strategy and result fields.
    `selection_run_id` and `review_import_id` are empty.
  - The finalist pairs come from the FINALIST marks.
- The retest manifest freezes its cohort with the same decisions as the
  cohort check before it.
- The registry decides once it holds any finalist row of the exchange
  (`records.registry_decides`). Before that, and without a registry, both
  consumers read the PerformanceDB selection reviews exactly as before (the
  same calls).
- A busy registry gives `REGISTRY_BUSY` (retest) or
  `PORTFOLIO_FINALISTS_UNAVAILABLE` (409, optimizer). An unreadable one gives
  `REGISTRY_UNAVAILABLE` (retest) or 422 (optimizer).
- While the registry decides, the old imports that set user decisions are
  refused with 409 `MARKS_IN_REGISTRY`:
  - selection review;
  - user fields;
  - the retest control workbook;
  - aggregate.
  The retest tags import stays. The A/B and Pareto cards themselves are
  removed in 4c.
- The retest outcome only moves each strategy to its new result; it holds no
  decision. ADR-0080 lists a `retest` writer, but none is needed: after a
  retest the marks are revised in the «Стратегии» view.

**Acceptance (4a).**
- Row union and join by name; metrics from a fixture PerformanceDB.
- Toggles and «Причина» use the selection module's own rule functions.
- Rank in pair; marks written and read back; the Excel export has no
  PerformanceDB writes.
- Round trip: changed, unchanged, unknown and invalid rows.
- One-time migration only.
- Static UI tests.
- A live read of the real registry, store and PerformanceDB.

## Stage 4c contract and evidence: the A/B and Pareto screens removed (2026-10-19)

**Removed and disconnected.** Each item below lost its UI, its routes, its
controller code and its tests.
- **A/B Performance analysis card:**
  - `POST /api/v2/strategies/performance-v2/windows`;
  - `GET …/catalog`;
  - `panel_performance_v2.calculate_performance_v2_windows` and
    `performance_v2_catalog`, with their helpers.
- **Pareto/filters card**, including the legacy filters 11–27:
  - `…/selection`, `…/selection/preview`, `…/selection/export`,
    `…/selection/write-rejected`, `…/selection-preview`,
    `…/selection-cache-status`, the single-pair `…/recalculate`,
    `…/selection-review-import`, `…/selection/aggregate-import`;
  - the aggregate controller helpers.
- **Filters 6–9 settings** on «Настройки» and `…/selection-settings`.
- **«Восстановить User Status и User Rank из selection XLSX»** in the
  finalist retest card and `…/selection-user-fields-import`. This import read
  Pareto workbooks, and since ADR-0080 the marks are set in «Стратегии».
- **Export card** and `GET/HEAD/OPTIONS …/export`, with
  `panel_performance_v2.export_performance_v2` and its helpers. The
  replacement is the «Excel» of «Стратегии».

**Moved.** The PerformanceDB maintenance card is now on «Настройки», after
«PerformanceDB и биржа».

**HTTP.** With the export route gone:
- HEAD and OPTIONS answer 404 on every path (403 without a local Host);
- OPTIONS sends the JSON body, HEAD only the headers;
- every removed route answers 404 — tested.

**Kept.** The two retest cards, renumbered 4 and 5:
- «4. Bulk RETEST current FINALIST», for the start status, IMPORT & REPLACE,
  the control workbook and the equity filter;
- «5. CHECK & RETEST», for the RETEST tags.

The retest import step is still only there. `…/recalculate-all` and its
progress stay; they are the «Посчитать метрики» of «Стратегии».

**Not retired.** The domain modules `performance_v2_selection` and
`performance_v2_selection_review` stay whole. The «Стратегии» view, the
retest and the control workbook use parts of them; their now-unused
functions (Pareto workbook writers and importers) are left for history and a
later cleanup.

**Tests.**
- Deleted because they test removed functionality (four checks of kept
  behaviour were ported, see the next items):
  - 69 tests in `test_panel_performance_v2.py`;
  - 20 static UI tests;
  - 1 settings test;
  - `test_panel_performance_v2_export.py` and
    `test_performance_v2_equity_regime_export.py`.
- Adapted to `recalculate-all`: the control-schema test (no longer compared
  with the Pareto workbook), the v8 migration test and the two control-export
  retest tests.
- Ported from the single-pair recalculate to `recalculate-all` (review of
  4c), because they covered kept behaviour:
  - the writer lock is held while the cache is prepared;
  - the real window workers run under the controller lock;
  - equity cache errors map to typed API errors;
  - only added or replaced current results are recalculated.
- A route test asserts 404 for every removed route and method.
- Independent review: `CODE_REVIEW_PASS`; its MINOR findings were fixed (the
  ported tests, README, PRD rows, the route test, the dead export helper,
  OPTIONS with a body, unused names) and the re-review returned
  `CODE_REVIEW_PASS`.
- New static assertions: the card layout, maintenance on «Настройки», and no
  route of a removed screen in `app.js`.
- Runs:
  - all Panel tests plus `test_performance_v2_selection_review`: 1,126 passed,
    3 failed. All three already fail on HEAD:
    the `previewtoken` shell test, the journal retry constant, and the flaky
    stage 2 test.
  - PerformanceDB, single-mode, registry and main-screen suites: see
    progress.

## Stage 4b evidence: retest and optimizer read the registry marks (2026-10-19)

**Code:**
- `records.finalist_marks`;
- the `decisions` parameter of `freeze_finalist_cohort` and
  `current_effective_finalist_members`;
- `portfolio.input`: `read_current_finalists(marks=…)`, `_registry_facts`,
  `registry_finalist_provenance`;
- `PanelController._registry_finalist_decisions` at every freeze call and
  the control export;
- `PortfolioPanelService`: `_registry_marks`, `_invoke_finalists`,
  `_finalist_pairs`.

**Review fixes.** The first review returned `CODE_REVIEW_FAIL`. The BLOCKER
was that the manifest re-froze the cohort without the decisions. Fixed, and
also:
- the guard on the decision imports;
- the reviews decide until the registry holds a mark of the exchange;
- busy and unreadable registry errors kept apart;
- one marks read per readiness check;
- no origin-run lookups for registry members.

The re-review returned `CODE_REVIEW_PASS`.

**Tests:**
- `tests/test_finalists_from_registry.py` (10): marks with provenance, the
  retest cohort from decisions with the review fallback, optimizer rows from
  marks, the provenance contract, and the Panel retest and optimizer wiring
  with and without a registry.
- Review tests: the manifest gets the same decisions; the reviews decide
  until a mark of the exchange exists (another exchange's mark does not
  count); a busy registry; the refused imports; one marks read.
- The retest, optimizer-input, portfolio Panel, registry and «Стратегии»
  suites have no new failures against HEAD:
  - the 14 `test_portfolio_input` failures already fail on HEAD (the
    `selection_review_imports` schema);
  - `test_stage2_rejects_ambiguous_or_non_file_report` is the known flaky
    test.

**Live** (read only): the registry holds no marks, and the PerformanceDB has
no selection reviews and no effective decisions. So nothing changes for the
current data.

## Stage 4a evidence: the «Стратегии» view and marks in the registry (2026-10-19)

**Code**
- `panel_strategies` builds the rows, metrics, toggles, rank and mark parsing.
  The PerformanceDB metrics are reused while the file and its WAL are
  unchanged.
- `performance_v2_selection.top5_evidence` and `hard_cutoff_evidence` are
  shared by the selection stage and the view; the selection suite has
  423 passed.
- `panel_main`: the `strategies` view, `finalist`, `import_marks`.
- Panel routes:
  - view options `hard` / `top5` / `hide`;
  - `POST /api/v2/main/finalist`;
  - `POST /api/v2/main/finalists.xlsx`, xlsx only (415 otherwise),
    20 MB at most.
- Pipeline: the one-time seed of the marks.
- UI:
  - a «Стратегии» view button;
  - toggles;
  - «Посчитать метрики» (the existing cache preparation, with progress);
  - «Загрузить отметки»;
  - a status select and a rank input in the table.

**Tests.**
- `tests/test_panel_strategies.py` (7):
  - row union and name join, report metrics, rank in pair;
  - toggles and «Причина»;
  - marks;
  - the Excel round trip and no PerformanceDB writes on export;
  - the one-time seed, including a Panel mark set before the first sync;
  - routes.
- One new static UI test.

**Review.** The independent review returned `CODE_REVIEW_PASS` with MINOR
findings, all fixed:
- the registry is read on a short connection, never during the slow
  PerformanceDB read;
- a repeated strategy row in the workbook is a row error;
- `finalist()` takes the pair and side from the view and refuses unknown
  strategies; clearing a strategy without a mark writes nothing;
- the seed runs until PerformanceDB marks have been imported once, and never
  overrides a registry mark;
- the mark message is no longer overwritten;
- NaN-safe rank in pair; report metrics joined through a dict; one `stat()`
  per file; the light read is not cached;
- the top-5 label follows the config;
- a pointer to ADR-0080 in the registry spec.

**Live** (read only): the real registry, store and PerformanceDB give 1,812
rows: 1,805 tested strategies plus 7 READY structures.
- No window cache is prepared in this database, so PnL/30 is empty until
  «Посчитать метрики» runs. The top-5 rule is evaluated now and cuts 4 rows.
- The first read takes 6.6 s; a repeat read takes 0.3 s.
- The Excel export returns 200.

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

## Stage 5 plan: BingX (2026-10-19)

Stage 5 is split so each part is reviewed and committed on its own:
- **5a** — the minute data layer: `ensure_minute_days` for both exchanges,
  the BingX minute verification and the registry table `minute_days`
  (contract below);
- **5b** — BingX history in the trade store («Посчитать историю») and READY;
- **5c** — BingX MRS2 tester sampling and MRS3 tick tests (data ensured
  first, `UpdateData = false`, BingX templates and fees, report symbols
  mapped back);
- **5d** — BingX liquidity, and the BingX actions enabled in the Panel.

## Stage 5a contract: the minute data layer (2026-10-19)

The rules are in the pair registry spec: «Minute data layer (both exchanges)»
and «BingX minute verification». This contract fixes the code shape.

**Module** `mrs3.pair_registry.minute_days`.
- `ensure_minute_days(con, settings, exchange, symbol, first, last)` returns
  the accepted days, the refused days with a reason, and the records:
  - Bybit: `minute_data.ensure_days` on the Bybit data folder. A day is
    accepted when its file exists or was created; nothing is recorded.
  - BingX: the pair must be in `pairs` (exchange `bingx`). Its `api_symbol`
    and `listing_ms` locate the files:
    `<bingx data root>/<api symbol>/<human ticker><day>_1m.parquet`, the
    downloader's format.
  - Days before the listing day or after the last available day (lag 9 h)
    are out of the plan, neither accepted nor refused.
- `verify_bingx_days(...)` does the same for BingX without the registry. It
  takes the known records and returns the new ones, so a process-pool worker
  can verify and the main process writes the records.

**BingX, per day:**
1. **Reuse.** In `incremental` mode, a recorded `OK` / `VERIFIED_GAPS` day
   whose file hash is unchanged is accepted with no request. A day with no
   file counts as unchanged while the file is still absent.
2. **Download.**
   - A valid file on disk is verified as it is (`incremental`).
   - A missing or invalid file is downloaded (1m klines). In `overwrite`
     mode every day is downloaded again and re-verified.
   - The fetched minutes are only a candidate. The file changes only when the
     candidate is accepted, and is then written by the downloader's
     `write_day_file`, so a valid file is never replaced by a worse answer.
   - An empty answer never removes a valid file: the day is refused with
     «the file is kept». Only a damaged file is removed.
3. **Check.**
   - Expected minutes run from the listing minute (on the listing day) or
     00:00 to 23:59 UTC.
   - Every expected minute present → `OK`.
   - Otherwise one hourly klines request for the day:
     - every hour equals the aggregate of its minutes (open of the first,
       close of the last, max high, min low, sum of volume, at a relative
       tolerance of 1e-9 with no absolute floor) → `VERIFIED_GAPS`;
     - an hour with no minutes must have no candle or zero volume.
   - When an hour differs, the minutes are downloaded again (up to 3 times).
     If it still differs the day is `SUSPECT`.
4. **Recorded** in `minute_days` (local, never synced), one row per
   (exchange, symbol, day), replaced on each verification:
   - candles, expected / zero-volume / missing minutes;
   - gap ranges and differing hours, as JSON;
   - status, verification time, file sha256.
5. **Accepted:** `OK` and `VERIFIED_GAPS`.
   - These are refused with the reason, and the caller refuses the segment,
     test or job with `MISSING_DAYS`:
     - `SUSPECT`;
     - a failed request;
     - a malformed klines answer.
   - A failed request or a malformed answer refuses only its own day.
   - A `SUSPECT` day is verified again on the next call.

**Settings** (`pair_registry.minute_data`):
- `bingx_data_root`: default `<tester_runner.bot_root>/tester/data/bingx`.
  An empty value means not set.
- `bingx_download_mode`: `incremental` (default) or `overwrite`.
- Bybit downloads use the shared `duckdb_import.workers` limit (CLAUDE.md).

**Acceptance (offline, fake BingX client):**
- a complete day is `OK`;
- a day with gaps whose hours equal the minutes is `VERIFIED_GAPS`, with the
  gap ranges;
- a day without candles whose hours are empty is `VERIFIED_GAPS` with 0
  candles and no file;
- a differing hour is refetched, then `SUSPECT` and refused;
- reuse makes no request while the hash is unchanged; a changed file and
  `overwrite` re-verify;
- days outside the plan are skipped;
- a day already on disk is verified once;
- Bybit delegates to `ensure_days`;
- an unknown BingX pair is refused.

## Stage 5a evidence (2026-10-19)

- **Code:**
  - `pair_registry/minute_days.py`;
  - the `minute_days` table and the settings `bingx_data_root` /
    `bingx_download_mode` in `pair_registry/store.py`;
  - `BingXClient.klines(..., interval)`.
- **Tests:** `tests/test_minute_days.py`, 19 tests:
  - one per acceptance item;
  - a failed request is refused and not recorded;
  - `verify_bingx_days` works without the registry;
  - added after the independent review (`CODE_REVIEW_FAIL`: a MAJOR, an
    empty BingX answer removed a valid file):
    - an empty answer in `overwrite` keeps the file;
    - a worse refetch never replaces the file;
    - a refetch that heals the day is written;
    - the listing day;
    - an hour without minutes;
    - the relative tolerance;
    - a malformed answer refuses only its own day;
    - the settings.
- **Live, BingX API, 2026-10-19,** in a temporary registry and data folder
  (removed afterwards); AMD and KOPN, 2026-10-14…17:
  - AMD: all four days `OK` with 1,440 candles;
  - KOPN: 10-14 and 10-17 `OK`;
  - KOPN 10-15: `VERIFIED_GAPS`, 11 missing minutes `22:52-23:02`, the
    exchange pause the registry spec recorded;
  - KOPN 10-16: `VERIFIED_GAPS`, `00:08-00:21`.
  - Every real hourly candle equalled the aggregate of its minutes.

## Order of work

1. Remove the obsolete screens and their backend (1a, 1b above); three-screen
   shell. ✅ 1a, 1b part A, 1b part B and the shell done (2026-10-18, reviewed).
2. ✅ Registry stages 1–3 ([pair registry](2026-10-18-pair-registry.md)).
3. ✅ «Пары и стратегии»: exchange switch, actions with plan dialogs, task area
   with progress/ETA/errors, views, pair card (2026-10-19, see «Stage 3
   evidence»).
4. ✅ «Стратегии» view: tick-test metrics, column chooser, filter toggles, ranks,
   finalist marks, Excel round trip, finalist retest (4a, 4b); A/B and the
   Pareto screen removed (4c) — 2026-10-19.
5. ⏳ BingX in the trade store, READY, tester sampling and tick tests (see
   «Stage 5 plan»): ✅ 5a, the minute data layer (2026-10-19); 5b–5d
   pending.

**Deferred (user, 2026-10-19; not urgent).**
- Retire the base-lot Excel export (`scripts/calculate_bybit_base_lots.py`,
  the Actual sheet) with its README and spec mentions. Nothing else reads the
  sheet: the optimizer and the «Ликвидность» view read the registry.
- Let `loader.load_listing_dates` read the listing days from the registry
  and drop the `pairs.xlsx` bridge. This changes the listing-date source
  recorded by the PerformanceDB import, so it is a stage of its own.

Each stage: spec details first, TDD, `.venv` tests on drive C, independent
review, then one commit per stage (the user asked for per-stage commits on
2026-10-18; no push until asked).
