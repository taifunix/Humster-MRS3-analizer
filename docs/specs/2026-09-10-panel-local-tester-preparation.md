# Local tester preparation from Panel

**Status:** Implemented / verified

## Goal

Make the existing local Panel action prepare the tester files predictably and,
when explicitly requested, remove stale tester reports before the later manual
start action.

## Scope

The local runner card exposes an unchecked `Delete old reports before start`
checkbox above its action row.  Its action row is ordered `Check runner and
disk`, `Prepare files`, `Start`, `Stop`.

`Prepare files` keeps the established contract: it renders the selected
MRS2 tester config with the submitted dates, symbols and configured parallel
submission count, then installs exactly one rendered strategy.  When the
checkbox is true, it also empties the configured `tester/report/my_test`
directory after the tester has been stopped and the target lock is held.
The report directory itself is retained.  Report cleanup rejects symbolic links
and unsupported entries rather than following or deleting through them.

`Start` launches the prepared local bot, waits
`tester_runner.request_timeout_seconds` (default 10), then invokes the
Files-tab action `POST /htmx/tester/run` through the configured local endpoint.
It does not invoke the Table-tab per-strategy wizard action.  A rejected HTTP
request stops the just-started bot but retains the preparation lock and files
for a retry.

## Non-goals

This change does not invoke the Table-tab wizard endpoint or submit individual
strategies.

## Invariants

- Report deletion is opt-in and defaults to false.
- Preparation validation and the initial stop complete before report deletion.
- A failed initial stop leaves tester config, strategy files and reports
  untouched.
- The cleanup target remains the validated canonical report directory inside
  `bot_root`; it never deletes the directory itself or paths outside it.
- Panel responses remain redacted and do not disclose local paths.
- Tester status returned to Panel is restricted to an allowlist; unrecognised
  endpoint text becomes `UNKNOWN`.
- `POST /htmx/tester/run` is sent only after the configured request timeout.

## Acceptance evidence

- Focused service tests prove default preservation, opt-in cleanup, no cleanup
  when the initial stop fails, the warm-up delay, the Files-tab route and
  rollback on an HTTP failure.
- Static UI tests prove the checkbox, label, request field and button order.
- `tests/test_panel_testing.py` and `tests/test_panel_static_ui.py` pass.

## Context

This is a local Panel preparation slice.  It follows the boundary in
`2026-09-06-portfolio-optimizer-panel-ui.md` and ADR-0031: no tester runtime
or wizard execution is introduced by this feature.
