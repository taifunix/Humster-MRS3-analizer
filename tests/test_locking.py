from __future__ import annotations

import json
from pathlib import Path

import pytest

import mrs3.locking as locking
from mrs3.locking import (
    OwnerEvidence,
    OutputDirectoryBusyError,
    OutputDirectoryLock,
    TesterTargetLock,
    TesterTargetBusyError,
    TesterTargetOwnerUnverifiableError,
    TesterTargetReleaseError,
    canonical_tester_target,
)


def test_second_writer_to_same_output_directory_is_rejected(tmp_path: Path) -> None:
    output = tmp_path / "selection"

    with OutputDirectoryLock(output):
        with pytest.raises(OutputDirectoryBusyError, match="already being written"):
            with OutputDirectoryLock(output):
                pass


def test_lock_is_released_after_writer_finishes(tmp_path: Path) -> None:
    output = tmp_path / "selection"

    with OutputDirectoryLock(output):
        pass
    with OutputDirectoryLock(output):
        pass

    assert (output / ".mrs3-selection.lock").exists()


def test_target_aliases_share_canonical_identity(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    assert canonical_tester_target(target) == canonical_tester_target(target / ".." / target.name)


def test_remote_target_identity_normalizes_host_and_rejects_credentials() -> None:
    assert canonical_tester_target("remote://tester@HOST.EXAMPLE:22/opt/hb/") == (
        "remote://tester@host.example:22/opt/hb"
    )
    with pytest.raises(ValueError, match="credentials"):
        canonical_tester_target("remote://tester:secret@host.example:22/opt/hb")


def test_release_keeps_evidence_when_owner_changed(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    lock = TesterTargetLock(target)
    lock.acquire()
    replacement = OwnerEvidence(
        pid=99999,
        process_start_identity="1.0",
        host_identity=lock.host,
        machine_identity=lock.machine,
        boot_identity=lock.boot,
        container_identity=lock.container,
        pid_namespace_identity=lock.pid_namespace,
        lock_kind=lock.lock_kind,
        canonical_target=lock.target_identity,
        acquisition_token="replacement",
        acquired_at_utc="now",
    )
    lock.path.write_text(json.dumps(replacement.as_dict()), encoding="utf-8")
    with pytest.raises(TesterTargetReleaseError):
        lock.release()
    assert lock.owner is not None
    assert lock.path.exists()


def test_unknown_process_probe_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    lock = TesterTargetLock(target)
    owner = OwnerEvidence(
        pid=99999,
        process_start_identity="1.0",
        host_identity=lock.host,
        machine_identity=lock.machine,
        boot_identity=lock.boot,
        container_identity=lock.container,
        pid_namespace_identity=lock.pid_namespace,
        lock_kind=lock.lock_kind,
        canonical_target=lock.target_identity,
        acquisition_token="owner",
        acquired_at_utc="now",
    )
    lock.path.write_text(json.dumps(owner.as_dict()), encoding="utf-8")
    with pytest.raises(TesterTargetOwnerUnverifiableError):
        TesterTargetLock(target, process_probe=lambda _: None).acquire()


def test_manual_clear_probes_same_host_owner_even_when_boot_drifted(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    lock = TesterTargetLock(target).acquire()
    try:
        owner = json.loads(lock.path.read_text(encoding="utf-8"))
        owner["boot_identity"] = "perturbed-boot"
        lock.path.write_text(json.dumps(owner), encoding="utf-8")
        with pytest.raises(TesterTargetBusyError):
            locking.manual_clear_tester_target_lock(
                target, operator_identity="operator", reason="drift probe"
            )
        assert lock.path.exists()
    finally:
        lock.owner = None
        lock.path.unlink(missing_ok=True)


def test_linux_container_identity_ignores_session_scoped_self_cgroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    self_cgroup = ["0::/session-a"]
    original = locking.Path.read_text

    def read(path: locking.Path, *args: object, **kwargs: object) -> str:
        if str(path) == "/proc/self/cgroup":
            return self_cgroup[0]
        if str(path) == "/proc/1/cgroup":
            return "0::/container-stable"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(locking.sys, "platform", "linux")
    monkeypatch.setattr(locking, "_pid_namespace_identity", lambda _machine: "namespace-1")
    monkeypatch.setattr(locking.Path, "read_text", read)
    first = locking._container_identity()
    self_cgroup[0] = "0::/session-b"
    assert locking._container_identity() == first


def test_non_linux_pid_namespace_uses_stable_host_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(locking.sys, "platform", "darwin")
    assert locking._pid_namespace_identity("machine-a") == "host:machine-a"


def test_os_link_failure_is_typed_on_acquire_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    real_link = locking.os.link

    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("link unsupported")

    monkeypatch.setattr(locking.os, "link", denied)
    with pytest.raises(TesterTargetOwnerUnverifiableError):
        TesterTargetLock(target).acquire()

    monkeypatch.setattr(locking.os, "link", real_link)
    lock = TesterTargetLock(target).acquire()
    monkeypatch.setattr(locking.os, "link", denied)
    with pytest.raises(TesterTargetReleaseError):
        lock.release()
    lock.owner = None
    lock.path.unlink(missing_ok=True)
