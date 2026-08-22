# Static Control Panel v1

**Status:** Active implementation contract

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
- `GET /api/ui/bootstrap` returns only safe, validated config-derived defaults. It never launches a process, scans artifacts, or exposes SSH credentials.

## Bootstrap contract

```json
{"version":"panel-ui-v1","config":{"path":"..."},"defaults":{"runner":{"configured":true}},"sections":{"testing":{"configured":true}}}
```

Runner paths are present only after `RunnerConfig` validation. Invalid configuration returns a stable generic status code, never exception text or local paths.

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
- Existing legacy panel tests and API routes keep passing.
