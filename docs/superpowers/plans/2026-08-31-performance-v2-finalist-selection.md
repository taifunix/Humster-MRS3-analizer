# Performance v2 finalist selection implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply every visible Panel filter/Pareto stage to current Performance DB v2 candidates and download one auditable XLSX for selected Pair + Side.

**Architecture:** A pure executor derives v2 facts and applies a closed ordered stage registry without selection persistence. The Panel sends one local UI snapshot only on click, then downloads a deterministic workbook. Tags, saved runs and RETEST remain deferred.

**Tech Stack:** Python 3.11, DuckDB, pandas, openpyxl, static HTML/CSS/JavaScript, pytest.

**Spec:** `docs/specs/2026-08-31-performance-v2-finalist-selection.md`

## Global constraints

- Read only ACTIVE current Performance DB v2 facts; never v1, HTML, CSV or Analysis DB lineage.
- Label DD5 values `DD5_PROXY` / `CALCULATION_ONLY`; never as tested PnL.
- Accept only all 13 visible stage IDs and the two UI scopes.
- Do not create selection runs/results, tags, discard, RETEST or edited-XLSX import.
- Use `.venv\\Scripts\\python.exe -m pytest`; run `git diff --check` and independent review before each commit.

---

### Task 1: Closed stage request and configuration

**Files:**
- Modify: `config.performance.json`
- Create: `src/mrs3/performance_v2_selection.py`
- Create: `tests/test_performance_v2_selection.py`

**Interfaces:**
- Produces `SelectionStage(id, enabled, scope)`, `SelectionRequest(symbol, side, stages)`, and `parse_selection_request(payload)`.

- [ ] **Step 1: Write failing validation tests**

```python
def test_parse_selection_request_rejects_unknown_and_duplicate_stages():
    with pytest.raises(PerformanceV2SelectionError, match="UNKNOWN_STAGE"):
        parse_selection_request({"symbol": "AAAUSDT", "side": "LONG", "stages": [{"id": "unknown", "enabled": True, "scope": "pair_side"}]})
```

- [ ] **Step 2: Verify the test is red**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_selection.py -q`
Expected: FAIL because the module is absent.

- [ ] **Step 3: Implement the closed parser and config defaults**

Define all 13 IDs. Add A/B final days `14`, B return floor `5`, return divisor `10`, B win-rate floor `58`, trade divisor `7`, and plateau proxy multiplier `2`; permit no arbitrary expression or stage parameter.

- [ ] **Step 4: Verify the test is green**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_selection.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```text
git add config.performance.json src/mrs3/performance_v2_selection.py tests/test_performance_v2_selection.py
git commit -m "feat: validate performance v2 selection request"
```

### Task 2: Derive current v2 facts

**Files:**
- Modify: `src/mrs3/performance_v2_selection.py`
- Test: `tests/test_performance_v2_selection.py`

**Interfaces:**
- Produces `load_selection_candidates(connection, request) -> pandas.DataFrame` with one requested ACTIVE strategy per row.

- [ ] **Step 1: Write the failing fact test**

```python
def test_loader_derives_proxy_holding_and_order_plateau_counts(connection):
    row = load_selection_candidates(connection, request).iloc[0]
    assert row["order_1_plateau_point_count"] == 12
    assert row["total_plateau_point_count"] == 12
    assert row["dd5_proxy"] == Decimal("0.10")
```

- [ ] **Step 2: Verify the test is red**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_selection.py::test_loader_derives_proxy_holding_and_order_plateau_counts -q`
Expected: FAIL because fact loading is absent.

- [ ] **Step 3: Implement one query-backed loader**

Join strategies/current results/orders/plateaus and use existing action/window helpers. Derive proxy, capital, efficiency, holding p95, first shift, ordered plateau values and total; calculate and cache default A/B, retaining only `ab_pnl_change_30d_pct`.

- [ ] **Step 4: Verify focused tests**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_selection.py tests/test_panel_performance_v2.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```text
git add src/mrs3/performance_v2_selection.py tests/test_performance_v2_selection.py
git commit -m "feat: derive performance v2 selection facts"
```

### Task 3: Ordered executor for all visible stages

**Files:**
- Modify: `src/mrs3/performance_v2_selection.py`
- Test: `tests/test_performance_v2_selection.py`

**Interfaces:**
- Produces `run_selection(candidates, request) -> pandas.DataFrame` with `eliminated_by_<stage_id>`, `finalist`, and `elimination_reason`.

- [ ] **Step 1: Write failing stage tests**

```python
@pytest.mark.parametrize("stage_id", [
    "filter_holding_outlier", "filter_low_trades", "ab_deterioration", "pareto_dd5_balanced",
    "pareto_plateau_points_per_order", "pareto_plateau_points_total", "pareto_efficiency_shift",
    "pareto_dd5_holding", "pareto_dd5_close_ma", "pareto_dd5_first_shift",
    "pareto_conditional_close_ma", "pareto_primary", "pareto_dd5_capital",
])
def test_each_stage_marks_only_its_failed_or_dominated_candidate(stage_id):
    assert run_selection(stage_fixture(stage_id), request_for(stage_id)).loc[1, f"eliminated_by_{stage_id}"]
```

- [ ] **Step 2: Verify the tests are red**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_selection.py -q`
Expected: FAIL because `run_selection` is absent.

- [ ] **Step 3: Implement scope, IQR and Pareto helpers**

Use one scope-key helper and one no-worse/strict-better Pareto helper. Implement IQR, A/B, conditional Close MA, and the two explicit plateau rules. Operate only on current survivors; missing values skip comparison, and insufficient A/B never eliminates. Stably order by strategy name then ID.

- [ ] **Step 4: Add and run order/scope/missing-data tests**

```python
def test_order_changes_survivors_and_missing_ab_does_not_eliminate():
    assert run_selection(frame, first).finalist.tolist() != run_selection(frame, second).finalist.tolist()
    assert not run_selection(frame, first).loc[2, "eliminated_by_ab_deterioration"]
```

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_selection.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```text
git add src/mrs3/performance_v2_selection.py tests/test_performance_v2_selection.py
git commit -m "feat: execute performance v2 finalist filters"
```

### Task 4: XLSX and endpoint

**Files:**
- Modify: `src/mrs3/performance_v2_selection.py`
- Modify: `src/mrs3/panel_performance_v2.py`
- Modify: `src/mrs3/panel.py`
- Test: `tests/test_performance_v2_selection.py`
- Test: `tests/test_panel_performance_v2.py`

**Interfaces:**
- Produces `write_selection_workbook(result, target) -> Path`, service method `selection_xlsx(payload)`, and `POST /api/v2/strategies/performance-v2/selection/xlsx`.

- [ ] **Step 1: Write failing workbook/endpoint tests**

```python
def test_workbook_keeps_all_candidates_one_ab_column_and_stage_trace(tmp_path):
    sheet = load_workbook(write_selection_workbook(result, tmp_path / "finalists.xlsx"), data_only=True)["All candidates"]
    assert "ab_pnl_change_30d_pct" in headers(sheet)
    assert sum(name.startswith("ab_") for name in headers(sheet)) == 1
    assert "eliminated_by_pareto_dd5_balanced" in headers(sheet)
```

- [ ] **Step 2: Verify the tests are red**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_selection.py tests/test_panel_performance_v2.py -q`
Expected: FAIL because the endpoint is absent.

- [ ] **Step 3: Implement export/download**

Reuse `audit.write_audit_workbook`; produce `All candidates` and `Finalists`. Round only copied display values, constrain output within configured performance root, write no database selection state, and return an attachment.

- [ ] **Step 4: Verify endpoint tests**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_selection.py tests/test_panel_performance_v2.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```text
git add src/mrs3/performance_v2_selection.py src/mrs3/panel_performance_v2.py src/mrs3/panel.py tests/test_performance_v2_selection.py tests/test_panel_performance_v2.py
git commit -m "feat: export performance v2 finalist xlsx"
```

### Task 5: Explicit Panel submit

**Files:**
- Modify: `src/mrs3/panel_web/index.html`
- Modify: `src/mrs3/panel_web/app.js`
- Modify: `src/mrs3/panel_web/app.css`
- Test: `tests/test_panel_static_ui.py`

**Interfaces:**
- Serializes current Pair/Side and stage order only when `#performance-v2-selection-xls` is clicked.

- [ ] **Step 1: Write failing static UI test**

```python
def test_selection_submits_only_on_xlsx_click():
    assert "/selection/xlsx" in APP_JS
    assert "selectionPreviewDirty" in APP_JS
```

- [ ] **Step 2: Verify the test is red**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_panel_static_ui.py -q`
Expected: FAIL because the preview has no endpoint.

- [ ] **Step 3: Implement minimum submit path**

Populate Pair/Side from existing catalog, serialize current order/check/scope, post only on click, and download workbook. Keep edits local and show typed errors in existing status text.

- [ ] **Step 4: Verify UI checks**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_panel_static_ui.py tests/test_panel_performance_v2.py -q`
Run: `node --check src/mrs3/panel_web/app.js`
Expected: PASS.

- [ ] **Step 5: Commit**

```text
git add src/mrs3/panel_web/index.html src/mrs3/panel_web/app.js src/mrs3/panel_web/app.css tests/test_panel_static_ui.py tests/test_panel_performance_v2.py
git commit -m "feat: run performance v2 finalist selection from panel"
```

### Task 6: Acceptance evidence

**Files:**
- Modify: `progress.md`
- Modify: `PRD.md`

- [ ] **Step 1: Run complete relevant tests**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_selection.py tests/test_panel_performance_v2.py tests/test_panel_static_ui.py -q`
Expected: PASS.

- [ ] **Step 2: Check diff and complete independent review**

Run: `git diff --check`
Expected: exit code 0.

- [ ] **Step 3: Record accepted evidence**

Update `progress.md` with commands/evidence and `PRD.md` only after independent review passes.

- [ ] **Step 4: Commit**

```text
git add PRD.md progress.md
git commit -m "docs: record performance v2 finalist selection evidence"
```

## Stage 3 (deferred)

No task implements persisted runs/results, tags, discard, RETEST, history, or importing edited XLSX tags. Create a focused spec only after Stage 2 output is accepted.
