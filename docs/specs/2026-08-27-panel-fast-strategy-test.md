# Panel Fast Strategy Test

**Status:** Implemented in working tree; pending commit
**Date:** 2026-08-27
**Owner surface:** Panel Web → Tester batch
**Implementation plan:** [2026-08-27-panel-fast-strategy-test.md](../superpowers/plans/2026-08-27-panel-fast-strategy-test.md)

## 1. Goal

Add a new, independent **Fast TEST стратегии** action beside the existing
**Проверить и запустить стратегии** action. The new path must test all READY
strategy JSON selected during generation with the minimum necessary disk and
validation work while preserving bounded tester concurrency, retries and a
recoverable list of missing reports. A completed Fast TEST can be handed off
to the existing Performance DB flow on demand from **Inbox → Performance DB →
Проверить**.

Fast TEST is a new orchestration path. It must not call the existing
`runner.workflow.run_batch` or change the behavior of the old tester and RUNS
buttons.

## 2. Non-goals

Fast TEST does not:

- create a Performance DB or verified inbox automatically when testing ends;
- copy reports or strategies into archival/backup directories;
- parse full PnL/DD/trade series while testing;
- delete HTML after Performance DB import;
- change strategy JSON shape or add provenance to tester JSON;
- replace or refactor the existing ordinary tester batch or RUNS workflow.

The existing Performance DB importer remains the owner of import, audit and
post-import cleanup. Fast TEST only provides an on-demand standard inbox handoff.

## 3. Inputs and fixed paths

The controller supplies the validated generation manifest returned by the
current READY generation. Fast TEST must not rediscover a manifest by scanning
arbitrary directories.

Runtime paths come only from validated `RunnerConfig`:

- source strategy JSON: the manifest's strategy directory;
- tester strategy directory: exact `<bot_root>\settings_strategy`;
- tester report directory: exact `<bot_root>\tester\report\my_test`;
- tester result/progress files: existing `RunnerConfig` paths;
- Fast TEST state: `<bot_root>\tester\report\my_test\fast_test_manifest.json`.

The request contains:

```json
{
  "analysis_run_id": "<current generated analysis id>",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD"
}
```

The recovery request contains only the prior Fast TEST `job_id`. Recovery uses
that job's persisted Fast TEST manifest; it does not depend on the currently
opened analysis.

## 4. Generation lineage and plateau diagnostics

`analysis_run_id` identifies which READY generation the user selected. It is a
lineage label, not the source of plateau metrics and not a required future
Performance DB join.

The existing generation manifest must persist the diagnostics already computed
by `fresh_analysis_strategies.py`. Strategy JSON remains template-only. Add one
candidate-level object:

```json
{
  "candidate_diagnostics": {
    "<candidate_identity>": {
      "order_count": 2,
      "orders": [
        {
          "order_id": 1,
          "plateau_id": "P100",
          "plateau_point_count": 3,
          "base_point_trades": 20,
          "plateau_total_trades": 61
        }
      ]
    }
  }
}
```

For a multi-order structure, scalar/list diagnostics are normalized into one
entry per order. The generation manifest hash covers this object. The manifest
validator rejects missing or malformed diagnostics for newly generated Fast
TEST input.

At Fast TEST start, write one compact `fast_test_manifest.json`. It contains:

- format version and Fast TEST job id;
- optional `analysis_run_id` lineage label;
- test start/end dates;
- expected strategy names and their generation hashes;
- strategy name → candidate identity mapping;
- candidate diagnostics needed by later Performance DB work;
- `strategy_batch_size`, `max_parallel_submissions` and
  `max_strategy_attempts` used by this run;
- per-strategy total attempt count;
- verified report filename per successful strategy;
- current failed/incomplete strategy names and terminal outcome.

Writing the compact manifest is O(number of selected strategies plus orders),
has no DuckDB read and must not materially delay strategy generation or test
startup.

READY generation does not compare the runtime `AlgorithmConfig` with the
analysis manifest hash. The analysis hash remains lineage metadata only;
generation uses the supplied runtime config for serialization and its existing
strategy validation rules. A config mismatch is not a generation error.

## 5. Tester HTML profile

Before starting the first batch, update only these `config_tester.json` fields:
the report switches live under the nested `report` object in the tester config.
The legacy top-level copies may be mirrored for older tester builds, but the
nested values are authoritative.

```json
{
  "StartDate": "YYYY-MM-DD",
  "EndDate": "YYYY-MM-DD",
  "use_runs": false,
  "enable_html_report": true,
  "include_chart_ohlc": false,
  "include_chart_balance": true,
  "include_chart_position": false,
  "include_strategy_settings": true,
  "include_trades_table": true,
  "include_summary_table": true,
  "include_monthly_returns_heatmap": false,
  "include_position_stats": false,
  "enable_timing_logs": false
}
```

`include_chart_balance=true` is mandatory because Performance DB requires the
wallet/equity series. Strategy settings, trades and summary remain mandatory.
Fast TEST does not overwrite tester-owned settings such as `max_parallel_runs`.

## 6. Batch algorithm

For one Fast TEST start:

1. Validate dates, runner paths, generation manifest and strategy hashes once.
2. Create `fast_test_manifest.json` and clear only the configured report
   directory and exact tester strategy directory.
3. Split expected strategies in deterministic manifest order into chunks of at
   most `strategy_batch_size`.
4. For each chunk:
   1. stop the previous bot process, if it is still running;
   2. clear exact `<bot_root>\settings_strategy`;
   3. copy only this chunk's strategy JSON;
   4. start the bot and wait for its existing HTTP endpoint;
   5. launch a rolling window of no more than
      `max_parallel_submissions` unfinished strategies;
   6. refill one slot only after a strategy has a matching stable report or is
      terminally failed;
   7. retry a missing report or a row returned to TEST until the strategy has
      made `max_strategy_attempts` total attempts, including the first;
   8. record terminal failures, continue the rest of the chunk, then continue
      subsequent chunks;
   9. stop the bot before installing the next chunk.
5. Reconcile reports, write the terminal manifest atomically and set the exact
   final contents of `settings_strategy`.

The READY publisher keeps the existing `Output\\strategies` directory in place;
only its generated files are replaced after staging. This preserves directory
ACLs across regeneration while retaining file-level rollback on install errors.

The module directly clears and copies the two exact validated runtime
directories. It does not use `prepare_batch_files`, because that helper creates
backup/staging artifacts required by the old workflow.

## 7. Timeouts and report acceptance

- `result_report_grace_seconds`: time after RESULT, or after a launch that never
  enters RUNNING, before the same strategy is retried.
- `report_stability_polls * poll_interval_seconds`: minimum unchanged-file
  observation before accepting a report.
- `stall_timeout_seconds`: no useful progress in the current batch.
- `batch_timeout_seconds`: hard wall-clock limit for the current batch.

A report is accepted only when:

- it is a stable HTML file created for the current run;
- it contains exactly one embedded strategy settings object;
- embedded `name` matches an expected strategy name;
- for a manually supplied report during recovery, test period, symbol,
  timeframe and order parameters match the expected strategy/run.

If the tester HTML has no unambiguous start/end-period fields, Fast TEST must
not guess from first/last trade timestamps: that manual report remains rejected
and the strategy stays available for retry.

Full performance parsing is deliberately deferred. The stable report snapshot
captured during polling is authoritative; no post-run all-reports deduplication
pass is performed. Any unrelated HTML files are identified later by subtracting
the filenames referenced by `verified_reports` from the report directory.

## 8. Outcomes and folder state

The panel displays these outcome labels:

- `COMPLETED · 1000 / 1000 reports`
- `PARTIAL · 993 / 1000 reports · FAILED 7`
- `CANCELLED · <ready> / <total> reports · INCOMPLETE <n>`
- `FAILED · <safe cause>` for fatal configuration, path, copy, bot, HTTP or
  manifest errors.

`PanelJobRegistry` keeps its existing terminal states. A valid partial result is
stored as `state=COMMITTED`, `phase=PARTIAL`, with failed names in `evidence`;
the UI displays the phase/outcome, not the transport state.

Final `settings_strategy` contents are exact:

- COMPLETED: empty;
- PARTIAL: only terminally failed strategy JSON;
- CANCELLED: all strategies that do not yet have an accepted report;
- fatal FAILED before a trustworthy reconciliation: preserve the currently
  installed chunk and report the cause.

If the failed set is larger than `strategy_batch_size`, the bot remains stopped.

## 9. Recovery action

Add **Проверить / повторить FAILED**, enabled only for PARTIAL or CANCELLED Fast
TEST jobs.

Recovery:

1. rescans reports first;
2. accepts manually added reports only after the checks in section 7;
3. completes without starting the bot if no failures remain;
4. otherwise splits remaining failures into ordinary batch-sized chunks;
5. grants each still-failed strategy exactly one additional attempt for this
   button press;
6. updates the same Fast TEST manifest and leaves only remaining failures in
   `settings_strategy`.

The terminal PARTIAL/CANCELLED manifest in the report directory is sufficient
to reconstruct a recovery source after a panel restart; no in-memory job state
or `analysis_run_id` lookup is required beyond the signed generation manifest.

## 9.1 On-demand Performance DB handoff

Fast TEST completion still writes only reports and `fast_test_manifest.json`.
When the operator presses **Inbox → Performance DB → Проверить**, the panel:

1. reads the Fast manifest and uses only its `verified_reports` mapping;
2. loads the matching strategy JSON from the signed generation manifest;
3. creates the existing immutable inbox with `run_mode=FAST`;
4. validates that inbox through the existing Performance DB trust boundary;
5. marks the tester job committed with the inbox path, enabling the existing
   **Импортировать в Performance DB** button.

Unreferenced HTML files are not scanned or imported. Inbox capture uses a
bounded thread pool for file reads/writes; ordinary RUNS capture keeps its
single-worker default.

## 10. Panel contract

Add job kinds:

- `strategies.tester.fast.start`
- `strategies.tester.fast.retry`

The existing `strategies.tester.cancel` endpoint also cancels the active Fast
TEST owner. All ordinary tester, RUNS and Fast TEST starts share the existing
`strategies.tester` resource lock. While one owns the tester, all tester start
and generation controls are disabled and only **Стоп** is enabled.

Running status example:

`FAST TEST · batch 3 / 8 · готово 347 / 1000 · активно 30 · повторы 12`

The status payload exposes only bounded, panel-safe data: counts, phase,
current/total batches, failed names and a safe error code/message. Local paths
are not returned to the browser.

## 11. Invariants

- Old ordinary strategy and RUNS paths retain their current behavior.
- Fast TEST never calls `runner.workflow.run_batch`.
- Active launched-but-unverified strategies never exceed
  `max_parallel_submissions`.
- One strategy failure never prevents later strategies or batches from running.
- `max_strategy_attempts=4` means four total attempts, not four retries.
- Strategy JSON remains free of provenance and Fast TEST metadata.
- Only exact validated tester strategy/report directories may be cleared.
- Fast TEST does not create an inbox until the operator presses **Проверить**;
  that handoff accepts only the manifest's verified report mapping.
- Reports and strategies are not deleted as a consequence of Performance DB
  work until that separate workflow has committed and passed its audit.

## 12. Acceptance evidence

Automated evidence must prove:

1. a source larger than `strategy_batch_size` runs as multiple chunks and the
   tester strategy directory never contains two chunks at once;
2. active submissions never exceed `max_parallel_submissions`;
3. one missing report becomes FAILED after four total attempts while subsequent
   batches complete;
4. PARTIAL leaves exactly failed JSON in `settings_strategy`;
5. a matching manually added report is accepted by recovery and can produce
   COMPLETED without restarting the bot;
6. old `strategies.tester.start` and `strategies.tester.runs` tests still pass;
7. a completed Fast TEST creates a valid Performance DB inbox from only its
   verified report mapping, including after a panel restart;
8. a five-strategy real smoke completes, an intentionally missing report gives
   PARTIAL, and a run larger than one chunk visibly performs at least two bot
   starts.
