# Local tester v2 strategy directory

**Status:** Implemented and verified locally
**Date:** 2026-10-14
**Decision:** [ADR-0071](../decisions/0071-local-tester-v2-strategy-directory.md)
**Cleanup safety:** [ADR-0075](../decisions/0075-tester-strategy-path-cleanup-safety.md)
**Related contract:** [SINGLE_MODE report collection](2026-09-28-single-mode-report-collection.md)

## Goal

Run local SINGLE_MODE strategy batches with the current tester layout, where the
tester reads strategy JSON files from `tester/settings_strategy` beneath
`tester_runner.bot_root`.

## Non-goals

- Change the tester HTTP endpoints, date semantics, report format, inbox, or
  PerformanceDB import.
- Change the remote runner's directory layout.
- Change the ordinary SINGLE_MODE job journal or collection behavior.

## Contract

- The default local strategy directory is `tester/settings_strategy`, relative
  to `bot_root`.
- The runner accepts only that exact path or the exact legacy
  `settings_strategy` path beneath `bot_root`; all other paths remain rejected.
- Reject any symlink or junction in the configured strategy path below
  `bot_root`. Strategy cleanup compares the validated path with an independently
  derived allowed path and never descends through a symlink child.
- Strategy staging and cleanup target the validated configured directory only.
- Tester reports remain under `tester/report/my_test`.

## Acceptance evidence

- Focused runner-config, config-default, and SINGLE_MODE staging checks pass
  (10 passed; 183 deselected).
- The seven related runner/config/Panel/template test modules pass 467 tests;
  one real directory-symlink test skips because this Windows account cannot
  create symlinks. Portable symlink and reparse-point tests pass, as do real
  Windows junction tests for both path validation and cleanup.
- A live Panel `SINGLE_MODE` run completed for the 20-strategy manifest over
  2026-09-13 through 2026-10-13: `COMMITTED`, 20 reports verified, zero
  failures. Report import was not requested.
