# ADR-0071: Use the nested strategy directory for local tester v2

**Status:** Accepted
**Date:** 2026-10-14

## Context

The current local tester keeps its runtime files below `tester/`. Its strategy
directory is `tester/settings_strategy`, while the Analyzer's runner
configuration and cleanup guard still targeted `bot_root/settings_strategy`.
The cleanup guard must remain restricted to a known directory inside
`bot_root`.

## Decision

- Default local tester configuration to `bot_root/tester/settings_strategy`.
- Accept only that exact directory and the exact legacy
  `bot_root/settings_strategy` directory for older tester installations.
- Validate the configured path again at the strategy cleanup boundary; do not
  permit a caller-supplied arbitrary directory.
- Leave the remote runner and tester HTTP contract unchanged.

## Consequences

- New local tester installs receive strategies in the folder the tester reads.
- Existing local configs remain usable when their tester still uses the legacy
  root-level folder.
- Path traversal, links outside `bot_root`, and broad cleanup targets remain
  rejected.
