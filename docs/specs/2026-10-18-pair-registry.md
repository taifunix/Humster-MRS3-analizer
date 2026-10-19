# Pair registry: one source of truth per exchange pair

**Status:** Approved by the user 2026-10-18 (revision 3 after the user's
review: sync scope, weekday liquidity with a configurable window, optimizer
reads computed liquidity only, button instead of a scheduler, automatic new
listings, per-pair filter settings). Stages 1–3 implemented (see the evidence
sections); stages 4–5 pending.
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

Since Panel stage 4 the `finalists` table is the only store of the marks
([ADR-0080](../decisions/0080-finalist-marks-in-the-pair-registry.md)). The PerformanceDB reviews
seed it once and never override a registry mark.

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
   **Amended by [ADR-0082](../decisions/0082-bingx-hourly-volume-noise.md)
   (2026-10-19):**
   - Prices are compared exactly.
   - The hour's volume may exceed the sum of its minutes by at most 0.1 %.
     BingX hourly volumes of older days are noisy aggregates, measured at
     +0.048 % at most over 864 hours.
   - More volume in the minutes than in the hour is no loss.
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
   listed above. ✅ 2026-10-19 (see «Stage 4 contract» and «Stage 4
   evidence»).
5. **Git sync**: the private repository, automatic upload on change,
   `registry sync` pull/import. ✅ 2026-10-19 (see «Stage 5 contract» and
   «Stage 5 evidence»).

Each stage: spec details first, TDD, `.venv` tests on drive C, independent
review, then a commit.

## Stage 1 contract: registry core and reference (2026-10-18)

**Module.** Package `mrs3.pair_registry`:
- `store`: schema and connection;
- `bybit` and `bingx`: fetch and normalise;
- `refresh`: change detection and journal;
- `history`: sufficiency;
- CLI `python -m mrs3.pair_registry refresh [--exchange bybit|bingx|all]
  [--config config.local.json]`.

**Configuration.** A `pair_registry` section in `config.local.json`. Every key
has a default. A missing config file or a malformed section is refused with a
clear error:
- `root` (`data/pair_registry`) holds `registry.duckdb`;
- `universe.bybit.symbol_types` (`stock`, `ETF`, `commodity`);
- `universe.bybit.exclude` (`SPXUSDT`, `XAUTUSDT`);
- `universe.bingx.prefixes` (`NCSK`, `NCSI`, `NCCO`);
- `universe.bingx.exclude` (empty).

**Tables.**
- **`pairs`**, key (exchange, symbol). The symbol is the human ticker.
  - Status values:
    - `TRADING`;
    - `SUSPENDED`: BingX status 25, stored but out of the working list;
    - `DELISTED`: left the exchange list.
  - Reference fields:
    - `api_symbol`, display name, base asset, symbol type, contract type,
      settle coin;
    - listing and delivery time;
    - BingX `api_open`/`api_close`;
    - tick, min/max price, price scale;
    - qty step, min/max order qty, max market qty, min notional;
    - min/max leverage, leverage step;
    - funding interval and caps;
    - maker/taker fee (BingX record);
    - full name.

    Numbers are stored as canonical decimal text, so change detection is
    exact.
  - Also stored: `first_seen` and `updated_at`.
- **`pair_changes`**: one row per event, kind `listed` / `delisted` / `field`
  (field, old, new) with time. A pair that comes back is a `field` change of
  `status`.
- **`risk_tiers`** (Bybit): one snapshot per (exchange, symbol, UTC day). A
  second refresh on the same day replaces that day's snapshot.
- **`journal`**: run id, step, exchange, status, counts, start, end, error.

**Refresh rules.**
- One exchange at a time, in one transaction.
- An empty instrument list is refused, so a broken API answer never delists
  the universe.
- Pairs of the exchange that are missing from the list become `DELISTED` with
  a `delisted` change. Their rows and history stay.
- A re-run with the same data writes no change rows. One changed field writes
  exactly one `field` row.
- The BingX human ticker follows «BingX names and data».
- BingX `tradeMinQuantity` is not stored: it is the minimum notional divided by
  the live price. In the first live run it moved on 80 contracts between two
  refreshes, which made the refresh non-idempotent. The stable reference is
  `min_notional` (`tradeMinUSDT`).
- Every refresh is journaled: a failure while fetching or normalising,
  including a ticker collision, writes a FAILED row too. With `--exchange all`
  a failure of one exchange never stops the other. The CLI always prints a
  summary and returns 1 if anything failed.
- A contract of a configured prefix that the ticker rule cannot read (not
  upper-case letters and digits) is left out and listed in the summary as
  `unreadable_symbols` (none on 2026-10-18).
- Known edges of the ticker rule, kept as specified (review 2026-10-18):
  - `NCCOXAU2TRY-USDT` (gold priced in TRY) reads as `XAUUSDT`;
  - a bare `USD` tail is not stripped (`NCCOXAGJPYUSD-USDT` → `XAGJPYUSDUSDT`);
  - if BingX ever lists a second contract with the same human ticker (e.g.
    `NCCOXAU2USD-USDT`), the whole BingX refresh is refused until the user
    adds one of them to `universe.bingx.exclude`. Refusing is preferred to a
    silent rename.
- Two BingX contracts with the same human ticker refuse the refresh; both are
  named in the error.

**History sufficiency** (`history.sufficiency(listing_ms, today)`):
- days since the listing day;
- `research_from` = listing day + 4 + 20 days;
- `ready_from` = listing day + 4 + 40 days;
- status «мало истории (N дней)» / «можно исследовать» / «READY возможен».

**Acceptance.**
- Unit tests with recorded API shapes: universe filters, ticker rule,
  collision refusal, idempotent re-run, one change row per changed field,
  delisting, empty-list refusal, risk-tier day snapshot, sufficiency boundaries.
- A live refresh of both exchanges loads the whole Bybit TradFi universe and
  the BingX TradFi list. A second live run writes no change rows.

## Stage 1 evidence (2026-10-18)

- **Tests:** `tests/test_pair_registry.py`, 27 cases. They cover:
  - universe filters for both exchanges;
  - the BingX ticker rule (`AMD`, `TMF`, `SOFTBANK`, `GOLD`, `US500`, `F`);
  - collision refusal;
  - statuses and reference fields;
  - an idempotent re-run with one change row per changed field;
  - delisting and coming back;
  - empty-list refusal;
  - risk-tier day snapshots;
  - exchange separation;
  - sufficiency boundaries;
  - settings defaults and overrides;
  - the CLI, including one exchange failing while the other completes, journaled
    fetch and normalisation failures, malformed or missing configuration.
- **Independent review:** `CODE_REVIEW_PASS` after fixes (CLI isolation per
  exchange, journal coverage, settings validation, unreadable BingX symbols).
- **Live** (`python -m mrs3.pair_registry refresh`, registry in
  `data/pair_registry`):
  - Bybit: 274 TradFi pairs and 8,240 risk tiers.
  - BingX: 605 TradFi contracts, 369 `TRADING` and 236 `SUSPENDED`.
  - A second run wrote 0 change rows on both exchanges. Before the fix it
    wrote 80 on BingX: `min_order_qty`, see the refresh rules.

## Stage 2 contract: weekday liquidity, LOT_MODEL_V2 (2026-10-18)

Decision: [ADR-0079](../decisions/0079-lot-model-v2-weekday-liquidity.md).

**Module.** `mrs3.pair_registry.liquidity`.
- `day_facts(path, day)`: the facts of one tester minute day file, read with the
  `minute_capacity` reader: turnover, positive minutes, active 15-minute bins,
  the minute turnovers (for `V25`) and the file hash.
- `select_window(files, end_day, n)`: the last `n` weekdays (Monday–Friday UTC)
  ending on `end_day`. It returns the weekdays with a file and those without
  one. Weekend days never enter.
- `window_row(days, k, step)`: the `LOT_MODEL_V2` window of the given days:
  - `V25` = type-7 Q25 of the positive minute turnover;
  - `A15` = active bins / (days × 96);
  - `L = V25 × A15`;
  - base lot = `floor_to_step(K × L, step)`, using V1's `_floor_step`;
  - the window's input files (day, sha256) and their digest `inputs_sha256`.

  The formulas are identical to V1 on the same days.
- `refresh_liquidity(con, exchange, today, settings)`, Bybit only until panel
  stage 5:
  - The window ends on the **last weekday before `today`**, never on «the
    last day that has a file». Stale or missing minute data therefore can
    never produce READY (fail closed, ADR-0079).
  - For every TRADING pair it reads the files from the window's first day to
    the day before `today`.
  - It rewrites the day rows of that span: a day whose file vanished or
    became unreadable loses its row and is counted in `unreadable_days`.
  - It writes the window row keyed (exchange, symbol, as-of day = window end).
  - A window weekday without a readable file gives
    `LIQUIDITY_MODEL_WINDOW_UNAVAILABLE`, with the missing days and a reason:
    `missing weekday files` or `no positive minutes`.
  - Every run writes a journal row (step `liquidity`, OK or FAILED, counts).
  - One transaction per pair. Re-running on the same files is idempotent.

**Tables.**
- **`liquidity_days`** (exchange, symbol, day): weekend flag, turnover,
  positive minutes, active bins, file hash.
- **`liquidity`** (exchange, symbol, as-of day):
  - model version and status;
  - window length, window days, missing days and reason;
  - positive minutes, active bins, total bins;
  - `V25`, `A15`, `L`, `K`, step and base lot, as decimal text;
  - window input files and `inputs_sha256`.

  Columns added after a draft table was created are added in place by
  `connect`.

**Settings** (`pair_registry.liquidity`):
- `window_weekdays` (7, whole number from 3 to 20);
- `k` (9, from 1 to 20, the optimizer's accepted range);
- `round_down_usdt` (10, whole);
- `data_root` (default `<tester_runner.bot_root>/tester/data/bybit`).

**Reader for consumers.** `mrs3.pair_registry.read_liquidity(con, exchange,
symbol)` returns the newest window of a TRADING pair only if that newest window
is READY, else None. A newer UNAVAILABLE window hides an older READY one.
`latest_liquidity` returns the newest row of any status, for views.

The optimizer and the base-lot export switch to the reader in registry stage 4
(«switch the consumers»). Until then V1 keeps working. Two requirements for
that switch (review 2026-10-18):
- The consumer requires `inputs_sha256`: a READY row written by the stage 2
  draft carries none.
- V1's `publication_lag_hours` has no equivalent yet. A run early in the UTC
  day, before yesterday's minute file exists, gives UNAVAILABLE for every
  pair; a later run on the same day overwrites those rows. This fails safe,
  and the minute top-up of «Синхронизировать» runs before the liquidity
  refresh.

**CLI.** `python -m mrs3.pair_registry liquidity [--config …] [--today
YYYY-MM-DD]`.

**Acceptance.**
- Unit tests:
  - weekday selection across a weekend (weekends never enter);
  - formulas and floor equal to V1 on the same days;
  - window length as provenance;
  - a missing or unreadable weekday gives UNAVAILABLE;
  - stale files give UNAVAILABLE;
  - a newer UNAVAILABLE window hides an older READY one;
  - a pair that left TRADING is not read;
  - day rows follow the files;
  - journal rows;
  - settings validation;
  - idempotent re-run;
  - the package export.
- A live run on the registry's Bybit TRADING pairs, with counts of READY and
  UNAVAILABLE pairs.

## Stage 2 evidence (2026-10-18)

- **Tests:** `tests/test_pair_registry_liquidity.py` (22, after the review fixes). They
  cover:
  - day facts;
  - V25/A15/L/base lot equal to `LOT_MODEL_V1` on the same seven days;
  - selection of the last weekdays across weekends;
  - a missing weekday gives UNAVAILABLE with the missing day;
  - window length as provenance (5 weekdays → 480 bins);
  - settings validation and defaults (data folder from
    `tester_runner.bot_root`);
  - idempotent re-run;
  - the CLI.
- **Live** (`python -m mrs3.pair_registry liquidity`, 70 s): 274 TRADING Bybit
  pairs.
  - 242 READY, all with the window 2026-10-08…2026-10-16, which skips the
    weekend 10–11.
  - 32 UNAVAILABLE: no minute files at all. These are pairs outside the old
    workbook universe; their minutes come with the minute top-up of
    «Синхронизировать».
  - 4,358 day rows in the first run. After the review fixes the span runs
    from the window's first day to the day before the run: 2,180 day rows,
    the same 242/32 result, one journal row, `inputs_sha256` on every READY
    row.
  - Examples: XOMUSDT `V25` 17.48, `A15` 0.940, base lot 140 USDT; NVDAUSDT
    base lot 11,060 USDT.
- **Independent review:** `CODE_REVIEW_PASS` after fixes (fail-closed reader and
  window end, journal, day rows follow the files, V1 floor, K range, input
  hashes, reason column, contract matching the code).
- **BingX liquidity** waits for the BingX minute data layer (panel stage 5).
  `refresh_liquidity` refuses any exchange other than Bybit.

## Stage 3 contract: pipeline, tests, finalists, decisions, pair settings (2026-10-18)

**Module.** `mrs3.pair_registry.records` handles the hand and imported rows;
`mrs3.pair_registry.track` holds the walk-forward track record;
`mrs3.pair_registry.pipeline` computes the state. The tables are created by
`records.ensure_schema`, called from `connect`.

**Hand rows** (append-only, synced in stage 5):
- **`decisions`**: seq, exchange, symbol, side (`LONG` / `SHORT` / `ALL`),
  decision (`INCLUDE` / `EXCLUDE` / `WATCH`), reason, author, time.
  - The pair must exist; reason and author are required.
  - The decision in force for a side is the latest row of that side or of
    `ALL`.
- **`pair_settings`**: seq, exchange, symbol, side, filter, parameter, value
  (empty = reset to the common default), reason, author, time.
  - Declared parameters of filter `ready_v4`, with type and range:
    - `admit_score` (number, 0–1000);
    - `max_dd`, `low_dd`, `moderate_dd` (number, 0.1–50);
    - `min_trades` (whole, 1–1000);
    - `min_shift_bp` (whole, 0–550);
    - `pool` (whole, 2–20);
    - `ratio` (number, 1–10).
  - An unknown filter or parameter, a wrong type, an out-of-range value, or a
    result that breaks `low_dd ≤ moderate_dd ≤ max_dd` is refused at entry.
  - `effective_ready_rule(con, exchange, symbol)` returns the rule and the
    overridden parameters: the latest row per parameter (side `LONG` or `ALL`)
    over `ReadyRule()`.
- **`finalists`**: seq, exchange, symbol, side, strategy, status (`FINALIST` /
  `RESERVE` / `REJECTED` / `NONE`), user rank, source, time.
  - The status in force is the latest row per strategy.
  - `import_finalists(con, performance_db, exchange)` takes each strategy's
    latest User Status from the PerformanceDB selection reviews (FINALIST,
    RESERVE, REJECTED). It appends only changes, with source
    `performance_db`.
  - The newest review is authoritative (ties broken by review id). A strategy
    whose newest status is no mark (ANALOG, FILTERED) gets `NONE` when the
    registry still had it marked.

**Imported tests** (`pair_tests`, keyed by source, so re-importing adds
nothing):
- `tester_sample`: every tester segment of the trade store (window start/end,
  source `store:<segment>`);
- `mrs3_tick`: every PerformanceDB strategy with a result (strategy name, test
  window, PnL %, MaxDD %, trades, source `perfdb:<result id>`), for the
  exchange of that result.

**Track record** (`track.track_record(store, symbol, exchange, rule)`):
- the walk-forward of `store_threshold_study.py` with the pair's effective
  production rule: train on up to the three periods before a cut, measure the
  admitted structures on the next period as a true combined curve;
- returns the cuts tested, the cuts with an admitted structure, the admitted
  candidates and the forward loss rate;
- `combined_forward` moves from the study script into this module, and the
  script imports it.

**Pipeline** (`pipeline.compute_pipeline(con, store, exchange, as_of_day,
machine, now_ms)`):
- One row per (exchange, symbol, side LONG, as-of day) for every pair of the
  exchange:
  - machine and computation time;
  - stage;
  - active simulator periods, first and last period day;
  - READY now from the latest store catalog row: status, rule version,
    admitted count, best score / DD / category;
  - latest tester window;
  - tick-test count;
  - FINALIST strategies;
  - decision in force;
  - history sufficiency status;
  - track record: cuts, admitted cuts, forward loss rate, and the hash of
    its inputs. It is recomputed only when the segments or the rule change.
- Stage, first match wins: EXCLUDED → FINALIST → TICK → TESTER (a tester
  segment of the newest simulator window) → READY → HISTORY (≥ 2 periods) →
  NEW.
- Journaled as step `pipeline`. Before computing it imports tests and
  finalists.

**READY with per-pair rules.**
- `build_ready(…, rules=…)` takes a rule per pair. The manifest pair entry and
  the catalog payload record the effective parameters and the overridden ones
  (the inputs hash already covers the rule).
- `ready_from_store` reads the effective rules from the registry **read-only**
  when `pair_registry` is configured and the registry exists. The build stops
  with an error when the registry is locked by a writer, or when stored
  overrides no longer form a valid rule.
- A rebuild with the same inputs moves the catalog row's time forward
  (microsecond timestamps, upsert). After an override is reset, «READY now»
  therefore shows the latest build again.
- `effective_ready_rule` lists the explicitly stored overrides; the manifest's
  `overridden` lists the parameters whose value differs from the common rule.

**CLI.**
- `python -m mrs3.pair_registry pipeline [--machine NAME]`;
- `decide --symbol … --side … --decision … --reason … --author …`;
- `setting --symbol … --side … --filter … --parameter … [--value …] --reason …
  --author …`.

**Acceptance.**
- Unit tests:
  - decision validation and the decision in force;
  - setting validation including the DD ordering, reset to default and
    the effective rule;
  - finalists import with changes only;
  - test import idempotence;
  - stage rule order;
  - track record on a synthetic store, with its reuse;
  - `build_ready` with an overridden pair (manifest records it, inputs hash
    changes);
  - the CLI.
- A live pipeline run over the registry with the real trade store and
  PerformanceDB, with counts per stage.

## Stage 3 evidence (2026-10-18)

- **Tests:** `tests/test_pair_registry_records.py` (18). They cover:
  - decision validation and the decision in force (side and `ALL`);
  - settings validation, including the DD ordering, reset to default and
    the effective rule;
  - finalists import from real selection reviews (latest review wins, changes
    only, ANALOG is not a mark);
  - tick-test and tester-sample import idempotence;
  - a protocol v1 database counts as Bybit;
  - the stage rule;
  - track record walk-forward and its reuse;
  - pipeline rows and journal;
  - `build_ready` with a per-pair override (manifest records it, inputs hash
    changes);
  - `ready_from_store` reading the registry overrides;
  - the CLI.
- **Exchange of a PerformanceDB:** `schema_info.exchange` (protocol v2). A
  protocol v1 database has none, holds Bybit results only, and stores
  `tester` in `strategy_results.exchange`; it is read as Bybit.
- **Live** (`python -m mrs3.pair_registry pipeline`) over 274 Bybit pairs:
  - First run: track records computed for the 198 pairs with ≥ 3 periods.
  - Second run: 15 s, track records reused, 1,805 tick tests imported from
    the PerformanceDB.
  - Stages: TICK 9, READY 1, HISTORY 220, NEW 44.
  - READY counts only pairs with a store READY catalog row; READY for the
    whole registry is the next «Пересобрать READY» run.
  - Track record: 35 pairs with admitted candidates, mean forward loss rate
    0.20. GDXUSDT and INTCUSDT lose on every admitted cut, matching the
    2026-10-17 study.
- **Independent review:** `CODE_REVIEW_PASS` after fixes. The fixes:
  - a rebuild moves the READY catalog row's time forward;
  - a demoted finalist gets NONE;
  - finalists of the LONG row count LONG strategies only;
  - the DD ordering is checked for every side;
  - `ready_from_store` reads the registry read-only;
  - the reason is reported when the PerformanceDB is unavailable;
  - CLI errors are returned as JSON.

  Note: catalog rows written before stage 3 keep a payload without
  `rule` / `overridden` when the same inputs are rebuilt, so readers must
  allow for those fields being absent.

## Stage 4 contract: switch of the consumers (2026-10-19)

**Panel screen, Excel and buttons** are delivered by Panel rework stage 3.
- The main screen has the views Пары, Справочник, Ликвидность and Изменения,
  each with an Excel export of exactly the rows shown.
- Decisions and per-pair thresholds are entered in the pair card.
- «Синхронизировать», «Пересчитать историю» and «Пересобрать READY» are
  actions with plan dialogs.
- See [Panel for the simulation workflow](2026-10-18-panel-simulation-workflow.md),
  «Stage 3 contract».

**1. Optimizer: LOT_MODEL_V2 from the registry** (ADR-0079).
- `portfolio.adapter.run_portfolio_adapter` takes a keyword
  `liquidity_reader(symbol, anchor, round_down_usdt) -> MinuteCapacityResult`
  (the Campaign's own floor). With a reader, no minute file is read or
  downloaded.
- The Panel passes `pair_registry.liquidity.registry_capacity` whenever the
  registry of the local config exists (`pair_registry.root`, default
  `data/pair_registry`). Without a registry the adapter keeps LOT_MODEL_V1
  (fixtures, a machine without a registry).
- A registry held by a writer gives the blocker `PAIR_REGISTRY_BUSY`.
- `registry_capacity(con, exchange, symbol, anchor_created_at=…,
  round_down_usdt=…)`:
  - takes the window of a TRADING pair that ends on the last weekday before
    the anchor's UTC day (`liquidity_on`), so a frozen Campaign retried after
    a later sync still finds its window;
  - requires it READY, LOT_MODEL_V2 and with `inputs_sha256`, otherwise
    `LIQUIDITY_MODEL_WINDOW_UNAVAILABLE` (the registry needs
    «Синхронизировать»);
  - applies no publication lag. Between UTC midnight and a sync run after the
    previous weekday's minute file exists, Campaigns get
    `LIQUIDITY_MODEL_WINDOW_UNAVAILABLE`. A sync run too early writes that day
    as UNAVAILABLE until the next sync. The Campaign keys
    `archive_publication_lag_hours`, `backfill_write_enabled` and
    `bybit_minute_data_root` are unused under V2;
  - returns `selected_basis = "LOT_MODEL_V2"`, the window weekdays, the
    `(day, sha256)` file hashes and a content digest over the row inputs.
- Position sizing validates a V2 window:
  - weekdays only, sorted and unique;
  - as many file hashes as days;
  - start and end equal to the capacity window;
  - the end is the last weekday before the anchor day;
  - active bins at most N × 96.
- Enriched rows carry `liquidity_lot_model` (`LOT_MODEL_V1` or
  `LOT_MODEL_V2`). The V2 model digest uses the schema
  `liquidity_lot_model_v2`, and the executable identity evidence records
  `LOT_MODEL_V2`; V1 identities keep their bytes. So V1 and V2 results cannot
  be mixed unnoticed.

**2. Base-lot export.**
- `scripts/calculate_bybit_base_lots.py --source registry` is the default.
  Each (pair, date) cell takes the registry window ending on the last weekday
  before that date (`liquidity_on`). The window must be READY and carry
  `inputs_sha256`, and its K and step must equal the export's.
- The registry's K and step settings are compared once, before any cell is
  computed.
- A missing or unavailable row, or a busy registry, is an error cell, as
  before.
- The workbook records the model of its values: the defined name
  `MRS3_BASE_LOT_MODEL`; without it the values are LOT_MODEL_V1.
  - A run of the other model refuses to add to an existing history, and the
    workbook is left unchanged.
  - Only `--rebuild-history` replaces all ten dates. V1 and V2 values are
    never mixed in one sheet.
  - Registry windows of past days come from
    `python -m mrs3.pair_registry liquidity --days 14`: oldest first, so the
    newest window stays today's.
- `--source minute-files` keeps LOT_MODEL_V1. `run_export(lot_source=…)` is
  the library form.

**3. Pairs and listing days of the history.**
- `pair_registry.history.history_pairs(config)` returns the TRADING Bybit
  pairs of the registry with their listing day, ordered by the latest
  liquidity `L` descending, then by symbol. The screening verdicts left with
  the screener.
- `simulate_history` and `generate_period_csv` use it. They fall back to the
  workbook (`trade_history.pair_order`) when there is no registry, or with a
  warning line while a writer holds it.
- Explicit `--pairs` resolve listing days of any status
  (`pair_registry.history.listing_days`).

**4. Listing dates of the PerformanceDB import, pipeline, finalist retest and
plateau research.**
- These keep reading a workbook through `loader.load_listing_dates`. The path
  and the source of the listing date are recorded in PerformanceDB, so their
  contract does not change.
- The registry writes that workbook atomically:
  - on demand: `python -m mrs3.pair_registry workbook [--out PATH]`; this
    needs an existing registry;
  - on every `sync`: `<registry root>/pairs.xlsx`. A locked file fails only
    that step, with no local path in the message.
- Sheets:
  - «Пары»: Пара, Дата листинга на Bybit (UTC);
  - «Пары_BingX»: Пара, Листинг.
- Pointing `panel_workflow.listing_dates_path` at it moves these consumers to
  the registry. That switch of the local config is the user's.

**Non-goals.**
- No PerformanceDB schema change.
- The V1 code stays for fixtures and comparison.
- BingX liquidity comes in Panel stage 5.

**Acceptance.**
- Unit tests:
  - `registry_capacity`: freshness, `inputs_sha256`, a pair that is not
    TRADING, no row;
  - sizing validation of a V2 window and the row's model field;
  - the adapter with a reader, with no minute files present;
  - the Panel wiring;
  - the base-lot export in registry mode;
  - `history_pairs`: ordering and fallback;
  - the workbook read back by `load_listing_dates` on both sheets.
- Live checks:
  - the listing dates in the generated workbook equal the current workbook's
    for the shared pairs;
  - the number of TRADING pairs with a usable V2 capacity.

## Stage 4 evidence (2026-10-19)

**Code**
- `pair_registry.liquidity`: `registry_capacity`, `liquidity_on`,
  `last_weekday_before`.
- `pair_registry.history.history_pairs`; `pair_registry.workbook`.
- CLI: `workbook`; `sync` gains a `workbook` step.
- `portfolio.adapter`: `liquidity_reader`.
- `portfolio.position_sizing`: V2 window checks, `liquidity_lot_model`, and
  the model digest schema `liquidity_lot_model_v2`.
- `panel_portfolio`: `_registry_liquidity_reader`.
- `scripts/calculate_bybit_base_lots.py`: `--source`, `registry_lot_source`.
- `simulate_history` and `generate_period_csv` use `history_pairs`.

**Review fixes.** The first independent review returned `CODE_REVIEW_FAIL`,
with one MAJOR finding: the default registry source would have mixed V1 and
V2 values in an existing base-lot sheet. The fixes:
- the model marker, the refusal and the full rebuild;
- `liquidity --days`;
- the Campaign's frozen window via `liquidity_on`;
- the busy-registry paths;
- the K/step check done once;
- explicit `--pairs` resolving listing days of any status;
- V2 recorded in the identity evidence;
- the workbook command requiring an existing registry;
- redacted workbook step errors.

The re-review returned `CODE_REVIEW_PASS`.

**Tests** (`.venv`, basetemp on drive C)
- `tests/test_pair_registry_consumers.py` (16).
- In `test_portfolio_adapter`: the reader test and the identity evidence of
  V2.
- `liquidity_lot_model` asserted on V1 sizing rows.
- The CLI sync test now includes the workbook step.
- The base-lot CLI tests now pass `--source minute-files` explicitly.
- After the fixes, the registry, consumer, sizing, adapter, base-lot,
  portfolio Panel and main-screen suites: 877 passed, 1 skipped.
- Before the fixes, the portfolio, base-lot, registry, Panel main-screen,
  store and READY suites: 2,289 passed and 17 failed.
  - 15 of the failures also fail on HEAD; one example is
    `test_portfolio_input` (the `selection_review_imports` schema).
  - The other two are the `test_panel_portfolio` stage 2 tests, which are
    flaky: the same run on HEAD also fails one of them.

**Live checks** (real registry, read-only; output to a temporary folder)
- The generated workbook has 274 Bybit and 610 BingX pairs.
  - All 242 pairs shared with the current workbook have equal listing dates
    through `load_listing_dates`.
  - The registry has 32 pairs the workbook lacks.
- `registry_capacity` with an anchor of now (Monday 2026-10-19): 242 of 274
  TRADING pairs are LOT_MODEL_V2; 32 are `LIQUIDITY_MODEL_WINDOW_UNAVAILABLE`
  (no minute files, the same 32 as in stage 2).
- `history_pairs`: 274 pairs, most liquid first.
- Not run live:
  - a real optimizer Campaign (it needs the market API and a v2
    PerformanceDB);
  - the base-lot export on the user's workbook.

  Both are covered by unit tests.

**Note for the user.** To move the PerformanceDB import and the other
loader-based consumers to the registry, point
`panel_workflow.listing_dates_path` in `config.local.json` at
`<registry root>/pairs.xlsx`. «Синхронизировать» writes it.

## Stage 5 contract: git sync (2026-10-19)

Decision: [ADR-0081](../decisions/0081-pair-registry-git-sync-format.md).

**Repository.**
- A local git repository, `pair_registry.sync.path` (default `<root>/sync`),
  is created with `python -m mrs3.pair_registry sync-init [--remote URL]`.
  Sync is on when it exists.
- Pull and push run only when the repository has a remote `origin`. The user
  creates the private GitHub repository (`gh repo create <name> --private`)
  and passes its URL; the code never creates anything on GitHub.
- Commits use the repository-local identity `mrs3-registry <machine>`.
- When the GitHub CLI is installed, `sync-init` makes it the only credential
  helper of the sync repository, including for the first clone. The private
  repository belongs to the account `gh` is logged in to, which may differ
  from the one in the system credential manager. Global git settings are
  never changed.

**Machine.**
- `pair_registry.machine` if it is set: it must match `[A-Za-z0-9._-]{1,64}`,
  otherwise the settings are refused.
- Otherwise the computer name. Other characters become `_`, and a short hash
  of the original name is appended. A non-ASCII computer name never stops
  the registry.

**One sync at a time.**
- Pull + import and export + commit + push run under an OS lock on
  `.git/mrs3-sync.lock`, shared by the Panel and the CLI (it waits up to
  300 s).
- Files are written atomically: a temporary file in `.git`, then a rename.
- Writing an append-only file keeps the rows already in it (union by
  `row_id`).
- A broken sync file stops the import with a `SyncError` naming
  `<table>/<file>`, and the registry is unchanged.
- A failed git merge is aborted, and the error names
  `pair_registry.machine`.

**Files.**
- One file per synced table and machine: `<table>/<machine>.jsonl`.
- The first line is a header (a JSON array of the column names). Every other
  line is a JSON array of the values in that order. UTF-8, LF, rows sorted by
  key.
- A machine writes only its own files.
- Contents:
  - `decisions`, `pair_settings`, `finalists`: the rows created on this
    machine, keyed by `row_id`;
  - `pair_tests`: the tests of this machine, keyed by `source`;
  - `pipeline`: the newest row per (exchange, symbol, side) that this machine
    computed.

**Row identity.**
- `decisions`, `pair_settings` and `finalists` gain `origin` and `row_id`.
  The first export stamps the local rows that have none: `origin` = this
  machine, `row_id` = sha256 of origin, seq and content.
- `pair_tests` gains `origin`. A test imported from another machine is
  stored under `<origin>:<source>`.

**In force.** The newest `created_ms` wins, then the larger `seq`, in:
- `current_decision` and the per-pair settings;
- `current_finalists`, `finalist_marks` and the change check of
  `record_finalist`;
- the pair card history.

**Import** (`sync_pull`):
1. `git pull` when a remote is set.
2. For every other machine's files:
   - append-only rows are added when their `row_id` (or namespaced `source`)
     is new;
   - a `pipeline` row replaces the local row of the same key and day only
     when its `computed_ms` is newer.
3. The import runs in one transaction and is idempotent.

**Export** (`sync_push`):
1. Stamp the local rows, then write this machine's files.
2. Commit when a file changed.
3. Push when a remote is set. A rejected push gets one pull and one retry.

Export runs after:
- the CLI `decide`, `setting`, `pipeline` and `sync`;
- every Panel decision, threshold or mark write. On the write connection the
  Panel stamps the rows and reads its hand-made rows into memory. A background
  thread then writes the files, commits and pushes under the sync lock, so
  the Panel never waits for git or the network. A sync failure never fails the write: the next write or
  «Синхронизировать» exports again.

**«Синхронизировать»** (`sync`): pull + import → refresh → liquidity →
pipeline → workbook → export + push. Each step reports progress, and a
failing step does not stop the next.

**Acceptance.**
- Two machines on one bare repository in a temporary folder:
  - a decision, a threshold and a mark made on A reach B;
  - concurrent writes on both merge with no conflict;
  - the newer pipeline row wins;
  - tests with the same local source stay apart;
  - re-import changes nothing.
- No remote means a local commit only.
- A rejected push is retried after a pull.
- The in-force order follows time across machines.
- The CLI `sync-init` / `sync` steps; Panel writes trigger the export.

## Stage 5 evidence (2026-10-19)

- Code:
  - `pair_registry/sync.py`: repository, export, import and push;
  - `records.ensure_schema` adds `origin` / `row_id` to an existing
    registry, and the in-force order is now by time;
  - `store.RegistrySettings`: `machine` and `sync_path`;
  - CLI `sync-init` and the export after `decide` / `setting` / `pipeline`;
    `sync` gains the `pull` and `push` steps;
  - `panel_main.MainScreenService._export_after_write` runs after decision,
    setting, finalist and mark-import writes.
- Tests (`.venv`, basetemp on drive C):
  - `test_pair_registry_sync.py`, 14 tests:
    - sync off; a local commit with no remote;
    - A → B over a bare repository, with idempotent re-import and no
      re-export;
    - concurrent writes, with a rejected push pulled and retried;
    - the newer pipeline row; stamping; an old registry gains the columns;
    - `init_repo` with the `gh` credential helper;
    - added after the independent review (`CODE_REVIEW_FAIL`: a BLOCKER on
      the machine name and a MAJOR on the missing cross-process lock and
      non-atomic writes):
      - a safe machine name from the system;
      - the lock;
      - an older snapshot never drops a row;
      - a broken file;
      - two machines with one name;
      - CLI `sync-init` + `decide` pushing to a bare remote.
  - `test_panel_main.py::test_a_panel_write_exports_the_registry_to_the_sync_repository`:
    sync off without a repository; with one, a Panel write exports and commits
    in the background.
  - Registry, Panel main-screen and «Стратегии» suites pass (see
    `progress.md`).
- On GitHub (2026-10-19): the user asked for the private sync repository, and
  `gh repo create --private` created it. `sync-init --remote` was then run on
  the first machine, and the first export was pushed (pair tests and
  pipeline). Before the `gh` credential helper was added, the push failed with
  «Repository not found», because the system credential manager held a
  different GitHub account. The second machine runs the same `sync-init`.
- **Stage 5 implemented ✅ 2026-10-19.**

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
