# Tester RUNS Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a mutually exclusive local RUNS tester job with truthful HTML-count progress and move READY/run generation controls into Tester batch.

**Architecture:** The controller owns a `strategies.tester.runs` job that validates immutable `tester/runs/*.json`, clears only `tester/report/my_test_runs`, and starts `run_tester.bat` without an interactive console. Its status counts completed HTML files against snapshots every 15 seconds and shares the existing `tester` resource key with ordinary strategy batches. The static frontend renders both modes through one tester status/progress owner.

**Tech Stack:** Python standard library (`pathlib`, `subprocess`, `threading`), existing panel job registry, static HTML/CSS/JavaScript, pytest.

**Spec:** `docs/specs/2026-08-24-panel-dd5-frontend-tuning.md`

## Global Constraints

- Use only the configured `tester_runner.bot_root`; browser requests accept no paths or commands.
- RUNS writes reports only to `<bot_root>/tester/report/my_test_runs` and clears only that directory before start.
- RUNS and READY strategy batch share the `tester` resource key; neither can run while the other is active.
- RUNS progress is completed HTML count / immutable snapshot count, sampled no more frequently than every 15 seconds.
- A process exit with incomplete HTML count is `FAILED`; success requires exact count and no interactive console remains.
- Snapshot generation sets `tester_config.name_comment` to `runs`; no Source/materialization contracts change.

---

### Task 1: Backend RUNS job and snapshot report naming

**Files:**
- Modify: `src/mrs3/tester_run_files.py`
- Modify: `src/mrs3/panel.py`
- Test: `tests/test_tester_run_files.py`
- Test: `tests/test_panel_fresh_strategies.py`

**Interfaces:**
- Produces `PanelController.strategies_tester_runs_start() -> dict[str, object]` and `PanelController.strategies_tester_status(job_id) -> dict[str, object]` snapshots with `{mode: "RUNS", progress: {current, total, unit: "reports"}}`.
- Consumes only configured `RunnerConfig.bot_root`; run inputs are `bot_root/tester/runs/*.json` and outputs are `bot_root/tester/report/my_test_runs/*.html`.

- [ ] **Step 1: Write failing tests**

```python
def test_run_snapshot_sets_a_nonduplicating_report_comment(...):
    result = publish_run_snapshots(...)
    assert snapshot["tester_config"]["name_comment"] == "runs"

def test_runs_start_rejects_empty_directory_and_blocks_the_tester_resource(...):
    with pytest.raises(ValueError, match="Папка runs пуста"):
        controller.strategies_tester_runs_start({})
    assert captured_resource_keys == ("strategies.tester",)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `.venv\\Scripts\\python.exe -m pytest --basetemp .pytest-runs-red tests/test_tester_run_files.py tests/test_panel_fresh_strategies.py -q`

Expected: failure because the RUNS comment/job does not exist.

- [ ] **Step 3: Implement the smallest backend job**

```python
def strategies_tester_runs_start(self) -> dict[str, object]:
    return self._start_tracked_panel_job(
        "strategies.tester.runs", {}, ("strategies.tester",), self._runs_batch.start,
    )
```

Use one service that validates JSON snapshots, removes only `my_test_runs`, starts `cmd /c run_tester.bat` without `pause`, and publishes HTML file counts. It terminates the wrapper after the child has exited.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `.venv\\Scripts\\python.exe -m pytest --basetemp .pytest-runs-green tests/test_tester_run_files.py tests/test_panel_fresh_strategies.py -q`

Expected: PASS.

- [ ] **Step 5: Commit backend task**

```bash
git add src/mrs3/tester_run_files.py src/mrs3/panel.py tests/test_tester_run_files.py tests/test_panel_fresh_strategies.py
git commit -m "feat: run tester snapshots from panel"
```

### Task 2: Tester batch controls and one status owner

**Files:**
- Modify: `src/mrs3/panel_web/index.html`
- Modify: `src/mrs3/panel_web/app.js`
- Modify: `docs/specs/2026-08-24-panel-dd5-frontend-tuning.md`
- Test: `tests/test_panel_static_ui.py`

**Interfaces:**
- Consumes `strategies.tester.start` and `strategies.tester.runs` snapshots from `GET /api/v2/jobs` and `/api/v2/strategies/tester/status`.
- Produces one Tester batch status bar and progress bar; both start controls are disabled for either non-terminal tester job.

- [ ] **Step 1: Write failing static contract test**

```python
def test_tester_batch_owns_generation_and_runs_controls() -> None:
    assert 'id="shortlist-generate"' not in shortlist_body
    assert 'id="shortlist-generate-runs"' not in shortlist_body
    assert 'id="tester-runs-start"' in tester_body
    assert "strategies.tester.runs" in js
    assert "Проверить и запустить стратегии" in html
```

- [ ] **Step 2: Run test and confirm RED**

Run: `.venv\\Scripts\\python.exe -m pytest --basetemp .pytest-runs-ui-red tests/test_panel_static_ui.py -q`

Expected: failure because controls still belong to Shortlist and RUNS start is absent.

- [ ] **Step 3: Implement the minimal UI**

Move the two generation buttons and their messages into Tester batch below period buttons; add `Проверить и запустить RUNS`. Route both generation messages and both batch modes to that card's existing status/progress elements. Poll RUNS at 15 seconds, ordinary batch at its existing cadence, and disable both starts during either active job.

- [ ] **Step 4: Run static and relevant backend checks**

Run: `.venv\\Scripts\\python.exe -m pytest --basetemp .pytest-runs-ui-green tests/test_panel_static_ui.py tests/test_panel_fresh_strategies.py -q`

Run: `node --check src/mrs3/panel_web/app.js`

Expected: PASS and no JavaScript syntax error.

- [ ] **Step 5: Document and commit UI task**

Update the Tester batch contract with RUNS fixed report root, HTML-count progress, and shared lock; then:

```bash
git add src/mrs3/panel_web/index.html src/mrs3/panel_web/app.js docs/specs/2026-08-24-panel-dd5-frontend-tuning.md tests/test_panel_static_ui.py
git commit -m "feat: add tester runs controls"
```

## Final verification

- [ ] Run `.venv\\Scripts\\python.exe -m pytest --basetemp .pytest-runs-final tests/test_tester_run_files.py tests/test_panel_fresh_strategies.py tests/test_panel_static_ui.py -q`.
- [ ] Run `node --check src/mrs3/panel_web/app.js` and `git diff --check`.
- [ ] Request independent review; resolve findings and re-run the affected checks.
- [ ] Merge only reviewed commits into `main`, leaving unrelated local changes untouched.
