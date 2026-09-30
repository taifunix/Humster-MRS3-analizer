# Performance v2 optional commission evidence

**Status:** Implemented / independently reviewed (2026-09-30).
**Decision:** [ADR-0049](../decisions/0049-performance-v2-optional-commission-evidence.md).
**Related:** [Unified Performance v2](2026-08-28-unified-performance-analytics-v2.md),
[SINGLE_MODE collection](2026-09-28-single-mode-report-collection.md),
[heavy DB optimization](2026-09-28-heavy-database-optimization.md).

## Purpose and boundary

Import a verified Performance v2 `SINGLE_MODE` report or collection member even
when the tester configuration has no usable `TakerFee`. The HTML action `Fee`,
`Total fees`, balances, equity and PnL remain the actual financial evidence.
`strategy_results.commission_rate` is optional provenance, not an input to
recalculate them. No rate is inferred from fees or substituted with zero.

This contract owns the `SINGLE_MODE`/collection commission handoff in
`runner/inbox.py`, `performance_v2_input.py`, `performance_v2_collection.py`,
the nullable result value in `performance_v2_import.py`, and the schema/catalog
change in `performance_v2_store.py`. The heavy DB optimization contract still
owns its independent import-throughput tasks and does not authorize this schema
change. Legacy v1 import and FAST/RUNS capture keep their five-field contract;
historical Panel RUNS recovery remains unchanged.

## Input and output rules

- The captured manifest always contains `tester_config_sha256`, the SHA-256 of
  the exact tester configuration bytes supplied to or read by capture. Those
  bytes are not copied into the inbox. The reader retains its existing
  64-character hash check; it cannot rehash unavailable original bytes.
- For v2 `SINGLE_MODE`, a finite `TakerFee` in the tester config may produce
  `commission_contract: {"TakerFee": canonical_decimal}` and its SHA-256
  `commission_contract_id`. If absent or unusable in the config, omit both
  fields. Do not fabricate a rate. FAST/RUNS remain mandatory five-field.
- In v2 input, a claimed contract and ID must be present together. The reader
  validates canonical finite decimal values and the canonical contract hash.
  Existing valid five-field manifests remain readable. A malformed claimed
  contract fails before database mutation. A missing pair is accepted only
  for `SINGLE_MODE` and `SINGLE_MODE_COLLECTION` member entries.
- Collection publication copies each member's pair only when present. A
  member's report range, strategy identity and report/strategy hashes remain
  independently checked.
- ADD with no verified `TakerFee` stores SQL `NULL` in `commission_rate`.
  REPLACE with no verified rate changes any prior rate to `NULL`. Existing
  non-null rates survive v6-to-v7 migration unchanged. Action fees, totals,
  PnL, balances, identities, rollback and all other financial columns retain
  their existing behavior.

## Schema and compatibility

Schema v7 differs from v6 only by making
`strategy_results.commission_rate DECIMAL(38,12)` nullable. The DuckDB writer
upgrades v6 to v7 in one transaction. DuckDB cannot drop this constraint while
the result table has its existing index and foreign-key children, so migration
snapshots the four child tables, recreates the parent and children from the
validated v6 catalog, restores their rows and indexes, drops `NOT NULL`, and
then changes the schema marker. Any failure rolls back marker, catalog and data.
Existing v5 writer upgrade continues through
v6 to v7. Reopening v7 is idempotent. No automatic downgrade exists; older
v6-only code must refuse v7. Read-only v5/v6/v7 catalogs are validated
strictly; an unknown version is rejected.

General read support and equity-cache support intentionally differ: v5 has no
`equity_quality_metrics` table and keeps its explicit `SCHEMA5` sentinel;
equity-cache reads are supported only for the known versions 6 and 7.
Panel export accepts unknown commission without adding a new workbook column
or writing to the DB. Portfolio input carries the nullable value without a
fallback or altered admission/calculation.

## Acceptance evidence

Before behavior changes, add focused failing tests for capture/input pairs,
legacy compatibility, collection members, fresh v7, v5/v6/v7 read matrix,
v5-to-v7 and v6-to-v7 migration, idempotency and injected migration rollback.
Golden ADD and REPLACE imports of the same HTML/strategy with a legacy fee
contract and without a fee contract must match every financial/action/equity
fact except `commission_rate`; only manifest-derived provenance may differ.
Test `NULL` explicitly, including REPLACE of a prior non-null rate, and retain
file integrity, path, reparse, snapshot and transaction failure tests.

Verify focused Performance v2 runner/input/collection/store/import/selection,
Panel export and Portfolio input tests, then the full suite with the repository
`.venv` Python; run `git diff --check` and independent code review. This change
removes a false metadata blocker; it makes no whole-import speed claim.

No live tester run or user-database migration is part of this implementation.
Before any later live rollout, require downtime and the writer lock, a
restorable SHA-recorded backup, a migration rehearsal on a separate copy,
identical row/child counts and existing commission/fee/PnL/balance facts, v7
catalog validation and an idempotent reopen. A failed live upgrade is restored
from the verified backup with matching code, not downgraded in place.
