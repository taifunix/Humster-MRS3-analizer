# Hamster Bot Tester Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows-safe runner that installs generated strategy JSON files, restarts one configured hamster-bot instance, launches every strategy through its HTMX endpoints, monitors progress, parses complete results, writes a CSV, and cleans only verified artifacts.

**Architecture:** The runner uses direct HTTP rather than browser automation. Process control resolves the listener PID from the configured port and verifies the executable path before shutdown or fallback termination. A transactional workflow separates preflight cleanup, launch, monitoring, result validation, CSV commit, and final cleanup so incomplete batches retain diagnostics.

**Tech Stack:** Python 3.11+, httpx, psutil, lxml, pandas, pytest, standard-library pathlib/subprocess/json/base64/shutil.

## Global Constraints

- Only the dedicated bot instance identified by configured URL, port, executable path, and resolved PID may be controlled.
- Primary shutdown is `POST /htmx/system/shutdown`; exact-PID termination is fallback only.
- The bot must be stopped before deleting reports or replacing strategies.
- Delete only the resolved configured directory ending in `tester/report/my_test`; reject broad or mismatched paths.
- Delete `tester/wizard_result.json` and `tester/wizard_progress.json` before a new batch.
- Strategies become visible only after the bot restarts.
- Do not call the global `/htmx/tester/run` endpoint.
- Launch each strategy through `GET /htmx/tester/wizard?single=<URL-encoded Base64 name>` followed by `POST /htmx/tester/wizard/run` with the returned full config and one strategy name.
- Multiple strategies may be submitted without waiting; the bot may queue them when `max_parallel_runs=1`.
- Live progress comes from `GET /htmx/tester/strategies-table`; `wizard_progress.json` is not a source of truth.
- Completion requires a Result button, a matching `wizard_result.json` entry, and a stable referenced HTML file.
- `wizard_result.json` is the authoritative strategy-to-report index; HTML supplies missing metrics such as Profit Factor.
- Any validation or parsing failure preserves reports and logs.
- Successful output is written atomically before reports and logs are removed.

---

## File Structure

- `src/mrs3/runner/config.py` — runner configuration and safe path resolution.
- `src/mrs3/runner/http.py` — HTMX endpoint client and HTML fragment parsers.
- `src/mrs3/runner/process.py` — exact-instance discovery, graceful shutdown, fallback termination, startup, and readiness.
- `src/mrs3/runner/files.py` — guarded cleanup and atomic strategy installation.
- `src/mrs3/runner/monitor.py` — table-state polling and progress events.
- `src/mrs3/runner/results.py` — wizard-result index, HTML report metrics, reconciliation, and CSV output.
- `src/mrs3/runner/workflow.py` — transactional batch state machine and dry-run plan.
- `tests/runner/` — local HTTP-server, process, filesystem, parser, and workflow tests.

### Task 1: Runner configuration and destructive-path guard

**Files:**
- Create: `src/mrs3/runner/__init__.py`
- Create: `src/mrs3/runner/config.py`
- Create: `tests/runner/test_config.py`

**Interfaces:**
- Produces: `RunnerConfig.from_json(path: Path) -> RunnerConfig`.
- Produces: `validate_report_directory(path: Path, bot_root: Path) -> Path`.

- [ ] **Step 1: Write failing path-safety tests**

```python
def test_accepts_exact_my_test_under_configured_bot_root(tmp_path):
    bot = tmp_path / "hb"
    target = bot / "tester" / "report" / "my_test"
    assert validate_report_directory(target, bot) == target.resolve()

@pytest.mark.parametrize("relative", [".", "tester", "tester/report", "other/my_test"])
def test_rejects_broad_or_wrong_cleanup_target(tmp_path, relative):
    bot = tmp_path / "hb"
    with pytest.raises(UnsafePathError):
        validate_report_directory(bot / relative, bot)
```

- [ ] **Step 2: Run `pytest tests/runner/test_config.py -q` and verify RED.**
- [ ] **Step 3: Implement canonical path resolution, URL/port/timeouts, exact log paths, staging/output paths, and immutable configuration.**
- [ ] **Step 4: Run configuration tests and verify green.**
- [ ] **Step 5: Commit with `git add src/mrs3/runner tests/runner/test_config.py && git commit -m "feat: validate tester runner configuration"`.**

### Task 2: HTMX client and wizard launch contract

**Files:**
- Create: `src/mrs3/runner/http.py`
- Create: `tests/runner/test_http.py`
- Create: `tests/fixtures/tester_table.html`
- Create: `tests/fixtures/tester_wizard.html`

**Interfaces:**
- Produces: `TesterHttpClient.list_strategies() -> tuple[StrategyRow, ...]`.
- Produces: `TesterHttpClient.launch_strategy(name: str) -> None`.
- Produces: `parse_wizard(html: str) -> WizardLaunch`.

- [ ] **Step 1: Write a failing real-response parser test**

```python
def test_wizard_post_uses_decoded_config_and_plain_strategy_name(fake_hb_server):
    client = TesterHttpClient(fake_hb_server.base_url)
    client.launch_strategy("BABASTOCK_1")
    assert fake_hb_server.requests[-1].path == "/htmx/tester/wizard/run"
    assert fake_hb_server.requests[-1].json["settings"] == ["BABASTOCK_1"]
    assert fake_hb_server.requests[-1].json["config"]["name_comment"] == "my_test"

def test_global_tester_run_endpoint_is_never_used(fake_hb_server):
    TesterHttpClient(fake_hb_server.base_url).launch_strategy("ADM1")
    assert all(r.path != "/htmx/tester/run" for r in fake_hb_server.requests)
```

- [ ] **Step 2: Run HTTP tests and verify they fail because the client is absent.**
- [ ] **Step 3: Implement Base64 URL encoding, wizard hidden-data decoding, full JSON POST, response checking, strategy-table parsing, and Result/runId extraction.**
- [ ] **Step 4: Run HTTP tests and full suite.**
- [ ] **Step 5: Commit with `git add src/mrs3/runner/http.py tests/runner && git commit -m "feat: launch tests through HTMX endpoints"`.**

### Task 3: Exact bot process control

**Files:**
- Create: `src/mrs3/runner/process.py`
- Create: `tests/runner/test_process.py`

**Interfaces:**
- Produces: `resolve_bot_process(config: RunnerConfig) -> BotProcess | None`.
- Produces: `stop_bot(config, client_factory) -> StopResult`.
- Produces: `start_bot(config) -> BotProcess`.

- [ ] **Step 1: Write failing multiple-process safety tests**

```python
def test_resolves_only_pid_listening_on_configured_port(process_fixture, config):
    wanted = process_fixture.add("C:/hb/hb_c.exe", listening_port=8087)
    process_fixture.add("D:/other/hb_c.exe", listening_port=8088)
    assert resolve_bot_process(config.with_port(8087)).pid == wanted.pid

def test_fallback_terminates_only_verified_pid(process_fixture, config, refusing_shutdown_client):
    wanted = process_fixture.add("C:/hb/hb_c.exe", listening_port=8087)
    other = process_fixture.add("C:/hb/hb_c.exe", listening_port=8088)
    stop_bot(config, lambda: refusing_shutdown_client)
    assert wanted.terminated
    assert not other.terminated
```

- [ ] **Step 2: Run process tests and verify RED.**
- [ ] **Step 3: Implement port-owner lookup with psutil, executable-path verification, graceful POST shutdown, bounded waits, exact-PID terminate/kill fallback, subprocess startup with configured cwd, and readiness polling.**
- [ ] **Step 4: Run process tests and full suite.**
- [ ] **Step 5: Commit with `git add src/mrs3/runner/process.py tests/runner/test_process.py && git commit -m "feat: control one configured bot instance"`.**

### Task 4: Guarded cleanup and strategy installation

**Files:**
- Create: `src/mrs3/runner/files.py`
- Create: `tests/runner/test_files.py`

**Interfaces:**
- Produces: `prepare_batch_files(config: RunnerConfig, source_strategies: Path) -> BatchFiles`.
- Produces: `cleanup_completed_batch(config: RunnerConfig) -> None`.

- [ ] **Step 1: Write failing transactional filesystem tests**

```python
def test_preparation_removes_whole_my_test_and_two_logs_then_installs_exact_batch(tmp_config):
    seed_stale_batch(tmp_config)
    source = seed_strategy_batch(tmp_config, names=["A", "B"])
    batch = prepare_batch_files(tmp_config, source)
    assert not tmp_config.report_dir.exists()
    assert not tmp_config.wizard_result.exists()
    assert not tmp_config.wizard_progress.exists()
    assert sorted(p.name for p in tmp_config.strategy_dir.glob("*.json")) == ["A.json", "B.json"]
    assert batch.expected_names == ("A", "B")

def test_invalid_strategy_json_leaves_existing_bot_files_untouched(tmp_config):
    original = seed_installed_strategy(tmp_config, "original")
    with pytest.raises(BatchPreparationError):
        prepare_batch_files(tmp_config, seed_invalid_json_batch(tmp_config))
    assert original.exists()
```

- [ ] **Step 2: Run filesystem tests and verify RED.**
- [ ] **Step 3: Implement pre-validation, exact report-tree removal, two-log removal, temporary staging, atomic replacement, expected-name manifest, and success-only cleanup.**
- [ ] **Step 4: Run filesystem tests and full suite.**
- [ ] **Step 5: Commit with `git add src/mrs3/runner/files.py tests/runner/test_files.py && git commit -m "feat: prepare isolated tester batches"`.**

### Task 5: Progress monitoring and completion state

**Files:**
- Create: `src/mrs3/runner/monitor.py`
- Create: `tests/runner/test_monitor.py`

**Interfaces:**
- Produces: `monitor_batch(client, expected_names, result_path, report_dir, config) -> BatchCompletion`.

- [ ] **Step 1: Write failing state-transition tests**

```python
def test_monitor_tracks_test_to_percent_to_result(scripted_client, batch_paths, config):
    scripted_client.tables = [table_ready("A"), table_progress("A", 0), table_progress("A", 62), table_result("A", "abc123")]
    write_result_and_report_on_last_poll(scripted_client, batch_paths, strategy="A", run_id="abc123")
    completion = monitor_batch(scripted_client, ("A",), batch_paths.result, batch_paths.reports, config)
    assert completion.strategies["A"].percent_history == (0, 62)
    assert completion.strategies["A"].completed

def test_result_button_without_matching_json_and_stable_html_is_not_complete(scripted_client, batch_paths, config):
    scripted_client.tables = [table_result("A", "abc123")]
    with pytest.raises(BatchTimeout):
        monitor_batch(scripted_client, ("A",), batch_paths.result, batch_paths.reports, config.with_timeout(.05))
```

- [ ] **Step 2: Run monitor tests and verify RED.**
- [ ] **Step 3: Implement row-state parsing, periodic table GET, percent events, stall/overall timeouts, expected-name tracking, result-index checks, path resolution, and two-observation size/mtime stability checks.**
- [ ] **Step 4: Run monitor tests and full suite.**
- [ ] **Step 5: Commit with `git add src/mrs3/runner/monitor.py tests/runner/test_monitor.py && git commit -m "feat: monitor tester batch completion"`.**

### Task 6: Result reconciliation, HTML metrics, and atomic CSV

**Files:**
- Create: `src/mrs3/runner/results.py`
- Create: `tests/runner/test_results.py`
- Copy test fixture from supplied `wizard_result.json` into `tests/fixtures/wizard_result.json`.
- Copy test fixture from supplied HTML report into `tests/fixtures/report_ADM1.html`.

**Interfaces:**
- Produces: `load_wizard_results(path: Path) -> tuple[WizardResult, ...]`.
- Produces: `parse_html_report(path: Path) -> HtmlReport`.
- Produces: `reconcile_results(expected_names, wizard_results, report_dir, tolerance) -> pd.DataFrame`.
- Produces: `write_results_csv_atomic(frame: pd.DataFrame, path: Path) -> Path`.

- [ ] **Step 1: Write failing supplied-fixture comparison tests**

```python
def test_supplied_json_maps_adm1_to_exact_report(supplied_wizard_result):
    result = load_wizard_results(supplied_wizard_result)[0]
    assert result.strategy_names == ("ADM1",)
    assert result.report_name == "my_test_run_001_of_001_ADMSTOCK_USDT_2h_2026-07-01_3.html"

def test_supplied_html_enriches_json_and_matches_core_metrics(supplied_wizard_result, supplied_html_report):
    row = reconcile_results(("ADM1",), load_wizard_results(supplied_wizard_result), supplied_html_report.parent, Decimal("0.01")).iloc[0]
    assert row.strategy_name == "ADM1"
    assert row.symbol == "ADMSTOCK_USDT"
    assert row.timeframe == "2h"
    assert row.profit_factor == pytest.approx(0.0070)
    assert row.total_pnl == pytest.approx(-6825.6651944)

def test_metric_mismatch_preserves_diagnostics_and_fails(mismatched_result_fixture):
    with pytest.raises(ResultMismatchError, match="TotalPnL"):
        reconcile_results(**mismatched_result_fixture)
```

- [ ] **Step 2: Run result tests and verify RED.**
- [ ] **Step 3: Implement JSON index parsing, chartUrl basename validation, full HTML metric tables, embedded strategy settings, JSON/HTML tolerance checks, one-result-per-expected-name enforcement, deterministic columns, and same-directory temporary CSV replacement.**
- [ ] **Step 4: Run result tests and full suite.**
- [ ] **Step 5: Commit with `git add src/mrs3/runner/results.py tests/runner tests/fixtures && git commit -m "feat: reconcile tester reports"`.**

### Task 7: Transactional workflow, dry-run, and CLI integration

**Files:**
- Create: `src/mrs3/runner/workflow.py`
- Modify: `src/mrs3/cli.py`
- Modify: `config.example.json`
- Create: `tests/runner/test_workflow.py`
- Create: `README.md`

**Interfaces:**
- Produces: `plan_batch(config, strategy_source) -> BatchPlan` without mutations.
- Produces: `run_batch(config, strategy_source, output_csv) -> BatchRunResult`.
- CLI: `mrs3 tester-plan --config PATH --strategies PATH`.
- CLI: `mrs3 tester-run --config PATH --strategies PATH --output-csv PATH`.

- [ ] **Step 1: Write failing end-to-end fake-server workflow tests**

```python
def test_dry_run_reports_actions_without_filesystem_process_or_http_mutation(workflow_fixture):
    plan = plan_batch(workflow_fixture.config, workflow_fixture.strategies)
    assert plan.expected_names == ("A", "B")
    assert workflow_fixture.mutation_count == 0

def test_successful_batch_writes_csv_before_cleanup(workflow_fixture):
    events = run_batch(workflow_fixture.config, workflow_fixture.strategies, workflow_fixture.output).events
    assert events.index("CSV_COMMITTED") < events.index("RAW_ARTIFACTS_REMOVED")

def test_failed_reconciliation_preserves_reports_and_logs(workflow_fixture):
    workflow_fixture.server.return_mismatched_result = True
    with pytest.raises(ResultMismatchError):
        run_batch(workflow_fixture.config, workflow_fixture.strategies, workflow_fixture.output)
    assert workflow_fixture.config.report_dir.exists()
    assert workflow_fixture.config.wizard_result.exists()
```

- [ ] **Step 2: Run workflow tests and verify RED.**
- [ ] **Step 3: Implement PRECHECK→STOPPED→CLEAN→INSTALLED→STARTED→SUBMITTED→MONITORING→RECONCILED→CSV_COMMITTED→CLEANED state transitions, recovery metadata, CLI logging, Ctrl+C preservation, dry-run output, and operational documentation.**
- [ ] **Step 4: Run `pytest -q`, `mrs3 tester-plan` against a temporary Windows-style configuration, and the full fake-server integration test.**
- [ ] **Step 5: Commit with `git add src/mrs3/runner src/mrs3/cli.py config.example.json README.md tests/runner && git commit -m "feat: orchestrate hamster bot test batches"`.**

