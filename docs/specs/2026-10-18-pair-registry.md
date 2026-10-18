# Pair registry: one source of truth per exchange pair

**Status:** Draft for user approval (2026-10-18), revision 3 after the user's
review (sync scope, weekday liquidity with a configurable window, optimizer
reads computed liquidity only, button instead of a scheduler, automatic new
listings, per-pair filter settings). No implementation yet.
**Replaces (when done):** the hand-maintained `bybit_tradfi_liquidity.xlsx`
(sheets «Пары», «По дням», «Оборот 30д», «Скрининг», «Скрининг BingX») and the
`Actual` sheet of the base-lot export as sources of truth.
**Related:** [Bybit market-data collector](2026-09-05-bybit-market-data-collector.md)
(reference data normalizers reused), [liquidity lot model](2026-09-27-liquidity-lot-model.md),
[base-lot export](2026-10-07-bybit-base-lot-export.md), [MRS2 trade store](2026-10-17-mrs2-history-store-and-tester-sampling.md)
(history periods, READY, tester jobs).

## Problem

Facts about a pair are spread over one workbook with seven sheets plus other
files, and are partly typed by hand:
- reference facts (listing day, type) sit next to one-off liquidity snapshots
  that every refresh overwrites, so no history accumulates;
- the «Скрининг» sheet describes the retired pipeline (screen → test → analysis
  → PerformanceDB) with free-form values (`+`, `−`, `НЕТ`, `ЕСТЬ`, `нет ЭЦ`,
  numbers in the same column), 10 unnamed columns, and a copy sheet for BingX;
- leverage limits, margin, risk tiers and order size limits are collected by
  `bybit_collector` for a handful of symbols only and are not visible anywhere
  as a per-pair reference;
- 14 modules read or write the workbook (inventory below), so its layout is a
  hidden data contract.

## Goal

One registry per (exchange, pair):
- facts are fetched automatically (exchange APIs, minute data, trade store,
  PerformanceDB) and kept with history;
- only human decisions are typed by hand, in the Panel, with author time and
  reason, never overwritten;
- the Panel shows chosen views on demand and exports any view to Excel;
- one «Синхронизировать» button keeps it current (no scheduler — user decision
  2026-10-18);
- it syncs between machines through git;
- filters (READY today, others later) can take personal thresholds per pair.

## Non-goals (this spec)

- News/calendar alerts, Telegram notifications and automatic trading actions
  (pause before news, `max_balance` changes when liquidity drops). The schema
  leaves a place for them («Future hooks»); they get their own spec.
- Migrating the old screening columns (Точек, BIG, READY, Plateu, Финалист,
  Стадия анализа) or the old liquidity snapshots — user decision 2026-10-18:
  not migrated. The old workbook stays as an archive file.
- Per-account fee tiers (Bybit `fee-rate` needs an API key); fees stay in the
  exchange templates for now.

## Storage and sync

- **Source of truth:** `registry.duckdb` on each machine (small: hundreds of
  pairs, daily rows). Location from config `pair_registry.root`.
- **Two kinds of tables:**
  - **local, refreshed from the exchange** — `pairs`, `risk_tiers`,
    `pair_changes`, `liquidity`, `minute_days`. Never synced: every machine fetches them itself
    when «Синхронизировать» is pressed.
  - **synced** — what only exists because of our own work on a pair:
    `decisions`, `pair_settings` (personal filter thresholds), `pair_tests`
    (tester samples and MRS3 tick tests per pair), `finalists` (FINALIST /
    RESERVE status and its history), and the `pipeline` snapshot (so a machine
    sees the state computed elsewhere). Both machines hold a trade store; the
    user moves its history between them by hand for now, so each `pipeline`
    row carries the machine name and its computation time, and the newer one
    wins.
- **Automatic upload:** every write to a synced table (a decision saved in the
  Panel, a tick test imported, a finalist status changed, a pipeline recompute)
  exports the synced tables and, if anything changed, commits and pushes to the
  sync repository. Nothing to remember.
- **«Синхронизировать» button** (Panel) and `registry sync` (CLI): pull the sync
  repository, import new rows into the local DuckDB, then run the exchange
  refresh (reference, risk tiers, new listings, minute data top-up, weekday
  liquidity). Press it before starting work on either machine.
- **Format in the sync repository:** one text file per synced table (UTF-8, LF,
  fixed column order, rows sorted by key). Decisions, tests and finalist
  changes are append-only rows, so edits made on two machines merge as plain
  line additions; `pipeline` is a snapshot keyed by (exchange, symbol, side,
  day), the newer day wins.
- **Where the sync repository lives: a separate private repository** (user
  decision 2026-10-18). The project repository is ruled out by CLAUDE.md (results and
  generated data are not committed there, and every data push would land in the
  code history and review flow). A password-protected zip would not merge: two
  machines changing it give a binary conflict, which defeats the automatic
  merge above. A separate private repo keeps text files that git merges line by
  line. Created once with `gh` (installed 2026-10-18; the user logs in with
  `gh auth login`).
- **Excel:** generated only (from a Panel view or the CLI), never read back.

## Tables

### 1. `pairs` — reference (one row per exchange, symbol; updated in place, changes logged; local)

Fetched from the exchange (Bybit `v5/market/instruments-info` linear and
`v5/market/risk-limit`, reusing the `bybit_collector.reference` normalizers;
BingX contracts endpoint for BingX):
- identity: exchange, symbol, display/base asset, symbol type (stock / ETF /
  commodity / crypto), contract type, settle coin, status (Trading / delisted),
  listing time, delivery time;
- exchange naming: `api_symbol` (the exchange's own symbol when it differs from
  the human ticker — BingX `NCSKPANW2USD-USDT` for `PANWUSDT`), display name;
- API trading state (BingX `apiStateOpen` / `apiStateClose`);
- observed trading regime per day (24/7 / exchange hours), from the verified
  minute data;
- price: tick size, min/max price, price scale;
- size: qty step, min/max order qty, max market order qty, min notional;
- leverage: min/max leverage, leverage step;
- funding: interval, upper/lower funding rate;
- first and last local minute-data day (tester data folder).

Every change of a reference field writes a `pair_changes` row (field, old,
new, time) — e.g. a tick size or max leverage change is visible later.

### 2. `risk_tiers` — risk limit tiers per pair (snapshot per refresh day; local)

exchange, symbol, tier id, risk limit value, initial margin, maintenance margin,
max leverage, MM deduction, as-of day. The latest snapshot is the current
reference; older snapshots stay.

### 3. `liquidity` — daily snapshots (one row per exchange, symbol, UTC day; local)

**Weekdays only, configurable window** (user decision 2026-10-18): every
liquidity figure is computed from UTC Monday–Friday only; Saturday and Sunday
are excluded. The window is the last **7 weekdays** by default, set in the
registry settings (`liquidity.window_weekdays`); changing it takes effect at the
next refresh. Today's
`LOT_MODEL_V1` (base-lot export, `minute_capacity._lot_model_capacity`) uses
seven consecutive calendar days including weekends, and the thin weekend
trading of stock perps pulls both `V25` and `A15` down. Weekday-only is
therefore a model change: it becomes `LOT_MODEL_V2` with its own ADR, and its
base lot will differ from the current export for the same day.

Per row (the day's facts):
- day turnover (USDT), day average minute turnover, average open interest
  (USDT), positive-turnover minute count, active 15-minute bins — weekend days
  are stored too but flagged and never used in a window.

Per row (the window ending that day, weekdays only):
- **optimizer inputs, stored raw** (user request: the optimizer computes the
  final strategy size for any number and depth of orders from the stored
  figures, without re-reading minute files):
  - window days used (list), positive minute count, active bins, total bins;
  - `V25` (Q25 of positive minute turnover), `A15` (active / total 15-minute
    bins), `L = V25 * A15`;
  - `K` and the USDT rounding step used, and the model version;
  - base lot `floor_to_step(K * L, step)` for one order of full weight.

  The optimizer applies the strategy geometry itself (liquidity lot model:
  `B = min_i(M(s_i) / W_i)`, `N_max = K * L * B`), so nothing order-specific is
  stored per pair.
- **Optimizer contract:** the optimizer (and the base-lot export) read the
  computed liquidity of a pair from the registry — `L`, `K`, the rounding step
  and the model version, through `mrs3.pair_registry` — and compute lot sizes
  from them. They never read minute files and do not know the window length:
  5, 7 or 10 weekdays is purely a registry setting, recorded on each row only as
  provenance.
- 30-day and window averages of turnover and OI (weekdays).

Trends (slope over the window) are computed views, not stored.

### 4. `pipeline` — computed state (one row per exchange, symbol, side, as-of day; synced snapshot)

Derived, never typed. From the trade store and its catalog:
- history: active simulator periods, first/last period day;
- READY now: best score/DD per TF, admitted structures, category, rule version;
- track record (walk-forward, as in `store_threshold_study.py`): cuts tested,
  times admitted, forward loss rate when admitted — flags pairs that fail at any
  threshold (e.g. GDX, INTC in the 2026-10-17 study);
- tester: latest window sampled / imported, agreement with the simulator;
- MRS3: tick-tested strategies and PerformanceDB User Status (FINALIST /
  RESERVE) — linked by pair, side and structure.

**Stage** (computed, first match wins):
EXCLUDED (latest decision EXCLUDE) → FINALIST → TICK (MRS3 tick test done) →
TESTER (latest window tester segment imported) → READY (admitted now) →
HISTORY (≥ 2 periods) → NEW (< 2 periods).

### 4b. `pair_tests` and `finalists` — synced

- `pair_tests`: one row per test of a pair (tester sample of a window, MRS3 tick
  test of a structure): exchange, symbol, side, kind, window, structure id,
  result summary (PnL %, MaxDD %, trades, agreement with the simulator), source
  (job id / PerformanceDB id), time. Append-only.
- `finalists`: FINALIST / RESERVE status changes per pair and strategy with time
  and source (PerformanceDB User Status import or Panel). Append-only.

### 4c. `pair_settings` — personal filter thresholds per pair (append-only; synced)

User request: a filter's thresholds can be set per pair (and side), e.g. a
lower or higher READY `admit_score` or `max_dd` for one pair; more filters can
use it later.
- Row: exchange, symbol, side (or all), filter, parameter, value, reason,
  author, time. A row with an empty value resets that parameter to the common
  default. The latest row per (exchange, symbol, side, filter, parameter) is in
  force; history stays.
- Filters and their parameters are declared in code with type and allowed
  range (today: `ready_v4` — `admit_score`, `max_dd`, `low_dd`, `moderate_dd`,
  `min_trades`, `min_shift_bp`, `pool`, `ratio`). An unknown filter or
  parameter, or an out-of-range value, is refused at entry.
- Effective value = the pair's override if set, else the common default
  (`ReadyRule()` today). READY builds each pair with its effective rule; the
  manifest and the catalog row record the effective parameters and which ones
  are overridden (the inputs hash already includes the rule parameters).
- Panel: a «Настройки пары» view with the common default next to the
  override, and «сбросить к общему».

### 5. `decisions` — the only hand-typed table (append-only; synced)

exchange, symbol, side (or all), decision (INCLUDE / EXCLUDE / WATCH), reason
(free text), author, time. The latest row per (exchange, symbol, side) is in
force; history is never edited or deleted. Entered in the Panel.

### Future hooks (schema reserved, behaviour in a later spec)

- `events`: scheduled pair events (earnings for stock perps, macro dates),
  source, time, importance.
- `actions`: proposed or automatic actions with states proposed → approved →
  applied / rejected: pause a strategy before an event, Telegram notice,
  change `max_balance` when the 10-day base lot falls below the live portfolio
  order size.

## Universe and new listings (automatic)

The user never adds pairs by hand. The exchange refresh behind the «Синхронизировать» button reads
the exchange's full instrument list and keeps every instrument that matches the
configured universe rules:
- Bybit: `linear`, `status = Trading`, `symbolType` in a configured set
  (today: stock, ETF, commodity — the current workbook universe), plus a
  configured exclude list (today: forex pairs, `SPXUSDT`, `XAUTUSDT`);
- BingX: its whole TradFi catalog — `symbol` prefixes in a configured set
  (today `NCSK` stocks/ETF, `NCSI` indices, `NCCO` commodities; `NCFX` forex
  and crypto excluded) with `status = 1`, plus an exclude list, rules in the
  same config (see «BingX names and data»); independent of what Bybit lists.

A matching instrument not yet in `pairs` is inserted with stage **NEW**, its
listing day and reference data, and a «new listing» row in `pair_changes`. An
instrument that leaves the list (or is no longer Trading) is marked DELISTED;
its history and decisions stay.

**History sufficiency** is shown for every pair (computed from the listing day
and the trade store rules: warm-up 4 days, 20-day P2 periods, READY needs two
periods):
- days of history so far;
- date from which the first full window exists (listing + 4 + 20 days) —
  research possible;
- date from which READY is possible (listing + 4 + 40 days);
- status: «мало истории (N дней)» → «можно исследовать» → «READY возможен».

New pairs show up in the «Пересчитать историю» plan as soon as they have enough
days (pre-checked), so a listing flows through NEW → HISTORY → READY with no
manual step beyond confirming the plan.

## BingX names and data

BingX is a full second exchange with its **own complete universe** (user
decision 2026-10-18): every BingX TradFi contract is worked on, whether or not
Bybit lists the same asset. There is no link between the exchanges' pairs.
Facts below were checked on the live API on 2026-10-18.

- **Catalog:** the public `/openApi/swap/v2/quote/contracts` (1,256 contracts).
  TradFi = `symbol` prefix `NCSK` (stocks and ETFs, 564), `NCSI` (indices, 16),
  `NCCO` (commodities, 25); `NCFX` forex (42) and crypto (609) are excluded.
  The prefix rule keeps a stock ticker from landing on a same-named coin
  (`ON` → `NCSKON2USD-USDT`, not crypto `ON-USDT`).
- **Status:** `status = 1` contracts trade; `status = 25` contracts do not trade
  at all (no candles even on a Friday) — stored as «торги приостановлены» and
  kept out of the working list. On 2026-10-18: 369 TradFi contracts with
  status 1.
- **`apiStateOpen = false`** (136 stocks on 2026-10-18, e.g. TSLL, SPCH): BingX
  does not accept opening orders through the API, so the bot cannot trade the
  pair. Stored and shown; READY and the optimizer flag such pairs.
- **Human ticker:** from the `symbol`, not from `displayName` (whose BingX-added
  `US` suffix — `AMDUS-USDT` — cannot be told from a real ticker ending in `US`,
  e.g. `FUS` = Ford `F`). Strip the prefix and `-USDT`, then the quote tail
  (`2USD`, `USDT`, or `2<currency>`): `NCSKAMD2USD-USDT` → `AMD`,
  `NCSKTMFUSDT-USDT` → `TMF`, `NCSKSOFTBANK2JPY-USDT` → `SOFTBANK`; the human
  ticker is base + `USDT` (`AMDUSDT`). 592 of 605 symbols follow the
  `…2USD-USDT` pattern. A collision inside the BingX TradFi set is refused at
  refresh (both rows listed) rather than renamed silently. `displayName`
  (`GOLD(XAU)-USDT`, `Nikkei 225-USDT`) is kept as the display name.
- **Reference fields** from the same record: launch time, price and quantity
  precision → tick and qty step, minimum quantity and notional, maker/taker
  fee rates, status, API open/close state.
- **The API symbol** (`NCSKPANW2USD-USDT`) is stored in `pairs.api_symbol` and
  used only where BingX or the tester needs it: data folder, tester config,
  strategy file, report symbol.
- **Trading regime:** active stocks trade 24/7 (1,440 candles on Friday and
  Saturday for TSLA, MSFT, PANW, XLP, BAC…), but BingX moves contracts to 24/7
  gradually: NU had 391 candles (13:00–20:59 UTC, US exchange hours) on
  Wednesday and Thursday and 1,440 on Saturday. The regime is therefore taken
  per day from the verified minute data, not assumed.
- **No public history archive** like `public.bybit.com` was found
  (2026-10-18); BingX minutes come from the klines API.

### Minute data layer (both exchanges)

One function `ensure_minute_days(exchange, pair, first, last)` used by every
step that needs candles (history, liquidity, MRS2 tester sampling, MRS3 tick
tests); the files are the tester's day parquet on both exchanges and every
figure is computed from them the same way:
- Bybit: the official public trade archive (`minute_data.ensure_days`), folder
  `tester\data\bybit\<SYMBOL>\` — complete by construction (built from all
  trades of the day);
- BingX: `bingx_downloader` klines, folder `tester\data\bingx\<API symbol>\`,
  files named by the human ticker; download mode `incremental` / `overwrite`
  from the registry settings.

### BingX minute verification

Checked on 2026-10-18: BingX now returns a candle for every minute of a trading
day, with zero volume when nothing traded (78 of 80 sampled days complete; 190
zero-volume minutes). A missing minute is therefore the exception and is
verified, not assumed:

1. **Expected minutes** of a day: from the listing minute (on the listing day)
   or 00:00 to 23:59 UTC. A day whose candles cover every expected minute, with
   strictly increasing distinct times and sane OHLC, is `OK`.
2. **A day with missing minutes** (or no candles at all) is checked against the
   hourly klines of the same day (one request): for every hour, the hour candle
   must equal the aggregate of that hour's minutes — open of the first, close of
   the last, maximum high, minimum low, sum of volume; an hour with no minutes
   must have no candle or zero volume. Verified on 2026-10-18 on 12 contracts
   (every hour equal) and on KOPN 2026-10-15, where 11 consecutive minutes
   (22:52–23:02) are absent but both hour candles equal the remaining minutes
   exactly: no trade was lost, it was an exchange pause.
   - all hours equal → `VERIFIED_GAPS`: the missing minutes had no trades; the
     day is accepted, and the number and placement of the gaps are recorded
     (this is also how exchange-hours days, 391 candles, are recognised);
   - an hour differs → the day's minutes are fetched again (up to 3 times); if
     an hour still differs the day is `SUSPECT`: not accepted, reported with
     the hours that differ, and the segment, test or job that needs it is
     refused with `MISSING_DAYS`.
3. **Recorded:** a local registry table `minute_days` (exchange, pair, day,
   candles, expected minutes, zero-volume minutes, missing minutes, gap ranges,
   status, verification time, file hash). A verified result is reused while
   the file hash is unchanged, so days are not re-requested on every run; an
   `overwrite` download re-verifies. `SUSPECT` days are retried at the next
   refresh.
4. Days already on disk (downloaded before this check existed) are verified
   once by the same rule.

The tester does not download BingX data, so **every tester run** — MRS2 sampled
jobs and MRS3 tick tests, on both exchanges — calls `ensure_minute_days` for
`StartDate − warm-up … EndDate` first and runs with `UpdateData = false`.

## Panel

A «Реестр пар» screen with buttons, each showing one view (filterable,
sortable) and an «Excel» export of exactly what is shown:
- **Справочник** — reference + current risk tiers (leverage, limits, margin);
- **Ликвидность** — latest snapshot, 10/30-day trend, base lot history;
- **Конвейер** — stage, periods, READY now, score/DD/category, track record,
  tester and MRS3 status;
- **Решения** — current decision per pair, editable (decision + reason), and the
  full history of a pair;
- **Изменения** — reference changes (`pair_changes`) and new/delisted pairs,
  with history sufficiency for the new ones;
- **Настройки пары** — personal filter thresholds next to the common defaults.

A **«Синхронизировать»** button on the screen runs `registry sync` (pull + import
+ exchange refresh) and shows what came in.

Two heavier actions run on the machine where they are pressed (both machines
hold a trade store), each as a Panel task, each **after a plan dialog** (user
decision 2026-10-18):
- **«Пересчитать историю»** — top up the minute days and simulate what changed:
  the latest window and the last period of every pair, full history for pairs
  that just reached enough days.
- **«Пересобрать READY»** — rebuild READY from the updated periods and
  recompute `pipeline`.

The plan dialog lists one row per pair with what would be done (for example
«новая пара: 3 периода», «продлить последнее окно», «без изменений»; for READY:
«появились новые периоды» / «без изменений»), a checkbox in every row, and
«Выделить все» / «Снять выделение». Only the checked pairs are processed.

## No scheduler

User decision 2026-10-18: with the «Синхронизировать» button a Windows Task
Scheduler job is not needed. All three actions are also CLI commands
(`registry sync`, `registry history`, `registry ready`, the last two taking an
explicit pair list), so a scheduler can be added later without code changes. Every run writes a journal row (step, status, counts,
duration).

## Consumers to switch (inventory, 2026-10-18)

Read or write the workbook today:
`src/mrs3/loader.py`, `panel.py`, `panel_settings.py`,
`panel_performance_v2.py`, `performance_v2_import.py`, `performance_v2_input.py`,
`analysis_profile.py`, `screener/registry.py`, `trade_history.py`;
scripts `calculate_bybit_base_lots.py`, `build_bingx_screening_sheet.py`,
`generate_period_csv.py`, `simulate_history.py`, `plateau_research/common.py`.

They move to one module `mrs3.pair_registry` (listing day, pair list with
filters, liquidity, decisions). Until each is switched, the registry generates a
compatible «Пары» sheet (same columns as today) so nothing breaks mid-way.

## Stages

1. **Registry core + reference.** DuckDB schema, `pairs`, `risk_tiers`,
   `pair_changes`, refresh CLI for Bybit and BingX, journal. Acceptance: the
   whole Bybit tradfi universe and BingX list load; a re-run is idempotent; a
   changed field writes one change row.
   Includes the universe rules, automatic new listings and history
   sufficiency.
2. **Liquidity (weekdays, `LOT_MODEL_V2`) + optimizer inputs.** ADR for the
   model change first; daily snapshots and raw inputs; the base-lot export and
   the optimizer read from here.
3. **Pipeline state, tests, finalists, decisions, pair settings.** Computed
   `pipeline`, stage rule, track record, `pair_tests`, `finalists`,
   `decisions`, `pair_settings` and READY reading the effective per-pair rule.
4. **Panel screen + Excel export + both buttons**, then switch the consumers
   listed above.
5. **Git sync**: the private repository, automatic upload on change,
   `registry sync` pull/import.

Each stage: spec details first, TDD, `.venv` tests on drive C, independent
review, then a commit.

## Decisions taken (2026-10-18)

- Source of truth DuckDB; Excel generated; decisions entered in the Panel; old
  screening columns and liquidity snapshots not migrated.
- Only our own pair data syncs (decisions, settings, tests, finalists, pipeline);
  exchange data is refreshed locally by the button; separate private repository
  via `gh`.
- Liquidity on weekdays only, window 7 weekdays by default and configurable; the
  optimizer reads computed values only.
- No scheduler; a «Синхронизировать» button plus «Пересчитать историю» and
  «Пересобрать READY», each with a per-pair plan dialog (checkboxes, select all /
  clear).
- A trade store on both machines; history moved by hand for now.
- Per-pair filter settings table.
- No trading-session fields: Bybit perpetuals, stock perps included, trade 24/7
  (user, 2026-10-18); thin trading while the underlying market is closed is a
  liquidity matter and is handled by the weekday-only window.
- BingX works on its own full TradFi catalog (status 1), independent of Bybit;
  no cross-exchange link. Human tickers come from the BingX `symbol`.
- No BingX history archive exists; minutes come from the klines API and every
  day with missing minutes is verified against the hourly klines
  (`minute_days`).
- Active BingX stocks trade 24/7; transitional exchange-hours days are
  recognised from the verified minute data.
- The MRS2 tester sampling rule stays `select-v1` (top 10 % plus cube
  neighbours, about 22–30 % of the grid depending on the pair).
