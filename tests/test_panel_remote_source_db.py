from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from mrs3.panel_remote_source_db import (
    RemoteSourceDbError,
    RemoteSourceDbExecutor,
    RemoteSourceDbService,
)
from mrs3.panel import PanelController
from mrs3.panel_remote_testing import RemoteRunnerConfig


REMOTE_ROOT = "/opt/hb1/source-db"
REMOTE_HTML = "/opt/hb1/reports-archive/run-set"
REMOTE_TARGET = f"{REMOTE_ROOT}/run-set.source-v6.duckdb"


def _service(data: bytes = b"duckdb bytes") -> RemoteSourceDbService:
    digest = hashlib.sha256(data).hexdigest()

    def read_remote_evidence(remote_path: str) -> dict[str, object]:
        assert remote_path == REMOTE_TARGET
        return {"size_bytes": len(data), "sha256": digest}

    def download(remote_path: str, temp_path: Path) -> None:
        assert remote_path == REMOTE_TARGET
        temp_path.write_bytes(data)

    return RemoteSourceDbService(
        source_db_root=REMOTE_ROOT,
        read_remote_evidence=read_remote_evidence,
        download=download,
    )


@pytest.mark.parametrize(
    ("html_path", "db_path"),
    [
        ("relative/reports", REMOTE_TARGET),
        ("/opt/hb1/reports/../escape", REMOTE_TARGET),
        ("/opt/hb1/reports\\escape", REMOTE_TARGET),
        (REMOTE_HTML, "/opt/hb1/other-db/run.duckdb"),
        (REMOTE_HTML, REMOTE_ROOT),
        (REMOTE_HTML, f"{REMOTE_ROOT}/../escape.duckdb"),
    ],
)
def test_request_rejects_unsafe_remote_paths(html_path: str, db_path: str, tmp_path: Path) -> None:
    service = _service()

    with pytest.raises(RemoteSourceDbError, match="invalid remote source db request") as raised:
        service.prepare_request(html_path, db_path, tmp_path / "final.duckdb")

    assert html_path not in str(raised.value)
    assert db_path not in str(raised.value)


def test_existing_local_target_is_rejected_before_remote_work(tmp_path: Path) -> None:
    target = tmp_path / "final.duckdb"
    target.write_bytes(b"keep me")
    calls: list[str] = []

    service = RemoteSourceDbService(
        source_db_root=REMOTE_ROOT,
        read_remote_evidence=lambda path: calls.append(path),
        download=lambda _remote, _temp: calls.append("download"),
    )

    with pytest.raises(RemoteSourceDbError, match="target already exists") as raised:
        service.run(REMOTE_HTML, REMOTE_TARGET, target)

    assert calls == []
    assert target.read_bytes() == b"keep me"
    assert str(tmp_path) not in str(raised.value)


def test_good_transfer_verifies_and_atomically_commits_basename_only(tmp_path: Path) -> None:
    service = _service(b"verified source database")

    document = service.run(REMOTE_HTML, REMOTE_TARGET, tmp_path / "final.duckdb")

    assert document["phase"] == "COMMITTED"
    assert document["phases"] == ["REMOTE_IMPORTED", "TRANSFERRING", "VERIFIED", "COMMITTED"]
    assert document["remote_db"] == "run-set.source-v6.duckdb"
    assert document["local_target"] == "final.duckdb"
    assert document["evidence"]["size_bytes"] == len(b"verified source database")
    assert (tmp_path / "final.duckdb").read_bytes() == b"verified source database"
    encoded = json.dumps(document)
    assert REMOTE_ROOT not in encoded
    assert REMOTE_HTML not in encoded
    assert str(tmp_path) not in encoded


def test_tampered_transfer_removes_temp_and_leaves_target_absent_without_leaks(
    tmp_path: Path,
) -> None:
    expected = b"expected source database"
    target = tmp_path / "final.duckdb"

    def read_remote_evidence(_path: str) -> dict[str, object]:
        return {"size_bytes": len(expected), "sha256": hashlib.sha256(expected).hexdigest()}

    def download(_remote: str, temp_path: Path) -> None:
        temp_path.write_bytes(b"tampered source database")

    service = RemoteSourceDbService(
        source_db_root=REMOTE_ROOT,
        read_remote_evidence=read_remote_evidence,
        download=download,
    )

    with pytest.raises(RemoteSourceDbError, match="transfer failed") as raised:
        service.run(REMOTE_HTML, REMOTE_TARGET, target)

    assert not target.exists()
    assert tuple(tmp_path.glob("*.part")) == ()
    encoded = str(raised.value)
    assert REMOTE_ROOT not in encoded
    assert REMOTE_HTML not in encoded
    assert str(tmp_path) not in encoded


def _remote_config() -> RemoteRunnerConfig:
    return RemoteRunnerConfig(
        host="runner.example.test",
        user="tester",
        port=22,
        password="correct horse battery staple",
        private_key_path="",
        bot_root="/opt/hb1",
        debian_runner_root="/opt/hb1/debian-duckdb-importer",
        reports_root="/opt/hb1/tester/report",
        source_db_root=REMOTE_ROOT,
        reports_archive_root="/opt/hb1/reports-archive",
    )


def test_executor_builds_argv_safe_script_and_redacted_document(tmp_path: Path) -> None:
    data = b"verified remote source db"
    calls: list[tuple[str, ...]] = []

    def command_runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        return f"EVIDENCE {len(data)} {hashlib.sha256(data).hexdigest()}\n"

    def file_downloader(argv: tuple[str, ...]) -> None:
        calls.append(argv)
        Path(argv[-1]).write_bytes(data)

    executor = RemoteSourceDbExecutor(_remote_config(), command_runner, file_downloader)

    document = executor.run(REMOTE_HTML, REMOTE_TARGET, tmp_path / "final.duckdb")

    assert document["phase"] == "COMMITTED"
    assert calls[0][:6] == ("plink", "-batch", "-ssh", "-P", "22", "-pw")
    assert calls[0][6] == "correct horse battery staple"
    assert calls[0][7] == "tester@runner.example.test"
    assert calls[1][:6] == ("pscp", "-batch", "-P", "22", "-pw", "correct horse battery staple")
    assert calls[1][6] == "tester@runner.example.test:" + REMOTE_TARGET
    script = calls[0][-1]
    assert "mkdir -p" in script
    assert "scripts/import-source-v6-debian.sh" in script
    assert 'if ! sh "$importer" "$html" "$target"' in script
    assert "import_source_v6_debian.py" not in script
    assert "sha256sum" in script and "wc -c" in script
    assert "TARGET_EXISTS" in script
    encoded = json.dumps(document)
    for secret in ("runner.example.test", "tester", "correct horse battery staple", REMOTE_ROOT, REMOTE_HTML, str(tmp_path)):
        assert secret not in encoded


def test_executor_rejects_non_strict_evidence_without_echoing_remote_output(tmp_path: Path) -> None:
    secret = "host=runner.example.test password=correct horse battery staple"
    executor = RemoteSourceDbExecutor(
        _remote_config(),
        lambda _argv: f"noise {secret}\nEVIDENCE 1 {'a' * 64}\n",
        lambda _argv: None,
    )

    with pytest.raises(RemoteSourceDbError, match="remote source db import failed") as raised:
        executor.run(REMOTE_HTML, REMOTE_TARGET, tmp_path / "final.duckdb")

    assert secret not in str(raised.value)


def test_executor_rejects_existing_remote_target_marker_without_local_download(tmp_path: Path) -> None:
    downloaded: list[tuple[str, ...]] = []
    executor = RemoteSourceDbExecutor(
        _remote_config(), lambda _argv: "TARGET_EXISTS\n", lambda argv: downloaded.append(argv)
    )

    with pytest.raises(RemoteSourceDbError, match="target already exists"):
        executor.run(REMOTE_HTML, REMOTE_TARGET, tmp_path / "final.duckdb")

    assert downloaded == []


def test_executor_rejects_html_outside_configured_report_roots(tmp_path: Path) -> None:
    executor = RemoteSourceDbExecutor(_remote_config(), lambda _argv: "", lambda _argv: None)

    with pytest.raises(RemoteSourceDbError, match="invalid remote source db request"):
        executor.run("/opt/hb1/unrelated/reports", REMOTE_TARGET, tmp_path / "final.duckdb")

    with pytest.raises(RemoteSourceDbError, match="invalid remote source db request"):
        executor.start_import("/opt/hb1/unrelated/reports", REMOTE_TARGET)
    assert not (tmp_path / "final.duckdb").exists()


def test_executor_rejects_invalid_runner_config_without_leaking_credentials(tmp_path: Path) -> None:
    invalid = RemoteRunnerConfig(
        host="runner.example.test",
        user="tester",
        port=22,
        password="secret",
        private_key_path="",
        bot_root="/opt/hb1",
        debian_runner_root="/opt/hb1/debian-duckdb-importer",
        reports_root="/opt/hb1/tester/report",
        source_db_root=REMOTE_ROOT,
        reports_archive_root="/opt/hb1/reports-archive",
        enabled=False,
    )

    with pytest.raises(RemoteSourceDbError, match="invalid remote runner configuration") as raised:
        RemoteSourceDbExecutor(invalid, lambda _argv: "", lambda _argv: None)

    assert "secret" not in str(raised.value)


def test_remote_import_start_returns_redacted_running_identity_and_fixed_script() -> None:
    calls: list[tuple[str, ...]] = []

    def command_runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        return "STARTED\n"

    executor = RemoteSourceDbExecutor(_remote_config(), command_runner, lambda _argv: None)

    document = executor.start_import(REMOTE_HTML, REMOTE_TARGET)

    assert len(document["job_id"]) == 32
    assert all(character in "0123456789abcdef" for character in document["job_id"])
    assert document["state"] == "RUNNING"
    assert document["phase"] == "REMOTE_IMPORT"
    assert document["progress"] == {"current": 0, "total": 0, "unit": "items"}
    assert document["error"] is None
    script = calls[0][-1]
    assert "mkdir -p" in script and "nohup" in script
    assert "scripts/import-source-v6-debian.sh" in script
    assert 'nohup setsid sh "$importer" "$html" "$target"' in script
    assert "TARGET_EXISTS" in script
    assert "pid" in script and "import.log" in script
    encoded = json.dumps(document)
    assert REMOTE_ROOT not in encoded and REMOTE_TARGET not in encoded
    assert "runner.example.test" not in encoded


def test_remote_import_identity_can_be_rehydrated_for_restart_cancellation() -> None:
    executor = RemoteSourceDbExecutor(_remote_config(), lambda _argv: "", lambda _argv: None)

    job = executor.resume_import("a" * 32, REMOTE_HTML, REMOTE_TARGET)

    assert job["job_id"] == "a" * 32
    assert job["state"] == "RUNNING"


def test_remote_import_status_verifies_identity_then_returns_redacted_evidence() -> None:
    calls: list[tuple[str, ...]] = []
    data = b"remote evidence"

    def command_runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        if len(calls) == 1:
            return "STARTED\n"
        return f"REMOTE_IMPORTED {len(data)} {hashlib.sha256(data).hexdigest()}\n"

    executor = RemoteSourceDbExecutor(_remote_config(), command_runner, lambda _argv: None)
    started = executor.start_import(REMOTE_HTML, REMOTE_TARGET)

    document = executor.status(started["job_id"])

    assert document["state"] == "REMOTE_IMPORTED"
    assert document["phase"] == "REMOTE_IMPORTED"
    assert document["evidence"] == {
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    script = calls[1][-1]
    assert "/proc/" in script and "cmdline" in script
    assert "sha256sum" in script and "wc -c" in script
    assert "scripts/import-source-v6-debian.sh" in script
    assert "import_source_v6_debian.py" in script
    encoded = json.dumps(document)
    assert REMOTE_ROOT not in encoded and REMOTE_TARGET not in encoded
    assert "runner.example.test" not in encoded


def test_remote_import_status_exposes_safe_html_progress_and_elapsed_time() -> None:
    calls: list[tuple[str, ...]] = []

    def command_runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        return "STARTED\n" if len(calls) == 1 else "RUNNING 12 40 4 1700000000\n"

    executor = RemoteSourceDbExecutor(_remote_config(), command_runner, lambda _argv: None)
    started = executor.start_import(REMOTE_HTML, REMOTE_TARGET)

    document = executor.status(started["job_id"])

    assert document["state"] == "RUNNING"
    assert document["phase"] == "REMOTE_IMPORT"
    assert document["progress"] == {"current": 12, "total": 40, "unit": "reports", "workers": 4}
    assert document["timing"]["started_at_epoch"] == 1700000000
    assert document["timing"]["elapsed_seconds"] >= 0
    assert "--progress" in calls[0][-1]
    assert "progress_file" in calls[1][-1]
    assert REMOTE_ROOT not in json.dumps(document)


def test_remote_import_delivery_requires_remote_imported_state(tmp_path: Path) -> None:
    executor = RemoteSourceDbExecutor(
        _remote_config(), lambda _argv: "STARTED\n", lambda _argv: None
    )
    started = executor.start_import(REMOTE_HTML, REMOTE_TARGET)

    with pytest.raises(RemoteSourceDbError, match="not ready for delivery"):
        executor.deliver_import(started["job_id"], tmp_path / "final.duckdb")


def test_remote_import_delivery_reports_transfer_bytes_then_verifies_before_commit(tmp_path: Path) -> None:
    data = b"verified remote source db"

    class Process:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

    process = Process()
    calls = 0

    def command_runner(_argv: tuple[str, ...]) -> str:
        nonlocal calls
        calls += 1
        return "STARTED\n" if calls == 1 else f"REMOTE_IMPORTED {len(data)} {hashlib.sha256(data).hexdigest()}\n"

    def transfer_starter(argv: tuple[str, ...]) -> Process:
        Path(argv[-1]).write_bytes(data)
        return process

    executor = RemoteSourceDbExecutor(
        _remote_config(), command_runner, lambda _argv: None, transfer_starter=transfer_starter
    )
    started = executor.start_import(REMOTE_HTML, REMOTE_TARGET)
    assert executor.status(started["job_id"])["state"] == "REMOTE_IMPORTED"

    transferring = executor.start_delivery(started["job_id"], tmp_path / "final.duckdb")
    assert transferring["phase"] == "TRANSFERRING"
    assert transferring["progress"] == {"current": len(data), "total": len(data), "unit": "bytes"}

    in_progress = executor.status(started["job_id"])
    assert in_progress["phase"] == "TRANSFERRING"
    assert in_progress["progress"] == {"current": len(data), "total": len(data), "unit": "bytes"}

    process.returncode = 0
    committed = executor.status(started["job_id"])
    assert committed["state"] == "COMMITTED"
    assert committed["phase"] == "COMMITTED"
    assert committed["evidence"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert (tmp_path / "final.duckdb").read_bytes() == data


def test_remote_import_delivery_cancel_terminates_local_transfer_and_removes_partial(tmp_path: Path) -> None:
    data = b"partial db"

    class Process:
        terminated = False
        waited = False

        def poll(self) -> int | None:
            return 0 if self.waited else None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> int:
            assert timeout > 0
            self.waited = True
            return 0

    process = Process()
    calls = 0

    def command_runner(_argv: tuple[str, ...]) -> str:
        nonlocal calls
        calls += 1
        return "STARTED\n" if calls == 1 else f"REMOTE_IMPORTED {len(data)} {hashlib.sha256(data).hexdigest()}\n"

    def transfer_starter(argv: tuple[str, ...]) -> Process:
        Path(argv[-1]).write_bytes(data)
        return process

    executor = RemoteSourceDbExecutor(_remote_config(), command_runner, lambda _argv: None, transfer_starter=transfer_starter)
    started = executor.start_import(REMOTE_HTML, REMOTE_TARGET)
    executor.status(started["job_id"])
    executor.start_delivery(started["job_id"], tmp_path / "final.duckdb")

    cancelled = executor.cancel(started["job_id"])

    assert cancelled["state"] == "CANCELLED"
    assert process.terminated is True
    assert process.waited is True
    assert not tuple(tmp_path.glob("*.part"))
    assert not (tmp_path / "final.duckdb").exists()


def test_remote_import_cancel_never_promotes_completed_race_to_delivery() -> None:
    data = b"remote evidence"
    calls = 0

    def command_runner(_argv: tuple[str, ...]) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "STARTED\n"
        if calls == 2:
            return "CANCELLING\n"
        return f"REMOTE_IMPORTED {len(data)} {hashlib.sha256(data).hexdigest()}\n"

    executor = RemoteSourceDbExecutor(_remote_config(), command_runner, lambda _argv: None)
    started = executor.start_import(REMOTE_HTML, REMOTE_TARGET)
    assert executor.cancel(started["job_id"])["state"] == "CANCELLING"

    status = executor.status(started["job_id"])

    assert status["state"] == "CANCELLED"
    assert status["phase"] == "CANCELLED"


def test_remote_import_delivery_cancel_kills_after_timeout_and_removes_partial(tmp_path: Path) -> None:
    data = b"partial db"

    class Process:
        waits = 0
        killed = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("pscp", timeout)
            return 0

        def kill(self) -> None:
            self.killed = True

    process = Process()
    calls = 0

    def command_runner(_argv: tuple[str, ...]) -> str:
        nonlocal calls
        calls += 1
        return "STARTED\n" if calls == 1 else f"REMOTE_IMPORTED {len(data)} {hashlib.sha256(data).hexdigest()}\n"

    def transfer_starter(argv: tuple[str, ...]) -> Process:
        Path(argv[-1]).write_bytes(data)
        return process

    executor = RemoteSourceDbExecutor(_remote_config(), command_runner, lambda _argv: None, transfer_starter=transfer_starter)
    started = executor.start_import(REMOTE_HTML, REMOTE_TARGET)
    executor.status(started["job_id"])
    executor.start_delivery(started["job_id"], tmp_path / "final.duckdb")

    assert executor.cancel(started["job_id"])["state"] == "CANCELLED"
    assert process.killed is True
    assert not tuple(tmp_path.glob("*.part"))


def test_remote_import_delivery_cancel_cleans_partial_when_termination_errors(tmp_path: Path) -> None:
    data = b"partial db"

    class Process:
        killed = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            raise OSError("terminate failed")

        def wait(self, timeout: float) -> int:
            return 0

        def kill(self) -> None:
            self.killed = True

    process = Process()
    calls = 0

    def command_runner(_argv: tuple[str, ...]) -> str:
        nonlocal calls
        calls += 1
        return "STARTED\n" if calls == 1 else f"REMOTE_IMPORTED {len(data)} {hashlib.sha256(data).hexdigest()}\n"

    def transfer_starter(argv: tuple[str, ...]) -> Process:
        Path(argv[-1]).write_bytes(data)
        return process

    executor = RemoteSourceDbExecutor(_remote_config(), command_runner, lambda _argv: None, transfer_starter=transfer_starter)
    started = executor.start_import(REMOTE_HTML, REMOTE_TARGET)
    executor.status(started["job_id"])
    executor.start_delivery(started["job_id"], tmp_path / "final.duckdb")

    assert executor.cancel(started["job_id"])["state"] == "FAILED"
    assert process.killed is True
    assert not tuple(tmp_path.glob("*.part"))


def test_remote_import_status_fails_closed_when_identity_is_not_owned() -> None:
    calls: list[tuple[str, ...]] = []

    def command_runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        return "STARTED\n" if len(calls) == 1 else "FAILED\n"

    executor = RemoteSourceDbExecutor(_remote_config(), command_runner, lambda _argv: None)
    started = executor.start_import(REMOTE_HTML, REMOTE_TARGET)

    document = executor.status(started["job_id"])

    assert document["state"] == "FAILED"
    assert document["error"] == {"code": "REMOTE_IMPORT_FAILED"}
    assert REMOTE_ROOT not in json.dumps(document)


def test_remote_import_cancel_verifies_exact_identity_before_term() -> None:
    calls: list[tuple[str, ...]] = []

    def command_runner(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        return "STARTED\n" if len(calls) == 1 else "CANCELLING\n"

    executor = RemoteSourceDbExecutor(_remote_config(), command_runner, lambda _argv: None)
    started = executor.start_import(REMOTE_HTML, REMOTE_TARGET)

    document = executor.cancel(started["job_id"])

    assert document["state"] == "CANCELLING"
    assert document["phase"] == "REMOTE_IMPORT_CANCEL"
    script = calls[1][-1]
    assert "cat --" in script
    assert "/proc/" in script and "cmdline" in script
    assert "kill -TERM \"-$pid\"" in script
    assert script.index("kill -TERM") > script.index("cmdline")
    assert REMOTE_ROOT not in json.dumps(document)


def test_controller_rejects_existing_local_target_before_remote_start(tmp_path: Path) -> None:
    class Executor:
        called = False
        def start_import(self, *_args):
            self.called = True
            return {"job_id": "job"}

    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    target = tmp_path / "existing.duckdb"
    target.write_bytes(b"keep")
    controller = PanelController(tmp_path, config)
    executor = Executor()
    controller._remote_source_executor = executor

    with pytest.raises(ValueError, match="local source db target already exists"):
        controller.source_db_remote_start({
            "remote_html_path": REMOTE_HTML,
            "remote_db_target": REMOTE_TARGET,
            "local_target_path": str(target),
        })

    assert executor.called is False


def test_controller_starts_visible_delivery_instead_of_blocking_status_request(tmp_path: Path) -> None:
    class Executor:
        delivery_target: Path | None = None

        def start_import(self, _html: str, _remote: str, *, job_id: str) -> dict[str, object]:
            return {"job_id": job_id, "state": "RUNNING", "phase": "REMOTE_IMPORT", "progress": {}}

        def status(self, job_id: str) -> dict[str, object]:
            return {"job_id": job_id, "state": "REMOTE_IMPORTED", "phase": "REMOTE_IMPORTED", "progress": {}}

        def start_delivery(self, _job_id: str, target: Path) -> dict[str, object]:
            self.delivery_target = target
            return {"job_id": _job_id, "state": "TRANSFERRING", "phase": "TRANSFERRING", "progress": {}}

    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config)
    executor = Executor()
    controller._remote_source_executor = executor
    target = tmp_path / "final.duckdb"
    started = controller.source_db_remote_start({
        "remote_html_path": REMOTE_HTML,
        "remote_db_target": REMOTE_TARGET,
        "local_target_path": str(target),
    })

    status = controller.source_db_remote_status(str(started["job_id"]))

    assert status["state"] == "TRANSFERRING"
    assert executor.delivery_target == target.resolve()
