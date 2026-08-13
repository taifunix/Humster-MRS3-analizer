# Handoff: tester runner audit, 2026-08-14

## Verified state

- Immutable strategy source: `data/tester_batches/ONUSDT_LONG_all_tf_6a65684feaf1/strategies`.
- Source contains exactly `1073` strategies and matches the interrupted state by name and hash.
- Resume evidence contains `803` validated results; `270` strategies remain.
- The saved-result ledger includes path, size, mtime and SHA-256 for each report.
- Local runner settings: batch size `250`, parallel submissions `35`, attempts per strategy `4`, bot restarts `30`, submission delay `0.2s`, report grace `15s`.
- No real tester run was launched during the audit. No HTML or DuckDB file was deleted.

Verification command:

```powershell
.\.venv\Scripts\python.exe -m mrs3.cli tester-plan --config config.local.json --strategies data\tester_batches\ONUSDT_LONG_all_tf_6a65684feaf1\strategies --output-csv results\mrs3_long_results.csv
```

Expected summary: `expected=1073`, `ready=803`, `remaining=270`.

## Implemented fixes

- Resume accepts only output-scoped persisted result evidence; a stale current wizard result cannot complete another strategy.
- Legacy HTML-only evidence was migrated once into an explicit audited ledger. Normal resume no longer synthesizes results from HTML alone.
- Attempt counters survive bot restarts and runner invocations; four attempts remain a run-wide limit.
- Successful batches clear collision-forced retry state instead of serializing later unrelated batches.
- Report collector lifecycle, immutable snapshots, stale row detection and retry snapshots were hardened.
- Bot shutdown falls back to verified terminate/kill even when HTTP client creation or close fails.
- Root JSON staging rolls back on interruption.
- The in-place HTML renaming/archive workaround was removed. Existing `*.html.saved` files remain historical evidence and are not modified.

## Verification evidence

```text
151 passed, 1 skipped, 2 deselected in 8.37s
```

The skipped test needs Windows symlink privilege. The two deselected legacy tests require the absent, untracked ADMSTOCK HTML fixture.

## Next session

1. Read `AGENTS.md`, `PRD.md`, `progress.md`, this handoff and `docs/specs/2026-08-10-v06-runner-safe-root-json-smoke.md`.
2. Check that no tester-run or bot process from an old run is active and that the runner lock is absent.
3. Start or refresh the panel, open Test plan, and run **Check plan** only.
4. Require the panel to show total `1073`, ready `803`, prepared `270` before starting tests.
5. Start the remaining tests only after user confirmation. Watch submitted, in work, results, checked, retries and restart reasons.
6. After completion, verify all `1073` by exact strategy name and result evidence before DD5/post-test.

## Residual work

- Run an independent re-review of the final attempt persistence, forced-retry reset and SHA evidence patches; the previous review identified these issues before they were fixed.
- Decide cleanup semantics for an all-results-reusable run. Reports are intentionally retained for now.
- Current state/progress files can still display the interrupted `MONITORING` counters until the next plan/run publishes fresh state; the authoritative read-only plan is `803/270`.
- Do not restore the report-renaming workaround unless a new reproducible collision proves it necessary.
