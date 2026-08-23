# Source v6 Minimal Rebuild Plan v4

> **For agentic workers:** execute one stage at a time with TDD, focused
> verification and independent review before the next stage.

**Status:** Approved — Stages 0–1 complete; Stage 2 in progress

**Goal:** Correct Source v6 metrics and validation in the existing
import/materialization passes, retain fast sealed-payload publication, and keep
analysis on materialized rows with zero factual-payload decoding.

**Spec:** [metric contract](../../specs/2026-08-23-source-v6-metric-contract.md)

## Fixed boundaries

- Fresh-only v2 rebuild from retained HTML; no migration or dual read.
- No new DuckDB table or column, dependency, analysis pass, event table or UI.
- W1–W10, W6, bounded workers, SQL payload copying, metadata readiness and the
  hydrated fallback remain.
- Raw tester declarations remain in the compact-header metrics JSON; derived
  analysis data remains in existing `point_analysis_input.row_json`.
- Every behavior change starts with one narrow failing test. All tests use
  `.venv\Scripts\python.exe -m pytest`.

## Stage 0 — Contract, ADR and v1 baseline

**Files:**

- Modify: `docs/specs/2026-08-23-source-v6-metric-contract.md`
- Create: `docs/decisions/0017-source-v6-facts-and-metrics-v2.md`

Lock these rules before code:

1. Payload v2 stores factual metadata, actions, wallet/equity samples and raw
   declared metrics. Cycles, compatibility events and open-tail ids are derived.
   Existing header counts/open-tail cache remain checked derived metadata.
2. M4 uses the merged, windowed raw balance before rebase. A full scope anchors
   on declared initial balance; a later window anchors on its first raw wallet
   sample. An empty required raw window raises the existing empty-series error;
   only the existing genuine-zero route creates a flat result. Returned series
   remain rebased exactly as today.
3. Round trips are built after seam/window filtering from `(timestamp_ms,
   action_id)` order. `opened`/`increased` are entries; `decreased`/`closed`
   realise. A maximal entry run followed by a maximal realisation run is one
   trip; leading realisations and entry-only tails produce none. Its timestamp
   is its first realisation and its id hashes canonical point/action-id data.
4. M6 uses merged raw windowed series plus only admissible declared candidates;
   tie rules and peak source are deterministic. M7 checks PnL, fees and Profit
   Factor at raw-token precision; Recovery Factor is conditional.
5. v2 `row_json` adds `weighted_trades`, `max_equity_drawdown` and
   `max_equity_drawdown_source`. It is canonical JSON: sorted compact keys,
   Decimal strings, integers, explicit `null`, no floats/non-finite values and
   no missing/extra fields.
6. Any quarantine blocks every scope. The operator fixes/removes the retained
   source and rebuilds a new DB; no partial or in-place unblock exists.

Before code, record same-host three-run v1 medians: import time,
materialization/publication time, coordinator/worker peak RSS, Source DB bytes,
surface bytes and output digests. Record run spread as well as medians.

## Stage 1 — Atomic facts-only format v2

**Files:**

- Modify: `src/mrs3/source_v6.py`
- Modify: `src/mrs3/source_v6_storage.py`
- Modify: `src/mrs3/source_v6_importer.py`
- Modify: `src/mrs3/source_v6_surface_fresh.py`
- Test: focused serialization, storage, importer and surface-throughput tests

1. First add tests proving normalization/decode derive identical cycles,
   compatibility events and open-tail ids; a cache mismatch raises
   `SourceV6StorageError`; a forged blob with a repaired blob checksum still
   fails W6; a second process creates the same bytes/id.
2. Extract one reconstruction helper used by normalization and decode. Remove
   only the three derived fields from canonical payload; retain header/cache
   production and checks.
3. Atomically bump payload, source DB, segment, importer-token, source and
   surface fingerprints to v2. Keep physical tables and zlib codec unchanged.
4. Verify no Source/surface DDL changes, v1/v2 artifacts reject before publish,
   `encode(decode(payload))` keeps bytes/id, and SQL-copy/Python-rebuild factual
   rows agree.

## Stage 2 — Metrics and compact analysis row

**Files:**

- Modify: `src/mrs3/source_v6_stitch.py`
- Modify: `src/mrs3/source_v6_materializer.py`
- Modify: `src/mrs3/source_v6_surface_fresh.py` (v2 row validation only)
- Test: metric, position, worker-materialization and fresh-analysis tests

1. Add failing tests for the worked example (`1 position / 2 trips / 1.5
   weighted / 3 entries`), deterministic ids, leading/tail actions, full and
   later-window M4, empty window, genuine zero, rebase, merged equity/wallet DD
   and every M6 tie.
2. Implement round-trip metrics, all-realising-action Profit Factor, raw M4 and
   merged-series M6. Keep returned series rebased.
3. Put the three audit fields in existing `row_json`, reject invalid v2 rows and
   keep those audit fields out of structural ranking inputs.
4. Verify explicit `null`, unchanged DDL, second-process row/digest equality,
   workers 1/N equality, and stored-row analysis with zero payload decoding and
   fact-derived-equivalent output.

## Stage 3 — Per-report M7 validation and clean-DB guard

**Files:**

- Modify: `src/mrs3/source_v6.py`
- Modify: `src/mrs3/source_v6_storage.py`
- Modify: `src/mrs3/source_v6_materializer.py`
- Modify: `src/mrs3/panel_surfaces.py` (backend preflight only)
- Test: focused importer, storage, materializer and panel-service tests

1. Add failures for independent PnL/fees/PF/conditional-RF mutations and raw
   token precisions.
2. Validate during normalization before encoding, rounding `ROUND_HALF_UP` to
   the raw declared exponent. One mismatch becomes one existing quarantine;
   healthy sibling reports continue.
3. Add a read-only quarantine-detail helper. Backend preflight marks all scopes
   non-READY; direct materialization rejects before decode/task submission and
   reports the source SHA/name/reason.
4. Verify no surface is made from a quarantined DB, no payload is decoded,
   `safe_to_delete=NO`, no DDL/status is added, and a complete clean reimport
   restores the unchanged READY route.

## Stage 4 — Fresh acceptance and performance gate

1. With the finished post-Stage-3 binary, freshly import retained HTML into a
   new Source DB. Never use pre-M7 artifacts for acceptance or benchmarking.
2. Require zero quarantine. If not zero, stop; fix/remove listed retained input
   and rebuild until clean.
3. From the clean DB, verify READY scope, ID-only worker materialization, one
   compact row per point, zero analysis decode, workers 1/N equivalence, focused
   tests and `.venv\Scripts\python.exe -m pytest -q`.
4. Run the same candidate benchmark three times. Against v1 medians, import and
   materialization/publication time and peak RSS may regress at most 5%; Source
   DB/surface bytes may be at most 105% (smaller is expected).
5. Never compare v2 identity with v1: capture a v2 snapshot and require a clean
   v2 rerun to have identical digest and semantic tables. A failed gate stops
   sign-off pending a user-approved exception or separately planned measured
   fix—never speculative optimization.
6. Update `progress.md` and `PRD.md` only with post-M7, zero-quarantine evidence;
   complete independent review and re-review after fixes.

## Explicitly excluded

- UI or `panel_web/` work.
- Weakening W6.
- In-place quarantine remediation, migration or dual read.
- New aggregate states, tables, columns, event storage or benchmark framework.
- Hydrated-path removal and algorithm-dependent surface data.
