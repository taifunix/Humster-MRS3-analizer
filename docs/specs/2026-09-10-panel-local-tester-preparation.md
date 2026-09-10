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

## Non-goals

This change does not invoke the tester HTTP or wizard endpoint.  `Start`
continues to launch only the already-prepared local bot.

## Invariants

- Report deletion is opt-in and defaults to false.
- Preparation validation and the initial stop complete before report deletion.
- A failed initial stop leaves tester config, strategy files and reports
  untouched.
- The cleanup target remains the validated canonical report directory inside
  `bot_root`; it never deletes the directory itself or paths outside it.
- Panel responses remain redacted and do not disclose local paths.

## Acceptance evidence

- Focused service tests prove default preservation, opt-in cleanup and no
  cleanup when the initial stop fails.
- Static UI tests prove the checkbox, label, request field and button order.
- `tests/test_panel_testing.py` and `tests/test_panel_static_ui.py` pass.

## Context

This is a local Panel preparation slice.  It follows the boundary in
`2026-09-06-portfolio-optimizer-panel-ui.md` and ADR-0031: no tester runtime
or wizard execution is introduced by this feature.
