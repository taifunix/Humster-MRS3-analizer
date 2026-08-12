# Handoff: remote HTML import on a 33-core machine

Date: 2026-08-12
Repository: `taifunix/Humster-MRS3-analizer`
Branch: `main`

## Goal

Continue the large HTML import on another LAN machine with 33 CPU cores. The
HTML reports and DuckDB share are accessible from both machines.

Do not start a second writer against the same source DuckDB until the current
import is stopped and its `running=false` state is confirmed.

## State snapshot at handoff creation

- The original machine runs the panel at `http://127.0.0.1:8765/`.
- The latest API poll at handoff creation showed `duckdb_import.running=true`,
  `phase=RUNNING`, `parsed=0`, `inserted=0`, `error=null`. This is a timestamped
  snapshot; poll the API again before deciding whether to move the job.
- Preflight had already completed with `READY`: `27,649` HTML files,
  `20,645,592,560` bytes.
- After Start, import re-snapshots/re-hashes the HTML before parsing. High CPU
  and disk usage at this stage is expected; the current UI does not expose a
  separate progress counter for this repeated import snapshot phase.
- If moving the work, press Cancel on the original panel, wait for
  `running=false`, and verify that the original Python process is no longer
  importing.
- Do not reuse the old preflight token on the new machine. Run a fresh
  Preflight there before Start.

## Published commits

- `5d9a6e6 docs: keep shared import worker setting`
- `0471fd0 docs: plan independent preflight workers` (historical; the later
  decision rejects a separate setting)
- `bd80f5f fix: parallelize html import preflight progress`
- `589b698 fix: use structural direct preflight`
- `675e24a fix: stream trusted v4 source migration`

The original worktree was clean. On the new machine run `git pull origin main`
and verify HEAD is at least `5d9a6e6`.

## Implemented behavior

- HTML Preflight snapshots and hashes through a `ThreadPoolExecutor`.
- One persisted `workers` setting controls both Preflight and import parsing.
  The original machine had `workers=15` at handoff creation. There is no
  separate `preflight_workers` setting.
- Import parsing is also multithreaded. DuckDB publication remains one writer.
- The panel exposes path-free Preflight file and byte progress.
- Duplicate Preflight/Start actions and concurrent Preflight/import operations
  are rejected.
- Existing v5 source validation in Preflight is structural and does not fully
  decode opaque payloads.

## Configuration and shared data

On the new machine verify these Settings/API values:

- source DuckDB: `mrs3_source_v5.duckdb`;
- analysis DuckDB: `mrs3_analysis_v3.duckdb`;
- audit root: `mrs3_import_audit_v5`;
- HTML root: the selected report directory;
- `workers`: start with `12`–`16`, not 33; the shared network disk may be the
  bottleneck;
- `transaction_batch_size`: keep `250` unless there is a measured reason to
  change it.

The current configuration uses an UNC share similar to
`\\Win-0satbdagoa4\share\!MN\hamster\hb\...`. Verify that the same user on
the new machine can read the HTML and write the DuckDB/audit directories.

## Safe procedure on the new machine

1. Run `git pull origin main`.
2. Create local, ignored `config.local.json` from the example file.
3. Run `scripts\start_panel.bat` **on the new machine**. It creates `.venv`,
   installs dependencies if needed, and starts the panel. Opening
   `127.0.0.1:8765` on the old machine cannot use the new machine's CPU.
4. In Settings, enter the UNC paths and choose `workers=12`–`16`.
5. Select the HTML root, run Preflight, and wait for `READY`.
6. Press Start import only after Preflight is complete.
7. Wait for `COMMITTED`; verify counts, `safe_to_delete=YES`, and the audit
   manifest/checklist. Never delete HTML manually without the checklist.

Never run two writer processes against the same source DuckDB. If the share is
unstable, stop and discuss a local staging design; do not manually copy,
replace, or delete the production database.

## Follow-up UX improvement

Import re-snapshots/re-hashes inputs to revalidate the preflight token, but the
current import callback does not expose progress during that repeated snapshot
phase. A future small UX task can pass `SnapshotProgress` through import too;
this does not change the safety contract and is not required for the remote
run.
