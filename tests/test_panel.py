from __future__ import annotations

from http.client import HTTPConnection
import io
import json
from pathlib import Path
import time

import pytest

from mrs3.panel import PanelController, _Job, create_panel_server


class _FakeProcess:
    def __init__(self, command: list[str], **_: object) -> None:
        self.command = command
        self.pid = 12345
        self.stdout = io.StringIO("started\nfinished\n")
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode


def _wait_finished(controller: PanelController) -> dict[str, object]:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        snapshot = controller.snapshot()
        if snapshot["job"] and not snapshot["job"]["running"]:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("panel job did not finish")


def test_controller_builds_shell_free_tester_command_and_captures_log(
    tmp_path: Path,
) -> None:
    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=_FakeProcess,
    )

    controller.start(
        "tester-run",
        {
            "config": "config.json",
            "strategies": "generated/strategies",
            "output_csv": "results/test.csv",
        },
    )
    snapshot = _wait_finished(controller)

    job = snapshot["job"]
    assert job["status"] == "SUCCEEDED"
    assert job["logs"] == ["started", "finished"]
    assert job["command"][1:4] == ["-m", "mrs3.cli", "tester-run"]
    assert job["command"][-2:] == [
        "--output-csv",
        str((tmp_path / "results/test.csv").resolve()),
    ]


def test_controller_builds_source_csv_command_without_tester_config(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    controller.start("source-csv", {"config": "config.json", "input_csv": "a.csv;b.csv", "start": "2026-07-15T00:00:00Z", "end": "2026-08-06T00:00:00Z", "output_dir": "package"})
    job = _wait_finished(controller)["job"]

    assert job["command"][1:4] == ["-m", "mrs3.cli", "source-csv"]
    assert job["command"].count("--input-csv") == 2
    assert "--config" in job["command"]


def test_controller_builds_duckdb_command_with_optional_html_verification(
    tmp_path: Path,
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    command, _ = controller._build_command(
        "source-duckdb",
        {
            "config": "config.json",
            "database": "source.duckdb",
            "start": "2026-07-15T00:00:00Z",
            "end": "2026-08-06T00:00:00Z",
            "output_dir": "package",
            "verify_html_root": "html",
            "verification_sample_count": "4",
        },
    )

    assert command[command.index("--verify-html-root") + 1] == str((tmp_path / "html").resolve())
    assert command[command.index("--verification-sample-count") + 1] == "4"


@pytest.mark.parametrize("sample_count", ["", "two", "2", "6", 3.5, None])
def test_controller_rejects_invalid_duckdb_verification_sample_count_before_launch(
    tmp_path: Path, sample_count: object
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    with pytest.raises(ValueError, match="verification_sample_count"):
        controller._build_command(
            "source-duckdb",
            {
                "config": "config.json",
                "database": "source.duckdb",
                "start": "2026-07-15T00:00:00Z",
                "end": "2026-08-06T00:00:00Z",
                "output_dir": "package",
                "verify_html_root": "html",
                "verification_sample_count": sample_count,
            },
        )


def test_controller_selects_verified_source_package_without_raw_csv(
    tmp_path: Path,
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    command, _ = controller._build_command(
        "select",
        {
            "config": "config.json",
            "source_package": "verified-package",
            "dates": "dates.xlsx",
            "template": "template.json",
            "side": "LONG",
            "output_dir": "output",
        },
    )

    assert "--source-package" in command
    assert str((tmp_path / "verified-package").resolve()) in command
    assert "--input-csv" not in command


def test_controller_keeps_compatibility_raw_csv_selection(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    command, _ = controller._build_command(
        "select",
        {
            "config": "config.json",
            "input_csv": "input.csv",
            "dates": "dates.xlsx",
            "template": "template.json",
            "side": "LONG",
            "output_dir": "output",
        },
    )

    assert "--input-csv" in command
    assert "--source-package" not in command


@pytest.mark.parametrize(
    "source_payload",
    [{}, {"input_csv": "input.csv", "source_package": "package"}],
)
def test_controller_rejects_select_without_exactly_one_source(
    tmp_path: Path, source_payload: dict[str, str]
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    with pytest.raises(ValueError, match="exactly one"):
        controller._build_command(
            "select",
            {
                "config": "config.json",
                "dates": "dates.xlsx",
                "template": "template.json",
                "side": "LONG",
                "output_dir": "output",
                **source_payload,
            },
        )


@pytest.mark.parametrize("invalid_source", [None, "   "])
def test_controller_rejects_null_or_blank_select_source(
    tmp_path: Path, invalid_source: str | None
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    with pytest.raises(ValueError, match="exactly one"):
        controller._build_command(
            "select",
            {
                "config": "config.json",
                "input_csv": invalid_source,
                "dates": "dates.xlsx",
                "template": "template.json",
                "side": "LONG",
                "output_dir": "output",
            },
        )


def test_controller_rejects_parallel_jobs(tmp_path: Path) -> None:
    class WaitingProcess(_FakeProcess):
        def wait(self) -> int:
            time.sleep(0.2)
            return 0

    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=WaitingProcess,
    )
    payload = {"config": "config.json", "strategies": "strategies"}

    controller.start("tester-plan", payload)

    with pytest.raises(RuntimeError, match="already running"):
        controller.start("tester-plan", payload)


def test_controller_hides_artifacts_left_by_an_older_job(tmp_path: Path) -> None:
    output = tmp_path / "results/test.csv"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")
    state = output.with_name("test.state.json")
    state.write_text('{"state":"COMPLETED"}', encoding="utf-8")
    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=_FakeProcess,
    )

    controller.start(
        "tester-run",
        {
            "config": "config.json",
            "strategies": "strategies",
            "output_csv": "results/test.csv",
        },
    )
    snapshot = _wait_finished(controller)

    assert snapshot["job"]["workflow"] is None
    assert snapshot["job"]["artifacts"] == {}


def test_dashboard_reports_manifest_counts_but_never_claims_v2_selectable(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    manifest = package / "package_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "package_version": 2,
                "event_mode": "real_independent_events",
                "window_start": "2026-07-15T00:00:00+00:00",
                "window_end": "2026-08-06T00:00:00+00:00",
                "report_count": 12,
                "coverage_accepted_reports": 10,
                "coverage_rejected_reports": 2,
                "point_count": 10,
                "included_cycles": 45,
                "exclusions": {"OPEN_BEFORE_WINDOW": 3, "CLOSE_ON_OR_AFTER_WINDOW": 4},
                "source_summary_status": "VERIFIED",
                "window_metrics_status": "DERIVED_FROM_VERIFIED_SOURCE",
            }
        ),
        encoding="utf-8",
    )
    controller = PanelController(tmp_path, tmp_path / "config.json")
    controller._section_jobs["duckdb"] = _Job(
        job_id="complete-duckdb",
        action="source-duckdb",
        command=(),
        artifacts={"manifest": manifest},
        artifact_baseline={"manifest": None},
        status="SUCCEEDED",
    )

    dashboard = controller.snapshot()["dashboard"]["duckdb"]

    assert dashboard["available"] is True
    assert dashboard["state"] == "VERIFICATION_STATUSES_PRESENT"
    # This deliberately lacks source_summary_samples and the verification CSV.
    # The dashboard is not the package verifier and must never overrule it.
    assert dashboard["state"] != "SELECTABLE"
    assert dashboard["metrics"] == [
        {"label": "Отчёты", "value": 12},
        {"label": "Точки", "value": 10},
        {"label": "Покрытие: принято", "value": 10},
        {"label": "Покрытие: отклонено", "value": 2},
        {"label": "Включено (циклы)", "value": 45},
        {"label": "Исключено (циклы)", "value": 7},
    ]
    assert dashboard["details"] == [
        "real_independent_events · пакет v2",
        "Окно UTC: 2026-07-15T00:00:00+00:00 — 2026-08-06T00:00:00+00:00",
        "Source summary: VERIFIED",
        "Window metrics: DERIVED_FROM_VERIFIED_SOURCE",
    ]
    assert str(package) not in json.dumps(dashboard)


def test_dashboard_reports_candidate_tester_and_posttest_final_artifacts(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "run_manifest.json").write_text(
        json.dumps(
            {
                "event_mode": "real_independent_events",
                "event_eligible_point_count": 11,
                "geometric_plateau_count": 4,
                "ready_plateau_count": 3,
                "ready_structure_count": 2,
                "ready_json_count": 5,
            }
        ),
        encoding="utf-8",
    )
    results = tmp_path / "results.csv"
    results.write_text(
        "strategy_name,total_pnl_pct,max_drawdown_pct\na,2.5,1.2\nb,3.0,2.0\n",
        encoding="utf-8",
    )
    posttest = tmp_path / "posttest"
    posttest.mkdir()
    (posttest / "posttest_manifest.json").write_text(
        json.dumps(
            {"raw_result_count": 2, "pareto_count": 1, "scaled_strategy_count": 1, "target_dd_pct": "5"}
        ),
        encoding="utf-8",
    )
    controller = PanelController(tmp_path, tmp_path / "config.json")
    controller._section_jobs = {
        "candidates": _Job("selected", "select", (), {"manifest": selected / "run_manifest.json"}, {"manifest": None}, status="SUCCEEDED"),
        "tester": _Job("tested", "tester-run", (), {"output_csv": results}, {"output_csv": None}, status="SUCCEEDED"),
        "posttest": _Job("dd5", "posttest", (), {"manifest": posttest / "posttest_manifest.json"}, {"manifest": None}, status="SUCCEEDED"),
    }

    dashboard = controller.snapshot()["dashboard"]

    assert dashboard["candidates"]["state"] == "READY_FOR_TEST"
    assert dashboard["candidates"]["metrics"][-1] == {"label": "JSON для теста", "value": 5}
    assert dashboard["tester"]["state"] == "COMPLETED"
    assert dashboard["tester"]["metrics"] == [
        {"label": "Результаты", "value": 2},
        {"label": "Лучший PnL, %", "value": "3"},
        {"label": "DD лучшего, %", "value": "2"},
        {"label": "Ошибки", "value": 0},
    ]
    assert dashboard["posttest"]["state"] == "RETEST_REQUIRED"
    assert dashboard["posttest"]["metrics"][-1] == {"label": "DD5 JSON", "value": 1}


def test_dashboard_keeps_last_artifact_when_a_later_job_has_none(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    manifest = package / "package_manifest.json"
    manifest.write_text(
        json.dumps(
            {"event_mode": "legacy_trades_proxy", "source_rows": 3, "accepted_rows": 2, "rejected_rows": 1}
        ),
        encoding="utf-8",
    )
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)
    controller._section_jobs["csv"] = _Job(
        "old", "source-csv", (), {"manifest": manifest}, {"manifest": None}, status="SUCCEEDED"
    )

    controller.start("tester-plan", {"config": "config.json", "strategies": "strategies"})
    dashboard = _wait_finished(controller)["dashboard"]

    assert dashboard["csv"]["available"] is True
    assert dashboard["csv"]["metrics"][1] == {"label": "Точки", "value": 2}


def test_panel_rejects_non_loopback_bind(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json")

    with pytest.raises(ValueError, match="loopback"):
        create_panel_server("0.0.0.0", 0, controller)


def test_http_panel_serves_ui_status_and_start_endpoint(tmp_path: Path) -> None:
    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=_FakeProcess,
    )
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode("utf-8")
        assert response.status == 200
        assert "MRS3 Control Panel" in html
        assert "Каталог JSON-стратегий" in html
        assert "MRS2 · CSV" in html
        assert html.count('<button role="tab"') == 5
        assert html.count('<section role="tabpanel"') == 5
        assert "MRS2 · CSV" in html
        assert "MRS2 · DuckDB" in html
        assert "Кандидаты стратегий" in html
        assert "Анализатор портфелей" in html
        assert "Настройки" in html
        assert "legacy_trades_proxy" in html
        assert "real_independent_events" in html
        assert 'id="verify_html_root"' in html
        assert 'id="verification_sample_count"' in html
        assert "Совместимый CSV-вход (текущий путь)" in html
        assert "Симулятор сетов недоступен" in html
        assert "Рекомендации недоступны" in html
        assert 'data-runnable="true"' in html
        assert "document.querySelectorAll('[data-runnable]')" in html
        assert 'aria-live="polite"' in html
        assert "prefers-reduced-motion: reduce" in html
        assert "prefers-reduced-transparency: reduce" in html
        assert "prefers-contrast: more" in html
        assert "function activateTab" in html
        assert "function browse" in html
        assert "/api/browse" in html
        assert "CSV-файлы" in html

        body = json.dumps(
            {
                "action": "tester-plan",
                "config": "config.json",
                "strategies": "strategies",
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/start",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        started = connection.getresponse()
        document = json.loads(started.read().decode("utf-8"))
        assert started.status == 202
        assert document["job"]["action"] == "tester-plan"

        connection.request("GET", "/api/status")
        status = connection.getresponse()
        status_document = json.loads(status.read().decode("utf-8"))
        assert status.status == 200
        assert status_document["defaults"]["config"] == str(
            (tmp_path / "config.json").resolve()
        )

        connection.request(
            "POST",
            "/api/start",
            body=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        unsupported = connection.getresponse()
        unsupported.read()
        assert unsupported.status == 415
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_panel_browse_returns_only_explicit_native_selection(tmp_path: Path) -> None:
    selected = (tmp_path / "one.csv", tmp_path / "two.csv")
    calls: list[tuple[str, bool]] = []

    def chooser(kind: str, multiple: bool) -> tuple[Path, ...]:
        calls.append((kind, multiple))
        return selected

    controller = PanelController(
        tmp_path,
        tmp_path / "config.json",
        browse_factory=chooser,
    )
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        body = json.dumps({"kind": "csv", "multiple": True}).encode("utf-8")
        connection.request("POST", "/api/browse", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        document = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert calls == [("csv", True)]
        assert document == {"paths": [str(path.resolve()) for path in selected]}

        body = json.dumps({"kind": "unknown", "multiple": False}).encode("utf-8")
        connection.request("POST", "/api/browse", body=body, headers={"Content-Type": "application/json"})
        rejected = connection.getresponse()
        rejected.read()
        assert rejected.status == 400
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
