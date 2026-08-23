"""An analysis that already exists must be openable.

The strategies screen could only reach a shortlist by running a new analysis:
`_fresh_analysis_paths` is a session dictionary filled solely by
`strategies_fresh_analyze`. Restarting the panel, or wanting to look at an
analysis produced earlier, left every committed `.analysis-v6.duckdb` on disk
unreachable — and the only way back was to recompute it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_fresh_analysis_strategies import _make_analysis


def _controller(tmp_path: Path):
    from mrs3.panel import PanelController

    config = tmp_path / "config.local.json"
    config.write_text(
        json.dumps({"panel": {"path_defaults": {"analysis_db_root": str(tmp_path / "analysis")}}}),
        encoding="utf-8",
    )
    return PanelController(tmp_path, config)


def test_the_catalog_lists_a_committed_analysis(tmp_path: Path) -> None:
    """The screen needs the same catalog affordance surfaces already have."""
    directory = tmp_path / "analysis"
    directory.mkdir()
    analysis_id, _surface = _make_analysis(directory / "run.analysis-v6.duckdb")

    result = _controller(tmp_path).analysis_catalog()

    assert [item["name"] for item in result["analyses"]] == ["run.analysis-v6.duckdb"]
    entry = result["analyses"][0]
    assert entry["analysis_run_id"] == analysis_id
    assert entry["scopes"] == 1


def test_an_artifact_that_is_not_a_fresh_analysis_is_skipped(tmp_path: Path) -> None:
    """A broken or foreign file must not appear as a choice."""
    directory = tmp_path / "analysis"
    directory.mkdir()
    (directory / "broken.analysis-v6.duckdb").write_bytes(b"not a database")

    assert _controller(tmp_path).analysis_catalog() == {"analyses": []}


def test_opening_an_analysis_makes_its_shortlist_reachable(tmp_path: Path) -> None:
    """Opening is what registers the run, so the shortlist stops needing a rerun."""
    directory = tmp_path / "analysis"
    directory.mkdir()
    path = directory / "run.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(path)
    controller = _controller(tmp_path)

    opened = controller.strategies_fresh_open({"analysis_path": str(path)})

    assert opened["analysis_run_id"] == analysis_id
    assert opened["phase"] == "COMMITTED"
    shortlist = controller.strategies_fresh_shortlist({"analysis_run_id": analysis_id})
    assert [item["candidate_id"] for item in shortlist["items"]] == ["STR-READY"]


def test_opening_a_file_that_is_not_an_analysis_names_the_reason(tmp_path: Path) -> None:
    """A refusal must say what went wrong and register nothing."""
    from mrs3.panel_jobs import PanelJobError

    directory = tmp_path / "analysis"
    directory.mkdir()
    path = directory / "broken.analysis-v6.duckdb"
    path.write_bytes(b"not a database")
    controller = _controller(tmp_path)

    with pytest.raises(PanelJobError) as raised:
        controller.strategies_fresh_open({"analysis_path": str(path)})
    # "invalid settings" is what every other v2 failure collapses to; a reason
    # code costs nothing and leaks no local path.
    assert raised.value.code == "ANALYSIS_NOT_READABLE"

    with pytest.raises(PanelJobError) as missing:
        controller.strategies_fresh_open({})
    assert missing.value.code == "ANALYSIS_PATH_REQUIRED"
