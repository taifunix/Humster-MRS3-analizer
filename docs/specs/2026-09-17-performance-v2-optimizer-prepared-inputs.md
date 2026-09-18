# Performance v2 optimizer prepared inputs

Date: 2026-09-17. Status: Implemented and accepted.

## Goal

Persist nullable typed action Price/Cost, six nullable WS1.1 sizing facts, and
one private, versioned, digest-bound, per-result prepared optimizer input so a
normal Performance import/analysis does not make every later portfolio
calculation reinterpret report JSON and reconstruct the same cycles from
scratch.

## Non-goals

- no new database, service, Panel mode, network request or tester run;
- no change to FINALIST selection, portfolio search, sizing or margin policy;
- no use of actual fill `Price`/`Cost` as planned full-position sizing base `S`;
- no duplication of raw action/equity series in public API, Members or XLSX;
- no rewrite of accepted A/B, PnL/30 or four-window stability calculations.

## Inputs and outputs

The existing Performance v2 parser remains the only report parser. ADD and
REPLACE keep the current accepted report contract. Optional missing or malformed
Price/Cost and sizing settings do not reject an otherwise valid report; their
typed values are `NULL` and dependent preparation is `UNAVAILABLE` with a reason.

Schema v5 adds nullable typed facts:

- `strategy_actions.price DECIMAL(38,12)`;
- `strategy_actions.cost DECIMAL(38,12)`;
- `strategy_results.sizing_use_upnl BOOLEAN`;
- `strategy_results.sizing_use_frozen_balance BOOLEAN`;
- `strategy_results.sizing_use_fix BOOLEAN`;
- `strategy_results.sizing_balance_percentage_long DECIMAL(38,12)`;
- `strategy_results.sizing_risk_long DECIMAL(38,12)`;
- `strategy_results.sizing_max_balance DECIMAL(38,12)`.

Only these six sizing fields are added because they are the exact WS1.1 source
geometry checks. Existing JSON metadata remains provenance/compatibility data;
it is not the v5 typed read contract.

One internal table stores the derived cache:

```text
optimizer_prepared_inputs(
    result_id BIGINT PRIMARY KEY REFERENCES strategy_results(result_id),
    preparation_version VARCHAR NOT NULL,
    source_digest VARCHAR NOT NULL,
    availability_status VARCHAR NOT NULL,
    unavailable_reason VARCHAR,
    prepared_json VARCHAR,
    prepared_at_utc TIMESTAMPTZ NOT NULL
)
```

`prepared_json` is canonical, bounded, schema-versioned internal data containing
only normalization-ready per-result series, reconstructed cycles, known source
bases and diagnostics. It does not contain strategy JSON or copies intended for
public output. Readers validate its version, `result_id`, `source_digest`, shape,
finite decimal strings and UTC timestamps before use.

## Data flow

```text
current tester report
  -> existing Performance parser
  -> typed actions/equity + typed optional Phase 8 facts
  -> existing WS1.1 cycle/source-base preparation
  -> one revision-bound prepared artifact
  -> Portfolio Optimizer combines current artifacts on its common grid
```

New ADD/REPLACE imports write typed facts and the prepared artifact in the same
transaction as the current result. REPLACE preserves `result_id`, clears the
previous prepared row first and never inherits facts from the previous report.
Import remains successful when preparation is `UNAVAILABLE`; the status and
reason are persisted and Portfolio Optimizer continues to fail closed for the
dependent calculation.

## Migration and compatibility

Opening schema v4 performs one additive v4-to-v5 migration. It adds the nullable
columns and the internal table, then backfills typed values only from existing
valid `raw_action_json` and revision-checked `optimizer_source_metadata_json`.
No Price, Cost or sizing value is inferred from fees, quantities, initial
balance or defaults. Rows without exact saved evidence remain `NULL`.

Every Performance import migrates an existing valid v2/v3/v4 target under the
normal writer lock before enforcing the current v5 schema gate. Import never
initializes an absent, zero-byte, bare, or foreign database target.

Migration does not eagerly rebuild every historical result. Performance
selection recalculation remains a separate incremental cache operation and
never prepares optimizer artifacts. A portfolio campaign first metadata-reads
its exact current `FINALIST` result IDs, prepares only missing or stale
artifacts for that finite set with the configured `duckdb_import.workers`, and
then performs the strict full read. Subsequent campaigns reuse an artifact only
when its source digest and preparation version match. Schema v5 does not accept
stale v4 values through an implicit JSON fallback.

ADD/REPLACE builds the new result's optimizer source directly from the already
normalized parsed report and persisted parent values. It does not reread the
new action/equity rows one result at a time. Child-row verification uses one
grouped query per child table and treats an absent group as a zero count.

Older report/inbox inputs remain accepted. Existing consumers that do not need
Phase 8 fields retain their behavior. A v5 database remains fail-closed to code
that only understands an older schema version.

## Invariants

- Price/Cost semantics are exactly `actual_fill_not_planned_position`.
- `S` follows WS1.1 (`balance - pnl + fee` at opening-from-flat under the exact
  approved sizing settings); Price/Cost never substitutes for `S`.
- Prepared identity binds current result revision, typed settings, ordered
  actions, ordered equity, effective period and preparation version.
- Missing, invalid, carry-in or non-attributable data remains explicit UNKNOWN;
  no zero/default substitution is allowed.
- One writer transaction owns migration/import/prepared replacement. Readers do
  not observe a typed result paired with an artifact from another revision.
- A/B deterioration, A/B PnL/30, four equal time windows and `Positive windows`
  retain their current calculation and XLSX contracts.

## Canonical source, digest and availability contract

`OptimizerSourceInput` is the sole builder input and the sole digest preimage.
Its canonical document contains the source-document version, result and
strategy identity, revision timestamp, report/effective periods, initial
balance, all six typed sizing facts, ordered typed actions (including Price and
Cost), and ordered equity. `source_digest` is deliberately absent from that
document. The digest is SHA-256 of its UTF-8 canonical JSON: sorted keys,
compact separators, `ensure_ascii=false`, and no non-finite values.

Typed decimals are exact `DECIMAL(38,12)` values. Their canonical spelling has
exactly twelve fractional digits, never uses an exponent, and normalizes
negative zero to `0.000000000000`; a typed value with more than twelve decimal
places is an integrity error, not a rounded value. Timestamps must be aware
UTC values rendered with six microseconds and a trailing `Z` (for example
`2026-09-17T00:00:00.000000Z`). Action and equity ordinals are unique,
non-negative, and ordered by timestamp then ordinal.

Both `AVAILABLE` and `UNAVAILABLE` rows require non-null
`preparation_version` and `source_digest`. A version or digest mismatch makes
an existing artifact stale. Expected evidence/size conditions may commit only
as one of these stable reasons: `MISSING_TYPED_FACTS`, `UNSUPPORTED_SIZING`, or
`PREPARED_TOO_LARGE`. The latter is measured from the exact UTF-8 bytes stored
in `prepared_json` and has a hard limit of 16 MiB. Integrity, ordering,
identity, programming, and DuckDB errors are fatal to the owning transaction.

## Concurrency, origin and privacy boundary

Lazy preparation first resolves the requested current scope and loads one
immutable typed snapshot through one read-only connection before acquiring the
writer lock; that connection is closed before any writer lock is acquired.
Workers receive only in-memory snapshots and perform pure CPU work; they
receive no database connection or path. The process then acquires
`PerformanceV2WriterLock`, opens one writer connection/transaction, resolves
the scope again and reloads canonical inputs. Work is discarded when current
membership, identity, preparation version, or source digest changed; only
matching missing/stale rows are replaced, and the batch commits atomically.
No read-only connection is opened while the writer lock or writer transaction
is active.

Performance v2 rows have explicit origin `PERFORMANCE_V2_DB`; they never fall
back to compatibility JSON. Only explicitly marked `LEGACY_FIXTURE` mappings
may use the fixture compatibility path. Prepared artifacts are private
internal inputs: they may enter only the internal `weighted_input_rows` path
and must not leak into public API/Members/XLSX payloads.

## Acceptance evidence

- v4 copy migrates additively to v5 without changing existing result/action/
  equity/window/review facts;
- legacy reports without optional fields still ADD/REPLACE successfully and
  produce typed `NULL` plus explicit preparation availability;
- reports with exact fields round-trip Decimal/Boolean types and do not inherit
  values across REPLACE;
- prepared data reconstructs the approved 1ORD and multi-order WS1.1 fixtures,
  rejects stale/tampered digests and matches the existing in-memory preparation;
- current optimizer input uses a valid prepared artifact and retains the old
  fail-closed results when evidence is unavailable;
- existing import, review, A/B, XLSX and Portfolio suites pass;
- independent implementation review returns `CODE_REVIEW_PASS`.

## Dependencies

- [Unified Performance Analytics v2](2026-08-28-unified-performance-analytics-v2.md)
- [Weighted Portfolio Search WS1.1](2026-09-14-portfolio-optimizer-weighted-search.md)
- [Phase 8 plan](../superpowers/plans/2026-09-12-portfolio-optimizer-weighted-search-discussion.md)
- [ADR-0037](../decisions/0037-performance-v2-optimizer-prepared-inputs.md)
- [Phase 8 acceptance evidence](../superpowers/plans/2026-09-17-performance-v2-optimizer-prepared-inputs-evidence.md)
