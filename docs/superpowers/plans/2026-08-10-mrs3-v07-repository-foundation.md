# MRS3 v0.7 Repository Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the root v0.7 repository, migrate the current Python baseline, and create the operational documentation system.

**Architecture:** The Python package and tests move from the v0.6 subdirectory into conventional root locations without behavior changes. Active project knowledge lives in the three root documents and feature specs; historical source material is archived and linked instead of duplicated.

**Tech Stack:** Python 3.11+, pytest, pandas/openpyxl/lxml/httpx/psutil, DuckDB import scripts, Git.

## Global Constraints

- Treat `programs/MRS3_v0.6` as source material for v0.7, not as a maintained release directory.
- Tester root on this machine is stored only in ignored local configuration derived from a tracked example.
- Never commit raw HTML, DuckDB databases, generated reports, credentials, or machine-specific configuration.
- Each commit requires focused verification and review by a separate agent before commit.
- The intended remote is `github.com:taifunix/Humster-MRS3-analizer.git`; do not create or push it without authenticated GitHub authority.

---

### Task 1: Initialize safe repository metadata and migrate the Python baseline

**Files:**
- Create: `.gitignore`, `pyproject.toml`, `config.example.json`, `config.local.json.example`
- Move: `programs/MRS3_v0.6/src/mrs3` to `src/mrs3`
- Move: `programs/MRS3_v0.6/tests` to `tests`
- Move: `programs/MRS3_v0.6/start_panel.bat` to `scripts/start_panel.bat`

**Interfaces:**
- Consumes: existing `mrs3` package and tests.
- Produces: root-level installable package and a local configuration template pointing to the fixed tester root.

- [ ] **Step 1: Create Git metadata and ignore rules.**
- [ ] **Step 2: Move package, tests, project metadata, and launch script without changing production behavior.**
- [ ] **Step 3: Replace the tracked tester root in `config.example.json` with a portable placeholder and create ignored `config.local.json` containing the machine's actual tester root.**
- [ ] **Step 4: Run `python -m pytest -q`; record dependency or test outcome.**
- [ ] **Step 5: Have a separate agent review the staged migration diff, resolve findings, and run `git diff --check`.**
- [ ] **Step 6: Commit only migration/setup files as `chore: establish v0.7 repository foundation`.**

### Task 2: Establish the operational documentation system

**Files:**
- Create: `AGENTS.md`, `PRD.md`, `progress.md`, `README.md`, `docs/decisions/0001-repository-and-documentation-model.md`
- Move: v0.6 source documents to `docs/archive/v0.6/`
- Keep: active v0.7 specifications in `docs/specs/`

**Interfaces:**
- Consumes: repository-foundation design and verified migration state.
- Produces: a minimal new-session reading protocol, PRD feature registry, operational progress record, public README, and durable decision record.

- [ ] **Step 1: Write root documents with cross-links and non-duplicating responsibilities.**
- [ ] **Step 2: Move historical documents into the archive and add an index describing their non-authoritative status.**
- [ ] **Step 3: Add active v0.7 specs to the PRD registry with status and dependency links.**
- [ ] **Step 4: Verify all Markdown links and ensure no local tester path or private machine data appears in README.**
- [ ] **Step 5: Have a separate agent review the documentation diff, resolve findings, and run `git diff --check`.**
- [ ] **Step 6: Commit only documentation files as `docs: establish v0.7 project operating model`.**

### Task 3: Record the next product feature boundary

**Files:**
- Create: `docs/specs/2026-08-10-v07-legacy-selection.md`
- Modify: `PRD.md`, `progress.md`

**Interfaces:**
- Consumes: master v0.7 handoff and event-filter specification.
- Produces: the single active implementation specification for the legacy materializer/input/selector work, with explicit prerequisite evidence.

- [ ] **Step 1: Write the focused v0.7 legacy-selection specification, separating import validation, materialization, and selector upgrades into ordered deliverables.**
- [ ] **Step 2: Link it from PRD as active and set `progress.md` to its first evidence gate: v4 import manifest/schema/quarantine verification.**
- [ ] **Step 3: Have a separate agent review the documentation diff, resolve findings, and run `git diff --check`.**
- [ ] **Step 4: Commit only this specification and its registry/progress updates as `docs: define v0.7 legacy selection delivery`.**
