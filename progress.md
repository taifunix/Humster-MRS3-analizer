# MRS3 — current verification

**Updated:** 2026-09-05
**Current branch:** `main`

## Portfolio Optimizer design clarification (2026-09-05)

Documentation-only discussion update in
`docs/superpowers/plans/Portfolio-Optimizer/08_OPTIMIZER_SETTINGS_AND_REPRODUCIBILITY_RU.md`:
separate optimizer settings file, explicit risk-type terminology, proposed
initial thresholds/ranking and a minimal campaign reproducibility contract.
Thresholds and ranking remain unapproved, uncalibrated proposals; snapshot
implementation and validation protocol remain open. General capital allocation
recommendations are explicitly post-MVP in the Phase 7 roadmap. No tester/bot
was launched, no runtime config was created, and portfolio runtime remains
inactive under the current PRD. Next: agree the proposals and remaining scope.

## Bybit collector current implementation status (2026-09-05)

Phases 1-6 and the minimal operations/runtime surface are implemented in this
isolated branch: strict config, RAM-only order books, scheduler/aggregation,
SQLite WAL spool, hourly immutable Parquet, paginated reference data/raw gzip,
  one-connection WebSocket protocol, runtime wiring, health/CLI, and Windows task
  scripts. The focused collector suite currently contains 248 passing tests after
  self-review. Remaining work is live integration/soak and Windows boot evidence;
  no live credentials or generated
market data are committed.

Final implementation review disposition (2026-09-05): the restart-only storage
root path now persists a visible health error, prints a diagnostic, and exits 3
so the Windows task restarts it. Independent review accepted the implementation;
  self-review corrected the linear subscription topic to the supported
  `orderbook.1000` depth and updated the focused protocol tests. Low-severity
  follow-ups are deliberately deferred: half-open WS idle watchdog,
snapshot ordering during multi-batch handshake, persisted reference baseline,
reference page-count cap, and a lock around cross-thread book snapshots.

Live smoke evidence (2026-09-05, isolated `.tmp` data root): public REST returned
HTTP 200 for BTCUSDT/ETHUSDT; normalized reference output contained 4 raw gzip
pages and instruments/risk Parquet; a 95-second run reached `connected=true` and
wrote minute rows; after forced stop, a 75-second restart reopened SQLite WAL,
reached `connected=true`, continued rows, published an eligible hourly Parquet,
and `verify-archive` returned `valid=true`. Generated smoke data is not tracked.

## Bybit market-data collector Phase 1 started (2026-09-05)

Implementation follows the approved [Bybit market-data collector Revision 2
specification](docs/specs/2026-09-05-bybit-market-data-collector.md) and its
[executable plan](docs/superpowers/plans/2026-09-05-bybit-market-data-collector.md).
The new strict TOML configuration loader validates the exact three-section
contract, resolves and checks the storage root relative to the config file, and
hashes exact UTF-8 bytes. `ConfigManager.reload()` is all-or-nothing: invalid
candidates preserve the accepted config; valid symbol/log changes report atomic
added/removed/unchanged sets; and a changed root remains on the accepted active
root while returning `restart_required`.

Evidence: `.venv\\Scripts\\python.exe -m pytest
tests/test_bybit_collector_config.py -q` — `36 passed` after the expected
pre-implementation import failure. This is Phase 1 configuration evidence only;
network validation and collector phases 4–9 remain pending.

## Bybit market-data collector Phase 3 implemented (2026-09-05)

The RAM-only minute aggregation slice now provides deterministic UTC five-second
boundary scheduling with monotonic wait calculation, forward/backward clock and
suspend reanchoring without backfill, and fixed-order `liquidity_1m` rows. It
tracks active targets, attempted/valid/connected samples, reset attribution,
nullable no-valid rows, interpolated p05/p50/p95 metrics, visible depth, and
per-band completeness according to the approved specification.

Evidence: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_bybit_collector_aggregation.py tests/test_bybit_collector_core.py
tests/test_bybit_collector_config.py` — `115 passed`; collector modules also
pass `py_compile` and `git diff --check`. SQLite spool is now implemented as
Phase 4; archive, reference data, operations, and integration phases remain
pending.

## Bybit market-data collector Phase 4 implemented (2026-09-05)

The SQLite spool persists only canonical `liquidity_1m` minute aggregates and
the `published_hours` marker index under `storage.root/spool`. WAL/NORMAL
settings, finite canonical JSON, first-winner duplicate/conflict policy,
bounded BUSY/LOCKED retries, marker idempotency/conflict rejection, half-open
hour reads, restart recovery, marker-only reader files, and the existing
`OutputDirectoryLock` are covered by focused tests. WebSocket frames, books,
and five-second samples remain RAM-only; Parquet export is deferred to Phase 5.

Evidence: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_bybit_collector_storage.py tests/test_bybit_collector_aggregation.py
tests/test_bybit_collector_core.py tests/test_bybit_collector_config.py` — `188
passed`; collector modules pass `py_compile` and `git diff --check`. The phase
delivers only the SQLite spool and `published_hours` marker index: no Parquet,
manifests, quarantine, or archive state machine is delivered here.

## Bybit market-data collector Phase 5 implemented (2026-09-05)

Hourly export snapshots committed SQLite rows for an eligible UTC hour, writes
DuckDB `COPY` Parquet with ZSTD metadata, fsyncs and structurally validates the
temporary file, publishes with a same-directory no-clobber link, and commits
`published_hours` last. Existing marked and unmarked finals are validated
self-consistently, so late SQLite rows never rewrite or invalidate immutable
archive files; only a fresh temporary file is compared with its SQLite
snapshot. Verification reads only marker-listed files. Recovery removes only
owned stale UUID scratch files, reports unlink/export errors, skips valid
marked history, and bounds unmarked-hour reconciliation to the recent
48-hour window; older unmarked files remain an operator-retention concern.

Evidence: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_bybit_collector_archive.py tests/test_bybit_collector_storage.py
tests/test_bybit_collector_aggregation.py tests/test_bybit_collector_core.py
tests/test_bybit_collector_config.py` — `209 passed`;
collector modules pass `py_compile` and
`git diff --check` remain required before integration. Phases 6–9 remain
pending.

## Performance v2 import hardening and unified panel (2026-09-05)

The normal tester and Performance DB import are one card. Import requires an
explicit inbox check for the current `SINGLE_MODE` job. The server consumes that
authorization atomically before dispatch, revalidates the metadata inbox, and
uses only the configured listing-dates path. A terminal import requires a new
check. All-rejected imports are `FAILED`, retain sources and expose the failure
report; successful imports no longer display a post-import check warning.

Evidence: importer/selection/windows/input `232 passed, 1 skipped`; panel
`187 passed, 4 skipped`; `node --check`; `git diff --check`; independent Opus
reviews returned `CODE_REVIEW_PASS` for both importer and panel scopes.

## Performance v2 SHORT import and terminal status (2026-09-05)

The normal `SINGLE_MODE` import resolves the project-configured listing dates
when the browser does not send a path. A failed all-rejected import is `FAILED`,
retains its failure report and tester sources, and never reports a false commit.
The terminal `COMMITTED` message no longer appends `CHECK REQUIRED`: that gate
exists only before import.

Production evidence: the normal SHORT batch committed 321 of 321 reports with
zero rejected entries. Database readback found 321 current strategy/results
across eight symbols; all have listing-aware effective periods and warm-up
provenance. Focused importer regressions: `20 passed`; focused panel failure
and RETEST recovery regressions: `5 passed`; static panel checks: `68 passed`.
The terminal-status change received external `CODE_REVIEW_PASS`.

## RETEST report-header retry fix (2026-09-04)

Native RETEST prevalidation now accepts the current tester action table by
required column names, including the tester's extra/reordered `Side`, `Price`,
and `Cost` columns. The main Performance v2 parser already followed this
contract; regression coverage now exercises both paths and preserves missing,
duplicate, typed, and legacy rejection.

Evidence: `117 passed, 1 skipped` in the focused RETEST/Performance v2 slice;
read-only replay accepted and parsed `186/186` captured reports for `149`
expected strategies. Commit: `54ee9b8`.

## Performance v2 shared tester-source cleanup (2026-09-04)

SINGLE_MODE inbox metadata now stores only the configured report filename;
imports resolve it under `tester_runner.report_dir` without copying HTML. The
import remains fail-closed for missing, changed, unsafe, or reparse-backed
reports. After a committed import, the exact configured report directory, the
configured tester strategy directory, and project `Output/strategies` are
emptied; failed imports leave these sources intact.
After a panel restart, the previous RETEST job remains a candidate only;
`CHECK & RETEST` must be pressed again. The check reuses a committed RETEST
inbox with a safe, structurally valid metadata manifest and current configured
report/strategy artifacts, without recapturing mutable tester state. A broken
committed inbox reports a deterministic error; it never silently starts another
native run. If the manifest is valid but its report or strategy files were
removed, the inbox is not reusable and CHECK starts a new native run.
RETEST replacement now updates the existing result row in place and replaces
only that result's action, equity, and window child rows in one transaction;
the obsolete full-table child rebuild is gone.

## Performance v2 selection review cleanup (2026-09-03)

The accumulated Performance v2 selection/review work is implemented, staged, and
ready for the scoped commit. It includes schema v3 selection snapshots, editable
XLSX review/import, REJECTED-only durable tags, period-integrity checks, typed
database/API failures, cache invalidation for result/config/window-fact changes,
and resilient multi-file review import. Trades remain completed round trips from
`strategy_actions`, not partial order openings or `strategy_results.total_trades`.

Verification: the full suite reports `2118 passed, 2 skipped, 1 warning` in
`730.19s`; the focused Performance v2/panel slice reports `248 passed in 29.96s`;
`node --check src/mrs3/panel_web/app.js` and `git diff --cached --check` pass.
The independent review found no High or blocking finding; its Medium/Low
follow-ups were addressed and verified. Next step after commit/push is manual
Excel round-trip acceptance on the real Performance v2 database.

## Current unified Performance v2 handoff (2026-08-30)

The native `SINGLE_MODE` tester handoff is the active path and is implemented
through commits `952bc22..3f535f4`. It creates a metadata-only inbox: strategy
JSON remains in the trusted `Output/strategies` root and current HTML remains
in `tester/report/my_test`; the manifest records exact paths, source hashes,
dates, commission and provenance. The v2 importer is the only authoritative
full source/identity/report/plateau validation before staging and DB commit.

After v2 `COMMITTED`, cleanup is limited to the approved exact tester/report
and `Output/strategies` roots. Inbox metadata and the v2 audit remain
provenance; cleanup failure leaves the DB committed and reports a path-safe
warning. The old Fast TEST panel dispatch/API/retry contour is removed, while
the Runs backend/API remains available with its UI hidden.

Fresh evidence: 366 passed across v2/native/panel tests, 338 passed in the v1
non-disturbance suite, `node --check src/mrs3/panel_web/app.js`, and
`git diff --check`; final Terra disposition is `CODE_REVIEW_PASS`.

Task 8 now executes native `SINGLE_MODE` batches through one `/htmx/tester/run`
POST and `/htmx/tester/status` polling cycle per attempt.  It installs each
bounded batch before startup, maps the newest complete current HTML by embedded
strategy name, retries only missing reports, and fails terminally without a
native PARTIAL commit; successful completion still creates the metadata-only
inbox.  Fresh evidence: runner/native and retained monitor suites — `58 passed`;
v2/panel regression slice — `104 passed`; `py_compile`, `node --check
src/mrs3/panel_web/app.js`, and `git diff --check` passed.

The 2026-08-28 main-branch commit audit is complete. Relevant work is now
split into `de0ee4c` (Fast TEST contour), `b58f22d` (inode-preserving strategy
publication), `edc5d40` (16-worker Performance import defaults), `4a2bab8`
(Fast TEST implementation plan), and `23575e8` (unified Performance v2
specification, ADR and vertical-slice plan). The v2 vertical slice (Tasks 1–6)
is implemented and independently reviewed; the full v2 pipeline remains
pending; aggregate verification is 81 focused v2 tests and 334 v1
non-disturbance tests plus
`node --check src/mrs3/panel_web/app.js`; no runtime or generated artifacts
were committed.

Unified Performance Analytics v2 design is approved and recorded in
`docs/specs/2026-08-28-unified-performance-analytics-v2.md` and ADR-0020. The
approved boundary is one Performance DuckDB, one current replaceable result per
strategy, shared order-to-plateau facts, arbitrary flat-boundary UPNL-relative
A/B windows, an ordered filter/Pareto pipeline, panel/XLSX/Portfolio Optimizer
outputs, transactional discard/add/replace and a durable `RETEST` tag whose
handler runs the common RUNS-to-inbox-to-replacement chain. The reviewed
vertical-slice code and schema are now committed. Next step is the deferred full
v2 pipeline; the later RUNS redesign must reuse the same metadata-only,
trusted-path and importer contract.

Performance DB import now defaults to 16 preparation processes and caps the
request at 16; DuckDB publication remains a single transactional writer.
Focused verification: `.venv\\Scripts\\python.exe -m pytest
  tests/test_performance_import.py -q` —
`48 passed`.

## Unified Performance Analytics v2 vertical slice (2026-08-28)

Tasks 1–6 of the approved v2 vertical-slice plan are implemented and
independently reviewed. Accepted commits are `3686af7` (Task 1), `ead1ded`
(Task 2), `5475dae` (Task 3), `f69737a` (Task 4), `a154015` (Task 5), and
`91381ce` (Task 6); each received Terra Medium `CODE_REVIEW_PASS`.

Focused v2 verification:
`.venv\\Scripts\\python.exe -m pytest -q
tests/test_performance_v2_store.py tests/test_performance_v2_input.py
tests/test_performance_v2_html.py tests/test_performance_v2_import.py
tests/test_performance_v2_windows.py tests/test_panel_performance_v2.py` —
`81 passed in 17.05s`.

V1 non-disturbance verification:
`.venv\\Scripts\\python.exe -m pytest -q tests/test_performance.py
tests/test_performance_store.py tests/test_performance_import.py
  tests/test_performance_metrics.py tests/runner/test_inbox.py tests/test_panel.py
tests/test_panel_static_ui.py tests/test_integration_contract.py` —
`334 passed in 139.69s`.

`node --check src/mrs3/panel_web/app.js` and `git diff --check` passed. The
vertical slice is implemented and verified; the full v2 pipeline is pending.
Source metrics are not MRS3 strategy, tick-test, DD5, or portfolio results.
Next increment: implement the explicitly deferred v2 scope under its approved
contracts, without deleting v1 runtime/storage or widening result claims.

Fast TEST now writes the HTML profile into the tester config's nested
`report` object as well as legacy top-level keys. Balance/equity series remain
enabled and position statistics are disabled for the minimal import profile.
Focused verification: `tests/test_panel_fast_strategy_test.py` — `11 passed`.

READY generation no longer blocks when the runtime algorithm-config hash
differs from the historical analysis hash. The analysis hash is retained as
lineage metadata; generated JSON uses the supplied runtime config and keeps
the existing strategy validation. Focused verification:
`.venv\\Scripts\\python.exe -m pytest tests/test_analysis_strategies.py
tests/test_fresh_analysis_strategies.py tests/test_panel_fresh_strategies.py -q`
— `75 passed`.

## Retired Fast Strategy Test design (2026-08-27)

The independent **Fast TEST стратегии** contour is approved and documented in
`docs/specs/2026-08-27-panel-fast-strategy-test.md`. Its implementation plan is
`docs/superpowers/plans/2026-08-27-panel-fast-strategy-test.md`.

This section is historical, not an active panel feature. Native `SINGLE_MODE`
is now active; Fast start/retry dispatch, API handlers and panel service
ownership were removed in `3f535f4`. The bounded implementation remains only
as shared runner machinery required by the Single mode service.

The new path will retain bounded `strategy_batch_size` chunks, the existing
low-level `max_parallel_submissions` rolling window and four total automatic
attempts. It will not call the old `runner.workflow.run_batch`, create verified
inboxes or write `data/import_audit`. A partial run continues later chunks and
leaves exactly failed strategy JSON in `<bot_root>\settings_strategy`; one
recovery action first accepts matching manual reports, then grants one extra
attempt to each remaining failure.

Historical implementation details follow. Task 1 persisted per-order plateau diagnostics
in the generation manifest; Task 2 supports partial controlled monitoring and
HTML settings extraction; Task 3 provides the independent bounded Fast TEST
service; Task 4 wires `strategies.tester.fast.start/retry` through the panel;
Task 5 adds the two Fast TEST controls and reload recovery. Focused verification
currently passes 154 tests; the dedicated runner suite passes 118 tests plus
one platform skip. The independent review returned `CODE_REVIEW_PASS` after
the period, recovery and UI fixes. A real disposable-tester smoke is still
pending.

The READY publisher now preserves the existing `Output\\strategies` directory
instead of replacing it, so its ACL survives regeneration; staged files are
installed with rollback on failure. Focused verification after this fix:
`.venv\\Scripts\\python.exe -m pytest tests/test_pipeline.py
tests/test_analysis_strategies.py tests/test_fresh_analysis_strategies.py -q`
— `69 passed`.

Fast TEST now removes a source HTML only after a stable snapshot is captured
and its size/mtime signature is rechecked. The legacy `run_batch` path keeps
its previous report-preservation behavior. Focused runner verification:
`.venv\\Scripts\\python.exe -m pytest tests/runner/test_monitor.py tests/test_panel_fast_strategy_test.py tests/runner/test_workflow.py tests/runner/test_results.py -q`
— `76 passed`.

The quadratic Fast TEST post-run deduplication pass was removed. Stable
`verified_reports` snapshot filenames are now authoritative, so completed batches
do not reread every HTML for every strategy. The interrupted 3917-report run
retains all 3917 manifest-referenced files; its stale runtime state is not
treated as a committed job. Fast-to-Performance-DB integration remains a
separate follow-up contract.

Reload recovery now prefers a Fast job with persisted `verified_reports` over an
older committed tester inbox. The Tester card and `Проверить` action therefore
use the reports currently present in `tester/report/my_test` instead of reviving
the historical 196-report status. Focused static/Fast verification: `72 passed`.

The Performance DB `Проверить` button now reports immediate verification state
and surfaces missing-job/API errors instead of returning silently. The current
Fast inbox can therefore be observed while its verified snapshot is captured.

## READY JSON validation recovery (2026-08-26)

READY generation now validates Phase 2 filters before registering its running
job, so a malformed request cannot leave the next generation permanently busy.
The fresh-generation endpoint returns a path-safe validation reason and the
panel displays it instead of collapsing it to `Server validation failed.`.
Thread-start failures follow the same path and clear the pending job before
returning the safe reason.

Evidence: fresh-strategy and static-panel tests `66 passed`; `node --check`
and `git diff --check` clean apart from existing Windows line-ending warnings.

The live failure also had a local filesystem cause: `Output\\strategies` had
inheritance disabled and denied the configured panel account. Re-enabling
inheritance restored the existing parent ACL; a live NVDL shortlist generation
then committed `4/4` strategies successfully.

## Performance DB display rounding (2026-08-26)

Performance import admission now tolerates tester display drift using inclusive
nearest-unit intervals: absolute `Total PnL` and `Max Drawdown` use one unit,
while their percentage fields use 0.1 percentage point. Precise series-derived
values stored in the database are unchanged. The two previously quarantined
PANW reports pass the updated validation in isolation. Focused verification:
  `.venv\\Scripts\\python.exe -m pytest tests/test_performance_metrics.py
  tests/test_performance_import.py -q` -- `59
passed`. A full inbox re-import remains pending because its referenced
`Output\\strategies` JSON files are currently absent.

## Tester run files (2026-08-26)

## Verified snapshot republish (2026-08-26)

An ordinary tester batch may republish a completed report from only its own
`.<batch_id>.report_snapshots` directory. This preserves the strict inbox path
boundary while allowing the worker to finish `COMMITTED` and unlock Performance
DB import after verified snapshot capture. Focused verification:
`.venv\\Scripts\\python.exe -m pytest tests/test_panel_strategy_batch.py
tests/test_panel_fresh_strategies.py tests/test_panel_jobs.py
tests/test_panel_static_ui.py -q` — `81 passed`.
On panel reload, an existing committed tester job now revalidates its persisted
inbox and restores `inbox_ready`, so the Performance DB action is not lost.

## READY JSON failure diagnostics (2026-08-26)

The panel preserves an actionable, path-safe cause when asynchronous READY
JSON generation fails. Permission failures identify publication access, missing
files identify the template/artifact boundary, and validation failures retain
their contract message. Focused verification: `14 passed` in
`tests/test_panel_fresh_strategies.py`.

## Tester snapshot completion recovery (2026-08-26)

If the tester returns an empty `chartUrl`, final reconciliation uses a report
name only when the controlled batch has already captured a fresh,
embedded-name-verified snapshot for that exact strategy. A reused tester
`runId` no longer suppresses such fresh evidence. Otherwise the existing
strict result validation remains in force. Focused verification:
`.venv\\Scripts\\python.exe -m pytest tests/runner/test_monitor.py
tests/runner/test_results.py tests/runner/test_workflow.py -q` — `63 passed`.

The Shortlist button creates isolated tester snapshots for every selected
server-recomputed `READY_AFTER_FILTERS` candidate. It clears only the exact
`<bot_root>/tester/runs` target, sets `use_runs=true`, and copies Tester batch
dates plus `max_parallel_submissions`; `run_tester.bat` remains manual.
The bot-exported run template may have a UTF-8 BOM and trailing commas; those
are accepted only while reading that template.
Generated snapshots serialize the template's default MakerFee as decimal
`0.00001`, rather than scientific notation.

Focused verification: `.venv\\Scripts\\python.exe -m pytest
tests/test_tester_run_files.py tests/test_panel_fresh_strategies.py -q`.

The Tester batch card now owns both generation actions and their status.
Ordinary READY batches and isolated RUNS share one panel job resource. RUNS
requires generated snapshots, clears only `tester/report/my_test_runs`, starts
`run_tester.bat` non-interactively, and reports generated HTML count every 15
seconds. Snapshots use `name_comment=runs`, keeping their reports out of the
ordinary READY report directory. After every expected RUNS report is present,
the panel captures a verified inbox from immutable snapshot settings and report
hashes; Performance DB accepts that inbox exactly as it accepts an ordinary
batch. The common `delete_html` cleanup removes the original mode-specific
reports only after a zero-quarantine committed import. Focused verification:
`87 passed` across run files, inbox, panel and Performance DB tests; independent
review is still required before integration.

Reload no longer restores a terminal tester job into the live Tester batch
status or progress bar; only an active job is reattached.

## Multi-order plateau admission (2026-08-25)

For `real_independent_events`, 2ORD--4ORD construction now admits a plateau
only when its frozen point-count and monthly event-count meet configurable
`multi_order_admission` limits (defaults: 3 and 20). The check is before
combinations, preserves legacy-proxy behaviour, and fails closed for missing
real-event diagnostics on `ready=true` plateaus. It changes the algorithm
configuration hash and therefore requires a new analysis; source/surface
materialization and Phase 2 remain unchanged.

Focused verification:
`.venv\\Scripts\\python.exe -m pytest tests/test_config.py tests/test_selection.py tests/test_source_v6_analysis_fresh.py -q` -- `239 passed`.

## Filtered Shortlist bucket counts (2026-08-25)

Phase 2 previously updated only READY/DEFERRED while the `1ORD`–`4ORD` table
counts remained unfiltered. With active filters, those bucket counts now show
only remaining READY candidates; `DEFERRED` and `ALL` stay as the full
context. Accordion status badges are right-aligned. Focused fresh-analysis and
panel tests, JS syntax and diff checks pass.

## Shortlist control alignment (2026-08-25)

The secondary Shortlist information caption is hidden. The Phase 2 heading now
matches Shortlist typography; Pair/Side and TF use the same 16px checkbox and
a vertically centred disclosure arrow. Filter logic and table columns remain
unchanged. Focused panel tests, JS syntax and diff checks pass.

### Phase 2 live refresh (2026-08-25)

The Phase 2 checkboxes now call the same filtered Shortlist refresh used by the
Refresh action. Previously their state reached the server only after a manual
Refresh click, so visible counts did not change immediately. The Pair/Side
triangle is reduced to `0.9rem`; no data contract changes. Focused panel tests,
JS syntax and diff checks pass.

## Shortlist filter presentation (2026-08-25)

Phase 2 filters are permanently visible with a bold heading and spaced labels;
their native summary remains visible as the title but cannot collapse. TF
selection checkboxes are indented beneath the Pair/Side row. This is
presentation-only: filters, counts and all current columns are unchanged.
Focused panel tests, JS syntax and diff checks pass.

## M7 final-balance PnL validation (2026-08-25)

The `my_test_APLD_TSEM_fixed_0.997net` import quarantined 1,009 reports:
1,007 `Total PnL` mismatches and 2 Recovery Factor mismatches. The tester's
`Final balance - Initial balance` matches its declared `Total PnL`, but its
last `walletSeries` point can differ in either direction. M7 now validates PnL
against declared final balance when present, falling back to the final wallet
sample only for sparse reports without that declaration; M4 materialized
wallet-series metrics are unchanged. ADR-0018 records the boundary.

Focused verification: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_source_v6_m7.py tests/test_source_v6.py` — `88 passed`.

## M7 Recovery Factor admission boundary (2026-08-26)

`Recovery Factor` is informational and no longer quarantines a Source v6
report: the tester's internal denominator precision is not contained in the
HTML payload. M7 continues to reject mismatched `Total PnL`, `Total fees` and
`Profit Factor`.

Targeted recovery imported only the affected reports and produced clean
two-symbol merge kits under `data/databases/repair-kits-2026-08-26/`:
NVDL/TSLL, LLY/TQQQ, BABA/SNOW and APLD/TSEM each contain 10,944 fragments,
zero quarantines and no other symbols.

Focused verification: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_source_v6_m7.py tests/test_source_v6_bundle.py` - `19 passed`;
each resulting DB passed `validate_source_v6_database`.

## Panel Web reliable batch recovery (2026-08-25)

The panel now keeps READY JSON generation failed when publication raises an
error, rejects restart while that generation is active, and restores a tester
batch after restart only after complete inbox validation, including the direct
strategy JSON file, raw hash and canonical version ID. Performance import
applies the same strategy provenance validation before both parsing and
known-evidence skips. The operator patch merge no longer bypasses unresolved
quarantine replacement checks.

Focused verification: panel suite `112 passed`; provenance/merge checks `10
passed`. Independent re-review: `CODE_REVIEW_PASS`.

## READY JSON template-only payload (2026-08-26)

`Generate READY JSON` now emits only the selected tester-template fields and
computed strategy settings. Per-strategy provenance is no longer copied into
the bot payload; the immutable strategy manifest retains the analysis lineage,
candidate mapping and exact JSON hashes used by batch validation. The generator
does not read or modify `config_tester.json`.

Focused verification: fresh-generation, manifest validation and v6 generation
tests — `63 passed`.

## BASE 1ORD selection (2026-08-24)

Implemented algorithm `0.7-canonical-phase1-base-1ord-v3`. Fresh CXMT corpus
evidence (`workers=30/30/8`) produced 26 BASE candidates: 15m `[8,12,12,4]`,
5m `[15,23,12,7]`, 1h `[3,10,13,8]`, 30m `[5,31,7,3]`, 45m `[5,16,20,14]`,
4h `[6,4,3]`, 2h `[27,3]`, 3h `[4]`; only 5m size 7 was appended as
`FALLBACK_1`. Full verification: `1736 passed, 2 skipped, 2 warnings`.
This is selection evidence, not tick-test or realized MRS3 PnL evidence.

## Panel Web trust/design alignment (2026-08-24)

Merged into `main` at `2a40518` after independent `CODE_REVIEW_PASS`.
Panel verification: **341 passed**; full project verification: **1668 passed,
2 skipped, 2 warnings**. The untracked BASE/1ORD specification and panel plans
remain in the working tree; a pre-merge archive is stored at
`backups/main-dirty-before-panel-merge-2026-08-24-102131.zip`.

The remote Source DB card now accepts a relative report-folder selector under
the configured archive root (`c6b7d8b`); absolute and traversal selectors are
rejected server-side. Independent review: `CODE_REVIEW_PASS`. Panel
verification after this fix: **345 passed**.

## Source v6 facts/metrics v2 Stage 3–4 (2026-08-23)

M7 validates declarations during normalization before encoding. PnL uses the
declared Final balance minus declared initial balance when Final balance is
available, falling back to the final wallet sample only for sparse reports;
fees sum all actions, Profit Factor sums realising actions, and Recovery Factor
is conditional on a sampled DD-compatible declaration. Quarantine details are
read-only and a quarantined database blocks every scope at panel preflight and
materialization.

Fresh baseline evidence: 684/684 reports committed, 0 quarantined, source
content digest `9612e26abc51b6e36f49ce3a73159358abdcd1a412747a235c70ffaa82fec940`.
The three-run import median is **68.712 s**, database size **24,653,824 bytes**,
and all three semantic signatures match the clean v2 database. Against the
recorded v1 medians (111.533 s, 30,683,136 bytes), this is faster and smaller.

Clean materialization with workers 1 and 4 produced the same analysis digest
`358ec8ba55a2888fe9b12ba38c82c915ecd5767b447a0a53a267e5bd1a55496c`, 684
compact rows, and zero empty results. Full workers=30 materialize + SQL-copy
publication ran three times at a **10.640 s** median, 684 rows/facts and
**23,867,392 bytes** (v1 median 71.160 s and 27,537,408 bytes). The full suite
is green: `.venv\\Scripts\\python.exe -m pytest -q` — **1629 passed, 2 skipped**.

The final Reviewer call still returned findings; the panel validator seam and
orphan-roundtrip traceability were fixed and focused/full verification rerun.
No fourth Reviewer call is permitted, so this remains a non-approval artifact
until a future task with a fresh review budget explicitly reopens review.

## Post-review metric corrections (2026-08-23)

An independent implementation review found that a leading realization could
remain in `total_pnl` while disappearing from round-trip counts and win rate.
The metric contract now emits one orphan round trip with empty
`entry_action_ids`, preserving the M1 allowance for a position opened before a
visible window; entry-only tails remain excluded. The review also found a
quadratic peak scan. Cycle peaks are now computed once from the ordered action
stream and reused by each round trip.

Focused Source v6 verification after the correction: **89 passed, 1 warning**.
A synthetic derive benchmark scaled approximately linearly: 185/370/740/1480
actions took 8.8/9.3/19.5/37.6 ms on this host.
Fresh full verification: `.venv\\Scripts\\python.exe -m pytest -q` — **1629
passed, 2 skipped, 2 warnings**.
**Current feature:** Source v6 high-throughput import and merge — implemented,
measured on both real corpora; the merge readback is now
parallel (C9): 2,080 s to 544 s on the two-corpus merge, identical artifact.

## Source v6 import throughput (2026-08-21)

Contract: [publication throughput spec](docs/specs/2026-08-21-source-v6-publication-throughput.md)
and [high-throughput import plan](docs/superpowers/plans/2026-08-21-source-v6-high-throughput-import.md).

Measured on Debian `46.4.84.220`, `/opt/hb1/debian-duckdb-importer`, corpus
`data/html/1` (5,859 reports), 32 cores, `workers=30`:
**886 s to 299 s (2.96x)**, published digest unchanged
(`c85fdb8372b2cce51d2a0e4aff537eb951e781eff5b4704a74d63c7163611b90`).

Defects found and fixed, each measured rather than assumed:

1. Leaf scheduling capped in-flight chunks at `segment_writer_limit`, so 30
   workers ran 4-wide (12.5% of 32 cores). The writer limit now bounds only
   segment writes, via a pool semaphore.
2. Merges decoded and re-encoded sealed payloads at every tree level; segment
   reads decoded every row to compare two stored columns.
3. Reduce built a fan-in tree, writing 6.8 GB of intermediates for 1.5 GB of
   leaves. It now `ATTACH`es segments and copies rows in one SQL pass.
4. Publication issued a per-fragment duplicate probe (quadratic), one statement
   per calendar day, and a per-fragment decode readback. All are now set-based.
5. `decode_fragment` rebuilt the canonical document to re-derive the identity —
   51.8% of all decode time. The decompressed bytes are that document, so the
   id is `sha256(raw)`.
6. The tail decoded all 5,859 fragments to serve consumers that read metadata
   only. Metadata now comes from indexed columns; payloads are decoded only for
   surface publication or a point with a real overlap to persist.

7. Metadata publication bound one row per call. DuckDB `executemany` was
   measured at 1,730 rows/s against 1,969,809 rows/s for the same rows inserted
   through a registered frame — **1138.9x**. One corpus emits ~1.2M
   `day_ownership` rows, so this was the unexplained "last stage" cost on both
   the import and the merge path. Both now go through `_insert_frame`.
8. The merge ran its identity readback inside the copy transaction. Committed,
   one 128-id window costs **0.239 s** against the committed 59,675-fragment
   input database; with the rows still open in the copy transaction the merge
   of both corpora did not finish in 40 minutes (`py-spy` parked it in
   `_verify_published_identity`). These are two artifacts, not an A/B on one.
   The mechanism is not settled — `EXPLAIN` gives the same `SEQ_SCAN` plan
   either way, so it is not index availability; transaction-local storage of
   the merge's 4.7 GB of payload is the likeliest cause and is recorded as
   unproven. The readback
   now runs after the commit and before publication — on the `.staging` file,
   which `merge_source_v6` publishes only by `compacted.replace(target)`, so
   nothing committed ever becomes reachable unverified.

9. The merge's identity readback ran serially and was the single largest phase
   of it, not a tail: **1,215 s of the 2,080 s merge**, measured on the
   published 5.6 GB artifact at 2.460 s per 128-id window over 494 windows.
   Within a window the SQL fetch is only **3.9%** (1.91 s against 47.56 s of
   Python over 20 windows); the Python side is 64.3%
   `_assert_canonical_matches_columns`, 20.3% `zlib` and 15.4% `sha256`. It is
   therefore CPU-bound, per-fragment and shares no state. `merge_source_v6` now
   takes `workers` and calls `verify_published_identity_parallel`. A/B on the
   same 8,192 ids: **134.2 s serial against 19.0 s at 16 workers, 7.07x**;
   it plateaus there (8 → 5.74x, 24 → 6.82x). The full 63,131-fragment
   verification runs in **113.9 s**, of which 59.3 s is now the single-statement
   column check — that is the next lever, and it belongs to DuckDB's own
   parallelism rather than to a fan-out.

   Re-run end to end with `workers=16`, the two-corpus merge took **543.7 s
   against 2,080.1 s** — about 9 minutes where it was about 35. Roughly 1,100 s
   of that difference is the verification saving; the rest is uncontrolled,
   because the serial run was the first read of those inputs and this one was
   not, so the page cache differs. The subset A/B is the isolated measurement,
   and it does not reconcile cleanly with the full-corpus figure — see C9,
   where the discrepancy is recorded open rather than explained away.
   Equivalence was checked rather than assumed: every published table digested
   inside DuckDB and compared, all identical, with `schema_info` differing only
   in the per-merge `database_id`.

Verified equivalent: full-database dump comparison across all published tables
including `mutation_generation` and `import_audit`; surface publication fails
closed on metadata-only fragments.

Verification for all of the above, including C9:
`.venv\Scripts\python.exe -m pytest -q` — **1341 passed, 2 skipped** through
defect 8, **1346 passed, 2 skipped** with defect 9, **1347 passed, 2 skipped**
with C10 (ADR-0015), and **1363 passed, 2 skipped** with ADR-0016.

## Merge of the two real corpora (2026-08-22)

`merge_source_v6` over `data/databases/1_3/` — 5,859 + 59,675 = **65,534 input
fragments, 6.1 GB** — completed in **2,080 s** and published 5,643,710,464
bytes with `source_content_digest`
`a26c00b965680ab50afb72874bd89cb087441b1ca3433d2d1551b6cd4cc4c814`. Read back
from the artifact: 63,131 `compact_fragments` (2,403 inputs were cross-corpus
duplicates), 5,041,855,558 bytes of payload, 1,205,395 `day_ownership`, 65,534
`fragment_origins` (one per *input* fragment — lineage keeps the duplicates
publication drops), 63,131 `points`, 63,131 `import_audit`. The same
merge previously could not complete at all: it was aborted twice, once parked
on the `day_ownership` bind (defect 7) and once on the in-transaction readback
(defect 8).

Merge order does not matter and cannot be chosen: `merge_source_v6` forbids the
target from existing or being an input, so there is no base to merge *into*.
The duplicate winner is the smallest `(source_sha256, source_name)`, publication
order is `(point_key, report_start_ms, fragment_id)`, copy order is by sorted
input path, and origins are re-sorted before the rewrite — so the artifact is a
function of the input set alone.

## Debian corpus `my_test_CX_GE_fixed` (2026-08-21)

Imported on `46.4.84.220` with `workers=30`: 38,305 HTML reports (36 GB) in
~15 min — **38,160 COMMITTED, 145 QUARANTINED** (144 × non-empty `walletSeries`
required, 1 × exactly one complete settings JSON object required).
`safe_to_delete=NO`, so the raw HTML must not be deleted.

All 145 were identified and inspected: `quarantine` stores no file name, so the
`source_sha256` values were exported and matched against a parallel sha256sum of
all 38,305 server-side HTML files — all 145 matched, confirming `source_sha256`
is the sha256 of the raw report. They are 102 × `CXMTUSDT_5m`, 36 ×
`CXMTUSDT_15m`, 6 × `CXMTUSDT_4h` (run ids 30577–38298) plus one stray optimizer
summary page, `report_optimizer_my_test_auto_x_auto_y_20260820_232555.html`,
which is not a run report at all. An earlier note here called all of these
source defects. That was wrong for the 144: `my_test_run_30577_of_38304_
CXMTUSDT_5m_2026-07-29.html` is a complete 1,183,513-byte report that emits
`const walletSeries = [];` and `const equitySeries = [];` because no trade
occurred in that shift window. They are valid zero-activity runs the importer
rejects — see Next. Published:
`compact_fragments` 38,160, `points` 38,160, `day_ownership` 766,702,
`import_audit` 38,305, max `generation_after` 38,160, 3,215,208,448 bytes.
Downloaded to `data/databases/`; byte size matches. The server-side original is
untouched. This run predates defects 7 and 8 above, so re-importing on the
fixed runner should be materially faster.

## Zero-activity runs are imported (2026-08-22)

Contract: [zero-activity spec](docs/specs/2026-08-22-source-v6-zero-activity-runs.md),
[ADR-0016](docs/decisions/0016-source-v6-zero-activity-runs.md).

The 144 `walletSeries`-empty quarantines of `my_test_CX_GE_fixed` were complete
reports of runs in which no trade occurred, not defects. They are now admitted,
but only on affirmative evidence: `Total Trades` and `Total transactions
(buy/sell)` present and zero, corroborating metrics consistent where present,
and the seven undefined ratios as the literal `n/a`. Absence of data is never
accepted as evidence of emptiness, because a truncated report has none either.
Opt-in per caller; only `normalize_source_v6` opts in, so ADR-0006's DD5
candidate contract is untouched.

Four defects were found by review across two rounds, each reproduced against
the repository's own fixtures before being fixed. A zero-activity outgoing fragment triggered
ADR-0013 seam exclusion and deleted the incoming fragment's only cycle while
reporting the batch `COMMITTED` — seam exclusion de-duplicates, and an empty
fragment has nothing to de-duplicate. And for an identical window the empty
fragment took ownership by `fragment_id` sort order, flagging the fragment with
four real actions as `AMBIGUOUS_INCOMING`. Round two found the mirror image of
the first — an empty *incoming* deletes the outgoing's open tail, one cycle and
two actions, also under `COMMITTED` — now `BRIDGE_NOT_COVERED`/`PARTIAL` as
ADR-0010 already specified; and that the new tie-break had desynchronised
`resolve_batch` from `persist_batch_resolution`, which re-derives the outgoing
side by bisecting its own ordering. A further one was found by the tests
themselves: a report with actions but empty series was admitted, which is not a
run where nothing happened but one whose samples did not render.

The 145th quarantine, an optimizer summary page, still fails. Re-importing the
CX_GE corpus is required to gain the 144 points; nothing is migrated.

## Surface publication throughput (2026-08-22)

Contract: [surface throughput spec](docs/specs/2026-08-22-source-v6-surface-throughput.md).

Measured on `my_test_CX_GE_fixed`, scope `CXMTUSDT|LONG|15m`, 648 fragments,
43 MB of payload: metadata + readiness 4.9 s, hydration 49.8 s,
`materialize_source_v6` 0.1 s, publication **100.4 s**, resident memory 42 MB to
2,450 MB. That is 239 ms per fragment; the whole 38,160-fragment corpus
extrapolates to ~2.5 h and ~74 GB resident, so it could not be published at all.

The preflight needs nothing: `preflight_source_v6` returns in 0.00 s and reads
no HTML, and `canonical_ready_intervals` costs 1.7 s over 38,160 fragments —
both already work from metadata. `folder1` correctly reports 0 READY scopes
because its widest grid is 12 of the required 114 point variants; `CX_GE`
reports 55 of 56.

Publication carried three defects already fixed elsewhere: it re-encoded the
sealed payload (59 ms each, and the result is byte-identical to what is stored —
120/120 on payload, codec and `payload_sha256`), inserted one statement per row
(736 rows/s), and ended by decoding every fragment again to check ids it could
derive directly (48.1 s of the 100.4 s). Payloads are now copied by SQL from the
source database, rows are written through `_insert_frame`, and publication
validates the C3a identity instead of reconstructing objects.

**Publication 100.4 s to 3.0 s (33x)**, same `surface_id`, and the two artifacts
compared directly: manifest, scope manifests, factual rows and the payload bytes
of all 648 fragments identical. The file is 43% smaller as a side effect of the
set-based insert.

Hydration is now the dominant cost and is untouched: `materialize_source_v6`
still rejects metadata views, so the caller decodes every fragment although
nothing on the publication path needs a decoded object.

The pass-through is opt-in and **the panel does not pass it yet**. Its single
production call site, `panel.py:1837`, was being edited by another session, so
switching it was left out rather than conflict. Until that one argument is
added the application still takes the 100.4 s path.

## Empty result combinations (2026-08-22)

Contract: [empty result spec](docs/specs/2026-08-22-source-v6-empty-result-combinations.md).

A "point" is a parameter combination — shift, open MA and close MA over one
symbol, side and timeframe — and since ADR-0016 one of them can be tested and
produce no trades. `calculate_metrics` raises for a combination with no samples,
and every consumer called it in a bare loop, so one such combination aborted the
whole build. Demonstrated: ten healthy combinations published, the same ten plus
one idle one raised and all eleven were lost.

Such a combination now keeps its cell in the canonical grid and carries the flat
result the tester itself declared — PnL 0, drawdown 0, no trades, every ratio
`None` under ADR-0006 — and is recorded under `empty_result_points`.

An earlier revision of this change excluded the cell instead, and that was wrong.
Exclusion published a 113-of-114 grid, which `load_source_v6_pipeline_input`
rejects with `INCOMPLETE_GRID` one stage later, naming neither the reason nor the
cell — a loud publish-time failure turned into a quiet artifact that dies later.
The objection to keeping it (that `build_persisted_analysis_facts` defaults a
missing metric row to 0% return at 0% drawdown, an outstanding risk-adjusted
result that never happened) does not apply: `annotate_eligibility` runs before
plateau geometry and rejects the cell with `REJECT_PNL_NONPOSITIVE` and
`REJECT_DD_NONPOSITIVE`. Verified — `plateau_id: None`, `role: UNASSIGNED`. It is
visible and unselectable, which is what a tested-and-idle combination should be.

A window that hides a *measurable* combination is still an error and raises,
naming the combination; that is a different fact and must not be flattened into a
zero.

The multiscope path needed the same rule one stage earlier: it stores facts, not
metrics, so it published happily and `run_multiscope_analysis` aborted
afterwards. `materialize_source_v6` now measures each scope over that scope's
READY witness — the same window the analysis measures over.

Coverage is not lost: the tested days remain in the source database as
`ACTIVE_EMPTY` under Z4.

## Next

1. Re-import `my_test_CX_GE_fixed` to pick up the 144 zero-activity runs and
   confirm `safe_to_delete` is no longer held at `NO` by them. The surface
   blocker that stood here is fixed — see "Empty result combinations" above.
2. Pass `source_database` at `panel.py:1837`, the only production caller of
   `publish_multiscope_surface`. One argument; it is what makes the 33x
   reachable from the application.
3. Let `materialize_source_v6` work from metadata. Readiness already does, and
   after the pass-through above nothing on the publication path needs a decoded
   fragment — so hydration is pure waste, and it is what makes a full-corpus
   surface need ~74 GB resident.
4. Close the same published-file gap on the import path. ADR-0015 scoped
   itself to the merge deliberately: `_publish_segments_single_pass` verifies
   its reduce target and then publishes a repack that receives neither the C3a
   payload readback nor `fragment_metadata`'s header pass, so the import
   publishes with weaker evidence than the merge now does — under the same
   `safe_to_delete=YES`. It needs its own change, not an assumption from
   ADR-0015's wording.
5. Remove the orphaned segment-merge path (`merge_source_v6_segments`,
   `_merge_segment_contents`, `_read_source_v6_segment`, `import_fragment`,
   `import_fragment_batch`) in its own `refactor:` commit — C5 left them
   without a production caller, and ~50 tests still reference them.
6. Open question from C8: the identity readback is a sequential scan per
   128-id window on both paths, so O(n²/128) in principle. It does not bite on
   import (5.52 s committed against 5.47 s with the metadata-only transaction
   open, at 5,000 fragments). A relation cursor is not the fix — `fetchmany`
   grew resident memory by 2.9 GB on a 4.3 GB corpus. A bounded-memory single
   pass is its own change.
7. The parse phase is now the dominant remaining cost. Compression level was
   measured and rejected. Swapping the raw-markup cross-check to the lexbor
   engine was measured at 3.9x on that step and then **reverted**: lexbor is an
   HTML5 tree builder and performs the same implicit-close recovery as lxml, so
   the two parsers stopped being independent. The cross-check must stay a
   tokenizer; no faster second parser has been found that preserves it.

## Prior feature: Source v6 fresh compact multi-scope

STAGE_1_GATE=ACCEPTED_BY_ROOT; date=2026-08-20; reviewer=CODE_REVIEW_PASS compact-publication and gate-checker final re-reviews; evidence=.codex/stage1-acceptance-ledger.md,.codex/task5-real-corpus-report.md,.codex/task6-merge-evidence-report.md,.codex/task6-recovery-overlap-report.md,.codex/task6-debian-recovery-report.md

## Verified implementation

- Fresh compact Source → multi-scope surface → separate analysis pipeline is complete.
- Panel supports multiple READY scopes; the analysis worker limit is
  `duckdb_import.workers` and `gap_rules` is part of analysis identity and
  selection.
- Independent review: `CODE_REVIEW_PASS`.
- Latest full local verification: `1206 passed, 2 skipped, 1 warning` via
  `.venv\Scripts\python.exe -m pytest -q`.

## Next: manual verification

1. In the panel, import the intended raw HTML set and select one or more READY
   `symbol|side|timeframe` scopes.
2. Confirm a new `.surface-v6.duckdb` appears under
   `Output/surfaces-v6-compact/`, then run analysis with the intended listing
   dates and configuration.
3. Confirm the `.analysis-v6.duckdb` appears under
   `Output/analysis-v6-compact/`; repeat after changing `gap_rules` and verify
   that it produces a distinct analysis artifact and expected structure result.
4. Before syncing Git, review the scoped diffs/commits; local `Input/`,
   `Output/` and `Data/` must remain untracked.

## Parallel panel work (2026-08-22)

Static Control Panel v1 is implemented and independently reviewed
`CODE_REVIEW_PASS`. It replaces the root shell, keeps `/legacy`, and covers
local testing, guarded remote profile operations, Source DB import/merge,
READY-only immutable surfaces, fresh analysis → local tester → Performance DB
and `CALCULATION_ONLY` DD5, plus local settings. Job terminal snapshots and
tester inbox lineage survive controller restart; interrupted remote importers
are rehydrated solely for a safe stop attempt. Portfolio remains disabled.

Latest verification: `1523 passed, 2 skipped, 1 warning` via
`.venv\Scripts\python.exe -m pytest -q`; focused panel suite: `68 passed`.
Contract and visual evidence: `docs/specs/2026-08-22-panel-static-frontend-v1.md`.

## Selected-scope surface materialization (2026-08-22)

The panel now keeps preflight metadata-only and hydrates only payload fragments
belonging to the explicitly selected READY scopes.  Hydration is deterministic
and parallel within the existing bounded worker limit; the whole-source lineage
digest still comes from validated Source DB metadata.  `materialize_source_v6`
remains hydrated and witness-based, because E1 requires `calculate_metrics` to
distinguish an actually idle point from data hidden by the selected window.

Evidence: `39 passed in 7.92s` over selected serial/parallel storage readers,
empty-result E1--E5, materializer, surfaces service and static panel tests;
independent review `CODE_REVIEW_PASS`.  The heavier throughput suite was not
claimed as evidence: its terminal invocation exceeded the local tool limit and
only its own child processes were stopped.  Source backup before this work:
`backups/surface-contract-before-metadata-materialization-2026-08-22.zip`.

Surface output now defaults to an editable `{pair...}_{start}_{end}` filename;
the immutable `surface_id` remains only in its manifest.  The Strategies/DD5
Analysis DB field derives an editable filename below saved `analysis_db_root`
and that full target is passed to the analysis runner.  Repeated explicit names
fail closed; automatically generated conflicting analysis names receive a
readable numeric suffix.  Evidence: `33 passed in 8.99s`, `node --check
src/mrs3/panel_web/app.js`, `git diff --check`; independent review
`CODE_REVIEW_PASS`.

The Strategies/DD5 selector now reloads every manifest-validated published
surface recursively from configured `source_v6_surface_dir`, falling back to
`data/surfaces`; full payload validation remains immediately before analysis,
not on panel bootstrap. `surface_target_path` is an approved, persisted panel
default, so the publication-card save button writes it to `config.local.json`.
Evidence: `49 passed in 15.64s`, JS syntax check, diff check, live local
catalog: one surface in `2.4s`; independent review `CODE_REVIEW_PASS`.

Fresh analysis now immediately reports its real entry phase, an indeterminate
bar and elapsed time; it does not invent a percent because the synchronous
analysis contract exposes none. The control is restored on every terminal
result. Evidence: `47 passed in 16.86s`, JS syntax check, diff check,
independent review `CODE_REVIEW_PASS`.

## Approved: Source v6 facts and metrics v2 (2026-08-23)

Contract and implementation plan are approved for a fresh-only rebuild:
[metric contract](docs/specs/2026-08-23-source-v6-metric-contract.md),
[ADR-0017](docs/decisions/0017-source-v6-facts-and-metrics-v2.md), and
[minimal rebuild plan](docs/superpowers/plans/2026-08-23-source-v6-minimal-rebuild.md).

### Stage 0 v1 same-host baseline (2026-08-23)

Source: 684 HTML reports from the retained 2026-07-15--2026-07-22 archive;
30 workers; all temporary databases and surfaces were written outside Git.

- Import, three runs: median `111.533 s`; spread `106.608..125.762 s`;
  Source DB median `30,683,136` bytes, spread `30,683,136..31,207,424`
  bytes; `684` accepted and `0` quarantined in every run. Identical source
  digest `935fb9c8270ce43a2510d08b4f2f0e1853aca7efb6a9a88d185ee49fb81551aa`
  and semantic signature
  `75c2ebe67dfb7de7281943c053478e736f83cd7ead025cebe0bbd731435e9dba`.
- ID-only selected materialization plus SQL-copy publication, three runs for
  READY `AAOIUSDT|LONG|15m`: median `71.160 s`; spread
  `67.527..72.910 s`; surface `27,537,408` bytes and the same surface id
  `454f083c1a13e987bb5ee00f030c5e02233f3c292e3f5f279a759a226221f3b8`
  in every run. Observed coordinator peak RSS
  median `1,588,150,272` bytes, spread `1,583,542,272..1,591,230,464`;
  worker peak RSS median `1,083,064,320` bytes, spread
  `1,012,334,592..1,083,469,824`. Scope digest:
  `f235abfd91ec20560757f188d284455b2a5dbec4d49df358d757a027d4401d1f`.

**Next step:** begin the atomic facts-only v2 boundary with TDD. No v2
performance or correctness claim is valid until a post-M7 fresh import has
zero quarantines and Stage 4 evidence.

### Stage 1 facts-only payload v2 (2026-08-23)

Implemented in the working tree and accepted by the mandatory independent
reviewer bridge (`CODE_REVIEW_PASS`, Claude Sonnet 5, medium effort). The
canonical fragment now persists factual actions only; cycles, events and
open-tail state are reconstructed deterministically by the typed decode path.
v1 payloads, source databases and resume tokens are rejected or invalidated by
the v2 schema/fingerprint boundary. The W6 identity readback validates raw
factual payloads without reconstructing derived facts, while full materializer
decode checks all derived header/cache counts, including in-range tampering.

Evidence: `.venv\\Scripts\\python.exe -m pytest -q
tests/test_source_v6_stage1_v2.py tests/test_source_v6.py` — 85 passed;
focused Stage 1/merge suite — 258 passed; `git diff --check` clean apart from
existing Windows line-ending warnings. Stage 2 is the next implementation
slice.

### Stage 2 metrics and compact analysis row (2026-08-23)

Implemented and committed as `9e92897`. The metric pass now derives
deterministic round trips and weighted exposure from seam-owned actions,
computes raw-anchor PnL before retaining the publication rebase, uses all
realising actions for Profit Factor, and combines merged-series and admissible
declared drawdown with an auditable source/tie set. The same pass emits strict
v2 Decimal-string analysis rows, including weighted trades and drawdown audit
fields; hydrated and worker paths carry the same rows and digest without
payload decoding in analysis.

Evidence: Stage 2 contract/worker/empty/source suite — `105 passed, 1 warning`;
consumer/storage/fresh-analysis suite — `142 passed, 1 warning`; final
official implementation review bridge — `CODE_REVIEW_PASS`; `git diff --check`
clean apart from existing Windows line-ending warnings. Stage 3 M7 validation
is the next implementation slice.

Pre-Stage-1 repository verification: `1597 passed, 2 skipped, 2 warnings` via
`.venv\Scripts\python.exe -m pytest -q`. The warnings are the existing tar
deprecation and unavailable Windows pytest cache; two symlink tests skip on
this host.

### Materializer and remote panel boundary (2026-08-23)

The static panel's Source v6 surface path is DB-native: selected metadata is
measured by `materialize_source_v6_from_database`, publication copies sealed
payloads into a `.surface-v6.duckdb`, and fresh analysis consumes its compact
`point_analysis_input` rows. The panel's initial surface validation now uses
`decode=False`, so the analysis button does not decode factual payloads before
the row-based analysis path.

`data/databases/my_test_CX_GE_fixed.source-v6.duckdb` was repaired through an
exact replacement merge for the quarantined
`my_test_run_15426_of_38304_REGNUSDT_45m_2026-07-29.html`. The canonical DB is
schema 6 / `source-v6-fresh-compact-v2`, contains 38,304 fragments and zero
quarantine rows; `validate_source_v6_database` and panel surface preflight
return clean (`56` rows, `7` groups). The pre-repair DB remains recoverable as
`my_test_CX_GE_fixed.pre-repair-quarantine.source-v6.duckdb`. Optimizer HTML
remains excluded during Source v6 preflight.

The configured Debian remote paths were checked read-only with the same SSH
script used by the panel: all five directories exist and the disk probe
returned a numeric value. The static panel's remote path check was returning
404 because its POST route was missing from the HTTP allowlist; the route and a
regression test are now present. `scripts/restart_new_panel.bat` also stops the
old 8766 listener before starting the canonical static panel, preventing stale
duplicate processes. A BAT launched from the normal desktop has ordinary
Windows network access; Codex's restricted shell cannot grant that access to a
child process, so live SSH checks must be run outside that sandbox.

The repaired CX/GE replacement was reproduced from the remote HTML: 158
actions produce `gross_profit=398.1348` and `gross_loss=-0.0640`, hence the
legitimate `Profit Factor=6220.85625`. M7 accepts absolute Profit Factor drift
up to `0.01` while still rejecting material mutations. Merge now fails closed
on unresolved quarantine instead of silently dropping it.

The static local merge card now persists all three paths, offers configured
Source DB candidates through native datalist controls, and renders determinate
fragment progress with current status polling.

Evidence: Source v6 M7 tests `12 passed`; the panel/materializer/remote focused
suite is green after the change. The Debian runner needs the same M7 module
deployed before rebuilding that corpus.

### Restored legacy remote Source DB panel scenario (2026-08-24)

Commits `c1ea7e5`, `c09f58f` and `7712199` restore the previous remote import
workflow: the panel again exposes editable runner HTML, remote staging DB and
local target fields, saves those paths, and starts imports with the legacy
`remote_html_path` / `remote_db_target` / `local_target_path` payload. Saved
paths round-trip through `/api/v2/settings/save` and bootstrap; changing the
HTML folder refreshes derived targets. Remote paths remain validated against
configured roots, while the local output target keeps the prior operator-
selected behavior and is used by verified delivery.

### Self-contained tester inbox strategies (2026-08-26)

The failed `PANW_PLTR` Performance DB import was traced to its committed
manifest pointing at the inaccessible/deleted `Output\\strategies` files.
The exact 196 strategy hashes were still present in the tester's
`settings_strategy` directory. The repaired inbox now stages those bytes under
its own `strategies` directory, and the import completed with `196/196`
imported and zero quarantine.

`capture_verified_inbox` now stages every ordinary tester strategy in the
immutable inbox at capture time, matching the existing v6 inbox contract and
making the flow independent of `Output\\strategies` lifetime or batch size.
Focused inbox evidence: `15 passed`.

Evidence: all panel tests (`test_panel*.py`) — `345 passed, 1 warning`;
settings/remote/static focused slice — `96 passed, 1 warning`; mandatory
reviewer — `CODE_REVIEW_PASS`; `py_compile`, `node --check` and `git diff
--check` clean apart from the existing Windows pytest-cache warning.

### READY JSON request diagnostics (2026-08-26)

The static panel has one listener on `127.0.0.1:8766`; the restart script
replaced it with the current process before this check. For
`/api/v2/strategies/fresh/generate`, malformed early HTTP requests now report
their actual cause (`Content-Type`, malformed `Content-Length`, or invalid
body size) rather than the misleading generic `invalid settings`. This closes
the last generic-error path before strategy generation begins.

Evidence: focused fresh-strategy/static-panel suite — `67 passed`; live
post-restart probe returned `415 Content-Type must be application/json` as
expected; independent review — `CODE_REVIEW_PASS`; `git diff --check` clean.

### Unified Performance DB v2 import (2026-08-31)

### Performance v2 single-strategy A/B analysis (2026-08-31)

Phase 2 is implemented: the panel now lists ACTIVE strategies from the v2
database, pre-fills UTC bounds, and calculates two safe UPNL-relative windows
for one authoritative current result. Valid unavailable windows return a
normal result; strict invalid input is typed `400`; unavailable/stale strategy
is typed `404`; cache conflict and writer lock are typed `409`. The only
database mutation is the existing versioned `window_metrics` cache.

Verification: focused v2 selector `84 passed in 13.55s`; panel/integration
`154 passed`; v1 non-disturbance `109 passed`; `node --check` and
`git diff --check` passed. Live smoke on the imported DB left
strategies/results/actions/equity at `1633/1633/741189/9305765`, added one
cache row, and measured 354 ms first calculation / 70 ms cache hit.
Independent Opus review: `CODE_REVIEW_PASS`.

### Performance v2 finalist-selection Stage 2 executable export (2026-08-31)

The selection panel additionally has an explicit pre-XLSX counter refresh. It
returns the current ordered stage snapshot as `eliminated` and `remaining` for
the chosen Pair + Side, and displays it immediately before the stage move
buttons. Counters are invalidated by any Pair, Side, checkbox, scope or order
edit; an in-flight stale response is discarded. The preview is read-only: it
does not generate a workbook or mutate Performance DB v2.

Evidence: focused selection/panel/static suite `130 passed`; `node --check`,
`git diff --check`; independent Opus re-review `CODE_REVIEW_PASS`.

XLSX is now fail-closed on default-window cache readiness: Pair + Side changes
show whether recalculation is required and keep XLSX disabled until every ACTIVE
current result has its full, A and B facts. The same check rejects direct XLSX
requests with typed `SELECTION_CACHE_INCOMPLETE` (`409`), so the browser button
cannot be bypassed. Evidence: focused suite `133 passed`; independent Opus
review `CODE_REVIEW_PASS`.

The preview is now executable. On `Смотреть результаты в xls`, the Panel sends
the current Pair + Side, checkbox state, stage order and per-stage scope to the
local v2 endpoint. It reads only ACTIVE current Performance DB v2 rows,
derives DD5_PROXY calculation facts, holding p95, ordered plateau point counts
and default A/B facts, then applies the 14 closed built-in filters/Pareto stages
strictly in submitted order. `pair_side_timeframe` splits comparisons by
timeframe; missing facts never eliminate or dominate; unavailable A/B records
remain finalists with `AB_NOT_EVALUATED_INSUFFICIENT_DATA`.

The stage registry now also includes disabled-by-default `filter_min_shift`.
Its compact inline field accepts a positive percentage (default `0.3`); when
enabled it eliminates a strategy if any existing order has a smaller shift.
Missing order-shift facts do not eliminate. The threshold participates in the
same local order, automatic counters and XLSX stage boolean as other filters;
it needs no fact recalculation. The Balanced Pareto label now correctly names
its primary metric as `PnL DD5/30`, rather than `PnL30`.

Disabled-by-default `pareto_window_b` compares survivors within Pair + Side +
timeframe: it maximizes B PnL/30d and B trades/30d, and minimizes B drawdown
and B holding p95. The latter uses completed positions closed in the final B
window; missing B facts never dominate or eliminate.

The adjacent disabled-by-default `pareto_window_b_dd_shift` maximizes B
PnL/30d and first shift while minimizing full-period drawdown, within the same
Pair + Side + timeframe scope.

Focused evidence after this change: `142 passed` for the selection, panel and
static-UI suites; `node --check src/mrs3/panel_web/app.js`; `git diff --check`.

The endpoint returns one in-memory attachment with `All candidates` and
`Finalists` sheets. Both retain exported A/B change, A/30d and B/30d fields;
all other A/B support fields remain internal. The
workbook retains every requested Pair + Side candidate, ordered plateau facts,
per-stage booleans, finalist flag and elimination reason. The run writes no
`selection_*` tables, tags, lifecycle state, discard, RETEST or history; those
remain Stage 3.

Focused acceptance evidence: `126 passed` for
`tests/test_performance_v2_selection.py`,
`tests/test_panel_performance_v2.py` and `tests/test_panel_static_ui.py`, plus
`node --check src/mrs3/panel_web/app.js` and `git diff --check`. The HTTP test
proves a `POST /api/v2/strategies/performance-v2/selection` returns an XLSX
attachment and leaves no selection tables. Independent Opus review:
`CODE_REVIEW_PASS`. Full suite evidence: `2023 passed, 2 skipped` plus seven
local-testing cases that initially lacked the ignored local `Input/` templates
in this worktree; the same seven passed after a local ignored junction exposed
the existing templates (`7 passed`).

### Performance v2 finalist-selection Stage 2 design and UI preview (2026-08-31)

The Panel now has a preview-only `6. Парето и фильтры` card: Pair + Side,
editable stage order, enable checkboxes and a per-stage Pair + Side or Pair +
Side + timeframe scope. Holding p95, A/B deterioration, Balanced Pareto and
the robust stages are enabled by default; Trades, Minimum Shift and both
plateau-points Pareto stages are disabled. Near-tie ranking and the former
global grouping block are hidden. No preview interaction calculates or
changes the database.

The selected strategy details for manual A/B analysis now show Close MA and
ordered Open MA/shift/multiplier/lot parameters; normalized values display at
two decimal places, trade rate is per 30 days, and holding duration is minutes.
The v2 catalog serves this metadata. Focused evidence before this documentation
update: `87 passed` for `tests/test_panel_static_ui.py` and
`tests/test_panel_performance_v2.py`, `node --check src/mrs3/panel_web/app.js`,
and a live BABAUSDT LONG catalog probe after panel restart.

The accepted next implementation contract is
`docs/specs/2026-08-31-performance-v2-finalist-selection.md`; its executable
plan is `docs/superpowers/plans/2026-08-31-performance-v2-finalist-selection.md`.
Stage 2 will calculate all visible built-in stages from current Performance DB
v2 facts and download an XLSX without persisted selection runs, tags, discard
or RETEST. Those lifecycle items are explicitly Stage 3.

Manual A/B output now keeps one four-column comparison table (metric, window A,
window B, change), semantic delta colours, and period shortcuts.  Response
serialization additionally derives a 30-day equivalent from each window's own
effective timestamps without changing the stored window cache: return and
growth factor are duration-normalized, trade rate is shown per 30 days, while
drawdown, fees, PF and similar metrics remain explicitly raw.  Windows shorter
than one day or with invalid duration show a status instead of a misleading
normalized value.  Evidence: `97 passed` focused, `154 passed` panel and
integration regression, `node --check`, `git diff --check`; reviewer:
`CODE_REVIEW_PASS`.

Native SINGLE_MODE now creates a metadata inbox that refers to the tested HTML
and `Output\\strategies` files without copying either directory. Import stages
and hash-verifies HTML once, parses it with the configured worker count, and
uses DuckDB dataframe appends for action/equity batches. It no longer performs
eager A/B-window calculations during import. The panel progress track receives
the live parse count and reaches 100% on commit.

Live evidence: `b26ac4db5f0b49c5a2fc17bd4561e4bd` committed `1633/1633`
reports with zero rejected/skipped. The target contains 1,633 strategies,
4,517 orders, 741,189 actions and 9,305,765 equity samples; audit is
`COMMITTED`. The completed import cleared the tester report directory and
`Output\\strategies` as requested. Focused tests: `143 passed` for the v2/panel
slice and `53 passed` for static-panel UI after the progress-track check.

Surface publication now has one server-owned output root: `panel.path_defaults.surface_target_path`.
Catalog listing and both publish entry points ignore request-supplied target paths;
the UI no longer exposes the stale `D:\\MRS3\\surfaces` default or a save-path button.
The configured project path is `D:\\SHARE\\!MN\\hamster\\MRS-Analizer\\data\\surfaces`.
Focused evidence: `216 passed`; full suite: `2050 passed, 1 failed, 2 skipped`;
the remaining failure is the pre-existing worker-default mismatch in
`config.performance.json` (30) versus its test expectation (16).

The READY JSON endpoint now accepts up to 1 MiB because a checked READY
selection can legitimately exceed the generic 64 KiB API limit. The live
70 KiB probe reached candidate validation, proving it no longer fails on body
size; all other endpoints retain the 64 KiB limit. Focused evidence: `68
passed`; independent review — `CODE_REVIEW_PASS`.

### Performance v2 persisted finalist snapshots (2026-09-02)

[ADR-0021](docs/decisions/0021-performance-v2-persisted-selection-snapshots.md)
records the accepted deferred Stage 3 architecture: an explicit save creates an
immutable Pair + Side selection snapshot, later used by the A/B `Только
финалисты` catalogue filter. Stage 2 remains stateless; the currently visible
checkbox stays disabled until this persistence contract is implemented.

The next accepted Stage 2 extension is specified in
`docs/specs/2026-09-01-performance-v2-robust-finalist-ranking.md`; its
executable plan is
`docs/superpowers/plans/2026-09-01-performance-v2-robust-finalist-ranking.md`.
It adds best-trade dependency and four-window consistency filters, a robust
Pareto, a 10%-tolerant preference for larger first Shift, and a fixed final
38/17/15/10/10/10 Top-50 ranker for Robust PnL, worst DD, A/B stability, Shift
1, minimum Points and Close MA. Existing Performance DB v2 tables are sufficient;
selection persistence, tags and XLSX import remain deferred to Stage 3.

Local implementation now adds the four robust movable stages, fixed final
Top-N ranker, seven-window cache requirement, rank diagnostics in XLSX and
panel controls. Focused verification passed: `155 passed` in finalist/static
panel suites and `167 passed` in panel/surfaces suites, plus
`node --check src/mrs3/panel_web/app.js` and staged `git diff --check`.
Follow-up independent review returned `CODE_REVIEW_PASS` before commit.

### Performance v2 selection review lifecycle design (2026-09-02)

The next approved Stage 3 contract is
`docs/specs/2026-09-02-performance-v2-selection-review-import.md`; its
implementation plan is
`docs/superpowers/plans/2026-09-02-performance-v2-selection-review-import.md`
and the storage decision is
`docs/decisions/0022-performance-v2-selection-review-ledger.md`.

The design revises final ranking to `30/15/15/12/10/9/9` for Robust PnL,
Worst DD, A/B stability, Worst Hold p95, Shift 1, PointsMin and Close MA. It
collapses surviving exact Pair + Side + TF + ORD + Close MA analog groups to
one weighted representative before a default Top-20 ceiling. The current
Close-MA near-tie Pareto remains available but becomes disabled by default.

Every downloaded selection becomes an immutable schema-v3 snapshot in the same
Performance DB. XLSX review keeps automatic and user status/rank separately;
successful imports append history and maintain only the current `REJECTED` tag.
`REJECTED` includes the previously discussed “мусор” meaning and never deletes
data. Strategy deletion, RETEST and generic tags remain outside this contract.

Implementation is complete. Performance DB v2 auto-migrates once from internal
schema 2 to 3 in one transaction; it preserves existing facts and adds immutable
selection runs/results, immutable XLSX review ledger and the scoped durable
`REJECTED` tag. The current local DB migration preserved 16,272 strategies,
16,272 results, 10,432,397 actions, 77,787,295 equity samples and 113,425
window-metric rows; it finished in 0.743 s. Reopening schema 3 completed in
0.089 s and does not require fact/cache recalculation. The existing tester
import contract remains unchanged.

Selection export now persists the exact calculated run and produces editable
review columns plus a very-hidden metadata sheet. The panel imports every XLSX
in a selected folder as an independent atomic request, validates the workbook
against the newest saved run and current facts, appends review history and
updates only `REJECTED` tags scoped to that run. A/B Pair filtering offers
`Только финалисты` after a saved selection exists and uses the latest effective
review result. The revised 30/15/15/12/10/9/9 score, exact analog groups and
Top-20 default are active.

Analog grouping additionally treats Close MA values differing by one as the
same group only when Pair, Side, timeframe, order count and the exact
`(analysis_run_id, plateau_id)` of every order match. It is deliberately
non-transitive: `5/6` may group, but `5/6/7` is split into `5/6` and `7`.
Focused regression evidence: `174 passed` across selection, selection-review
and panel/static-UI suites, plus `node --check` and `git diff --check`.

Selection `Trades` now uses the full-window completed-round-trip count from
`WindowMetrics.trade_count`, not the tester's execution-level `TotalTrades`.
For the diagnosed SNOWUSDT examples it reports ID 961 as 49 and ID 1110 as 51;
their former report-level values were 183 and 166. The focused selection,
selection-review and panel/static-UI evidence remains `174 passed`.

`Positive quarters` now exports `positive/available` rather than hiding an
incomplete early report fragment. For example, RDWUSDT ID 9428 has three
usable quarter windows and now exports `3/3`; an unavailable first window is
not counted as negative. The consistency filter continues to evaluate the
numeric positive count and does not reject missing data by itself. Focused
selection, selection-review and panel/static-UI evidence: `174 passed`.

Fresh verification: `214 passed` across Performance v2 store/import/selection,
selection-review and panel/static-UI suites; the full repository suite passed
with `2097 passed, 2 skipped, 1 warning` in 737.79 s. The skips are Windows
symlink permission cases and the warning is the existing Python 3.14 tar
extraction deprecation. `node --check src/mrs3/panel_web/app.js`, Python
byte-compilation and `git diff --check` passed. Acceptance still requires one
operator round-trip through Microsoft Excel and an independent code review; the
external review bridge was unavailable and a fallback reviewer exhausted its
usage quota, so no review disposition is claimed. Next step: perform the Excel
round-trip, obtain review, then commit the scoped change.

Independent Opus plan review initially found lifecycle edge cases around scoped
tag removal, repeated/stale workbooks, manual analog transitions, prior
rejections and manual Top-N overrides. The contract and plan now define and test
those cases; the final disposition is `PLAN_APPROVED`.

### Performance v2 calendar-window normalization (2026-09-02)

30-day metrics now normalize over the requested calendar interval intersected
with the report range, never the shorter first/last event span. The panel
displays both ranges. XLSX exposes the actual normalizers as blue centered
`Дней A` and `Дней B` columns next to the respective A/B PnL/30 columns;
their widths are based on data values. The raw cache remains valid and does not
need recalculation. For ID 8859 the B interval is 14 calendar days while its
event span is 2026-08-13T10:13Z through 2026-08-15T18:43Z; the corrected B
values are 8.3511%/30d and 23.5714 trades/30d rather than an event-span
extrapolation. Focused regression suites: `77 passed` for panel/window/HTML
and `145 passed` for selection/import/static UI/audit export.
The current HTML importer accepts a timestamp exactly at the tester's declared
inclusive report end and rejects only timestamps later than that endpoint;
optional declared transaction/final-balance checks remain skipped when an older
current report omits those fields.

The re-review of calendar normalization, import integrity and A/B XLSX
duration columns returned `CODE_REVIEW_PASS` after bounds were threaded through
selection as well. No cache migration or recalculation is needed.

### Performance v2 selection comparable windows (2026-09-03)

Task 3a of the active RETEST workflow is independently reviewed
(`PLAN_APPROVED`, then `CODE_REVIEW_PASS`). `filter_low_trades` now uses
`Trades/30`; raw `total_pnl_pct` remains an audit field but is absent from the
selection XLSX. Time consistency uses four equal calendar windows at 28 days
or more, three at 21--28 days, and otherwise `UNAVAILABLE` without excluding
the strategy. `NO_TRADES` is excluded from the assessed denominator; unsafe
windows are `UNAVAILABLE`. Cache metrics version is `performance-window-v2.2`, so v2.1
rows are not reused. Raw DD and the `PnL/30 * 5 / raw DD` proxy are unchanged.

Evidence: focused selection suite `78 passed`; selection plus review suite
`102 passed`; related suites `146 passed`; full `.venv` suite `2165 passed,
2 skipped, 1 warning` in 725.00 s; `git diff --check` passed. The skips are
Windows symlink-permission cases and the warning is the existing Python 3.14
  tar-extraction deprecation. The legacy posttest/DD5 module and its dedicated
  CLI/panel routes were removed after call-site tracing; the v2 Performance
  workflow remains the supported path.

### Performance v2 RETEST manifest and mixed-run input (2026-09-03)

Task 4 of the active RETEST workflow is implemented and independently reviewed
(`CODE_REVIEW_PASS`). `build_retest_manifest` renders only ACTIVE RETEST
strategies with current results, rechecks typed identity and plateau facts,
publishes the existing strategy output paths with staged hash binding, and
records `strategy_analysis_run_ids` per JSON file. The input boundary uses the
per-entry run for strategies, orders and plateau facts; v6 provenance is
authoritative and legacy manifests retain the common-run fallback. Duplicate,
malformed and incomplete identities/maps fail closed; failed publication keeps
the prior batch recoverable.

Evidence: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_retest.py
tests/test_performance_v2_input.py tests/test_performance_v2_store.py -q` —
`93 passed` in 9.60 s; related manifest/batch suites — `28 passed` in 2.62 s;
`git diff --check` passed. No real DuckDB or generated output was modified.

### Performance v2 CHECK & RETEST implementation (2026-09-03)

Tasks 5-6 and the legacy-removal portion of Task 8 are implemented. The import
contract applies listing-date plus five-day warm-up per strategy, excludes whole
crossing lifecycles, recomputes retained evidence, keeps raw DD, publishes valid
siblings while retaining RETEST for invalid ones, and exposes a safe CSV/XLSX
failure report link. The panel provides separate SINGLE_MODE CHECK and
server-built IMPORT & REPLACE actions with persisted recovery, atomic duplicate
reservation and path-safe artifact streaming. `posttest` and the obsolete DD5
runtime are no longer live paths.

Evidence: focused RETEST/import/selection/panel suites `386 passed, 1 skipped`
(Windows symlink capability); `node --check src/mrs3/panel_web/app.js`; clean
`git diff --check`; external Opus review `CODE_REVIEW_PASS` after three rounds.
At that point Task 7 production DB backup/migration/HIGH+REVIEW seed remained
pending; it was executed later under explicit authorization as recorded below.

### Performance v2 Task 7 production migration and audit seed (2026-09-04)

With explicit user authorization, the existing local Performance DB was checked
under `PerformanceV2WriterLock`. The target was already schema v4, so
`initialize_performance_v2` performed idempotent v4 validation without a forced
rewrite. The HIGH and REVIEW sheets of
`Output/performance-v2-period-integrity-audit-2026-09-02.xlsx` contained 49 and
100 rows respectively: exact `Strategy ID` headers, 149 integer values, zero
duplicates and zero IDs missing from `strategies`. `mark_retest_from_audit`
seeded exactly 149 unique `RETEST` IDs with source
`PERIOD_INTEGRITY_AUDIT`.

Before mutation an adjacent ignored backup was created at
`data/performance-v2/strategy_performance.duckdb.task7-backup-20260904T045944Z`;
it is 7,603,499,008 bytes with SHA-256
`1748395f353476018efeb77b88a7c6755a4071f2ce47d5a229917282ebb2eb9c`, and its
read-only DuckDB catalog/schema probe passed (v4, 15 tables, 4 sequences, 6
indexes, 16,272 strategies). The post-mutation target hash differs as expected
because the RETEST seed changed the database; the backup is the pre-seed
snapshot. A second lock-protected initialize/seed pass kept schema/catalog,
exact RETEST set, REJECTED rows and all facts unchanged, with zero net new seed
rows. The lock released cleanly and immediate reacquisition succeeded.

Facts before and after the idempotence pass: strategies/results `16,272 / 16,272`,
orders `41,280`, actions `10,432,397`, equity `77,787,295`, window metrics
`113,426`, analysis plateaus `1,468`. The backup, DB and lock are covered by
`.gitignore` rule `[Dd]ata/`; no generated artifact or source file was committed.
The original pre-copy source hash was not persisted, so byte-for-byte fidelity
is evidenced by the backup size/hash and independent read-only catalog/count
probe rather than a retroactive source-hash comparison.

## Performance v2 RETEST recovery and import retry fix (2026-09-04)

Panel reload recovery now selects the newest committed RETEST inbox before
active or failed jobs, so a stale FAILED tester cannot keep `IMPORT & REPLACE`
disabled. A real failed Performance v2 import (`PERFORMANCE_V2_IMPORT_FAILED`)
or cancelled import can be retried; committed, running, interrupted and
unknown-error jobs remain duplicate-protected. The recovery selector is a
browser-served, Node-tested helper, and its static route is covered.

Evidence: RETEST/panel/job suites `91 passed, 1 skipped`; both JavaScript files
pass `node --check`; `git diff --check` passes. External Opus review completed
three rounds; final local fixes addressed its remaining recovery and static
delivery findings, with no fourth review requested because the review budget
was exhausted.

## Performance v2 panel listing-date root fix (2026-09-04)

Panel imports now resolve the configured relative `listing_dates_path` from the
server project root, with a safe inbox-parent compatibility fallback. Client
payloads cannot provide the trusted root. Focused checks pass (`7 passed,
2 skipped` for Windows symlink capability); full suite baseline was `2238
passed, 3 skipped`. The scoped fix is committed.

## Performance v2 typed-config dedup contract (2026-09-04)

The active import contract now treats executable settings, not strategy names
or analysis lineage, as the deduplication identity. The key includes Close MA,
Open MA, shift and quantized `lot_x`; this preserves intentional EQUAL/INCOME
variants. Effective coverage uses the configured listing date plus the existing
five-day warm-up, symmetrically for new and legacy-null result provenance.
Only a proper interval superset may replace a canonical result; equal, narrow
or shifted intervals are skipped. The one-time production audit is dry-run by
default and soft-discard is allowed only behind an explicit apply operation.

Read-only baseline and post-implementation audit are recorded in the ignored
artifact
`data/performance-v2/typed-config-dedup-audit-20260904T064327Z.json`:
7,904 apparent groups when `lot_x` is omitted (intentional EQUAL/INCOME
pairs), zero exact full-key groups, zero unresolved effective intervals;
16,272 strategies/results and 41,280 orders remain active, with 149 RETEST
tags. All 16,272 active typed keys are computable with zero order-count
mismatches. The importer and RETEST regression suites pass (92 tests), and the
broader Performance v2 regression set passes (216 tests, 1 Windows symlink
capability skip); no
database mutation or duplicate cleanup was warranted.

## Performance v2 lot-variant redundancy filter (2026-09-04)

The selection pipeline now has a default-on `filter_lot_variant_redundancy`
stage. It runs first, is fixed to `pair_side_timeframe`, and groups only
identical executable settings with the same known effective comparison
interval; order lots are intentionally excluded from the canonical key. The
representative is chosen by `dd5_proxy` descending, `capital_proxy` ascending,
`robust_pnl_30d_pct` descending, `worst_drawdown_pct` ascending,
`profit_factor` descending, then `strategy_id` ascending. Missing or malformed
intervals/metrics fail closed. Losers remain in All candidates as
`FILTERED / LOT_VARIANT_REDUNDANT` with representative and group-key audit
fields, and never reach later filters, Pareto or Top-N. Import and database
rows are unchanged.

The panel exposes the checked-by-default fixed-first stage and the persisted
`lot_variant_redundancy_enabled` setting. The active specification is
`docs/specs/2026-09-04-performance-v2-lot-variant-filter.md`.

Evidence: `.venv\\Scripts\\python.exe -m pytest tests/test_performance_v2_selection.py
tests/test_panel_static_ui.py tests/test_panel_performance_v2.py -q` —
`200 passed`; broader Performance v2/panel set — `411 passed`; `node --check
src/mrs3/panel_web/app.js`; `git diff --check`. External reviewer was
unavailable by user instruction; root self-review covered the scoped diff and
the fail-closed/default-order invariants.

## Performance v2 targeted cache warming after RETEST (2026-09-04)

Cache recalculation now resolves missing current `result_id` windows first and
passes only those strategy IDs to the workers. Existing cached strategies in
the same pair are no longer recalculated; the all-pairs action applies the same
missing-ID selection per pair/side. The panel keeps the existing pair-level
button, but its backend work is now incremental.

Evidence: focused selection/panel/retest/review tests — `201 passed`; `node --check
src/mrs3/panel_web/app.js`; `git diff --check`.
