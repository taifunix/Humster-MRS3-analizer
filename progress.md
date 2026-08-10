# Progress

**Updated:** 2026-08-10

## Current feature

[v0.7 legacy selection](docs/specs/2026-08-10-v07-legacy-selection.md)

## Last verified state

- Root v0.7 package/test migration committed as `e58a4fc`.
- `.venv` created and `python -m pytest -q` completed successfully with `162 passed` during the elevated setup run.
- The local tester configuration is present and ignored by Git.

## Next action

Obtain and verify v4 import evidence: `schema_info.schema_version`, `import_manifest.json`, quarantine count, and `html_delete_checklist.csv`. Do not implement the materializer or delete HTML before this evidence exists.

## Blockers

- No v4 import result, manifest, or DuckDB file is present in this repository.
- The intended GitHub remote is configured locally but has not been created or pushed by this session.
