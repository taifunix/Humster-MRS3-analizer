# Shortlist Scope Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show only Pair/TF aggregate strategy counts and generate all READY JSON in the selected scope.

**Architecture:** `PanelController` reduces the complete immutable shortlist into Pair/TF counters and omits candidate rows from HTTP. Strategy start resolves READY IDs from the same filter result and scope before launching the existing generator.

**Tech Stack:** Python, stdlib HTML/JavaScript, pytest.

## Global Constraints

- Source PnL remains per-order and is never summed.
- Analysis runs remain immutable.
- XLS remains the complete candidate-level audit.

### Task 1: Aggregate API

**Files:** `src/mrs3/panel.py`, `tests/test_panel.py`

- [x] Add a failing controller test for Pair/TF and 2/3/4/READY/DEFERRED/ALL counts.
- [x] Return scopes and facets without candidate rows.
- [x] Verify focused controller tests.

### Task 2: Scoped JSON Generation

**Files:** `src/mrs3/panel.py`, `tests/test_panel.py`

- [x] Add a failing test proving READY IDs are resolved by Pair/TF on the server.
- [x] Remove client candidate IDs from the request and resolve them before starting the job.
- [x] Verify strategy controller tests.

### Task 3: Summary UI

**Files:** `src/mrs3/panel.py`, `tests/test_panel.py`

- [x] Add failing markup assertions for the aggregate table and absence of candidate checkboxes.
- [x] Render Pair/TF summary rows and update controls/copy.
- [x] Run panel and relevant analysis tests, compileall, diff check, review, restart and smoke test.

### Deferred: Filter Audit Workbook Format

**Files:** `docs/specs/v07-event-filter-and-shortlist.md`, `src/mrs3/analysis_filter_export.py`, `tests/test_analysis_filter_export.py`

- [ ] Agree the human-readable column order, sheet layout, labels, widths, formatting and summary presentation for `phase2_filter_audit.xlsx`.
- [ ] Add workbook-format acceptance tests before changing the exporter.
- [ ] Preserve exact numeric values, deterministic row membership and complete candidate-level audit while improving presentation.
