# Static Control Panel v1 — approved implementation plan

**Status:** approved by independent advisor (2026-08-22); implementation in progress.

**Spec:** [panel-static-frontend-v1.md](../../specs/2026-08-22-panel-static-frontend-v1.md). The approved visual references are the five mockups linked from that specification.

## Boundaries

- The static panel is the new root; `/legacy` and every existing non-v2 API retain their current semantics.
- New work uses `/api/v2/*` only. It never exposes legacy CSV, `DUCKDB_DIRECT`, Artefacts, credentials, arbitrary remote commands, or an operational portfolio screen.
- Portfolio is a visible disabled placeholder. DD5 is always `CALCULATION_ONLY`; Source PnL is never presented as tested PnL.
- Mutable outputs require preflight, explicit confirmation, staging, verified readback, and atomic publication. Existing source artifacts stay immutable.
- Every task runs focused tests plus the legacy smoke gate: `.venv\Scripts\python.exe -m pytest tests/test_panel.py -q`.
- Every implementation slice is independently reviewed before the next shared handoff.

## Ownership and handoff

`panel.py`, `index.html`, and `app.js` are serial handoffs. `app.css` is reserved for Task 10 cross-screen styling; earlier tasks reuse its existing tokens/classes. New support modules are owned by the task that creates them. No task edits a shared file while its predecessor is unintegrated.

| Task | Deliverable | Primary files | Verification |
| --- | --- | --- | --- |
| 1 | Root switch, static shell, namespace | `panel.py`, `panel_web/index.html`, `tests/test_panel.py` | root static/legacy/malformed/missing cases; `/legacy` invariant; asset type/traversal/Host tests |
| 2 | Redacted bootstrap and safe Settings | `panel_settings.py`, `panel.py`, focused tests | validation, no secret leakage, atomic `.bak` save |
| 3 | Frozen v2 job contract and recovery | `panel_contracts.py`, `panel_jobs.py`, focused tests | locks, capacity, idempotency, cancellation, restart reconciliation |
| 4 | Local Testing | `panel_testing.py`, adapters/tests | runner/disk preflight, config snapshot, progress and cancel |
| 5 | Remote Testing | `panel_remote.py`, testing adapter/tests | backend-owned profile, argv-only transport, retries and clean abort |
| 6 | Local Source DB import and merge | `panel_source_db.py`, adapters/tests | preflight/staging/readback, new target, immutable sources, parallel safe jobs |
| 7 | Remote Source DB verified delivery | remote/source adapters/tests | remote readback, staged transfer, digest/size verification, atomic local publish |
| 8 | Surfaces | `panel_surfaces.py`, adapters/tests | coverage token, READY-only selection, immutable publish |
| 9 | Analysis through DD5 | `panel_provenance.py`, adapters/tests | complete artifact lineage and zero-quarantine Performance gate |
| 10 | Cross-screen CSS, accessibility, exclusions, release evidence | `app.css`, UI tests, `progress.md` | full tests, keyboard/mobile checks, `git diff --check` |

## Task 1 — root switch and static shell

- [x] `panel.default_root` is local-only and accepts `static` or `legacy`.
- [x] `static` serves a fixed allowlisted shell; `legacy`, missing, or malformed panel config serves unchanged `PANEL_HTML`; `/legacy` is always unchanged.
- [x] Fixed assets are only `/`, `/panel-web/app.css`, and `/panel-web/app.js`; traversal and unknown assets fail.
- [x] The shell has the approved navigation and a non-operational disabled Portfolio item; it excludes CSV, `DUCKDB_DIRECT`, Artefacts, connection fields, and portfolio actions.
- [x] Tests cover root choices, legacy invariance, content types, Host protection, a representative existing GET/POST route, exclusions, keyboard access, and `git diff --check`.

## Frozen job protocol — prerequisite for Tasks 4–9

Every job has version, UUID id, kind, idempotency key, resource keys, sanitized immutable manifest, UTC time, redacted bounded logs, status/progress, and generic error envelope. Local and remote use the same `submit`, `poll`, `log`, `list`, and `cancel` shapes. Resource collisions fail visibly; capacity exhaustion never creates invisible queued work.

Legal states are `PREFLIGHT_REQUIRED → PREFLIGHT_READY → QUEUED → RUNNING → TRANSFERRING/VERIFYING/PUBLISHING → COMMITTED`, with active-state cancellation through `CANCELLING → CANCELLED` and terminal `FAILED`. On restart, nonterminal jobs become `FAILED/INTERRUPTED` and release locks unless their explicit remote identity and staging integrity permit a documented resume. Partial artifacts never gain a UI link. Reusing the old idempotency key returns the reconciled terminal record; a retry uses a new key.

## Artifact gates

`committed Source DB → verified merged Source DB → current coverage token/READY witnesses → immutable surface → exact analysis identity → READY JSON batch → linked tester inbox → committed zero-quarantine Performance DB → DD5/final shortlist`.

Each artifact carries the upstream digest/identity and manifest reference. The server enforces every gate. Merge preserves source immutability, uses the existing canonical duplicate rule, and rejects schema drift before mutation. Remote work is profile-selected only: connection material stays backend-local, commands use allowlisted argv, retry/resume requires proven identity and integrity, and cancellation is cooperative/auditable.

## Per-screen quality gate

Every task that changes visible UI proves keyboard reach/activation, visible focus, accessible control/status names, and that Portfolio is visible but disabled with no handler or focus trap. It also proves the excluded capabilities remain absent. Task 10 performs the responsive cross-screen sweep and full repository regression.
