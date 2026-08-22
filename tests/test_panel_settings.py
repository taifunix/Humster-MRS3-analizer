from __future__ import annotations

from http.client import HTTPConnection
import hashlib
import json
from pathlib import Path
import threading

import pytest

from mrs3.panel import PanelController, create_panel_server


def _runner_config() -> dict[str, object]:
    return {
        "tester_runner": {
            "bot_root": "bot",
            "executable": "hb_c.exe",
            "base_url": "http://127.0.0.1:8087",
            "port": 8087,
            "strategy_dir": "settings_strategy",
            "report_dir": "tester/report/my_test",
            "wizard_result": "tester/wizard_result.json",
            "wizard_progress": "tester/wizard_progress.json",
            "tester_config": "tester/tester_config.json",
            "inbox_root": "data/tester_inbox",
        }
    }


def _request(
    connection: HTTPConnection,
    method: str,
    path: str,
    payload: object | None = None,
) -> tuple[int, dict[str, object]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    return response.status, json.loads(response.read().decode("utf-8"))


@pytest.fixture
def panel_http(tmp_path: Path):
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps(_runner_config()), encoding="utf-8")
    controller = PanelController(tmp_path, config)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        yield tmp_path, config, connection
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_bootstrap_reports_only_safe_capabilities_and_redacts_runner_and_remote(
    panel_http,
) -> None:
    root, config, connection = panel_http
    document = json.loads(config.read_text(encoding="utf-8"))
    document["remote_runner"] = {
        "host": "10.0.0.8",
        "user": "alice",
        "password": "do-not-leak",
        "private_key": "C:\\secret\\id_ed25519",
        "bot_root": "D:\\remote\\worker",
        "debian_runner_root": "/opt/remote/runner",
        "report_root": "/opt/remote/reports",
        "output_root": "/opt/remote/output",
    }
    config.write_text(json.dumps(document), encoding="utf-8")

    status, body = _request(connection, "GET", "/api/v2/bootstrap")
    encoded = json.dumps(body)
    assert status == 200
    assert body["version"] == "panel-ui-v2"
    assert body["defaults"]["runner"] == {"configured": True}
    assert body["defaults"]["remote"] == {"configured": True}
    paths = body["defaults"]["panel"]["path_defaults"]
    assert paths["local_bot_root"] == "bot"
    assert paths["local_runner_root"] == "settings_strategy"
    assert paths["local_reports_root"] == "tester/report/my_test"
    assert paths["local_output_root"] == "data/tester_inbox"
    assert paths["remote_bot_root"] == "D:\\remote\\worker"
    assert paths["remote_runner_root"] == "/opt/remote/runner"
    assert paths["remote_reports_root"] == "/opt/remote/reports"
    assert paths["remote_output_root"] == "/opt/remote/output"
    assert str(root) not in encoded
    for secret in ("10.0.0.8", "alice", "do-not-leak", "id_ed25519", "password", "private_key"):
        assert secret not in encoded


@pytest.mark.parametrize(
    "content", ["", "{not-json", json.dumps([]), json.dumps({"tester_runner": []})]
)
def test_bootstrap_malformed_or_missing_config_fails_closed(panel_http, content: str) -> None:
    _, config, connection = panel_http
    if content:
        config.write_text(content, encoding="utf-8")
    else:
        config.unlink()

    status, body = _request(connection, "GET", "/api/v2/bootstrap")
    encoded = json.dumps(body)
    assert status == 200
    assert body["defaults"]["runner"] == {"configured": False}
    assert body["defaults"]["remote"] == {"configured": False}
    assert str(config) not in encoded
    assert "JSONDecodeError" not in encoded


def test_source_catalog_lists_only_duckdb_files_from_configured_source_directory(panel_http) -> None:
    root, config, connection = panel_http
    source_root = root / "source-db"
    source_root.mkdir()
    (source_root / "zeta.duckdb").write_bytes(b"db")
    (source_root / "alpha.duckdb").write_bytes(b"db")
    (source_root / "note.txt").write_text("ignore", encoding="utf-8")
    document = json.loads(config.read_text(encoding="utf-8"))
    document["duckdb_import"] = {"source_duckdb_path": "source-db/default.duckdb"}
    config.write_text(json.dumps(document), encoding="utf-8")

    status, body = _request(connection, "GET", "/api/v2/source/local/catalog")

    assert status == 200
    assert body == {"databases": [
        {"name": "alpha.duckdb", "path": str(source_root / "alpha.duckdb")},
        {"name": "zeta.duckdb", "path": str(source_root / "zeta.duckdb")},
    ]}


def test_source_catalog_is_empty_for_invalid_import_settings(panel_http) -> None:
    _, config, connection = panel_http
    document = json.loads(config.read_text(encoding="utf-8"))
    document["duckdb_import"] = {"source_duckdb_path": 42}
    config.write_text(json.dumps(document), encoding="utf-8")

    status, body = _request(connection, "GET", "/api/v2/source/local/catalog")

    assert status == 200
    assert body == {"databases": []}


def test_bootstrap_does_not_derive_runner_paths_from_invalid_runner_config(panel_http) -> None:
    _, config, connection = panel_http
    config.write_text(json.dumps({"tester_runner": {"bot_root": "private-bot"}}), encoding="utf-8")

    status, body = _request(connection, "GET", "/api/v2/bootstrap")

    assert status == 200
    assert body["defaults"]["runner"] == {"configured": False}
    assert "private-bot" not in json.dumps(body)


def test_validate_does_not_write_and_roundtrips_approved_absolute_path(panel_http) -> None:
    root, config, connection = panel_http
    before = config.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()
    before_mtime = config.stat().st_mtime_ns
    backup = Path(f"{config}.bak")
    assert not backup.exists()
    status, body = _request(
        connection,
        "POST",
        "/api/v2/settings/validate",
        {"panel": {"default_root": "static", "path_defaults": {"output_root": "Output"}}},
    )
    assert status == 200
    assert body["valid"] is True
    assert body["settings"]["panel"]["default_root"] == "static"
    assert config.read_bytes() == before
    assert hashlib.sha256(config.read_bytes()).hexdigest() == before_hash
    assert config.stat().st_mtime_ns == before_mtime
    assert not backup.exists()

    absolute = "D:\\SHARE\\!MN\\hamster\\MRS-Analizer\\Output\\surfaces"
    status, body = _request(
        connection,
        "POST",
        "/api/v2/settings/validate",
        {"panel": {"path_defaults": {"output_root": absolute}}},
    )
    assert status == 200
    assert body["settings"]["panel"]["path_defaults"]["output_root"] == absolute
    assert config.read_bytes() == before

    status, body = _request(
        connection,
        "POST",
        "/api/v2/settings/validate",
        {"panel": {"path_defaults": {"output_root": "../Output"}}},
    )
    assert status == 400
    assert body == {"error": "invalid settings"}


def test_save_preserves_unknown_keys_formats_and_prior_backup_and_reload_persists(panel_http) -> None:
    root, config, connection = panel_http
    config.write_text(
        '{\n  "unknown": {"keep": true},\n  "panel": {"default_root": "legacy"}\n}\n',
        encoding="utf-8",
    )
    old = config.read_bytes()
    status, body = _request(
        connection,
        "POST",
        "/api/v2/settings/save",
        {"panel": {"default_root": "static", "path_defaults": {"output_root": "Output"}}},
    )
    backup = Path(f"{config}.bak")
    assert status == 200
    assert body["saved"] is True
    assert config.exists() and backup.read_bytes() == old
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["unknown"] == {"keep": True}
    assert saved["panel"] == {
        "default_root": "static",
        "path_defaults": {"output_root": "Output"},
    }

    status, body = _request(connection, "GET", "/api/v2/settings/reload")
    assert status == 200
    assert body["valid"] is True
    assert body["settings"]["panel"]["default_root"] == "static"
    assert PanelController(root, config).panel_default_root() == "static"


def test_save_path_defaults_updates_local_and_remote_runtime_paths(panel_http) -> None:
    _, config, connection = panel_http
    document = _runner_config()
    document["remote_runner"] = {
        "host": "runner.example.test", "user": "tester", "bot_root": "/old/bot",
        "debian_runner_root": "/old/runner", "reports_root": "/old/reports",
        "reports_archive_root": "/old/archive",
    }
    config.write_text(json.dumps(document), encoding="utf-8")
    paths = {
        "local_bot_root": "D:\\MRS3\\bot", "local_runner_root": "D:\\MRS3\\bot\\settings_strategy",
        "local_reports_root": "D:\\MRS3\\bot\\tester\\report", "local_output_root": "D:\\MRS3\\archive",
        "remote_bot_root": "/opt/hb1", "remote_runner_root": "/opt/hb1/debian-duckdb-importer",
        "remote_reports_root": "/opt/hb1/tester/report", "remote_reports_archive_root": "/opt/hb1/archive",
        "local_merge_source_a": "D:\\MRS3\\source-a.duckdb", "local_merge_source_b": "D:\\MRS3\\source-b.duckdb",
        "local_merge_target": "D:\\MRS3\\merged.duckdb",
    }

    status, body = _request(connection, "POST", "/api/v2/settings/save", {"panel": {"path_defaults": paths}})

    assert status == 200 and body["saved"] is True
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert {key: saved["tester_runner"][key] for key in ("bot_root", "strategy_dir", "report_dir", "inbox_root")} == {
        "bot_root": paths["local_bot_root"], "strategy_dir": paths["local_runner_root"],
        "report_dir": paths["local_reports_root"], "inbox_root": paths["local_output_root"],
    }
    assert {key: saved["remote_runner"][key] for key in ("bot_root", "debian_runner_root", "reports_root", "reports_archive_root")} == {
        "bot_root": paths["remote_bot_root"], "debian_runner_root": paths["remote_runner_root"],
        "reports_root": paths["remote_reports_root"], "reports_archive_root": paths["remote_reports_archive_root"],
    }
    assert saved["panel"]["path_defaults"] == paths


def test_panel_restart_endpoint_relaunches_only_without_active_jobs(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps(_runner_config()), encoding="utf-8")
    controller = PanelController(tmp_path, config)
    launched: list[bool] = []
    server = create_panel_server("127.0.0.1", 0, controller, restart_launcher=lambda: launched.append(True))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        status, body = _request(connection, "POST", "/api/v2/panel/restart", {})
        assert status == 200
        assert body == {"restarting": True}
        assert launched == [True]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_panel_restart_endpoint_rejects_active_jobs(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps(_runner_config()), encoding="utf-8")
    controller = PanelController(tmp_path, config)
    job = controller.panel_job_submit({"kind": "test.job", "request": {}, "idempotency_key": "restart-test", "resource_keys": []})
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    launched: list[bool] = []
    server = create_panel_server("127.0.0.1", 0, controller, restart_launcher=lambda: launched.append(True))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        status, _ = _request(connection, "POST", "/api/v2/panel/restart", {})
        assert status == 409
        assert launched == []
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_save_operational_settings_updates_the_fields_consumed_by_panel_jobs(panel_http) -> None:
    _, config, connection = panel_http
    payload = {
        "panel": {"default_root": "static"},
        "operational": {
            "local_bot_root": "new-bot", "remote_runner_root": "/opt/new-runner",
            "source_db_path": "Output/source.duckdb", "output_root": "Output/inbox",
            "listing_dates_path": "Input/dates.json", "algorithm_version": "v7-test",
            "import_workers": 3, "transaction_batch_size": 200,
        },
    }

    status, body = _request(connection, "POST", "/api/v2/settings/save", payload)

    assert status == 200
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["tester_runner"]["bot_root"] == "new-bot"
    assert saved["tester_runner"]["inbox_root"] == "Output/inbox"
    assert saved["remote_runner"]["debian_runner_root"] == "/opt/new-runner"
    assert saved["duckdb_import"] == {"source_duckdb_path": "Output/source.duckdb", "workers": 3, "transaction_batch_size": 200}
    assert saved["panel_workflow"] == {"listing_dates_path": "Input/dates.json", "algorithm_version": "v7-test"}
    assert body["settings"]["operational"]["import_workers"] == 3


def test_failed_save_leaves_config_and_existing_backup_unchanged(panel_http) -> None:
    _, config, connection = panel_http
    config.write_text(json.dumps({"keep": "current", "panel": {"default_root": "legacy"}}), encoding="utf-8")
    backup = Path(f"{config}.bak")
    backup.write_text("old backup", encoding="utf-8")
    before = config.read_bytes()
    before_backup = backup.read_bytes()

    status, body = _request(
        connection,
        "POST",
        "/api/v2/settings/save",
        {"panel": {"default_root": "unsafe", "path_defaults": {}}},
    )
    assert status == 400
    assert body == {"error": "invalid settings"}
    assert config.read_bytes() == before
    assert backup.read_bytes() == before_backup


def test_failed_atomic_replace_restores_existing_backup(panel_http, monkeypatch) -> None:
    _, config, connection = panel_http
    config.write_text(json.dumps({"panel": {"default_root": "legacy"}}), encoding="utf-8")
    backup = Path(f"{config}.bak")
    backup.write_bytes(b"old backup")
    before = config.read_bytes()
    before_backup = backup.read_bytes()
    from mrs3 import panel_settings

    original_replace = panel_settings.os.replace
    calls = 0

    def fail_config_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        return original_replace(source, target)

    monkeypatch.setattr(panel_settings.os, "replace", fail_config_replace)
    status, body = _request(
        connection,
        "POST",
        "/api/v2/settings/save",
        {"panel": {"default_root": "static"}},
    )
    assert status == 400
    assert body == {"error": "invalid settings"}
    assert config.read_bytes() == before
    assert backup.read_bytes() == before_backup


def test_failed_backup_staging_leaves_live_config_and_backup_byte_identical(
    panel_http, monkeypatch
) -> None:
    _, config, connection = panel_http
    config.write_text(json.dumps({"panel": {"default_root": "legacy"}}), encoding="utf-8")
    backup = Path(f"{config}.bak")
    backup.write_bytes(b"old backup")
    before_hash = hashlib.sha256(config.read_bytes()).hexdigest()
    before_mtime = config.stat().st_mtime_ns
    before_backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    before_backup_mtime = backup.stat().st_mtime_ns
    from mrs3 import panel_settings

    def fail_backup_copy(*_args, **_kwargs):
        raise OSError("simulated backup staging failure")

    monkeypatch.setattr(panel_settings.shutil, "copyfileobj", fail_backup_copy)
    status, body = _request(
        connection,
        "POST",
        "/api/v2/settings/save",
        {"panel": {"default_root": "static"}},
    )
    assert status == 400
    assert body == {"error": "invalid settings"}
    assert hashlib.sha256(config.read_bytes()).hexdigest() == before_hash
    assert config.stat().st_mtime_ns == before_mtime
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == before_backup_hash
    assert backup.stat().st_mtime_ns == before_backup_mtime


def test_validate_missing_config_does_not_create_config_or_backup(panel_http) -> None:
    root, config, connection = panel_http
    config.unlink()
    backup = Path(f"{config}.bak")
    status, body = _request(
        connection,
        "POST",
        "/api/v2/settings/validate",
        {"panel": {"default_root": "static"}},
    )
    assert status == 200
    assert body["valid"] is True
    assert not config.exists()
    assert not backup.exists()


def test_invalid_v2_request_and_unknown_endpoint_are_generic(panel_http) -> None:
    _, _, connection = panel_http
    status, body = _request(connection, "POST", "/api/v2/settings/validate", {"password": "secret"})
    assert status == 400
    assert body == {"error": "invalid settings"}
    status, body = _request(
        connection,
        "POST",
        "/api/v2/settings/validate",
        {"panel": {"default_root": "static", "password": "secret"}},
    )
    assert status == 400
    assert body == {"error": "invalid settings"}
    assert "secret" not in json.dumps(body)
    status, body = _request(connection, "GET", "/api/v2/settings/unknown")
    assert status == 404
    assert body == {"error": "not found"}


@pytest.mark.parametrize("path", ["\\\\server\\share", "\\\\?\\C:\\device"])
def test_validate_rejects_unc_and_device_paths(panel_http, path: str) -> None:
    _, _, connection = panel_http
    status, body = _request(
        connection,
        "POST",
        "/api/v2/settings/validate",
        {"panel": {"path_defaults": {"output_root": path}}},
    )
    assert status == 400
    assert body == {"error": "invalid settings"}


def test_reload_malformed_config_uses_stable_generic_error(panel_http) -> None:
    _, config, connection = panel_http
    config.write_text("{not-json", encoding="utf-8")
    status, body = _request(connection, "GET", "/api/v2/settings/reload")
    assert status == 400
    assert body == {"error": "settings unavailable"}


def test_jobs_endpoint_has_stable_job_errors_and_accepts_queued_job(panel_http) -> None:
    _, _, connection = panel_http
    status, body = _request(connection, "POST", "/api/v2/jobs", {"kind": "", "request": {}, "idempotency_key": "x"})
    assert status == 400
    assert body == {"error": "INVALID_REQUEST"}
    status, body = _request(
        connection, "POST", "/api/v2/jobs",
        {"kind": "testing.local", "request": {}, "idempotency_key": "unique", "resource_keys": ["testing:local"]},
    )
    assert status == 202
    assert body["job"]["state"] == "QUEUED"


def test_v2_local_testing_status_is_redacted(panel_http) -> None:
    root, _, connection = panel_http

    status, body = _request(connection, "GET", "/api/v2/testing/local/status")

    assert status == 200
    assert body["preflight_ok"] is False
    assert str(root) not in json.dumps(body)


def test_v2_local_testing_fill_rejects_unconfigured_runner(panel_http) -> None:
    _, _, connection = panel_http

    status, body = _request(
        connection, "POST", "/api/v2/testing/local/fill",
        {"symbols": "CXUSDT", "side": "LONG", "start": "2026-07-15", "end": "2026-08-06"},
    )

    assert status == 400
    assert body == {"error": "invalid settings"}


def test_static_settings_markup_is_semantic_and_non_operational() -> None:
    from mrs3 import panel as panel_module

    html = (Path(panel_module.__file__).parent / "panel_web" / "index.html").read_text(encoding="utf-8")
    assert '<section id="settings"' in html
    assert '<form' in html
    assert 'for="settings-default-root"' in html
    assert 'id="settings-default-root"' in html
    assert 'aria-live="polite"' in html
    assert 'id="portfolio"' not in html
