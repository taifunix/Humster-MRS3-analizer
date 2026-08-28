# Fast Strategy Test Implementation Plan

> **For Codex:** Execute this plan task-by-task with TDD. Keep the existing ordinary tester and RUNS behavior unchanged. Do not call `runner.workflow.run_batch` from the new module.

**Goal:** Add an independent **Fast TEST стратегии** panel action that runs READY strategy JSON in bounded batches, continues after individual report failures, and leaves only failed strategy JSON available for recovery.

**Architecture:** Extend the existing READY generation manifest with compact plateau diagnostics, add an opt-in partial-completion mode to the existing low-level report monitor, and build one new panel service around the existing tester process/HTTP primitives. Reuse the panel job registry and tester resource lock; do not reuse the old workflow coordinator, inbox or `import_audit` path.

**Tech Stack:** Python 3.11+, standard library, existing `mrs3.runner` modules, Panel Web vanilla HTML/JavaScript, pytest.

**Spec:** `docs/specs/2026-08-27-panel-fast-strategy-test.md`

**Global Constraints:**

- Run tests only with `.venv\Scripts\python.exe -m pytest` plus the explicit
  test paths listed below.
- Preserve all unrelated dirty-worktree changes.
- Do not add dependencies, migrations, backup folders or generated artifacts.
- Strategy JSON remains template-only; lineage stays in manifests.
- Destructive cleanup is limited to the exact validated tester strategy/report directories.
- Every behavior change starts with a focused failing test.
- Before a commit: focused/broader tests, `git diff --check`, staged diff inspection and independent review as required by `AGENTS.md`.

### Reload recovery correction

On panel reload, a Fast job with persisted `verified_reports` is selected before
any historical committed tester inbox. This keeps the `Проверить` action bound
to the reports currently present in `tester/report/my_test`; an older ordinary
batch with `inbox_ready=true` no longer masks that Fast batch.

---

## Task 1: Persist order-level plateau diagnostics in the generation manifest

**Files:**

- Modify: `src/mrs3/fresh_analysis_strategies.py`
- Test: `tests/test_fresh_analysis_strategies.py`

### Step 1: Write the failing manifest test

Add a test beside the existing generated-manifest assertions:

```python
def test_generation_manifest_persists_order_plateau_diagnostics(tmp_path: Path) -> None:
    analysis_id, _ = _make_analysis(tmp_path / "run.analysis-v6.duckdb")
    result = generate_fresh_analysis_strategies(
        tmp_path / "run.analysis-v6.duckdb",
        analysis_id,
        ["STR-READY"],
        [("BTCUSDT", "LONG", "1h")],
        _template(tmp_path),
        tmp_path / "out",
        AlgorithmConfig.defaults(),
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    diagnostic = manifest["candidate_diagnostics"]["STR-READY"]
    assert diagnostic == {
        "order_count": 2,
        "orders": [
            {
                "order_id": 1,
                "plateau_id": "P100",
                "plateau_point_count": 3,
                "base_point_trades": 10,
                "plateau_total_trades": 30,
            },
            {
                "order_id": 2,
                "plateau_id": "P300",
                "plateau_point_count": 4,
                "base_point_trades": 11,
                "plateau_total_trades": 40,
            },
        ],
    }
```

Also assert that changing one persisted diagnostic makes
`generation_manifest_sha256` invalid under the same canonical hashing rule.

### Step 2: Run the test and confirm failure

```powershell
.venv\Scripts\python.exe -m pytest tests/test_fresh_analysis_strategies.py::test_generation_manifest_persists_order_plateau_diagnostics -q
```

Expected: FAIL because `candidate_diagnostics` is absent.

### Step 3: Add one normalization helper

In `fresh_analysis_strategies.py`, add a private helper that consumes the
already validated `_plateau_diagnostics(structure)` result and the existing
`structure["orders"]` list:

```python
def _candidate_diagnostics(structure: Mapping[str, object]) -> dict[str, object]:
    values = _plateau_diagnostics(structure)
    orders = list(structure["orders"])
    count = int(structure["order_count"])

    def at(name: str, index: int) -> int:
        value = values[name]
        return int(value if count == 1 else value[index])

    return {
        "order_count": count,
        "orders": [
            {
                "order_id": int(order["id"]),
                "plateau_id": str(order["plateau_id"]),
                "plateau_point_count": at("plateau_point_count", index),
                "base_point_trades": at("base_point_trades", index),
                "plateau_total_trades": at("plateau_total_trades", index),
            }
            for index, order in enumerate(orders)
        ],
    }
```

Build `candidate_diagnostics` once per selected candidate and add it to
`manifest_unsigned` before calculating `generation_manifest_sha256`. Keep
`format_version=1`; the existing ordinary validator already tolerates extra
hashed fields and old batches retain compatibility.

Do not add these fields to calls to `generate_strategy()` or the emitted JSON.

### Step 4: Run focused generation tests

```powershell
.venv\Scripts\python.exe -m pytest tests/test_fresh_analysis_strategies.py tests/test_panel_strategy_batch.py -q
```

Expected: PASS; existing manifest validation and strategy hashes remain valid.

### Step 5: Inspect the strategy payload boundary

```powershell
git diff -- src/mrs3/fresh_analysis_strategies.py tests/test_fresh_analysis_strategies.py
```

Confirm `candidate_diagnostics` occurs only in the manifest path and no
`provenance` or diagnostic array is added to generated strategy JSON.

### Step 6: Commit the scoped contract change

```powershell
git add src/mrs3/fresh_analysis_strategies.py tests/test_fresh_analysis_strategies.py
git commit -m "feat: persist fast test plateau diagnostics"
```

---

## Task 2: Let the low-level monitor return partial completion on request

**Files:**

- Modify: `src/mrs3/runner/monitor.py`
- Modify: `src/mrs3/runner/results.py`
- Test: `tests/runner/test_monitor.py`
- Test: `tests/runner/test_results.py`

### Step 1: Write failing partial-monitor tests

Add one test using the existing fake controlled client:

```python
def test_controlled_monitor_can_finish_partially_without_aborting_other_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completion = monitor_controlled_batch(
        client,
        ("MISSING", "GOOD"),
        result_path,
        report_dir,
        replace(config, max_strategy_attempts=4),
        allow_partial=True,
    )

    assert completion.failed_names == ("MISSING",)
    assert completion.strategies["MISSING"].attempts == 4
    assert completion.strategies["GOOD"].completed is True
    assert client.maximum_simultaneous <= config.max_parallel_submissions
```

Retain an explicit regression test showing the default call still raises
`BatchRetryExhausted` at the same point.

Add a result helper test:

```python
def test_extract_html_strategy_settings_returns_the_single_embedded_object(tmp_path: Path) -> None:
    report = _report(tmp_path, name="FAST")
    settings = extract_html_strategy_settings(report)
    assert settings is not None
    assert settings["name"] == "FAST"
    assert settings["basic"]["symbol"] == "BTCUSDT"
```

### Step 2: Confirm both tests fail

```powershell
.venv\Scripts\python.exe -m pytest tests/runner/test_monitor.py -k "finish_partially or retry" -q
.venv\Scripts\python.exe -m pytest tests/runner/test_results.py -k "strategy_settings" -q
```

Expected: FAIL because `allow_partial`, `failed_names` and the public settings
extractor do not exist.

### Step 3: Extend the existing tracker, not the old workflow

Make the smallest backwards-compatible monitor change:

```python
@dataclass(frozen=True, slots=True)
class BatchCompletion:
    strategies: dict[str, StrategyCompletion]
    polls: int
    elapsed_seconds: float
    failed_names: tuple[str, ...] = ()


def monitor_controlled_batch(
    client: ControlledStrategyClient,
    expected_names: tuple[str, ...],
    result_path: Path,
    report_dir: Path,
    config: RunnerConfig,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    collision_keys: Mapping[str, str] | None = None,
    verified_report_dir: Path | None = None,
    snapshot_report_dir: Path | None = None,
    initial_attempt_counts: Mapping[str, int] | None = None,
    attempts_callback: Callable[[dict[str, int]], None] | None = None,
    *,
    allow_partial: bool = False,
) -> BatchCompletion:
```

Add `failed: bool = False` to `_Tracker`. In the exhausted branch:

```python
if exhausted and not allow_partial:
    raise BatchRetryExhausted(
        "tester retry limit exceeded for strategies: "
        + ", ".join(sorted(exhausted))
    )
for name in exhausted:
    trackers[name].failed = True
```

Exclude failed trackers from `active_count`, treat `completed or failed` as
terminal, and return sorted `failed_names`. Do not change launch counting,
grace timing, collision detection or the default exception path.

Refactor the existing regex parsing in `results.py` once. The new
`extract_html_strategy_settings(path: Path) -> dict[str, object] | None`
contains the current lightweight `<pre>` scan and returns the single complete
settings object. `extract_html_strategy_name(path)` calls it and returns the
string `settings["name"]`, or `None` when the settings object is unavailable.

No DOM or full performance parser is added to Fast TEST report polling.

### Step 4: Run focused runner tests

```powershell
.venv\Scripts\python.exe -m pytest tests/runner/test_monitor.py tests/runner/test_results.py -q
```

Expected: PASS, including all old default retry behavior.

### Step 5: Commit the reusable primitive

```powershell
git add src/mrs3/runner/monitor.py src/mrs3/runner/results.py tests/runner/test_monitor.py tests/runner/test_results.py
git commit -m "feat: allow partial controlled tester completion"
```

---

## Task 3: Implement the independent Fast TEST service

**Files:**

- Create: `src/mrs3/panel_fast_strategy_test.py`
- Create: `tests/test_panel_fast_strategy_test.py`
- Read/reuse only: `src/mrs3/runner/config.py`
- Read/reuse only: `src/mrs3/runner/http.py`
- Read/reuse only: `src/mrs3/runner/process.py`
- Read/reuse only: `src/mrs3/runner/workflow.py::_wait_for_exact_batch`

### Step 1: Write the service contract tests first

Use temporary exact bot paths and injected fake functions. Cover four cases:

```python
def test_fast_test_replaces_strategy_dir_for_each_chunk(tmp_path: Path) -> None:
    # 5 strategies, strategy_batch_size=2
    # Every start callback observes exactly [2, 2, 1] JSON files.
    # The service stops before installing the next chunk.


def test_fast_test_continues_after_one_exhausted_strategy(tmp_path: Path) -> None:
    # First chunk returns failed_names=("S2",), second chunk completes.
    # Final phase is PARTIAL and S3/S4 were still executed.


def test_fast_test_partial_leaves_only_failed_json(tmp_path: Path) -> None:
    # settings_strategy contains exactly S2.json after terminal reconciliation.


def test_fast_retry_accepts_manual_report_without_starting_bot(tmp_path: Path) -> None:
    # Add a stable report whose embedded settings/date contract matches S2.
    # retry() reaches COMMITTED, clears settings_strategy, start callback is unused.
```

Also add focused tests for:

- four total attempts, not four retries;
- cancel leaves all currently incomplete JSON and preserves reports;
- fatal path/config errors do not clear unvalidated locations;
- tester config changes only the fields in specification section 5;
- atomic manifest records attempt counts, failed names, report filenames,
  candidate mapping and diagnostics;
- stable snapshot filenames in `verified_reports` remain the authoritative report
  set; no post-run all-reports deduplication pass is required.

### Step 2: Run and confirm import failure

```powershell
.venv\Scripts\python.exe -m pytest tests/test_panel_fast_strategy_test.py -q
```

Expected: FAIL because `mrs3.panel_fast_strategy_test` does not exist.

### Step 3: Define the minimal public service API

Create exactly one orchestration class with these public signatures:

```python
class FastStrategyTestError(ValueError):
    pass


class LocalFastStrategyTestService:
    def start(
        self,
        manifest_path: Path,
        *,
        analysis_run_id: str,
        start_date: str,
        end_date: str,
        job_id: str,
    ) -> dict[str, object]:
        """Validate and start one new Fast TEST worker."""

    def retry(self, source_job_id: str, *, job_id: str) -> dict[str, object]:
        """Rescan and grant one additional attempt to remaining failures."""

    def status(self, job_id: str) -> dict[str, object]:
        """Return the redacted worker snapshot."""

    def cancel(self, job_id: str) -> dict[str, object]:
        """Request cancellation and return the current snapshot."""

    def has_active_job(self) -> bool:
        """Return whether this service currently owns the tester."""
```

Its constructor accepts `RunnerConfig` and keyword-injected `start_bot`,
`stop_bot`, `client_factory`, `wait_for_exact_batch`, `monitor` and `on_update`
callables, using the existing runner functions as defaults. This keeps tests
deterministic without introducing a new interface hierarchy.

Keep the worker record as one private dataclass in this module. Do not create a
base service, repository, event bus or second state abstraction.

### Step 4: Validate once and write the Fast manifest

At `start()`:

1. call existing `validate_strategy_manifest(manifest_path)`;
2. require matching `analysis_run_id`;
3. validate ISO dates and `start_date <= end_date`;
4. validate `candidate_diagnostics` and name → candidate mapping from the
   hashed generation manifest;
5. call existing runner path validation before any delete;
6. atomically write `fast_test_manifest.json` with a sibling `.tmp` plus
   `os.replace`.

The manifest stores the validated generation-manifest path for recovery, but
the status snapshot never exposes it.

### Step 5: Configure, clear and install directly

Implement three small private functions in the same module with exact
signatures: `_write_fast_tester_config(config: RunnerConfig, start: str,
end: str) -> None`, `_clear_directory(path: Path, *, expected: Path) -> None`,
and `_install_names(source: Path, target: Path, names: tuple[str, ...]) -> None`.

`_clear_directory` resolves both paths and refuses a mismatch before
`shutil.rmtree`. `_install_names` copies only `<name>.json` after hash/name
validation already succeeded. Do not call `prepare_batch_files`; do not create
backup or staging directories.

On a fresh start, clear the exact report directory once, recreate it, then write
the Fast manifest. On every chunk, stop the bot, clear the exact strategy
directory and install only that chunk.

The config write also sets `use_runs=false`; otherwise a previous RUNS session
could make the bot ignore the installed strategy JSON. It must not change
`max_parallel_runs` or any other tester-owned concurrency setting.

### Step 6: Run chunks with existing low-level primitives

For each deterministic chunk:

```python
start_bot(config)
client = client_factory(config)
wait_for_exact_batch(client, chunk, config)
completion = monitor(
    client,
    chunk,
    config.wizard_result,
    config.report_dir,
    config,
    progress_callback=record_progress,
    initial_attempt_counts=attempt_counts,
    attempts_callback=record_attempts,
    allow_partial=True,
)
```

Merge verified reports and failed names into the in-memory state, atomically
refresh the Fast manifest after every chunk, and stop the bot in `finally`.
Proceed to the next chunk even when `completion.failed_names` is non-empty.

The status snapshot uses:

```python
{
    "state": "RUNNING|COMMITTED|CANCELLED|FAILED",
    "phase": "RUNNING|PARTIAL|COMMITTED|CANCELLED|FAILED",
    "mode": "FAST",
    "progress": {
        "current": verified_count,
        "total": expected_count,
        "batch_current": batch_number,
        "batch_total": batch_count,
        "active": active_count,
        "retries": retry_count,
        "failed": failed_count,
    },
    "evidence": {"failed_names": ["S2"]},
    "error": None | {"code": "TESTER_HTTP_FAILED", "message": "path-safe text"},
}
```

For partial completion return `state="COMMITTED", phase="PARTIAL"`.

### Step 7: Implement exact terminal folder reconciliation

Use one `_publish_incomplete(names)` function:

- empty names → clear `settings_strategy`;
- non-empty names → clear it, then copy exactly those source JSON;
- if count exceeds `strategy_batch_size`, never start the bot after publishing.

Cancellation sets the event, stops the bot, rescans accepted reports, then
publishes every still-incomplete name.

### Step 8: Implement recovery as rescan plus one attempt

`retry(source_job_id, job_id=new_job_id)` reads and validates the current Fast
manifest, rescans HTML first and compares the lightweight embedded settings to
the expected strategy plus requested dates. If no failures remain, reconcile
the folder and return COMMITTED without bot startup.

Otherwise group remaining names by their prior attempt count, then make one
runtime config copy per group with:

```python
replace(config, max_strategy_attempts=group_previous_attempts + 1)
```

Split each equal-attempt group further by `strategy_batch_size`, pass its
previous counts as `initial_attempt_counts`, and allow exactly one new attempt
per still-failed name during this button press. Persist the cumulative count.

### Step 9: Run service tests

```powershell
.venv\Scripts\python.exe -m pytest tests/test_panel_fast_strategy_test.py tests/runner/test_monitor.py tests/runner/test_results.py -q
```

Expected: PASS.

### Step 10: Commit the independent service

```powershell
git add src/mrs3/panel_fast_strategy_test.py tests/test_panel_fast_strategy_test.py
git commit -m "feat: add independent fast strategy tester"
```

---

## Task 4: Wire Fast TEST into the panel controller and shared tester lock

**Files:**

- Modify: `src/mrs3/panel.py`
- Test: `tests/test_panel_fresh_strategies.py`
- Test: `tests/test_panel_jobs.py`

### Step 1: Write failing controller tests

Add tests asserting:

```python
def test_fast_tester_start_uses_own_service_and_shared_tester_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = controller.panel_job_submit({
        "kind": "strategies.tester.fast.start",
        "request": {
            "analysis_run_id": analysis_id,
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
        },
    })
    assert job["kind"] == "strategies.tester.fast.start"
    assert job["resource_keys"] == ["strategies.tester"]


```

Add three further named tests:

- `test_fast_and_old_tester_jobs_cannot_own_the_resource_together`;
- `test_fast_retry_uses_failed_job_manifest_not_current_analysis`;
- `test_tester_cancel_routes_to_fast_owner`;
- `test_old_tester_start_still_routes_to_local_strategy_batch`.

### Step 2: Confirm failure

```powershell
.venv\Scripts\python.exe -m pytest tests/test_panel_fresh_strategies.py tests/test_panel_jobs.py -k "fast or tester_resource" -q
```

Expected: FAIL because Fast job kinds are unsupported.

### Step 3: Add one lazy service field and router branches

In `PanelController`:

```python
self._fast_strategy_test_service: LocalFastStrategyTestService | None = None

def _fast_strategy_test(self) -> LocalFastStrategyTestService:
    if self._fast_strategy_test_service is None:
        self._fast_strategy_test_service = LocalFastStrategyTestService(
            RunnerConfig.from_json(self.default_config),
            on_update=self._record_special_job,
        )
    return self._fast_strategy_test_service
```

Add request handlers:

```python
strategies.tester.fast.start
strategies.tester.fast.retry
```

Both call `_start_tracked_panel_job` with the exact resource tuple
`("strategies.tester",)`.
Start accepts only analysis id/start/end. Retry accepts only `job_id` and passes
that source id to the service. Unexpected fields are rejected.

### Step 4: Route status and cancellation by tracked kind

Update `strategies_tester_status()` and `strategies_tester_cancel()` with one
small kind switch:

- RUNS → existing RUNS service;
- Fast start/retry → Fast service;
- otherwise → existing ordinary service.

Include the Fast service in `has_active_panel_jobs()`. Do not include Fast jobs
in old verified-inbox reconciliation; Fast has no inbox by design.

For a restarted controller whose worker memory is gone,
`_tracked_job_or_interrupted` returns the persisted job snapshot. Recovery is a
new Fast retry job using the durable Fast manifest and reports.

### Step 5: Run controller regressions

```powershell
.venv\Scripts\python.exe -m pytest tests/test_panel_fresh_strategies.py tests/test_panel_jobs.py tests/test_panel_strategy_batch.py tests/test_panel_tester_runs.py -q
```

Expected: PASS; old ordinary and RUNS tests remain unchanged.

### Step 6: Commit controller wiring

```powershell
git add src/mrs3/panel.py tests/test_panel_fresh_strategies.py tests/test_panel_jobs.py
git commit -m "feat: expose fast strategy tester jobs"
```

---

## Task 5: Add Fast TEST and recovery controls to Tester batch

**Files:**

- Modify: `src/mrs3/panel_web/index.html`
- Modify: `src/mrs3/panel_web/app.js`
- Modify only if existing styles are insufficient: `src/mrs3/panel_web/app.css`
- Test: `tests/test_panel_static_ui.py`

### Step 1: Write failing static UI tests

Add assertions for:

```python
def test_tester_card_exposes_independent_fast_test_controls() -> None:
    html = _read("index.html")
    js = _read("app.js")
    assert 'id="tester-start-fast"' in html
    assert '>Fast TEST стратегии<' in html
    assert 'id="tester-retry-fast"' in html
    assert '>Проверить / повторить FAILED<' in html
    assert "kind: 'strategies.tester.fast.start'" in js
    assert "kind: 'strategies.tester.fast.retry'" in js


```

Add three further named tests:

- `test_fast_status_uses_phase_and_failed_names_without_paths`;
- `test_all_tester_start_buttons_share_one_busy_state`;
- `test_reload_recovers_fast_job_snapshot`.

### Step 2: Confirm failure

```powershell
.venv\Scripts\python.exe -m pytest tests/test_panel_static_ui.py -k "fast or tester" -q
```

Expected: FAIL because the controls and job kinds are absent.

### Step 3: Add two buttons without redesigning the card

Beside the existing ordinary tester button add:

```html
<button type="button" id="tester-start-fast" class="button button-primary">
  Fast TEST стратегии
</button>
<button type="button" id="tester-retry-fast" class="button button-secondary" disabled>
  Проверить / повторить FAILED
</button>
```

Reuse current button classes. Do not add CSS unless wrapping is visibly broken.

### Step 4: Extend the existing tester state functions

Add Fast controls to `setTesterControls(busy)`. Submit Fast start with the same
validated date fields and current `analysis_run_id` used by the ordinary start.
Submit retry with the selected Fast source job id.

In `renderTester(job)`:

- when `job.mode === 'FAST'`, render
  `FAST TEST · batch X / N · готово A / T · активно R · повторы K`;
- for `phase === 'PARTIAL'`, render
  `PARTIAL · A / T reports · FAILED F` and list `evidence.failed_names`;
- for full commit render `COMPLETED · T / T reports`;
- enable recovery only for Fast PARTIAL/CANCELLED and no active tester owner;
- keep **Стоп** enabled only for a running owner.

Add Fast kinds to reload recovery and poll at the existing one-second ordinary
tester interval. Do not expose manifest or local filesystem paths.

### Step 5: Run UI syntax and focused tests

```powershell
node --check src/mrs3/panel_web/app.js
.venv\Scripts\python.exe -m pytest tests/test_panel_static_ui.py tests/test_panel_fresh_strategies.py -q
```

Expected: PASS.

### Step 6: Commit the panel controls

```powershell
git add src/mrs3/panel_web/index.html src/mrs3/panel_web/app.js tests/test_panel_static_ui.py
git commit -m "feat: add fast tester panel controls"
```

---

## Task 6: Verify the complete independent contour and document evidence

**Files:**

- Modify: `progress.md`
- Modify: `PRD.md` only if implementation status changes from Planned
- Modify if verified behavior needs clarification: `docs/specs/2026-08-27-panel-fast-strategy-test.md`
- No generated artifacts committed

### Step 1: Run the relevant automated suite

```powershell
.venv\Scripts\python.exe -m pytest tests/test_fresh_analysis_strategies.py tests/test_panel_fast_strategy_test.py tests/test_panel_fresh_strategies.py tests/test_panel_jobs.py tests/test_panel_strategy_batch.py tests/test_panel_tester_runs.py tests/test_panel_static_ui.py tests/runner/test_monitor.py tests/runner/test_results.py -q
node --check src/mrs3/panel_web/app.js
git diff --check
```

Expected: all tests PASS, JavaScript syntax PASS and no whitespace errors.

### Step 2: Run a five-strategy real smoke

Using disposable READY strategies and the configured local tester:

1. start Fast TEST with five strategies and confirm no more than
   `max_parallel_submissions` are active;
2. confirm five matching stable HTML reports and COMPLETED;
3. retain the Fast manifest and exact command/output evidence outside Git.

Do not use production strategy/report folders unless the user has explicitly
approved their cleanup for this smoke.

### Step 3: Run the partial and recovery smoke

1. arrange one disposable strategy to produce no accepted report;
2. confirm four total attempts and PARTIAL while later strategies finish;
3. confirm `settings_strategy` contains only that failed JSON;
4. add a matching manual HTML report;
5. press **Проверить / повторить FAILED**;
6. confirm COMPLETED without a bot restart and an empty strategy directory.

### Step 4: Run the multi-chunk smoke

Use `strategy_batch_size + 1` disposable strategies. Confirm two bot starts,
the first chunk is removed before the second is installed, and the active
submission count remains bounded.

### Step 5: Update status documentation with evidence

At the top of `progress.md`, record:

- exact automated command and pass count;
- five-strategy, partial/recovery and multi-chunk smoke outcomes;
- any remaining blocker;
- next step: separate Performance DB consumer integration, if still pending.

Change the PRD registry row from **Planned** to **Implemented / verified** only
after all required evidence is present. Do not claim Performance DB integration
or cleanup as part of this feature.

### Step 6: Independent review and final commit

Follow `AGENTS.md`:

1. inspect `git status --short` and the scoped staged diff;
2. request an independent correctness review;
3. fix confirmed findings and rerun affected tests;
4. run `git diff --check` again;
5. create one final documentation/status commit if needed:

```powershell
git add progress.md PRD.md docs/specs/2026-08-27-panel-fast-strategy-test.md
git commit -m "docs: record fast tester verification"
```

### Step 7: Do not start follow-up scope implicitly

Performance DB import from `fast_test_manifest.json`, DD5, and post-commit
cleanup of reports/strategies require a separate approved specification and
plan. Stop after reporting Fast TEST evidence.
