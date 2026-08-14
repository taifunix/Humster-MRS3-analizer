# Post-test Sequential Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the approved scoped outlier filter and sequential Pareto selection to post-test output.

**Architecture:** A single pure dataframe transform in `posttest.py` groups rows by `symbol + side + timeframe`, calculates one-sided IQR thresholds, and applies the three Pareto stages in order. `compare_posttest` invokes it before formatting the final sheet.

**Tech Stack:** Python, pandas, pytest, openpyxl through the existing post-test writer.

## Global Constraints

- Compare only within `symbol + side + timeframe`.
- Reject only high holding and low trade-count outliers.
- Run stage 3 only when stage 2 leaves more than three rows.
- Keep all calculations unrounded; round only report-facing values.

---

### Task 1: Sequential selection transform

**Files:**
- Modify: `tests/test_posttest.py`
- Modify: `src/mrs3/posttest.py`
- Modify: `docs/specs/v07-event-filter-and-shortlist.md`
- Modify: `progress.md`

**Interfaces:**
- Consumes: final normalized post-test dataframe.
- Produces: `sequential_selection(frame: pd.DataFrame) -> pd.DataFrame` with thresholds, stage flags, final flag and reason.

- [x] **Step 1: Write failing tests** for one-sided IQR filtering, scope isolation, stage ordering and conditional stage 3.
- [x] **Step 2: Run focused tests and verify failure** because `sequential_selection` does not exist.
- [x] **Step 3: Implement the minimal dataframe transform** by reusing `_pareto_flag` on scoped survivor subsets.
- [x] **Step 4: Integrate it into `compare_posttest` and final column ordering.**
- [x] **Step 5: Run focused and broader post-test tests.**
- [x] **Step 6: Update active specification and progress evidence.**
- [x] **Step 7: Verify the `55 -> 47 -> 7 -> 1` counts read-only; do not rebuild the workbook while the user starts the next tester batch.**
