# Tester HTML Collision Lanes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep concurrent tester throughput while rejecting and retrying the rare HTML report collision produced by the closed-source bot.

**Architecture:** The controlled monitor retains the global concurrency window. A completed strategy remains complete only after its Result row, matching wizard entry, stable HTML file, and HTML strategy identity validate; a collision becomes a per-strategy retry rather than a batch-wide failure.

**Tech Stack:** Python 3, pytest, existing tester HTTP/wizard/HTML adapters.

## Global Constraints

- Do not change `hb_c.exe`, its JSON strategy contract, existing reports, or DuckDB/HTML import data.
- Keep `max_parallel_submissions` as the global cap and preserve `submission_delay_seconds`.
- Treat a missing, unstable, or mismatching HTML as unverified and retain the existing retry contract.

---

### Task 1: Collision-aware controlled monitor

**Files:**
- Modify: `src/mrs3/runner/monitor.py`
- Modify: `src/mrs3/runner/workflow.py`
- Test: `tests/runner/test_monitor.py`
- Modify: `docs/specs/2026-08-10-v06-runner-safe-root-json-smoke.md`
- Modify: `progress.md`

**Interfaces:**
- Consumes: visible `StrategyRow.symbol` and `StrategyRow.timeframe` for every installed strategy.
- Produces: `monitor_controlled_batch(..., collision_keys=...)` that never launches two incomplete names with the same key.

- [ ] **Step 1: Write the failing test**

```python
def test_controlled_monitor_serializes_names_with_same_html_collision_key(tmp_path):
    config = replace(_config(tmp_path), max_parallel_submissions=2)
    client = ControlledClient(config, ("A", "B", "C"))
    monitor_controlled_batch(
        client, ("A", "B", "C"), config.wizard_result, config.report_dir, config,
        collision_keys={"A": "ONUSDT|15m", "B": "ONUSDT|15m", "C": "ONUSDT|1h"},
    )
    assert client.launches[:2] == ["A", "C"]
    assert client.launches == ["A", "C", "B"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/runner/test_monitor.py::test_controlled_monitor_serializes_names_with_same_html_collision_key -q`

Expected: FAIL because `monitor_controlled_batch` has no `collision_keys` parameter.

- [ ] **Step 3: Write minimal implementation**

```python
def _next_launchable(pending, trackers, collision_keys):
    occupied = {
        collision_keys[name]
        for name, tracker in trackers.items()
        if tracker.attempts > 0 and not tracker.completed
    }
    for index, name in enumerate(pending):
        if collision_keys[name] not in occupied:
            return pending.pop(index)
    return None
```

Derive the mapping in `workflow.py` from the rows returned by `_wait_for_exact_batch` and pass it into the monitor.

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/runner/test_monitor.py tests/runner/test_workflow.py -q`

Expected: PASS except documented missing local tester fixtures, if selected by the command.

- [ ] **Step 5: Update contracts and commit**

Document that identical `symbol + timeframe` jobs are serialized as a temporary closed-binary workaround, while independent groups still fill the global window. Update `progress.md` with exact test evidence, run `git diff --check`, request independent review, then create a scoped `fix:` commit.
