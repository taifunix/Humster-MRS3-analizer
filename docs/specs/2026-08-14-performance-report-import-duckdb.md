# Performance Report Import to DuckDB

**Status:** Active implementation contract
**Date:** 2026-08-14
**Governing specification:** [Strategy performance DuckDB](2026-08-14-strategy-performance-duckdb.md)
**Related ADRs:** [ADR-0004](../decisions/0004-strategy-performance-evidence-store.md), [ADR-0005](../decisions/0005-tester-runtime-exchange-marker.md), [ADR-0006](../decisions/0006-undefined-profit-factor-contract.md)

## 1. Purpose

Import completed Hamster Bot MRS3 reports into the separate local Performance
DuckDB as immutable, queryable tester evidence. The importer is the only path
that turns copied HTML, strategy JSON, commission settings, action rows and
wallet/equity series into typed performance facts.

The importer must be reproducible, fail closed and append-only. A successful
import makes DD5 calculation possible; it does not prove that a scaled DD5
strategy has passed another tick-test.

## 2. Scope and Non-Goals

In scope:

- completed inbox validation and import into `strategy_performance.duckdb`;
- structural HTML parsing, immutable strategy/commission evidence, canonical
  tester metrics, actions and wallet/equity series;
- deterministic idempotent supplements, transactional publication, readback,
  progress and safe cleanup eligibility.

Out of scope:

- live monitoring HTML parsing; the runner remains name-only there;
- rewriting tester reports, strategy JSON or bot-owned files;
- MRS2 source database changes;
- portfolio simulation, live-trade ingestion, retesting DD5-scaled JSON or
  treating DD5 output as a tick-test.

## 3. Inputs and Inbox Contract

The panel and CLI receive one completed directory
`data/tester_inbox/<batch_id>/`, not the inbox root. It must contain:

- `inbox_manifest.json` with `schema_version`, `batch_id`, the commission
  contract and one entry per expected strategy;
- copied report bytes under each entry `report_path`;
- copied immutable strategy JSON under each entry `strategy_path`.

Every manifest entry includes an opaque ID, strategy name/version ID, wizard
run ID, source report and strategy SHA-256 values, paths and source exchange.
The manifest commission contract requires MakerFee, TakerFee, SlippagePercent,
FundingRate and FundingIntervalHours. Missing or malformed required evidence
rejects the entire operation before publication.

The panel validates that the selected directory itself contains
`inbox_manifest.json`. It must never accept `data/tester_inbox` when the
manifest belongs to a child batch.

## 4. Validation and Parse Sequence

For each manifest entry, in manifest order:

1. Resolve both relative paths under the inbox and reject traversal or a missing
   file.
2. Hash report and strategy bytes and compare them with the manifest.
3. Decode the canonical strategy JSON; require a nonempty strategy name,
   required exchange and valid MRS3 active order list.
4. Parse the copied HTML structurally. Require one complete embedded settings
   object, Metric/Value tables with consistent unique headers, one actions
   table and exactly one wallet/equity series pair.
5. Compare settings with strategy JSON. The only permitted runtime difference is
   `exchange.name=tester`, projected back to the immutable strategy exchange as
   defined by ADR-0005.
6. Require ordered, finite, positive wallet/equity samples with equal counts;
   timestamps must be strictly increasing UTC instants. Tester timestamps in
   `YYYY-MM-DD HH:MM:SS` are UTC, not local time.
7. Validate action rows, timestamps and numeric fields. All persisted timestamps
   are timezone-aware UTC values.
8. Validate required metrics, trade counts and the canonical metric derivation
   below. Fractional `Total Trades`, `Win Trades` or `Loss Trades` are invalid;
   values are never truncated.
9. Calculate identity and payload hashes, then perform idempotency/conflict
   checks only after the complete candidate has been prepared.

Any failure produces quarantined audit evidence, leaves all inbox bytes intact
and prevents a successful DD5 run for the batch.

## 5. Canonical Metric Contract

The HTML Metric/Value summary is retained losslessly in `metrics_json`, but it
is rounded diagnostic evidence. Typed canonical values come from immutable
series/count evidence:

```text
final_balance = wallet[-1]
total_pnl = final_balance - initial_balance
total_pnl_pct = total_pnl / initial_balance * 100

max_drawdown = max(peak_equity - equity[i])
peak_at_max_drawdown = peak_equity at the same sample
max_drawdown_pct = max_drawdown / peak_at_max_drawdown * 100

win_rate_pct = win_trades / total_trades * 100
```

The maximum percentage drawdown from another sample is not substituted for
`max_drawdown_pct`. Canonical values must agree with declared report values at
the declared decimal precision, except that tester display fields are compared
using the nearest-unit display interval (an inclusive half-unit tolerance):
absolute `Total PnL` and `Max Drawdown` use one unit; relative `Total PnL, %`
and `Max Drawdown, %` use 0.1 percentage point. These tolerances apply only to
admission; the precise series-derived values remain stored. An empty,
mismatched, non-finite or non-monotonic series, nonpositive initial balance,
non-integral count, or declared mismatch outside its interval quarantines the
report.

`Profit Factor (gross profit/gross loss)=n/a` is the only permitted unavailable
typed metric: store `profit_factor=NULL` and
`profit_factor_status=UNDEFINED_GROSS_LOSS_ZERO`; never invent zero or infinity.

## 6. Identity, Idempotency and Conflict Rules

`test_run_id` is SHA-256 over canonical JSON of:

- strategy version ID;
- UTC `[period_start, period_end)` milliseconds;
- normalized source exchange;
- commission contract ID.

The strategy version ID is SHA-256 over canonical strategy JSON. A previous
committed row may be skipped only if manifest entry, report hash/size, strategy
version and complete payload hash all match. Same identity with a different
payload is a hard conflict. Unknown or partially matching evidence is never
silently skipped.

## 7. Persistence and Transaction Boundary

One coordinator owns the sole DuckDB writer. It publishes all prepared entries
in one transaction to immutable tables:

- `import_runs` and `import_files` for batch and per-file state;
- `strategy_versions`, `backtest_runs` and `backtest_metrics` for typed facts;
- `backtest_actions` and `backtest_equity` for timestamped evidence;
- audit/checklist records for every prepared, skipped or quarantined entry.

Action and equity rows are bulk-written in bounded chunks. Worker processes
never open DuckDB. Before commit, readback verifies report/action/equity counts,
payload hashes and expected row identities. Any error or cancellation rolls the
whole transaction back and preserves inbox files.

## 8. Parallel Preparation and Progress

Preparation is independent and may use a bounded `ProcessPoolExecutor`. The
default worker count is 4 and the documented maximum is 8. Results are buffered
and sorted by manifest index before identity decisions, so completion order
cannot change stored facts or audits.

The observable progress stages are:

```text
VALIDATE -> SCHEDULED -> PARSE_PREPARE -> TRANSACTIONAL_IMPORT
-> READBACK_VERIFIED -> CALCULATE_EXPORT -> CLEANUP
```

Progress includes scheduled/prepared/imported/skipped/quarantined counts and
per-stage elapsed time. Nonterminal duplicate journal records are throttled;
terminal errors always name the failed stage. `transaction_batch_size` from
`duckdb_import` applies to CLI and panel imports alike.

## 9. Cleanup Safety

Import success alone does not delete HTML. Cleanup is permitted only after
schema-versioned audit evidence, zero quarantine, committed readback and
`safe_to_delete=YES`. Per-file lifecycle is:

```text
DELETE_READY -> DELETING -> DELETED
```

Interrupted cleanup rechecks hashes and resumes idempotently. A failed parse,
transaction, readback or export never marks files delete-ready.

## 10. Acceptance Evidence

The implementation is accepted only with:

1. focused parser/import/store/CLI/panel tests;
2. regression tests for UTC tester timestamp persistence, fractional count
   rejection, canonical wallet/equity metrics and CLI batch-size propagation;
3. an import with expected report count, zero quarantine and successful
   transactional readback;
4. a reproducible DD5 export from the saved `dd5_run_id` without rereading HTML
   or runner CSV;
5. a retained audit/checklist proving cleanup eligibility before any deletion.
