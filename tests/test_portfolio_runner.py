from __future__ import annotations

import json
import os
import mrs3.portfolio.runner as runner_module
from pathlib import Path
import subprocess
import sys
import time

import pytest

from mrs3.locking import (
    OwnerEvidence,
    TesterTargetBusyError,
    TesterTargetLock,
    TesterTargetOwnerUnverifiableError,
    TesterTargetReleaseError,
    manual_clear_tester_target_lock,
)
from mrs3.portfolio.runner import (
    ArtifactMismatchError,
    DuplicateRunError,
    M6CommitReadbackProof,
    OperationUnconfirmedError,
    PortfolioModeUnsupported,
    PortfolioCancelled,
    PortfolioTesterRunner,
    PortfolioTimedOut,
    RunManifest,
    RunWorkspace,
    SnapshotError,
    TransportContractError,
)
from mrs3.portfolio.store import PortfolioStore, PortfolioStoreError


def _tester_settings() -> dict[str, object]:
    return {
        "contract": "portfolio_tester_settings_v1",
        "test_start": "2026-08-01T00:00:00Z",
        "test_end": "2026-08-31T00:00:00Z",
        "limiter": {"policy": "fixture-limiter-v1"},
        "account": {"equity": "1000"},
    }


def test_target_owner_records_identity_and_releases(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    with TesterTargetLock(target, host="host-a", machine="machine-a", boot="boot-a", container="container-a", pid_namespace="namespace-a") as lock:
        owner = json.loads(lock.path.read_text(encoding="utf-8"))
        assert owner["pid"] > 0
        assert float(owner["process_start_identity"]) > 0
        assert owner["host_identity"] == "host-a"
        assert owner["boot_identity"] == "boot-a"
        assert owner["acquisition_token"]
    assert not lock.path.exists()


def test_release_never_unlinks_replacement_owner(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    lock = TesterTargetLock(target).acquire()
    replacement = lock.path.read_bytes().replace(
        lock.owner.acquisition_token.encode(), b"0" * len(lock.owner.acquisition_token)
    )
    lock.path.write_bytes(replacement)
    with pytest.raises(TesterTargetReleaseError):
        lock.release()
    assert lock.path.read_bytes() == replacement
    lock.path.unlink()


def test_target_lock_blocks_a_second_process_and_distinct_target_is_independent(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    other = tmp_path / "other"
    target.mkdir()
    other.mkdir()
    release = tmp_path / "release"
    code = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from mrs3.locking import TesterTargetLock\n"
        "lock=TesterTargetLock(Path(sys.argv[1])).acquire()\n"
        "marker=Path(sys.argv[2])\n"
        "while not marker.exists(): time.sleep(0.01)\n"
        "lock.release()\n"
    )
    process = subprocess.Popen([sys.executable, "-c", code, str(target), str(release)])
    lock_path = TesterTargetLock(target).path
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not lock_path.exists() and process.poll() is None:
        time.sleep(0.01)
    try:
        assert lock_path.exists()
        with pytest.raises(TesterTargetBusyError):
            TesterTargetLock(target).acquire()
        with TesterTargetLock(other):
            pass
    finally:
        release.write_text("release", encoding="ascii")
        assert process.wait(timeout=5) == 0


def test_live_foreign_and_unknown_owners_block(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    lock = TesterTargetLock(target, host="host-a", machine="machine-a", boot="boot-a", container="container-a", pid_namespace="namespace-a")
    owner = OwnerEvidence(44, "12.0", "host-a", "machine-a", "boot-a", "container-a", "namespace-a", "tester_target_lease", str(target.resolve()), "token", "now")
    lock.path.write_text(json.dumps(owner.as_dict()), encoding="utf-8")
    with pytest.raises(TesterTargetBusyError):
        TesterTargetLock(target, host="host-a", machine="machine-a", boot="boot-a", container="container-a", pid_namespace="namespace-a", process_probe=lambda _: 12.0).acquire()
    owner = OwnerEvidence(44, "12.0", "host-b", "machine-a", "boot-a", "container-a", "namespace-a", "tester_target_lease", str(target.resolve()), "token", "now")
    lock.path.write_text(json.dumps(owner.as_dict()), encoding="utf-8")
    with pytest.raises(TesterTargetOwnerUnverifiableError):
        TesterTargetLock(target, host="host-a", machine="machine-a", boot="boot-a", container="container-a", pid_namespace="namespace-a", process_probe=lambda _: None).acquire()
    owner = OwnerEvidence(44, "12.0", "host-a", "machine-a", "boot-a", "container-a", "namespace-a", "tester_target_lease", str(target.resolve()), "token", "now")
    lock.path.write_text(json.dumps(owner.as_dict()), encoding="utf-8")
    with TesterTargetLock(target, host="host-a", machine="machine-a", boot="boot-a", container="container-a", pid_namespace="namespace-a", process_probe=lambda _: 99.0):
        pass


def test_manual_clear_requires_attestation_and_writes_audit(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    lock = TesterTargetLock(target, host="h", boot="b")
    lock.acquire()
    audit = manual_clear_tester_target_lock(
        target, operator_identity="operator", reason="retired host", attestation="ticket-1"
    )
    assert not lock.path.exists()
    assert json.loads(audit.read_text(encoding="utf-8"))["reason"] == "LOCK_MANUAL_CLEAR"


def test_manual_clear_never_removes_a_live_local_owner(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    lock = TesterTargetLock(target).acquire()
    try:
        with pytest.raises(TesterTargetBusyError):
            manual_clear_tester_target_lock(
                target, operator_identity="operator", reason="must not bypass live owner"
            )
        assert lock.path.exists()
    finally:
        lock.release()


def test_workspace_manifest_hash_and_cleanup_gate(tmp_path: Path) -> None:
    workspace = RunWorkspace.create(tmp_path / "runs", "run-1", "attempt-1")
    artifact = workspace.write_file("input.json", b"{}", "strategy")
    manifest = RunManifest(
        "run-1", "attempt-1", "target", ("A",), ("A",), "b", "s", "t", (artifact,)
    )
    manifest_path = workspace.write_manifest(manifest)
    loaded, document = RunWorkspace.load(manifest_path)
    assert loaded.artifacts[0].sha256 == artifact.sha256
    assert document["members"] == ["A"]
    assert loaded.cleanup(True, manifest) is False
    assert workspace.root.exists()
    proof = M6CommitReadbackProof("run-1", "attempt-1", {})
    assert loaded.cleanup(proof, manifest) is False
    assert workspace.root.exists()


def test_m6_cleanup_requires_semantic_versions_and_replay_counts(tmp_path: Path) -> None:
    workspace = RunWorkspace.create(tmp_path / "runs", "m6-proof", "attempt")
    artifact = workspace.write_file("report_A.html", b"report", "report")
    manifest = RunManifest(
        "m6-proof", "attempt", "target", ("A",), ("A",), "b", "s", "t", (artifact,)
    )
    workspace.write_manifest(manifest)
    digest = artifact.sha256
    incomplete = M6CommitReadbackProof(
        "m6-proof", "attempt", {"A": digest}, normalized_facts_complete=True,
    )
    assert workspace.cleanup(incomplete, manifest) is False
    complete = M6CommitReadbackProof(
        "m6-proof", "attempt", {"A": digest}, normalized_facts_complete=True,
        parser_version="parser-v1", metrics_version="metrics-v1",
        semantic_digests={"A": "a" * 64},
        replay_fact_counts={"reports": 1, "actions": 0, "series": 0, "cycles": 0},
    )
    assert workspace.cleanup(complete, manifest) is True


def test_duplicate_committed_attempts_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    runs = tmp_path / "runs"
    for attempt in ("attempt-a", "attempt-b"):
        workspace = RunWorkspace.create(runs, "duplicate", attempt)
        workspace.write_manifest(
            RunManifest(
                "duplicate",
                attempt,
                TesterTargetLock(target).target_identity,
                ("A",),
                ("A",),
                "bin",
                "set",
                "ticks",
                workspace.artifacts,
                "COMMITTED",
            )
        )

    with pytest.raises(DuplicateRunError, match="multiple committed attempts"):
        PortfolioTesterRunner(target, runs, _FakeTransport()).run(
            members=("A",),
            strategies={},
            expected_reports=("A",),
            tester_settings=_tester_settings(),
            run_id="duplicate",
        )


def test_recovered_attempt_is_not_discovered_as_incomplete(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    runs = tmp_path / "runs"
    workspace = RunWorkspace.create(runs, "recovered", "attempt")
    workspace.write_manifest(
        RunManifest(
            "recovered",
            "attempt",
            TesterTargetLock(target).target_identity,
            ("A",),
            ("A",),
            "bin",
            "set",
            "ticks",
            workspace.artifacts,
            "RECOVERED",
        )
    )
    assert PortfolioTesterRunner(target, runs, _FakeTransport()).discover_incomplete() == ()


class _FakeTransport:
    def __init__(self, *, reports: dict[str, object] | None = None, portfolio: bool = True) -> None:
        self.portfolio = portfolio
        self.reports = reports or {"A": b"report"}
        self.installed = False
        self.restored: list[object] = []
        self.report_evidence: dict[str, object] = {}

    def capability_manifest(self) -> dict[str, object]:
        return {
            "portfolio_mode": self.portfolio,
            "transport_contract": "portfolio_transport_v1",
            "cancellable_operations": True,
            "cancellation_signal": "cancel_event",
            "binary_identity": "bin",
            "settings_identity": "set",
            "tick_identity": "ticks",
        }

    def identities(self) -> dict[str, str]:
        return {"binary": "bin", "settings": "set", "ticks": "ticks"}

    def snapshot_settings(self, run_id: str, attempt_id: str) -> dict[str, object]:
        return {"maker_fee": "0", "run_id": run_id, "attempt_id": attempt_id}

    def ensure_stopped(self, run_id: str, attempt_id: str) -> dict[str, object]:
        return {"run_id": run_id, "attempt_id": attempt_id, "stopped": True}

    def restore_settings(self, snapshot: object, run_id: str, attempt_id: str) -> dict[str, object]:
        self.restored.append(snapshot)
        return {"run_id": run_id, "attempt_id": attempt_id, "restored": True}

    def install(self, workspace: Path, members: tuple[str, ...], run_id: str, attempt_id: str) -> dict[str, object]:
        self.installed = True
        settings = workspace / "tester_settings.json"
        document = json.loads(settings.read_text(encoding="utf-8"))
        self.report_evidence = {
            "test_start": document["test_start"],
            "test_end": document["test_end"],
            "tester_settings_sha256": __import__("hashlib").sha256(settings.read_bytes()).hexdigest(),
            "strategy_sha256": {
                member: __import__("hashlib").sha256((workspace / f"strategy_{member}.json").read_bytes()).hexdigest()
                for member in members
            },
        }
        return {"run_id": run_id, "attempt_id": attempt_id, "installed": True}

    def run(self, run_id: str, attempt_id: str, members: tuple[str, ...], timeout_seconds: float | None, cancel_event: object) -> dict[str, object]:
        assert self.installed
        return {
            "reports": {
                name: {
                    "bytes": value,
                    "metadata": {
                        "contract": "portfolio_report_v1",
                        "version": 1,
                        "member": name,
                        "run_id": run_id,
                        "attempt_id": attempt_id,
                        "sha256": __import__("hashlib").sha256(value).hexdigest(),
                        "size": len(value),
                        "complete": True,
                        "test_start": self.report_evidence["test_start"],
                        "test_end": self.report_evidence["test_end"],
                        "tester_settings_sha256": self.report_evidence["tester_settings_sha256"],
                        "strategy_sha256": self.report_evidence["strategy_sha256"].get(name, "wrong"),
                    },
                }
                for name, value in self.reports.items() if isinstance(value, bytes)
            }
        }


def test_portfolio_runner_is_separate_and_restores_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text('{"name":"A"}', encoding="utf-8")
    transport = _FakeTransport()
    runner = PortfolioTesterRunner(target, tmp_path / "runs", transport)
    result = runner.run(
        members=("A",),
        strategies={"A": source},
        expected_reports=("A",),
        binary_identity="bin",
        settings_identity="set",
        tick_identity="ticks",
        tester_settings=_tester_settings(),
    )
    assert result.status == "COMPLETED"
    assert result.reports[0].read_bytes() == b"report"
    assert transport.restored
    assert json.loads(result.manifest_path.read_text(encoding="utf-8"))["attempt_id"] == result.attempt_id

    bad = _FakeTransport(reports={"wrong": b"late"})
    with pytest.raises(ArtifactMismatchError):
        PortfolioTesterRunner(target, tmp_path / "runs", bad).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            binary_identity="bin", settings_identity="set", tick_identity="ticks",
            tester_settings=_tester_settings(),
        )
    assert bad.restored


def test_restore_evidence_is_durable_before_release_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    original_release = TesterTargetLock.release
    calls = 0

    def fail_first_release(lock: TesterTargetLock) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TesterTargetReleaseError("injected release failure", owner=lock.owner)
        original_release(lock)

    monkeypatch.setattr(TesterTargetLock, "release", fail_first_release)
    transport = _FakeTransport()
    runner = PortfolioTesterRunner(target, tmp_path / "runs", transport)
    with pytest.raises(TesterTargetReleaseError):
        runner.run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), run_id="release-failure",
        )

    manifest_path = next((tmp_path / "runs" / "release-failure").glob("*/run_manifest.json"))
    workspace, document = RunWorkspace.load(manifest_path)
    assert document["status"] == "READY_TO_RELEASE"
    assert (workspace.root / "restore_evidence.json").is_file()
    assert len(transport.restored) == 1
    assert not TesterTargetLock(target).path.exists()


def test_recovery_after_release_does_not_restore_stale_snapshot_to_new_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    transport = _FakeTransport()
    runner = PortfolioTesterRunner(target, tmp_path / "runs", transport)
    original_transition = runner._transition

    def fail_terminal(workspace: RunWorkspace, manifest: RunManifest, status: str) -> RunManifest:
        if status == "COMPLETED":
            raise RuntimeError("injected terminal transition failure")
        return original_transition(workspace, manifest, status)

    monkeypatch.setattr(runner, "_transition", fail_terminal)
    with pytest.raises(RuntimeError, match="terminal transition"):
        runner.run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), run_id="terminal-failure",
        )

    manifest_path = next((tmp_path / "runs" / "terminal-failure").glob("*/run_manifest.json"))
    workspace, document = RunWorkspace.load(manifest_path)
    assert document["status"] == "READY_TO_RELEASE"
    assert len(transport.restored) == 1

    with pytest.raises(DuplicateRunError):
        runner.run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), run_id="terminal-failure",
        )

    new_owner = TesterTargetLock(target).acquire()
    try:
        with pytest.raises(SnapshotError):
            runner.recover(manifest_path)
        assert len(transport.restored) == 1
        assert json.loads(workspace.manifest_path.read_text(encoding="utf-8"))["status"] == "READY_TO_RELEASE"
    finally:
        new_owner.release()


def test_portfolio_runner_fails_closed_without_capability(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    transport = _FakeTransport(portfolio=False)
    with pytest.raises(PortfolioModeUnsupported):
        PortfolioTesterRunner(target, tmp_path / "runs", transport).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(),
        )
    assert not transport.installed


def test_portfolio_runner_requires_exact_period_limiter_and_account_settings(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")

    with pytest.raises(TransportContractError, match="tester settings"):
        PortfolioTesterRunner(target, tmp_path / "runs", _FakeTransport()).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",)
        )


def test_transport_signature_is_strict_and_snapshot_is_before_install(tmp_path: Path) -> None:
    class MissingAttempt(_FakeTransport):
        def snapshot_settings(self) -> dict[str, object]:
            return {"snapshot": True}

    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    transport = MissingAttempt()
    with pytest.raises(TransportContractError):
        PortfolioTesterRunner(target, tmp_path / "runs", transport).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(),
        )
    assert not transport.installed


def test_missing_process_stop_attestation_retains_owner_before_install(tmp_path: Path) -> None:
    class MissingStop(_FakeTransport):
        ensure_stopped = None

    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    transport = MissingStop()

    with pytest.raises(OperationUnconfirmedError, match="stop attestation"):
        PortfolioTesterRunner(target, tmp_path / "runs", transport).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(),
        )

    assert not transport.installed
    assert TesterTargetLock(target).path.exists()


def test_timeout_signals_and_joins_blocking_fake(tmp_path: Path) -> None:
    class Blocking(_FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.stopped = False

        def run(self, run_id: str, attempt_id: str, members: tuple[str, ...], timeout_seconds: float | None, cancel_event: object) -> dict[str, object]:
            event = cancel_event
            while not event.is_set():  # type: ignore[union-attr]
                time.sleep(0.005)
            self.stopped = True
            return {"reports": {}}

    import time
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    transport = Blocking()
    with pytest.raises(PortfolioTimedOut):
        PortfolioTesterRunner(target, tmp_path / "runs", transport).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), timeout_seconds=0.03,
        )
    assert transport.stopped
    assert transport.restored


def test_bytes_without_versioned_report_metadata_are_rejected(tmp_path: Path) -> None:
    class Unverified(_FakeTransport):
        def run(self, run_id: str, attempt_id: str, members: tuple[str, ...], timeout_seconds: float | None, cancel_event: object) -> dict[str, object]:
            return {"reports": {"A": b"arbitrary"}}

    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ArtifactMismatchError):
        PortfolioTesterRunner(target, tmp_path / "runs", Unverified()).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(),
        )


def test_owned_report_path_without_execution_metadata_is_rejected(tmp_path: Path) -> None:
    class UnverifiedPath(_FakeTransport):
        def capability_manifest(self) -> dict[str, object]:
            return {**super().capability_manifest(), "allowed_output_root": str(tmp_path / "allowed")}

        def run(self, run_id: str, attempt_id: str, members: tuple[str, ...], timeout_seconds: float | None, cancel_event: object) -> dict[str, object]:
            report = tmp_path / "allowed" / run_id / attempt_id / "A.html"
            report.parent.mkdir(parents=True)
            report.write_bytes(b"report")
            return {"reports": {"A": str(report)}}

    target = tmp_path / "tester"
    target.mkdir()
    (tmp_path / "allowed").mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")

    with pytest.raises(ArtifactMismatchError, match="metadata"):
        PortfolioTesterRunner(target, tmp_path / "runs", UnverifiedPath()).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(),
        )


@pytest.mark.parametrize("case", ["missing", "external", "stale"])
def test_report_paths_must_be_fresh_regular_files_inside_allowed_root(tmp_path: Path, case: str) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    report = (tmp_path if case == "external" else allowed) / "A.html"
    if case != "missing":
        report.write_bytes(b"report")
    if case == "stale":
        os.utime(report, ns=(1, 1))

    class PathTransport(_FakeTransport):
        def capability_manifest(self) -> dict[str, object]:
            return {**super().capability_manifest(), "allowed_output_root": str(allowed)}

        def run(self, run_id: str, attempt_id: str, members: tuple[str, ...], timeout_seconds: float | None, cancel_event: object) -> dict[str, object]:
            return {
                "reports": {"A": str(report)},
                "report_metadata": {"A": {
                    "contract": "portfolio_report_v1", "version": 1, "member": "A",
                    "run_id": run_id, "attempt_id": attempt_id,
                    "sha256": __import__("hashlib").sha256(b"report").hexdigest(),
                    "size": len(b"report"), "complete": True,
                }},
            }

    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ArtifactMismatchError):
        PortfolioTesterRunner(target, tmp_path / "runs", PathTransport()).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(),
        )


def test_report_metadata_rejects_wrong_attempt_and_expected_set_must_cover_members(tmp_path: Path) -> None:
    class WrongAttempt(_FakeTransport):
        def run(self, run_id: str, attempt_id: str, members: tuple[str, ...], timeout_seconds: float | None, cancel_event: object) -> dict[str, object]:
            value = super().run(run_id, attempt_id, members, timeout_seconds, cancel_event)
            value["reports"]["A"]["metadata"]["attempt_id"] = "late-attempt"
            return value

    target = tmp_path / "tester"
    target.mkdir()
    first = tmp_path / "A.json"
    second = tmp_path / "B.json"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    runner = PortfolioTesterRunner(target, tmp_path / "runs", WrongAttempt())
    with pytest.raises(ArtifactMismatchError):
        runner.run(
            members=("A",), strategies={"A": first}, expected_reports=("A",),
            tester_settings=_tester_settings(),
        )
    with pytest.raises(ValueError, match="exactly cover"):
        runner.run(
            members=("A", "B"), strategies={"A": first, "B": second}, expected_reports=("A",)
        )


@pytest.mark.parametrize("field", ["test_end", "tester_settings_sha256", "strategy_sha256"])
def test_report_metadata_must_match_period_and_installed_settings(tmp_path: Path, field: str) -> None:
    class WrongEvidence(_FakeTransport):
        def run(self, run_id: str, attempt_id: str, members: tuple[str, ...], timeout_seconds: float | None, cancel_event: object) -> dict[str, object]:
            value = super().run(run_id, attempt_id, members, timeout_seconds, cancel_event)
            value["reports"]["A"]["metadata"][field] = "wrong"
            return value

    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")

    with pytest.raises(ArtifactMismatchError, match="execution evidence"):
        PortfolioTesterRunner(target, tmp_path / "runs", WrongEvidence()).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(),
        )


def test_retry_allocates_new_attempt_and_committed_run_is_idempotent(tmp_path: Path) -> None:
    class Flaky(_FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True
            self.calls = 0

        def install(self, workspace: Path, members: tuple[str, ...], run_id: str, attempt_id: str) -> dict[str, object]:
            self.calls += 1
            if self.fail:
                self.fail = False
                raise RuntimeError("install failed")
            return super().install(workspace, members, run_id, attempt_id)

    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    transport = Flaky()
    runner = PortfolioTesterRunner(target, tmp_path / "runs", transport)
    with pytest.raises(RuntimeError):
        runner.run(members=("A",), strategies={"A": source}, expected_reports=("A",), tester_settings=_tester_settings(), run_id="same")
    result = runner.run(members=("A",), strategies={"A": source}, expected_reports=("A",), tester_settings=_tester_settings(), run_id="same")
    assert result.attempt_id != ""
    calls = transport.calls
    with pytest.raises(DuplicateRunError):
        runner.run(members=("A",), strategies={"A": source}, expected_reports=("A",), tester_settings=_tester_settings(), run_id="same")
    assert transport.calls == calls


def test_corrupt_prior_attempt_blocks_same_run_id(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "runs" / "same" / "attempt" / "run_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{broken", encoding="utf-8")

    runner = PortfolioTesterRunner(target, tmp_path / "runs", _FakeTransport())
    with pytest.raises(ArtifactMismatchError, match="prior attempt"):
        runner.run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), run_id="same",
        )
    with pytest.raises(ArtifactMismatchError, match="prior attempt"):
        runner.discover_incomplete()


def test_uncooperative_cancel_retains_target_owner_and_skips_restore(tmp_path: Path) -> None:
    class IgnoresCancel(_FakeTransport):
        def run(self, run_id: str, attempt_id: str, members: tuple[str, ...], timeout_seconds: float | None, cancel_event: object) -> dict[str, object]:
            while True:
                time.sleep(0.01)

    import time
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    transport = IgnoresCancel()
    runner = PortfolioTesterRunner(target, tmp_path / "runs", transport)
    with pytest.raises(OperationUnconfirmedError):
        runner.run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), run_id="stuck", timeout_seconds=0.01,
        )
    assert TesterTargetLock(target).path.exists()
    assert transport.restored == []
    manifest_path = next((tmp_path / "runs" / "stuck").glob("*/run_manifest.json"))
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "OPERATION_UNCONFIRMED"


def test_cancel_callback_failure_is_unconfirmed_and_retains_owner(tmp_path: Path) -> None:
    class CancelFails(_FakeTransport):
        def run(self, run_id: str, attempt_id: str, members: tuple[str, ...], timeout_seconds: float | None, cancel_event: object) -> dict[str, object]:
            while True:
                time.sleep(0.01)

        def cancel(self, run_id: str, attempt_id: str) -> None:
            raise RuntimeError("disconnect")

    import time
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    transport = CancelFails()

    with pytest.raises(OperationUnconfirmedError):
        PortfolioTesterRunner(target, tmp_path / "runs", transport).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), timeout_seconds=0.01,
        )

    assert TesterTargetLock(target).path.exists()
    assert transport.restored == []


def test_transport_disconnect_after_install_retains_owner(tmp_path: Path) -> None:
    class Disconnects(_FakeTransport):
        def run(self, run_id: str, attempt_id: str, members: tuple[str, ...], timeout_seconds: float | None, cancel_event: object) -> dict[str, object]:
            raise ConnectionError("remote disconnected")

    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    transport = Disconnects()

    with pytest.raises(OperationUnconfirmedError):
        PortfolioTesterRunner(target, tmp_path / "runs", transport).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(),
        )

    assert TesterTargetLock(target).path.exists()
    assert transport.restored == []


def test_restore_failure_retains_manifest_and_cleanup_requires_typed_proof(tmp_path: Path) -> None:
    class RestoreFails(_FakeTransport):
        def restore_settings(self, snapshot: object, run_id: str, attempt_id: str) -> dict[str, object]:
            raise RuntimeError("restore failed")

    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    transport = RestoreFails()
    runner = PortfolioTesterRunner(target, tmp_path / "runs", transport)
    with pytest.raises(SnapshotError):
        runner.run(members=("A",), strategies={"A": source}, expected_reports=("A",), tester_settings=_tester_settings(), run_id="restore-fail")
    manifest_path = next((tmp_path / "runs" / "restore-fail").glob("*/run_manifest.json"))
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "RESTORE_FAILED"
    assert TesterTargetLock(target).path.exists()

    payload = b"report"
    digest = __import__("hashlib").sha256(payload).hexdigest()
    good = _FakeTransport()
    proof = M6CommitReadbackProof("cleanup", "attempt", {"A": digest})
    clean_target = tmp_path / "clean-tester"
    clean_target.mkdir()
    result = PortfolioTesterRunner(clean_target, tmp_path / "runs2", good).run(
        members=("A",), strategies={"A": source}, expected_reports=("A",), tester_settings=_tester_settings(), run_id="cleanup", attempt_id="attempt", cleanup_gate=proof
    )
    assert not result.cleanup_performed and result.reports


def test_recovery_restores_incomplete_attempt_and_records_terminal_state(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    transport = _FakeTransport()
    workspace = RunWorkspace.create(tmp_path / "runs", "recover", "attempt")
    snapshot = {"run_id": "recover", "attempt_id": "attempt", "maker_fee": "0"}
    workspace.write_file("settings_snapshot.json", json.dumps(snapshot).encode(), "settings_snapshot")
    manifest = RunManifest(
        "recover", "attempt", TesterTargetLock(target).target_identity,
        ("A",), ("A",), "bin", "set", "ticks", workspace.artifacts, "RUNNING",
    )
    workspace.write_manifest(manifest)
    runner = PortfolioTesterRunner(target, tmp_path / "runs", transport)
    runner.recover(workspace.manifest_path)
    assert transport.restored == [snapshot]
    assert json.loads(workspace.manifest_path.read_text(encoding="utf-8"))["status"] == "RECOVERED"


def test_recovery_confirms_tester_is_stopped_before_restore(tmp_path: Path) -> None:
    class Ordered(_FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []

        def ensure_stopped(self, run_id: str, attempt_id: str) -> dict[str, object]:
            self.events.append("stop")
            return super().ensure_stopped(run_id, attempt_id)

        def restore_settings(self, snapshot: object, run_id: str, attempt_id: str) -> dict[str, object]:
            self.events.append("restore")
            return super().restore_settings(snapshot, run_id, attempt_id)

    target = tmp_path / "tester"
    target.mkdir()
    workspace = RunWorkspace.create(tmp_path / "runs", "recover", "attempt")
    workspace.write_file("settings_snapshot.json", b'{}', "settings_snapshot")
    workspace.write_manifest(RunManifest(
        "recover", "attempt", TesterTargetLock(target).target_identity,
        ("A",), ("A",), "bin", "set", "ticks", workspace.artifacts, "RUNNING",
    ))
    transport = Ordered()

    PortfolioTesterRunner(target, tmp_path / "runs", transport).recover(workspace.manifest_path)

    assert transport.events == ["stop", "restore"]


def test_recovery_restore_failure_retains_target_owner(tmp_path: Path) -> None:
    class RestoreFails(_FakeTransport):
        def restore_settings(self, snapshot: object, run_id: str, attempt_id: str) -> dict[str, object]:
            raise RuntimeError("restore failed")

    target = tmp_path / "tester"
    target.mkdir()
    workspace = RunWorkspace.create(tmp_path / "runs", "recover", "attempt")
    snapshot = {"run_id": "recover", "attempt_id": "attempt", "maker_fee": "0"}
    workspace.write_file("settings_snapshot.json", json.dumps(snapshot).encode(), "settings_snapshot")
    workspace.write_manifest(RunManifest(
        "recover", "attempt", TesterTargetLock(target).target_identity,
        ("A",), ("A",), "bin", "set", "ticks", workspace.artifacts, "RUNNING",
    ))

    with pytest.raises(SnapshotError):
        PortfolioTesterRunner(target, tmp_path / "runs", RestoreFails()).recover(workspace.manifest_path)

    assert TesterTargetLock(target).path.exists()
    assert json.loads(workspace.manifest_path.read_text(encoding="utf-8"))["status"] == "RECOVERY_FAILED"


def test_m6_runner_decodes_publishes_reads_back_and_then_cleans(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    decoded = {
        "schema": "portfolio_report_v1", "version": 1,
        "identity": {"run_id": "m6", "attempt_id": "attempt", "member": "A"},
        "action_count": 2,
        "period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:02Z"},
        "actions": [
            {"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "LONG", "size": "1"},
            {"timestamp": "2026-01-01T00:00:01Z", "symbol": "BTCUSDT", "action": "CLOSE", "side": "LONG", "size": "1", "pnl": "1"},
        ],
        "series": {"equity": [["2026-01-01T00:00:00Z", "100"], ["2026-01-01T00:00:02Z", "101"]]},
    }
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    result = PortfolioTesterRunner(target, tmp_path / "runs", _FakeTransport()).run(
        members=("A",), strategies={"A": source}, expected_reports=("A",),
        tester_settings=_tester_settings(), run_id="m6", attempt_id="attempt",
        report_decoder=lambda _: decoded, portfolio_store=store,
    )
    assert result.cleanup_performed and result.commit_readback_proof is not None
    assert store.read_portfolio_run("m6", "attempt")["action_count"] == 2


def test_m6_unknown_q06_report_without_decoder_retains_raw_evidence(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="decoder"):
        PortfolioTesterRunner(target, tmp_path / "runs", _FakeTransport()).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), run_id="q06", attempt_id="attempt",
            portfolio_store=PortfolioStore(tmp_path / "portfolio.duckdb"),
        )
    assert list((tmp_path / "runs" / "q06" / "attempt").glob("report_*.html"))


def test_m6_decoder_without_store_cannot_fall_back_to_legacy_cleanup(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    decoded = {
        "schema": "portfolio_report_v1", "version": 1,
        "identity": {"run_id": "decoder-only", "attempt_id": "attempt", "member": "A"},
        "action_count": 0,
        "period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:02Z"},
        "actions": [],
        "series": {
            "equity": [["2026-01-01T00:00:00Z", "100"], ["2026-01-01T00:00:02Z", "101"]],
            "notional": [["2026-01-01T00:00:00Z", "200"], ["2026-01-01T00:00:02Z", "200"]],
            "margin_balance": [["2026-01-01T00:00:00Z", "100"], ["2026-01-01T00:00:02Z", "100"]],
        },
    }
    legacy_proof = M6CommitReadbackProof(
        "decoder-only", "attempt", {"A": __import__("hashlib").sha256(b"report").hexdigest()}
    )
    with pytest.raises(PortfolioStoreError, match="requires publication store"):
        PortfolioTesterRunner(target, tmp_path / "runs", _FakeTransport()).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), run_id="decoder-only", attempt_id="attempt",
            report_decoder=lambda _: decoded, cleanup_gate=legacy_proof,
        )
    assert list((tmp_path / "runs" / "decoder-only" / "attempt").glob("report_*.html"))


def test_m6_incomplete_metrics_proof_retains_report_files(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    decoded = {
        "schema": "portfolio_report_v1", "version": 1,
        "identity": {"run_id": "incomplete", "attempt_id": "attempt", "member": "A"},
        "action_count": 0,
        "period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:02Z"},
        "actions": [], "series": {},
    }
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    result = PortfolioTesterRunner(target, tmp_path / "runs", _FakeTransport()).run(
        members=("A",), strategies={"A": source}, expected_reports=("A",),
        tester_settings=_tester_settings(), run_id="incomplete", attempt_id="attempt",
        report_decoder=lambda _: decoded, portfolio_store=store,
    )
    assert not result.cleanup_performed and result.reports
    assert result.commit_readback_proof is not None and not result.commit_readback_proof.normalized_facts_complete
    assert store.read_portfolio_run("incomplete", "attempt")["status"] == "INCOMPLETE"


def test_m6_margin_bound_failure_retains_raw_report_evidence(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    decoded = {
        "schema": "portfolio_report_v1", "version": 1,
        "identity": {"run_id": "margin-fail", "attempt_id": "attempt", "member": "A"},
        "action_count": 0,
        "period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:02Z"},
        "actions": [],
        "portfolio": {"realized_pnl": "0", "fees": "0"},
        "series": {
            "equity": [["2026-01-01T00:00:00Z", "100"], ["2026-01-01T00:00:02Z", "101"]],
            "notional": [["2026-01-01T00:00:00Z", "200"], ["2026-01-01T00:00:02Z", "200"]],
            "margin_balance": [["2026-01-01T00:00:00Z", "100"], ["2026-01-01T00:00:02Z", "100"]],
        },
    }
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    result = PortfolioTesterRunner(target, tmp_path / "runs", _FakeTransport()).run(
        members=("A",), strategies={"A": source}, expected_reports=("A",),
        tester_settings=_tester_settings(), run_id="margin-fail", attempt_id="attempt",
        report_decoder=lambda _: decoded, portfolio_store=store,
    )
    assert not result.cleanup_performed and result.reports
    assert result.commit_readback_proof is not None
    assert not result.commit_readback_proof.committed
    assert store.read_portfolio_run("margin-fail", "attempt")["status"] == "INCOMPLETE"


def test_m6_missing_store_publication_method_uses_store_error_and_retains_files(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    decoded = {
        "schema": "portfolio_report_v1", "version": 1,
        "identity": {"run_id": "store-error", "attempt_id": "attempt", "member": "A"},
        "action_count": 0,
        "period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:02Z"},
        "actions": [], "series": {},
    }
    with pytest.raises(PortfolioStoreError, match="publication"):
        PortfolioTesterRunner(target, tmp_path / "runs", _FakeTransport()).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), run_id="store-error", attempt_id="attempt",
            report_decoder=lambda _: decoded, portfolio_store=object(),
        )
    assert list((tmp_path / "runs" / "store-error" / "attempt").glob("report_*.html"))


def test_m6_publish_failure_retains_raw_report_evidence(tmp_path: Path) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    decoded = {
        "schema": "portfolio_report_v1", "version": 1,
        "identity": {"run_id": "publish-error", "attempt_id": "attempt", "member": "A"},
        "action_count": 0,
        "period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:02Z"},
        "actions": [], "series": {},
    }

    class FailingStore:
        def publish_portfolio_run(self, *_args, **_kwargs):
            raise PortfolioStoreError("forced publication failure")

    with pytest.raises(PortfolioStoreError, match="forced publication"):
        PortfolioTesterRunner(target, tmp_path / "runs", _FakeTransport()).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), run_id="publish-error", attempt_id="attempt",
            report_decoder=lambda _: decoded, portfolio_store=FailingStore(),
        )
    assert list((tmp_path / "runs" / "publish-error" / "attempt").glob("report_*.html"))


def test_m6_runner_durable_readback_failure_retains_raw_report_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    decoded = {
        "schema": "portfolio_report_v1", "version": 1,
        "identity": {"run_id": "readback-error", "attempt_id": "attempt", "member": "A"},
        "action_count": 0,
        "period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:02Z"},
        "actions": [], "series": {},
    }
    store = PortfolioStore(tmp_path / "portfolio.duckdb")

    def fail_readback(*_args, **_kwargs):
        raise PortfolioStoreError("forced durable readback failure")

    monkeypatch.setattr(store, "read_portfolio_run", fail_readback)
    with pytest.raises(PortfolioStoreError, match="forced durable"):
        PortfolioTesterRunner(target, tmp_path / "runs", _FakeTransport()).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), run_id="readback-error", attempt_id="attempt",
            report_decoder=lambda _: decoded, portfolio_store=store,
        )
    assert list((tmp_path / "runs" / "readback-error" / "attempt").glob("report_*.html"))


def test_m6_restore_failure_does_not_publish_commit(tmp_path: Path) -> None:
    class RestoreFails(_FakeTransport):
        def restore_settings(self, snapshot: object, run_id: str, attempt_id: str) -> dict[str, object]:
            raise RuntimeError("restore failed")

    target = tmp_path / "tester"
    target.mkdir()
    source = tmp_path / "A.json"
    source.write_text("{}", encoding="utf-8")
    decoded = {
        "schema": "portfolio_report_v1", "version": 1,
        "identity": {"run_id": "restore-publish", "attempt_id": "attempt", "member": "A"},
        "action_count": 2,
        "period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:02Z"},
        "actions": [
            {"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "LONG", "size": "1"},
            {"timestamp": "2026-01-01T00:00:01Z", "symbol": "BTCUSDT", "action": "CLOSE", "side": "LONG", "size": "1", "pnl": "1"},
        ],
        "series": {"equity": [["2026-01-01T00:00:00Z", "100"], ["2026-01-01T00:00:02Z", "101"]]},
    }
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    with pytest.raises(SnapshotError):
        PortfolioTesterRunner(target, tmp_path / "runs", RestoreFails()).run(
            members=("A",), strategies={"A": source}, expected_reports=("A",),
            tester_settings=_tester_settings(), run_id="restore-publish", attempt_id="attempt",
            report_decoder=lambda _: decoded, portfolio_store=store,
        )
    assert store.read_portfolio_run("restore-publish", "attempt") is None
    assert list((tmp_path / "runs" / "restore-publish" / "attempt").glob("report_*.html"))


def test_cleanup_surfaces_partial_deletion_and_keeps_only_remaining_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = RunWorkspace.create(tmp_path / "runs", "partial", "attempt")
    first = workspace.write_file("report_A.html", b"A", "report")
    second = workspace.write_file("report_B.html", b"B", "report")
    manifest = RunManifest("partial", "attempt", "target", ("A", "B"), ("A", "B"), "b", "s", "t", workspace.artifacts)
    proof = M6CommitReadbackProof(
        "partial", "attempt", {"A": first.sha256, "B": second.sha256}, normalized_facts_complete=True,
        parser_version="p", metrics_version="m", semantic_digests={"A": "a" * 64, "B": "b" * 64},
        replay_fact_counts={"reports": 2, "actions": 0, "series": 0, "cycles": 0},
    )
    def partially_remove(path: Path) -> None:
        (path / "report_A.html").unlink()
        raise OSError("partial delete")
    monkeypatch.setattr(runner_module.shutil, "rmtree", partially_remove)
    assert workspace.cleanup(proof, manifest) is False
    assert not (workspace.root / "report_A.html").exists()
    assert (workspace.root / "report_B.html").exists()
