# v0.7 DuckDB Materializer and Package Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a verified `real_independent_events` DuckDB package that the v0.7 selector and panel can consume without mixing it with CSV proxy data.

**Architecture:** `duckdb_events.py` remains the read-only boundary for compact payloads. It will decode series and position cycles into selector-compatible point rows plus an event mapping and verification audit. A package loader validates the manifest and event mode before `pipeline.run_selection`; CLI and the local panel select a package directory rather than passing arbitrary source CSV directly.

**Tech Stack:** Python 3.11+, pandas, DuckDB, zlib, pytest, standard-library local HTTP panel.

## Global Constraints

- Read v4 DuckDB and source HTML only; never mutate reports, HTML or raw payloads.
- A run consumes exactly one declared package; CSV proxy and real events never mix.
- Real-event metrics are diagnostic source metrics, never MRS3 PnL claims.
- Real selector input requires v2's conjunction of
  `source_summary_status=VERIFIED` (3–5 full-horizon HTML samples) and
  `window_metrics_status=DERIVED_FROM_VERIFIED_SOURCE`; a full-horizon HTML
  summary is never an equality check for the selected window. Legacy v1 proxy
  input retains its existing rules.
- Stream v4 data in bounded batches: decode actions for every report's audit,
  but decode timestamp/equity/wallet payloads only after stored grid bounds
  show complete window coverage; the decoded grid remains the final check.
- Use TDD: observe each new test fail before production code, then run focused and full suites.
- Update `progress.md`, run `git diff --check`, obtain independent review and make one scoped conventional commit per task.

## Evidence Gate

Before Task 1, record read-only v4 evidence: `schema_version=4`, readable
import manifest and delete checklist, and the quarantine count/reasons. The
current v4 audit has two accepted malformed-HTML quarantine records; they
block no read-only materialization but HTML deletion remains forbidden unless
every intended target satisfies the separate `safe_to_delete=YES` rule and no
unresolved quarantine remains.

---

### Task 1: Lossless series and realised-action metrics — complete (`pending review commit`)

**Files:**
- Modify: `src/mrs3/duckdb_events.py`
- Test: `tests/test_source_packs.py`

**Consumes:** v4 `zlib-int64-delta-v1` equity/wallet payloads and `zlib-columnar-json-v1` actions.

**Produces:** `decode_compact_deltas`, `decode_wallet_changes`, and a pure metric function returning PnL, DD, trades, wins, losses, win rate, profit factor and flat count for an explicit `[start, end)` interval.

- [ ] **Step 1: Write failing tests** for a hand-built compact report containing an opening, a decrease, a close, a zero-PnL realization and a known equity peak/drop. Assert the five selector metrics, `flat_trades`, half-open exclusion at `end` and an error for a non-covering grid.
- [ ] **Step 2: Run the focused test**

Run: `python -m pytest tests/test_source_packs.py -q -p no:cacheprovider --basetemp .test-tmp/materializer-red`

Expected: FAIL because the series decoder and metric API do not exist.

- [ ] **Step 3: Implement the minimum pure functions.** Decode `<q` deltas and `<Iq` wallet records; expand wallet changes on the supplied grid; use `closed` and `decreased` actions associated with a fully in-window position cycle; reject unknown codecs and partial coverage.
- [ ] **Step 4: Re-run the focused test** and require PASS.
- [ ] **Step 5: Commit** `feat: calculate DuckDB point metrics`.

### Task 2: Verified real-event source package — complete (`pending review commit`)

**Files:**
- Modify: `src/mrs3/duckdb_events.py`, `src/mrs3/source_packs.py`
- Test: `tests/test_source_packs.py`

**Consumes:** Task 1 metrics, complete v4 report rows and optional local HTML verification root.

**Produces:** v2 `points.csv` with selector columns and a per-point
`window_metrics_status`, `point_events.csv`, `source_audit.csv`,
full-horizon `metric_verification.csv`, and manifest counters for
coverage/exclusions plus `source_summary_status` and
`window_metrics_status`.

- [ ] **Step 1: Write failing tests** proving: events are written as sorted `(point_id,event_id)` rows; a point aggregates deterministically; 3–5 full-horizon HTML samples establish only `source_summary_status`; a full-horizon summary is not compared with a selected window; mismatch, missing HTML or invalid sample count fail closed; only after source verification does every derived window point receive `window_metrics_status=DERIVED_FROM_VERIFIED_SOURCE`; real v1 is audit-only and rejected by selector loading.
- [ ] **Step 2: Run the focused test** and observe the expected failure.
- [ ] **Step 3: Implement staging publication** with source DB hash, source report hashes, all metric/exclusion counters, v2's manifest-level `source_summary_status`, and one derived-window status per point. Resolve only the sampled verification files under the explicit local root and compare the five parsed full-horizon source-summary values using documented summary rounding. Record this sample integrity evidence separately from all selected-window metrics/events, which are derived from immutable v4 records and are never asserted equal to a full-horizon summary.
- [ ] **Step 4: Re-run focused source-package tests** and require PASS.
- [ ] **Step 5: Commit** `feat: materialize verified DuckDB source packages`.

### Task 3: Package-directory selector input

**Files:**
- Create: `src/mrs3/package_loader.py`
- Modify: `src/mrs3/pipeline.py`, `src/mrs3/cli.py`
- Test: `tests/test_package_loader.py`, `tests/test_cli.py`, `tests/test_pipeline.py`

**Consumes:** package manifest, points CSV and real-event mapping from Task 2.

**Produces:** a validated `PackageInput` passed to `run_selection`, actual per-point event sets for plateau unions, and a manifest which retains the package hash/event mode.

- [ ] **Step 1: Write failing tests** for accepting one valid verified package; rejecting missing/mixed/unknown mode, missing event mapping, unverified real rows, and raw `--input-csv` combined with `--source-package`; assert a real plateau union uses distinct IDs rather than point hashes.
- [ ] **Step 2: Run the focused tests** and observe the expected failures.
- [ ] **Step 3: Implement package loading and CLI mutual exclusion.** Keep raw CSV as a separately named compatibility command only; `select --source-package` is the v0.7 path and requires one declared mode.
- [ ] **Step 4: Re-run focused tests** and require PASS.
- [ ] **Step 5: Commit** `feat: select from verified source packages`.

### Task 4: Panel package controls and documentation

**Files:**
- Modify: `src/mrs3/panel.py`, `README.md`, `PRD.md`, `progress.md`
- Test: `tests/test_panel.py`, `tests/test_cli.py`

**Consumes:** `source-duckdb --verify-html-root` and `select --source-package` from prior tasks.

**Produces:** explicit panel fields for source package directory and optional local HTML verification root, with no hard-coded local paths.

- [ ] **Step 1: Write failing panel/controller tests** that capture command arguments and prove source-package selection passes no raw CSV and uses the configured local path only at runtime.
- [ ] **Step 2: Run focused tests** and observe failure.
- [ ] **Step 3: Implement the two controls and safe command construction; update public usage only after the CLI command is exercised.**
- [ ] **Step 4: Run focused panel/CLI tests** and require PASS.
- [ ] **Step 5: Commit** `feat: expose source package selection in panel`.

### Task 5: Real read-only evidence and Phase-2 decision

**Files:**
- Modify: `progress.md`, `PRD.md`
- Test: full suite

**Consumes:** verified DuckDB package, listing dates, template and one selected side.

**Produces:** retained local audit plus documented before/after candidate count; an explicit Phase-2 go/no-go decision.

**v2 verification prerequisite:** migrate real package publication and loading
to the two-horizon contract in [ADR-0002](../../decisions/0002-source-summary-and-window-metrics-verification.md).
The retained audit must show full-horizon source-summary evidence separately
from `DERIVED_FROM_VERIFIED_SOURCE` window metrics; it must not label the
latter as a direct HTML equality match.

- [ ] **Step 1: Run `source-duckdb` read-only** against the approved v4 database with the common window and 3–5 HTML samples.
- [ ] **Step 2: Run `select --source-package`** with external listing-date/template inputs and retain its manifest/workbook locally.
- [ ] **Step 3: Record evidence**: report/point/exclusion counts,
  `source_summary_status`, `window_metrics_status`, event-count distributions,
  JSON count and whether Phase 2 is needed. Do not implement Phase 2 unless
  the count demonstrates it.
- [ ] **Step 4: Run full pytest, `git diff --check`, independent review and commit** `docs: record v07 source package evidence`.
