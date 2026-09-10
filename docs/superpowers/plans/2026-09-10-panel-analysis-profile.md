# Panel Analysis Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide one Russian-labeled Panel profile that edits only configuration affecting fresh Source v6 analysis.

**Architecture:** A typed whitelist service projects, merges and validates the approved configuration through `AlgorithmConfig.from_json`. A dedicated local API serves that projection, and the static UI renders one main settings card with flat visual sections and Reload/Save actions.

**Tech Stack:** Python 3.11, stdlib JSON, existing `AlgorithmConfig`, static HTML/CSS/JavaScript, pytest.

**Spec:** `docs/superpowers/specs/2026-09-10-panel-analysis-profile-design.md`

## Global Constraints

- Labels and validation text are Russian and never reveal local paths or secrets.
- Only values proven to affect `run_multiscope_analysis`, plus shared `duckdb_import.workers`, are editable.
- Reload is read-only; Save validates the merged full configuration before one atomic write.
- Existing artifacts stay immutable; only future fresh analyses use the saved profile.
- Run project tests only through `.venv\\Scripts\\python.exe -m pytest`.

---

### Task 1: Typed profile service

**Files:**
- Create: `src/mrs3/analysis_profile.py`
- Test: `tests/test_analysis_profile.py`

**Interfaces:**
- `load_analysis_profile(config_path: Path) -> dict[str, object]`
- `validate_analysis_profile(config_path: Path, profile: Mapping[str, object]) -> dict[str, object]`
- `save_analysis_profile(config_path: Path, profile: Mapping[str, object]) -> dict[str, object]`
- The fixed schema is `eligibility`, `economics`, `geometry`, `plateau`, `ready`, `structures`, and `workers`.

- [ ] Write a failing test that creates valid config plus `remote_runner`, loads the profile, and asserts the economic PnL, BASE READY values and workers are projected while `remote_runner` is absent.
- [ ] Run `.venv\\Scripts\\python.exe -m pytest tests/test_analysis_profile.py::test_profile_projects_only_fresh_analysis_fields_and_keeps_unknown_config -q`; expect import failure because the module does not exist.
- [ ] Implement explicit extract/merge maps for: eligibility (`history_min_days`, rates, factors, floors, point events); economics; canonical shifts and MA radius; plateau/close thresholds; BASE and multi-order admission; gap rules/max orders/target DD; workers.
- [ ] Write a failing test that passes an unknown section and `base_min_points=1`, then expects whitelist rejection and `AlgorithmConfig` validation failure.
- [ ] Run the validation test; expect failure because validation is absent.
- [ ] Implement strict section/key validation. Merge only listed keys into the decoded document, validate the merged document with `AlgorithmConfig.from_json`, and validate workers with the existing DuckDB import settings loader.
- [ ] Write a failing save test changing only PnL. It must assert the changed config value is persisted, while `remote_runner` and import `transaction_batch_size` are preserved.
- [ ] Implement save with the repository's temp-file, fsync, replace and backup convention from `panel_settings.py`; no write occurs before validation succeeds.
- [ ] Run `.venv\\Scripts\\python.exe -m pytest tests/test_analysis_profile.py -q`; expect PASS.
- [ ] Commit `feat: add typed analysis profile settings`.

### Task 2: Dedicated Panel API

**Files:**
- Modify: `src/mrs3/panel.py`
- Test: `tests/test_panel_analysis_profile.py`

**Interfaces:**
- `PanelController.analysis_profile_get() -> dict[str, object]` returns `{"profile": ...}`.
- `PanelController.analysis_profile_save(payload: Mapping[str, object]) -> dict[str, object]` accepts `{"profile": ...}`.
- `GET` and `POST /api/v2/settings/analysis-profile` expose these operations.

- [ ] Write a failing HTTP test: GET returns a profile, changing its PnL and POSTing it returns normalized PnL `"7"`.
- [ ] Run that test; expect 404 because the route is absent.
- [ ] Add controller methods delegating only to Task 1 service and handler routes for GET/POST.
- [ ] Write a failing HTTP test that POSTs an unlisted section and expects status 400 with fixed safe Russian text, not a path or raw exception.
- [ ] Map only profile-service validation errors to fixed safe Russian messages; leave generic settings routes unchanged.
- [ ] Run `.venv\\Scripts\\python.exe -m pytest tests/test_panel_analysis_profile.py -q`; expect PASS.
- [ ] Commit `feat: expose analysis profile in panel API`.

### Task 3: One main static profile card

**Files:**
- Modify: `src/mrs3/panel_web/index.html`
- Modify: `src/mrs3/panel_web/app.js`
- Modify: `src/mrs3/panel_web/app.css`
- Test: `tests/test_panel_static_ui.py`

**Interfaces:**
- `renderAnalysisProfile(profile)` fills the form from the dedicated GET API.
- `analysisProfilePayload()` emits exactly `{profile: ...}`.
- `#analysis-profile-reload` and `#analysis-profile-save` trigger GET and POST respectively.

- [ ] Write a failing static test requiring `#analysis-profile-card`, Reload/Save buttons, the Russian labels `Минимальный PnL, %` and `Минимум точек в плато для BASE 1ORD`, and no nested `details` in that card.
- [ ] Run the static test; expect failure because the card is absent.
- [ ] Implement one `<details>` card in Settings. Use flat headings and field grids for every Task 1 group, repeatable row inputs for rates/factors/gaps, explicit units and help text. Remove batch size and unrelated legacy profile controls.
- [ ] Write a failing static test requiring `renderAnalysisProfile`, `analysisProfilePayload`, both button IDs and `/api/v2/settings/analysis-profile` in JavaScript.
- [ ] Run the wiring test; expect failure because the functions are absent.
- [ ] Implement GET rendering, read-only Reload and validated POST Save. Render returned normalized profile and fixed error text in the card's ARIA-live status. Render workers as shared parallelism for import/publication/analysis.
- [ ] Run `.venv\\Scripts\\python.exe -m pytest tests/test_panel_static_ui.py -q`; report only Node-dependent failures if Node is unavailable.
- [ ] Run `node --check src/mrs3/panel_web/app.js` when Node exists.
- [ ] Commit `feat: add panel analysis profile editor`.

### Task 4: Regression and operational evidence

**Files:**
- Modify: `README.md`
- Modify: `progress.md`
- Test: `tests/test_analysis_profile.py`
- Test: `tests/test_panel_analysis_profile.py`
- Test: `tests/test_panel_static_ui.py`

- [ ] Add a test that saves PnL `7` through the profile and asserts `AlgorithmConfig.from_json(config).economic_min_pnl_pct == Decimal("7")` for the next load.
- [ ] Run that test; expect PASS after Tasks 1–3 and repair the merge if it fails.
- [ ] Document that the profile affects future fresh analysis only, Reload does not write, Save is atomic, and workers are shared.
- [ ] Run `.venv\\Scripts\\python.exe -m pytest tests/test_analysis_profile.py tests/test_panel_analysis_profile.py tests/test_panel_static_ui.py -q`, `.venv\\Scripts\\python.exe -m compileall -q src`, and `git diff --check`.
- [ ] Restart the local Panel; reload, save an unchanged profile, then run fresh analysis from the existing published surface. Confirm `COMMITTED` without changing private inputs or thresholds.
- [ ] Update `progress.md`, independently review the diff, and commit `docs: document panel analysis profile`.

## Plan Self-Review

- Tasks 1–2 cover typed whitelist projection, validation, atomic persistence and safe API errors.
- Task 3 covers one Russian-labeled non-nested card and its dedicated actions.
- Task 4 covers the fresh-config regression, operator instructions, live smoke and review evidence.
- No task exposes paths, secrets or unlisted configuration values.

## Execution evidence (2026-09-10)

- Tasks 1--3 implemented in one scoped change: typed whitelist projection,
  local GET/POST API, and one flat Russian-labeled Profile card.
- Focused verification passes: `8 passed` for profile service, Panel API and
  static profile contracts; `compileall` and `git diff --check` pass.
- Full static suite reaches `94 passed`; its two remaining failures require an
  unavailable local `node` executable and exercise unrelated Portfolio helpers.
- Independent review: `CODE_REVIEW_PASS` after three review rounds. The final
  corrections align UI/API keys, reject malformed shift tokens, and reject
  empty or fractional integer fields before a save.
