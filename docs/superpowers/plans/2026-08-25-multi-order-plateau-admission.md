# Multi-order plateau admission implementation plan

> Execute inline in worktree `feat/multi-order-plateau-admission` after the
> user's approval on 2026-08-25.

1. Add the two configuration values, validation and example defaults.
   Verify their parsing with a focused test.
2. Add a single admission guard in `build_structures` before candidate
   combinations. Reuse the frozen plateau diagnostics and keep legacy proxy
   behaviour unchanged. Verify pass, reject and missing-diagnostic cases.
3. Verify that changing the setting changes fresh-analysis identity, then run
   focused selection/config/fresh-analysis tests.
4. Update PRD and progress, inspect the diff, obtain independent review, then
   create one scoped feature commit on this branch.
