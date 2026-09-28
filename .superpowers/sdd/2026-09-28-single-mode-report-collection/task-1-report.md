# Task 1 report: versioned heterogeneous collection inbox

## Implementation

- Added `build_single_mode_collection_inbox(...)` in
  `src/mrs3/performance_v2_collection.py`.
- The builder validates each explicit member through the existing v2 input
  trust boundary, requires ordinary `SINGLE_MODE`, verifies current report
  hashes before publication, rejects duplicate member paths, strategy names,
  report basenames and unsafe collection IDs, and publishes one metadata-only
  manifest through a staging-directory rename.
- The collection manifest is schema version 2 with explicit
  `collection_manifest_version=1` and `run_mode=SINGLE_MODE_COLLECTION`.
  Entries retain source paths/hashes, member snapshot identity, candidate
  diagnostics, analysis run ID, test range, tester config hash and commission
  contract.
- Extended `PreparedV2Entry` with per-entry batch context. Schema-version-1
  manifests continue to use their existing shared context and remain readable.
- Updated report validation to use the entry range and publication to use the
  entry commission contract, while retaining fallback behavior for legacy
  manually-constructed prepared entries.

## Files

- `src/mrs3/performance_v2_collection.py`
- `src/mrs3/performance_v2_input.py`
- `src/mrs3/performance_v2_import.py`
- `tests/test_performance_v2_collection.py`
- `docs/specs/2026-09-28-single-mode-report-collection.md`
- `docs/superpowers/plans/2026-09-28-single-mode-report-collection.md`

## RED/GREEN commands and output

RED (before production implementation):

```text
D:\Humster-MRS3-analizer-copy\.venv\Scripts\python.exe -m pytest tests/test_performance_v2_collection.py --basetemp=.task-collection-red -q
ERROR collecting tests/test_performance_v2_collection.py
ModuleNotFoundError: No module named 'mrs3.performance_v2_collection'
```

The first sandboxed pytest cleanup also hit the documented Windows
`WinError 5`; subsequent pytest runs used the approved escalation and the
same local `.venv` interpreter.

Focused GREEN:

```text
D:\Humster-MRS3-analizer-copy\.venv\Scripts\python.exe -m pytest tests/test_performance_v2_collection.py --basetemp=.task-collection-green4 -q
6 passed in 1.99s
```

Required affected suites:

```text
D:\Humster-MRS3-analizer-copy\.venv\Scripts\python.exe -m pytest tests/test_performance_v2_collection.py tests/test_performance_v2_input.py tests/test_performance_v2_import.py --basetemp=.task-affected -q
125 passed, 1 skipped in 68.85s (0:01:08)
```

The single skip is the existing unavailable-symlink test environment case in
`tests/test_performance_v2_input.py`.

## Affected suites

- `tests/test_performance_v2_collection.py` (new): heterogeneous context,
  per-entry range/commission, deterministic digest, explicit membership and
  duplicate/changed-artifact rejection.
- `tests/test_performance_v2_input.py`: full suite passed with the reader
  extension.
- `tests/test_performance_v2_import.py`: full suite passed with per-entry
  range/commission handling.

## Self-review

- Collection membership is taken only from the ordered `member_inboxes`
  argument; no report-directory glob or candidate discovery is used.
- The collection has no copied HTML or strategy payloads; the reader and
  staging path re-check trusted containment and hashes.
- Existing schema-version-1 tests remain green, including ordinary
  SINGLE_MODE filename handling and staging behavior.
- Duplicate reports are checked case-insensitively before publication, and
  the reader/stager retain their own duplicate basename guard.
- The importer still uses one existing parser staging and one transaction;
  no schema migration or alternate importer was added.
- `git diff --check` passed before commit preparation.

## Concerns

- A collection's report hash is checked when the collection is built and
  again during normal v2 staging; a report changed after verification fails
  in staging before database publication, preserving the existing fail-closed
  behavior.
- No real tester, Panel, or production database was started or mutated.
