# ADR-0075: Reject links in tester strategy cleanup paths

**Status:** Accepted
**Date:** 2026-10-15

## Context

The tester replaces the contents of its configured strategy directory before
each batch. Resolving an allowed path before cleanup does not by itself prove
that the configured path used ordinary directories: an in-root link can alias
another directory and makes the cleanup boundary difficult to audit.

## Decision

- Reject strategy paths containing a symlink or Windows reparse point,
  including a junction, below `bot_root`.
- Keep the exact `bot_root/tester/settings_strategy` and legacy
  `bot_root/settings_strategy` allowlist.
- Derive the cleanup expectation independently from `bot_root` and those two
  fixed names.
- Inspect children without following links. Remove linked files with `unlink`
  and linked directories with `rmdir`; recurse only into entries whose
  non-following stat identifies an ordinary directory.

## Consequences

- A linked tester strategy path fails before strategy staging or cleanup.
- A symbolic link or junction inside an otherwise allowed strategy directory
  is removed without traversing its target.
