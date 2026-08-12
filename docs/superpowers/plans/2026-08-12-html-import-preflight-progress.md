# HTML Import Preflight Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` or `superpowers:executing-plans`
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make large HTML import preflight bounded, parallel and visibly
progressing without weakening its snapshot/token authorization contract.

**Architecture:** Snapshot each discovered HTML in a bounded thread pool and
report aggregate files/bytes progress through a background panel preflight job.
The panel polls the existing status endpoint; a Start request remains disabled
until the completed job supplies its token. Source v5 validation uses the
existing structural validator, never full payload decoding.

**Tech Stack:** Python 3.12 stdlib threads, DuckDB, existing loopback panel,
pytest.

## Global Constraints

- No raw paths in job status or progress.
- One active preflight/import operation; no concurrent source writer.
- Snapshot token still covers each input SHA-256 and target identity.
- Keep one DuckDB writer; do not add dependencies.

---

### Task 1: Parallel snapshot and observable preflight

**Files:**

- Modify: `src/mrs3/duckdb_import.py`
- Modify: `src/mrs3/panel.py`
- Test: `tests/test_duckdb_import.py`
- Test: `tests/test_panel.py`

- [x] Write RED tests: workers snapshot independent files concurrently;
  structural source validation is used; panel status reports aggregate
  discovered/snapshotted counts and processed/total bytes while preflight runs;
  Start is rejected until the completed token exists.
- [x] Replace serial `_snapshot_reports` with an ordered result collection
  over a bounded `ThreadPoolExecutor`
  of one-file snapshot workers, accepting an aggregate progress callback.
- [x] Add a panel `_PreflightJob`; run it in one daemon thread and expose only
  state/counts/bytes/token through `snapshot()`.
- [x] Change `Preflight` UI action to start the job, disable duplicate
  Preflight/Start controls while running, and poll/render its aggregate state.
- [x] Replace HTML-import preflight's full v5 validator with
  `validate_source_database_structural`.
- [x] Run focused tests, `git diff --check`, independent review and re-review
  if needed; update spec/PRD/progress and create one scoped `fix:` commit.

### Follow-up backlog: independent preflight worker count

The current production setting `workers=6` is shared by HTML snapshotting,
HTML parsing and detached import preparation. This is adequate for the
current 10-core/50-GB machine, but it couples two different bottlenecks.
Evaluate adding a separate persisted `preflight_workers` setting in the panel
and `DuckDBImportSettings`, with validation and a safe fallback to `workers`
for old `config.local.json` files. The setting must affect only snapshot/hash
workers; import parsing and the single DuckDB writer keep their existing
contracts. Add focused settings, request-wiring and concurrency tests before
implementation. Do not change the current setting until that task is
implemented and reviewed.
