from __future__ import annotations

import json
from pathlib import Path

import pytest

import mrs3.panel as panel_module
from mrs3.panel_remote_testing import (
    RemoteRunnerConfig,
    RemoteRunnerConfigError,
    RemoteTestingService,
    load_remote_runner_config,
    prepare_request,
)
from mrs3.panel import PanelController, PanelTestingError


def _config(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "host": "runner.example.test",
        "user": "tester",
        "port": 22,
        "password": "correct horse battery staple",
        "private_key_path": "",
        "bot_root": "/opt/hb1",
        "debian_runner_root": "/opt/hb1/debian-duckdb-importer",
        "reports_root": "/opt/hb1/tester/report",
        "source_db_root": "/opt/hb1/debian-duckdb-importer/data/db",
        "reports_archive_root": "/opt/hb1/debian-duckdb-importer/data/html",
    }
    value.update(overrides)
    return value


class _FakeOwnership:
    restored = False

    def acquire(self, target: str) -> dict[str, str]:
        return {"target": target}

    def attest(self, lease: object, target: str) -> bool:
        return lease == {"target": target}

    def release(self, _lease: object) -> bool:
        return True

    def snapshot_settings(self, lease: object, target: str) -> dict[str, object]:
        return {"lease": lease, "target": target}

    def restore_settings(self, lease: object, target: str, snapshot: object) -> bool:
        self.restored = snapshot == {"lease": lease, "target": target}
        return self.restored


def _owned_service(**kwargs: object) -> RemoteTestingService:
    return RemoteTestingService(
        load_remote_runner_config(_config()), ownership_adapter=_FakeOwnership(), **kwargs
    )


def test_remote_config_validates_and_never_repr_secrets() -> None:
    config = RemoteRunnerConfig.from_mapping(_config())

    assert config.host == "runner.example.test"
    assert config.port == 22
    assert config.auth_method == "password"
    assert "correct horse battery staple" not in repr(config)
    assert "correct horse battery staple" not in str(config)


def test_remote_target_identity_quotes_special_posix_root() -> None:
    config = load_remote_runner_config(
        _config(
            bot_root="/opt/hb #1/?",
            debian_runner_root="/opt/hb #1/debian",
            reports_root="/opt/hb #1/reports",
            source_db_root="/opt/hb #1/db",
            reports_archive_root="/opt/hb #1/archive",
        )
    )
    identity = RemoteTestingService(config).target_identity
    assert identity.endswith("/opt/hb%20%231/%3F")
    assert " " not in identity and "#" not in identity and "?" not in identity
    assert identity != RemoteTestingService(
        load_remote_runner_config(
            _config(
                bot_root="/opt/hb #2/?",
                debian_runner_root="/opt/hb #2/debian",
                reports_root="/opt/hb #2/reports",
                source_db_root="/opt/hb #2/db",
                reports_archive_root="/opt/hb #2/archive",
            )
        )
    ).target_identity


def test_remote_target_identity_normalizes_host_and_root_forms() -> None:
    canonical = load_remote_runner_config(_config())
    equivalent = load_remote_runner_config(
        _config(
            host="RUNNER.EXAMPLE.TEST.",
            bot_root="//opt//hb1/",
            debian_runner_root="//opt//hb1//debian-duckdb-importer/",
            reports_root="//opt//hb1//tester/report/",
            source_db_root="//opt//hb1//debian-duckdb-importer/data/db/",
            reports_archive_root="//opt//hb1//debian-duckdb-importer/data/html/",
        )
    )

    assert equivalent.host == "runner.example.test"
    assert equivalent.bot_root == "/opt/hb1"
    assert RemoteTestingService(equivalent).target_identity == RemoteTestingService(canonical).target_identity


def test_remote_target_identity_normalizes_bracketed_ipv6() -> None:
    unbracketed = load_remote_runner_config(_config(host="2001:DB8::1"))
    bracketed = load_remote_runner_config(_config(host="[2001:db8::1]"))

    assert RemoteTestingService(unbracketed).target_identity == RemoteTestingService(bracketed).target_identity


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": ""},
        {"user": ""},
        {"port": 0},
        {"port": 65536},
        {"bot_root": "relative"},
        {"reports_root": "/opt/hb1/../escape"},
        {"bot_root": "/opt/./hb1"},
        {"source_db_root": "/opt/hb1\n/db"},
    ],
)
def test_remote_config_rejects_invalid_values_without_echoing_input(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(RemoteRunnerConfigError) as raised:
        load_remote_runner_config(_config(**overrides))

    assert str(raised.value) == "invalid remote runner configuration"
    assert "escape" not in str(raised.value)


def test_status_is_redacted_and_preflight_does_no_remote_io() -> None:
    config = load_remote_runner_config(_config())
    service = RemoteTestingService(config)

    status = service.status()
    preflight = service.preflight()
    encoded = json.dumps({"status": status, "preflight": preflight})

    assert status["configured"] is True
    assert status["auth_method"] == "password"
    assert status["paths"] == {
        "bot_root": "configured",
        "debian_runner_root": "configured",
        "reports_root": "configured",
        "source_db_root": "configured",
        "reports_archive_root": "configured",
    }
    assert status["source_db_root_exists"] is None
    assert preflight["preflight_ok"] is True
    assert preflight["source_db_root_exists"] is None
    for secret in (
        "runner.example.test",
        "tester",
        "correct horse battery staple",
        "/opt/hb1",
        "debian-duckdb-importer",
    ):
        assert secret not in encoded


def test_prepare_request_validates_and_derives_deterministic_archive_folder() -> None:
    request = prepare_request(
        symbols=("BTCUSDT", "ethusdt"),
        side="short",
        start="2026-07-15",
        end="2026-08-06",
    )

    assert request == {
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "side": "SHORT",
        "start": "2026-07-15",
        "end": "2026-08-06",
        "report_archive_folder": "BTC_ETH_2026-07-15_2026-08-06",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"symbols": ("BTCUSDT", "bad symbol")},
        {"symbols": ("BTCUSDT",), "side": "BOTH"},
        {"symbols": ("BTCUSDT",), "start": "2026-08-07"},
        {"symbols": (),},
    ],
)
def test_prepare_request_rejects_invalid_inputs(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "symbols": ("BTCUSDT",),
        "side": "LONG",
        "start": "2026-07-15",
        "end": "2026-08-06",
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match="invalid remote testing request"):
        prepare_request(**values)


def test_panel_controller_remote_status_and_prepare_do_not_leak_connection_data(tmp_path) -> None:
    secret = "correct horse battery staple"
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"remote_runner": _config(password=secret)}), encoding="utf-8")
    controller = PanelController(tmp_path, config)

    status = controller.remote_testing_status()
    request = controller.remote_testing_prepare(
        {"symbols": "BTCUSDT, ETHUSDT", "side": "LONG", "start": "2026-07-15", "end": "2026-08-06"}
    )

    encoded = json.dumps({"status": status, "request": request})
    assert status["configured"] is True
    assert request["report_archive_folder"] == "BTC_ETH_2026-07-15_2026-08-06"
    assert secret not in encoded
    assert "runner.example.test" not in encoded


def test_panel_controller_remote_fill_uses_side_templates_without_connection_leakage(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    class FakeRemoteService:
        def __init__(self, _document):
            pass

        def fill(self, request, *, tester_template, strategy_template, max_parallel_runs):
            calls.append({
                "request": request,
                "tester_template": tester_template,
                "strategy_template": strategy_template,
                "max_parallel_runs": max_parallel_runs,
            })
            return {"state": "FILLED", "strategy_name": "safe"}

        def start(self):
            calls.append({"started": True})
            return {"state": "STARTED"}

    monkeypatch.setattr(panel_module, "RemoteTestingService", FakeRemoteService)
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({
        "remote_runner": _config(),
        "tester_runner": {
            "bot_root": str(tmp_path / "hb"), "executable": "hb_c.exe",
            "base_url": "http://127.0.0.1:8087", "port": 8087,
            "strategy_dir": "settings_strategy", "report_dir": "tester/report/my_test",
            "wizard_result": "tester/wizard_result.json", "wizard_progress": "tester/wizard_progress.json",
            "tester_config": "config_tester.json", "inbox_root": str(tmp_path / "inbox"),
            "max_parallel_submissions": 7,
        },
    }), encoding="utf-8")
    controller = PanelController(Path(__file__).parents[1], config)

    result = controller.remote_testing_fill({
        "symbols": "BTCUSDT", "side": "SHORT", "start": "2026-07-15", "end": "2026-08-06",
    })

    assert result == {"state": "FILLED", "strategy_name": "safe"}
    assert calls[0]["request"]["side"] == "SHORT"
    assert calls[0]["max_parallel_runs"] == 7
    assert "mrs2.ma_short" in calls[0]["tester_template"]
    assert '"use_short"' in calls[0]["strategy_template"]
    assert "runner.example.test" not in json.dumps(result)
    assert controller.remote_testing_start() == {"state": "STARTED"}
    assert calls[-1] == {"started": True}


def test_panel_controller_remote_fill_fails_closed_when_worker_config_is_invalid(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeRemoteService:
        def __init__(self, _document):
            pass

        def fill(self, **_kwargs):
            pytest.fail("remote upload must not start")

    monkeypatch.setattr(panel_module, "RemoteTestingService", FakeRemoteService)
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({
        "remote_runner": _config(),
        "tester_runner": {
            "bot_root": str(tmp_path / "hb"), "executable": "hb_c.exe",
            "base_url": "http://127.0.0.1:8087", "port": 8087,
            "strategy_dir": "settings_strategy", "report_dir": "tester/report/my_test",
            "wizard_result": "tester/wizard_result.json", "wizard_progress": "tester/wizard_progress.json",
            "tester_config": "config_tester.json", "inbox_root": str(tmp_path / "inbox"),
            "max_parallel_submissions": True,
        },
    }), encoding="utf-8")

    with pytest.raises(PanelTestingError, match="invalid tester configuration"):
        PanelController(Path(__file__).parents[1], config).remote_testing_fill({
            "symbols": "BTCUSDT", "side": "LONG", "start": "2026-07-15", "end": "2026-08-06",
        })


def test_remote_start_is_rejected_until_this_controller_fills_the_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRemoteService:
        def __init__(self, _document):
            pass

        def start(self):
            pytest.fail("must not start before fill")

    monkeypatch.setattr(panel_module, "RemoteTestingService", FakeRemoteService)
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"remote_runner": _config()}), encoding="utf-8")

    with pytest.raises(Exception, match="invalid testing request"):
        PanelController(Path(__file__).parents[1], config).remote_testing_start()


def test_check_paths_uses_one_injected_plink_argv_and_returns_only_labels() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        return "1\n1\n0\n1\n1\n1048576\n"

    service = _owned_service(command_runner=runner)
    result = service.check_paths()

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "plink"
    assert "-batch" in argv
    assert "-pw" in argv and argv[argv.index("-pw") + 1] == "correct horse battery staple"
    command = argv[-1]
    assert command.count("test -d") == 5
    assert "df -Pk" in command
    assert result == {
        "paths": {
            "bot_root": True,
            "debian_runner_root": True,
            "reports_root": False,
            "source_db_root": True,
            "reports_archive_root": True,
        },
        "source_db_root_exists": True,
        "disk_free_bytes": 1048576 * 1024,
    }
    assert "/opt/hb1" not in json.dumps(result)
    assert "correct horse battery staple" not in json.dumps(result)


def test_check_paths_reports_free_space_on_reports_filesystem() -> None:
    def runner(_argv: tuple[str, ...]) -> str:
        return "1\n1\n1\n1\n1\n1048576\n"

    service = _owned_service(command_runner=runner)

    assert service.check_paths()["disk_free_bytes"] == 1048576 * 1024


def test_check_paths_keeps_path_result_when_disk_probe_is_unavailable() -> None:
    def runner(_argv: tuple[str, ...]) -> str:
        return "1\n1\n1\n1\n1\n"

    service = _owned_service(command_runner=runner)

    assert service.check_paths()["disk_free_bytes"] == 0


def test_remote_command_failure_is_generic_and_does_not_echo_secret() -> None:
    secret = "correct horse battery staple"

    def runner(_argv: tuple[str, ...]) -> str:
        raise RuntimeError(secret)

    service = RemoteTestingService(load_remote_runner_config(_config()), command_runner=runner)

    with pytest.raises(ValueError, match="remote command failed") as raised:
        service.check_paths()
    assert secret not in str(raised.value)


@pytest.mark.parametrize("output, expected", [("RUNNING\n", "RUNNING"), ("STARTED\n", "STARTED")])
def test_start_uses_verified_binary_and_fixed_log_without_http_assumptions(
    output: str, expected: str
) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        return output

    service = _owned_service(command_runner=runner)
    result = service.start()

    assert result == {"state": expected}
    command = calls[0][-1]
    assert "pgrep" in command
    assert "readlink" in command
    assert "nohup" in command
    assert "/opt/hb1/hb_c" in command
    assert "/opt/hb1/.mrs3-panel-tester.log" in command
    assert "8087" not in command


def test_remote_mutation_fails_before_io_without_ownership_adapter() -> None:
    calls: list[tuple[str, ...]] = []
    service = RemoteTestingService(
        load_remote_runner_config(_config()), command_runner=lambda argv: calls.append(argv) or "STARTED\n"
    )
    with pytest.raises(ValueError, match="remote ownership capability unavailable"):
        service.start()
    assert calls == []


def test_remote_attestation_and_release_fail_closed() -> None:
    class BadAttestation(_FakeOwnership):
        released = False

        def attest(self, lease: object, target: str) -> bool:
            return False

        def release(self, _lease: object) -> bool:
            self.released = True
            return True

    calls: list[tuple[str, ...]] = []
    ownership = BadAttestation()
    service = RemoteTestingService(
        load_remote_runner_config(_config()),
        command_runner=lambda argv: calls.append(argv) or "STARTED\n",
        ownership_adapter=ownership,
    )
    with pytest.raises(ValueError, match="attestation failed"):
        service.start()
    assert calls == []
    assert ownership.released is True

    class BadRelease(_FakeOwnership):
        def release(self, _lease: object) -> bool:
            return False

    service = RemoteTestingService(
        load_remote_runner_config(_config()), command_runner=lambda _argv: "STOPPED\n",
        ownership_adapter=BadRelease(),
    )
    with pytest.raises(ValueError, match="release failed"):
        service.stop()
    assert service._ownership_lease is not None


def test_stop_fails_closed_when_executable_verification_is_unavailable() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        return "VERIFY_FAILED\n"

    service = _owned_service(command_runner=runner)

    assert service.stop() == {"state": "FAILED"}
    command = calls[0][-1]
    assert "/proc/" in command
    assert "readlink" in command
    assert "kill" in command


def test_stop_signals_only_verified_binary_processes() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        return "STOPPED\n"

    service = _owned_service(command_runner=runner)

    assert service.stop() == {"state": "STOPPED"}
    command = calls[0][-1]
    assert '"$exe" = "$bin"' in command
    assert 'kill -TERM "$pid"' in command


def _fill_request() -> dict[str, object]:
    return prepare_request(
        symbols=("BTCUSDT", "ETHUSDT"),
        side="SHORT",
        start="2026-07-15",
        end="2026-08-06",
    )


def _tester_template() -> str:
    return json.dumps(
        {
            "StartDate": "old",
            "EndDate": "old",
            "parameter_mining": [
                {"name": "settings[*].basic.symbol", "values": ["OLDUSDT"]},
                {"name": "settings[*].mrs2.ma_long.len", "values": []},
            ],
        }
    )


def _strategy_template() -> str:
    return json.dumps(
        {
            "name": "AAOIUSDT",
            "basic": {"symbol": "OLDUSDT", "use_long": True, "use_short": False},
            "keep": {"untouched": True},
        }
    )


def test_fill_uploads_rendered_files_then_one_redacted_install_action() -> None:
    uploaded: list[tuple[str, str, str]] = []
    commands: list[tuple[str, ...]] = []

    def uploader(local_path: Path, remote_path: str, config: RemoteRunnerConfig) -> None:
        assert config.host == "runner.example.test"
        assert local_path.is_file()
        uploaded.append((local_path.name, local_path.read_text(encoding="utf-8"), remote_path))

    def runner(argv: tuple[str, ...]) -> str:
        commands.append(argv)
        return "FILLED\n" if "stage_created=0" in argv[-1] else "STOPPED\n"

    service = _owned_service(command_runner=runner, file_uploader=uploader)
    result = service.fill(
        _fill_request(),
        tester_template=_tester_template(),
        strategy_template=_strategy_template(),
        max_parallel_runs=7,
    )

    assert result == {
        "state": "FILLED",
        "strategy_name": "AAOIUSDT",
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "side": "SHORT",
        "report_archive_folder": "BTC_ETH_2026-07-15_2026-08-06",
    }
    assert len(uploaded) == 2
    uploaded_by_suffix = {name: (content, remote) for name, content, remote in uploaded}
    rendered_config, config_destination = uploaded_by_suffix["config_tester.json"]
    rendered_strategy, strategy_destination = uploaded_by_suffix["AAOIUSDT.json"]
    assert json.loads(rendered_config)["StartDate"] == "2026-07-15T00:00:00"
    assert json.loads(rendered_config)["max_parallel_runs"] == 7
    assert json.loads(rendered_config)["parameter_mining"][0]["values"] == ["BTCUSDT", "ETHUSDT"]
    strategy = json.loads(rendered_strategy)
    assert strategy["basic"] == {"symbol": "BTCUSDT", "use_long": False, "use_short": True}
    assert config_destination.startswith("/opt/hb1/")
    assert strategy_destination.startswith("/opt/hb1/")
    command = commands[-1][-1]
    assert "mkdir" in command and "backup" in command
    assert "config_tester.json" in command
    assert "settings_strategy" in command
    assert "find" in command and "*.json" in command
    assert "FILLED" in command and "FAILED" in command
    encoded = json.dumps(result)
    assert "/opt/hb1" not in encoded
    assert "correct horse battery staple" not in encoded


def test_remote_fill_holds_owner_until_stop_restores_settings() -> None:
    ownership = _FakeOwnership()

    def runner(argv: tuple[str, ...]) -> str:
        script = argv[-1]
        if "printf 'FILLED" in script:
            return "FILLED\n"
        if "nohup" in script:
            return "STARTED\n"
        return "STOPPED\n"

    service = RemoteTestingService(
        load_remote_runner_config(_config()), command_runner=runner,
        file_uploader=lambda *_args: None, ownership_adapter=ownership,
    )
    service.fill(
        _fill_request(), tester_template=_tester_template(), strategy_template=_strategy_template(),
        max_parallel_runs=1,
    )
    assert service._ownership_lease is not None
    assert ownership.restored is False
    assert service.start() == {"state": "STARTED"}
    assert service.stop() == {"state": "STOPPED"}
    assert ownership.restored is True
    assert service._ownership_lease is None


def test_remote_failed_start_keeps_owner_and_snapshot() -> None:
    ownership = _FakeOwnership()
    service = RemoteTestingService(
        load_remote_runner_config(_config()), command_runner=lambda _argv: "VERIFY_FAILED\n",
        file_uploader=lambda *_args: None, ownership_adapter=ownership,
    )
    service._ownership_lease = ownership.acquire(service.target_identity)
    service._settings_snapshot = ownership.snapshot_settings(service._ownership_lease, service.target_identity)

    assert service.start() == {"state": "FAILED"}
    assert service._ownership_lease is not None
    assert ownership.restored is False


def test_remote_failed_stop_keeps_owner_and_snapshot() -> None:
    ownership = _FakeOwnership()
    service = RemoteTestingService(
        load_remote_runner_config(_config()), command_runner=lambda _argv: "STOP_FAILED\n",
        file_uploader=lambda *_args: None, ownership_adapter=ownership,
    )
    service._ownership_lease = ownership.acquire(service.target_identity)
    service._settings_snapshot = ownership.snapshot_settings(service._ownership_lease, service.target_identity)

    assert service.stop() == {"state": "FAILED"}
    assert service._ownership_lease is not None
    assert ownership.restored is False


def test_remote_fill_disconnect_keeps_owner_and_snapshot() -> None:
    ownership = _FakeOwnership()
    service = RemoteTestingService(
        load_remote_runner_config(_config()),
        command_runner=lambda argv: (
            (_ for _ in ()).throw(RuntimeError("disconnect"))
            if "stage_created=0" in argv[-1] else "STOPPED\n"
        ),
        file_uploader=lambda *_args: None,
        ownership_adapter=ownership,
    )

    with pytest.raises(ValueError, match="remote command failed"):
        service.fill(
            _fill_request(), tester_template=_tester_template(),
            strategy_template=_strategy_template(), max_parallel_runs=1,
        )

    assert service._ownership_lease is not None
    assert ownership.restored is False


def test_fill_requires_configured_worker_count() -> None:
    service = RemoteTestingService(load_remote_runner_config(_config()))

    with pytest.raises(ValueError, match="invalid remote testing template"):
        service.fill(
            _fill_request(), tester_template=_tester_template(), strategy_template=_strategy_template()
        )


def test_default_uploader_keeps_port_and_password_as_separate_argv_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import mrs3.panel_remote_testing as remote_module

    captured: list[tuple[str, ...]] = []

    class Completed:
        returncode = 0

    monkeypatch.setattr(
        remote_module.subprocess,
        "run",
        lambda argv, **_kwargs: captured.append(tuple(argv)) or Completed(),
    )
    source = tmp_path / "one.json"
    source.write_text("{}", encoding="utf-8")
    config = load_remote_runner_config(_config(port=2222))

    remote_module._default_file_uploader(source, "/opt/hb1/upload.json", config)

    assert captured[0][:4] == ("pscp", "-batch", "-P", "2222")
    assert captured[0][4:6] == ("-pw", "correct horse battery staple")


def test_fill_rejects_unsafe_strategy_filename_without_uploading() -> None:
    uploaded: list[Path] = []

    def uploader(local_path: Path, _remote_path: str, _config: RemoteRunnerConfig) -> None:
        uploaded.append(local_path)

    service = RemoteTestingService(load_remote_runner_config(_config()), file_uploader=uploader)
    unsafe = json.dumps({"name": "../../escape", "basic": {}})

    with pytest.raises(ValueError, match="invalid remote testing template"):
        service.fill(
            _fill_request(), tester_template=_tester_template(), strategy_template=unsafe,
            max_parallel_runs=1,
        )
    assert uploaded == []


def test_fill_cleans_only_its_uploaded_files_after_upload_failure() -> None:
    commands: list[tuple[str, ...]] = []

    def uploader(local_path: Path, _remote_path: str, _config: RemoteRunnerConfig) -> None:
        if local_path.name.endswith(".json"):
            raise RuntimeError("network failed")

    def runner(argv: tuple[str, ...]) -> str:
        commands.append(argv)
        return "STOPPED\n"

    service = _owned_service(command_runner=runner, file_uploader=uploader)

    with pytest.raises(ValueError, match="remote upload failed"):
        service.fill(
            _fill_request(), tester_template=_tester_template(), strategy_template=_strategy_template(),
            max_parallel_runs=1,
        )

    cleanup = [argv[-1] for argv in commands if argv[-1].startswith("rm -f -- ")]
    assert len(cleanup) == 1
    assert cleanup[0].startswith("rm -f -- '/opt/hb1/.mrs3-panel-upload-")


def test_read_progress_parses_last_bounded_marker_without_returning_log_text() -> None:
    secret = "correct horse battery staple"

    def runner(_argv: tuple[str, ...]) -> str:
        return f"first 1 из 8\nsecond 2 of 4 secret={secret}\n"

    service = RemoteTestingService(load_remote_runner_config(_config()), command_runner=runner)
    result = service.read_progress()

    assert result == {
        "current": 2,
        "total": 4,
        "percent": 50.0,
        "elapsed": None,
        "message": "2/4",
    }
    assert secret not in json.dumps(result)


def test_read_progress_without_marker_returns_zero_null_and_tails_fixed_log() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        return "arbitrary status text"

    service = RemoteTestingService(load_remote_runner_config(_config()), command_runner=runner)
    result = service.read_progress()

    assert result == {"current": 0, "total": 0, "percent": None, "elapsed": None, "message": None}
    assert "tail" in calls[0][-1]
    assert "/opt/hb1/.mrs3-panel-tester.log" in calls[0][-1]
