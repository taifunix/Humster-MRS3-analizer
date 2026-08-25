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
  materializer Source DB selector and the two native merge datalists; validation
  still happens at surface or merge preflight.

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
`remote_runner`. It contains connection material plus canonical remote roots;
the Source DB card may display and submit the operator-selected remote HTML
folder and staging target; credentials remain server-only and the backend
validates both values against the configured roots. Remote commands are fixed,
argv-based operations and stop only a process proven to run the configured
`hb_c` binary.

Remote Source DB import is two-stage: a verified background Debian import from
the operator-selected folder below `remote_runner.reports_archive_root` into a
new staging target below `remote_runner.source_db_root`, followed by a
same-directory
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

Local and remote Source DB jobs persist the same safe importer evidence in the
panel journal: source-content digest, accepted/quarantined counts,
quarantine reasons, coverage-cell count when supplied, and
`safe_to_delete`. Paths, credentials and raw report lists are never persisted.
The local adapter passes the configured Source v6 throughput settings and
publishes truthful per-report progress. The static panel is the default root;
`/legacy` remains an explicit compatibility path.

The local merge card accepts two immutable Source v6 inputs and a fresh target.
Its three path defaults are persisted through the settings endpoint, catalog
candidates can be selected from either input field, and the merge job publishes
fragment progress while polling. A merge with quarantine is publishable only
when every quarantined `(fragment_id, source_name)` has an exact replacement in
the input set; unresolved quarantine fails closed.

The operator-only patch merge is not available from the panel. It publishes
only when every quarantine row has an exact replacement in the immutable input
set; otherwise it fails closed.

The Source DB screen reserves a separate `Manual merge` subsection below the
normal local merge. It describes the coverage-only patch merge but has no
settings or start control until that operator contract is specified.

Surface coverage preflight remains a read-only synchronous validation. While
the request is in flight, the panel shows an active phase bar and elapsed
time; on completion it shows 100%, the scoped count and elapsed time, then
opens the READY scope result. It never presents synthetic per-scope progress.

Surface publication is an asynchronous local job. Its status returns only the
phase, selected scope count, fragment work units and final filename: never a
local source or output path. The progress bar is determinate only for hydrated,
copied or readback-validated fragments; staging, checkpoint and atomic commit
are phase-only. A changed selection clears prior confirmation and blocks
publication until the user confirms it again. The publication directory is a
safe panel path default (`surface_target_path`) and may be saved explicitly.
Its filename defaults to `{pair...}_{start}_{end}.surface-v6.duckdb`, remains
editable, and never exposes the immutable `surface_id` hash; that identity
stays in the manifest. A selected surface derives an editable Analysis DB
filename in the saved `analysis_db_root`. An explicitly chosen colliding name
fails closed; an automatic analysis name uses a readable `-2`, `-3`, … suffix
for a distinct analysis identity.

The Strategies/DD5 surface selector scans the configured surface library (or
`data/surfaces` when it is unset) recursively and quickly verifies its
manifest/scope identity. Full payload verification remains the required first
step of the analysis operation, so opening the panel does not decode a large
surface library.

Fresh analysis starts with a visible indeterminate progress bar, the actual
current entry phase (`Reading and validating surface`) and elapsed time. The
control is disabled until the synchronous backend operation returns; terminal
status is either the committed candidate count or the returned error. No
percentage is shown because this contract does not expose a truthful one yet.

## Strategies/DD5 runner scope

The Strategies and DD5 screen uses the configured local `tester_runner` only.
Its immutable fresh READY JSON manifest is passed into the local runner with
complete v6 provenance; the resulting inbox is the sole input to Performance
DB and `CALCULATION_ONLY` DD5.  Remote strategy testing is intentionally not
shown or started in this version.  READY JSON creation is a panel-local
background job: the start request returns promptly, and the panel polls its
`RUNNING`, `COMMITTED`, or `FAILED` status so a dropped browser connection
cannot be displayed as a failed generation.  HTML cleanup is opt-in and permitted only
after a committed zero-quarantine import and verified DD5 export.

`Generate READY JSON` replaces the single operator batch in
`Output/strategies`: before publication the previous contents of that exact
directory are removed, and the new JSON files are written there.  Its
provenance manifest is validated from disk when the panel starts, so choosing a
tester period and starting the batch does not depend on the browser session
that generated the JSON.

The completed tester manifest records verified direct paths to original JSON
and tester HTML rather than duplicating payloads. Performance DB reads and
hashes those files directly; opted-in cleanup deletes original HTML only after
a zero-quarantine committed import.

The Performance DB selector has an explicit `Обновить список` control. It
re-reads the existing Performance DB catalog without restarting the panel.

An operator may run a separate explicit partial import for investigation: it
publishes only reports that passed parsing, records the rejected reports as
quarantine, marks the audit `PARTIAL_COMMITTED`, and never enables HTML cleanup.
The normal panel import remains zero-quarantine only.

Panel restart is rejected while either a tracked panel job or the live local
tester service has a non-terminal batch.  If a process nevertheless exits
after the runner has durably completed its inbox, startup may recover only
that validated completed inbox; a progress counter alone is never success.
When the runner had already verified every report and stopped only for inbox
capture, startup may finish that capture without resubmitting any strategy;
otherwise the interrupted batch remains failed and cannot unlock Performance
DB.  The Tester status explicitly reports this `RECOVERING_INBOX` phase; an
incomplete inbox without a manifest is replaced only after that verified
resume check succeeds.

The grouped shortlist exposes the analysis facts available for each
`Pair + Side + TF`: order buckets, the distinct persisted `plateau_id` count,
and the available report interval formatted as `DD.MM-DD.MM`.  It never
substitutes tester dates for that interval or presents source PnL as tested
strategy PnL.  Tester date controls remain in the Tester batch card.

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
