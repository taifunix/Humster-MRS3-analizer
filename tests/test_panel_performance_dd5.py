from __future__ import annotations

import json
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

import pytest

from mrs3.config import AlgorithmConfig
from mrs3.panel import PanelController
from mrs3.panel_performance_dd5 import (
    LocalPerformanceDd5Service,
    LocalPerformanceDd5Jobs,
    PanelPerformanceDd5Error,
    PerformanceDd5Request,
)


def _request(tmp_path: Path, *, delete_html: bool = False) -> PerformanceDd5Request:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    manifest = {
        "schema_version": 1,
        "batch_id": "batch-1",
        "expected_strategy_names": ["A"],
        "entries": [{"strategy_name": "A"}],
        "v6_provenance": {
            "analysis_run_id": "analysis-1",
            "generation_manifest_sha256": "a" * 64,
            "strategy_json_sha256": {"A.json": "b" * 64},
        },
    }
    (inbox / "inbox_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return PerformanceDd5Request(
        inbox=inbox,
        database=tmp_path / "performance.duckdb",
        output_dir=tmp_path / "posttest",
        config=AlgorithmConfig.defaults(),
        delete_html=delete_html,
    )


def _audit(request: PerformanceDd5Request, *, status: str = "COMMITTED", quarantine: int = 0) -> None:
    (request.inbox / "import_audit.v4.json").write_text(
        json.dumps(
            {
                "schema_version": 4,
                "batch_id": "batch-1",
                "import_id": "import-1",
                "status": status,
                "quarantine_count": quarantine,
            }
        ),
        encoding="utf-8",
    )


def _artifacts(request: PerformanceDd5Request, *, mode: str = "CALCULATION_ONLY") -> SimpleNamespace:
    request.output_dir.mkdir()
    manifest = {"dd5_run_id": "dd5-1", "import_id": "import-1", "dd5_mode": mode}
    path = request.output_dir / "posttest_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return SimpleNamespace(
        manifest=path,
        manifest_json=manifest,
        dd5_run_id="dd5-1",
    )


def test_preflight_requires_v6_provenance_and_rejects_legacy_inputs(tmp_path: Path) -> None:
    request = _request(tmp_path)
    service = LocalPerformanceDd5Service()
    assert service.preflight(request)["analysis_run_id"] == "analysis-1"

    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("v6_provenance")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PanelPerformanceDd5Error, match="v6 provenance"):
        service.preflight(request)

    manifest["v6_provenance"] = _request(tmp_path / "other").inbox.joinpath("inbox_manifest.json") if False else {
        "analysis_run_id": "analysis-1",
        "generation_manifest_sha256": "a" * 64,
        "strategy_json_sha256": {"A.json": "b" * 64},
    }
    manifest["legacy_csv"] = "results.csv"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PanelPerformanceDd5Error, match="legacy CSV"):
        service.preflight(request)


def test_run_imports_then_dd5_and_does_not_cleanup_by_default(tmp_path: Path) -> None:
    request = _request(tmp_path)
    calls: list[str] = []

    def importer(import_request, *, progress=None):
        calls.append("import")
        _audit(request)
        return SimpleNamespace(import_id="import-1", quarantined_count=0)

    def dd5(database, import_id, output_dir, config):
        calls.append("dd5")
        return _artifacts(request)

    def cleanup(import_request):
        calls.append("cleanup")

    result = LocalPerformanceDd5Service(
        import_batch=importer,
        run_dd5=dd5,
        cleanup=cleanup,
    ).run(request)

    assert calls == ["import", "dd5"]
    assert result.import_id == "import-1"
    assert result.dd5_run_id == "dd5-1"
    assert result.dd5_mode == "CALCULATION_ONLY"


def test_run_rejects_quarantine_and_only_cleans_after_verified_calculation_only_dd5(tmp_path: Path) -> None:
    request = _request(tmp_path, delete_html=True)
    calls: list[str] = []

    def importer(import_request, *, progress=None):
        calls.append("import")
        _audit(request, quarantine=1)
        return SimpleNamespace(import_id="import-1", quarantined_count=1)

    with pytest.raises(PanelPerformanceDd5Error, match="zero quarantine"):
        LocalPerformanceDd5Service(import_batch=importer, cleanup=lambda _: calls.append("cleanup")).run(request)
    assert calls == ["import"]

    _audit(request)
    calls.clear()
    service = LocalPerformanceDd5Service(
        import_batch=lambda *_args, **_kwargs: calls.append("import") or SimpleNamespace(import_id="import-1", quarantined_count=0),
        run_dd5=lambda *_args: calls.append("dd5") or _artifacts(request, mode="TICK_TEST"),
        cleanup=lambda _: calls.append("cleanup"),
    )
    with pytest.raises(PanelPerformanceDd5Error, match="CALCULATION_ONLY"):
        service.run(request)
    assert calls == ["import", "dd5"]


def test_jobs_runs_asynchronously_and_redacts_failure(tmp_path: Path) -> None:
    request = _request(tmp_path)
    started = Event()
    release = Event()

    def run(_request, *, progress=None):
        started.set()
        release.wait(2)
        raise RuntimeError("D:/private/report.html")

    jobs = LocalPerformanceDd5Jobs(run=run)
    launched = jobs.start(request)
    assert started.wait(1)
    assert launched["state"] == "RUNNING"
    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and jobs.status(str(launched["job_id"]))["state"] == "RUNNING":
        time.sleep(0.01)
    assert jobs.status(str(launched["job_id"]))["error"] == {"code": "FAILED"}


def test_controller_returns_reconciled_dd5_job_when_worker_does_not_survive_restart(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    job = controller._panel_jobs.submit("strategies.performance-dd5", {}, "interrupted")
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    restarted = PanelController(tmp_path, tmp_path / "config.local.json")

    status = restarted.strategies_performance_dd5_status(job["job_id"])

    assert status["state"] == "FAILED"
    assert status["error"] == {"code": "INTERRUPTED"}
