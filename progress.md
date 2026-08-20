# MRS3 — current verification

**Updated:** 2026-08-20
**Current branch:** `main`
**Current feature:** Source v6 fresh compact multi-scope — implementation complete; awaiting manual verification.

STAGE_1_GATE=ACCEPTED_BY_ROOT; date=2026-08-20; reviewer=CODE_REVIEW_PASS compact-publication and gate-checker final re-reviews; evidence=.codex/stage1-acceptance-ledger.md,.codex/task5-real-corpus-report.md,.codex/task6-merge-evidence-report.md,.codex/task6-recovery-overlap-report.md,.codex/task6-debian-recovery-report.md

## Verified implementation

- Fresh compact Source → multi-scope surface → separate analysis pipeline is complete.
- Panel supports multiple READY scopes; the analysis worker limit is
  `duckdb_import.workers` and `gap_rules` is part of analysis identity and
  selection.
- Independent review: `CODE_REVIEW_PASS`.
- Latest full local verification: `1206 passed, 2 skipped, 1 warning` via
  `.venv\Scripts\python.exe -m pytest -q`.

## Next: manual verification

1. In the panel, import the intended raw HTML set and select one or more READY
   `symbol|side|timeframe` scopes.
2. Confirm a new `.surface-v6.duckdb` appears under
   `Output/surfaces-v6-compact/`, then run analysis with the intended listing
   dates and configuration.
3. Confirm the `.analysis-v6.duckdb` appears under
   `Output/analysis-v6-compact/`; repeat after changing `gap_rules` and verify
   that it produces a distinct analysis artifact and expected structure result.
4. Before syncing Git, review the scoped diffs/commits; local `Input/`,
   `Output/` and `Data/` must remain untracked.
