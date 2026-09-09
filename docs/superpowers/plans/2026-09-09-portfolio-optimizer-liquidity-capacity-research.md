# Portfolio Optimizer — coarse position capacity and candle backfill proposal

**Date:** 2026-09-09
**Status:** simplified design proposal; implementation requires approval

## Goal

Give the Optimizer a deliberately coarse maximum full-position/closing-order
estimate with a useful resolution of 50 USDT, warn about strategy shifts
comparable with the spread, and update the tester's existing Bybit minute files
before a test.

This is a screening tool. It does not model queue position, exact maker fills,
or dollar-accurate market impact.

## Existing data

The current collector already stores every minute:

- p05 and median bid/ask depth within 10/25/50/100 bps of mid;
- median/p95/maximum spread;
- coverage and book-depth completeness diagnostics.

The tester stores daily files at:

```text
<bot_root>/tester/data/bybit/<SYMBOL>/<SYMBOL>YYYY-MM-DD_1m.csv
```

with this schema:

```text
timestamp,open,high,low,close,volume,buy_volume,sell_volume,trades
```

Missing rows are minutes with no trades. The reader may restore a complete
minute grid in memory and use zero trade volume for those minutes; the files
remain sparse.

Bybit publishes the source trades as daily gzip files at:

```text
https://public.bybit.com/trading/<SYMBOL>/<SYMBOL>YYYY-MM-DD.csv.gz
```

The source contains timestamp, symbol, side, size and price for each trade. A
read-only check of `BABAUSDT2026-08-16.csv.gz` against the existing local file
produced the same 547 minute keys and zero mismatches in OHLC, total volume,
Buy/Sell volume and trade count. The local format is therefore a direct minute
aggregation of this official archive.

## Simplified position-capacity estimate

The strategy uses only limit orders. Its opening orders are `PostOnly`, closing
orders are ordinary `Limit`, and `allow_close_by_market_order` is false. Market
order capacity and `maxMarketOrderQty` therefore do not participate.

The liquidity cap applies to the fully accumulated directional position because
that position may become one closing Limit order. Opening orders are not given
the full cap independently.

The current tester has `LimitOrderVolumeCheck=true`. An empirical join of 1,377
report actions to exact local one-minute candles established that every fill is
bounded by the remaining **total** candle `volume`; several eligible orders for
the same symbol/minute share one budget. Directional `buy_volume` and
`sell_volume` do not set the tester's limit. The coarse estimator therefore uses
the same total volume:

```text
traded_minute_turnover = close * volume
clock_minute_turnover = traded_minute_turnover when a row exists, otherwise 0
mean_clock_minute_turnover_7d = mean(
    clock_minute_turnover for all 10,080 minutes in the last 7 calendar days
)
position_cap_raw_7d = (
    mean_clock_minute_turnover_7d
    * close_volume_participation_pct / 100
)
position_cap_7d = floor_to_50(
    min(position_cap_raw_7d, current_exchange_max_limit_notional)
)
```

The weekday analytical value uses the same formula over all 7,200 clock minutes
after excluding the configured weekend. The on-disk files are sparse by design:
a missing row inside a complete daily file means no trade and contributes zero
turnover. A missing or incomplete daily file is unavailable data and must be
backfilled or reported; it is not silently converted to zero. `traded_minute_ratio`
is retained as a separate activity diagnostic.

`close_volume_participation_pct` defaults to **30%**. The Settings screen allows
whole percentages from **1% through 200%**. Thirty percent is the operator's
initial calibration value, not a Bybit constant:

- BIS describes POV execution as an established method and gives 10%, 15% and
  20% as common examples selected according to urgency, size and volatility;
- Bitcoin metaorder evidence shows that impact depends on order size relative
  to volume and volatility, rather than a universal safe percentage;
- limit-order research likewise makes execution probability depend on order
  placement and the observed book.

The research supports participation-of-volume as the coarse variable but does
not establish 30% as universally safe. Values above the cited examples are
deliberately available for empirical calibration. At 100%, the full position
equals one complete average clock minute, including zero-trade minutes, before
rounding; values above 100% deliberately test positions requiring more than one
average minute of turnover. The tester supplies the final evidence and must flag
multi-period partial closing.

A read-only sensitivity check on five selected symbols found mean clock-minute
notionals of about 50–1,518 USDT over their latest available seven-day windows.
Thirty percent produces raw position caps of about 15–455 USDT, or 0–450 USDT
after round-down to 50 USDT. A zero rounded cap means that the configured model
does not admit even a 50 USDT position for that symbol. The source files
currently end on different dates, so these values are provisional and must be
recomputed after backfill.

The configured weekend defaults to Saturday 00:00 through Monday 00:00 UTC.
The MVP sizes from `position_cap_7d`; the weekday cap is analytics only. The
Portfolio still runs one normal seven-day test.

The current Bybit maximum limit-order quantity remains an upper bound. The final
displayed result is the smaller of the rounded candle-flow estimate and the
current exchange limit expressed in USDT.

An individual finalist has no fixed size. For the current adapter, the capacity
estimate supplies its single tested full-position size. Let
`L = sum(lot_x)`:

```text
base_notional = B_cap * scalar_pct / 100
opening_order_notional[i] = base_notional * lot_x[i]
full_directional_position = base_notional * L
closing_limit_notional = full_directional_position
tested_scalar_pct = 100 * position_cap / (B_cap * L)
```

`lot_x` values preserve the strategy's internal order proportions and are not
normalised to sum to one. For `lot_x = [0.5, 1.0, 1.5]` and a 600 USDT position
cap, `base_notional_max = 600 / 3 = 200`; the opening orders are 100, 200 and
300 USDT, the accumulated position is 600 USDT, and the maximum close is 600
USDT. The current adapter creates this one size after exchange round-down.

This intentionally supersedes the currently accepted per-opening-order ceiling.
That old formula can allow every opening order to pass while their accumulated
position and closing Limit exceed the intended cap. Implementation therefore
requires a new ADR, active-spec amendment, TDD changes in liquidity/search/render
and re-review of the affected M2 sizing contract. After exchange quantity
rounding, both accumulated position and closing quantity must be checked again.

This proposal does not claim that all orders can execute simultaneously; the
joint portfolio test and limiter remain responsible for combined exposure and
same-symbol competition.

The book archive does not set normal order size. It provides spread, PostOnly
cross/cancel risk, and an ordinary-Limit exit diagnostic. Only book minutes with
coverage at least 90% participate in those diagnostics. Book completeness is a
quality flag, not a hard gate. Less than seven days is `PRELIMINARY`; book data
older than two hours is `LIQUIDITY_STALE` and cannot be presented as a current
execution diagnostic.

## Deferred Phase 8: multi-size liquidity calibration

This section is not part of the current one-size adapter. It is retained as the
contract for a later phase after the first real joint portfolio path works.

The tester does not emit a dedicated liquidity-shortage flag. Its action table
does expose enough facts to recognize proven volume pressure without guessing
from PnL:

- `decreased` is a partial close;
- `closed` with `Post Size = 0` is flat;
- `Timestamp`, `Size`, `Order ID` and `Post Size` allow position-cycle
  reconstruction even when the closing Limit is replaced under a new order ID.

For every position cycle, sort actions by source order and record:

```text
close_execution_count
distinct_close_order_ids
first_close_fill_at
flat_at
first_fill_to_flat_seconds
close_fill_tf_buckets = count(distinct strategy-TF buckets with close fills)
```

The first close fill is the first proven touch. Time before it is market waiting
and must not be labelled a liquidity problem. `close_fill_tf_buckets > 1` is a
clear warning that the position did not close in the first touched strategy-TF
period.

A stronger, directly verifiable observation joins actions to the existing
one-minute candles:

```text
VOLUME_BOUND_PARTIAL when
    Post Size != 0
    and sum(all execution Size for symbol/minute) == candle.volume
    and another close execution occurs later
```

`LIQUIDITY_STRESSED` is set when at least one `VOLUME_BOUND_PARTIAL` is followed
by close fills in another strategy-TF bucket. Counting exact touches with no
fill is impossible from the current imported action subset because it lacks the
close order's active price interval. This is not needed for the MVP: fill
buckets are a reliable lower bound and proven volume exhaustion is stronger
evidence.

In Phase 8, for otherwise identical tested size variants, the Optimizer recommends the
largest smaller full-position size with no `LIQUIDITY_STRESSED` cycles. If every
tested size is stressed, it recommends lowering
`close_volume_participation_pct` and retesting. PnL and recovery-factor changes
are shown beside the execution evidence, but PnL alone never creates the flag.
This reuses the existing finite sizing variants; no second execution model or
capacity table is added.

## Simplified spread warning

Use the mean of the collector's minute p95 spread over the same window as a
conservative coarse reference:

```text
SPREAD_CLEAR   when every opening shift_bp > average_p95_spread_bp
SPREAD_OVERLAP otherwise
```

This is explicitly a heuristic: shift is measured from the strategy MA while
spread is measured around current bid/ask. It is sufficient for first-pass
screening and avoids reimplementing the tester's exact MA engine. A PostOnly
order that crosses the opposite quote is cancelled by the exchange; an ordinary
Limit may execute immediately but never beyond its explicit limit price.

Inside one symbol and side:

1. when at least one finalist is `SPREAD_CLEAR`, remove `SPREAD_OVERLAP`
   finalists before individual ranking;
2. when none is clear, keep them and show a prominent spread warning;
3. while liquidity history is `PRELIMINARY`, show the warning but do not remove
   a strategy.

If this heuristic produces questionable results in practice, the later upgrade
is to reconstruct exact order prices from tester candles. It is intentionally
outside MVP.

## Minute-file backfill before a test

Before generating or submitting a tester batch, the Optimizer determines the
selected symbols and complete UTC dates required by the test window.

For every missing daily `_1m.csv`:

1. download the matching official `csv.gz` into a temporary file;
2. stream it with the Python standard library and group trades by UTC minute;
3. map Bybit `Buy` to `buy_volume` and `Sell` to `sell_volume`;
4. write the existing nine-column sparse CSV format to a temporary sibling;
5. validate symbol/date, ordered unique minute timestamps, OHLC, non-negative
   values, `volume = buy_volume + sell_volume`, and positive `trades`;
6. atomically rename it to the final tester path.

Existing files are never rewritten. A malformed existing file or unavailable
required archive blocks the test with the exact symbol/date. Current incomplete
UTC day is not downloaded; a test may end no later than the last completed UTC
day. Independent symbol/day downloads use at most 16 workers.

No dependency and no separate candle store are required.

## Future live control compatibility

The estimator result for every symbol and side persists:

- the unrounded and rounded caps;
- `calculated_at`, latest source timestamp and freshness;
- exact source window and available-day count;
- coverage/completeness and `PRELIMINARY/READY/STALE` state;
- current desired order size, participation ratio, headroom and limiting reason;
- input and policy digests.

The immutable minute book archive remains the source of history. A later live
phase recalculates the two smoothed 7-calendar-day and 5-weekday averages once
per UTC day and maps the selected daily result to `KEEP`, `REDUCE` or `DISABLE`.
It does not react to an individual minute. Live action, bot integration and
decision thresholds are not implemented in this MVP; they can be added without
changing the collector or the stored estimator result.

## Settings exposed in the existing Optimizer JSON and Settings screen

- `liquidity.parameters.close_volume_participation_pct`, integer `1..200`,
  default `30`;
- `liquidity.round_down_usdt`, default `50`;
- `liquidity.minimum_coverage_pct`, default `90`;
- `liquidity.maximum_age_hours`, default `2`;
- `liquidity.weekend_start_utc`, default `SATURDAY 00:00`;
- `liquidity.weekend_end_utc`, default `MONDAY 00:00`;
- `inputs.bybit_minute_data_root`, default from
  `<tester_runner.bot_root>/tester/data/bybit`.

The seven-day window is an implementation contract value, not an additional
user control.

## Research references

- [BIS: FX execution algorithms and market functioning](https://www.bis.org/publ/mktc13.pdf)
  describes POV execution and 10%, 15% and 20% participation examples.
- [A million metaorder analysis of market impact on Bitcoin](https://arxiv.org/abs/1412.4503)
  finds volume/volatility-normalised square-root impact and material variation
  around the average relation.
- [Optimal trade execution in cryptocurrency markets](https://link.springer.com/article/10.1007/s42521-023-00103-y)
  shows that passive limit-order execution depends on price level, queue depth
  and empirical execution probability.
- [Bybit recent public trades](https://bybit-exchange.github.io/docs/v5/market/recent-trade)
  defines `side` as taker side and links the historical archive used for local
  candle reconstruction.

## Acceptance evidence

- The official BABA fixture aggregates to the checked local minute file.
- Missing zero-trade minutes remain absent on disk and become zeros only in an
  in-memory complete grid where needed.
- Existing daily files are skipped and cannot be overwritten by a download.
- Failed download, gzip, aggregation or validation leaves no final or partial
  tester file.
- A pre-test backfill fills every missing completed UTC day required by the
  selected campaign or blocks with symbol/date details.
- Capacity uses actual total candle turnover, a configurable participation
  rate, arithmetic mean and round down to 50 USDT; 7D and weekday values are
  independently reproducible.
- The cap bounds the fully accumulated position and maximum closing Limit;
  opening-order notionals sum to no more than that cap after exchange rounding.
- The deferred Phase 8 reports close-fill TF buckets and proven volume-bound
  partial closes while keeping pre-first-fill waiting separate.
- The deferred Phase 8 may recommend the largest already-tested smaller clean
  size or lowering the configured participation percentage when none is clean.
- Partial collector history is visibly `PRELIMINARY`; data older than two hours
  cannot support current spread or Limit-exit diagnostics.
- Spread overlap is a warning on preliminary data and becomes a finalist filter
  only with seven complete days.
- The persisted result contains enough raw cap, freshness, quality and lineage
  facts for a future once-daily, 5–7-day-smoothed live-control policy without a
  collector migration.
