# Performance v2 Optimizer Prepared Inputs Implementation Plan

> **For Codex:** execute this plan sequentially with one Luna xhigh executor. Each
> task's focused suite must be green before the next task starts. Do not commit or
> push before independent implementation review returns `CODE_REVIEW_PASS`.

**Goal:** Upgrade the existing Performance v2 DuckDB to schema v5, persist exact
typed optimizer facts and one revision-bound prepared input per result, and make
weighted Portfolio Optimizer consume those inputs without changing selection,
XLSX, tester, network or UI behavior.

**Architecture:** Extend the existing database and importer. Extract one small
shared per-result preparation boundary from the current portfolio input logic.
New imports prepare eagerly in their current transaction; normal analysis
prepares missing or stale current results from immutable typed snapshots, with
workers performing CPU work only. The Campaign-specific common `T x N` grid
remains outside PerformanceDB.

**Tech stack:** Python, DuckDB, stdlib JSON/hash/Decimal/concurrency, pytest. No
new dependency, database, service, worker setting or endpoint.

**Approved by:** independent Advisor `PLAN_APPROVED` on 2026-09-17 after closure
of findings P8-01 through P8-08.

## Canonical contract

Create `src/mrs3/performance_v2_optimizer.py` as the sole shared boundary.
`OptimizerSourceInput` is both the only builder input and the only digest
preimage. It contains source-document version, result and strategy identity,
revision timestamp, report/effective periods, initial balance, all six typed
sizing facts, ordered typed actions including Price/Cost, and ordered equity.

The digest is `sha256(canonical_json(source_input.to_document()))`.
`source_digest` is not part of the input document, avoiding circularity.

Canonical JSON rules:

- UTF-8, sorted keys, compact separators, `ensure_ascii=False`, no NaN/Infinity;
- Decimal values are already exact `DECIMAL(38,12)` values and render with
  exactly 12 fractional digits, no exponent, with negative zero normalized;
- timestamps are aware UTC values with exactly six fractional digits and `Z`;
- actions order by timestamp/action index and equity by timestamp/sample index;
- ordinals are unique and non-negative.

Both `AVAILABLE` and `UNAVAILABLE` rows have non-null preparation version and
source digest. Stable expected unavailability reasons are
`MISSING_TYPED_FACTS`, `UNSUPPORTED_SIZING` and `PREPARED_TOO_LARGE`. The size
limit is measured on the exact stored prepared bytes and is 16 MiB. Integrity,
ordering, identity, programming and DuckDB failures are fatal to their owning
transaction.

Only explicitly marked `LEGACY_FIXTURE` mappings may use compatibility JSON.
Rows from PerformanceDB are marked `PERFORMANCE_V2_DB` and never fall back.

## Task 1: Pin specification and locking decision

**Files:**

- Modify `docs/specs/2026-09-17-performance-v2-optimizer-prepared-inputs.md`.
- Add `docs/decisions/0038-performance-v2-prepared-canonicalization-and-locking.md`.

1. Add the canonical source/digest, Decimal/timestamp and size contracts.
2. Add stable availability reasons and expected/fatal error boundary.
3. State that preparation version/digest are non-null for both statuses and a
   preparation-version mismatch is stale.
4. Record the concurrency sequence: load one immutable typed snapshot through
   one read-only connection before the writer lock; close it; CPU workers receive
   no database/path; then acquire the writer lock and use one writer transaction;
   reload current scope and canonical inputs; discard work whose membership or
   digest changed; commit atomically.
5. Record strict DB origin and private-artifact/no-public-leak rules.

Check:

```powershell
rg -n "0\.000000000000|PREPARED_TOO_LARGE|LEGACY_FIXTURE|writer lock|source_digest" docs/specs/2026-09-17-performance-v2-optimizer-prepared-inputs.md docs/decisions/0038-performance-v2-prepared-canonicalization-and-locking.md
git diff --check
```

## Task 2: Schema v5 and additive v4 migration

**Files:**

- Modify `src/mrs3/performance_v2_store.py`.
- Modify `tests/test_performance_v2_store.py`.

Add RED tests for:

- exact v4 catalog validation before migration;
- action Price/Cost, six result sizing columns and prepared table constraints;
- exact bounded action JSON and revision-checked sizing backfill;
- an explicit parameter table/count of every intentionally dropped invalid class;
- no prepared rows during migration;
- v5 reopen idempotence preserving prepared bytes;
- total rollback of DDL/backfill/marker on injected failure;
- v2/v3 migration chain ending at v5.

Run RED:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_store.py -q
```

Implement `_SCHEMA_VERSION = "5"`, distinct exact v4/v5 catalogs, nullable typed
columns, `optimizer_prepared_inputs`, and one atomic v4-to-v5 migration. Backfill
only bounded exact facts accepted by the existing provenance decoders. Do not
round, infer or create prepared rows.

Run GREEN:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_store.py -q
git diff --check
```

## Task 3: Canonical source, builder and decoder

**Files:**

- Add `src/mrs3/performance_v2_optimizer.py`.
- Add `tests/test_performance_v2_optimizer.py`.
- Modify `src/mrs3/portfolio/input.py` minimally.
- Modify `tests/test_portfolio_input.py`.

Add RED tests for canonical Decimal/timestamp behavior, key-order stability,
digest coverage of every `to_document()` field, import/readback digest equality,
result identity, 1ORD/multi-order equivalence, dynamic source basis, Price/Cost
never replacing `S`, each availability reason, carry-in/censored diagnostics,
exact-byte size boundary, strict decoder failures, prepared execution bypassing
raw reconstruction, and explicit fixture-only fallback. A typed value exceeding
scale 12 is an integrity error, never silently quantized.

Run RED:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_optimizer.py tests/test_portfolio_input.py -q
```

Extract only the pure per-result cycle/source-basis logic needed by both paths.
Keep common-period and grid combination in `prepare_weighted_input`. Build and
decode the bounded private artifact; convert only expected evidence and size
conditions to `UNAVAILABLE`.

Run GREEN:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_optimizer.py tests/test_portfolio_input.py -q
git diff --check
```

## Task 4: Eager import persistence

**Files:**

- Modify `src/mrs3/performance_v2_import.py`.
- Modify `tests/test_performance_v2_import.py`.

Add RED tests for exact typed ADD, malformed optional facts committing as
`MISSING_TYPED_FACTS`, available artifact/digest, eager `PREPARED_TOO_LARGE`,
REPLACE retaining `result_id` but inheriting nothing, digest change, import vs
readback digest equality, total rollback on unexpected failure, and no extra
connection/pool/transaction.

Run RED:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_import.py -q
```

Extend existing action/result writes. Clear the prepared row on REPLACE. After
existing child flush/readback, load the just-written typed source through the
same connection and transaction, build once, and insert one status row before
commit. Expected unavailability commits; unexpected failures use existing
rollback handling.

Run GREEN:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_import.py tests/test_performance_v2_store.py tests/test_performance_v2_optimizer.py -q
git diff --check
```

## Task 5: Lazy current preparation and strict reads

**Files:**

- Modify `src/mrs3/performance_v2_optimizer.py`.
- Modify the existing Performance recalculation/controller and Portfolio input
  files reached by the current call flow; do not add an endpoint.
- Modify the corresponding Performance, Portfolio and export tests only where
  the flow proves necessary.

Add RED tests for requested-current-only preparation, valid reuse, stale/malformed
replacement, `UNAVAILABLE` becoming `AVAILABLE`, lazy size overflow, worker
determinism/no DB access, read connection closing before writer lock, no read
connection during writer transaction, mutation-between-phases stale discard,
atomic batch rollback, exclusive use of `duckdb_import.workers`, metadata-only
read independence, strict prepared series reads, DB no-fallback, fixture fallback,
prepared-path reconstruction bypass, unchanged adapter/grid behavior and no
public/Members/XLSX leakage.

Run RED:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_optimizer.py tests/test_panel_performance_v2.py tests/test_portfolio_input.py tests/test_panel_portfolio.py tests/test_portfolio_export.py tests/test_portfolio_reports.py -q
```

Implement this sequence only:

1. Before any writer lock, use one read-only connection to resolve the requested
   current scope and load immutable typed snapshots/current artifact metadata.
2. Close it, then run pure CPU builders capped by the existing Performance
   config workers sourced from `duckdb_import.workers`.
3. Acquire `PerformanceV2WriterLock`, open one writer transaction, resolve the
   scope again and reload canonical inputs.
4. Discard pre-lock work if current identity, scope, version or digest changed.
5. Replace only missing/stale rows that still match, then commit atomically.

`include_series=False` remains metadata-only. `include_series=True` uses strict
prepared inputs and typed sizing without reading raw action/equity JSON. Private
prepared rows may enter only internal `weighted_input_rows`; public outputs stay
unchanged.

Run GREEN:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_optimizer.py tests/test_panel_performance_v2.py tests/test_performance_v2_selection.py tests/test_portfolio_input.py tests/test_panel_portfolio.py tests/test_portfolio_adapter.py tests/test_portfolio_export.py tests/test_portfolio_reports.py -q
git diff --check
```

## Task 6: Verification, review and closure

Run relevant focused and protected suites, then the full suite:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_store.py tests/test_performance_v2_optimizer.py tests/test_performance_v2_import.py tests/test_performance_v2_html.py tests/test_performance_v2_windows.py tests/test_performance_v2_selection.py tests/test_panel_performance_v2.py tests/test_portfolio_input.py tests/test_panel_portfolio.py tests/test_portfolio_adapter.py tests/test_portfolio_export.py tests/test_portfolio_reports.py -q
.venv\Scripts\python.exe -m pytest tests/test_performance_v2_retest.py tests/test_performance_v2_finalist_retest.py tests/test_performance_v2_selection_review.py tests/test_performance_v2_prune.py tests/test_portfolio_weighted_search.py tests/test_portfolio_integration.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
git status --short
git diff --stat
```

Omit a named optional test file if it does not exist; do not create a duplicate
test module solely to satisfy a command.

Send an ASCII-only packet with contract, diff and all evidence to the configured
Opus-high implementation reviewer. Fix confirmed findings, rerun affected and
full relevant suites, and re-review until `CODE_REVIEW_PASS`.

Only after PASS:

- add Phase 8 evidence;
- mark the spec and ADR accepted;
- update `PRD.md` and `progress.md` without claiming tester/live readiness;
- let the designated plan-status maintainer update Phase 8 checkboxes from root
  evidence and the actual review disposition;
- rerun the full suite and diff checks;
- create one scoped commit `feat: persist optimizer prepared inputs in performance v2`;
- push under the user's existing authorization.
