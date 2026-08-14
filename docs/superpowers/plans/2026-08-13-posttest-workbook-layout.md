# Post-test Workbook Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make post-test workbooks decision-first without removing audit detail.

**Architecture:** Derive a compact scope summary and finalists table from the already calculated final comparison, then place them before existing audit sheets. Reuse the existing deterministic workbook writer and its filters, frozen headers and width sizing.

**Tech Stack:** Python, pandas, pytest, existing openpyxl audit writer.

## Global Constraints

- Do not alter selection calculations.
- Do not overwrite the user's current workbook; write a separate preview.
- Keep full audit sheets and CSV exports unchanged.

---

### Task 1: Decision-first workbook sheets

**Files:**
- Modify: `tests/test_posttest.py`
- Modify: `src/mrs3/posttest.py`
- Modify: `progress.md`

**Interfaces:**
- Produces: `selection_summary(frame: pd.DataFrame) -> pd.DataFrame`.
- Produces: `selection_finalists(frame: pd.DataFrame) -> pd.DataFrame`.

- [x] Add failing tests for scope counts, thresholds, finalist filtering and ordering.
- [x] Implement both pure dataframe transforms.
- [x] Add `00_Selection_Summary` and `01_Finalists` before the audit sheets.
- [x] Run focused and CLI/panel tests.
- [x] Generate a separate preview workbook from the current 55-row input.
