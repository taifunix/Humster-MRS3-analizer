from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import sys
from typing import BinaryIO
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psutil


class OutputDirectoryBusyError(RuntimeError):
    """Raised when another selection process owns the output directory."""


class OutputDirectoryLock:
    """Cross-platform advisory lock held for one complete selection run."""

    def __init__(self, output_directory: Path) -> None:
        self.output_directory = output_directory.resolve()
        self.path = self.output_directory / ".mrs3-selection.lock"
        self._handle: BinaryIO | None = None

    def __enter__(self) -> OutputDirectoryLock:
        self.output_directory.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise OutputDirectoryBusyError(
                f"output directory is already being written: {self.output_directory}"
            ) from error
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None


class TesterTargetLockError(RuntimeError):
    """A tester target cannot be safely claimed."""


class TesterTargetBusyError(TesterTargetLockError):
    """A verified live owner already holds the tester target."""

    __test__ = False


class TesterTargetOwnerUnverifiableError(TesterTargetLockError):
    """The existing owner cannot be safely identified or reclaimed."""

    __test__ = False


class TesterTargetReleaseError(TesterTargetLockError):
    """The caller could not prove that it released its own target lease."""

    __test__ = False

    def __init__(self, message: str, *, owner: OwnerEvidence | None = None) -> None:
        super().__init__(message)
        self.owner = owner


@dataclass(frozen=True, slots=True)
class OwnerEvidence:
    pid: int
    process_start_identity: str
    host_identity: str
    machine_identity: str
    boot_identity: str
    container_identity: str
    pid_namespace_identity: str
    lock_kind: str
    canonical_target: str
    acquisition_token: str
    acquired_at_utc: str

    def as_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "process_start_identity": self.process_start_identity,
            "host_identity": self.host_identity,
            "machine_identity": self.machine_identity,
            "boot_identity": self.boot_identity,
            "container_identity": self.container_identity,
            "pid_namespace_identity": self.pid_namespace_identity,
            "lock_kind": self.lock_kind,
            "canonical_target": self.canonical_target,
            "target_identity": self.canonical_target,
            "target_path": self.canonical_target,
            "acquisition_token": self.acquisition_token,
            "acquired_at_utc": self.acquired_at_utc,
        }

    @classmethod
    def from_dict(cls, value: object) -> OwnerEvidence:
        if not isinstance(value, dict):
            raise ValueError("owner evidence must be an object")
        try:
            pid = int(value["pid"])
            fields = {
                name: value[name]
                for name in (
                    "process_start_identity", "host_identity", "machine_identity",
                    "boot_identity", "container_identity", "pid_namespace_identity",
                    "lock_kind", "canonical_target", "target_identity",
                    "acquisition_token", "acquired_at_utc",
                )
            }
            if any(not isinstance(item, str) for item in fields.values()):
                raise ValueError("owner evidence fields must be strings")
            process_start = fields["process_start_identity"]
            host = fields["host_identity"]
            machine = fields["machine_identity"]
            boot = fields["boot_identity"]
            container = fields["container_identity"]
            namespace = fields["pid_namespace_identity"]
            lock_kind = fields["lock_kind"]
            target = fields["canonical_target"]
            if fields["target_identity"] != target:
                raise ValueError("target identity mismatch")
            token = fields["acquisition_token"]
            acquired = fields["acquired_at_utc"]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("owner evidence is incomplete") from error
        if pid <= 0 or not all(
            (process_start, host, machine, boot, container, namespace, lock_kind, target, token, acquired)
        ) or process_start.casefold() == "unknown":
            raise ValueError("owner evidence is incomplete")
        try:
            if not math.isfinite(float(process_start)) or float(process_start) <= 0:
                raise ValueError("process start identity is invalid")
        except (TypeError, ValueError) as error:
            raise ValueError("process start identity is invalid") from error
        return cls(pid, process_start, host, machine, boot, container, namespace, lock_kind, target, token, acquired)

    @property
    def target_identity(self) -> str:
        return self.canonical_target


class _ProcessProbe:
    def __init__(self, start: float | None, *, unknown: bool = False) -> None:
        self.start, self.unknown = start, unknown


def _current_process_start() -> str:
    return str(psutil.Process(os.getpid()).create_time())


def _current_boot_identity() -> str:
    # boot_id is stable across process restarts and changes on container boot.
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        value = ""
    # Windows derives this value from uptime and its fractional part drifts
    # between processes.  A UTC-second boot timestamp is stable for the boot
    # while retaining more structure than an integer epoch comparison.
    if value:
        return value
    boot = datetime.fromtimestamp(float(psutil.boot_time()), timezone.utc)
    return boot.isoformat(timespec="seconds")


def _container_identity() -> str:
    machine = platform.node() or socket.gethostname()
    if sys.platform.startswith("linux"):
        # /proc/self/cgroup can be scoped to a transient service/session.  PID
        # namespace plus init's cgroup is stable for the containing runtime.
        namespace = _pid_namespace_identity(machine)
        init_cgroup = ""
        for path in (Path("/proc/1/cgroup"), Path("/run/.containerenv"), Path("/.dockerenv")):
            try:
                value = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                continue
            if value and value not in {"/", "0::/"}:
                init_cgroup = value
                break
        stable = "|".join((machine, namespace, init_cgroup or _current_boot_identity()))
        return "linux:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()
    # A native host is itself the runtime container; bind it to this boot so a
    # stale owner cannot cross a reboot under the same machine name.
    return f"native:{machine}:{_current_boot_identity()}"


def _pid_namespace_identity(machine_identity: str) -> str:
    if sys.platform.startswith(("win", "cygwin")):
        return f"windows-host:{machine_identity}" if machine_identity else "unknown"
    if sys.platform.startswith("linux"):
        try:
            return str(os.stat("/proc/self/ns/pid").st_ino)
        except (AttributeError, OSError):
            return "unknown"
    # Other hosts have no portable namespace API.  The machine identity is a
    # stable local fallback, so stale leases remain reclaimable after restart.
    return f"host:{machine_identity}" if machine_identity else "unknown"


def _known_identity(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.casefold() != "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


@contextmanager
def _locked_gate(path: Path, existing: object | None = None):
    if existing is not None:
        yield existing
        return
    try:
        handle = path.open("a+b")
    except OSError as error:
        raise TesterTargetOwnerUnverifiableError("LOCK_GATE_UNAVAILABLE") from error
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise TesterTargetBusyError("tester target lock gate is busy") from error
        locked = True
        yield handle
    finally:
        if locked:
            with suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with suppress(OSError):
            handle.close()


def _publish_attestation(audit_path: Path, event: dict[str, object]) -> Path:
    audit_path.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _fsync_directory(audit_path.parent)
    for _ in range(3):
        token = uuid4().hex
        temporary = audit_path / f".{token}.tmp"
        published = audit_path / f"event-{token}.json"
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, published)
            except FileExistsError:
                continue
            _fsync_directory(audit_path)
            return published
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
    raise TesterTargetOwnerUnverifiableError("could not publish lock attestation")


def _remove_lock_if_unchanged(lock_path: Path, expected: bytes, gate: object | None = None) -> bool:
    guard = lock_path.with_name(f".{lock_path.name}.remove-{uuid4().hex}.tmp")
    with _locked_gate(Path(str(lock_path) + ".gate.v1"), gate):
        try:
            try:
                os.link(lock_path, guard)
            except (FileExistsError, FileNotFoundError):
                return False
            try:
                if guard.read_bytes() != expected or not os.path.samefile(lock_path, guard):
                    return False
                lock_path.unlink()
                return True
            except FileNotFoundError:
                return False
            finally:
                with suppress(FileNotFoundError):
                    guard.unlink()
        finally:
            with suppress(FileNotFoundError):
                guard.unlink()


def _default_process_probe(pid: int) -> _ProcessProbe:
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return _ProcessProbe(None)
        return _ProcessProbe(float(process.create_time()))
    except psutil.NoSuchProcess:
        return _ProcessProbe(None)
    except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return _ProcessProbe(None, unknown=True)


def canonical_tester_target(target: Path | str) -> str:
    """Return the identity used by all local and caller-provided remote owners."""
    value = os.fspath(target) if isinstance(target, Path) else str(target).strip()
    if not value:
        raise ValueError("tester target identity cannot be empty")
    if value.casefold().startswith(("remote://", "ssh://", "http://", "https://")):
        parsed = urlsplit(value)
        if parsed.hostname is None or parsed.password is not None or parsed.query or parsed.fragment:
            label = "credentials" if parsed.password is not None else "identity"
            raise ValueError(f"remote tester target {label} is invalid")
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("remote tester target identity is invalid") from error
        host = parsed.hostname.casefold()
        if ":" in host:
            host = f"[{host}]"
        authority = f"{parsed.username}@" if parsed.username is not None else ""
        authority += host
        if port is not None:
            authority += f":{port}"
        return urlunsplit((parsed.scheme.casefold(), authority, parsed.path.rstrip("/"), "", ""))
    # realpath collapses symlink aliases; normcase collapses Windows aliases.
    return os.path.normcase(os.path.realpath(os.path.abspath(value)))


def tester_target_lock_path(target: Path | str, *, lock_path: Path | None = None) -> Path:
    if lock_path is not None:
        return Path(os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(lock_path)))))
    value = os.fspath(target) if isinstance(target, Path) else str(target).strip()
    if value.casefold().startswith(("remote://", "ssh://", "http://", "https://")):
        raise ValueError("a remote tester target requires an explicit local lock_path")
    return Path(canonical_tester_target(value)) / ".mrs3-tester-target.lock"


class TesterTargetLock:
    """One target-wide, cross-process owner shared by every tester caller."""

    __test__ = False

    def __init__(
        self,
        target: Path | str,
        *,
        lock_path: Path | None = None,
        host: str | None = None,
        boot: str | None = None,
        machine: str | None = None,
        container: str | None = None,
        pid_namespace: str | None = None,
        lock_kind: str = "tester_target_lease",
        process_probe: object | None = None,
    ) -> None:
        self.target_identity = canonical_tester_target(target)
        self.path = tester_target_lock_path(target, lock_path=lock_path)
        self.host = host or platform.node() or socket.gethostname()
        self.boot = boot or _current_boot_identity()
        self.machine = machine or platform.node() or self.host
        self.container = container or _container_identity()
        self.pid_namespace = pid_namespace or _pid_namespace_identity(self.machine)
        self.lock_kind = lock_kind
        self._process_probe = process_probe
        self.owner: OwnerEvidence | None = None

    def _probe(self, pid: int) -> _ProcessProbe:
        if self._process_probe is None:
            return _default_process_probe(pid)
        try:
            value = self._process_probe(pid)  # type: ignore[operator]
        except (psutil.NoSuchProcess, ProcessLookupError):
            return _ProcessProbe(None)
        except Exception as error:
            raise TesterTargetOwnerUnverifiableError(
                "LOCK_OWNER_UNVERIFIABLE: process identity probe failed"
            ) from error
        if value is False:
            return _ProcessProbe(None)
        if value is None:
            return _ProcessProbe(None, unknown=True)
        if isinstance(value, _ProcessProbe):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _ProcessProbe(float(value))
        if isinstance(value, tuple) and len(value) == 2:
            live, start = value
            if live is False:
                return _ProcessProbe(None)
            if isinstance(start, (int, float)) and float(start) > 0:
                return _ProcessProbe(float(start))
            return _ProcessProbe(None, unknown=True)
        return _ProcessProbe(None, unknown=True)

    def _read_owner(self) -> tuple[OwnerEvidence, bytes]:
        try:
            raw = self.path.read_bytes()
            return OwnerEvidence.from_dict(json.loads(raw.decode("utf-8"))), raw
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise TesterTargetOwnerUnverifiableError(
                "LOCK_OWNER_UNVERIFIABLE: owner evidence is invalid"
            ) from error

    def _assert_reclaimable(self, owner: OwnerEvidence) -> None:
        try:
            owner_target = canonical_tester_target(owner.canonical_target)
        except ValueError:
            owner_target = ""
        if owner.lock_kind != self.lock_kind or owner_target != self.target_identity:
            raise TesterTargetOwnerUnverifiableError(
                "LOCK_OWNER_UNVERIFIABLE: target or lock kind mismatch"
            )
        if (
            not _known_identity(owner.host_identity)
            or not _known_identity(owner.machine_identity)
            or not _known_identity(owner.boot_identity)
            or not _known_identity(owner.container_identity)
            or not _known_identity(owner.pid_namespace_identity)
            or owner.host_identity != self.host
            or owner.machine_identity != self.machine
            or owner.boot_identity != self.boot
            or owner.container_identity != self.container
            or owner.pid_namespace_identity != self.pid_namespace
        ):
            raise TesterTargetOwnerUnverifiableError(
                "LOCK_OWNER_UNVERIFIABLE: foreign or unknown identity"
            )
        self._assert_process_not_live(owner)

    def _assert_process_not_live(self, owner: OwnerEvidence) -> None:
        probe = self._probe(owner.pid)
        if probe.unknown:
            raise TesterTargetOwnerUnverifiableError(
                "LOCK_OWNER_UNVERIFIABLE: process identity cannot be verified"
            )
        if probe.start is not None:
            try:
                same_start = math.isclose(
                    probe.start, float(owner.process_start_identity), rel_tol=0, abs_tol=1e-6
                )
            except ValueError as error:
                raise TesterTargetOwnerUnverifiableError(
                    "LOCK_OWNER_UNVERIFIABLE: process start identity is invalid"
                ) from error
            if same_start:
                raise TesterTargetBusyError(
                    f"tester target is already owned by pid {owner.pid}"
                )
        # Missing PID or a different process-start proves the recorded owner is dead
        # or the PID was reused. The new owner gets a fresh acquisition token.

    def acquire(self) -> TesterTargetLock:
        if not self.path.parent.is_dir():
            raise FileNotFoundError(f"tester target does not exist: {self.path.parent}")
        owner = OwnerEvidence(
            pid=os.getpid(),
            process_start_identity=_current_process_start(),
            host_identity=self.host,
            machine_identity=self.machine,
            boot_identity=self.boot,
            container_identity=self.container,
            pid_namespace_identity=self.pid_namespace,
            lock_kind=self.lock_kind,
            canonical_target=self.target_identity,
            acquisition_token=uuid4().hex,
            acquired_at_utc=_utc_now(),
        )
        payload = (json.dumps(owner.as_dict(), sort_keys=True) + "\n").encode("utf-8")
        temporary = self.path.with_name(f".{self.path.name}.{owner.acquisition_token}.tmp")
        try:
            with temporary.open("xb") as staged:
                staged.write(payload)
                staged.flush()
                os.fsync(staged.fileno())
            with _locked_gate(self.gate_path) as gate:
                try:
                    os.link(temporary, self.path)
                except FileExistsError as error:
                    existing, raw = self._read_owner()
                    self._assert_reclaimable(existing)
                    try:
                        removed = _remove_lock_if_unchanged(self.path, raw, gate)
                    except OSError as link_error:
                        raise TesterTargetOwnerUnverifiableError(
                            "LOCK_OWNER_UNVERIFIABLE: lock link is unsupported"
                        ) from link_error
                    if not removed:
                        raise TesterTargetBusyError("tester target owner changed during reclaim") from error
                    try:
                        os.link(temporary, self.path)
                    except FileExistsError as race:
                        raise TesterTargetBusyError("tester target is already owned") from race
                    except OSError as link_error:
                        raise TesterTargetOwnerUnverifiableError(
                            "LOCK_OWNER_UNVERIFIABLE: lock link is unsupported"
                        ) from link_error
                except OSError as link_error:
                    raise TesterTargetOwnerUnverifiableError(
                        "LOCK_OWNER_UNVERIFIABLE: lock link is unsupported"
                    ) from link_error
            self.owner = owner
            return self
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    @property
    def gate_path(self) -> Path:
        return Path(str(self.path) + ".gate.v1")

    def release(self) -> None:
        owner = self.owner
        if owner is None:
            return
        expected = json.dumps(owner.as_dict(), sort_keys=True).encode("utf-8") + b"\n"
        try:
            removed = _remove_lock_if_unchanged(self.path, expected)
        except TesterTargetLockError as error:
            raise TesterTargetReleaseError(
                f"LOCK_RELEASE_FAILED: {error}", owner=owner
            ) from error
        except OSError as error:
            raise TesterTargetReleaseError(
                "LOCK_RELEASE_FAILED: lock link is unsupported", owner=owner
            ) from error
        if not removed:
            # Keep owner evidence available for a bounded retry or operator audit.
            raise TesterTargetReleaseError(
                "LOCK_RELEASE_FAILED: owner evidence changed or disappeared", owner=owner
            )
        self.owner = None

    def __enter__(self) -> TesterTargetLock:
        return self.acquire()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()


def manual_clear_tester_target_lock(
    target: Path | str,
    *,
    operator_identity: str,
    reason: str,
    attestation: str = "operator_attestation_v1",
    lock_path: Path | None = None,
) -> Path:
    """Clear only through an explicit, audited operator attestation."""
    if not all(isinstance(value, str) and value.strip() for value in (operator_identity, reason, attestation)):
        raise ValueError("operator_identity, reason, and attestation are required")
    path = tester_target_lock_path(target, lock_path=lock_path)
    checker = TesterTargetLock(target, lock_path=path)
    owner, raw = checker._read_owner()
    if owner.lock_kind != checker.lock_kind or canonical_tester_target(owner.canonical_target) != checker.target_identity:
        raise TesterTargetOwnerUnverifiableError(
            "LOCK_OWNER_UNVERIFIABLE: target or lock kind mismatch"
        )
    same_host_machine = (
        owner.host_identity == checker.host
        and owner.machine_identity == checker.machine
    )
    if same_host_machine:
        # Manual attestation is for retired/unreachable runtimes, never a bypass
        # around a live or unverifiable local owner.
        checker._assert_process_not_live(owner)
    audit = Path(str(path) + ".audit.v1")
    record = {
        "attestation_version": 1,
        "lock_kind": owner.lock_kind,
        "lock_path": str(path),
        "canonical_path": owner.canonical_target,
        "target_path": owner.canonical_target,
        "stale_owner": owner.as_dict(),
        "operator_identity": operator_identity.strip(),
        "cleared_at_utc": _utc_now(),
        "reason": "LOCK_MANUAL_CLEAR",
        "operator_reason": reason.strip(),
        "attestation": attestation.strip(),
    }
    with _locked_gate(Path(str(path) + ".gate.v1") ) as gate:
        published = _publish_attestation(audit, record)
        if not _remove_lock_if_unchanged(path, raw, gate):
            raise TesterTargetOwnerUnverifiableError(
                "LOCK_OWNER_UNVERIFIABLE: owner changed during manual clear"
            )
    return published
