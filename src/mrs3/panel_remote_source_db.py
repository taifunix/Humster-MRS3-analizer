"""Validation and verified local delivery primitives for remote Source DBs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import secrets
import subprocess
import tempfile
from threading import RLock
import time

from .panel_remote_testing import RemoteRunnerConfig


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PHASES = ("REMOTE_IMPORTED", "TRANSFERRING", "VERIFIED", "COMMITTED")


class RemoteSourceDbError(ValueError):
    """Stable client-safe error for remote Source DB validation/delivery."""


@dataclass(frozen=True, slots=True)
class RemoteDbEvidence:
    size_bytes: int
    sha256: str

    @classmethod
    def from_value(cls, value: object) -> "RemoteDbEvidence":
        if isinstance(value, cls):
            size_bytes, digest = value.size_bytes, value.sha256
        elif isinstance(value, Mapping):
            size_bytes, digest = value.get("size_bytes"), value.get("sha256")
        else:
            try:
                size_bytes, digest = value.size_bytes, value.sha256  # type: ignore[union-attr]
            except BaseException:
                raise RemoteSourceDbError("invalid remote source db evidence") from None
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            raise RemoteSourceDbError("invalid remote source db evidence")
        return cls(size_bytes, digest)

    def as_dict(self) -> dict[str, object]:
        return {"size_bytes": self.size_bytes, "sha256": self.sha256}


@dataclass(frozen=True, slots=True, repr=False)
class RemoteSourceDbRequest:
    remote_html_path: str
    remote_db_target: str
    local_target: Path

    def __repr__(self) -> str:
        return "RemoteSourceDbRequest(<redacted>)"


@dataclass(slots=True)
class _RemoteImportJob:
    job_id: str
    remote_html_path: str
    remote_db_target: str
    stage_path: str
    pid_path: str
    log_path: str
    state: str = "RUNNING"
    evidence: RemoteDbEvidence | None = None
    progress_current: int = 0
    progress_total: int = 0
    progress_workers: int = 0
    started_at_epoch: int = 0
    local_target: Path | None = None
    transfer_temp: Path | None = None
    transfer_process: object | None = None
    transfer_started_at_epoch: int = 0


def _remote_path(value: object) -> str:
    if not isinstance(value, str) or _CONTROL.search(value) or "\\" in value:
        raise RemoteSourceDbError("invalid remote source db request")
    if not value.startswith("/") or value.startswith("//"):
        raise RemoteSourceDbError("invalid remote source db request")
    if any(part in {".", ".."} for part in value.split("/")):
        raise RemoteSourceDbError("invalid remote source db request")
    return posixpath.normpath(value)


def _basename(remote_path: str) -> str:
    return PurePosixPath(remote_path).name


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


class RemoteSourceDbService:
    """Injectable remote-evidence and local verified-transfer boundary."""

    def __init__(
        self,
        *,
        source_db_root: str,
        read_remote_evidence: Callable[[str], object],
        download: Callable[[str, Path], object],
    ) -> None:
        self.source_db_root = _remote_path(source_db_root)
        if not callable(read_remote_evidence) or not callable(download):
            raise RemoteSourceDbError("remote source db executor unavailable")
        self._read_remote_evidence = read_remote_evidence
        self._download = download

    def prepare_request(
        self,
        remote_html_path: str,
        remote_db_target: str,
        local_target: Path,
    ) -> RemoteSourceDbRequest:
        html_path = _remote_path(remote_html_path)
        db_target = _remote_path(remote_db_target)
        root = self.source_db_root.rstrip("/")
        if db_target == root or not db_target.startswith(f"{root}/"):
            raise RemoteSourceDbError("invalid remote source db request")
        target = Path(local_target)
        if target.exists() or target.is_symlink():
            raise RemoteSourceDbError("source db target already exists")
        return RemoteSourceDbRequest(html_path, db_target, target)

    def remote_import(self, request: RemoteSourceDbRequest) -> RemoteDbEvidence:
        try:
            raw = self._read_remote_evidence(request.remote_db_target)
        except BaseException:
            raise RemoteSourceDbError("remote source db import failed") from None
        try:
            return RemoteDbEvidence.from_value(raw)
        except RemoteSourceDbError:
            raise
        except BaseException:
            raise RemoteSourceDbError("invalid remote source db evidence") from None

    def deliver(
        self,
        request: RemoteSourceDbRequest,
        evidence: RemoteDbEvidence | Mapping[str, object],
    ) -> dict[str, object]:
        expected = RemoteDbEvidence.from_value(evidence)
        target = request.local_target
        temporary: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise RemoteSourceDbError("source db target already exists")
            descriptor, name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".part", dir=target.parent
            )
            os.close(descriptor)
            temporary = Path(name)
            self._download(request.remote_db_target, temporary)
            result = self.publish_temporary(request, expected, temporary)
            temporary = None
            return result
        except RemoteSourceDbError:
            raise
        except BaseException:
            raise RemoteSourceDbError("remote source db transfer failed") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def publish_temporary(
        self, request: RemoteSourceDbRequest, evidence: RemoteDbEvidence | Mapping[str, object], temporary: Path
    ) -> dict[str, object]:
        expected = RemoteDbEvidence.from_value(evidence)
        target = request.local_target
        if temporary.is_symlink() or not temporary.is_file():
            raise RemoteSourceDbError("remote source db transfer failed")
        actual_size, actual_digest = self._digest(temporary)
        if actual_size != expected.size_bytes or actual_digest != expected.sha256:
            raise RemoteSourceDbError("remote source db transfer failed")
        if target.exists() or target.is_symlink():
            raise RemoteSourceDbError("source db target already exists")
        try:
            os.link(temporary, target)
        except FileExistsError:
            raise RemoteSourceDbError("source db target already exists") from None
        except OSError:
            raise RemoteSourceDbError("remote source db transfer failed") from None
        temporary.unlink(missing_ok=True)
        return {
            "phase": "COMMITTED",
            "phases": list(_PHASES),
            "remote_db": _basename(request.remote_db_target),
            "local_target": target.name,
            "evidence": expected.as_dict(),
        }

    def run(
        self,
        remote_html_path: str,
        remote_db_target: str,
        local_target: Path,
    ) -> dict[str, object]:
        request = self.prepare_request(remote_html_path, remote_db_target, local_target)
        evidence = self.remote_import(request)
        return self.deliver(request, evidence)

    @staticmethod
    def _digest(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()


class RemoteSourceDbExecutor:
    """Fixed-command remote importer using verified local delivery."""

    def __init__(
        self,
        config: RemoteRunnerConfig,
        command_runner: Callable[[tuple[str, ...]], str] | None = None,
        file_downloader: Callable[[tuple[str, ...]], object] | None = None,
        transfer_starter: Callable[[tuple[str, ...]], object] | None = None,
    ) -> None:
        self.config = self._validate_config(config)
        if command_runner is not None and not callable(command_runner):
            raise RemoteSourceDbError("remote command runner unavailable")
        if file_downloader is not None and not callable(file_downloader):
            raise RemoteSourceDbError("remote file downloader unavailable")
        if transfer_starter is not None and not callable(transfer_starter):
            raise RemoteSourceDbError("remote file downloader unavailable")
        self._command_runner = command_runner or self._default_command_runner
        self._file_downloader = file_downloader or self._default_file_downloader
        self._transfer_starter = transfer_starter or self._default_transfer_starter
        self._service = RemoteSourceDbService(
            source_db_root=self.config.source_db_root,
            read_remote_evidence=lambda _path: None,
            download=self._download,
        )
        self._job_lock = RLock()
        self._jobs: dict[str, _RemoteImportJob] = {}

    @staticmethod
    def _validate_config(config: object) -> RemoteRunnerConfig:
        if not isinstance(config, RemoteRunnerConfig) or not config.enabled:
            raise RemoteSourceDbError("invalid remote runner configuration")
        try:
            if (
                not isinstance(config.enabled, bool)
                or not config.host
                or not config.user
                or not isinstance(config.host, str)
                or not isinstance(config.user, str)
                or not isinstance(config.password, str)
                or not isinstance(config.private_key_path, str)
                or _CONTROL.search(config.host)
                or _CONTROL.search(config.user)
                or _CONTROL.search(config.password)
                or _CONTROL.search(config.private_key_path)
                or isinstance(config.port, bool)
                or not isinstance(config.port, int)
                or not 1 <= config.port <= 65535
                or (config.password and config.private_key_path)
            ):
                raise ValueError
            for path in (
                config.bot_root,
                config.debian_runner_root,
                config.reports_root,
                config.source_db_root,
                config.reports_archive_root,
            ):
                _remote_path(path)
        except BaseException:
            raise RemoteSourceDbError("invalid remote runner configuration") from None
        return config

    def run(
        self,
        remote_html_path: str,
        remote_db_target: str,
        local_target: Path,
    ) -> dict[str, object]:
        request = self._service.prepare_request(remote_html_path, remote_db_target, local_target)
        allowed_html_roots = (
            self.config.reports_root.rstrip("/"),
            self.config.reports_archive_root.rstrip("/"),
        )
        if not any(
            request.remote_html_path == root
            or request.remote_html_path.startswith(f"{root}/")
            for root in allowed_html_roots
        ):
            raise RemoteSourceDbError("invalid remote source db request")
        script = self._import_script(request)
        try:
            output = self._command_runner(self._plink_argv(script))
        except BaseException:
            raise RemoteSourceDbError("remote source db import failed") from None
        evidence = self._parse_evidence(output)
        return self._service.deliver(request, evidence)

    def start_import(self, remote_html_path: str, remote_db_target: str, *, job_id: str | None = None) -> dict[str, object]:
        html_path, db_target = self._validate_remote_paths(remote_html_path, remote_db_target)
        with self._job_lock:
            assigned = job_id is not None
            job_id = job_id or secrets.token_hex(16)
            if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 128:
                raise RemoteSourceDbError("invalid remote source db request")
            while job_id in self._jobs:
                if assigned:
                    raise RemoteSourceDbError("invalid remote source db request")
                job_id = secrets.token_hex(16)
            stage_path, pid_path, log_path = self._job_paths(job_id)
            script = self._start_script(job_id, html_path, db_target)
            try:
                marker = self._strict_marker(self._command_runner(self._plink_argv(script)))
            except BaseException:
                raise RemoteSourceDbError("remote source db import failed") from None
            if marker == "TARGET_EXISTS":
                raise RemoteSourceDbError("remote source db target already exists")
            if marker != "STARTED":
                raise RemoteSourceDbError("remote source db import failed")
            self._jobs[job_id] = _RemoteImportJob(
                job_id, html_path, db_target, stage_path, pid_path, log_path,
                started_at_epoch=int(time.time()),
            )
            return self._job_document(self._jobs[job_id])

    def status(self, job_id: str) -> dict[str, object]:
        with self._job_lock:
            job = self._job(job_id)
            if job.state in {"FAILED", "COMMITTED", "CANCELLED"}:
                return self._job_document(job)
            if job.state == "TRANSFERRING":
                return self._transfer_status(job)
            script = self._status_script(job)
            try:
                marker = self._strict_marker(self._command_runner(self._plink_argv(script)))
            except BaseException:
                job.state = "FAILED"
                return self._job_document(job)
            if marker == "RUNNING":
                if job.state != "CANCELLING":
                    job.state = "RUNNING"
                job.evidence = None
                return self._job_document(job)
            if marker.startswith("RUNNING "):
                try:
                    current, total, workers, started_at = self._parse_running(marker)
                except RemoteSourceDbError:
                    job.state = "FAILED"
                    return self._job_document(job)
                if job.state != "CANCELLING":
                    job.state = "RUNNING"
                job.progress_current = current
                job.progress_total = total
                job.progress_workers = workers
                job.started_at_epoch = started_at
                job.evidence = None
                return self._job_document(job)
            if marker == "FAILED":
                job.state = "CANCELLED" if job.state == "CANCELLING" else "FAILED"
                job.evidence = None
                return self._job_document(job)
            try:
                job.evidence = self._parse_remote_imported(marker)
            except RemoteSourceDbError:
                job.state = "FAILED"
            else:
                job.state = "CANCELLED" if job.state == "CANCELLING" else "REMOTE_IMPORTED"
            return self._job_document(job)

    def resume_import(self, job_id: str, remote_html_path: str, remote_db_target: str) -> dict[str, object]:
        """Rehydrate a known staging identity only to inspect or cancel it after restart."""
        html_path, db_target = self._validate_remote_paths(remote_html_path, remote_db_target)
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise RemoteSourceDbError("invalid remote source db request")
        with self._job_lock:
            if job_id not in self._jobs:
                stage_path, pid_path, log_path = self._job_paths(job_id)
                self._jobs[job_id] = _RemoteImportJob(job_id, html_path, db_target, stage_path, pid_path, log_path)
            return self._job_document(self._jobs[job_id])

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._job_lock:
            job = self._job(job_id)
            if job.state in {"FAILED", "COMMITTED", "CANCELLED"}:
                return self._job_document(job)
            if job.state == "REMOTE_IMPORTED":
                job.state = "CANCELLED"
                return self._job_document(job)
            if job.state == "TRANSFERRING":
                failed = False
                process = job.transfer_process
                wait = getattr(process, "wait", None)
                kill = getattr(process, "kill", None)
                try:
                    terminate = getattr(process, "terminate", None)
                    if not callable(terminate) or not callable(wait):
                        raise RemoteSourceDbError("remote source db transfer failed")
                    terminate()
                    try:
                        wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if not callable(kill):
                            raise RemoteSourceDbError("remote source db transfer failed")
                        kill()
                        wait(timeout=5)
                except (OSError, RemoteSourceDbError, subprocess.TimeoutExpired):
                    failed = True
                finally:
                    try:
                        if callable(getattr(process, "poll", None)) and process.poll() is None:
                            if not callable(kill) or not callable(wait):
                                failed = True
                            else:
                                kill()
                                wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        failed = True
                    try:
                        if job.transfer_temp is not None:
                            job.transfer_temp.unlink(missing_ok=True)
                    except OSError:
                        failed = True
                    job.transfer_temp = None
                    job.transfer_process = None
                    job.state = "FAILED" if failed else "CANCELLED"
                return self._job_document(job)
            script = self._cancel_script(job)
            try:
                marker = self._strict_marker(self._command_runner(self._plink_argv(script)))
            except BaseException:
                job.state = "FAILED"
                return self._job_document(job)
            if marker == "CANCELLING":
                job.state = "CANCELLING"
            else:
                job.state = "FAILED"
            return self._job_document(job)

    def deliver_import(self, job_id: str, local_target: Path) -> dict[str, object]:
        """Transfer only an already verified remote job into a fresh local target."""
        with self._job_lock:
            job = self._job(job_id)
            if job.state != "REMOTE_IMPORTED" or job.evidence is None:
                raise RemoteSourceDbError("remote source db is not ready for delivery")
            request = self._service.prepare_request(
                job.remote_html_path, job.remote_db_target, Path(local_target)
            )
            result = self._service.deliver(request, job.evidence)
            job.state = "COMMITTED"
            return result

    def start_delivery(self, job_id: str, local_target: Path) -> dict[str, object]:
        """Start a visible local download of an already verified remote database."""
        with self._job_lock:
            job = self._job(job_id)
            if job.state == "TRANSFERRING":
                return self._job_document(job)
            if job.state != "REMOTE_IMPORTED" or job.evidence is None:
                raise RemoteSourceDbError("remote source db is not ready for delivery")
            request = self._service.prepare_request(job.remote_html_path, job.remote_db_target, Path(local_target))
            request.local_target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=f".{request.local_target.name}.", suffix=".part", dir=request.local_target.parent
            )
            os.close(descriptor)
            temporary = Path(name)
            try:
                process = self._transfer_starter(self._pscp_argv(request.remote_db_target, temporary))
                if not callable(getattr(process, "poll", None)):
                    raise RemoteSourceDbError("remote source db transfer failed")
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise RemoteSourceDbError("remote source db transfer failed") from None
            job.local_target = request.local_target
            job.transfer_temp = temporary
            job.transfer_process = process
            job.transfer_started_at_epoch = int(time.time())
            job.state = "TRANSFERRING"
            return self._job_document(job)

    def _transfer_status(self, job: _RemoteImportJob) -> dict[str, object]:
        temporary, process, target, evidence = job.transfer_temp, job.transfer_process, job.local_target, job.evidence
        if temporary is None or process is None or target is None or evidence is None:
            job.state = "FAILED"
            return self._job_document(job)
        try:
            returncode = process.poll()
            if returncode is None:
                return self._job_document(job)
            if returncode != 0:
                raise RemoteSourceDbError("remote source db transfer failed")
            request = self._service.prepare_request(job.remote_html_path, job.remote_db_target, target)
            result = self._service.publish_temporary(request, evidence, temporary)
        except RemoteSourceDbError:
            temporary.unlink(missing_ok=True)
            job.state = "FAILED"
            return self._job_document(job)
        job.transfer_temp = None
        job.state = "COMMITTED"
        document = self._job_document(job)
        document.update(result)
        document["job_id"] = job.job_id
        document["state"] = "COMMITTED"
        return document

    def _validate_remote_paths(self, remote_html_path: str, remote_db_target: str) -> tuple[str, str]:
        html_path = _remote_path(remote_html_path)
        db_target = _remote_path(remote_db_target)
        if not any(
            html_path == root.rstrip("/") or html_path.startswith(f"{root.rstrip('/')}/")
            for root in (self.config.reports_root, self.config.reports_archive_root)
        ):
            raise RemoteSourceDbError("invalid remote source db request")
        root = self._service.source_db_root.rstrip("/")
        if db_target == root or not db_target.startswith(f"{root}/"):
            raise RemoteSourceDbError("invalid remote source db request")
        return html_path, db_target

    def _job_paths(self, job_id: str) -> tuple[str, str, str]:
        root = self._service.source_db_root.rstrip("/") or "/"
        stage = f"{root}/.mrs3-source-job-{job_id}"
        return stage, f"{stage}/pid", f"{stage}/import.log"

    def _job(self, job_id: str) -> _RemoteImportJob:
        if not isinstance(job_id, str):
            raise RemoteSourceDbError("remote source db job not found")
        try:
            return self._jobs[job_id]
        except KeyError:
            raise RemoteSourceDbError("remote source db job not found") from None

    def _start_script(self, job_id: str, html_path: str, db_target: str) -> str:
        root = self._service.source_db_root.rstrip("/") or "/"
        stage, pid_path, log_path = self._job_paths(job_id)
        importer = self.config.debian_runner_root.rstrip("/") + "/scripts/import-source-v6-debian.sh"
        return "\n".join(
            (
                "set -eu",
                f"root={_shell_quote(root)}",
                f"stage={_shell_quote(stage)}",
                f"html={_shell_quote(html_path)}",
                f"target={_shell_quote(db_target)}",
                f"importer={_shell_quote(importer)}",
                f"pid_file={_shell_quote(pid_path)}",
                f"log_file={_shell_quote(log_path)}",
                "mkdir -p -- \"$root\" || { printf 'FAILED\\n'; exit 0; }",
                "if [ -e \"$target\" ] || [ -L \"$target\" ]; then printf 'TARGET_EXISTS\\n'; exit 0; fi",
                "if [ -e \"$stage\" ] || [ -L \"$stage\" ]; then printf 'FAILED\\n'; exit 0; fi",
                "if [ ! -f \"$importer\" ] || [ -L \"$importer\" ]; then printf 'FAILED\\n'; exit 0; fi",
                "if ! command -v setsid >/dev/null 2>&1; then printf 'FAILED\\n'; exit 0; fi",
                "mkdir -- \"$stage\" || { printf 'FAILED\\n'; exit 0; }",
                "progress_file=\"$stage/progress\"",
                "nohup setsid sh \"$importer\" \"$html\" \"$target\" --progress \"$progress_file\" >\"$log_file\" 2>&1 </dev/null &",
                "pid=$!",
                "case \"$pid\" in ''|*[!0-9]*) printf 'FAILED\\n'; exit 0;; esac",
                "printf '%s\\n' \"$pid\" >\"$pid_file\" || { printf 'FAILED\\n'; exit 0; }",
                "printf 'STARTED\\n'",
            )
        )

    def _status_script(self, job: _RemoteImportJob) -> str:
        importer = self.config.debian_runner_root.rstrip("/") + "/scripts/import-source-v6-debian.sh"
        runner = self.config.debian_runner_root.rstrip("/") + "/scripts/import_source_v6_debian.py"
        return "\n".join(
            (
                "set -eu",
                f"pid_file={_shell_quote(job.pid_path)}",
                f"target={_shell_quote(job.remote_db_target)}",
                f"importer={_shell_quote(importer)}",
                f"runner={_shell_quote(runner)}",
                f"progress_file={_shell_quote(job.stage_path + '/progress')}",
                "if [ ! -f \"$pid_file\" ] || [ -L \"$pid_file\" ]; then printf 'FAILED\\n'; exit 0; fi",
                "pid=$(cat -- \"$pid_file\" 2>/dev/null) || { printf 'FAILED\\n'; exit 0; }",
                "case \"$pid\" in ''|*[!0-9]*) printf 'FAILED\\n'; exit 0;; esac",
                "if [ -r \"/proc/$pid/cmdline\" ]; then",
                "  cmdline=$(tr '\\000' ' ' <\"/proc/$pid/cmdline\" 2>/dev/null || true)",
                "  case \"$cmdline\" in",
                "    *\"$importer\"*\"$target\"*|*\"$runner\"*\"$target\"*)",
                "      if [ -f \"$progress_file\" ] && [ ! -L \"$progress_file\" ]; then",
                "        IFS=' ' read -r current total workers started extra <\"$progress_file\" || true",
                "        if [ -z \"${extra:-}\" ]; then",
                "          case \"${current:-}:${total:-}:${workers:-}:${started:-}\" in",
                "            *[!0-9:]*|:*|*::*) ;;",
                "            *) printf 'RUNNING %s %s %s %s\\n' \"$current\" \"$total\" \"$workers\" \"$started\"; exit 0;;",
                "          esac",
                "        fi",
                "      fi",
                "      printf 'RUNNING\\n'; exit 0;;",
                "  esac",
                "fi",
                "if [ ! -f \"$target\" ] || [ -L \"$target\" ]; then printf 'FAILED\\n'; exit 0; fi",
                "if ! size=$(wc -c <\"$target\"); then printf 'FAILED\\n'; exit 0; fi",
                "if ! digest=$(sha256sum -- \"$target\"); then printf 'FAILED\\n'; exit 0; fi",
                "digest=${digest%% *}",
                "printf 'REMOTE_IMPORTED %s %s\\n' \"$size\" \"$digest\"",
            )
        )

    def _cancel_script(self, job: _RemoteImportJob) -> str:
        importer = self.config.debian_runner_root.rstrip("/") + "/scripts/import-source-v6-debian.sh"
        runner = self.config.debian_runner_root.rstrip("/") + "/scripts/import_source_v6_debian.py"
        return "\n".join(
            (
                "set -eu",
                f"pid_file={_shell_quote(job.pid_path)}",
                f"target={_shell_quote(job.remote_db_target)}",
                f"importer={_shell_quote(importer)}",
                f"runner={_shell_quote(runner)}",
                "if [ ! -f \"$pid_file\" ] || [ -L \"$pid_file\" ]; then printf 'FAILED\\n'; exit 0; fi",
                "pid=$(cat -- \"$pid_file\" 2>/dev/null) || { printf 'FAILED\\n'; exit 0; }",
                "case \"$pid\" in ''|*[!0-9]*) printf 'FAILED\\n'; exit 0;; esac",
                "if [ ! -r \"/proc/$pid/cmdline\" ]; then printf 'FAILED\\n'; exit 0; fi",
                "cmdline=$(tr '\\000' ' ' </proc/$pid/cmdline 2>/dev/null || true)",
                "case \"$cmdline\" in *\"$importer\"*\"$target\"*|*\"$runner\"*\"$target\"*) ;; *) printf 'FAILED\\n'; exit 0;; esac",
                "if kill -TERM \"-$pid\" 2>/dev/null; then printf 'CANCELLING\\n'; else printf 'FAILED\\n'; fi",
            )
        )

    @staticmethod
    def _strict_marker(output: object) -> str:
        if not isinstance(output, str):
            raise RemoteSourceDbError("remote source db import failed")
        line = output
        if line.endswith("\n"):
            line = line[:-1]
            if line.endswith("\r"):
                line = line[:-1]
        if not line or "\n" in line or "\r" in line:
            raise RemoteSourceDbError("remote source db import failed")
        return line

    @classmethod
    def _parse_remote_imported(cls, marker: str) -> RemoteDbEvidence:
        match = re.fullmatch(r"REMOTE_IMPORTED[ \t]+([0-9]+)[ \t]+([0-9a-f]{64})", marker)
        if match is None:
            raise RemoteSourceDbError("remote source db import failed")
        try:
            return RemoteDbEvidence(int(match.group(1)), match.group(2))
        except (TypeError, ValueError):
            raise RemoteSourceDbError("remote source db import failed") from None

    @staticmethod
    def _parse_running(marker: str) -> tuple[int, int, int, int]:
        match = re.fullmatch(r"RUNNING[ \t]+([0-9]+)[ \t]+([0-9]+)[ \t]+([0-9]+)[ \t]+([0-9]+)", marker)
        if match is None:
            raise RemoteSourceDbError("remote source db import failed")
        current, total, workers, started_at = (int(item) for item in match.groups())
        if total < current or workers < 1 or started_at < 1:
            raise RemoteSourceDbError("remote source db import failed")
        return current, total, workers, started_at

    @staticmethod
    def _job_document(job: _RemoteImportJob) -> dict[str, object]:
        state = job.state
        phase = {
            "RUNNING": "REMOTE_IMPORT",
            "CANCELLING": "REMOTE_IMPORT_CANCEL",
            "REMOTE_IMPORTED": "REMOTE_IMPORTED",
            "TRANSFERRING": "TRANSFERRING",
            "COMMITTED": "COMMITTED",
            "CANCELLED": "CANCELLED",
            "FAILED": "FAILED",
        }[state]
        if state == "TRANSFERRING":
            current = 0
            if job.transfer_temp is not None:
                try:
                    current = job.transfer_temp.stat().st_size
                except OSError:
                    pass
            progress: dict[str, object] = {
                "current": current,
                "total": job.evidence.size_bytes if job.evidence is not None else 0,
                "unit": "bytes",
            }
        else:
            progress = {
                "current": job.progress_current,
                "total": job.progress_total,
                "unit": "reports" if job.progress_total else "items",
                **({"workers": job.progress_workers} if job.progress_workers else {}),
            }
        document: dict[str, object] = {
            "job_id": job.job_id,
            "state": state,
            "phase": phase,
            "progress": progress,
            "error": None if state != "FAILED" else {"code": "REMOTE_IMPORT_FAILED"},
        }
        if job.started_at_epoch:
            document["timing"] = {
                "started_at_epoch": job.started_at_epoch,
                "elapsed_seconds": max(0, int(time.time()) - job.started_at_epoch),
            }
        if job.transfer_started_at_epoch:
            document.setdefault("timing", {})["stage_elapsed_seconds"] = max(
                0, int(time.time()) - job.transfer_started_at_epoch
            )
        if job.evidence is not None:
            document["evidence"] = job.evidence.as_dict()
        return document

    def _download(self, remote_path: str, temporary: Path) -> None:
        try:
            self._file_downloader(self._pscp_argv(remote_path, temporary))
        except BaseException:
            raise RemoteSourceDbError("remote source db transfer failed") from None

    def _import_script(self, request: RemoteSourceDbRequest) -> str:
        source_root = self._service.source_db_root.rstrip("/") or "/"
        importer = (
            self.config.debian_runner_root.rstrip("/")
            + "/scripts/import-source-v6-debian.sh"
        )
        root = _shell_quote(source_root)
        html = _shell_quote(request.remote_html_path)
        target = _shell_quote(request.remote_db_target)
        importer = _shell_quote(importer)
        return "\n".join(
            (
                "set -eu",
                f"root={root}",
                f"html={html}",
                f"target={target}",
                f"importer={importer}",
                "mkdir -p -- \"$root\" || { printf 'IMPORT_FAILED\\n'; exit 0; }",
                "if [ -e \"$target\" ] || [ -L \"$target\" ]; then printf 'TARGET_EXISTS\\n'; exit 0; fi",
                "if [ ! -f \"$importer\" ] || [ -L \"$importer\" ]; then printf 'IMPORT_FAILED\\n'; exit 0; fi",
                "if ! sh \"$importer\" \"$html\" \"$target\" >/dev/null 2>&1; then printf 'IMPORT_FAILED\\n'; exit 0; fi",
                "if [ ! -f \"$target\" ] || [ -L \"$target\" ]; then printf 'IMPORT_FAILED\\n'; exit 0; fi",
                "if ! size=$(wc -c < \"$target\"); then printf 'IMPORT_FAILED\\n'; exit 0; fi",
                "if ! digest=$(sha256sum -- \"$target\"); then printf 'IMPORT_FAILED\\n'; exit 0; fi",
                "digest=${digest%% *}",
                "printf 'EVIDENCE %s %s\\n' \"$size\" \"$digest\"",
            )
        )

    @staticmethod
    def _parse_evidence(output: object) -> RemoteDbEvidence:
        if not isinstance(output, str):
            raise RemoteSourceDbError("remote source db import failed")
        line = output
        if line.endswith("\n"):
            line = line[:-1]
            if line.endswith("\r"):
                line = line[:-1]
        if "\n" in line or "\r" in line:
            raise RemoteSourceDbError("remote source db import failed")
        if line == "TARGET_EXISTS":
            raise RemoteSourceDbError("remote source db target already exists")
        match = re.fullmatch(r"EVIDENCE[ \t]+([0-9]+)[ \t]+([0-9a-f]{64})", line)
        if match is None:
            raise RemoteSourceDbError("remote source db import failed")
        try:
            return RemoteDbEvidence(int(match.group(1)), match.group(2))
        except (TypeError, ValueError):
            raise RemoteSourceDbError("remote source db import failed") from None

    def _plink_argv(self, script: str) -> tuple[str, ...]:
        argv = ["plink", "-batch", "-ssh", "-P", str(self.config.port)]
        if self.config.password:
            argv.extend(("-pw", self.config.password))
        elif self.config.private_key_path:
            argv.extend(("-i", self.config.private_key_path))
        argv.extend((f"{self.config.user}@{self.config.host}", script))
        return tuple(argv)

    def _pscp_argv(self, remote_path: str, temporary: Path) -> tuple[str, ...]:
        argv = ["pscp", "-batch", "-P", str(self.config.port)]
        if self.config.password:
            argv.extend(("-pw", self.config.password))
        elif self.config.private_key_path:
            argv.extend(("-i", self.config.private_key_path))
        argv.extend((f"{self.config.user}@{self.config.host}:{remote_path}", str(temporary)))
        return tuple(argv)

    @staticmethod
    def _default_command_runner(argv: tuple[str, ...]) -> str:
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                shell=False,
            )
        except BaseException:
            raise RemoteSourceDbError("remote command failed") from None
        if completed.returncode != 0:
            raise RemoteSourceDbError("remote command failed")
        return completed.stdout

    @staticmethod
    def _default_file_downloader(argv: tuple[str, ...]) -> None:
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                shell=False,
            )
        except BaseException:
            raise RemoteSourceDbError("remote file download failed") from None
        if completed.returncode != 0:
            raise RemoteSourceDbError("remote file download failed")

    @staticmethod
    def _default_transfer_starter(argv: tuple[str, ...]) -> object:
        try:
            return subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
        except BaseException:
            raise RemoteSourceDbError("remote file download failed") from None
