# Stage 1 Task 6 — Debian recovery/replacement evidence

Date: 2026-08-20

The supplied Debian runner used Python 3.11 and DuckDB 1.5.5. All generated
artifacts were placed in an isolated directory beneath the runner target; raw
HTML was read only.

## Bundled smoke

- 31/31 HTML reports: `COMMITTED`.
- Quarantine: `0`.
- Source digest: `4748d23395707a7a4802e7fda5be16140c847373824e895adf0884ee0cda590a`.
- Published DB reopened and returned 31 fragments.
- Handoff manifest was written and matched the import result.
- Raw HTML content-manifest SHA-256 before and after was unchanged:
  `47a9e7479535ddf9e2c3eb6454896b611bda951393730d575adb89557752917f`.

## Forced termination and replacement

Each importer/merge child was terminated with `SIGKILL` at the specified
in-process publication hook. The replacement completed under the same runner
and no `*.staging*` file remained.

| Operation | Before publish: recovered digest | After publish: published/replacement digest |
| --- | --- | --- |
| Single-report import | `ea4b42e596ad1fc262d5f1556bb16d87a644f749c58d731326adfe0367148ccd` | same |
| Two-input merge | `17f3f9c6da5adca4c567136ba8d30862dc224c4585d239e70dc28b827c6cd9c1` | same |

The raw-input harness digest was identical before and after:
`4dd683f0992764c67a185da47600fa4188cf0a3cfe45fb427ebd6e4770ad8dc0`.

The bundled smoke initially exposed a Python 3.11 dataclass-default error in
`DirectPreflight`; `witnesses` now uses an immutable per-instance
`default_factory`. The fix has a focused red/green regression test and an
independent `CODE_REVIEW_PASS`; the repeated full Debian smoke passed after it.
