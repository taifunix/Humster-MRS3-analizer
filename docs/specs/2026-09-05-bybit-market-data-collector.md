# Bybit public market-data collector v1 — Revision 2

**Status:** Approved implementation contract. Runtime is being delivered on
`feat/bybit-market-data-collector`.

## Boundary

The future `src/mrs3/bybit_collector` is a public no-key Bybit `linear` service.
It has no MRS3, bot, order, position, balance, private-API, or trading access.
Its output is market-data evidence, not a portfolio/capacity/lot/MRS3-result claim.
Only minute aggregates, symbol events, daily instruments/risk data, and raw REST
JSON gzip persist. Raw WebSocket frames, snapshots/deltas, books, and five-second
samples are RAM-only, including diagnostics.

## Config and WebSocket

Only this UTF-8 TOML is user-configurable:

```toml
[storage]
root = "C:\\MRS\\data\\bybit_market_data"
[symbols]
items = ["AAOIUSDT", "AEHRUSDT"]
[logging]
level = "INFO"
```

Reload every 30 seconds. Invalid candidates retain the last accepted config;
`storage.root` is restart-only; valid symbol/log changes are atomic and do not
reconnect unaffected symbols. Bad new symbols are isolated. Add/remove/re-add
creates real gaps and a `symbol_events` row with event time, symbol, type,
reason, and config revision; no backfill exists.

One public `wss://stream.bybit.com/v5/public/linear` connection handles 20–30
symbols. Subscribe to `orderbook.1000.{SYMBOL}` in acknowledged batches below
Bybit's 21,000-character `args` cap. One 20-second ping and one pong/idle
reconnect loop operate for all symbols. No max-ten grouping, rebalance, or second
connection is v1 behavior. Multi-connection split is deferred until measured
lag, dropped-frame, or reconnect evidence requires a new decision.

Snapshot replaces the RAM book; delta inserts/updates; quantity zero deletes.
After accepted snapshot the book stays synchronised until explicit invalidation.
Silence alone is not stale. Invalidation is disconnect/reconnect, ping failure,
resubscribe, clock discontinuity, delta before snapshot, malformed/non-finite
update, non-increasing delta ID without snapshot, or impossible local state. It
forces a subscription which yields a new snapshot. A greater ID alone is not a
gap. `book_reset_count` increments once per accepted snapshot in that minute.

RPI orders are absent from public orderbook. `depth_*` is visible API depth, not
guaranteed full exchange liquidity. A complete ratio below one means the known
1000 levels did not reach that bps boundary for part of the minute.

## Sampling and liquidity_1m

UTC 5-second boundaries use wall time for buckets and monotonic time for waiting.
Suspend/clock discontinuity records missed boundaries, does not backfill,
invalidates the book, and reanchors the scheduler. A valid sample has a synced
book, non-empty finite positive bid/ask, and `best_bid < best_ask`.

`active_sample_target` counts boundaries in the actual active interval. Target
zero emits no row; every other minute emits one. `sample_count` counts attempts,
`valid_sample_count` its valid subset; missed boundaries increment neither.
`coverage_ratio=valid/target`, `ws_connected_ratio=connected/target`. No-valid
rows have coverage 0.0, measured connected ratio, and all market fields NULL.

`mid=(best_bid+best_ask)/2`; `spread_bps=(best_ask-best_bid)/mid*10000`.
Depth is approximate USDT `sum(price*quantity)` within 10/25/50/100 bps. All
valid samples enter depth quantiles; complete ratio is the share whose **both**
sides reach a band, or NULL with no valid sample. Sort values; with
`h=(n-1)*p`, `i=floor(h)`, `j=ceil(h)`, use x[i] if i=j else
x[i]+(h-i)*(x[j]-x[i]); n=0 is NULL and n=1 is x[0].

Fixed column order: `minute_ts_ms` INT64, `symbol` UTF-8, `sample_count` INT16,
`valid_sample_count` INT16, `coverage_ratio` FLOAT64, `book_reset_count` INT16,
`ws_connected_ratio` FLOAT64, `active_sample_target` INT16; nullable FLOAT64
`mid_median`, `spread_bps_median`, `spread_bps_p95`, `spread_bps_max`; then for
10/25/50/100 bps nullable FLOAT64 bid p05/median and ask p05/median USDT depth;
then nullable FLOAT64 four band complete ratios. Required metadata:
`schema_name=bybit_liquidity_1m`, `schema_version=1`, `exchange=bybit`,
`category=linear`, `collector_version`, `created_at_utc`.

Validator pins required metadata plus logical schema/order/types/nullability. It
ignores unknown future metadata, writer version, row groups, and compression layout,
so a DuckDB upgrade cannot invalidate existing files.

## SQLite, hourly archive, and recovery

`spool/collector.sqlite3` is durable truth: WAL, `synchronous=NORMAL`, unique
`(minute_ts_ms,symbol)`, short transactions. Exact duplicates are counted and
ignored; differing duplicates keep the first row and degrade health. BUSY/LOCKED
uses `busy_timeout=5000`, three attempts, then 100 ms/500 ms waits. Corrupt,
read-only, disk-full, or exhausted write error is fatal. After startup, only a
failed minute SQLite write exits `run`; WS/REST/export/validator/disk issues
degrade health.

Output is immutable `liquidity_1m/date=YYYY-MM-DD/part-HH.parquet`.
`published_hours(hour_start_ms PRIMARY KEY,file_name UNIQUE,row_count,
validated_at_ms)` is a small SQLite reader index, not a manifest. Consumers open
only files named by this index; raw globbing is not authoritative. Unmarked final
files are ignored. A marker proves structural publication, not liquidity coverage;
read coverage fields from the rows.

For UTC H=[H,H+1h), export is eligible at H+1h+120s. Startup and forward clock
reanchor enumerate eligible unmarked spool hours in ascending order; backward
clock change never exports early/re-exports marked hours. A late row for a marked
hour stays in spool, increments `late_rows_pending`, degrades health, and never
rewrites final.

Publication: read committed H rows; write UUID `.tmp` beside final; close/flush/
fsync; validate metadata/schema/count versus spool/unique key/range; publish via
no-clobber primitive (Windows MoveFileEx without replace; POSIX link/reservation);
fsync directory where supported (no-op documented on Windows); commit marker.
Precheck is only optimisation. Existing valid unmarked final gets marker; invalid
final stays untouched, is retried once/process, and remains health-degraded until
an operator moves it. Current-run tmp skipped for an existing final is deleted.
At startup delete only collector UUID tmp scratch older than one export cycle.
Never auto-delete Parquet, raw JSON, or spool.

Crash before final retries from spool; crash after final/before marker validates
then marks it; crash after marker does nothing. Marker without file is cleared,
health degrades, and catch-up re-exports it; readers skip/report it. There are no
manifests, hashes, quarantine directories, late archives, or export-state machine.
Retention is operator responsibility; health exposes free disk, spool bytes,
pending/late rows, and invalid finals.

## Reference data and operations

At startup, accepted add, and daily 00:10 UTC query symbol-specific public
`instruments-info`/`risk-limit` through cursor end. Save each response as
deterministic `raw_reference/date=.../<symbol>_<feed>_<time>_<page>.json.gz`, no
sidecar/hash. Daily immutable Parquet uses the same tmp/validate/no-clobber flow.

`instruments` fields: `captured_at_ms`, `symbol`, `status`, `symbol_type`,
`contract_type`, `launch_time_ms`, `settle_coin`, `tick_size`, `qty_step`,
`min_order_qty`, `max_order_qty`, `max_market_order_qty`, `min_notional_value`,
`min_leverage`, `max_leverage`, `leverage_step`, `funding_interval`,
`upper_funding_rate`, `lower_funding_rate`, `full_name`, `market_region`,
`underlying_ticker`; key `(captured_at_ms,symbol)`. `risk_limits`: `captured_at_ms`,
`symbol`, `risk_id`, `risk_limit_value`, `maintenance_margin`, `initial_margin`,
`max_leverage`, `mm_deduction`, `is_lowest_risk`; key `(captured_at_ms,symbol,risk_id)`.
Times are INT64, text UTF-8, numbers DECIMAL(38,18), flag BOOLEAN; unknown fields
remain only raw.

Every 10 minutes disk <10 GiB is WARNING, <2 GiB CRITICAL. Neither deletes data
or disconnects WS. Atomic `status/health.json` updates every 60 seconds. The
snapshot includes collector/start/update timestamps, config revision, configured
and active symbols, last completed minute, last exported date, free disk/spool
bytes, pending/late rows, connection state, and recent errors; late rows or
errors set status `DEGRADED` when disk thresholds are normal. Future commands are
exactly `run`, `validate-config`, `health`, `verify-archive`, each
with `--config PATH`; verify is read-only. Existing advisory `OutputDirectoryLock`
owns a root (Windows byte lock/POSIX flock); live peer exits 3, stale filename is
not a lock. Windows scripts create SYSTEM task `MRS_BybitMarketCollector` at boot
with a 30-second delay, restart-on-failure, no limit/no parallel, and project `.venv`.

## Revision 2 changelog

Removed: manifests/hashes/reconstruction (reader index + structural validator),
quarantine/late archive (retained spool + health), archive transaction machine
(one tmp/validate/no-clobber/marker flow), max-ten WS groups (one connection).
Unchanged: data boundaries, minute schema, hot reload, health/CLI/Windows, RPI.
