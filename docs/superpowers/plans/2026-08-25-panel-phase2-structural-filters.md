# Panel Phase 2 Structural Filters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore four structural Pareto filters in Panel Web and generate READY JSON only from their server-side view.

**Architecture:** Read immutable fresh-analysis `points` and `structures` payloads into the Phase 2 comparison rules, then expose one result to shortlist, audit and generation. The browser holds checkbox and scope state only.

**Tech Stack:** Python, DuckDB, pandas, static HTML/CSS/JavaScript, pytest.

**Spec:** `docs/specs/2026-08-25-panel-phase2-structural-filters.md`

## Global Constraints

- Do not alter Source DuckDB schema/payload, materializer, selection or analysis artifact.
- Require `event_mode=real_independent_events`, exact event validation and exact comparison keys.
- No new dependency, weighted score or Top-N; use `.venv\\Scripts\\python.exe -m pytest`.
- Generated manifest records filters and scopes; browser candidate IDs are not authoritative.

---

### Task 1: Evaluate immutable fresh candidates

**Files:**
- Modify: `src/mrs3/fresh_analysis_strategies.py`
- Modify: `tests/test_fresh_analysis_strategies.py`

**Interfaces:** Produces `filter_fresh_analysis_candidates(path, analysis_id, filters)` with candidate rows, per-criterion audit rows, combined audit rows, counts and ready IDs by scope.

- [ ] **Step 1: Write failing evaluator tests**

```python
def test_fresh_phase2_defers_only_dominated_candidate(tmp_path: Path) -> None:
    result = filter_fresh_analysis_candidates(_analysis(tmp_path), ANALYSIS_ID, {"source_pnl": True})
    assert _row(result, "A")["filter_status"] == "READY_AFTER_FILTERS"
    assert _row(result, "B")["deferred_by_candidate_id"] == "A"
```

- [ ] **Step 2: Verify RED**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_fresh_analysis_strategies.py -k phase2`
Expected: FAIL because the evaluator is absent.

- [ ] **Step 3: Implement minimal evaluator**

```python
def filter_fresh_analysis_candidates(analysis_path, analysis_run_id, filters):
    # validate fresh analysis, read points/structures, validate events,
    # then apply existing exact Pareto comparison semantics.
```

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_fresh_analysis_strategies.py -k phase2`
Commit: `feat: evaluate fresh Phase 2 shortlist`.

### Task 2: Connect shortlist and safe generation

**Files:**
- Modify: `src/mrs3/fresh_analysis_strategies.py`
- Modify: `src/mrs3/panel.py`
- Modify: `tests/test_fresh_analysis_shortlist_groups.py`
- Modify: `tests/test_panel_fresh_strategies.py`

**Interfaces:** `list_fresh_analysis_shortlist(path, id, filters=None)` returns item `filter_status` and group `ready/deferred`; generator receives filters and selected scopes, recomputes READY IDs itself.

- [ ] **Step 1: Write failing API/generation tests**

```python
def test_shortlist_reports_filtered_ready_and_deferred_counts(tmp_path: Path) -> None:
    result = list_fresh_analysis_shortlist(_analysis(tmp_path), ANALYSIS_ID, {"source_pnl": True})
    assert (result["groups"][0]["ready"], result["groups"][0]["deferred"]) == (1, 1)

def test_generation_cannot_select_deferred_browser_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="READY_AFTER_FILTERS"):
        generate_fresh_analysis_strategies(..., selected_scopes=[SCOPE], filters={"source_pnl": True}, ...)
```

- [ ] **Step 2: Verify RED**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_fresh_analysis_shortlist_groups.py tests/test_panel_fresh_strategies.py -k "filter or deferred"`
Expected: FAIL because requests ignore filters and accept client IDs.

- [ ] **Step 3: Implement server recomputation**

```python
def strategies_fresh_generate(self, payload):
    # validate run, filters and scopes; do not accept candidate_ids;
    # the generator derives eligible IDs from its fresh filter result.
```

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_fresh_analysis_shortlist_groups.py tests/test_panel_fresh_strategies.py`
Commit: `feat: enforce Phase 2 READY generation`.

### Task 3: Audit workbook

**Files:**
- Modify: `src/mrs3/analysis_filter_export.py`
- Modify: `src/mrs3/panel.py`
- Modify: `tests/test_analysis_filter_export.py`

**Interfaces:** An audit endpoint writes only a configured safe output and returns its file token/name.

- [ ] **Step 1: Write failing export test**

```python
def test_fresh_filter_audit_has_required_sheets(tmp_path: Path) -> None:
    assert _sheet_names(export_fresh_filter_audit(_fresh_result(), tmp_path / "audit.xlsx")) == ["Summary", "READY_AFTER_FILTERS", "Source PnL", "DEFERRED_COMBINED"]
```

- [ ] **Step 2: Verify RED, implement adapter, verify GREEN**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_analysis_filter_export.py -k fresh`
Expected first: FAIL for missing fresh exporter.
Implement: normalize the fresh result into the existing audit workbook writer; preserve numerical values and criterion-sheet order.
Expected after: PASS.

- [ ] **Step 3: Commit**

Commit: `feat: export fresh Phase 2 audit`.

### Task 4: Mockup-aligned static panel

**Files:**
- Modify: `src/mrs3/panel_web/index.html`
- Modify: `src/mrs3/panel_web/app.js`
- Modify: `src/mrs3/panel_web/app.css`
- Modify: `tests/test_panel_static_ui.py`
- Modify: `progress.md`

**Interfaces:** DOM IDs are `shortlist-filter-source-pnl`, `shortlist-filter-efficiency`, `shortlist-filter-close-support`, `shortlist-filter-point-event-count`, and `shortlist-audit`; every shortlist/generate/audit request sends the exact four filter booleans.

- [ ] **Step 1: Write failing static UI test**

```python
def test_panel_shortlist_has_phase2_controls_before_grouped_table() -> None:
    page = _static("index.html")
    assert page.index('id="shortlist-filter-source-pnl"') < page.index('id="shortlist-body"')
    assert 'id="shortlist-audit"' in page
```

- [ ] **Step 2: Verify RED**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_panel_static_ui.py -k phase2`
Expected: FAIL because controls are absent.

- [ ] **Step 3: Implement the smallest UI**

```html
<details class="phase2-filters"><summary>Phase 2 structural filters</summary>...</details>
```

```javascript
const currentShortlistFilters = () => ({ source_pnl: ..., efficiency: ..., close_support: ..., point_event_count: ... });
```

Use the supplied mockup's control order and dark grouped table; keep the existing table data and scope selection mechanics.

- [ ] **Step 4: Verify GREEN, update progress and commit**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_panel_static_ui.py tests/test_panel_fresh_strategies.py tests/test_fresh_analysis_shortlist_groups.py`
Commit: `feat: add Phase 2 filters to Panel Web`.

## Final verification

- [ ] Run `.venv\\Scripts\\python.exe -m pytest -q tests/test_fresh_analysis_strategies.py tests/test_fresh_analysis_shortlist_groups.py tests/test_analysis_filter_export.py tests/test_panel_fresh_strategies.py tests/test_panel_static_ui.py`.
- [ ] Run `.venv\\Scripts\\python.exe -m pytest -q`.
- [ ] Run `git diff --check`, inspect the scoped diff and obtain independent review before committing.
