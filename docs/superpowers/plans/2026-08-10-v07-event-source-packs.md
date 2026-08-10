# v0.7 Event Source Packs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build auditable, mutually exclusive CSV and DuckDB source packages that feed v0.7 event filtering.

**Architecture:** Add a small source-package layer before the existing loader. It writes normalized CSV plus a manifest/audit, preserving a single declared event mode. DuckDB decoding is read-only and reconstructs only fully closed in-window cycles; the existing selection pipeline later consumes the normalized package.

**Tech Stack:** Python 3.11+, pandas, DuckDB, zlib/JSON, pytest.

## Global Constraints

- One selector run consumes one declared event mode.
- Window is UTC `[start, end)`.
- DuckDB source data is read-only; raw HTML is untouched.
- No source metric is described as MRS3 test PnL.
- New logic follows TDD: prove RED, implement minimally, then run focused tests.

### Task 1: Source-package data contract

**Files:** Create `src/mrs3/source_packs.py`; create `tests/test_source_packs.py`; modify `src/mrs3/config.py` only to add `event_filter.min_point_events`.

- [ ] Write failing tests for a CSV row with exact window mapping `PointEventCount=TotalTrades`, a non-exact row rejection, and mixed-mode rejection.
- [ ] Run `pytest tests/test_source_packs.py -q`; observe the expected import failure.
- [ ] Implement immutable package metadata, UTC window parsing and deterministic manifest/audit writing.
- [ ] Re-run the focused tests; expect pass.

### Task 2: CSV package builder and CLI

**Files:** Modify `src/mrs3/cli.py`; modify `src/mrs3/panel.py`; extend `tests/test_cli.py` and `tests/test_panel.py`.

- [ ] Write failing CLI and panel-contract tests for one-or-more CSV paths, declared `legacy_trades_proxy`, and exact-window audit output.
- [ ] Run the focused tests and observe RED.
- [ ] Implement `mrs3 source-csv` and panel request wiring, producing a package directory outside `bot_root`.
- [ ] Re-run focused tests; expect pass.

### Task 3: Read-only DuckDB closed-cycle materializer

**Files:** Create `src/mrs3/duckdb_events.py`; extend `src/mrs3/source_packs.py`; extend `tests/test_source_packs.py`.

- [ ] Write failing tests with compact action blobs: a complete cycle is counted; unclosed, open-before-window and close-at-end cycles are separately excluded; a repeat run has the same event ID/hash.
- [ ] Run `pytest tests/test_source_packs.py -q`; observe RED.
- [ ] Implement only the v4 action-codec decoder and cycle reconstructor required by the tests, then expose `mrs3 source-duckdb` with explicit start/end.
- [ ] Re-run focused tests; expect pass.

### Task 4: Feed package metadata into selection and event filter

**Files:** Modify `src/mrs3/loader.py`, `src/mrs3/pipeline.py`, `src/mrs3/selection.py`; extend their tests.

- [ ] Write failing tests proving the selector rejects absent/mixed modes, filters points below three events, and records event fields in audit.
- [ ] Run focused tests and observe RED.
- [ ] Implement minimal propagation and full rebuild ordering; add before/after counts, point and plateau event sheets.
- [ ] Run focused tests, full `pytest -q`, `git diff --check`, independent review and a scoped commit.
