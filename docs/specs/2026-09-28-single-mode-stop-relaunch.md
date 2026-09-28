# SINGLE_MODE Stop → relaunch without Panel restart

## Goal

After an operator presses **Stop** for an ordinary `SINGLE_MODE` tester job,
the same Panel process must be able to finish safe tester-target cleanup and
launch another ordinary job without requiring a Panel restart.

## Root cause and scope

`LocalSingleModeStrategyTestService` owns the shared tester target through
`TesterTargetLock`. The lease intentionally contains the Panel PID. If bot-stop
confirmation or tester-settings restoration fails during cancellation, the
current worker publishes a terminal job while deliberately retaining the
lease. A later launch is therefore admitted by the service but rejected by the
still-live Panel PID in the lease.

This change is limited to the ordinary SINGLE_MODE service lifecycle and its
focused tests. It does not weaken `TesterTargetLock`, delete a live lock, change
RETEST behavior, or launch the real tester.

## Contract

- A job is not exposed as fully terminal/relaunchable while its tester target
  still needs cleanup or lease release.
- Cleanup-pending state retains enough in-process ownership context to retry
  bot stop, settings restoration and lease release safely.
- Cleanup retry is bounded and deterministic; failures remain fail-closed and
  report an actionable status without discarding the lease.
- A subsequent ordinary start first reconciles same-process cleanup-pending
  ownership. When reconciliation succeeds, the old job becomes terminal and
  the new job may acquire the target normally.
- Successful cancellation behavior remains unchanged.
- No code path unlinks or steals a lease owned by a live process.

## Non-goals

- Recovering an unconfirmed cleanup owned by a different live Panel process.
- Changing the shared tester-target lock format or PID validation.
- Starting the Panel or tester as acceptance evidence.
- Changing accumulation/report-collection membership semantics.

## Acceptance evidence

- A focused fake reproduces a cancellation cleanup failure while the same
  process retains the lease.
- The job is not falsely advertised as safely relaunchable during that state.
- A later same-process cleanup retry succeeds, releases the original lease,
  and a second `start()` acquires the target and proceeds.
- Repeated cleanup failure remains blocked without deleting the lease.
- Existing ordinary cancellation, retry, collection and tester tests pass.
- `git diff --check` passes and an independent reviewer returns
  `CODE_REVIEW_PASS`.
