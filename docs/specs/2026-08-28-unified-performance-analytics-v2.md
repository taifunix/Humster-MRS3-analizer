# Unified Performance Analytics v2

**Status:** vertical slice and single-strategy A/B analysis implemented
**Date:** 2026-08-28
**Decision:** [ADR-0020](../decisions/0020-unified-performance-analytics-v2.md)

## Purpose

Replace the not-yet-populated Performance DB v1 contract with one local,
mutable-by-explicit-operator-action DuckDB that stores the current tester
result for each logical strategy, its order-to-plateau context, cached metrics
for arbitrary safe windows, and reproducible finalist-selection results.

The same database is the only input boundary for the later Portfolio
Optimizer. DD5 is no longer a separate database/workflow; it is a
calculation-only feature and filter stage inside the unified analyzer.

## Scope

In scope:

- one `strategy_performance.duckdb` under the configured Performance root;
- current tester HTML only, with the complete current action and balance
  report profile;
- one current test result per logical strategy;
- transactional add, replace and discard operations;
- order-to-plateau facts copied from the strategy generation evidence;
- arbitrary Window A and Window B analysis from one imported test;
- relative UPNL-aware metrics;
- configurable ordered filters and Pareto stages;
- panel and one XLSX output;
- a `RETEST` handler that regenerates RUNS inputs and replaces results.

Out of scope:

- Performance DB v1 migration or dual read;
- copying the complete Analysis DB into Performance DB;
- point-level Analysis DB lineage (`point_id`) until a concrete point-level
  query requires it;
- a user-defined expression language for filters;
- treating DD5 lot scaling as a tested result;
- portfolio simulation.

The v1 Performance import and DD5 specifications remain historical contracts
for the current code until v2 is implemented. New v2 implementation follows
this specification.

## One database and one current result

The target is exactly:

```text
<performance_db_root>/strategy_performance.duckdb
```

The panel must not allocate a new database from pair names or test dates.
DuckDB has one transactional writer. HTML parsing may run in parallel before
the writer transaction.

A logical strategy has a generated integer `strategy_id` and one current
`result_id`. Canonical strategy JSON hashes and `strategy_version_id` are not
business identities in v2. The stored typed identity is:

- strategy name, symbol, side and timeframe;
- common Close MA;
- ordered Open MA, multiplier/shift and `lot_x` values.

## Core schema

The minimum logical tables are:

### `strategies`

```text
strategy_id BIGINT PRIMARY KEY
strategy_name VARCHAR UNIQUE NOT NULL
symbol VARCHAR NOT NULL
side VARCHAR NOT NULL
timeframe VARCHAR NOT NULL
close_ma_len INTEGER NOT NULL
analysis_run_id VARCHAR NOT NULL
candidate_identity VARCHAR NOT NULL
lifecycle_status VARCHAR NOT NULL       -- ACTIVE / DISCARDED
current_result_id BIGINT
created_at_utc TIMESTAMPTZ NOT NULL
updated_at_utc TIMESTAMPTZ NOT NULL
```

### `strategy_orders`

```text
strategy_id BIGINT NOT NULL
order_id INTEGER NOT NULL
open_ma_len INTEGER NOT NULL
open_multiplier DECIMAL NOT NULL
lot_x DECIMAL NOT NULL
plateau_id VARCHAR NOT NULL
base_point_trades INTEGER NOT NULL
PRIMARY KEY (strategy_id, order_id)
```

One strategy has one to four order rows. Each order may reference a different
plateau. The same plateau may be referenced by any number of orders and
strategies.

### `analysis_plateaus`

```text
analysis_run_id VARCHAR NOT NULL
plateau_id VARCHAR NOT NULL
plateau_point_count INTEGER NOT NULL
plateau_total_trades INTEGER NOT NULL
PRIMARY KEY (analysis_run_id, plateau_id)
```

No synthetic plateau-strength score is persisted in v2. Strength is derived
from typed plateau facts by the selected analysis configuration.

### Current result facts

`strategy_results` stores the report period, exchange/commission facts,
initial balance, imported full-report metrics and the current result identity.
`strategy_actions` stores typed current report actions, including:

```text
result_id, action_index, timestamp_utc, order_id, action,
size, post_size, post_side, pnl, fee, balance
```

`strategy_equity` stores aligned wallet/equity samples. Raw action JSON may be
retained only for report fields not represented by the typed schema; it is not
a replacement for typed window inputs.

Minimal import-run/file evidence remains for transaction audit and guarded HTML
cleanup. It must not duplicate the full strategy JSON or become an analytics
source.

## Accepted HTML contract

Only the current report layout is accepted. Every action table must contain:

```text
Timestamp, Symbol, Order ID, Action, Fee, PnL,
Balance, Size, Post Size, Post Side
```

There is no legacy fallback. Missing `Post Size` makes the report invalid for
v2 import. The table may carry additional tester columns or order its columns
differently; the listed fields are read by name. `Order ID` identifies an
action/order in the tester report, not an MRS3 strategy-order slot. Position
state is authoritative from `Post Size`; flat means zero.
Actions are ordered by `(timestamp_utc, action_index)`.

## Arbitrary safe windows

The user selects independent Window A and Window B, each with arbitrary start
and end timestamps. Windows may overlap, nest or be disjoint.

For each requested boundary independently:

- keep it when the position is flat there;
- move an open start forward to the first subsequent flat state;
- move an open end backward to the preceding flat state;
- never expand beyond the requested interval.

The result records requested and effective timestamps and both shifts. No
valid non-empty flat interval yields `WINDOW_UNAVAILABLE`; insufficient days
or realised trades yields `INSUFFICIENT_DATA`.

## UPNL-relative metrics

Tester runs use `use_upnl=true`, so absolute PnL, drawdown and fee amounts are
audit values, not comparison objectives. At flat boundaries let `W0` and `W1`
be wallet values and `days` be effective elapsed days:

```text
return_pct = (W1 / W0 - 1) * 100
daily_log_return = ln(W1 / W0) / days
daily_growth_pct = (exp(daily_log_return) - 1) * 100
max_drawdown_pct = max((running_equity_peak - equity) / running_equity_peak) * 100
return_dd_ratio = return_pct / max_drawdown_pct
fees_pct = fees / W0 * 100
```

PnL comes from the wallet curve so fees and funding remain included. Profit
Factor uses all realising `decreased` and `closed` actions. Trade count and Win
Rate use reconstructed round trips so partial fills/closures do not inflate
strategy decisions. Holding and time-in-market use the same position episodes.

Both windows are calculated by the same function. Signed return metrics use
differences for A/B deterioration; positive dimensionless metrics may use
ratios.

## Window cache

`window_metrics` caches only requested windows, never every possible interval.
Its identity is `(result_id, requested_start, requested_end)`. It stores the
requested/effective boundaries, relative metrics, availability status and
calculation timestamp. Replacing a result deletes its dependent cache.

## Phase 2: one-strategy A/B analysis

The first Phase 2 vertical is a separate interactive analysis action, never an
import side effect. It accepts one `ACTIVE` `strategy_id`; the server resolves
the result only through `strategies.current_result_id` joined to the same
strategy row. The client never submits a result id, database path, or calculator
version.

`GET /api/v2/strategies/performance-v2/catalog` returns active strategies with
their authoritative current result and report bounds, ordered by strategy name
and id. `POST /api/v2/strategies/performance-v2/windows` accepts the strategy
id plus independent A/B pairs. Every timestamp is ISO-8601 UTC with uppercase
`Z` or `+00:00`; naive and non-UTC offsets are rejected. Responses canonicalize
timestamps to `Z` and encode `Decimal` values as strings.

The manual card may offer date shortcuts derived only from those authoritative
report bounds: Window A "entire period" uses both bounds; Window B "last week"
and "last two weeks" end at the report end and clamp their start to the report
start. They only fill the existing UTC fields and never trigger a calculation.

After calculation, the card shows a compact interval summary for both windows.
It keeps the calendar interval used for duration normalization separate from
the effective first/last event-backed flat boundaries. A short event span is
not an import warning: the tester's wallet/equity series is event-based and may
legitimately end at the last transaction before the report end. An unavailable
window remains visually marked. This presentation uses fields already returned
by the API and does not add a query, cache row or calculation.

### 30-day equivalent for unequal manual windows

The manual A/B response adds `normalization_30d` to each returned window. Its
duration is the requested calendar interval intersected with the authoritative
report interval. It must not use the first or last action/equity timestamp as a
duration boundary: those series are event-based, so an idle tail after the last
trade remains part of the tested period. It is additive: the raw
`window_metrics` cache, schema and all pre-existing response fields remain
unchanged.

`observed_days` is the intersected calendar elapsed microseconds divided by
86,400,000,000 as a `Decimal`. With a full-precision duration of at least one day, the server
returns a constant-rate 30-day equivalent:

```text
growth_factor_30d = exp(30 * ln(growth_factor) / observed_days)
return_pct_30d = 100 * (growth_factor_30d - 1)
trade_rate_30d = trade_count * 30 / observed_days
```

The object has fixed-scale decimal strings: six places for `observed_days`,
eight for `growth_factor`, and four for `return_pct` and `trade_rate`; rounding
is `ROUND_HALF_UP`. `status` is `ok`, `too_short` (positive duration below one
day) or `invalid_duration` (missing/non-positive duration). `observed_days`
remains available for `too_short`; normalized values are then null. Invalid or
negative growth factors, overflow, or a 30-day growth factor at least `10^18`
make both normalized growth and return null. A zero growth factor maps to zero
growth and `-100%` return. A bad trade count affects only `trade_rate`.

Only growth/return and the trade rate are normalized. Fees stay raw because
their current denominator is the window's opening wallet; max drawdown, profit
factor, win rate, holding time and time in market are path- or ratio-dependent
raw metrics. The UI must always label those rows as not duration-normalized and
state that the 30-day values are a mathematical constant-rate equivalent of a
source window, not a forecast, tick test or MRS3 PnL.

Selection and XLSX use the same calendar duration for `PnL/30`, A/B PnL and
`Trades/30d`. Sparse trading never compresses that denominator to the event
span. Existing cached raw window facts remain reusable; this correction does
not require a cache-version bump or fact recalculation.

### Current-report import integrity

When a current Performance v2 HTML report declares them, the parser fails closed when the declared
`Total transactions (buy/sell)` differs from the number of parsed action rows,
or when the final wallet sample does not round to the declared `Final balance`
at that declared precision. The existing raw/semantic inventory comparison and
post-write DuckDB row-count verification remain mandatory. The final event or
equity timestamp is only required to fall inside the report interval; the
tester's declared `EndDate` is inclusive for imported event and equity samples,
so a timestamp exactly equal to that endpoint is valid. It is not required to
equal the report end.

Each interval requires `start < end`; A and B may be identical, overlapping,
nested or disjoint. Equity samples use the closed effective interval and
actions use `(effective_start, effective_end]`. A wholly out-of-range request
returns typed `OUT_OF_RANGE` data, not an HTTP error. Valid unavailable windows
remain normal result data. The window cache key is exactly
`(result_id, requested_start_utc, requested_end_utc, metrics_version)`.

The window request uses one short DuckDB transaction for both cache entries.
It returns typed `409` only for an actual writer lock or an unresolved cache
transaction conflict; it does not retry, wait, create a worker pool, or mutate
strategy/result/action/equity facts. The import request's legacy
`window_a`/`window_b` fields remain deprecated no-ops for compatibility;
`window_count` remains zero. DD5 proxy, filters, Pareto, XLSX, tags, RETEST,
Portfolio input, Runs UI and Fast remain out of this vertical.

## DD5 proxy

With UPNL sizing, linear lot/PnL scaling is path-dependent and is not a test
result. The unified analyzer exposes only `DD5_PROXY`:

```text
risk_scale = target_dd_pct / max_drawdown_pct
dd5_daily_log_return_proxy = daily_log_return * risk_scale
```

Scaled lots and the existing capital requirement proxy may remain diagnostics.
They require a new tester run before being treated as results.

## Ordered analysis and finalist selection

Feature calculation is fixed and independent of filter order: window metrics,
A/B deltas, holding, plateau summaries and DD5 proxies are calculated first.

The operator then supplies an ordered list of known built-in stages. Each stage
has an ID, enabled flag and typed parameters. No arbitrary expressions are
accepted. Existing holding/trade filters and Pareto variants are moved from the
standalone DD5 workflow into this registry. A/B deterioration is an ordinary
stage and can therefore run before or after Pareto stages. Its mode is either
`ANNOTATE` or `FILTER`.

`config.performance.json` contains `workers` (default `16`), A/B thresholds,
the enabled stage order and stage parameters. Parser processes and DuckDB query
threads use the same limit in separate phases; DuckDB publication keeps one
writer.

The accepted first Stage 2 delivery is deliberately stateless: the operator
submits the ordered built-in stages and receives one XLSX from current v2 facts.
It retains all requested Pair + Side candidates and the per-stage trace, but it
does not persist selection runs/results, tags, or a Portfolio Optimizer input.
The precise Stage 2 contract is
[Performance v2 finalist selection and XLSX](2026-08-31-performance-v2-finalist-selection.md).
Persisted `selection_runs`/`selection_results`, panel result history and the
Portfolio Optimizer input are Stage 3 work after the real XLSX rules are
accepted.

## Strategy lifecycle, tags and replacement

`strategy_tags(strategy_id, tag, source, created_at_utc)` supports manual and
analytic tags. `lifecycle_status` is separate from tags.

Discarding a strategy deletes its heavy current result, actions, equity,
window cache and result-dependent selection/DD5 facts, then sets
`lifecycle_status=DISCARDED`. Typed strategy/order/plateau facts and manual tags
remain so an already rejected strategy is not unknowingly tested again.

Adding a report creates a current result when absent. Replacing one requires an
explicit selected `strategy_id`; typed strategy parameters must match. The new
result is prepared first, then new rows are written, read back, switched to
current and old dependent rows removed in one transaction. Any failure rolls
back to the old result.

## RETEST handler

`RETEST` is a durable strategy tag, not a second queue table. The panel exposes
one action for all `ACTIVE` strategies tagged `RETEST`:

1. Read typed strategy and order parameters from Performance DB.
2. The panel requires an explicit test start and test end for the selected
   strategies. For every selected symbol it reads its listing date from
   `dates.xlsx` and rejects the request unless
   `listing_date <= test_start < test_end`. It renders one `run_snapshot` per
   selected strategy only after that validation, using the current tester
   template/profile.
3. Invoke the common RUNS testing algorithm.
4. Map completed reports back to `strategy_id` through the job's name mapping.
5. Build the same committed Performance inbox contract used by FAST; no
   RUNS-specific Performance manifest is introduced.
6. Replace each successful strategy result transactionally.
7. Remove `RETEST` only from successfully replaced strategies.
8. Retain `RETEST` and report an explicit reason for every failed/missing
   strategy so a later invocation retries only those rows.
9. Clean generated snapshots, copied strategies and reports only after their
   successful database replacement and guarded cleanup evidence.

Partial completion is valid and reported as `PARTIAL`. A failed strategy never
causes an already verified replacement to be repeated.

The next RUNS redesign must implement this common inbox boundary rather than a
new mode-specific import path.

## Throughput contract

Keep necessary trust-boundary validation, transaction rollback, zero quarantine
before cleanup and set-based readback. Remove repeated work:

- parse the inbox manifest once;
- pass shared commission/lineage context to workers once;
- preload existing result evidence in one query;
- never open DuckDB once per report;
- never issue existence/readback queries once per result;
- bulk append actions/equity and verify counts with grouped queries;
- insert plateau facts once per candidate evidence set.

## Acceptance evidence

Implementation is complete only when focused tests prove:

- 1ORD and 2ORD--4ORD strategies retain one plateau link per order and shared
  plateau rows are not duplicated;
- current HTML action fields are typed and missing `Post Size` is rejected;
- both arbitrary windows move independently to inward flat boundaries;
- UPNL comparison uses relative/geometric metrics, not absolute PnL;
- identical window requests hit persisted cache;
- filter/Pareto order changes deterministic survivors and leaves a full trace;
- panel and XLSX finalists match database selection results;
- discard removes heavy facts but retains strategy/order/plateau/tags;
- add and replace are atomic and rollback preserves the old result;
- RETEST success replaces the result and clears the tag, while partial failure
  keeps the tag only on failed strategies;
- preparation uses the configured default of 16 workers and database work is
  set-based with one writer.
