# Strategy Performance DuckDB v1

**Status:** Approved for implementation
**Date:** 2026-08-14

## 1. Goal

Create a durable source of truth for completed MRS3 tester results. The system
must parse every verified tester HTML report, retain complete strategy settings,
metrics, actions and wallet/equity series, calculate DD5 from the imported data,
and delete source HTML only after a verified database commit.

The database will also become the backtest input boundary for the future
Portfolio Analyzer and for later live-versus-backtest comparisons.

## 2. Scope

This feature includes:

- a separate local DuckDB database for MRS3 performance evidence;
- immutable capture of each name-verified tester HTML before tester cleanup;
- full HTML parsing when the user starts DD5;
- transactional import, deduplication, conflict detection and quarantine;
- typed core metrics plus lossless raw settings, metric and action payloads;
- timestamped trade actions and wallet/equity samples;
- DD5 runs and DD5 results stored in the same database;
- human-readable XLSX output generated from database records;
- deletion of imported HTML only after explicit `safe_to_delete=YES` evidence;
- a clean retest and import of the current 476 non-5m ONUSDT strategies.

## 3. Non-goals

- Do not mix MRS3 tester results with the existing MRS2 source DuckDB.
- Do not use XLSX or CSV as the durable source of truth.
- Do not implement Portfolio Analyzer simulation in this feature.
- Do not define the live-account ingestion schema before a real live export is
  available.
- Do not include tester version or market-data hash in test identity.
- Do not infer missing commission values, metrics or trades.
- Do not make DD5 a second tick-test. It remains a calculated normalization of
  imported real tester results.

## 4. Storage Boundary

The local ignored database path is:

```text
data/databases/strategy_performance.duckdb
```

The tester inbox is:

```text
data/tester_inbox/<batch_id>/
  inbox_manifest.json
  import_audit.v4.json
  html_delete_checklist.v4.csv
  strategies/<strategy_version_id>.json
  reports/<manifest_entry_id>.html
```

The inbox is temporary durable evidence between tester completion and database
import. Bot-owned report paths are never treated as durable storage.

The existing MRS2 source DuckDB and analysis DuckDB remain separate databases
with separate schemas and provenance.

## 5. Identity

### 5.1 Strategy version

`strategy_version_id` is the SHA-256 of canonical strategy JSON. Canonical JSON
uses sorted keys, UTF-8 and compact separators. The strategy name is descriptive
and is not the identity.

The top-level strategy `exchange.name` and manifest `exchange_name` are
required, must match after case normalization, and are the exchange source of
truth. Missing or conflicting source exchange data is a contract error. If HTML
also exposes the exchange, its name must either match the source after case
normalization or equal the exact case-normalized Hamster Bot runtime marker
`tester`. For the marker form only `exchange.name` is substituted with the
source name before canonical comparison; every other HTML settings field remains
an exact canonical match.

### 5.2 Commission contract

The effective tester commission contract is required because the test cannot
run reproducibly without it. The runner must snapshot the configured
the tester config once per batch and extract `MakerFee`, `TakerFee`,
`SlippagePercent`, `FundingRate` and `FundingIntervalHours` from that immutable
copy. Both the legacy nested form (`{"tester_config": {...}}`) and the actual
Hamster Bot flat form (`{...commission fields...}`) are accepted; when the
nested key is present it remains authoritative and must be an object. The
canonical object is stored and hashed as `commission_contract_id`.
Missing, non-finite or substituted commission evidence quarantines the batch.
No default and no `UNKNOWN` value are permitted.

Commission belongs to the backtest run. It is part of strategy identity only
when the same fields are physically present in canonical strategy JSON.

### 5.3 Backtest identity

`test_run_id` is SHA-256 of one canonical compact JSON object with these keys:

```text
strategy_version_id
+ period_start_utc_ms
+ period_end_utc_ms
+ exchange_normalized
+ commission_contract_id
```

Periods use UTC and half-open `[start, end)` bounds. Timestamps in the hash
preimage are integer Unix milliseconds. Exchange normalization is Unicode trim
plus `casefold()`.

Tester version and market-data hash are deliberately excluded by product
decision.

### 5.4 Duplicate and conflict behavior

The importer calculates `result_payload_sha256` from canonical parsed settings,
period, metrics, actions and wallet/equity samples.

- Same `test_run_id` and same `result_payload_sha256`: `SKIPPED_IDENTICAL`.
- Same `test_run_id` and different `result_payload_sha256`:
  `IDENTITY_CONFLICT`; the import fails and the HTML is retained.
- Different strategy settings or period: a new immutable backtest record.

No existing backtest result is overwritten.

## 6. Schema v1

### 6.1 Metadata and import audit

`schema_info`

- `key VARCHAR PRIMARY KEY`
- `value VARCHAR NOT NULL`

Required entries: `schema_version=1`, `database_kind=strategy_performance`,
`import_evidence_schema_version=4`. Database schema version and deletion
evidence schema version are independent.

`import_runs`

- `import_id VARCHAR PRIMARY KEY`
- `batch_id VARCHAR NOT NULL`
- `started_at_utc TIMESTAMPTZ NOT NULL`
- `finished_at_utc TIMESTAMPTZ`
- `expected_report_count INTEGER NOT NULL`
- `imported_count INTEGER NOT NULL`
- `skipped_count INTEGER NOT NULL`
- `quarantined_count INTEGER NOT NULL`
- `status VARCHAR NOT NULL`
- `manifest_json VARCHAR NOT NULL`

`import_files`

- `import_id VARCHAR NOT NULL`
- `manifest_entry_id VARCHAR NOT NULL`
- `strategy_version_id VARCHAR NOT NULL`
- `strategy_name VARCHAR NOT NULL`
- `source_filename VARCHAR NOT NULL`
- `source_html_sha256 VARCHAR NOT NULL`
- `source_size BIGINT NOT NULL`
- `test_run_id VARCHAR`
- `action_count INTEGER`
- `equity_sample_count INTEGER`
- `status VARCHAR NOT NULL`
- `error_classification VARCHAR`
- `error_message VARCHAR`
- `safe_to_delete BOOLEAN NOT NULL`
- `cleanup_state VARCHAR NOT NULL`
- `deleted_at_utc TIMESTAMPTZ`
- primary key: `(import_id, manifest_entry_id)`

`cleanup_state` is one of `RETAIN`, `DELETE_READY`, `DELETING`, `DELETED`.
Cleanup updates each file independently and is idempotent after a crash.

`import_audit.v4.json` and `html_delete_checklist.v4.csv` are durable sidecars
inside the inbox. They are written atomically even when parsing fails before a
DuckDB transaction or when the transaction rolls back. Their schema version is
4 and they record every expected manifest entry, source hash, structural
inventory, status, quarantine reason, cleanup state and deletion result.

### 6.2 Strategy catalog

`strategy_versions`

- `strategy_version_id VARCHAR PRIMARY KEY`
- `strategy_name VARCHAR NOT NULL`
- `symbol VARCHAR NOT NULL`
- `side VARCHAR NOT NULL`
- `timeframe VARCHAR NOT NULL`
- `settings_json VARCHAR NOT NULL`
- `first_seen_at_utc TIMESTAMPTZ NOT NULL`

Canonical settings from the inbox JSON and embedded HTML settings must be
identical, subject only to the `tester` exchange runtime-marker rule in section
5.1. A mismatch quarantines the report.

### 6.3 Backtest facts

`backtest_runs`

- `test_run_id VARCHAR PRIMARY KEY`
- `strategy_version_id VARCHAR NOT NULL`
- `period_start_utc TIMESTAMPTZ NOT NULL`
- `period_end_utc TIMESTAMPTZ NOT NULL`
- `exchange VARCHAR NOT NULL`
- `commission_contract_id VARCHAR NOT NULL`
- `commission_json VARCHAR NOT NULL`
- `initial_balance DECIMAL(38,12) NOT NULL`
- `source_html_sha256 VARCHAR NOT NULL`
- `result_payload_sha256 VARCHAR NOT NULL`
- `import_id VARCHAR NOT NULL`
- `imported_at_utc TIMESTAMPTZ NOT NULL`

`backtest_metrics`

- `test_run_id VARCHAR PRIMARY KEY`
- required `DECIMAL(38,12)` fields: `final_balance`, `total_pnl`,
   `total_pnl_pct`, `max_drawdown`, `max_drawdown_pct`, `total_fees`,
   `win_rate_pct`, `days_in_test`;
- nullable `DECIMAL(38,12)` field: `profit_factor`, with required
  `profit_factor_status VARCHAR`;
- required integer fields: `total_trades`, `win_trades`, `loss_trades`;
- nullable `DECIMAL(38,12)` diagnostics: `gross_profit`, `gross_loss`,
  `trading_volume_usdt`, `funding_net`, `funding_received`, `funding_paid`,
  `expectancy_per_trade`, `position_avg_pct`, `position_max_pct`,
  `risk_reward`, `recovery_factor`, `months_in_test`, `months_with_data`;
- nullable integer diagnostics: `total_transactions`, `pairs_count`;

The required Profit Factor label accepts only `Profit Factor` or `Profit Factor
(gross profit/gross loss)`. The stored field name remains `profit_factor`. The
exact raw value `n/a` means gross loss is zero: it is retained in `metrics_json`,
stores typed `profit_factor=NULL` with
`profit_factor_status=UNDEFINED_GROSS_LOSS_ZERO`, and is never converted to a
numeric default or infinity. A missing, other non-numeric or non-finite value
quarantines the report. `AVAILABLE` is the only numeric status.
- nullable text diagnostic: `report_range`;
- `metrics_json VARCHAR NOT NULL` containing the complete canonical metric map.

`initial_balance` remains required on `backtest_runs`. Any missing or
non-finite required metric quarantines the report. Optional diagnostics may be
NULL only when the HTML does not expose that metric; the reason is recorded in
the import audit. `profit_factor` is the sole typed DD5 diagnostic allowed to
be NULL under the explicit status contract above; DD5 PnL/DD calculation and
ranking do not use it, and exports mark it unavailable.

Canonical typed performance metrics come from immutable series evidence, not
the rounded HTML Metric/Value summary: `final_balance`, `total_pnl` and
`total_pnl_pct` use the last `wallet` sample and the persisted initial balance;
`max_drawdown` is the greatest monetary drawdown in the ordered `equity`
series; and `max_drawdown_pct` divides that same drawdown by its peak equity.
`win_rate_pct` is `win_trades / total_trades * 100`. Empty, mismatched,
non-finite or non-monotonic series, invalid trade counts, or disagreement with
the declared report precision quarantine the report. `metrics_json` retains the
complete HTML summary as diagnostic evidence only.

New HTML metrics are retained in `metrics_json` even before a typed column is
added in a later schema migration.

`backtest_actions`

- `test_run_id VARCHAR NOT NULL`
- `action_index INTEGER NOT NULL`
- `timestamp_utc TIMESTAMPTZ NOT NULL`
- typed columns for Symbol, Action, position side, price, quantity, PnL, fee and
  balance when present;
- `raw_action_json VARCHAR NOT NULL` containing the complete canonical source
  row;
- primary key: `(test_run_id, action_index)`.

`backtest_equity`

- `test_run_id VARCHAR NOT NULL`
- `sample_index INTEGER NOT NULL`
- `timestamp_utc TIMESTAMPTZ NOT NULL`
- `wallet DECIMAL(38,12) NOT NULL`
- `equity DECIMAL(38,12) NOT NULL`
- primary key: `(test_run_id, sample_index)`.

Normalized actions and equity samples are intentional. DuckDB columnar storage
keeps them queryable for portfolio and period comparisons without custom BLOB
decoders. The existing compact MRS2 payload schema is not reused.

### 6.4 DD5 evidence

`dd5_runs`

- `dd5_run_id VARCHAR PRIMARY KEY`
- `import_id VARCHAR NOT NULL`
- `created_at_utc TIMESTAMPTZ NOT NULL`
- `target_dd_pct DECIMAL(18,8) NOT NULL`
- `config_json VARCHAR NOT NULL`
- `input_test_count INTEGER NOT NULL`
- `status VARCHAR NOT NULL`

`dd5_results`

- `dd5_run_id VARCHAR NOT NULL`
- `test_run_id VARCHAR NOT NULL`
- typed calculated fields use the `projected_` prefix, including
  `projected_pnl_dd5`, `projected_dd_pct`, `projected_pnl30_dd5`, scaled lots,
  capital proxy, holding, filter, Pareto and ranking fields;
- primary key: `(dd5_run_id, test_run_id)`.

For each imported run, DD5 uses the existing calculated comparison contract:
`dd5_scale = target_dd_pct / max_drawdown_pct`,
`projected_pnl_dd5 = total_pnl_pct * dd5_scale`,
`projected_dd_pct = max_drawdown_pct * dd5_scale`,
`pnl30_dd5 = projected_pnl_dd5 * 30 / days_in_test`, and
`capital_requirement_proxy = sum(scaled_lots) + projected_dd_pct / 100`.
The result is labelled `CALCULATION_ONLY`; it is never a DD5 tick-test.

Useful read-only views are part of schema v1:

- `latest_backtest_by_strategy_version`;
- `dd5_latest_results`;
- `portfolio_layer_a_input`.

`portfolio_layer_a_input` exposes only analytic Layer A facts and a
`portfolio_event_ready=FALSE` flag in schema v1. It must not be consumed by the
Layer B simulator until a separate event contract defines signal, order,
pending/cancel, fill, position ID, event ordering, limiter and margin semantics.

## 7. Runner Capture Contract

Runner result verification remains lightweight:

1. Require one wizard entry for the strategy.
2. Require a stable HTML report.
3. Read the embedded strategy name from the stable report bytes and compare it
   exactly.
4. Write those exact bytes to `reports/<manifest_entry_id>.html` through a
   temporary file and atomic rename; calculate the manifest hash from those
   copied bytes, not the bot-owned path.
5. Copy the immutable strategy JSON to
   `strategies/<strategy_version_id>.json` through the same atomic procedure.
6. Copy the batch `tester_config` before the first submission and record its
   SHA-256 plus the required commission contract in `inbox_manifest.json`.
7. Record unique `manifest_entry_id`, strategy name, strategy version ID,
   wizard run ID, report and strategy hashes in the manifest.

The runner may clean bot-owned report files after every verified inbox copy.
It must not delete the inbox or claim the batch ready for DD5 until every
expected strategy has one unique report and one unique strategy JSON.

The runner CSV remains a progress/export artifact. It is not DD5 input and does
not need to contain parsed HTML metrics, settings or trades.

### 7.1 Full-parse structural inventory

Before semantic extraction, the DD5 importer independently inventories each
immutable HTML snapshot. It requires exactly one complete strategy JSON object,
at least one `Metric`/`Value` table with non-conflicting metric keys, exactly one
trade table with required `Timestamp`, `Symbol`, `Action` and `PnL` headers, and
exactly one `walletSeries` plus one `equitySeries` JavaScript array. It records
raw table headers, metric count, trade-row count, wallet/equity sample counts and
their minimum/maximum UTC timestamps in the v4 audit sidecar.

The semantic parser must reproduce the inventory counts exactly. Trade
timestamps accept ISO-8601 UTC/offset values or the tester's exact
`YYYY-MM-DD HH:MM:SS` representation, which is interpreted as UTC. Other
timezone-less or locale-dependent formats are invalid. Series timestamps must
be valid UTC instants; series must be non-empty and
strictly increasing. A missing, duplicate, malformed or unrecognised mandatory
section is `STRUCTURAL_QUARANTINE`; it cannot be imported or deleted.

## 8. DD5 Import Workflow

Pressing `Calculate DD5` performs these phases:

1. `PREFLIGHT`: load the completed runner state and inbox manifest; require an
   exact expected-name set and matching file hashes.
2. `PARSING`: inventory and parse reports read-only with a bounded
   `ProcessPoolExecutor`. The coordinator submits independent entries in
   manifest order and collects completed outcomes by manifest index before any
   identity decision. Workers receive immutable inbox paths or report bytes,
   never open DuckDB, and never delete HTML or mutate audit/checklist. Parse
   full settings, every metric, every action and complete wallet/equity series
   from the immutable copied bytes.
3. `VALIDATING`: compare embedded settings with strategy JSON, validate the
   required strategy exchange and captured commission contract, periods,
   timestamps, numeric values, inventory counts and unique identities.
4. `STAGING`: prepare all database rows without changing the published database.
5. `COMMITTING`: use one DuckDB writer and one transaction for the whole import.
6. `READBACK`: within the same publication transaction, read the staged rows
   and verify report, action and equity counts plus payload hashes before the
   coordinator commits.
7. `CALCULATING_DD5`: calculate DD5 only from committed database rows and write
   `dd5_runs`/`dd5_results` transactionally.
8. `EXPORTING`: generate `posttest.xlsx` and a small JSON manifest from the DD5
   database records. CSV export is optional and user-triggered.
9. `CLEANUP`: only after the whole batch has `schema_version=4` evidence,
   zero quarantines and successful readback, atomically mark each file
   `DELETE_READY`, then `DELETING`, delete it, and finally mark `DELETED`.

Any parse, validation, conflict, database or readback failure prevents DD5 and
prevents HTML deletion. The v4 audit sidecar records the failure even when the
database transaction rolls back. Successfully imported duplicates may be deleted
only when readback confirms their existing identical payload. Cleanup resumes
idempotently after a process crash: `DELETING` is rechecked by file hash and
either returned to `DELETE_READY` or completed as `DELETED`.

### 8.1 Parallel Preparation and Bulk Publication

- The default preparation worker count is `4`; the safe documented maximum is
  `8`. Explicit worker counts come from the existing `duckdb_import` settings
  and are clamped by the import coordinator; workers below `1` are rejected.
- Preparation is deterministic: each manifest entry is submitted with its
  manifest index, and completed outcomes are buffered and sorted by that index
  before identity/conflict decisions. A different worker completion order must
  not change database rows, audit entries, checklist rows or results.
- There is exactly one DuckDB publication writer and one coordinator
  transaction. `backtest_actions` and `backtest_equity` are written with bulk
  append/register operations in bounded chunks, not one `execute` per row.
  Schema precision, typed columns and readback validation are unchanged.
- Idempotent supplements may avoid a full HTML parse only when committed
  `import_files`/`backtest_runs` evidence proves the same manifest entry,
  source report hash/size, strategy version and payload hash. New or changed
  entries always receive full validation; conflicting identities fail closed
  and unknown reports are never skipped silently.
- Progress events expose stage, prepared/scheduled, imported/skipped,
  quarantined, elapsed phase seconds and total counters. CLI/panel journals
  throttle duplicate preparation events; terminal stage failures are always
  emitted. Result and audit evidence include phase timing/counts without local
  absolute paths.
- Cancellation or any preparation/write/readback failure leaves every inbox
  HTML intact, preserves audit/checklist safety rules, rolls back the single
  publication transaction and does not mark anything `DELETE_READY`.

## 9. XLSX and CSV Contract

`posttest.xlsx` remains the default human report. It contains summaries,
finalists, normalized results, comparison results, holding cycles and import
audit references. It is reproducible from `dd5_run_id` and may be regenerated.

CSV files are not produced as the primary DD5 result. The panel may expose an
explicit export action for selected database views when interoperability is
needed. Parquet export is deferred until data must be exchanged between
machines or processed outside DuckDB.

## 10. Future Live Comparison Boundary

Schema v1 does not invent live tables without a real payload. A later schema
adds `live_periods`, `live_trades` and `live_metrics`, linked by
`strategy_version_id`. Backtest rows remain immutable.

Weekly comparison will align live and backtest windows and compare PnL, DD,
trade frequency, holding, fees and execution differences. Portfolio Analyzer
consumes `portfolio_layer_a_input` and timestamped actions/equity, but its
limiter, L2 and margin contracts remain separate prerequisites.

## 11. Current 476-Strategy Rerun

The existing 476 name-only results cannot be upgraded because their HTML and
snapshots have already been deleted. Their CSV and DD5 export remain historical
artifacts and must not be imported as complete evidence.

After implementation:

1. Preserve the existing results and DD5 output unchanged.
2. Run `tester-plan` against the exact immutable 476-strategy non-5m source.
3. Use a new output stem and a new inbox `batch_id`.
4. Retest all 476 strategies; old name-only CSV rows are not reusable for this
   new evidence contract.
5. Require inbox completeness `476/476` before DD5.
6. Import all 476 reports, require zero quarantine and verified readback.
7. Calculate DD5 from DuckDB.
8. Delete HTML only after `safe_to_delete=YES`.

## 12. Invariants

- One database contains only MRS3 performance evidence, never MRS2 source rows.
- Strategy settings are content-addressed and immutable.
- A test identity is never overwritten.
- Same identity with different parsed payload is a hard conflict.
- All parsed source fields remain recoverable from typed columns or canonical
  raw JSON.
- DD5 reads committed database evidence, never HTML or runner CSV directly.
- A report is deleted only after committed readback evidence.
- Quarantine count must be zero for DD5 calculation.
- Source metrics are never presented as MRS3 tester results.
- Live results never overwrite backtest results.

## 13. Acceptance Evidence

Implementation is complete only when automated and real-data evidence proves:

1. Schema initialization/migration is deterministic and rejects an unknown
   schema version.
2. A representative HTML round-trips settings, all metrics, actions and
   wallet/equity values without loss.
3. Identical imports skip; conflicting imports fail without changing the DB.
4. Any malformed report produces quarantine and keeps every inbox HTML.
5. Successful readback produces `safe_to_delete=YES` before cleanup.
6. DD5 results generated from DuckDB match a fixture's expected calculation.
7. XLSX can be regenerated from a stored `dd5_run_id` without HTML or CSV.
8. Portfolio input view exposes strategy identity, periods, DD5 lots, metrics
   and timestamped event availability.
9. The real non-5m ONUSDT batch imports `476/476`, quarantine is `0`, DD5 is
   calculated, and all deleted HTML hashes exist in committed import evidence.

## 14. Required Dependencies

- Current runner name-only verification and collision retry contract.
- Existing immutable 476-strategy source batch.
- Existing HTML parser/compact importer code may be reused for parsing and
  validation, but its MRS2 database schema is not reused.
- Portfolio contracts remain governed by
  `docs/specs/2026-08-09-portfolio-analyzer-v04.md`.
