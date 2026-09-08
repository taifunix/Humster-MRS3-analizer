# Portfolio Optimizer Phase 2B-1 LiveStore evidence

**Status:** `ACCEPTED_FIXTURE_BOUNDARY`

The accepted slice provides a separate stdlib SQLite LiveStore with WAL,
foreign keys and recursive append-only triggers. It stores immutable
secret-free fixture manifests and versioned account, position, order,
execution, cashflow, reconciliation, checkpoint and watchdog facts.

Existing foreign SQLite files are probed read-only before any schema or sidecar
write. Performance/Portfolio aliases and configured canonical paths are
rejected before connection. Exact replay is idempotent; conflicting immutable
identity is explicit, and corrections append parent provenance.

## Verification and review

- `.venv\Scripts\python.exe -m pytest tests/test_portfolio_live_store.py -q`
  - `32 passed in 2.04s`.
- Store plus provisional monitor/chart regression tests - `70 passed in 2.60s`.
- `py_compile` and `git diff --check` passed.
- Claude Opus 5 high reviewed three complete versions. The final review
  confirmed all findings R1-R11 and returned `CODE_REVIEW_PASS`.

No real REST/WS, credentials, tester, notifications, trading, bot/config
mutation, Panel registration, production database or tested baseline write was
used. Reconcile, monitor/read models, order projection and charts remain later
fixture/fake slices.
