# DuckDB Surface Coverage Review Baseline Implementation Plan

**Status:** Completed baseline evidence for ADR-0007. This plan records the
implemented one-MA-pair contract and is not the implementation plan for the
pending ADR-0008 amendment.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add readiness-gated Pair -> LONG/SHORT -> TF selection, canonical CSV audit, sparse V2 surfaces, and sequential LONG/SHORT publication.

**Architecture:** Extend `duckdb_direct.py`, `analysis_storage.py`, and `panel.py`. Store V2 evidence in existing `grid_contract_json`; keep V1 unchanged. Prepare every selected side in memory from one read-only source transaction, then publish LONG followed by SHORT.

**Tech Stack:** Python 3.12, DuckDB, existing pandas materialization, stdlib JSON/CSV/hashlib, panel JavaScript, pytest.

## Historical Baseline Constraints

- Preserve the dirty basic coverage implementation and unrelated user files.
- TDD every behavior change; stage only task-owned paths; review every scoped commit.
- Readiness is exact `30/150/430`, gaps `<=10 bp` then `<=40 bp`, denser valid, one common MA pair.
- `430 bp` is a minimum gate; publish every fully covering factual point.
- V1 remains unchanged; V2 evidence is deterministic in existing `grid_contract_json` without schema migration.
- Prepare all sides before publication; publish LONG then SHORT; later-side failure is `PARTIAL` with manual rerun.
- Coverage progress UI and speculative persistent retry/lease infrastructure are deferred.

### Task 1: Govern Sparse V2

**Files:** create `docs/decisions/0007-observed-sparse-surface-contract.md`; create canonical `docs/specs/2026-08-14-duckdb-surface-coverage-review.md`; modify the approved design, `PRD.md`, and `progress.md`.

- [x] Record V1 compatibility, V2 `grid_contract_json`, one-transaction in-memory preparation, sequential publication, and deferred infrastructure in ADR-0007.
- [x] Verify with `rg -n "OBSERVED_SPARSE_GRID_CONTRACT_V2|grid_contract_json|PARTIAL|deferred"` across all three documents.
- [x] Run diff checks, independent review, and commit `docs: accept sparse DuckDB coverage contract`.

### Task 2: Factual Coverage, Readiness, and CSV

**Files:** modify `src/mrs3/duckdb_direct.py`, `tests/test_duckdb_direct.py`.

**Interfaces:** add `DirectScope`, `CoverageInterval`, `ReadinessWitness`, `DirectCoverage`; `list_duckdb_direct_coverage(..., symbols=()) -> DirectCoverage`; canonical JSON/CSV helpers; `write_coverage_artifact(audit_root, relative_name, data) -> Path` uses temp-write, atomic replace, readback, and SHA-256 verification.

- [x] Add failing tests named `test_coverage_uses_report_grid_intersection_and_rejects_empty`, `test_coverage_merges_touching_effective_windows_per_cell`, `test_readiness_uses_30_150_430_boundaries_and_gap_limits`, `test_readiness_accepts_denser_shifts_and_requires_common_ma_pair`, `test_optional_points_above_430_do_not_disable_ready_scope`, `test_inventory_emits_every_candidate_chain_but_publication_emits_one_exact_block`, `test_coverage_csv_exact_columns_order_nulls_timestamps_reasons_and_hash`, and `test_coverage_scan_returns_long_and_short_groups`.
- [x] Run `.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py -k "coverage_ or readiness_ or optional_points" -q`; expect RED.
- [x] Implement report/grid intersections, atomic chains, greedy witnesses, deterministic tie-breaks, factual `POINT_CANDIDATE` and readiness-only `READINESS_GAP` rows, UTF-8/LF stdlib CSV, and exact hashes. Inventory emits every candidate-chain block; publication emits one exact common-interval block. Write inventory to `<audit_root>/surface_coverage/<coverage_token>/coverage_inventory.csv` and side audits before publication to `<audit_root>/surface_coverage/<audit_sha256>/surface_coverage_audit_<side>.csv`; write/readback/hash failure blocks commit. Only logical relative names enter evidence.
- [x] Run all `tests/test_duckdb_direct.py`; review and commit `feat: evaluate DuckDB coverage readiness`.

### Task 3: Sparse V2 in Existing Storage

**Files:** modify `src/mrs3/duckdb_direct.py`, `src/mrs3/analysis_storage.py`, `tests/test_duckdb_direct.py`, `tests/test_analysis_storage.py`.

**Interfaces:** V2 grid contract stores selected scopes, witnesses, sorted point-to-report/source evidence and hash, plus audit schema/row-count/hash. Requests/preflights gain V2 fields with V1-compatible defaults.

- [x] Add failing tests for unchanged literal V1 identity, sparse optional points, identity changes from witness/assignment/audit, malformed/duplicate evidence rejection, schema v4 persistence, audit verification, and `test_v2_point_evidence_jsonl_has_decoded_key_order_lf_canonical_types_and_exact_sha256`.
- [x] Run focused `-k "v1_rectangular or v2_"`; expect RED.
- [x] Revalidate readiness, select narrowest fully covering reports, include every factual point, and freeze canonical evidence.
- [x] Branch validation by `grid_contract["kind"]`; leave V1 path unchanged and persist V2 through existing JSON. Point evidence JSONL sorts decoded six-field tuples, uses canonical JSON with floats forbidden, appends one LF per record, and verifies exact `point_evidence_sha256`.
- [x] Run direct/storage suites; review and commit `feat: publish sparse DuckDB coverage surfaces`.

### Task 4: Multi-Side Controller and Date-Only UI

**Files:** modify `src/mrs3/duckdb_direct.py`, `src/mrs3/panel.py`, `tests/test_duckdb_direct.py`, `tests/test_panel.py`.

**Interfaces:** retain one side per `DirectBuildRequest` and `DirectSurface`. Add `DirectScope(symbol, side, timeframe)`, `_CoverageScan(token, coverage, inventory_path)`, `_DirectJob(requests: tuple[DirectBuildRequest, ...], ...)`, `prepare_direct_surfaces(...) -> tuple[DirectSurface, ...]`, and `publish_direct_surfaces(...) -> DirectQueueResult`. Coverage API returns both sides/token/CSV artifact; start accepts `{pair, side, timeframe}` scopes and creates one request per selected side. The token hashes both-side rows, witnesses, inventory hash, and source report/hash evidence; every start validates the latest scan.

- [x] Add failing tests `test_direct_job_prepares_long_and_short_in_one_source_transaction_before_publication`, `test_either_side_preparation_failure_rolls_back_and_publishes_zero`, `test_direct_job_publishes_long_before_short`, `test_long_publication_failure_reports_failed_with_zero_surfaces`, `test_direct_job_reports_partial_when_short_publication_fails`, `test_cancellation_after_long_commit_prevents_short_and_reports_partial`, `test_audit_write_failure_blocks_that_side_before_commit`, `test_stale_coverage_token_and_mixed_surface_side_are_rejected`, `test_direct_start_derives_one_common_interval_per_side`, `test_selected_common_intervals_are_returned_and_rendered_as_dates_before_start`, `test_direct_coverage_review_ui_is_pair_side_tf_and_date_only`, and `test_direct_coverage_ui_keeps_preflight_activity_feedback_deferred`.
- [x] Run direct/panel focused tests; expect RED.
- [x] Implement `STARTING -> PREPARING_LONG -> PREPARING_SHORT -> PUBLISHING_LONG -> PUBLISHING_SHORT -> PUBLISHED`. Preparation failures become `FAILED`/`CANCELLED` with zero surfaces; LONG publication failure before commit becomes `FAILED` with zero surfaces; failure or cancellation after LONG commits becomes `PARTIAL`. Expose only hash-verified artifact paths through existing `artifact()` names `coverage_inventory`, `surface_coverage_audit_long`, and `surface_coverage_audit_short`.
- [x] Return each selected side's exact common interval from controller selection state and render its `YYYY-MM-DD .. YYYY-MM-DD` form before queueing. Render Pair -> Side -> TF and reuse existing build progress/journal.
- [x] Run direct/panel suites; review and commit `feat: queue LONG and SHORT surface builds`.

### Task 5: Documentation and Verification

**Files:** modify `PRD.md`, `progress.md`.

- [x] Run focused direct/panel/storage tests, broader importer/source/published-surface tests, then full pytest; record exact counts.
- [x] Mark feature verified, link ADR, record V2 schema-v4 evidence, sequential/partial semantics, and deferred work.
- [x] Run final diff/status/review and commit `docs: record DuckDB coverage verification`.

### Deferred Follow-up: MA-C Coverage Summary

**Status:** Deferred; not part of the completed coverage implementation.

When explicitly activated, add a separate human-readable
`coverage_summary.csv` without changing the canonical detailed
`coverage_inventory.csv` or publication-audit schemas.

- Sort rows by `Pair`, LONG-before-SHORT `Side`, `TF`, then `MA-C`.
- Emit one row for every expected closing MA in `2..7` with `FULL`, `PARTIAL`,
  or `MISSING` status for the displayed exact interval.
- Include the displayed interval, the best readiness-capable exact interval for
  that MA-C, comma-separated readiness-capable Open MA values, and a stable
  missing/partial reason.
- Define `FULL` as at least one Open MA pair satisfying the complete shift
  readiness contract across the displayed interval. `PARTIAL` means such a
  pair has a shorter readiness-capable interval; `MISSING` means none exists.
- Add a compact panel `MA-C` column that lists fully covered values and visually
  highlights any missing or partial value from `2..7`.
- The checkbox gate is not deferred with this summary: the canonical contract
  enables a Pair/Side/TF row only when every Close MA in `2..7` shares the
  displayed continuous readiness interval, with at least one readiness-capable
  Open MA per Close MA.
- Keep exact UTC timestamps in the CSV while the compact panel continues to
  render dates only.
- Report ignored degenerate rows in this future summary; their runtime handling
  is governed separately by ADR-0008 and is not deferred with this CSV.

## Ponytail Result

No new production module, schema/table, dependency, persistent queue, retry endpoint, lease framework, or path-replacement hook. Add those only after their measurable deferred triggers occur.
