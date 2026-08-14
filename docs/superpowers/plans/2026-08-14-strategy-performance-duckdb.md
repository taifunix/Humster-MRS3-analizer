# Strategy Performance DuckDB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Persist complete MRS3 tester evidence in a separate DuckDB, calculate DD5 from it, and delete inbox HTML only after auditable readback.

**Architecture:** The runner remains name-only for live tester monitoring, but atomically copies each verified HTML, its strategy JSON and tester commission configuration into an immutable inbox. A performance importer inventories and parses those snapshots, writes immutable facts to a new DuckDB in one transaction, and generates DD5/XLSX from database rows.

**Tech Stack:** Python 3.12, DuckDB, pandas, lxml, pytest, openpyxl through the existing audit writer.

## Global Constraints

- Database path is exactly data/databases/strategy_performance.duckdb; it is local and ignored.
- Inbox path is exactly data/tester_inbox/<batch_id>/; bot-owned reports never become durable evidence.
- exchange.name in canonical strategy JSON and MakerFee, TakerFee, SlippagePercent, FundingRate and FundingIntervalHours from immutable tester_config are required. No default, UNKNOWN, tester version or market-data hash.
- Test identity is SHA-256 canonical JSON of strategy version ID, UTC [start,end) milliseconds, normalized exchange and commission contract ID.
- Full parser must validate its semantic result against an independent structural inventory of copied HTML bytes.
- Any parse, validation, conflict or readback failure preserves all inbox HTML and writes v4 audit evidence even if DuckDB rolls back.
- HTML deletion requires schema_version=4 audit evidence, zero quarantines, successful readback, safe_to_delete=YES and per-file DELETE_READY -> DELETING -> DELETED state.
- DD5 is CALCULATION_ONLY and uses the existing normalize_dd5_row formula, not a new tick-test.
- Preserve unrelated dirty worktree changes. Do not touch MRS2 databases or HTML outside the new inbox.

---

### Task 1: Immutable Runner Inbox

**Files:**
- Create: src/mrs3/runner/inbox.py
- Modify: src/mrs3/runner/config.py, src/mrs3/runner/workflow.py, config.example.json, config.local.json.example, .gitignore
- Test: tests/runner/test_inbox.py, tests/runner/test_config.py, tests/runner/test_workflow.py

**Interfaces:**
- Consumes: verified one-strategy WizardResult, stable HTML bytes and the immutable generated strategy JSON.
- Produces: capture_verified_inbox(config, output_csv, plan, results, report_paths) -> Path.
- Adds required RunnerConfig.tester_config and RunnerConfig.inbox_root. tester_config is inside bot_root; inbox_root is outside bot_root.

- [ ] **Step 1: Write the failing test**

~~~python
def test_capture_copies_exact_html_strategy_and_fee_contract(tmp_path: Path) -> None:
    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})
    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["commission_contract"]["MakerFee"] == "0.0002"
    entry = manifest["entries"][0]
    assert (inbox / entry["report_path"]).read_bytes() == report.read_bytes()
    assert entry["strategy_name"] == "A"

def test_capture_rejects_missing_maker_fee(tmp_path: Path) -> None:
    with pytest.raises(InboxCaptureError, match="MakerFee"):
        capture_verified_inbox(incomplete_config, output, plan, (wizard,), {"A": report})
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: .venv\Scripts\python.exe -m pytest tests/runner/test_inbox.py -q

Expected: FAIL because inbox capture has not been implemented.

- [ ] **Step 3: Write minimal implementation**

Read immutable tester_config once before the first submission. Copy the exact verified report bytes and source JSON through temporary files plus atomic rename. Hash the destination bytes. Use opaque hash-derived manifest_entry_id in filenames. Publish the manifest only after all required entries exist. Call capture after reconcile_results and before bot cleanup.

- [ ] **Step 4: Run focused tests to verify it passes**

Run: .venv\Scripts\python.exe -m pytest tests/runner/test_inbox.py tests/runner/test_config.py tests/runner/test_workflow.py -q

Expected: PASS; existing name-only reconciliation test still proves the full parser is not run by the runner.

- [ ] **Step 5: Commit**

~~~bash
git add src/mrs3/runner/inbox.py src/mrs3/runner/config.py src/mrs3/runner/workflow.py tests/runner/test_inbox.py tests/runner/test_config.py tests/runner/test_workflow.py config.example.json config.local.json.example .gitignore
git commit -m "feat: capture immutable tester performance inbox"
~~~

### Task 2: Strict Performance Parser and Schema

**Files:**
- Create: src/mrs3/performance.py, src/mrs3/performance_store.py, tests/test_performance.py, tests/test_performance_store.py
- Modify: tests/fixtures/duckdb_import/report_a.html, tests/fixtures/duckdb_import/report_b.html

**Interfaces:**
- Consumes: immutable HTML bytes, strategy JSON and commission contract from the inbox.
- Produces: parse_performance_report(source: bytes) -> ParsedPerformanceReport and initialize_performance_database(path: Path) -> None.
- ParsedPerformanceReport holds canonical settings, metrics, actions, equity data and StructuralInventory. It does not open paths or write state.

- [ ] **Step 1: Write the failing test**

~~~python
def test_parser_inventory_matches_semantic_counts() -> None:
    parsed = parse_performance_report(REPORT_A.read_bytes())
    assert parsed.inventory.trade_rows == len(parsed.actions) == 2
    assert parsed.inventory.wallet_samples == parsed.inventory.equity_samples == 3

def test_parser_rejects_second_equity_series() -> None:
    source = REPORT_A.read_bytes() + b"<script>const equitySeries=[];</script>"
    with pytest.raises(PerformanceParseError, match="exactly one equitySeries"):
        parse_performance_report(source)

def test_schema_rejects_unknown_version(tmp_path: Path) -> None:
    database = tmp_path / "strategy_performance.duckdb"
    initialize_performance_database(database)
    with duckdb.connect(str(database)) as connection:
        connection.execute("UPDATE schema_info SET value='999' WHERE key='schema_version'")
    with pytest.raises(PerformanceStoreError, match="unknown schema version"):
        initialize_performance_database(database)
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: .venv\Scripts\python.exe -m pytest tests/test_performance.py tests/test_performance_store.py -q

Expected: FAIL because performance parser and store do not exist.

- [ ] **Step 3: Write minimal implementation**

Independently inventory one settings object, Metric/Value tables, one trade table and one wallet/equity series. Record raw headers, counts and UTC time bounds. Reject missing, duplicate, malformed or non-monotonic mandatory data. Semantic counts must exactly equal inventory counts.

Create only the approved v1 tables and views: schema_info, import_runs, import_files, strategy_versions, backtest_runs, backtest_metrics, backtest_actions, backtest_equity, dd5_runs, dd5_results, latest_backtest_by_strategy_version, dd5_latest_results and portfolio_layer_a_input. Persist instants as TIMESTAMPTZ; schema_info includes schema_version=1 and import_evidence_schema_version=4.

- [ ] **Step 4: Run focused tests to verify it passes**

Run: .venv\Scripts\python.exe -m pytest tests/test_performance.py tests/test_performance_store.py -q

Expected: PASS; removing a required series from a fixture produces Structural Quarantine.

- [ ] **Step 5: Commit**

~~~bash
git add src/mrs3/performance.py src/mrs3/performance_store.py tests/test_performance.py tests/test_performance_store.py tests/fixtures/duckdb_import/report_a.html tests/fixtures/duckdb_import/report_b.html
git commit -m "feat: add strategy performance parser and schema"
~~~

### Task 3: Transactional Import and Resumable Cleanup

**Files:**
- Create: src/mrs3/performance_import.py, tests/test_performance_import.py
- Modify: src/mrs3/performance_store.py

**Interfaces:**
- Consumes: PerformanceImportRequest(inbox: Path, database: Path).
- Produces: import_performance_batch(request) -> PerformanceImportResult, import_audit.v4.json and html_delete_checklist.v4.csv.
- Uses one DuckDB writer and one database transaction. The sidecar audit remains durable even after a rollback.

- [ ] **Step 1: Write the failing test**

~~~python
def test_identical_payload_skips_without_second_run(tmp_path: Path) -> None:
    first = import_performance_batch(request)
    second = import_performance_batch(request)
    assert first.imported_count == 1
    assert second.skipped_count == 1
    assert row_count(database, "backtest_runs") == 1

def test_conflict_keeps_html_and_database_unchanged(tmp_path: Path) -> None:
    import_performance_batch(request)
    replace_report_metric(inbox, "Total PnL, %", "999")
    with pytest.raises(PerformanceImportError, match="IDENTITY_CONFLICT"):
        import_performance_batch(request)
    assert (inbox / "reports" / ENTRY_HTML).is_file()
    assert row_count(database, "backtest_runs") == 1

def test_cleanup_resumes_after_crash_in_deleting_state(tmp_path: Path) -> None:
    mark_entry_deleting_without_unlink(inbox)
    resume_performance_cleanup(request)
    assert checklist_state(inbox, ENTRY_ID) == "DELETED"
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: .venv\Scripts\python.exe -m pytest tests/test_performance_import.py -q

Expected: FAIL because importer does not exist.

- [ ] **Step 3: Write minimal implementation**

Verify manifest hashes before parsing. Derive strategy_version_id, commission_contract_id, test_run_id and result_payload_sha256 only from canonical values. Same identity and same payload skips; same identity and different payload raises IDENTITY_CONFLICT. Quarantine malformed reports in the v4 sidecar and prevent DD5.

After transaction commit, reopen and verify payload hashes plus action/equity counts. Mark files DELETE_READY only if the complete batch has zero quarantines and safe_to_delete=YES. Cleanup transitions each entry independently and can resume DELETING safely after a crash.

- [ ] **Step 4: Run focused tests to verify it passes**

Run: .venv\Scripts\python.exe -m pytest tests/test_performance_import.py tests/test_performance.py tests/test_performance_store.py -q

Expected: PASS; forced transaction failure writes audit evidence but removes no HTML.

- [ ] **Step 5: Commit**

~~~bash
git add src/mrs3/performance_import.py src/mrs3/performance_store.py tests/test_performance_import.py
git commit -m "feat: import tester performance evidence transactionally"
~~~

### Task 4: DuckDB-backed DD5 and Panel

**Files:**
- Create: src/mrs3/performance_dd5.py, tests/test_performance_dd5.py
- Modify: src/mrs3/cli.py, src/mrs3/panel.py, tests/test_cli.py, tests/test_panel.py, PRD.md, progress.md

**Interfaces:**
- Consumes: committed import_id/strategy performance DuckDB and AlgorithmConfig.
- Produces: run_performance_dd5(database, import_id, output_dir, config) -> PerformanceDd5Artifacts.
- Existing CSV posttest remains for legacy historical output; the panel DD5 path exclusively uses completed inbox and database evidence.

- [ ] **Step 1: Write the failing test**

~~~python
def test_dd5_reads_committed_database_not_runner_csv(tmp_path: Path) -> None:
    artifacts = run_performance_dd5(database, import_id, output, AlgorithmConfig.defaults())
    assert artifacts.manifest_json["dd5_mode"] == "CALCULATION_ONLY"
    assert query_scalar(database, "SELECT count(*) FROM dd5_results") == 1

def test_panel_dd5_refuses_incomplete_inbox(tmp_path: Path) -> None:
    controller = configured_controller_with_inbox(tmp_path, complete=False)
    with pytest.raises(ValueError, match="inbox"):
        controller.command_for("posttest", payload)
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: .venv\Scripts\python.exe -m pytest tests/test_performance_dd5.py tests/test_panel.py tests/test_cli.py -q

Expected: FAIL because the panel still builds the legacy CSV DD5 command.

- [ ] **Step 3: Write minimal implementation**

Load imported rows from DuckDB, reuse normalize_dd5_row and existing comparison/workbook code, then store dd5_runs/dd5_results transactionally. Export posttest.xlsx and posttest_manifest.json from DB records. The panel invokes import, then DD5, then cleanup only after export. Dashboard copy says DD5 is calculated from imported tester HTML and is not a tick-test. Register the feature in PRD and replace obsolete DD5 status in progress.

- [ ] **Step 4: Run focused and relevant tests to verify it passes**

Run: .venv\Scripts\python.exe -m pytest tests/test_performance_dd5.py tests/test_performance_import.py tests/test_posttest.py tests/test_panel.py tests/test_cli.py -q

Expected: PASS; legacy CSV posttest tests remain green.

- [ ] **Step 5: Commit**

~~~bash
git add src/mrs3/performance_dd5.py src/mrs3/cli.py src/mrs3/panel.py tests/test_performance_dd5.py tests/test_cli.py tests/test_panel.py PRD.md progress.md
git commit -m "feat: run DD5 from strategy performance DuckDB"
~~~

## Final Verification

- [ ] Run git diff --check.
- [ ] Run .venv\Scripts\python.exe -m pytest tests/runner/test_inbox.py tests/test_performance.py tests/test_performance_store.py tests/test_performance_import.py tests/test_performance_dd5.py tests/test_posttest.py tests/test_panel.py tests/test_cli.py -q.
- [ ] Run an independent review of the final diff.
- [ ] Start a fresh 476-strategy non-5m ONUSDT tester batch only after its new inbox is complete. Require 476/476, zero quarantine, readback, DuckDB DD5 and audit-backed cleanup before treating the run as evidence.
