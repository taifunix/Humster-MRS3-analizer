# Humster MRS3 Analyzer

Deterministic tooling for selecting, generating, testing, and auditing MRS3 mean-reversion strategy candidates.

## Status

The repository is being upgraded to v0.7. The current code is a migrated v0.6 baseline; v0.7 behavior is delivered through the active specifications in [PRD.md](PRD.md).

## Setup

Requires Python 3.11 or newer.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config.local.json.example config.local.json
```

Set `tester_runner.bot_root` in `config.local.json` to your local Hamster Bot Tester directory. This file is intentionally ignored by Git.

## Commands

```powershell
# Run automated tests
.\.venv\Scripts\python.exe -m pytest -q

# Start the local control panel
.\scripts\start_panel.bat

# Show CLI commands
.\.venv\Scripts\python.exe -m mrs3.cli --help
```

The selector, tester runner and post-test comparison require their respective validated input files. See [PRD.md](PRD.md) and the active feature specification before running production data.

## Documentation

- [Project requirements and feature registry](PRD.md)
- [Current verified state](progress.md)
- [Contributor/session rules](AGENTS.md)

Do not delete raw HTML until the v4 DuckDB import audit confirms each file is safe to delete.
