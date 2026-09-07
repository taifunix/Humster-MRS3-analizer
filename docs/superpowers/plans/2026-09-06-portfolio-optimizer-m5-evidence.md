# Portfolio Optimizer M5 evidence

schema: portfolio_optimizer_m5_evidence_v1
version: 2
date: 2026-09-07
source_baseline: 38fbe0f
status: ACCEPTED
review_disposition: CODE_REVIEW_PASS
executor: GPT-5.6 Luna (xhigh), integrated and verified by root
reviewer: Claude Opus 5 high; CODE_REVIEW_PASS in three rounds

## Boundary

M5 is fixture/fake-only. It does not launch the real tester or bot, contact a
remote target, write a real database, or grant `RECOMMENDATION_READY`, trading
admission, tester permission, or live use.

## Implemented contracts

| Contract | Evidence |
|---|---|
| Shared target owner | `src/mrs3/locking.py` provides one canonical local/remote target identity and cross-process owner record with PID, process-start, host, machine, boot/container, PID namespace and acquisition token. Live, foreign and unverifiable owners block; only a same-runtime proven-dead owner is reclaimed automatically. Manual clear requires a durable operator audit. |
| Existing runtime integration | Panel local fill/start/stop, Strategy Batch, Fast/SINGLE_MODE, RUNS, run-snapshot publication and the common CLI workflow use the same ownership primitive. Performance import retains shared tester sources. Remote mutation requires an injected ownership/attestation/snapshot/restore adapter and otherwise fails before I/O. |
| Portfolio adapter | `src/mrs3/portfolio/runner.py` defines a separate versioned portfolio transport contract, run/attempt workspace, exact manifest and strict capability, binary/settings/tick and report validation. It does not substitute legacy `SINGLE_MODE`. |
| Restore and cancellation | Local config and root strategy JSON are captured and restored while ownership is held. Remote settings use opaque adapter snapshots. Timeout/cancel waits for bounded confirmation; an unconfirmed operation or failed stop/restore/release retains the owner evidence. |
| Artifact ownership | Shared report and wizard directories are no longer cleared by the common workflow. RUNS filters against a pre-run report baseline. Failed/interrupted workspaces and Fast snapshots remain. Portfolio workspace deletion accepts only an exact typed M6 commit/readback proof. |
| Retry and recovery | Each portfolio retry has a distinct attempt identity, a completed run cannot execute twice, and incomplete manifests can restore settings after restart under the same target owner. |

## Verification

Focused ownership/runner/panel/common-runner verification:

`.venv\Scripts\python.exe -m pytest tests/test_locking.py tests/test_portfolio_runner.py tests/test_panel_testing.py tests/test_panel_remote_testing.py tests/test_panel_strategy_batch.py tests/test_panel_fast_strategy_test.py tests/test_panel_fresh_strategies.py tests/runner/test_files.py tests/runner/test_workflow.py tests/test_tester_run_files.py tests/test_single_mode_handoff.py -q`

The final expanded focused ownership run passed: `239 passed, 1 skipped`.
The R12-R14 regression subset then passed: `131 passed, 1 skipped`. The skip
is the existing Windows privilege limit for symlink creation.

The complete project suite on the final post-fix tree passed: `2938 passed, 7
skipped, 8 warnings` in `855.71s`. A preceding full run had one two-second HTTP
timeout (`2937 passed`); that test passed alone and its complete module passed
`60 passed, 2 skipped` before the unchanged full-tree rerun succeeded. Skips
are Windows symlink capability checks; warnings are existing
pandas/deprecation warnings outside M5.

All touched Python modules pass `.venv\Scripts\python.exe -m py_compile`.
`git diff --check` is run again after this evidence update.

## Review ledger

Opus round 1 returned R1-R10. R1-R9 were accepted and fixed: same-host liveness
probes cannot be bypassed by boot/container drift; Linux container and portable
namespace identities are stable; clean Fast failures release ownership;
Strategy Batch fails before mutation without a typed target; retained wizard
files require fresh stable observations; remote POSIX identities are quoted;
link failures are typed; and duplicate committed attempts fail closed. R10's
recovery order and terminal-state exclusion were confirmed by tests.

Opus round 2 accepted those dispositions and returned R11-R14. The final tree
adds the full-suite evidence above, writes restore evidence plus
`READY_TO_RELEASE` durably before releasing, never reapplies that snapshot after
the release boundary, canonicalizes equivalent textual remote host/path forms,
and parses only a stable private copy of a shared wizard result. DNS alias/IP
equivalence was rejected as unknowable without network resolution; the config's
validated host remains the trust boundary. Opus round 3 returned
`CODE_REVIEW_PASS` for the exact final tree. N1 (the existing two-second HTTP
test timeout) and N2 (temporary-name hygiene for stable reads) are non-blocking
M6 follow-ups.

## Handoff

M5 is accepted. The next action is fixture/fake-only M6. U1 has not started.
Real tester/bot execution still requires the later explicit gate in the active
plan.
