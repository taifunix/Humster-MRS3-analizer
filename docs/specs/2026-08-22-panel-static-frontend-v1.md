# Static Control Panel v1

**Status:** Implemented

## Approved UI evidence

- Testing: `.superpowers/brainstorm/515-1787384829/content/testing-screen-paths-final.html`
- Source DB: `.superpowers/brainstorm/515-1787384829/content/source-db-screen.html`
- Surfaces: `.superpowers/brainstorm/515-1787384829/content/surfaces-screen-full-restored.html`
- Strategies and DD5: `.superpowers/brainstorm/515-1787384829/content/strategies-dd5-screen-full-restored.html`
- Settings: `.superpowers/brainstorm/515-1787384829/content/settings-screen.html`

Portfolio remains a disabled placeholder. The Artefacts tab is explicitly excluded.

## Goal

Replace the default monolithic embedded panel with a static, local-only frontend while preserving the existing controller and safety rules.

## Scope

- Default `/` serves external `panel_web/index.html`, `app.css`, and `app.js`.
- Navigation: Testing, Source DB, Surfaces, Strategies and DD5, disabled Portfolio, Settings.
- Legacy embedded panel remains at `/legacy` during migration; legacy CSV and `DUCKDB_DIRECT` are not linked from the new UI.
- `GET /api/v2/bootstrap` returns only safe, validated config-derived defaults. It never launches a process, scans artifacts, or exposes SSH credentials.
- `GET /api/v2/source/local/catalog` lists only non-symlink `*.duckdb` files in
  the configured local Source DB directory. It is used solely to populate the
  materializer Source DB selector; validation still happens at surface
  preflight.

## Bootstrap contract

```json
{"version":"panel-ui-v2","defaults":{"panel":{"default_root":"static","path_defaults":{}},"runner":{"configured":true},"remote":{"configured":true}},"capabilities":{"settings":true,"portfolio":false}}
```

Runner paths are present only after `RunnerConfig` validation. Invalid configuration returns a stable generic status code, never exception text or local paths.

## Testing templates

The local testing flow uses the canonical repository templates
`Input/config_tester_long_standart.json`,
`Input/config_tester_short_standart.json`, `Input/Bybit_long.json`, and
`Input/Bybit_short.json`. The selected direction chooses both templates.

The rendered tester configuration changes only `StartDate`, `EndDate`, and
the one `settings[*].basic.symbol` mining value list. Its JSONC-style trailing
commas are accepted; all other template fields remain intact. The rendered
strategy changes its base symbol and `basic.use_long`/`basic.use_short` only.
Before a tester starts, the runner's existing transactional preparation puts
exactly that one rendered strategy JSON in `settings_strategy`; it never uses
the Input template in place and never mixes sides.

`POST /api/v2/testing/local/fill` performs that installation and writes the
rendered `config_tester.json`; it preserves existing reports. Start and stop
are separate explicit actions. The browser receives only the selected side,
symbols and strategy name, never local absolute paths.

The remote profile lives only in ignored `config.local.json` under
`remote_runner`. It contains connection material plus canonical remote paths;
only redacted configuration status and opaque path-check results reach the
browser. Remote commands are fixed, argv-based operations and stop only a
process proven to run the configured `hb_c` binary.

Remote Source DB import is two-stage: a verified background Debian import into
a new target below `remote_runner.source_db_root`, followed by a same-directory
temporary download to a fresh local target. Size and SHA-256 must match before
the local hard-link publication; either existing target is rejected and no
partial local database is shown as an artifact. The only remote importer command
is the configured runner's `scripts/import-source-v6-debian.sh HTML_DIR DB`.

The remote import status is observable without exposing paths or credentials.
Stage 1 reports completed and total preflight HTML reports, worker count and
elapsed time; the client derives percentage, rate and ETA only when the total
is non-zero. Stage 2 reports the size of the same-directory temporary local
download against the verified remote byte count, then reports SHA-256
verification before atomic publication. The panel shows both stage time and
overall elapsed time. Missing or malformed progress data is displayed as
indeterminate progress and never changes the import result.

## Strategies/DD5 runner scope

The Strategies and DD5 screen uses the configured local `tester_runner` only.
Its immutable fresh READY JSON manifest is passed into the local runner with
complete v6 provenance; the resulting inbox is the sole input to Performance
DB and `CALCULATION_ONLY` DD5.  Remote strategy testing is intentionally not
shown or started in this version.  HTML cleanup is opt-in and permitted only
after a committed zero-quarantine import and verified DD5 export.

## Invariants

- Server remains loopback-only and retains Host validation.
- Static files are fixed allowlist resources; traversal fails.
- Existing source-v6, tester, provenance, quarantine and DD5 safeguards remain backend-authoritative.
- Source PnL is never presented as tested strategy PnL; DD5 remains `CALCULATION_ONLY`.
- Portfolio is a disabled placeholder until its input contract and backend exist.

## Acceptance

- Root serves the new shell and `/legacy` serves the unchanged `PANEL_HTML`.
- Static CSS/JS load with correct content types.
- Bootstrap is safe for missing/malformed config and exposes no credentials.
- Local tester filling leaves exactly one rendered strategy JSON and preserves
  previous reports; start/stop refuse an invalid runner preflight.
- Remote status, path checks and lifecycle controls expose neither passwords,
  hosts, users nor remote paths; remote stop fails closed when its executable
  cannot be verified.
- Remote Source DB targets are fresh on both hosts and are published locally
  only after exact size/SHA-256 verification.
- Existing legacy panel tests and API routes keep passing.
