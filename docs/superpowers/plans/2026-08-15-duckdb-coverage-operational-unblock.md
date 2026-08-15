# DuckDB Coverage Operational Unblock

**Goal:** unblock the current one-Close-MA-pair `shift_readiness_v1` flow without implementing ADR-0008.

**Architecture:** `Check coverage` remains a preview. `Start` reruns the same current contract against a fresh read-only source and rejects any canonical mismatch before materialization or publication. Panel changes only reset stale state and expose existing diagnostics/artifacts.

## Global Constraints

- Ignore only rows whose report and grid intervals are both zero-duration. Single-degenerate, reversed, and disjoint intervals remain fail-closed.
- Freeze ADR-0008, MA-C, `coverage_summary.csv`, repeated-pass optimization, reusable prepared preflight, and a new graphical progress bar.
- Preserve the existing progress scale unchanged and add only textual side/ordinal/total.
- Add no dependency, route, publication state machine, or speculative abstraction.
- Never expose local paths in API or HTML.
- Update feature status only after verified implementation.
- Preserve unrelated worktree files and execute overlapping changes sequentially.
- Each implementation slice uses TDD, DeepSeek Executor, independent review, focused retest, and re-review after fixes.

## Task 1: Double-Zero Admission

**Files:** `src/mrs3/duckdb_direct.py`, `tests/test_duckdb_direct.py`

1. Add failing tests proving `_reports` filters rows only when both report and grid have `start == end`.
2. Cover single-degenerate report, single-degenerate grid, reversed, and non-empty disjoint cases; all must still fail closed.
3. Add a deterministic 18-row ONUSDT/LONG/15m fixture for shift 430, Open MA 5-7 x Close MA 2-7.
4. Filter confirmed double-zero rows once in `_reports`; keep `_effective_window` fail-closed and preserve all scope/MA/shift validation.
5. Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py -k "double_degenerate or effective_window" -q
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py -q
```

Commit: `fix: filter double-degenerate DuckDB coverage rows`

## Task 2: Preview/Start Contract Identity

**Files:** `src/mrs3/duckdb_direct.py`, `tests/test_duckdb_direct.py`

1. Add failing tests for unchanged-source identity, changed source, scope/interval mismatch, and non-publication on failure.
2. Reuse or add one minimal canonical representation containing `shift_readiness_v1`, its readiness witness/version, existing source identity and inventory hash, every displayed coverage row, selected Pair+Side+TF, current MA pair, exact report/grid/effective intervals, and exact per-side common intervals.
3. Normalize only ordering/container shape using the existing canonical UTC encoding. Do not round, widen, or recompute intervals.
4. At `Start`, rerun the current coverage/preflight in a fresh read-only transaction and exact-compare all canonical bindings, including displayed rows and witnesses, before materializer/publisher calls.
5. Any mismatch returns a controlled non-published failure; spies must prove zero materializer/publisher calls.
6. Do not derive real preflight from manual UTC/Side fields or introduce reusable prepared state.
7. Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py -k "preview or changed_source or interval_mismatch or non_published" -q
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py -q
```

Commit: `fix: enforce canonical DuckDB preflight revalidation`

## Task 3: Coverage UI State and CSV Links

**Files:** `src/mrs3/panel.py`, `tests/test_panel.py`

1. Add failing tests for clearing before synchronous Check validation, failure staying clear, Start isolation, artifact lifecycle, and UTC/Side guidance.
2. Make clearing the first Check operation: remove old token, table, inventory/audit links, artifacts, and Start eligibility. A failed Check restores none of them.
3. Start preserves the current preview consumed by the request but clears prior execution result/error/publication/job artifacts. Failure cannot restore an older job state.
4. Render existing inventory and side-audit CSV artifacts through the existing `/api/artifact` route only after its existing artifact verification accepts them. Test success -> clear -> failure -> later success, rejected unverified artifacts, and working verified route links.
5. Explain that manual UTC and Side fields do not constrain the coverage-token workflow.
6. Use existing panel test techniques; add no browser dependency.
7. Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_panel.py -k "stale or prior_job or artifact_links or manual_fields" -q
.venv\Scripts\python.exe -m pytest tests/test_panel.py -q
```

Commit: `fix: isolate DuckDB coverage and job UI state`

## Task 4: Safe Job Reporting

**Files:** `src/mrs3/panel.py`, `tests/test_panel.py`

Task 2 alone owns pre-publication decisions. This task only transports and renders their result.

1. Add failing tests for controlled error/publication state, callback side/ordinal/total, unexpected exception sanitization, and unchanged existing progress scale.
2. Copy controlled `result.error` and existing publication state into the direct job.
3. Preserve callback side/ordinal/total and render textual coordinates such as `LONG 3/8`.
4. Keep the existing graphical scale unchanged.
5. Convert unexpected exceptions at the job/API boundary to one stable generic client error and non-publication state. Keep exception text server-side; assert Windows and POSIX paths are absent from JSON and HTML.
6. Verify:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_panel.py -k "publication_state or progress_side or unexpected or progress_scale" -q
.venv\Scripts\python.exe -m pytest tests/test_panel.py -q
```

Commit: `fix: expose safe DuckDB direct job status`

## Task 5: Verification and Status

1. Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py tests/test_panel.py tests/test_analysis_storage.py -q
.venv\Scripts\python.exe -m pytest -q
git diff --check
git status --short
```

2. Run the configured real source in read-only Check mode and verify ONUSDT/LONG/15m no longer fails on the 18 confirmed double-zero rows. Do not commit local paths, databases, reports, or artifacts.
3. Obtain cumulative independent review and reverify any fixes.
4. Only then update the canonical spec, `PRD.md`, and `progress.md` with exact evidence while retaining all frozen deferrals.

Commit: `docs: record verified DuckDB coverage patch`

## Task 6: One Current Panel

1. Discover and use the launch command documented by the repository; do not invent or change it.
2. Map port 8765 listeners to executable command lines. Continue only for a listener verified as this repository's panel.
3. Stop only the verified listener process tree, never all Python processes.
4. Launch the documented command exactly once from its documented working directory.
5. Require one listener, HTTP 200, and current HTML markers for Check coverage, `shift_readiness_v1`, date-only intervals, side-aware grouping, UTC/Side guidance, and textual side/ordinal/total.
6. Recheck after a short delay and require exactly one listener. Restart creates no commit.

## Completion

Complete only after focused and full tests pass, independent reviews are clear, real read-only ONUSDT evidence succeeds, verified status documents are current, and exactly one current panel listens on port 8765.
