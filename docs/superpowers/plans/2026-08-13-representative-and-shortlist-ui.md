# Representative And Shortlist UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce one representative point per Plateau/TF/CloseMA before candidate combinations, then make structural Phase 2 filtering visible and usable.

**Architecture:** `selection.build_structures` collapses every eligible Plateau family through `choose_equivalent_default` before Cartesian products. `analysis_shortlist` groups immutable candidates by structural `ComparisonKey` and applies per-order Pareto criteria. The panel renders summary counts and a bounded review table while XLS remains the complete audit.

**Tech Stack:** Python, pandas, DuckDB, stdlib HTML/JavaScript, pytest.

## Global Constraints

- Source PnL is compared per corresponding order only and is never summed.
- One candidate order comes from one distinct Plateau.
- Exactly one representative is available per Plateau/Pair/Side/TF/CommonCloseMA.
- Existing analysis runs remain immutable; corrected behavior requires a new run.

### Task 1: Representative Before Combinations

**Files:** `src/mrs3/selection.py`, `tests/test_selection.py`

- [x] Add a failing test with two event-eligible equivalent points in one Plateau.
- [x] Select one representative before `itertools.product`.
- [x] Verify selection and pipeline tests.

### Task 2: Structural Phase 2 Groups

**Files:** `src/mrs3/analysis_shortlist.py`, `tests/test_analysis_shortlist.py`, `src/mrs3/analysis_filter_export.py`

- [x] Add failing tests proving event-set differences do not split structural groups and PointEventCount can dominate.
- [x] Replace event-signature BehaviorKey with structural ComparisonKey and expose group counts.
- [x] Verify shortlist, XLS, and strategy guards.

### Task 3: Bounded Review UI

**Files:** `src/mrs3/panel.py`, `tests/test_panel.py`

- [x] Add failing markup tests for ALL/READY/DEFERRED/COMPARABLE counters and a review table.
- [x] Render at most 200 filtered rows with status, structure, orders, metrics and selection.
- [x] Show explicit no-op/comparison-group explanations and preserve complete XLS export.
- [x] Verify panel tests and restart the local panel.

### Task 4: Real-Run Verification

**Files:** `progress.md`

- [x] Re-run analysis on the existing real-event surface without rebuilding HTML/source data.
- [x] Verify one point per Plateau/TF/CloseMA and report actual filter counts.
- [x] Run focused tests, `git diff --check`, and independent review.
