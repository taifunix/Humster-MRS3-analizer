# Stage 1 acceptance ledger

Date: 2026-08-20

| Plan requirement | Current evidence | Status |
| --- | --- | --- |
| Task 1 contract and ADR | ADR-0012/spec/PRD/progress; independent `CODE_REVIEW_PASS` | Pass |
| Task 2 compact lossless storage | focused codec/storage evidence; independent `CODE_REVIEW_PASS` | Pass |
| Task 3 bounded importer, lock and recovery | 155 focused tests; independent `CODE_REVIEW_PASS` | Pass |
| Task 4 merge, immutable inputs and writer serialization | merge tests; independent `CODE_REVIEW_PASS` | Pass |
| Task 5 real 684 Windows worker sweep | 684/684 cells, zero quarantine, equal digest, readback and audit in `task5-real-corpus-report.md` | Pass |
| Task 5 Debian bundled smoke | Debian Python 3.11 / DuckDB 1.5.5: 31/31 `COMMITTED`, zero quarantine, manifest written, reopen returned 31 fragments, raw digest unchanged (`47a9…2917`) | Pass |
| Task 6 partition merge/recovery on Windows | AB=BA=full, associativity, idempotence, sidecar immutability and stale recovery in `task6-merge-evidence-report.md` and `task6-recovery-overlap-report.md` | Pass |
| Task 6 old-owned cross-corpus seam | 684/684 real pairs, local PnL/DD/PF verification, 240 broader tests, independent `CODE_REVIEW_PASS` | Pass |
| Task 6 importer/merge kill and replacement on Debian | `task6-debian-recovery-report.md`: SIGKILL before/after publish, clean recovery/replacement digests equal, no staging, raw digest unchanged | Pass |
| Final `git diff --check`, full suite, final review and root gate | `git diff --check`; `.venv\\Scripts\\python.exe -m pytest -q`: 1206 passed, 2 skipped, 1 warning; compact-publication and gate-checker re-reviews: `CODE_REVIEW_PASS`; root gate accepted | Pass |

The required Debian bundled smoke and recovery/replacement evidence are
recorded above. No raw HTML, database, source code or test was modified to
create this ledger.
