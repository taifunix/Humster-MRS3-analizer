"""Fixture-safe portfolio tester transport and run-owned artifacts.

M5 deliberately contains no tester process or network implementation.  A
transport is an explicit, versioned fake/adapter contract and every artifact
is retained until an M6 commit/readback proof authorizes deletion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from ..locking import TesterTargetLock, canonical_tester_target


TRANSPORT_CONTRACT = "portfolio_transport_v1"
REPORT_METADATA_CONTRACT = "portfolio_report_v1"
TESTER_SETTINGS_CONTRACT = "portfolio_tester_settings_v1"
CLEANUP_PROOF_CONTRACT = "portfolio_m6_commit_readback_v1"


class PortfolioRunnerError(RuntimeError):
    """Base error for a failed-closed portfolio run."""


class PortfolioModeUnsupported(PortfolioRunnerError):
    """The transport has no explicit portfolio capability contract."""


class TransportContractError(PortfolioRunnerError):
    """A transport does not implement the versioned M5 contract."""


class ArtifactMismatchError(PortfolioRunnerError):
    """The result set does not match the run manifest."""


class SnapshotError(PortfolioRunnerError):
    """Settings could not be snapshotted or restored."""


class PortfolioCancelled(PortfolioRunnerError):
    pass


class PortfolioTimedOut(PortfolioRunnerError):
    pass


class DuplicateRunError(PortfolioRunnerError):
    """A committed run cannot be executed a second time."""


class OperationUnconfirmedError(TransportContractError):
    """Cancellation could not prove that target mutation stopped."""


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_SECRET_WORDS = ("password", "passwd", "secret", "token", "credential", "api_key", "apikey")


def _safe_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ValueError(f"{label} must be a simple artifact identifier")
    return value


def _has_secret(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(any(word in str(key).casefold() for word in _SECRET_WORDS) or _has_secret(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_secret(item) for item in value)
    return False


def _redacted(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): "<redacted>" if any(word in str(key).casefold() for word in _SECRET_WORDS) else _redacted(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted(item) for item in value]
    return value


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PortfolioRunnerError("manifest value is not JSON serializable") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            try:
                import os
                os.fsync(handle.fileno())
            except OSError:
                pass
        temporary.replace(path)
        try:
            import os
            descriptor = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class Artifact:
    path: str
    role: str
    sha256: str
    size: int

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "role": self.role, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True, slots=True)
class RunManifest:
    run_id: str
    attempt_id: str
    target_identity: str
    members: tuple[str, ...]
    expected_reports: tuple[str, ...]
    binary_identity: object
    settings_identity: object
    tick_identity: object
    artifacts: tuple[Artifact, ...]
    status: str = "PREPARED"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "portfolio_tester_run_manifest_v1",
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "target_identity": self.target_identity,
            "members": list(self.members),
            "expected_reports": list(self.expected_reports),
            "binary_identity": _redacted(self.binary_identity),
            "settings_identity": _redacted(self.settings_identity),
            "tick_identity": _redacted(self.tick_identity),
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
            "status": self.status,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class M6CommitReadbackProof:
    """Typed proof needed before deleting a run workspace."""

    run_id: str
    attempt_id: str
    report_digests: Mapping[str, str]
    committed: bool = True
    readback_verified: bool = True
    contract: str = CLEANUP_PROOF_CONTRACT
    # M6 fills these fields from a transactional readback.  The default keeps
    # the M5 proof constructor source-compatible for reports with no decoder.
    # None is the legacy M5 proof shape.  M6 proofs always set this boolean
    # and therefore must satisfy the additional replay-fact checks below.
    normalized_facts_complete: bool | None = None
    parser_version: str | None = None
    metrics_version: str | None = None
    semantic_digests: Mapping[str, str] = field(default_factory=dict)
    replay_fact_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def normalized_fact_completeness(self) -> bool:
        return self.normalized_facts_complete is True

    @property
    def raw_report_digests(self) -> Mapping[str, str]:
        return self.report_digests

    @property
    def committed_readback(self) -> bool:
        return self.committed and self.readback_verified


class RunWorkspace:
    """A directory whose files are owned by exactly one run/attempt."""

    def __init__(self, root: Path, run_id: str, attempt_id: str) -> None:
        self.root = root.resolve()
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.manifest_path = self.root / "run_manifest.json"
        self._artifacts: dict[str, Artifact] = {}

    @classmethod
    def create(cls, parent: Path, run_id: str, attempt_id: str) -> RunWorkspace:
        _safe_component(run_id, "run_id")
        _safe_component(attempt_id, "attempt_id")
        root = parent.resolve() / run_id / attempt_id
        root.mkdir(parents=True, exist_ok=False)
        return cls(root, run_id, attempt_id)

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise PortfolioRunnerError(f"path is outside run workspace: {path}") from error
        return resolved

    def add_file(self, path: Path, role: str) -> Artifact:
        path = self._inside(path)
        if not path.is_file() or path.is_symlink():
            raise ArtifactMismatchError(f"artifact is not an owned regular file: {path}")
        relative = path.relative_to(self.root).as_posix()
        artifact = Artifact(relative, role, _sha256(path), path.stat().st_size)
        self._artifacts[relative] = artifact
        return artifact

    def write_file(self, name: str, value: bytes, role: str) -> Artifact:
        name = _safe_component(name, "artifact name")
        path = self.root / name
        _atomic_write(path, value)
        return self.add_file(path, role)

    def copy_file(self, source: Path, name: str, role: str) -> Artifact:
        name = _safe_component(name, "artifact name")
        source = source.resolve()
        destination = self.root / name
        shutil.copy2(source, destination)
        return self.add_file(destination, role)

    def write_manifest(self, manifest: RunManifest) -> Path:
        _atomic_write(self.manifest_path, (json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        return self.manifest_path

    @property
    def artifacts(self) -> tuple[Artifact, ...]:
        return tuple(self._artifacts.values())

    def cleanup(self, proof: object, manifest: RunManifest) -> bool:
        if not isinstance(proof, M6CommitReadbackProof) or proof.contract != CLEANUP_PROOF_CONTRACT:
            return False
        if proof.committed is not True or proof.readback_verified is not True or proof.normalized_facts_complete is not True or proof.run_id != manifest.run_id or proof.attempt_id != manifest.attempt_id:
            return False
        actual = {
            artifact.path.removeprefix("report_").removesuffix(".html"): artifact.sha256
            for artifact in manifest.artifacts
            if artifact.role == "report"
        }
        if not isinstance(proof.report_digests, Mapping) or dict(proof.report_digests) != actual:
            return False
        members = set(actual)
        if not isinstance(proof.semantic_digests, Mapping) or set(proof.semantic_digests) != members:
            return False
        if not isinstance(proof.parser_version, str) or not proof.parser_version.strip() or not isinstance(proof.metrics_version, str) or not proof.metrics_version.strip():
            return False
        expected_counts = {"reports", "actions", "series", "cycles"}
        if not isinstance(proof.replay_fact_counts, Mapping) or set(proof.replay_fact_counts) != expected_counts:
            return False
        if any(type(value) is not int or value < 0 for value in proof.replay_fact_counts.values()):
            return False
        if any(type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value) for value in proof.semantic_digests.values()):
            return False
        if proof.replay_fact_counts["reports"] != len(members):
            return False
        try:
            shutil.rmtree(self.root)
        except OSError:
            # Commit/readback already happened; cleanup is an advisory warning.
            return False
        return True

    @classmethod
    def load(cls, manifest_path: Path) -> tuple[RunWorkspace, dict[str, object]]:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema") != "portfolio_tester_run_manifest_v1":
            raise ArtifactMismatchError("unsupported run manifest")
        run_id, attempt_id = document.get("run_id"), document.get("attempt_id")
        if not isinstance(run_id, str) or not isinstance(attempt_id, str):
            raise ArtifactMismatchError("manifest identity is malformed")
        _safe_component(run_id, "run_id")
        _safe_component(attempt_id, "attempt_id")
        workspace = cls(manifest_path.parent.resolve(), run_id, attempt_id)
        for raw in document.get("artifacts", []):
            if not isinstance(raw, dict):
                raise ArtifactMismatchError("malformed artifact manifest")
            try:
                artifact = Artifact(str(raw["path"]), str(raw["role"]), str(raw["sha256"]), int(raw["size"]))
            except (KeyError, TypeError, ValueError) as error:
                raise ArtifactMismatchError("malformed artifact manifest") from error
            path = workspace._inside(workspace.root / artifact.path)
            if not path.is_file() or path.is_symlink() or _sha256(path) != artifact.sha256 or path.stat().st_size != artifact.size:
                raise ArtifactMismatchError(f"artifact hash mismatch: {artifact.path}")
            workspace._artifacts[artifact.path] = artifact
        return workspace, document


class PortfolioTransport(Protocol):
    def capability_manifest(self) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class PortfolioRunResult:
    run_id: str
    attempt_id: str
    status: str
    workspace: Path
    manifest_path: Path
    reports: tuple[Path, ...]
    cleanup_performed: bool = False
    commit_readback_proof: M6CommitReadbackProof | None = None


def _call(method: Callable[..., Any], kwargs: Mapping[str, object], *, required: Sequence[str] = ()) -> Any:
    """Call a contract method without silently dropping required arguments."""
    _validate_signature(method, kwargs, required=required)
    parameters = inspect.signature(method).parameters
    has_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    accepted = {name for name, parameter in parameters.items() if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
    supplied = dict(kwargs) if has_kwargs else {name: value for name, value in kwargs.items() if name in accepted}
    return method(**supplied)


def _validate_signature(method: Callable[..., Any], kwargs: Mapping[str, object], *, required: Sequence[str] = ()) -> None:
    """Validate a contract call without invoking a potentially blocking method."""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError) as error:
        raise TransportContractError("transport method has no inspectable signature") from error
    parameters = signature.parameters
    for name in required:
        parameter = parameters.get(name)
        if parameter is None or parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
            raise TransportContractError(f"transport method omits required argument: {name}")
    accepted = {name for name, parameter in parameters.items() if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
    has_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    unknown = set(kwargs) - accepted
    if unknown and not has_kwargs:
        raise TransportContractError(f"transport method has no arguments: {sorted(unknown)}")


def _evidence(value: object, label: str, *, run_id: str, attempt_id: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not value:
        raise TransportContractError(f"{label} evidence is missing")
    for key, expected in (("run_id", run_id), ("attempt_id", attempt_id)):
        if value.get(key) != expected:
            raise TransportContractError(f"{label} evidence identity mismatch")
    return value


class PortfolioTesterRunner:
    """Run a portfolio-capable fake/remote transport under one target lease."""

    def __init__(self, target: Path | str, workspace_root: Path, transport: object, *, lock_path: Path | None = None) -> None:
        self.target = target
        self.workspace_root = workspace_root.resolve()
        self.transport = transport
        self.lock_path = lock_path

    def _capabilities(self) -> Mapping[str, object]:
        method = getattr(self.transport, "capability_manifest", None)
        if not callable(method):
            raise PortfolioModeUnsupported("portfolio capability manifest is absent")
        value = _call(method, {})
        if not isinstance(value, Mapping) or value.get("portfolio_mode") is not True:
            raise PortfolioModeUnsupported("portfolio capability manifest is absent or unsupported")
        if value.get("transport_contract") != TRANSPORT_CONTRACT:
            raise PortfolioModeUnsupported("unsupported portfolio transport contract")
        if value.get("cancellable_operations") is not True:
            raise TransportContractError("cancellable operation contract is required")
        return value

    def _identities(self, capabilities: Mapping[str, object], expected: Mapping[str, object]) -> None:
        method = getattr(self.transport, "identities", None)
        actual = _call(method, {}) if callable(method) else capabilities.get("identities")
        if not isinstance(actual, Mapping):
            raise PortfolioRunnerError("binary/settings/tick identity is unavailable")
        for key in ("binary", "settings", "ticks"):
            wanted = expected.get(key)
            if wanted is None:
                wanted = capabilities.get(f"{key}_identity")
                if key == "ticks" and wanted is None:
                    wanted = capabilities.get("tick_identity")
            if wanted is None or actual.get(key) != wanted:
                raise ArtifactMismatchError(f"{key} identity mismatch")

    def _snapshot(self, *, run_id: str, attempt_id: str) -> object:
        method = getattr(self.transport, "snapshot_settings", None)
        if not callable(method):
            raise SnapshotError("tester settings snapshot is unavailable")
        snapshot = _call(method, {"run_id": run_id, "attempt_id": attempt_id}, required=("run_id", "attempt_id"))
        _evidence(snapshot, "snapshot", run_id=run_id, attempt_id=attempt_id)
        if _has_secret(snapshot) and (not isinstance(snapshot.get("snapshot_ref"), str) or not snapshot["snapshot_ref"]):  # type: ignore[union-attr]
            raise SnapshotError("secret-bearing snapshot must use an opaque snapshot_ref")
        return snapshot

    def _quiesce(self, *, run_id: str, attempt_id: str) -> Mapping[str, object]:
        method = getattr(self.transport, "ensure_stopped", None)
        if not callable(method):
            raise OperationUnconfirmedError("tester process stop attestation is unavailable")
        try:
            result = _call(
                method, {"run_id": run_id, "attempt_id": attempt_id},
                required=("run_id", "attempt_id"),
            )
            evidence = _evidence(result, "stop", run_id=run_id, attempt_id=attempt_id)
        except BaseException as error:
            raise OperationUnconfirmedError("tester process stop attestation failed") from error
        if evidence.get("stopped") is not True:
            raise OperationUnconfirmedError("tester process stop attestation failed")
        return evidence

    def _restore(self, snapshot: object, *, run_id: str, attempt_id: str) -> Mapping[str, object]:
        method = getattr(self.transport, "restore_settings", None)
        if not callable(method):
            raise SnapshotError("tester settings restore is unavailable")
        result = _call(method, {"snapshot": snapshot, "run_id": run_id, "attempt_id": attempt_id}, required=("snapshot", "run_id", "attempt_id"))
        return _evidence(result, "restore", run_id=run_id, attempt_id=attempt_id)

    def _transition(self, workspace: RunWorkspace, manifest: RunManifest, status: str) -> RunManifest:
        manifest = RunManifest(manifest.run_id, manifest.attempt_id, manifest.target_identity, manifest.members, manifest.expected_reports, manifest.binary_identity, manifest.settings_identity, manifest.tick_identity, workspace.artifacts, status)
        workspace.write_manifest(manifest)
        return manifest

    def _existing(self, run_id: str) -> PortfolioRunResult | None:
        root = self.workspace_root / run_id
        if not root.is_dir():
            return None
        committed: list[tuple[str, Path, dict[str, object]]] = []
        for attempt_root in root.iterdir():
            if not attempt_root.is_dir() or attempt_root.is_symlink():
                raise ArtifactMismatchError(f"prior attempt is malformed: {attempt_root.name}")
            manifest_path = attempt_root / "run_manifest.json"
            try:
                workspace, document = RunWorkspace.load(manifest_path)
            except (OSError, ValueError, json.JSONDecodeError, PortfolioRunnerError) as error:
                raise ArtifactMismatchError(f"prior attempt is malformed: {attempt_root.name}") from error
            if workspace.run_id != run_id or workspace.attempt_id != attempt_root.name:
                raise ArtifactMismatchError(f"prior attempt identity mismatch: {attempt_root.name}")
            if document.get("status") in {"COMPLETED", "COMMITTED", "READY_TO_RELEASE"}:
                committed.append((workspace.attempt_id, workspace.root, document))
        if not committed:
            return None
        if len(committed) > 1:
            raise DuplicateRunError(f"run_id has multiple committed attempts: {run_id}")
        committed.sort()
        attempt_id, path, document = committed[0]
        reports = tuple(path / str(artifact["path"]) for artifact in document.get("artifacts", []) if isinstance(artifact, Mapping) and artifact.get("role") == "report")
        return PortfolioRunResult(run_id, attempt_id, str(document["status"]), path, path / "run_manifest.json", reports, False)

    def _new_attempt(self, run_id: str, attempt_id: str | None) -> str:
        if self._existing(run_id) is not None:
            raise DuplicateRunError(f"run_id is already committed: {run_id}")
        if attempt_id is not None:
            _safe_component(attempt_id, "attempt_id")
            if (self.workspace_root / run_id / attempt_id).exists():
                raise DuplicateRunError(f"attempt_id already exists: {attempt_id}")
            return attempt_id
        root = self.workspace_root / run_id
        root.mkdir(parents=True, exist_ok=True)
        while True:
            generated = uuid4().hex
            if not (root / generated).exists():
                return generated

    def _report_values(self, run_value: object, expected: tuple[str, ...], *, run_id: str, attempt_id: str) -> tuple[Mapping[str, object], Mapping[str, object]]:
        metadata: Mapping[str, object] = {}
        if isinstance(run_value, Mapping) and isinstance(run_value.get("reports"), Mapping):
            reports = run_value["reports"]
            if isinstance(run_value.get("report_metadata"), Mapping):
                metadata = run_value["report_metadata"]  # type: ignore[assignment]
        elif isinstance(run_value, Mapping):
            reports = run_value
        else:
            method = getattr(self.transport, "collect_reports", None)
            if not callable(method):
                raise ArtifactMismatchError("tester did not return reports")
            collected = _call(method, {"run_id": run_id, "attempt_id": attempt_id, "expected_reports": expected}, required=("run_id", "attempt_id", "expected_reports"))
            if not isinstance(collected, Mapping):
                raise ArtifactMismatchError("tester did not return reports")
            reports = collected.get("reports", collected)
            if isinstance(collected.get("report_metadata"), Mapping):
                metadata = collected["report_metadata"]  # type: ignore[assignment]
        if not isinstance(reports, Mapping):
            raise ArtifactMismatchError("tester did not return reports")
        names = tuple(sorted(str(name) for name in reports))
        if names != tuple(sorted(expected)):
            raise ArtifactMismatchError(f"report set mismatch: expected={expected!r} actual={names!r}")
        return reports, metadata

    @staticmethod
    def _report_metadata(metadata: object, *, member: str, run_id: str, attempt_id: str, digest: str, size: int, expected_evidence: Mapping[str, object]) -> None:
        if not isinstance(metadata, Mapping) or metadata.get("contract") != REPORT_METADATA_CONTRACT:
            raise ArtifactMismatchError("report bytes require versioned metadata")
        required = {"member": member, "run_id": run_id, "attempt_id": attempt_id, "sha256": digest, "complete": True}
        if any(metadata.get(key) != expected for key, expected in required.items()):
            raise ArtifactMismatchError("report metadata identity/digest mismatch")
        if metadata.get("version") != 1 or metadata.get("size") != size:
            raise ArtifactMismatchError("report metadata is incomplete")
        if any(metadata.get(key) != value for key, value in expected_evidence.items()):
            raise ArtifactMismatchError("report execution evidence mismatch")

    def _allowed_root(self, capabilities: Mapping[str, object], workspace: RunWorkspace) -> Path:
        configured = capabilities.get("allowed_output_root", capabilities.get("report_root"))
        root = Path(configured).resolve() if isinstance(configured, (str, Path)) else workspace.root
        if not root.exists() or not root.is_dir():
            raise ArtifactMismatchError("configured report root is unavailable")
        return root

    def _copy_reports(self, workspace: RunWorkspace, reports: Mapping[str, object], metadata: Mapping[str, object], expected: tuple[str, ...], capabilities: Mapping[str, object], *, run_id: str, attempt_id: str, started_ns: int, tester_settings_sha256: str, strategy_sha256: Mapping[str, str], test_start: str, test_end: str) -> list[Path]:
        allowed_root = self._allowed_root(capabilities, workspace)
        paths: list[Path] = []
        for member in expected:
            value = reports[member]
            member_metadata = metadata.get(member)
            if isinstance(value, Mapping):
                member_metadata = value.get("metadata", member_metadata)
                raw = value.get("bytes", value.get("data", value.get("content")))
            else:
                raw = value
            destination = workspace.root / f"report_{_safe_component(member, 'report')}.html"
            expected_evidence = {
                "test_start": test_start,
                "test_end": test_end,
                "tester_settings_sha256": tester_settings_sha256,
                "strategy_sha256": strategy_sha256[member],
            }
            if isinstance(raw, bytes):
                digest = hashlib.sha256(raw).hexdigest()
                self._report_metadata(member_metadata, member=member, run_id=run_id, attempt_id=attempt_id, digest=digest, size=len(raw), expected_evidence=expected_evidence)
                _atomic_write(destination, raw)
            elif isinstance(raw, (str, Path)):
                original = Path(raw)
                if original.is_symlink():
                    raise ArtifactMismatchError("report source is a symbolic link")
                source = original.resolve()
                try:
                    source.relative_to(allowed_root)
                except ValueError as error:
                    raise ArtifactMismatchError(f"report source is outside configured output root: {source}") from error
                if not source.is_file() or source.is_symlink() or source.stat().st_mtime_ns < started_ns:
                    raise ArtifactMismatchError(f"report source is missing or stale: {source}")
                self._report_metadata(
                    member_metadata, member=member, run_id=run_id,
                    attempt_id=attempt_id, digest=_sha256(source),
                    size=source.stat().st_size, expected_evidence=expected_evidence,
                )
                shutil.copy2(source, destination)
            else:
                raise ArtifactMismatchError("report must be an owned file path or metadata-wrapped bytes")
            workspace.add_file(destination, "report")
            paths.append(destination)
        return paths

    def _operation(self, capabilities: Mapping[str, object], *, run_id: str, attempt_id: str, members: tuple[str, ...], timeout_seconds: float | None, cancel_check: Callable[[], bool] | None) -> object:
        method = getattr(self.transport, "run", None)
        if not callable(method):
            raise TransportContractError("portfolio run transport is unavailable")
        cancel_event = threading.Event()
        kwargs = {"run_id": run_id, "attempt_id": attempt_id, "members": members, "timeout_seconds": timeout_seconds, "cancel_event": cancel_event}
        _validate_signature(method, kwargs, required=tuple(kwargs))
        result: list[object] = []
        failure: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(_call(method, kwargs, required=tuple(kwargs)))
            except BaseException as error:
                failure.append(error)

        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        started = time.monotonic()
        while not result and not failure:
            if cancel_check is not None and cancel_check():
                self._stop_operation(worker, cancel_event, run_id, attempt_id)
                raise PortfolioCancelled("portfolio run cancelled")
            if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
                self._stop_operation(worker, cancel_event, run_id, attempt_id)
                raise PortfolioTimedOut("portfolio tester run timed out")
            worker.join(0.01)
        if failure:
            raise OperationUnconfirmedError(
                "transport operation failed without completion evidence"
            ) from failure[0]
        value = result[0]
        if callable(getattr(value, "wait", None)):
            raise OperationUnconfirmedError("transport returned an unsupported live operation handle")
        return value

    def _stop_operation(self, worker: threading.Thread, cancel_event: threading.Event, run_id: str, attempt_id: str) -> None:
        cancel_event.set()
        method = getattr(self.transport, "cancel", None)
        cancel_worker: threading.Thread | None = None
        if callable(method):
            kwargs = {"run_id": run_id, "attempt_id": attempt_id}
            _validate_signature(method, kwargs, required=("run_id", "attempt_id"))
            def cancel() -> None:
                try:
                    _call(method, kwargs, required=("run_id", "attempt_id"))
                except BaseException:
                    pass

            cancel_worker = threading.Thread(target=cancel, daemon=True)
            cancel_worker.start()
        elif "cancel_event" not in inspect.signature(self.transport.run).parameters:
            raise TransportContractError("cancellable transport operation is unavailable")
        deadline = time.monotonic() + 1.0
        worker.join(max(0.0, deadline - time.monotonic()))
        if cancel_worker is not None:
            cancel_worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive() or (cancel_worker is not None and cancel_worker.is_alive()):
            raise OperationUnconfirmedError("operation cancellation was not confirmed")
        self._quiesce(run_id=run_id, attempt_id=attempt_id)

    def run(self, *, members: Sequence[str], strategies: Mapping[str, Path] | Sequence[Path], expected_reports: Sequence[str], binary_identity: object | None = None, settings_identity: object | None = None, tick_identity: object | None = None, tester_settings: Mapping[str, object] | None = None, run_id: str | None = None, attempt_id: str | None = None, timeout_seconds: float | None = None, cancel_check: Callable[[], bool] | None = None, cleanup_gate: object = False, report_decoder: object | None = None, portfolio_store: object | None = None, planned_leverage: Mapping[str, object] | None = None) -> PortfolioRunResult:
        members = tuple(str(member) for member in members)
        expected = tuple(str(report) for report in expected_reports)
        if not members or len(set(members)) != len(members):
            raise ValueError("portfolio members must be non-empty and unique")
        if not expected or len(set(expected)) != len(expected):
            raise ValueError("expected reports must be non-empty and unique")
        if set(expected) != set(members):
            raise ValueError("expected reports must exactly cover portfolio members")
        for report in expected:
            _safe_component(report, "report")
        if (
            not isinstance(tester_settings, Mapping)
            or tester_settings.get("contract") != TESTER_SETTINGS_CONTRACT
            or not isinstance(tester_settings.get("test_start"), str)
            or not isinstance(tester_settings.get("test_end"), str)
            or not isinstance(tester_settings.get("limiter"), Mapping)
            or not tester_settings["limiter"]
            or not isinstance(tester_settings.get("account"), Mapping)
            or not tester_settings["account"]
        ):
            raise TransportContractError("versioned tester settings with exact period, limiter and account are required")
        if _has_secret(tester_settings):
            raise SnapshotError("tester settings contain credentials")
        tester_settings_bytes = _json_bytes(tester_settings)
        test_start = str(tester_settings["test_start"])
        test_end = str(tester_settings["test_end"])
        try:
            start_time = datetime.fromisoformat(test_start.replace("Z", "+00:00"))
            end_time = datetime.fromisoformat(test_end.replace("Z", "+00:00"))
            valid_period = start_time < end_time
        except (TypeError, ValueError):
            valid_period = False
        if not valid_period:
            raise TransportContractError("tester settings period is invalid")
        run_id = run_id or uuid4().hex
        _safe_component(run_id, "run_id")
        workspace: RunWorkspace | None = None
        manifest: RunManifest | None = None
        snapshot: object | None = None
        primary: BaseException | None = None
        started_ns = time.time_ns()
        lock = TesterTargetLock(self.target, lock_path=self.lock_path).acquire()
        release_lock = True
        try:
            attempt_id = self._new_attempt(run_id, attempt_id)
            capabilities = self._capabilities()
            self._identities(capabilities, {"binary": binary_identity, "settings": settings_identity, "ticks": tick_identity})
            if cancel_check is not None and cancel_check():
                raise PortfolioCancelled("portfolio run cancelled before install")
            workspace = RunWorkspace.create(self.workspace_root, run_id, attempt_id)
            try:
                stopped_before = self._quiesce(run_id=run_id, attempt_id=attempt_id)
                workspace.write_file(
                    "stopped_before.json", _json_bytes(_redacted(stopped_before)),
                    "process_evidence",
                )
                snapshot = self._snapshot(run_id=run_id, attempt_id=attempt_id)
                snapshot_for_manifest = _redacted(snapshot)
                if _has_secret(snapshot):
                    snapshot_for_manifest = {"snapshot_ref": snapshot["snapshot_ref"]}  # type: ignore[index]
                settings_artifact = workspace.write_file("tester_settings.json", tester_settings_bytes, "tester_config")
                strategy_items = tuple((str(name), Path(path)) for name, path in strategies.items()) if isinstance(strategies, Mapping) else tuple((path.stem, Path(path)) for path in strategies)
                if {name for name, _ in strategy_items} != set(members):
                    raise ArtifactMismatchError("strategy set does not match declared portfolio members")
                strategy_digests: dict[str, str] = {}
                for name, source in strategy_items:
                    if not source.is_file() or source.is_symlink():
                        raise ArtifactMismatchError(f"strategy source is not a regular file: {source}")
                    artifact = workspace.copy_file(source, f"strategy_{_safe_component(name, 'member')}.json", "strategy")
                    strategy_digests[name] = artifact.sha256
                workspace.write_file("settings_snapshot.json", _json_bytes(snapshot_for_manifest), "settings_snapshot")
                manifest = RunManifest(run_id, attempt_id, lock.target_identity, tuple(sorted(members)), tuple(sorted(expected)), binary_identity if binary_identity is not None else capabilities.get("binary_identity"), settings_identity if settings_identity is not None else capabilities.get("settings_identity"), tick_identity if tick_identity is not None else capabilities.get("tick_identity"), workspace.artifacts)
                workspace.write_manifest(manifest)
                install = getattr(self.transport, "install", None)
                if not callable(install):
                    raise TransportContractError("portfolio install transport is unavailable")
                manifest = self._transition(workspace, manifest, "INSTALLING")
                install_evidence = _call(install, {"workspace": workspace.root, "members": members, "run_id": run_id, "attempt_id": attempt_id}, required=("workspace", "members", "run_id", "attempt_id"))
                workspace.write_file("install_evidence.json", _json_bytes(_evidence(install_evidence, "install", run_id=run_id, attempt_id=attempt_id)), "install_evidence")
                manifest = self._transition(workspace, manifest, "INSTALLED")
                manifest = self._transition(workspace, manifest, "RUNNING")
                run_value = self._operation(capabilities, run_id=run_id, attempt_id=attempt_id, members=members, timeout_seconds=timeout_seconds, cancel_check=cancel_check)
                stopped_after = self._quiesce(run_id=run_id, attempt_id=attempt_id)
                workspace.write_file(
                    "stopped_after.json", _json_bytes(_redacted(stopped_after)),
                    "process_evidence",
                )
                reports, report_metadata = self._report_values(run_value, expected, run_id=run_id, attempt_id=attempt_id)
                report_paths = self._copy_reports(
                    workspace, reports, report_metadata, expected, capabilities,
                    run_id=run_id, attempt_id=attempt_id, started_ns=started_ns,
                    tester_settings_sha256=settings_artifact.sha256,
                    strategy_sha256=strategy_digests, test_start=test_start, test_end=test_end,
                )
                if len(report_paths) != len(expected) or tuple(path.name.removeprefix("report_").removesuffix(".html") for path in report_paths) != expected:
                    raise ArtifactMismatchError("report/member cardinality or binding mismatch")
                m6_proof: M6CommitReadbackProof | None = None
                m6_attempted = report_decoder is not None or portfolio_store is not None
                normalized = []
                if m6_attempted:
                    from .metrics import calculate_metrics
                    from .reports import normalize_report
                    if report_decoder is not None and portfolio_store is None:
                        from .store import PortfolioStoreError
                        raise PortfolioStoreError("portfolio report decoding requires publication store")
                    for member, path in zip(expected, report_paths):
                        decoder = report_decoder.get(member) if isinstance(report_decoder, Mapping) else report_decoder
                        normalized_report = normalize_report(
                            path.read_bytes(), decoder=decoder, source_report_name=path.name,
                            source_report_sha256=_sha256(path), run_id=run_id, attempt_id=attempt_id, member=member,
                        )
                        calculate_metrics(normalized_report, planned_leverage=planned_leverage)
                        normalized.append(normalized_report)
                manifest = self._transition(workspace, manifest, "REPORTS_VALIDATED")
                try:
                    restore_evidence = self._restore(snapshot, run_id=run_id, attempt_id=attempt_id)
                    workspace.write_file("restore_evidence.json", _json_bytes(restore_evidence), "restore_evidence")
                    manifest = self._transition(workspace, manifest, "RESTORED")
                except BaseException as error:
                    release_lock = False
                    manifest = self._transition(workspace, manifest, "RESTORE_FAILED")
                    raise SnapshotError("settings restore failed; run evidence retained") from error
                if m6_attempted:
                    publish = getattr(portfolio_store, "publish_portfolio_run", None) or getattr(portfolio_store, "publish_run", None)
                    if not callable(publish):
                        from .store import PortfolioStoreError
                        raise PortfolioStoreError("portfolio store publication is unavailable")
                    publish_kwargs = {"attempt_id": attempt_id, "executable_identity": {"binary": binary_identity if binary_identity is not None else capabilities.get("binary_identity"), "settings": settings_identity if settings_identity is not None else capabilities.get("settings_identity"), "ticks": tick_identity if tick_identity is not None else capabilities.get("tick_identity")}}
                    if planned_leverage is not None:
                        publish_kwargs["planned_leverage"] = planned_leverage
                    m6_proof = publish(run_id, tuple(normalized), **publish_kwargs)
                # The restore evidence and this pre-release state are durable before
                # the target lease is touched.  Releasing the lease is the last
                # target mutation; the terminal transition is safe to retry after a
                # crash in the small window that follows it.
                manifest = self._transition(workspace, manifest, "READY_TO_RELEASE")
                lock.release()
                release_lock = False
                manifest = self._transition(workspace, manifest, "COMPLETED")
                # Once M6 was requested, a legacy M5 proof cannot authorize
                # deletion if publication/readback did not produce a proof.
                cleanup = workspace.cleanup(m6_proof if m6_attempted else cleanup_gate, manifest)
                retained_reports = tuple(path for path in report_paths if path.is_file() and not path.is_symlink())
                return PortfolioRunResult(run_id, attempt_id, "COMPLETED", workspace.root, workspace.manifest_path, tuple() if cleanup else retained_reports, cleanup, m6_proof)
            except BaseException as error:
                primary = error
                if isinstance(error, OperationUnconfirmedError):
                    release_lock = False
                    if manifest is not None:
                        manifest = self._transition(workspace, manifest, "OPERATION_UNCONFIRMED")
                elif manifest is not None and manifest.status not in {"COMPLETED", "RESTORE_FAILED", "READY_TO_RELEASE", "RESTORED"}:
                    manifest = self._transition(workspace, manifest, "FAILED")
                raise
            finally:
                if release_lock and snapshot is not None and (manifest is None or manifest.status not in {"COMPLETED", "RESTORE_FAILED", "READY_TO_RELEASE", "RESTORED"}):
                    try:
                        evidence = self._restore(snapshot, run_id=run_id, attempt_id=attempt_id)
                        workspace.write_file("restore_evidence.json", _json_bytes(evidence), "restore_evidence")
                    except BaseException as restore_error:
                        release_lock = False
                        if manifest is not None:
                            self._transition(workspace, manifest, "RESTORE_FAILED")
                        if primary is None:
                            raise SnapshotError("settings restore failed; run evidence retained") from restore_error
        finally:
            if release_lock:
                lock.release()

    def discover_incomplete(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        if self.workspace_root.is_dir():
            for run_root in self.workspace_root.iterdir():
                if not run_root.is_dir() or run_root.is_symlink():
                    raise ArtifactMismatchError(f"prior run is malformed: {run_root.name}")
                try:
                    _safe_component(run_root.name, "run_id")
                except ValueError as error:
                    raise ArtifactMismatchError(f"prior run is malformed: {run_root.name}") from error
                self._existing(run_root.name)
        for path in self.workspace_root.glob("*/*/run_manifest.json"):
            _, document = RunWorkspace.load(path)
            if document.get("status") not in {"COMPLETED", "COMMITTED", "RECOVERED"}:
                paths.append(path)
        return tuple(sorted(paths))

    def recover(self, manifest_path: Path) -> None:
        workspace, document = RunWorkspace.load(manifest_path)
        if document.get("target_identity") != canonical_tester_target(self.target):
            raise ArtifactMismatchError("run manifest target does not match recovery target")
        if document.get("status") in {"COMPLETED", "COMMITTED", "RECOVERED"}:
            raise PortfolioRunnerError("only incomplete portfolio runs can be recovered")
        manifest = RunManifest(workspace.run_id, workspace.attempt_id, str(document["target_identity"]), tuple(document["members"]), tuple(document["expected_reports"]), document.get("binary_identity"), document.get("settings_identity"), document.get("tick_identity"), workspace.artifacts, str(document["status"]))
        if manifest.status == "READY_TO_RELEASE":
            # Restore was already attested and fsynced.  Never reapply its stale
            # snapshot: a new owner may have acquired the target after release.
            if not (workspace.root / "restore_evidence.json").is_file():
                raise SnapshotError("ready-to-release run has no restore evidence")
            try:
                if TesterTargetLock(self.target, lock_path=self.lock_path).path.exists():
                    lock = TesterTargetLock(self.target, lock_path=self.lock_path).acquire()
                    lock.release()
                terminal_status = (
                    "RECOVERED"
                    if (workspace.root / "recovery_stopped.json").is_file()
                    else "COMPLETED"
                )
                self._transition(workspace, manifest, terminal_status)
                return
            except BaseException as error:
                raise SnapshotError("settings recovery could not finish lease release") from error
        try:
            snapshot = json.loads((workspace.root / "settings_snapshot.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SnapshotError("recoverable settings snapshot is unavailable") from error
        self._transition(workspace, manifest, "INTERRUPTED")
        lock: TesterTargetLock | None = None
        try:
            lock = TesterTargetLock(self.target, lock_path=self.lock_path).acquire()
            stopped = self._quiesce(run_id=workspace.run_id, attempt_id=workspace.attempt_id)
            workspace.write_file(
                "recovery_stopped.json", _json_bytes(_redacted(stopped)), "process_evidence"
            )
            evidence = self._restore(snapshot, run_id=workspace.run_id, attempt_id=workspace.attempt_id)
            workspace.write_file("restore_evidence.json", _json_bytes(evidence), "restore_evidence")
            manifest = self._transition(workspace, manifest, "READY_TO_RELEASE")
            lock.release()
            lock = None
            self._transition(workspace, manifest, "RECOVERED")
        except BaseException as error:
            if manifest.status != "READY_TO_RELEASE":
                self._transition(workspace, manifest, "RECOVERY_FAILED")
            raise SnapshotError("settings recovery failed; run evidence retained") from error

    def recover_incomplete(self) -> tuple[Path, ...]:
        paths = self.discover_incomplete()
        for path in paths:
            self.recover(path)
        return paths


PortfolioRunner = PortfolioTesterRunner
